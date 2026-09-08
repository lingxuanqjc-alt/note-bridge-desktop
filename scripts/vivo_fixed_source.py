"""Capture only the original M2 and V1 Vivo seeds for the remaining OPPO direction."""
import hashlib
import json
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from huawei_scoped_source import fixture
from vivo_scoped_readback import DIVIDER_DOWNGRADE_WARNING, read_notes, verify_account
from xiaomi_browser_scope import _module, _ReadOnlyStore

from note_bridge.errors import BridgeError
from note_bridge.models import NoteDocument
from note_bridge.operations import receipt_key
from note_bridge.paths import confined
from note_bridge.providers.factory import create_provider
from note_bridge.providers.vivo_groups import folder_key
from note_bridge.richtext import DEFAULT_LAYOUT_WARNING

MANIFESTS = ('vivo-group-M2-job.json', 'vivo-complex-V1-job.json')
FIXTURES = ('fixture-20260906-vivo-group-M2', 'fixture-20260906-vivo-complex-V1')
OPERATION = 'vivo_fixed_source_capture'
PATH_PATTERN = r'\.private/evidence/vivo-fixed-source-[0-9a-f]{32}/proof\.json'


def require(condition, code='invalid_vivo_fixed_source'):
    if not condition:
        raise BridgeError(code, 'vivo 来源只允许原始 M2、V1 的精确已确认样例，未读取其他笔记。')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def bindings(store, root, fixture_builder=None):
    refs, seeds = [], []
    for index, (name, identifier) in enumerate(zip(MANIFESTS, FIXTURES, strict=True)):
        raw = (root / '.private/checkpoints' / name).read_bytes()
        old_raw = (root / '.private/evidence' / (identifier + '.json')).read_bytes()
        job, old = json.loads(raw), json.loads(old_raw)
        require(job.get('kind') == 'independent-cloud-write-smoke' and job.get('id') == identifier
                and job.get('armed') is False and job.get('target') == 'vivo' and job.get('from_cloud_fixture') is None
                and isinstance(job.get('consumed_at'), str) and bool(job['consumed_at'])
                and job.get('with_images') is True and job.get('with_group') is True
                and (job.get('content_case') == 'complex') is bool(index)
                and re.fullmatch(r'[0-9a-f]{64}', str(job.get('expected_account', ''))))
        require(old.get('kind') == job['kind'] and old.get('platform') == 'vivo' and old.get('fixture_id') == identifier
                and old.get('status') in ('verified', 'verified_with_degradation') and old.get('formal_acceptance') is False
                and all(old.get(k) is True for k in ('receipt_confirmed', 'group_mapping_verified',
                         'image_bytes_verified', 'content_and_style_verified'))
                and old.get('expected_images') == old.get('restored_images') == 2
                and old.get('cloud_resource_receipt_states') == ['linked', 'linked'])
        seed = fixture_builder(job) if fixture_builder else fixture(root, job)
        account = job['expected_account']
        require(seed.source_id == identifier and seed.account_id == 'synthetic-fixture-source'
                and len(seed.attachments) == 2 and all(a.kind == 'image' for a in seed.attachments))
        key = receipt_key(seed, SimpleNamespace(spec=SimpleNamespace(id='vivo'), account_id=account))
        receipt, group = store.receipt(key), store.receipt(folder_key(seed, account))
        resources = store.resource_receipts(key)
        require(receipt and receipt['status'] == 'confirmed' and len(receipt['remote_ids']) == 1
                and re.fullmatch(r'[0-9a-f]{32}', str(receipt['remote_ids'][0]))
                and group and group['status'] == 'confirmed' and len(group['remote_ids']) == 1
                and len(resources) == 2 and all(r['status'] == 'linked' for r in resources))
        refs.append(dict(manifest=name, manifest_sha256=digest(raw), evidence_sha256=digest(old_raw),
            account=account, source_id=receipt['remote_ids'][0], receipt_key=key, group_id=group['remote_ids'][0],
            asset_ids=sorted(r['resource_id'] for r in resources)))
        seeds.append(seed)
    require(refs[0]['account'] == refs[1]['account'] and refs[0]['source_id'] != refs[1]['source_id'])
    return refs, seeds


def check_note(note, ref, seed, root, resource_keys, diagnostics=None):
    checks = diagnostics if diagnostics is not None else {}
    checks.update(identity=(note.platform, note.account_id, note.source_id) == ('vivo', ref['account'], ref['source_id']),
        group=(note.source_folder_id, note.source_folder_name) == (ref['group_id'], seed.source_folder_name),
        warnings=set(note.warnings) <= {DEFAULT_LAYOUT_WARNING, DIVIDER_DOWNGRADE_WARNING},
        resources=isinstance(resource_keys, dict) and len(note.attachments) == 2
            and set(resource_keys) == {a.id for a in note.attachments}
            and all(isinstance(value, str) and value for value in resource_keys.values())
            and sorted(resource_keys.values()) == ref['asset_ids'])
    require(all(checks.values()))
    checks['original_bytes'] = True
    for asset in note.attachments:
        path = confined(root / '.private/session-lab/resources', asset.local_path or '')
        checks['original_bytes'] = checks['original_bytes'] and (asset.kind == 'image' and path.is_file()
            and path.stat().st_size == asset.size and digest(path.read_bytes()) == asset.sha256)
    require(checks['original_bytes'])
    checks['content_and_originals'] = _module(root, 'live-matrix-job').compare_note(seed, note, [])
    require(checks['content_and_originals'])


def capture(jars, root):
    capture_id = 'vivo-fixed-source-' + uuid4().hex
    directory = root / '.private/evidence' / capture_id
    directory.mkdir(parents=True)
    report = dict(kind=OPERATION, status='started', scope_complete=False, whole_account_snapshot=False,
        formal_acceptance=False, cloud_writes=0, cache_writes=0, receipt_writes=0,
        captured_started_at=datetime.now(timezone.utc).isoformat())
    provider, progress, issues = None, {}, []
    context = SimpleNamespace(cancelled=threading.Event(), check_cancel=lambda: None,
        update=lambda **changes: progress.update(changes),
        issue=lambda code, message, note_id='': issues.append({'code': code, 'note_id': note_id}))
    try:
        database = root / '.private/session-lab/notes.sqlite'
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            refs, seeds = bindings(_ReadOnlyStore(db), root)
        provider = create_provider('vivo', jars, root / '.private/session-lab/resources/scoped-sources' / capture_id)
        require(verify_account(provider) == refs[0]['account'], 'account_changed')
        exact_ids = tuple(r['source_id'] for r in refs)
        acquired = read_notes(provider, exact_ids, context)
        require(acquired.complete is True and acquired.account_id == refs[0]['account']
                and acquired.exact_ids == exact_ids and len(acquired.notes) == 2
                and verify_account(provider) == refs[0]['account'], 'incomplete_scoped_read')
        require(all((note.platform, note.account_id, note.source_id) == ('vivo', ref['account'], ref['source_id'])
                    for note, ref in zip(acquired.notes, refs, strict=True)))
        require(set(acquired.resource_keys) == set(exact_ids)
                and all(isinstance(mapping, dict) and all(isinstance(k, str) and isinstance(v, str) and k and v
                        for k, v in mapping.items()) for mapping in acquired.resource_keys.values()))
        for note in acquired.notes:
            for asset in note.attachments:
                require(asset.local_path is not None)
                asset.local_path = f'scoped-sources/{capture_id}/' + asset.local_path
                require(confined(root / '.private/session-lab/resources', asset.local_path).is_relative_to(
                    root / '.private/session-lab/resources/scoped-sources' / capture_id))
        # Persist only the two account/ID-bound acquired documents. A failed proof
        # remains unusable, but content checks can be diagnosed without another read.
        raw = json.dumps([n.model_dump(mode='json') for n in acquired.notes], ensure_ascii=False).encode()
        with (directory / 'notes.json').open('xb') as output:
            output.write(raw)
        report.update(bindings=refs, resource_keys=acquired.resource_keys,
            notes_sha256=digest(raw), fingerprints={n.source_id: n.fingerprint() for n in acquired.notes}, checks=[])
        for note, ref, seed in zip(acquired.notes, refs, seeds, strict=True):
            checks = {}
            report['checks'].append({'manifest': ref['manifest'], 'checks': checks})
            check_note(note, ref, seed, root, acquired.resource_keys[note.source_id], checks)
        require(progress.get('total') == progress.get('completed') == progress.get('succeeded') == 2
                and not ({i['code'] for i in issues} - {'source_warning'}))
        report.update(status='captured', scope_complete=True)
    except BridgeError as error:
        report.update(status='blocked', code=error.code)
    except Exception as error:
        report.update(status='failed', code='scoped_capture_error', error_type=type(error).__name__)
    finally:
        if provider is not None:
            provider.close()
        report.update(captured_finished_at=datetime.now(timezone.utc).isoformat(),
            completed=progress.get('completed', 0), total=progress.get('total', 2), issues=issues)
        with (directory / 'proof.json').open('x', encoding='utf-8') as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
    return {k: report[k] for k in ('kind', 'status', 'scope_complete', 'whole_account_snapshot',
            'formal_acceptance', 'cloud_writes', 'cache_writes', 'receipt_writes', 'completed', 'total')} | {
        'proof': {'path': (directory / 'proof.json').relative_to(root).as_posix(),
                  'sha256': digest((directory / 'proof.json').read_bytes())},
        **({'code': report['code']} if 'code' in report else {})}


def verify_source(reference, store, root, fixture_builder=None):
    require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'}
            and re.fullmatch(PATH_PATTERN, str(reference.get('path', '')))
            and re.fullmatch(r'[0-9a-f]{64}', str(reference.get('sha256', ''))))
    path = confined(root, reference['path'])
    raw = path.read_bytes()
    require(digest(raw) == reference['sha256'])
    proof = json.loads(raw)
    require(proof.get('kind') == OPERATION and proof.get('status') == 'captured'
            and proof.get('scope_complete') is True and proof.get('whole_account_snapshot') is False
            and proof.get('formal_acceptance') is False and proof.get('completed') == proof.get('total') == 2
            and all(type(proof.get(k)) is int and proof[k] == 0 for k in ('cloud_writes', 'cache_writes', 'receipt_writes')))
    start, end = (datetime.fromisoformat(proof[k]) for k in ('captured_started_at', 'captured_finished_at'))
    require(start.tzinfo and end.tzinfo and start <= end <= datetime.now(timezone.utc))
    refs, seeds = bindings(store, root, fixture_builder)
    require(proof.get('bindings') == refs)
    raw = path.with_name('notes.json').read_bytes()
    require(digest(raw) == proof.get('notes_sha256'))
    notes = [NoteDocument.model_validate(value) for value in json.loads(raw)]
    require(len(notes) == 2 and proof.get('fingerprints') == {n.source_id: n.fingerprint() for n in notes})
    require(isinstance(proof.get('resource_keys'), dict) and set(proof['resource_keys']) == {r['source_id'] for r in refs})
    for note, ref, seed in zip(notes, refs, seeds, strict=True):
        require(all(a.local_path and a.local_path.startswith(f'scoped-sources/{path.parent.name}/') for a in note.attachments))
        check_note(note, ref, seed, root, proof['resource_keys'][note.source_id])
    return notes

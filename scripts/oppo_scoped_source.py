"""Read the two original OPPO fixtures once for the six OPPO outbound batches."""
import hashlib
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from huawei_scoped_source import fixture
from xiaomi_browser_scope import _module, _ReadOnlyStore

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, NoteDocument
from note_bridge.operations import receipt_key
from note_bridge.paths import confined
from note_bridge.providers.factory import create_provider
from note_bridge.providers.oppo import parse_entry
from note_bridge.providers.oppo_groups import DEFAULT_GROUP, folder_key

MANIFESTS = ('fixture-20260906-oppo-image-F2-consumed-job.json', 'oppo-complex-OR1-job.json')
CONSUMED = ('fixture-20260906-oppo-image-F2-consumed-job.json',
            'fixture-20260907-oppo-complex-OR1-consumed-job.json')
FIXTURES = ('fixture-20260906-oppo-image-F2', 'fixture-20260907-oppo-complex-OR1')
OPERATION = 'oppo_fixed_source_capture'
PATH_PATTERN = r'\.private/evidence/oppo-fixed-source-[0-9a-f]{32}/proof\.json'
PREFIX = '/owork-server/web/note/v2/'


def require(condition, code='invalid_oppo_source_scope'):
    if not condition:
        raise BridgeError(code, 'OPPO 固定 F2、OR1 来源的范围、回执或内容未通过核对。')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def bindings(store, root, fixture_builder=None):
    refs, seeds = [], []
    for index, (name, identifier) in enumerate(zip(MANIFESTS, FIXTURES, strict=True)):
        raw = (root / '.private/checkpoints' / name).read_bytes()
        evidence_raw = (root / '.private/evidence' / (identifier + '.json')).read_bytes()
        job, old = json.loads(raw), json.loads(evidence_raw)
        consumed_raw = (root / '.private/checkpoints' / CONSUMED[index]).read_bytes()
        consumed = json.loads(consumed_raw)
        require(isinstance(consumed.get('consumed_at'), str) and bool(consumed['consumed_at'])
                and {k: v for k, v in consumed.items() if k != 'consumed_at'} ==
                    {k: v for k, v in job.items() if k != 'consumed_at'})
        require(job.get('kind') == 'independent-cloud-write-smoke' and job.get('id') == identifier
                and job.get('target') == 'oppo' and job.get('armed') is False
                and job.get('from_cloud_fixture') is None and job.get('with_images') is True
                and bool(job.get('with_group')) is bool(index)
                and (job.get('content_case') == 'complex') is bool(index)
                and re.fullmatch(r'[0-9a-f]{64}', str(job.get('expected_account', ''))))
        require(old.get('kind') == job['kind'] and old.get('platform') == 'oppo'
                and old.get('fixture_id') == identifier and old.get('status') in ('verified', 'verified_with_degradation')
                and old.get('formal_acceptance') is False
                and all(old.get(k) is True for k in ('receipt_confirmed', 'image_bytes_verified',
                                                    'original_notes_unchanged', 'content_and_style_verified'))
                and old.get('expected_images') == old.get('restored_images') == 2)
        if index:
            require(old.get('group_mapping_verified') is True
                    and old.get('comparison_policy') == 'strict_matrix_expectations')
        seed = fixture_builder(job) if fixture_builder else fixture(root, job)
        account = job['expected_account']
        require(seed.account_id == 'synthetic-fixture-source' and seed.source_id == identifier
                and len(seed.attachments) == 2 and all(a.kind == 'image' for a in seed.attachments)
                and seed.display_title.startswith('笔记互迁验收 · '))
        key = receipt_key(seed, SimpleNamespace(spec=SimpleNamespace(id='oppo'), account_id=account))
        receipt, resources = store.receipt(key), store.resource_receipts(key)
        require(receipt and receipt['status'] == 'confirmed' and len(receipt['remote_ids']) == 1
                and re.fullmatch(r'[A-Za-z0-9_-]{1,150}', str(receipt['remote_ids'][0]))
                and 4 <= len(resources) <= 6 and all(r['status'] == 'linked' for r in resources)
                and old.get('cloud_resource_receipt_states') == ['linked'] * len(resources))
        names = [r['resource_id'] for r in resources]
        cloud_ids = sorted(n for n in names if re.fullmatch(r'[A-Za-z0-9_-]{1,150}', n))
        prepared = [n for n in names if re.fullmatch(r'prepare/[0-9a-f]{32}', n)]
        # applyId is an opaque upload receipt, not a download path; preserve the vendor value.
        applied = [n for n in names if n.startswith('apply/') and n[6:]]
        require(len(cloud_ids) == len(prepared) == 2 and len(applied) <= 2
                and len(cloud_ids) + len(prepared) + len(applied) == len(names))
        group = store.receipt(folder_key(seed, account))
        if index:
            require(group and group['status'] == 'confirmed' and len(group['remote_ids']) == 1
                    and group['remote_ids'][0] != DEFAULT_GROUP)
            group_id = group['remote_ids'][0]
        else:
            require(not seed.source_folder_id and not seed.source_folder_name and group is None)
            group_id = DEFAULT_GROUP
        refs.append({'manifest': name, 'manifest_sha256': digest(raw), 'evidence_sha256': digest(evidence_raw),
            'consumed_manifest': CONSUMED[index], 'consumed_sha256': digest(consumed_raw),
            'account': account, 'source_id': receipt['remote_ids'][0], 'receipt_key': key,
            'group_id': group_id, 'group_policy': 'named' if index else 'default', 'cloud_ids': cloud_ids,
            'resources': sorted(resources, key=lambda r: r['resource_id'])})
        seeds.append(seed)
    require(refs[0]['account'] == refs[1]['account'] and refs[0]['source_id'] != refs[1]['source_id'])
    return refs, seeds


def check_note(note, ref, seed, root):
    require((note.platform, note.account_id, note.source_id, note.source_folder_id) ==
            ('oppo', ref['account'], ref['source_id'], ref['group_id']) and not note.warnings
            and len(note.attachments) == 2 and sorted(a.name for a in note.attachments) == ref['cloud_ids'])
    if ref['group_policy'] == 'named':
        require(note.source_folder_name == seed.source_folder_name)
    for asset in note.attachments:
        path = confined(root / '.private/session-lab/resources', asset.local_path or '')
        require(asset.kind == 'image' and path.is_file() and path.stat().st_size == asset.size
                and digest(path.read_bytes()) == asset.sha256)
    require(_module(root, 'live-matrix-job').compare_note(seed, note, []))


@contextmanager
def read_guard(provider, refs, downloads, counts):
    """Allow only identity, groups, the exact two info bodies, and their receipt-bound files."""
    original_json, original_request = provider._json, provider.transport.session.request
    identities = {r['source_id'] for r in refs}
    permitted = {'key': None, 'path': None, 'params': None}
    logical = {}

    def read(path, data=None, **kwargs):
        require(not kwargs.get('write') and not kwargs.get('files'))
        if path == '/web/note/v2/group-list-new':
            require(data in (None, {}))
            key = 'groups'
        else:
            require(path == '/web/note/v2/info' and isinstance(data, dict)
                    and data.get('recordId') in identities
                    and data == {'recordId': data['recordId'], 'status': 0, 'version': ''}
                    and kwargs.get('params') == {'recordId': data['recordId'], 'version': ''})
            key = 'info:' + data['recordId']
        require(logical.get(key, 0) < 2, 'scoped_request_limit')
        logical[key] = logical.get(key, 0) + 1
        permitted['key'] = key + ':' + str(logical[key])
        permitted['path'], permitted['params'] = '/owork-server' + path, kwargs.get('params')
        try:
            return original_json(path, data, **kwargs)
        finally:
            permitted.update(key=None, path=None, params=None)

    def request(method, url, **kwargs):
        parsed = urlsplit(url)
        require(parsed.scheme == 'https' and parsed.hostname == 'owork-api-cn.oppo.com'
                and not parsed.query and not kwargs.get('files') and not kwargs.get('allow_redirects'))
        if method == 'GET':
            require(not any(kwargs.get(k) for k in ('params', 'json', 'data')))
        path = parsed.path
        if method == 'GET' and path == '/owork-server/web/account/v1/userInfo':
            key, limit = 'identity', 2
        elif method == 'POST' and path in (PREFIX + 'group-list-new', PREFIX + 'info'):
            require(permitted['key'] is not None and isinstance(kwargs.get('json'), dict)
                    and set(kwargs['json']) == {'key', 'iv', 'encryptContent'}
                    and path == permitted['path'] and kwargs.get('params') == permitted['params'])
            key, limit = permitted['key'], 1
        else:
            require(method == 'GET' and path in downloads)
            key, limit = path, 1
        require(counts.get(key, 0) < limit, 'scoped_request_limit')
        counts[key] = counts.get(key, 0) + 1
        return original_request(method, url, **kwargs)

    provider._json, provider.transport.session.request = read, request
    try:
        yield
    finally:
        provider._json, provider.transport.session.request = original_json, original_request


def acquire(provider, refs, seeds, root, capture_id, counts, group_shapes=None):
    groups_seen, original_probe_json = [], provider._json

    def observe(path, *args, **kwargs):
        result = original_probe_json(path, *args, **kwargs)
        if path == '/web/note/v2/group-list-new':
            require(isinstance(result, list) and all(isinstance(r, dict) and isinstance(r.get('groupGuid'), str)
                    for r in result), 'oppo_group_list_shape')
            # Project system/unrelated rows just as OppoProvider.fetch does.
            # Only the selected named group needs unambiguous original names;
            # repeated default/other rows cannot broaden the two-note scope.
            rows_by_id = {}
            for row in result:
                if row['groupGuid']:
                    rows_by_id.setdefault(row['groupGuid'], []).append(row.get('groupName', ''))
            selected = {ref['group_id'] for ref in refs}
            if group_shapes is not None:
                group_shapes.append({'rows': len(result),
                    'selected_duplicate_rows': sum(len(names) - 1 for identity, names in rows_by_id.items() if identity in selected),
                    'other_duplicate_rows': sum(len(names) - 1 for identity, names in rows_by_id.items() if identity not in selected),
                    'selected': [{'index': index, 'rows': len(rows_by_id.get(ref['group_id'], [])),
                        'name_types': dict(Counter(type(name).__name__ for name in rows_by_id.get(ref['group_id'], []))),
                        'distinct_name_values': len({json.dumps(name, sort_keys=True) for name in rows_by_id.get(ref['group_id'], [])})}
                        for index, ref in enumerate(refs)]})
            require(all(ref['group_policy'] != 'named' or (rows_by_id.get(ref['group_id'])
                        and all(name == seed.source_folder_name for name in rows_by_id[ref['group_id']]))
                        for ref, seed in zip(refs, seeds, strict=True)), 'oppo_named_group_mismatch')
            groups = {r['groupGuid']: r.get('groupName', '') for r in result if r['groupGuid']}
            groups_seen.append(groups)
        return result

    provider._json = observe
    downloads = {PREFIX + 'file-download/' + cloud_id for ref in refs for cloud_id in ref['cloud_ids']}
    notes, versions, metadata, downloaded = [], {}, {}, {}
    context = SimpleNamespace(check_cancel=lambda: None, cancelled=SimpleNamespace(wait=lambda _: False))
    try:
        with read_guard(provider, refs, downloads, counts):
            require(provider.probe() == refs[0]['account'], 'account_changed')
            groups = groups_seen[-1]
            for ref, seed in zip(refs, seeds, strict=True):
                identifier = ref['source_id']
                detail = provider._json('/web/note/v2/info', {'recordId': identifier, 'status': 0, 'version': ''},
                                       params={'recordId': identifier, 'version': ''})
                require(isinstance(detail, dict) and detail.get('recordId') == identifier and detail.get('status') == 0
                        and isinstance(detail.get('version'), str) and bool(detail['version'])
                        and detail.get('groupGuid') == ref['group_id'])
                rows = detail.get('attachments')
                require(isinstance(rows, list) and len(rows) == 2 and all(isinstance(r, dict)
                        and r.get('type') == 0 and isinstance(r.get('url'), str) and r.get('id') for r in rows)
                        and len({str(r['id']) for r in rows}) == 2
                        and sorted(r['url'].removeprefix('/') for r in rows) == ref['cloud_ids'])
                assets = []
                for row in rows:
                    cloud_id = row['url'].removeprefix('/')
                    if cloud_id not in downloaded:
                        asset = Attachment(id=str(row['id']), name=cloud_id, kind='image')
                        provider._files.download(asset, cloud_id, context)
                        asset.local_path = f'scoped-sources/{capture_id}/' + asset.local_path
                        require(sum((a.kind, a.size, a.sha256) == (asset.kind, asset.size, asset.sha256)
                                    for a in seed.attachments) == 1)
                        downloaded[cloud_id] = asset
                    assets.append(downloaded[cloud_id].model_copy(update={'id': str(row['id'])}))
                note = parse_entry(detail, ref['account'], groups, assets)
                check_note(note, ref, seed, root)
                notes.append(note)
                versions[identifier] = detail['version']
                metadata[identifier] = digest(json.dumps(detail, sort_keys=True, ensure_ascii=False).encode())
            require(provider.probe() == refs[0]['account'], 'account_changed')
            require(all((groups_seen[-1].get(r['group_id']) or '') == (groups.get(r['group_id']) or '') for r in refs))
            # Use the already implemented full-info query; do not invent a conditional version response.
            for ref in refs:
                identifier = ref['source_id']
                detail = provider._json('/web/note/v2/info', {'recordId': identifier, 'status': 0, 'version': ''},
                                       params={'recordId': identifier, 'version': ''})
                require(isinstance(detail, dict) and detail.get('recordId') == identifier
                        and detail.get('version') == versions[identifier]
                        and digest(json.dumps(detail, sort_keys=True, ensure_ascii=False).encode()) == metadata[identifier])
        return notes, versions, metadata
    finally:
        provider._json = original_probe_json


def capture(jars, root):
    capture_id = 'oppo-fixed-source-' + uuid4().hex
    directory = root / '.private/evidence' / capture_id
    directory.mkdir(parents=True)
    report = dict(kind=OPERATION, status='started', scope_complete=False, whole_account_snapshot=False,
        formal_acceptance=False, cloud_writes=0, cache_writes=0, receipt_writes=0,
        captured_started_at=datetime.now(timezone.utc).isoformat())
    provider, counts, group_shapes = None, {}, []
    try:
        database = root / '.private/session-lab/notes.sqlite'
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            refs, seeds = bindings(_ReadOnlyStore(db), root)
        provider = create_provider('oppo', jars, root / '.private/session-lab/resources/scoped-sources' / capture_id)
        notes, versions, metadata = acquire(provider, refs, seeds, root, capture_id, counts, group_shapes)
        raw = json.dumps([n.model_dump(mode='json') for n in notes], ensure_ascii=False).encode()
        with (directory / 'notes.json').open('xb') as output:
            output.write(raw)
        report.update(status='captured', scope_complete=True, bindings=refs, versions=versions,
            detail_sha256=metadata, notes_sha256=digest(raw), fingerprints={n.source_id: n.fingerprint() for n in notes})
    except BridgeError as error:
        report.update(status='blocked', code=error.code)
    except Exception as error:
        report.update(status='failed', code='scoped_capture_error', error_type=type(error).__name__)
    finally:
        if provider is not None:
            provider.close()
        report.update(captured_finished_at=datetime.now(timezone.utc).isoformat(), physical_requests=sum(counts.values()),
                      group_shapes=group_shapes)
        with (directory / 'proof.json').open('x', encoding='utf-8') as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
    return {k: report[k] for k in ('kind', 'status', 'scope_complete', 'whole_account_snapshot',
            'formal_acceptance', 'cloud_writes', 'cache_writes', 'receipt_writes', 'physical_requests', 'group_shapes')} | {
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
            and proof.get('formal_acceptance') is False
            and all(type(proof.get(k)) is int and proof[k] == 0 for k in ('cloud_writes', 'cache_writes', 'receipt_writes')))
    start, end = (datetime.fromisoformat(proof[k]) for k in ('captured_started_at', 'captured_finished_at'))
    require(start.tzinfo and end.tzinfo and start <= end <= datetime.now(timezone.utc))
    refs, seeds = bindings(store, root, fixture_builder)
    require(proof.get('bindings') == refs)
    raw = path.with_name('notes.json').read_bytes()
    require(digest(raw) == proof.get('notes_sha256'))
    notes = [NoteDocument.model_validate(value) for value in json.loads(raw)]
    ids = {r['source_id'] for r in refs}
    require(len(notes) == 2 and proof.get('fingerprints') == {n.source_id: n.fingerprint() for n in notes}
            and set(proof.get('versions', {})) == set(proof.get('detail_sha256', {})) == ids
            and all(isinstance(v, str) and v for v in proof['versions'].values())
            and all(re.fullmatch(r'[0-9a-f]{64}', str(v)) for v in proof['detail_sha256'].values()))
    for note, ref, seed in zip(notes, refs, seeds, strict=True):
        require(all(a.local_path and a.local_path.startswith(f'scoped-sources/{path.parent.name}/') for a in note.attachments))
        check_note(note, ref, seed, root)
    return notes

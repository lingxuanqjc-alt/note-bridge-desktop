"""Read-only native scopes for confirmed Huawei targets; legacy entries stay explicit."""
import hashlib
import io
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

from cloud_fixture_source import select_source
from fetch_scope import task_summary
from huawei_legacy_native_scope import SCOPES as LEGACY_SCOPES
from huawei_legacy_native_scope import browser_target as legacy_target
from huawei_scoped_source import fixture
from PIL import Image
from xiaomi_browser_scope import _module, _ReadOnlyStore

from note_bridge.models import NoteDocument
from note_bridge.operations import receipt_key
from note_bridge.paths import confined
from note_bridge.providers.huawei_groups import folder_key
from note_bridge.receipt_identity import FIELDS, migration_identity

SCOPES = {'wps-huawei-BD18-job.json': (0,), 'wps-huawei-BD20-job.json': (0,),
          'honor-huawei-BD19-job.json': (0, 1)}
RECOVERY = {'wps-huawei-BD18-job.json': 'BD18-confirmed-entry-readback.json',
            'honor-huawei-BD19-job.json': 'BD19-confirmed-entries-readback.json'}


def require(condition):
    if not condition:
        raise ValueError('huawei_native_scope_unverified')


def render_expectations(note):
    def runs(blocks):
        return [{'text': span.text, **{key: getattr(span, key) for key in
                 ('bold', 'italic', 'underline', 'strike', 'highlight', 'code', 'link')}}
                for block in blocks if block.kind != 'attachment' for span in block.spans]

    expected = {'expectedText': note.plain_text, 'expectedRuns': runs(note.blocks)}
    # The official edited-title loader removes exactly the first element and renders
    # it in the separate title editor. Keep the complete cached model unchanged.
    if len(note.blocks) > 1 and note.blocks[0].kind == 'paragraph' and note.blocks[0].text == note.display_title:
        body = note.model_copy(update={'blocks': note.blocks[1:]})
        expected['editedTitleProjection'] = {'firstBlockKind': 'paragraph', 'firstBlockText': note.blocks[0].text,
            'titleRuns': runs(note.blocks[:1]), 'expectedText': body.plain_text, 'expectedRuns': runs(body.blocks)}
    return expected


def browser_target(scope, root, account):
    name, index = scope.get('manifest'), scope.get('index')
    if name == 'oppo-huawei-BI4-job.json':
        # Lazy import: BI4 uses this module's title projection after validating its
        # independent readback, without substituting the incomplete account cache.
        from huawei_bi4_readback import browser_target as bi4_target

        return bi4_target(scope, root, account)
    require('readback_proof' not in scope)
    if name in LEGACY_SCOPES:
        return legacy_target(scope, root, account)
    require(name in SCOPES and type(index) is int and index in SCOPES[name])
    base = root / '.private'
    manifest_path = base / 'checkpoints' / name
    manifest = json.loads(manifest_path.read_text('utf-8'))
    require(manifest.get('kind') == 'cloud-matrix-batch' and manifest.get('target') == 'huawei'
            and manifest.get('armed') is False and manifest.get('expected_account') == account
            and manifest.get('source_policy') == 'direct_seed_only'
            and manifest.get('id') == 'matrix-20260907-' + name.removesuffix('-job.json'))
    evidence_path = base / 'evidence' / (manifest['id'] + '.json')
    evidence = json.loads(evidence_path.read_text('utf-8'))
    require(evidence.get('batch_id') == manifest['id'] and evidence.get('target') == 'huawei')
    entry = manifest['sources'][index]
    paths = [manifest_path, evidence_path]
    with closing(sqlite3.connect((base / 'session-lab/notes.sqlite').resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        store = _ReadOnlyStore(db)
        source = select_source({**entry, 'target': 'huawei'}, store, root, lambda job: fixture(root, job))
        destination = SimpleNamespace(spec=SimpleNamespace(id='huawei'), account_id=account)
        key = receipt_key(source, destination)
        receipt = store.receipt(key)
        require(receipt and receipt['status'] == 'confirmed' and len(receipt['remote_ids']) == 1)
        identity = migration_identity(source, destination)
        context = db.execute('SELECT ' + ','.join(FIELDS) + ' FROM receipt_contexts WHERE key=?', (key,)).fetchone()
        require(context is not None and tuple(context) == tuple(identity[field] for field in FIELDS))
        require(store.snapshot('huawei', account).get('complete') is True)
        actual_row = db.execute('SELECT document FROM notes WHERE platform=? AND account=? AND source_id=?',
                                ('huawei', account, receipt['remote_ids'][0])).fetchone()
        require(actual_row is not None)
        actual = NoteDocument.model_validate_json(actual_row[0])
        require(actual.platform == 'huawei' and actual.account_id == account and actual.source_id == receipt['remote_ids'][0])
        warnings = [issue['message'] for issue in evidence.get('issues', [])
                    if issue.get('note_id') == source.source_id and issue.get('code') == 'format_downgrade']
        require(_module(root, 'live-matrix-job').compare_note(source, actual, warnings))
        group = store.receipt(folder_key(source, account))
        require(group and group['status'] == 'confirmed' and group['remote_ids'] == [actual.source_folder_id]
                and actual.source_folder_name and actual.display_title.startswith('笔记互迁验收 · '))
        resources = store.resource_receipts(key)
        require(len(resources) == 4 and all(r['status'] == 'linked' for r in resources))
        if name in RECOVERY:
            proof_path = base / 'evidence' / RECOVERY[name]
            proof = json.loads(proof_path.read_text('utf-8'))
            require(evidence.get('status') == 'needs_review' and proof.get('batch_id') == manifest['id']
                    and proof.get('formal_acceptance') is False and proof.get('cloud_writes') == 0)
            if name.startswith('wps'):
                require(proof.get('status') == 'confirmed_entry_verified_remainder_pending' and index == 0
                        and all(proof.get(k) is True for k in ('original_18_unchanged', 'only_confirmed_addition',
                                                             'content_verified', 'group_verified'))
                        and proof.get('image_originals_verified') == 2)
            else:
                require(proof.get('status') == 'target_entries_verified_remaining_checks_pending'
                        and proof.get('original_19_unchanged') is True and proof.get('only_confirmed_additions') is True)
                item = proof['items'][index]
                require(item.get('entry_index') == index and item.get('receipt_status') == 'confirmed'
                        and item.get('content_verified') is True and item.get('group_verified') is True
                        and item.get('image_originals_verified') == 2)
            fetch_path = base / 'evidence' / ('fetch-' + proof['task_id'] + '-scope.json')
            hashes = {'manifest': manifest_path, 'original_evidence': evidence_path,
                      'baseline': base / 'checkpoints' / (manifest['id'] + '-before.json'), 'fetch_scope': fetch_path}
            require(proof.get('file_sha256') == {key: hashlib.sha256(path.read_bytes()).hexdigest()
                                               for key, path in hashes.items()})
            fetch = json.loads(fetch_path.read_text('utf-8'))
            task = store.task(proof['task_id'])
            require(task and task.status == 'succeeded' and fetch.get('task') == task_summary(task)
                    and fetch.get('account') == account and fetch.get('platform') == 'huawei'
                    and fetch.get('notes', {}).get(actual.source_id) == actual.fingerprint())
            paths.extend([proof_path, *hashes.values()])
        else:
            require(evidence.get('status') == 'api_verified' and evidence.get('original_target_notes_unchanged') is True
                    and evidence.get('only_confirmed_additions') is True)
            item = evidence['items'][index]
            require(item.get('source_id') == source.source_id and item.get('source_fingerprint') == source.fingerprint()
                    and item.get('status') == 'api_verified' and item.get('receipt_status') == 'confirmed'
                    and item.get('content_verified') is True and item.get('group_verified') is True)
        images = []
        for asset in actual.attachments:
            path = confined(base / 'session-lab/resources', asset.local_path or '')
            data = path.read_bytes()
            require(asset.kind == 'image' and len(data) == asset.size and hashlib.sha256(data).hexdigest() == asset.sha256)
            with Image.open(io.BytesIO(data)) as picture:
                width, height = picture.size
                picture.verify()
            images.append({'width': width, 'height': height})
        require(len(images) == 2 and 0 < len(actual.plain_text) <= 100000 and not actual.warnings)
        require(all(b.kind in ('paragraph', 'heading', 'list', 'todo', 'quote', 'code', 'divider', 'attachment') for b in actual.blocks))
        return {'scopeVersion': 1, 'scopeLabel': name.removesuffix('-job.json') + ':' + str(index),
                'fixtureId': actual.source_id, 'group_id': actual.source_folder_id,
                'group_name': actual.source_folder_name, 'title': actual.display_title,
                **render_expectations(actual), 'image_specs': images,
                'target_fingerprint': actual.fingerprint(), 'source_degradation_count': len(warnings),
                'evidence_sha256': {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                    for path in dict.fromkeys(paths)}}

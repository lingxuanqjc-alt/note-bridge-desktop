"""Seven historical Huawei targets, read-only; never a migration source selector."""
import hashlib
import io
import json
import sqlite3
from contextlib import closing

from huawei_scoped_source import fixture as image_fixture
from PIL import Image
from xiaomi_browser_scope import _module, _ReadOnlyStore

from note_bridge.models import NoteDocument
from note_bridge.paths import confined
from note_bridge.providers.huawei_groups import folder_key
from note_bridge.receipt_identity import FIELDS, identity_key

SCOPES = {'xiaomi-huawei-BC3-job.json': (0, 1, 2),
          'meizu-huawei-AA2-job.json': (0, 1, 2), 'meizu-huawei-AA5-job.json': (0,)}
BATCHES = {'xiaomi-huawei-BC3-job.json': ('matrix-20260907-xiaomi-huawei-BC3', 14, 4),
           'meizu-huawei-AA2-job.json': ('matrix-20260906-meizu-huawei-AA2', 9, 4),
           'meizu-huawei-AA5-job.json': ('matrix-20260906-meizu-huawei-AA5', 12, 1)}
ORIGINS = {'xiaomi-huawei-BC3-job.json': (('vivo-xiaomi-AD1-job.json', 0),
            ('wps-xiaomi-AD5-job.json', 0), ('wps-xiaomi-AD5-job.json', 1)),
           'meizu-huawei-AA2-job.json': tuple(('vivo-meizu-W1-job.json', i) for i in range(3)),
           'meizu-huawei-AA5-job.json': (('meizu-empty-X1-job.json', None),)}


def require(condition):
    if not condition:
        raise ValueError('huawei_legacy_native_scope_unverified')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cached(db, platform, account, source_id):
    # Exact SQL only: inherited synthetic ancestry may include Vivo, whose cache is excluded.
    require(platform in ('xiaomi', 'meizu', 'huawei'))
    row = db.execute('SELECT document FROM notes WHERE platform=? AND account=? AND source_id=?',
                     (platform, account, source_id)).fetchone()
    require(row is not None)
    note = NoteDocument.model_validate_json(row[0])
    require((note.platform, note.account_id, note.source_id) == (platform, account, source_id))
    return note


def _identity(note):
    return dict(platform=note.platform, account=note.account_id, source_id=note.source_id,
                fingerprint=note.fingerprint())


def _proof(name, index, job, original, baseline, source, actual, key, store, read, paths):
    """Recompute recovery bindings; old top-level status alone never grants a scope."""
    warnings = [i['message'] for i in original.get('issues', [])
                if i.get('note_id') == source.source_id and i.get('code') == 'format_downgrade']
    require(actual.source_id not in baseline['target'])
    if name == 'xiaomi-huawei-BC3-job.json':
        report = read('evidence/BC3-readonly-reconciliation.json')
        require(original.get('status') == 'needs_review' and report.get('batch_id') == job['id']
                and report.get('kind') == 'BC3-readonly-reconciliation' and report.get('status') == 'needs_review'
                and report.get('formal_acceptance') is False and report.get('cloud_writes') == 0
                and report.get('target_notes') == 17 and report.get('original_target_notes_unchanged') is True
                and report.get('only_recorded_additions') is True and len(report['items']) == 3
                and report.get('fourth_item') == 'not_attempted')
        require(len({i['remote_ids'][0] for i in report['items'] if len(i['remote_ids']) == 1}) == 3)
        item = report['items'][index]
        require(item.get('index') == index and item.get('source_id') == source.source_id
                and item.get('source_fingerprint') == source.fingerprint() and item.get('receipt_key') == key
                and item.get('remote_ids') == [actual.source_id] and item.get('group_verified') is True
                and item.get('attachment_originals_verified') is True)
        task = store.task(report['task_id'])
        require(task and task.operation == 'fetch' and task.status == 'succeeded'
                and task.completed == task.succeeded == task.total == 17 and not task.issues)
        warnings = item['warnings']
        if index == 0:
            repair = read('evidence/huawei-BC3-empty-paragraph-reparse.json')
            expected = {'manifest': paths[0], 'reconciliation': paths[-2], 'original_failure': paths[1]}
            require(repair.get('file_sha256') == {k: digest(v) for k, v in expected.items()}
                    and repair.get('kind') == 'huawei-BC3-empty-paragraph-reparse'
                    and repair.get('batch_id') == job['id'] and repair.get('entry_index') == 0
                    and repair.get('status') == 'verified' and repair.get('formal_acceptance') is False
                    and repair.get('cloud_writes') == repair.get('attachment_downloads') == 0
                    and repair.get('guid') == actual.source_id and repair.get('receipt_key') == key
                    and repair.get('source_fingerprint') == source.fingerprint()
                    and repair.get('cached_fingerprint') == item['target_fingerprint']
                    and repair.get('restored_fingerprint') == actual.fingerprint()
                    and all(repair.get(k) is True for k in ('content_verified', 'group_verified',
                         'cached_assets_verified', 'response_asset_identity_and_size_verified', 'old_projection_verified')))
            # Historical etag/version assertions were explicitly not proven; native reads verify the current target.
        else:
            require(item.get('content_verified') is True and item.get('target_fingerprint') == actual.fingerprint())
            if index == 2:
                repair = read('evidence/BC3-third-confirmed-readonly.json')
                require(repair.get('kind') == 'BC3-third-readonly-confirmation' and repair.get('batch_id') == job['id']
                        and repair.get('entry_index') == 2 and repair.get('status') == 'confirmed'
                        and repair.get('formal_acceptance') is False and repair.get('cloud_writes') == 0
                        and repair.get('task_id') == report['task_id']
                        and repair.get('source_fingerprint') == source.fingerprint()
                        and repair.get('target_fingerprint') == actual.fingerprint()
                        and repair.get('receipt_key') == key and repair.get('remote_ids') == [actual.source_id]
                        and all(repair.get(k) is True for k in
                                ('content_verified', 'group_verified', 'attachment_originals_verified')))
    elif name == 'meizu-huawei-AA2-job.json':
        repair = read('evidence/huawei-AA2-recovery-repair.json')
        recovery_before = read('checkpoints/huawei-AA2-recovery-before.json')
        proof = read('evidence/huawei-AA2-source-lineage.json')
        expected = dict(zip(('manifest', 'original_evidence', 'baseline', 'recovery_evidence', 'recovery_baseline'),
                            (paths[0], paths[1], paths[2], paths[-3], paths[-2]), strict=True))
        require(original.get('status') == 'needs_review' and proof.get('kind') == 'huawei-recovered-source-lineage'
                and proof.get('batch_id') == job['id'] and proof.get('formal_acceptance') is False
                and proof.get('cloud_writes') == 0
                and proof.get('file_sha256') == {k: digest(v) for k, v in expected.items()}
                and repair.get('kind') == 'huawei-AA2-scoped-recovery' and repair.get('mode') == 'repair'
                and repair.get('status') == 'verified_with_degradation'
                and repair.get('content_verified') is True and repair.get('original_other_notes_unchanged') is True
                and all(type(repair.get(k)) is int and repair[k] == v for k, v in
                        {'creates': 0, 'uploads': 0, 'updates': 1, 'existing_images': 2, 'missing_images': 0}.items()))
        require(len(proof['items']) == 3 and [i.get('entry_index') for i in proof['items']] == [0, 1, 2])
        item = proof['items'][index]
        require(item.get('source') == _identity(source) and item.get('target') == _identity(actual)
                and item.get('receipt_key') == key)
        before = recovery_before['fingerprints']
        recovered = {i['target']['source_id'] for i in proof['items']}
        require(len(recovered) == 3 and set(before) - set(baseline['target']) == recovered
                and all(before.get(k) == v for k, v in baseline['target'].items()))
        task = store.task(proof['fresh_readback_task_id'])
        require(task and task.operation == 'fetch' and task.status == 'succeeded' and not task.issues)
        if index == 2:
            require(store.receipt(key + ':AA2-scoped-recovery') ==
                    {'status': 'confirmed', 'remote_ids': [actual.source_id]})
            warnings = repair['warnings']
        else:
            require(before.get(actual.source_id) == actual.fingerprint())
    else:
        require(original.get('status') == 'api_verified' and original.get('original_target_notes_unchanged') is True
                and original.get('only_confirmed_additions') is True and len(original['items']) == 1)
        item = original['items'][0]
        require(item.get('source_id') == source.source_id and item.get('source_fingerprint') == source.fingerprint()
                and item.get('status') == 'api_verified' and item.get('receipt_status') == 'confirmed'
                and item.get('content_verified') is True and item.get('group_verified') is True
                and item.get('source_title_empty') is True and not source.title
                and item.get('target_title_empty') is False and bool(actual.title))
        # AA5's original report predates per-target hashes. Its existing target is
        # bound by the later, preserved BC3 before snapshot, never today's content alone.
        later = read('checkpoints/matrix-20260907-xiaomi-huawei-BC3-before.json')
        require(len(later['target']) == 14 and later['target'].get(actual.source_id) == actual.fingerprint())
    require(isinstance(warnings, list) and all(isinstance(w, str) for w in warnings))
    return warnings


def browser_target(scope, root, account):
    name, index = scope.get('manifest'), scope.get('index')
    require(name in SCOPES and type(index) is int and index in SCOPES[name])
    base, paths = root / '.private', []

    def read(relative):
        path = base / relative
        paths.append(path)
        return json.loads(path.read_text('utf-8'))

    job = read('checkpoints/' + name)
    batch_id, before_count, entry_count = BATCHES[name]
    require(job.get('kind') == 'cloud-matrix-batch' and job.get('target') == 'huawei'
            and job.get('armed') is False and job.get('id') == batch_id
            and job.get('expected_account') == account and job.get('source_policy') is None
            and len(job['sources']) == entry_count)
    original = read('evidence/' + batch_id + '.json')
    baseline = read('checkpoints/' + batch_id + '-before.json')
    require(original.get('batch_id') == batch_id and original.get('target') == 'huawei'
            and original.get('formal_acceptance') is False and original.get('before_count') == before_count
            and len(baseline['target']) == before_count)
    entry = job['sources'][index]
    origin = entry['from_cloud_fixture']
    parent, parent_index = ORIGINS[name][index]
    image_count = 0 if name == 'meizu-huawei-AA5-job.json' else 2
    require(origin.get('platform') == ('xiaomi' if name.startswith('xiaomi') else 'meizu')
            and origin.get('batch_manifest' if image_count else 'manifest') == parent
            and origin.get('entry_index') == parent_index
            and ('manifest' not in origin if image_count else 'batch_manifest' not in origin)
            and entry.get('with_images') is bool(image_count) and entry.get('with_group') is True)
    matrix = _module(root, 'live-matrix-job')
    fixture = _module(root, 'live-fixture-job').fixture

    def build(job):
        return image_fixture(root, job) if job.get('with_images') is True else fixture({**job, 'with_images': False})

    with closing(sqlite3.connect((base / 'session-lab/notes.sqlite').resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        store = _ReadOnlyStore(db)
        binding = matrix.confirmed_tool_binding(name, index, store, root, build)
        identity = dict(zip(FIELDS, (origin['platform'], origin['account'], origin['source_id'],
                                    origin['fingerprint'], 'huawei', account), strict=True))
        key = identity_key(identity)
        require(binding['receipt_key'] == key and binding['platform'] == 'huawei' and binding['account'] == account)
        context = db.execute('SELECT ' + ','.join(FIELDS) + ' FROM receipt_contexts WHERE key=?', (key,)).fetchone()
        # These confirmed legacy receipts predate contexts. Exact original key/ancestry replaces no write guard.
        require(context is None or tuple(context) == tuple(identity[k] for k in FIELDS))
        source = cached(db, origin['platform'], origin['account'], origin['source_id'])
        actual = cached(db, 'huawei', account, binding['remote_id'])
        require(source.fingerprint() == origin['fingerprint'] == baseline['source'].get(source.source_id)
                and source.display_title == entry['title'] and len(source.attachments) == image_count
                and len(actual.attachments) == image_count and not actual.warnings
                and store.snapshot('huawei', account).get('complete') is True)
        require(all(cached(db, 'huawei', account, k).fingerprint() == v for k, v in baseline['target'].items()))
        warnings = _proof(name, index, job, original, baseline, source, actual, key, store, read, paths)
        require(matrix.compare_note(source, actual, warnings))
        group = store.receipt(folder_key(source, account))
        require(group == {'status': 'confirmed', 'remote_ids': [actual.source_folder_id]}
                and actual.source_folder_name and actual.display_title.startswith('笔记互迁验收 · '))
        resources = store.resource_receipts(key)
        require(len(resources) == image_count * 2 and all(r['status'] == 'linked' for r in resources)
                and {r['resource_id'] for r in resources} == {v for a in actual.attachments for v in (a.id, a.name)})
        images = []
        for note in (source, actual):
            for asset in note.attachments:
                path = confined(base / 'session-lab/resources', asset.local_path or '')
                data = path.read_bytes()
                require(asset.kind == 'image' and len(data) == asset.size and hashlib.sha256(data).hexdigest() == asset.sha256)
                with Image.open(io.BytesIO(data)) as picture:
                    width, height = picture.size
                    picture.verify()
                if note is actual:
                    images.append({'width': width, 'height': height})
        require(0 < len(actual.plain_text) <= 100000 and all(b.kind in
                ('paragraph', 'heading', 'list', 'todo', 'quote', 'code', 'divider', 'attachment') for b in actual.blocks))
        while binding:
            paths.append(base / 'checkpoints' / binding['manifest'])
            binding = binding['lineage'][0] if binding['lineage'] else None
        from huawei_native_scope import render_expectations
        return {'scopeVersion': 1, 'scopeLabel': name.removesuffix('-job.json') + ':' + str(index),
                'fixtureId': actual.source_id, 'group_id': actual.source_folder_id,
                'group_name': actual.source_folder_name, 'title': actual.display_title,
                **render_expectations(actual), 'image_specs': images,
                'target_fingerprint': actual.fingerprint(), 'source_degradation_count': len(warnings),
                'evidence_sha256': {p.relative_to(root).as_posix(): digest(p) for p in dict.fromkeys(paths)}}

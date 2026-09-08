"""Read-only official-renderer scopes for OR1 and the six fixed incoming OPPO pairs."""
import hashlib
import io
import json
import re
import sqlite3
from contextlib import closing
from types import SimpleNamespace

from cloud_fixture_source import select_source
from huawei_scoped_source import fixture
from PIL import Image
from xiaomi_browser_scope import _module, _ReadOnlyStore

from note_bridge.models import NoteDocument
from note_bridge.operations import receipt_key
from note_bridge.paths import confined
from note_bridge.providers.oppo_groups import DEFAULT_GROUP, folder_key
from note_bridge.receipt_identity import FIELDS, migration_identity

OR1 = 'oppo-complex-OR1-job.json'
MATRIX = r'(wps|xiaomi|honor|meizu|huawei|vivo)-oppo-BI(?:[1-9]|1[0-2])-job\.json'
MARKS = ('bold', 'italic', 'underline', 'strike', 'highlight', 'code', 'link')
NATIVE_CHECKS = ('uniqueEditor', 'identity', 'group', 'title', 'ownedDOM', 'modelText',
                 'renderedText', 'styles', 'structures', 'images')


def require(condition):
    if not condition:
        raise ValueError('oppo_native_scope_unverified')


def source_fixture(root, job):
    return fixture(root, job) if job.get('with_images') is True else _module(root, 'live-fixture-job').fixture({**job, 'with_images': False})


def render_expectations(note):
    runs = []
    for block in note.blocks:
        if block.kind == 'table':
            runs.extend({'text': cell} for row in block.rows for cell in row)
        elif block.kind != 'attachment':
            runs.extend({'text': span.text, **{key: getattr(span, key) for key in MARKS}} for span in block.spans)
    return {'expectedText': note.plain_text, 'expectedRuns': runs,
        'headings': [{'text': b.text, 'level': b.level} for b in note.blocks if b.kind == 'heading'],
        'todos': [{'text': b.text, 'checked': b.checked} for b in note.blocks if b.kind == 'todo'],
        'lists': [{'text': b.text, 'ordered': b.ordered} for b in note.blocks if b.kind == 'list'],
        'quotes': [b.text for b in note.blocks if b.kind == 'quote'],
        'codes': [b.text for b in note.blocks if b.kind == 'code'],
        'tables': [b.rows for b in note.blocks if b.kind == 'table'],
        'dividers': sum(b.kind == 'divider' for b in note.blocks)}


def validate_report(report, scopes):
    """Check the independent worker's complete result before lab evidence is labelled verified."""
    require(isinstance(report, dict) and report.get('kind') == 'oppo-matrix-native-batch'
            and report.get('formal_acceptance') is False and type(report.get('cloud_writes')) is int
            and report['cloud_writes'] == 0 and report.get('status') in ('verified', 'needs_review', 'blocked')
            and isinstance(report.get('items'), list) and len(report['items']) <= len(scopes))
    if report['status'] != 'verified':
        return False
    require(report.get('total') == report.get('checked') == len(report['items']) == len(scopes)
            and report.get('remaining') == 0)
    for index, (item, scope) in enumerate(zip(report['items'], scopes, strict=True)):
        require(type(item.get('index')) is int and item['index'] == index
                and item.get('status') == 'content_images_styles_verified'
                and isinstance(item.get('checks'), dict) and set(item['checks']) == set(NATIVE_CHECKS)
                and all(item['checks'][key] is True for key in NATIVE_CHECKS)
                and isinstance(item.get('structures'), dict) and set(item['structures']) ==
                    {'headings', 'todos', 'lists', 'quotes', 'codes', 'tables', 'dividers'}
                and all(value is True for value in item['structures'].values())
                and isinstance(item.get('images'), list) and len(item['images']) == len(scope['image_specs'])
                and sorted(image.get('scopeIndex', -1) for image in item['images']) == list(range(len(scope['image_specs'])))
                and all(all(image.get(key) is True for key in ('identityMatched', 'decoded', 'visible', 'dimensionsMatched'))
                        for image in item['images']))
    return True


def browser_target(scope, root, account):
    require(isinstance(scope, dict) and set(scope) == {'manifest', 'index'})
    name, index = scope['manifest'], scope['index']
    require((name == OR1 and index is None) or (isinstance(name, str) and re.fullmatch(MATRIX, name)
            and type(index) is int and index in (0, 1)))
    base = root / '.private'
    manifest_path = base / 'checkpoints' / name
    job = json.loads(manifest_path.read_text('utf-8'))
    require(job.get('armed') is False and job.get('target') == 'oppo' and job.get('expected_account') == account
            and re.fullmatch(r'[0-9a-f]{64}', account))
    expected_id = 'fixture-20260907-oppo-complex-OR1' if name == OR1 else 'matrix-20260907-' + name.removesuffix('-job.json')
    require(job.get('id') == expected_id)
    consumed_path = base / 'checkpoints' / (expected_id + '-consumed-job.json')
    consumed = json.loads(consumed_path.read_text('utf-8'))
    require((name != OR1 and consumed == job) or (name == OR1 and bool(consumed.get('consumed_at'))
            and {k: v for k, v in consumed.items() if k != 'consumed_at'} ==
                {k: v for k, v in job.items() if k != 'consumed_at'}))
    evidence_path = base / 'evidence' / (expected_id + '.json')
    evidence = json.loads(evidence_path.read_text('utf-8'))
    require(evidence.get('formal_acceptance') is False)
    paths = [manifest_path, consumed_path, evidence_path]
    with closing(sqlite3.connect((base / 'session-lab/notes.sqlite').resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        store = _ReadOnlyStore(db)
        matrix = _module(root, 'live-matrix-job')
        if name == OR1:
            require(job.get('kind') == 'independent-cloud-write-smoke' and job.get('from_cloud_fixture') is None
                    and job.get('with_images') is True and job.get('with_group') is True and job.get('content_case') == 'complex'
                    and evidence.get('kind') == job['kind'] and evidence.get('platform') == 'oppo'
                    and evidence.get('fixture_id') == expected_id and evidence.get('status') in ('verified', 'verified_with_degradation')
                    and evidence.get('comparison_policy') == 'strict_matrix_expectations'
                    and all(evidence.get(k) is True for k in ('receipt_confirmed', 'group_mapping_verified',
                        'image_bytes_verified', 'original_notes_unchanged', 'content_and_style_verified'))
                    and evidence.get('expected_images') == evidence.get('restored_images') == 2)
            source = source_fixture(root, job)
            lineage = matrix.confirmed_tool_binding(name, None, store, root, lambda j: source_fixture(root, j))
            warnings = []
        else:
            require(matrix.oppo_batch_scope(job) is True and evidence.get('kind') == 'cloud-matrix-batch'
                    and evidence.get('batch_id') == expected_id and evidence.get('target') == 'oppo'
                    and evidence.get('oppo_scope') == matrix.OPPO_SCOPE
                    and evidence.get('status') == 'api_verified'
                    and evidence.get('original_target_notes_unchanged') is True and evidence.get('only_confirmed_additions') is True
                    and len(evidence.get('items', [])) == 2)
            entry = job['sources'][index]
            source = select_source({**entry, 'target': 'oppo'}, store, root, lambda j: source_fixture(root, j))
            item = evidence['items'][index]
            require(evidence.get('source') == source.platform and item.get('source_id') == source.source_id
                    and item.get('source_fingerprint') == source.fingerprint() and item.get('status') == 'api_verified'
                    and item.get('receipt_status') == 'confirmed'
                    and all(item.get(k) is True for k in ('content_verified', 'group_verified')))
            lineage = matrix.confirmed_tool_binding(name, index, store, root, lambda j: source_fixture(root, j))
            require(len(lineage['lineage']) == 1 and not lineage['lineage'][0]['lineage'])
            origin = entry['from_cloud_fixture']
            source_manifest_path = base / 'checkpoints' / origin['manifest']
            source_job = json.loads(source_manifest_path.read_text('utf-8'))
            paths.extend([source_manifest_path, base / 'evidence' / (source_job['id'] + '.json')])
            if 'scoped_proof' in origin:
                path = confined(root, origin['scoped_proof']['path'])
                require(hashlib.sha256(path.read_bytes()).hexdigest() == origin['scoped_proof']['sha256'])
                paths.append(path)
            warnings = [issue['message'] for issue in evidence.get('issues', [])
                        if issue.get('note_id') == source.source_id and issue.get('code') == 'format_downgrade']
        destination = SimpleNamespace(spec=SimpleNamespace(id='oppo'), account_id=account)
        key = receipt_key(source, destination)
        require(lineage['receipt_key'] == key and lineage['platform'] == 'oppo' and lineage['account'] == account)
        identity = migration_identity(source, destination)
        context = db.execute('SELECT ' + ','.join(FIELDS) + ' FROM receipt_contexts WHERE key=?', (key,)).fetchone()
        require(context is not None and tuple(context) == tuple(identity[field] for field in FIELDS))
        require(store.snapshot('oppo', account).get('complete') is True)
        row = db.execute('SELECT document FROM notes WHERE platform=? AND account=? AND source_id=?',
                         ('oppo', account, lineage['remote_id'])).fetchone()
        require(row is not None)
        actual = NoteDocument.model_validate_json(row[0])
        require(actual.platform == 'oppo' and actual.account_id == account and actual.source_id == lineage['remote_id']
                and not actual.warnings and re.fullmatch(r'[A-Za-z0-9_-]{1,150}', actual.source_id)
                and matrix.compare_note(source, actual, warnings))
        group = store.receipt(folder_key(source, account))
        require((source.source_folder_name and group and group['status'] == 'confirmed'
                    and group['remote_ids'] == [actual.source_folder_id] and actual.source_folder_name == source.source_folder_name)
                or (not source.source_folder_name and actual.source_folder_id == DEFAULT_GROUP and group is None))
        resources = store.resource_receipts(key)
        expected_images = 0 if name != OR1 and entry['from_cloud_fixture']['manifest'] == 'xiaomi-group-P2-job.json' else 2
        require(len(source.attachments) == expected_images
                and ((4 <= len(resources) <= 6) if expected_images else not resources)
                and all(r['status'] == 'linked' for r in resources))
        ids = [r['resource_id'] for r in resources]
        cloud_ids = {value for value in ids if re.fullmatch(r'[A-Za-z0-9_-]{1,150}', value)}
        require(len(cloud_ids) == expected_images and sum(bool(re.fullmatch(r'prepare/[0-9a-f]{32}', value)) for value in ids) == expected_images
                and all(value in cloud_ids or re.fullmatch(r'prepare/[0-9a-f]{32}', value) or value.startswith('apply/') for value in ids)
                and len(actual.attachments) == expected_images and {a.name for a in actual.attachments} == cloud_ids)
        images = []
        for asset in actual.attachments:
            data = confined(base / 'session-lab/resources', asset.local_path or '').read_bytes()
            require(asset.kind == 'image' and len(data) == asset.size and hashlib.sha256(data).hexdigest() == asset.sha256)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                image.verify()
            images.append({'id': asset.id, 'cloud_id': asset.name, 'width': width, 'height': height, 'sha256': asset.sha256})
        require(len({i['id'] for i in images}) == expected_images and actual.display_title.startswith('笔记互迁验收 · ')
                and 0 < len(actual.plain_text) <= 100000 and len(actual.blocks) <= 2000)
        return {'scopeVersion': 1, 'scopeLabel': name.removesuffix('-job.json') + ':' + ('seed' if index is None else str(index)),
                'fixtureId': actual.source_id, 'title': actual.display_title, 'group_id': actual.source_folder_id,
                'group_name': actual.source_folder_name or '', **render_expectations(actual), 'image_specs': images,
                'target_fingerprint': actual.fingerprint(), 'receipt_key': key, 'lineage': lineage,
                'source_degradation_count': len(warnings),
                'evidence_sha256': {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in dict.fromkeys(paths)}}

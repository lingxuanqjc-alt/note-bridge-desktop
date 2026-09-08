"""Resolve a confirmed synthetic matrix item for a read-only official-page check."""
import importlib.util
import json
import re
from types import SimpleNamespace

from cloud_fixture_source import select_source

from note_bridge.operations import receipt_key
from note_bridge.storage import Store


def browser_target(scope, root, account, platform='vivo'):
    assert platform in ('vivo', 'wps')
    name, index = scope.get('manifest'), scope.get('index')
    assert isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9-]+-job\.json', name)
    assert type(index) is int and 0 <= index < 30
    manifest = json.loads((root / '.private/checkpoints' / name).read_text('utf-8'))
    single_table = name == 'vivo-table-T1-job.json'
    if single_table:
        assert platform == 'vivo' and index == 0
        assert manifest['kind'] == 'independent-cloud-write-smoke'
        assert manifest['id'] == 'fixture-20260906-vivo-table-T1' and manifest['content_case'] == 'table'
    else:
        assert manifest['kind'] == 'cloud-matrix-batch'
        assert re.fullmatch(r'matrix-\d{8}-[A-Za-z0-9-]{1,30}', manifest['id'])
    assert manifest['target'] == platform
    assert manifest['armed'] is False and manifest['expected_account'] == account
    evidence = json.loads((root / '.private/evidence' / (manifest['id'] + '.json')).read_text('utf-8'))
    api_verified = evidence.get('status') == 'verified' if single_table else evidence['items'][index]['status'] == 'api_verified'
    if single_table:
        assert api_verified and evidence['fixture_id'] == manifest['id']
    spec = importlib.util.spec_from_file_location('vivo_scope_fixture', root / 'scripts/live-fixture-job.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    store = Store(root / '.private/session-lab/notes.sqlite')
    source = module.fixture(manifest) if single_table else select_source(
        {**manifest['sources'][index], 'target': platform}, store, root, module.fixture)
    target = SimpleNamespace(spec=SimpleNamespace(id=platform), account_id=account)
    receipt = store.receipt(receipt_key(source, target))
    assert receipt['status'] == 'confirmed' and len(receipt['remote_ids']) == 1
    identity = receipt['remote_ids'][0]
    actual = next(note for note in store.notes(platform, account) if note.source_id == identity)
    assert actual.display_title.startswith('笔记互迁验收 · ')
    if single_table:
        baseline = json.loads((root / '.private/checkpoints/vivo-table-T1-before-refetch.json').read_text('utf-8'))
        assert baseline[identity] == actual.fingerprint()
        assert store.snapshot(platform, account)['complete']
    if not api_verified:
        assert platform == 'vivo' and name == 'wps-vivo-AK1-job.json' and index == 0
        review = json.loads((root / '.private/evidence/vivo-AK1-empty-paragraph-reparse.json').read_text('utf-8'))
        assert review['kind'] == 'vivo-AK1-empty-paragraph-reparse' and review['cloud_writes'] == 0
        assert review['content_verified'] and review['raw_body_unchanged'] and review['group_unchanged']
        assert review['source_fingerprint'] == source.fingerprint()
        assert actual.fingerprint() in (review['cached_fingerprint'], review['restored_fingerprint'])
    return {'fixtureId': identity, 'title': actual.display_title,
            'expectedText': actual.plain_text,
            'expected_images': len(actual.attachments), 'group_name': actual.source_folder_name,
            'headings': [{'level': b.level, 'text': b.text} for b in actual.blocks if b.kind == 'heading'],
            'quotes': [b.text for b in actual.blocks if b.kind == 'quote'],
            'tables': [b.rows for b in actual.blocks if b.kind == 'table'],
            'dividers': sum(b.kind == 'divider' for b in actual.blocks)}

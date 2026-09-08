"""Native inspection must never select an unrelated account/note or stale cache."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span
from note_bridge.operations import receipt_key
from note_bridge.providers.honor import parse_entry
from note_bridge.providers.honor_groups import folder_key
from note_bridge.providers.honor_html import encode
from note_bridge.storage import Store

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('honor_matrix_scope', ROOT / 'scripts/honor_matrix_scope.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize(('platform', 'batch'), [('honor', True), ('honor', False), ('meizu', True)])
def test_only_honor_batch_waits_for_node_browser_cleanup(tmp_path, monkeypatch, platform, batch):
    """A multi-note Honor check must not kill Node before its browser finally runs."""
    import subprocess

    from session_discovery import discover

    closed, calls = [], []
    monkeypatch.setattr('note_bridge.providers.factory.create_provider', lambda *args: SimpleNamespace(
        probe=lambda: 'synthetic-account', close=lambda: closed.append(True)))
    monkeypatch.setitem(sys.modules, platform + '_matrix_scope', SimpleNamespace(
        browser_target=lambda item, root, account, **kwargs: {'fixtureId': str(item['index'])}))
    key = 'fixture_scopes' if batch else 'fixture_scope'
    selected = [{'manifest': 'synthetic-job.json', 'index': i} for i in range(2)]
    job = {'platform': platform, 'operation': platform + '_matrix_browser',
           key: selected if batch else selected[0]}
    private = tmp_path / '.private'
    (private / 'evidence').mkdir(parents=True)
    (private / 'lab-discovery.json').write_text(json.dumps(job), 'utf8')

    def run(command, **kwargs):
        assert closed == [True]
        assert kwargs['timeout'] is None if platform == 'honor' and batch else kwargs['timeout'] == 100
        payload = json.loads(kwargs['input'])
        assert payload == ({'scopes': [{'fixtureId': '0'}, {'fixtureId': '1'}], 'cookies': []}
                           if batch else {'fixtureId': '0', 'cookies': []})
        calls.append(command)
        return SimpleNamespace(stdout=json.dumps({'status': 'needs_review'}))

    monkeypatch.setattr(subprocess, 'run', run)
    result = discover(platform, [], tmp_path)
    assert len(calls) == 1 and result[key] == job[key]
    assert result['status'] == 'needs_review', 'Waiting for cleanup must not promote a failed content check.'


@pytest.fixture
def scope(tmp_path, monkeypatch):
    private = tmp_path / '.private'
    for name in ('checkpoints', 'evidence', 'session-lab/resources'):
        (private / name).mkdir(parents=True, exist_ok=True)
    picture = private / 'session-lab/resources/fixture.png'
    Image.new('RGB', (4, 2), (13, 22, 33)).save(picture)
    data = picture.read_bytes()
    asset = Attachment(id='fixture-image', name='fixture.png', kind='image', mime='image/png',
                       local_path='fixture.png', size=len(data), sha256=hashlib.sha256(data).hexdigest())
    seed = NoteDocument(platform=PlatformId.VIVO, account_id='synthetic', source_id='seed',
                        title='笔记互迁验收 · 荣耀范围测试', source_folder_id='source-group',
                        source_folder_name='笔记互迁分组验收 T', attachments=[asset], blocks=[
                            Block(spans=[Span(text='加粗', bold=True)]), Block(kind='attachment', attachment_id=asset.id)])
    source = seed.model_copy(update={'platform': PlatformId.XIAOMI, 'account_id': 'source', 'source_id': 'source-id'})
    target_id, group_id = 'a' * 32, 'b' * 32
    actual = parse_entry({'uuid': target_id, 'type': 2, 'title': source.title, 'html_content': encode(source, {asset.id: asset.id})[0],
                          'folder_uuid': group_id}, 'target', {group_id: source.source_folder_name}, [asset])
    store = Store(private / 'session-lab/notes.sqlite')
    store.replace_snapshot(source.platform, source.account_id, [source], True)
    store.replace_snapshot(actual.platform, actual.account_id, [actual], True)
    source_provider = SimpleNamespace(spec=SimpleNamespace(id='xiaomi'), account_id='source')
    target_provider = SimpleNamespace(spec=SimpleNamespace(id='honor'), account_id='target')
    store.save_receipt(receipt_key(seed, source_provider), 'confirmed', [source.source_id])
    key = receipt_key(source, target_provider)
    store.save_receipt(key, 'confirmed', [target_id])
    store.save_receipt(folder_key(source, 'target'), 'confirmed', [group_id])
    original = {'id': 'fixture-20260907-scope', 'armed': False, 'expected_account': 'source', 'target': 'xiaomi', 'title': seed.title}
    manifest = {'id': 'matrix-20260907-scope', 'kind': 'cloud-matrix-batch', 'target': 'honor', 'expected_account': 'target', 'armed': False,
                'sources': [{'title': source.title, 'with_images': True, 'with_group': True, 'from_cloud_fixture': {
                    'manifest': 'source-job.json', 'platform': 'xiaomi', 'account': 'source', 'source_id': source.source_id,
                    'fingerprint': source.fingerprint()}}]}
    evidence = {'batch_id': manifest['id'], 'target': 'honor', 'status': 'api_verified', 'original_target_notes_unchanged': True,
                'only_confirmed_additions': True, 'items': [{'source_id': source.source_id, 'source_fingerprint': source.fingerprint(),
                    'receipt_status': 'confirmed', 'content_verified': True, 'group_verified': True, 'status': 'api_verified'}]}
    for path, value in [('checkpoints/source-job.json', original), ('checkpoints/target-job.json', manifest),
                        ('evidence/fixture-20260907-scope.json', {'status': 'verified'}), ('evidence/matrix-20260907-scope.json', evidence)]:
        (private / path).write_text(json.dumps(value), 'utf-8')
    loader = module._module
    monkeypatch.setattr(module, '_module', lambda root, name: SimpleNamespace(fixture=lambda _: seed)
                        if name == 'live-fixture-job' else loader(ROOT, name))
    return SimpleNamespace(root=tmp_path, store=store, source=source, actual=actual, key=key,
                           manifest=manifest, evidence=evidence, request={'manifest': 'target-job.json', 'index': 0})


def test_another_source_cannot_claim_oppo_default_group_by_name_or_forged_scope_flag(scope):
    scope.actual.source_folder_name = '未分类'
    scope.store.replace_snapshot('honor', 'target', [scope.actual], True)
    scope.request['default_group_binding'] = {'fixture': 'fixture-20260906-oppo-image-F2',
        'source_fingerprint': scope.source.fingerprint(), 'target_id': scope.actual.source_id,
        'target_group_id': scope.actual.source_folder_id}
    with pytest.raises(ValueError):
        module.browser_target(scope.request, scope.root, 'target')


def test_exact_receipt_and_images_selected_without_changing_existing_database(scope):
    database = scope.root / '.private/session-lab/notes.sqlite'
    before = database.read_bytes()
    result = module.browser_target(scope.request, scope.root, 'target')
    assert result['fixtureId'] == scope.actual.source_id
    assert result['group_id'] == scope.actual.source_folder_id
    assert result['expectedText'] == scope.actual.plain_text
    assert result['image_specs'] == [{'id': 'fixture-image', 'width': 4, 'height': 2}]
    assert result['expected_styles'][0]['bold']
    assert database.read_bytes() == before


def test_verified_multihop_group_keeps_each_numbered_suffix(scope):
    scope.actual.source_folder_name = '笔记互迁分组验收 M (2) (3)'
    scope.store.replace_snapshot('honor', 'target', [scope.actual], True)
    assert module.browser_target(scope.request, scope.root, 'target')['group_name'] == scope.actual.source_folder_name


@pytest.mark.parametrize('failure', ['account', 'platform', 'index_bool', 'path', 'armed', 'receipt',
                                    'cache_incomplete', 'target_changed', 'image_changed', 'item_wrong_source', 'group_receipt'])
def test_unrelated_or_unverified_state_is_rejected_before_browser_launch(scope, failure):
    account, platform = 'target', 'honor'
    if failure == 'account':
        account = 'other-account'
    elif failure == 'platform':
        platform = 'vivo'
    elif failure == 'index_bool':
        scope.request['index'] = False
    elif failure == 'path':
        scope.request['manifest'] = '../target-job.json'
    elif failure == 'armed':
        scope.manifest['armed'] = True
        (scope.root / '.private/checkpoints/target-job.json').write_text(json.dumps(scope.manifest), 'utf-8')
    elif failure == 'receipt':
        scope.store.save_receipt(scope.key, 'uncertain', [scope.actual.source_id])
    elif failure == 'cache_incomplete':
        scope.store.replace_snapshot('honor', 'target', [scope.actual], False)
    elif failure == 'target_changed':
        scope.actual.blocks[0].spans[0].text = 'unverified replacement'
        scope.store.replace_snapshot('honor', 'target', [scope.actual], True)
    elif failure == 'image_changed':
        (scope.root / '.private/session-lab/resources/fixture.png').write_bytes(b'changed')
    elif failure == 'item_wrong_source':
        scope.evidence['items'][0]['source_fingerprint'] = 'wrong'
        (scope.root / '.private/evidence/matrix-20260907-scope.json').write_text(json.dumps(scope.evidence), 'utf-8')
    else:
        scope.store.save_receipt(folder_key(scope.source, 'target'), 'confirmed', ['other-group'])
    from note_bridge.errors import BridgeError
    with pytest.raises((ValueError, BridgeError)):
        module.browser_target(scope.request, scope.root, account, platform)

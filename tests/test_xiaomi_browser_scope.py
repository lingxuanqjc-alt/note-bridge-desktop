"""Same-title synthetic targets must resolve by receipt, with no writable DB access."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span
from note_bridge.operations import receipt_key
from note_bridge.providers.xiaomi import encode_content, parse_entry
from note_bridge.providers.xiaomi_groups import folder_key
from note_bridge.storage import Store

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('xiaomi_browser_scope', ROOT / 'scripts/xiaomi_browser_scope.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def scope(tmp_path, monkeypatch):
    private = tmp_path / '.private'
    for name in ('checkpoints', 'evidence', 'session-lab/resources'):
        (private / name).mkdir(parents=True, exist_ok=True)
    picture = private / 'session-lab/resources/image.png'
    Image.new('RGB', (4, 2), 'purple').save(picture)
    data = picture.read_bytes()
    asset = Attachment(id='image', name='image.png', kind='image', mime='image/png', local_path='image.png',
                       size=len(data), sha256=hashlib.sha256(data).hexdigest())
    seeds, sources, actuals, entries, items = [], [], [], [], []
    store = Store(private / 'session-lab/notes.sqlite')
    target = SimpleNamespace(spec=SimpleNamespace(id='xiaomi'), account_id='target')
    origin = SimpleNamespace(spec=SimpleNamespace(id='wps'), account_id='source')
    for index in range(2):
        seed = NoteDocument(platform=PlatformId.VIVO, account_id='seed', source_id=f'seed-{index}',
            title='笔记互迁验收 · WPS分组 J2', source_folder_id='folder', source_folder_name='笔记互迁分组验收 J',
            blocks=[Block(spans=[Span(text=f'正文{index}', bold=True)]), Block(kind='attachment', attachment_id='image')],
            attachments=[asset])
        source = seed.model_copy(update={'platform': PlatformId.WPS, 'account_id': 'source', 'source_id': f'source-{index}'})
        content, _ = encode_content(source, {'image': {'fileId': 'image'}})
        identity = str(12345678 + index)
        actual = parse_entry({'id': identity, 'content': content, 'folderId': '17',
            'setting': {'data': [{'fileId': 'image', 'mimeType': 'image/png'}]}}, 'target', {'17': seed.source_folder_name})
        actual.attachments = [asset]
        seeds.append(seed)
        sources.append(source)
        actuals.append(actual)
        store.save_receipt(receipt_key(seed, origin), 'confirmed', [source.source_id])
        store.save_receipt(receipt_key(source, target), 'confirmed', [identity])
        store.save_receipt(folder_key(source, 'target'), 'confirmed', ['17'])
        original = {'id': f'fixture-20260907-wps-BD1-{index}', 'armed': False, 'expected_account': 'source',
                    'target': 'wps', 'title': seed.title, 'index': index}
        (private / f'checkpoints/source-{index}-job.json').write_text(json.dumps(original), 'utf-8')
        (private / f'evidence/{original["id"]}.json').write_text(json.dumps({'status': 'verified'}), 'utf-8')
        entries.append({'title': source.title, 'with_images': True, 'with_group': True, 'from_cloud_fixture': {
            'platform': 'wps', 'account': 'source', 'manifest': f'source-{index}-job.json',
            'source_id': source.source_id, 'fingerprint': source.fingerprint()}})
        items.append({'source_id': source.source_id, 'source_fingerprint': source.fingerprint(),
                      'status': 'api_verified', 'receipt_status': 'confirmed',
                      'content_verified': True, 'group_verified': True})
    store.replace_snapshot('wps', 'source', sources, True)
    store.replace_snapshot('xiaomi', 'target', actuals, True)
    manifest = {'id': 'matrix-20260907-wps-xiaomi-BD4', 'kind': 'cloud-matrix-batch', 'target': 'xiaomi',
                'expected_account': 'target', 'armed': False, 'source_policy': 'direct_seed_only', 'sources': entries}
    evidence = {'batch_id': manifest['id'], 'target': 'xiaomi', 'status': 'api_verified',
                'original_target_notes_unchanged': True, 'only_confirmed_additions': True, 'items': items}
    (private / 'checkpoints/wps-xiaomi-BD4-job.json').write_text(json.dumps(manifest), 'utf-8')
    (private / f'evidence/{manifest["id"]}.json').write_text(json.dumps(evidence), 'utf-8')
    loader = module._module
    monkeypatch.setattr(module, '_module', lambda root, name: SimpleNamespace(fixture=lambda job: seeds[job['index']])
                        if name == 'live-fixture-job' else loader(ROOT, name))
    return SimpleNamespace(root=tmp_path, manifest=manifest, evidence=evidence, sources=sources,
                           actuals=actuals, store=store, target=target)


def request(index=0):
    return {'manifest': 'wps-xiaomi-BD4-job.json', 'index': index}


def test_bd_labels_and_two_same_title_notes_use_different_receipt_ids_with_readonly_sqlite(scope, monkeypatch):
    database = scope.root / '.private/session-lab/notes.sqlite'
    before = database.read_bytes()
    connection = module.sqlite3.connect
    calls = []
    def readonly(address, **kwargs):
        assert address.endswith('?mode=ro') and kwargs['uri'] is True
        calls.append(address)
        return connection(address, **kwargs)
    monkeypatch.setattr(module.sqlite3, 'connect', readonly)
    selected = [module.browser_target(request(index), scope.root, 'target') for index in range(2)]
    assert selected[0]['fixtureTitle'] == selected[1]['fixtureTitle']
    assert [item['fixtureId'] for item in selected] == [note.source_id for note in scope.actuals]
    assert len(set(item['fixtureId'] for item in selected)) == 2
    assert len(calls) == 2 and database.read_bytes() == before


@pytest.mark.parametrize('failure', ['account', 'index_bool', 'path', 'armed', 'wrong_item', 'receipt',
                                    'snapshot_incomplete', 'snapshot_count', 'changed_body', 'image', 'group'])
def test_changed_scope_or_content_fails_before_browser_launch(scope, failure):
    args, account = request(), 'target'
    if failure == 'account':
        account = 'other'
    elif failure == 'index_bool':
        args['index'] = False
    elif failure == 'path':
        args['manifest'] = '../wps-xiaomi-BD4-job.json'
    elif failure == 'armed':
        scope.manifest['armed'] = True
        (scope.root / '.private/checkpoints/wps-xiaomi-BD4-job.json').write_text(json.dumps(scope.manifest), 'utf-8')
    elif failure == 'wrong_item':
        scope.evidence['items'][0]['source_id'] = 'wrong'
        (scope.root / f'.private/evidence/{scope.manifest["id"]}.json').write_text(json.dumps(scope.evidence), 'utf-8')
    elif failure == 'receipt':
        scope.store.save_receipt(receipt_key(scope.sources[0], scope.target), 'uncertain', ['12345678'])
    elif failure == 'snapshot_incomplete':
        scope.store.replace_snapshot('xiaomi', 'target', scope.actuals, False)
    elif failure == 'snapshot_count':
        with scope.store.connection() as db:
            db.execute("UPDATE snapshots SET count=17 WHERE platform='xiaomi'")
    elif failure == 'changed_body':
        scope.actuals[0].blocks[1].spans[0].text = 'unexpected'
        scope.store.replace_snapshot('xiaomi', 'target', scope.actuals, True)
    elif failure == 'image':
        (scope.root / '.private/session-lab/resources/image.png').write_bytes(b'changed')
    else:
        scope.store.save_receipt(folder_key(scope.sources[0], 'target'), 'confirmed', ['other'])
    with pytest.raises((BridgeError, ValueError)):
        module.browser_target(args, scope.root, account)

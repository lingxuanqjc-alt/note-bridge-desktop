"""One consumed repair batch for the three confirmed AD1 synthetic notes."""
import hashlib
import importlib.util
import json
from threading import Event
from types import SimpleNamespace
from urllib.parse import quote

from cloud_fixture_source import select_source

from note_bridge.errors import BridgeError
from note_bridge.operations import receipt_key
from note_bridge.providers.factory import create_provider
from note_bridge.providers.xiaomi import encode_content, parse_entry
from note_bridge.storage import Store


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def execute_updates(provider, prepared, originals, report, save):
    expected = {key: digest(raw) for key, raw in originals.items()}
    token = provider.transport.cookie('serviceToken')
    assert token
    for index, identity, payload, warnings in prepared:
        before = provider._json('GET', '/note/note/' + quote(identity, safe='') + '/')['entry']
        assert digest(before) == expected[identity]
        item = {'index': index, 'target_id': identity, 'status': 'write_uncertain',
                'before_sha256': expected[identity], 'expected_content_sha256': digest(payload['content']),
                'warnings': warnings}
        report['items'].append(item)
        report['update_requests'] += 1
        save()
        response = provider._json('POST', '/note/note/' + quote(identity, safe=''),
            data={'entry': json.dumps(payload, ensure_ascii=False), 'serviceToken': token}, write=True)
        if response.get('conflict'):
            item['status'] = 'conflict_needs_review'
            save()
            return
        actual = provider._json('GET', '/note/note/' + quote(identity, safe='') + '/')['entry']
        assert actual['content'] == payload['content']
        assert all(actual.get(key) == before.get(key) for key in ('setting', 'folderId', 'createDate', 'extraInfo'))
        expected[identity] = digest(actual)
        item.update(status='content_repaired_readback_verified', after_sha256=expected[identity])
        save()
        # Check every other original, including earlier repaired notes, after each update.
        for key in expected:
            raw = provider._json('GET', '/note/note/' + quote(key, safe='') + '/')['entry']
            assert digest(raw) == expected[key]
        item['other_notes_unchanged'] = len(expected) - 1
        save()
    report['status'] = 'all_content_repaired_readback_verified'


def run(jars, root):
    path = root / '.private/lab-xiaomi-complex-repair.json'
    intent = json.loads(path.read_text('utf-8'))
    if intent.get('armed') is not True or intent.get('id') != 'xiaomi-AH-AD1-content-only':
        return {'status': 'not_armed'}
    output = root / '.private/evidence/xiaomi-AH-complex-repair.json'
    assert not output.exists()
    for name in ('lab-write-job.json', 'lab-matrix-job.json', 'lab-xiaomi-native-repair.json'):
        assert json.loads((root / '.private' / name).read_text('utf-8'))['armed'] is False
    intent['armed'] = False
    path.write_text(json.dumps(intent), 'utf-8')
    plan = json.loads((root / '.private/checkpoints/xiaomi-AF-readonly-plan.json').read_text('utf-8'))
    ag = json.loads((root / '.private/evidence/xiaomi-AG-AC6-content-repair.json').read_text('utf-8'))
    assert ag['status'] == 'content_repaired_readback_verified' and ag['other_notes_unchanged'] == 7
    assert ag['target_id'] == next(item['target_id'] for item in plan['items'] if item['index'] == 3)
    expected = dict(plan['target_raw_hashes'])
    expected[ag['target_id']] = ag['after_sha256']
    modules = []
    for name in ('live-fixture-job.py', 'repair-xiaomi-native.py'):
        spec = importlib.util.spec_from_file_location(name, root / 'scripts' / name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    fixture, native = modules
    batch = json.loads((root / '.private/checkpoints/vivo-xiaomi-AD1-job.json').read_text('utf-8'))
    assert batch['id'] == 'matrix-20260907-vivo-xiaomi-AD1' and batch['armed'] is False
    assert batch['expected_account'] == plan['account'] and len(batch['sources']) == 3
    store = Store(root / '.private/session-lab/notes.sqlite')
    sources = [select_source({**row, 'target': 'xiaomi'}, store, root, fixture.fixture) for row in batch['sources']]
    provider = create_provider('xiaomi', jars, root / '.private/session-lab/resources')
    report = {'kind': 'xiaomi-AH-AD1-content-repair', 'formal_acceptance': False, 'status': 'needs_review',
              'creates': 0, 'uploads': 0, 'update_requests': 0, 'items': [], 'native_visual_verification': 'pending'}

    def save():
        output.write_text(json.dumps(report, indent=2), 'utf-8')

    try:
        assert provider.probe() == plan['account']
        originals = {key: provider._json('GET', '/note/note/' + quote(key, safe='') + '/')['entry'] for key in expected}
        assert {key: digest(raw) for key, raw in originals.items()} == expected
        prepared = []
        context = SimpleNamespace(check_cancel=lambda: None, cancelled=Event())
        for index, source in enumerate(sources):
            item = next(row for row in plan['items'] if row['index'] == index)
            assert source.fingerprint() == item['source_fingerprint']
            receipt = store.receipt(receipt_key(source, provider))
            assert receipt['status'] == 'confirmed' and receipt['remote_ids'] == [item['target_id']]
            raw = originals[item['target_id']]
            parsed = parse_entry(raw, provider.account_id, {})
            assert len(parsed.attachments) == len(source.attachments) == 2
            for asset in parsed.attachments:
                provider._download(asset, context)
                matches = [a for a in source.attachments if (a.sha256, a.size) == (asset.sha256, asset.size)]
                assert len(matches) == 1 and item['images'][matches[0].id] == asset.id
            metadata = {row['fileId']: row for row in raw['setting']['data']}
            assert set(metadata) == set(item['images'].values())
            content, warnings = encode_content(source, {key: metadata[value] for key, value in item['images'].items()})
            payload = native.native_update_entry(raw, content)
            prepared.append((index, item['target_id'], payload, warnings))
        assert len({row[1] for row in prepared}) == 3
        report['original_image_bytes_verified'] = 6
        execute_updates(provider, prepared, originals, report, save)
        return report
    except BridgeError as error:
        report['code'] = error.code
        return report
    finally:
        save()
        provider.close()

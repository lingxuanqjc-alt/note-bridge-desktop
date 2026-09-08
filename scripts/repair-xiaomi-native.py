"""One-shot content-only repair of the confirmed AC6 synthetic note."""
import importlib.util
import json
from pathlib import Path
from urllib.parse import quote

from note_bridge.errors import BridgeError
from note_bridge.operations import receipt_key
from note_bridge.providers.factory import create_provider
from note_bridge.providers.xiaomi import encode_content, parse_entry
from note_bridge.storage import Store


def native_update_entry(raw, content):
    """Official +XW4 projection: preserve every field; replace only content."""
    fields = ('id', 'tag', 'status', 'createDate', 'modifyDate', 'colorId', 'content', 'setting',
              'folderId', 'alertDate', 'extraInfo')
    assert set(fields).issubset(raw) and raw['status'] not in ('deleted', 'purged')
    assert not raw.get('encryptInfo')
    result = {key: raw[key] for key in fields}
    for key in ('id', 'tag', 'folderId'):
        result[key] = str(result[key])
    result['content'] = content
    return result


def reconcile(jars, root, label="AF"):
    assert label in ("AF", "AG")
    import hashlib

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()

    outcome = json.loads((root / f'.private/evidence/xiaomi-{label}-AC6-content-repair.json').read_text('utf-8'))
    plan = json.loads((root / '.private/checkpoints/xiaomi-AF-readonly-plan.json').read_text('utf-8'))
    assert outcome['kind'] == f'xiaomi-{label}-AC6-content-repair' and outcome['update_requests'] == 1
    identity = next(row['target_id'] for row in plan['items'] if row['index'] == 3)
    assert identity == outcome['target_id']
    provider = create_provider('xiaomi', jars, root / '.private/session-lab/resources')
    try:
        assert provider.probe() == plan['account']
        details = {key: provider._json('GET', '/note/note/' + quote(key, safe='') + '/')['entry']
                   for key in plan['target_raw_hashes']}
        others = all(digest(raw) == plan['target_raw_hashes'][key] for key, raw in details.items() if key != identity)
        actual = details[identity]
        repaired = digest(actual['content']) == outcome['expected_content_sha256']
        untouched = digest(actual) == outcome['before_sha256']
        result = {'kind': f'xiaomi-{label}-AC6-readonly-reconciliation', 'formal_acceptance': False, 'cloud_writes': 0,
                  'other_notes_unchanged': others, 'new_content_present': repaired, 'old_entry_unchanged': untouched,
                  'status': 'repaired_readback_verified' if repaired and others else
                            'old_entry_unchanged' if untouched and others else 'needs_review'}
        (root / f'.private/evidence/xiaomi-{label}-AC6-readonly-reconciliation.json').write_text(json.dumps(result, indent=2), 'utf-8')
        return result
    finally:
        provider.close()


def run(jars, root: Path):
    import hashlib

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()

    intent_path = root / '.private/lab-xiaomi-native-repair.json'
    intent = json.loads(intent_path.read_text('utf-8'))
    if intent.get('armed') is not True or intent.get('id') not in ('xiaomi-AF-AC6-content-only', 'xiaomi-AG-AC6-content-only'):
        return {'status': 'not_armed'}
    label = 'AG' if intent['id'] == 'xiaomi-AG-AC6-content-only' else 'AF'
    if label == 'AG':
        prior = json.loads((root / '.private/evidence/xiaomi-AF-AC6-readonly-reconciliation.json').read_text('utf-8'))
        assert prior['status'] == 'old_entry_unchanged' and prior['other_notes_unchanged'] is True
    assert not (root / f'.private/evidence/xiaomi-{label}-AC6-content-repair.json').exists(), 'Repair outcome already recorded'
    for name in ('lab-matrix-job.json', 'lab-write-job.json'):
        assert json.loads((root / '.private' / name).read_text('utf-8')).get('armed') is False
    intent['armed'] = False
    intent_path.write_text(json.dumps(intent), 'utf-8')
    plan = json.loads((root / '.private/checkpoints/xiaomi-AF-readonly-plan.json').read_text('utf-8'))
    assert plan['kind'] == 'xiaomi-AF-scoped-readonly-inspection' and plan['armed'] is False
    item = next(row for row in plan['items'] if row['index'] == 3)
    manifest = json.loads((root / '.private/checkpoints/xiaomi-image-AC6-job.json').read_text('utf-8'))
    assert manifest['id'] == 'fixture-20260907-xiaomi-image-AC6' and manifest['armed'] is False
    assert plan['account'] == manifest['expected_account']
    spec = importlib.util.spec_from_file_location('xiaomi_native_fixture', root / 'scripts/live-fixture-job.py')
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    source = fixture.fixture(manifest)
    assert source.fingerprint() == item['source_fingerprint']
    store = Store(root / '.private/session-lab/notes.sqlite')
    provider = create_provider('xiaomi', jars, root / '.private/session-lab/resources')
    output = root / f'.private/evidence/xiaomi-{label}-AC6-content-repair.json'
    report = {'kind': f'xiaomi-{label}-AC6-content-repair', 'formal_acceptance': False,
              'status': 'not_started', 'creates': 0, 'uploads': 0, 'update_requests': 0,
              'native_visual_verification': 'pending'}

    def save():
        output.write_text(json.dumps(report, indent=2), 'utf-8')

    def detail(identity):
        result = provider._json('GET', '/note/note/' + quote(identity, safe='') + '/')['entry']
        assert str(result['id']) == identity
        return result

    try:
        assert provider.probe() == plan['account']
        receipt = store.receipt(receipt_key(source, provider))
        assert receipt['status'] == 'confirmed' and receipt['remote_ids'] == [item['target_id']]
        originals = {identity: detail(identity) for identity in plan['target_raw_hashes']}
        assert {key: digest(raw) for key, raw in originals.items()} == plan['target_raw_hashes']
        raw = originals[item['target_id']]
        assert raw.get('tag') is not None and not raw.get('encryptInfo')
        metadata = {row['fileId']: row for row in raw['setting']['data']}
        assert set(metadata) == set(item['images'].values()) and len(metadata) == 2
        images = {key: metadata[value] for key, value in item['images'].items()}
        content, warnings = encode_content(source, images)
        assert content != raw['content'] and content.startswith('<new-format/>')
        token = provider.transport.cookie('serviceToken')
        assert token
        payload = native_update_entry(raw, content) if label == 'AG' else {'content': content, 'tag': str(raw['tag'])}
        report.update(status='write_uncertain', update_requests=1, warnings=warnings,
                      target_id=item['target_id'], before_sha256=digest(raw), expected_content_sha256=digest(content))
        save()  # Persist uncertainty before the sole mutation; this intent cannot be reused.
        original_json = provider.transport.json if label == 'AG' else None
        if original_json:
            def observed_json(method, path, **kwargs):
                value = original_json(method, path, **kwargs)
                if method == 'POST':
                    report['response_code'] = value.get('code') if type(value.get('code')) is int else None
                    report['response_has_data_object'] = isinstance(value.get('data'), dict)
                    save()
                return value
            provider.transport.json = observed_json
        response = provider._json('POST', '/note/note/' + quote(item['target_id'], safe=''),
            data={'entry': json.dumps(payload, ensure_ascii=False),
                  'serviceToken': token}, write=True)
        if response.get('conflict'):
            report.update(status='conflict_needs_review')
            return report
        after = detail(item['target_id'])
        assert after['content'] == content
        for key in ('setting', 'folderId', 'createDate', 'extraInfo'):
            assert after.get(key) == raw.get(key)
        for identity, expected in originals.items():
            if identity != item['target_id']:
                assert digest(detail(identity)) == digest(expected)
        parsed = parse_entry(after, provider.account_id, {})
        assert {b.attachment_id for b in parsed.blocks if b.attachment_id} == set(metadata)
        report.update(status='content_repaired_readback_verified', other_notes_unchanged=len(originals) - 1,
                      image_references_unchanged=True, source_time_and_group_unchanged=True,
                      after_sha256=digest(after))
        return report
    except BridgeError as error:
        report['code'] = error.code
        return report
    finally:
        save()
        provider.close()

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('xiaomi_repair_test', Path(__file__).parents[1] / 'scripts/repair-xiaomi-native.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


def test_native_update_preserves_full_record_but_excludes_unrelated_response_fields():
    raw = {'id': 12, 'tag': 34, 'status': 'normal', 'createDate': 1, 'modifyDate': 2,
           'colorId': 0, 'content': 'old', 'setting': {'data': [{'fileId': 'existing-image'}]},
           'folderId': 56, 'alertDate': 0, 'extraInfo': '{}', 'derived': {'temporary': True}}
    before = json.loads(json.dumps(raw))
    result = repair.native_update_entry(raw, 'new')
    assert set(result) == {'id', 'tag', 'status', 'createDate', 'modifyDate', 'colorId', 'content',
                           'setting', 'folderId', 'alertDate', 'extraInfo'}
    assert result['setting'] == raw['setting'] and result['content'] == 'new'
    assert (result['id'], result['tag'], result['folderId']) == ('12', '34', '56')
    assert raw == before, 'Preparing a request must not mutate the preserved before-image.'


@pytest.mark.parametrize('label', ['AF', 'AG'])
@pytest.mark.parametrize('mode', ['success', 'conflict', 'changed_before'])
def test_scoped_repair_never_creates_uploads_or_retries_and_checks_originals(tmp_path, monkeypatch, mode, label):
    def write(name, value):
        path = tmp_path / '.private' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), 'utf-8')

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()

    raw = {'id': 'known', 'tag': 'one', 'content': 'old', 'folderId': 'group', 'createDate': 1720000000000,
           'status':'normal', 'modifyDate':1720000001000, 'colorId':0, 'alertDate':0, 'extraInfo':'{}',
           'setting': {'data': [{'fileId': 'image1'}, {'fileId': 'image2'}]}}
    other = {'id': 'other', 'content': 'original'}
    content = '<new-format/>new\n<img fileid="image1"/>\n<img fileid="image2"/>'
    write('lab-xiaomi-native-repair.json', {'armed': True, 'id': f'xiaomi-{label}-AC6-content-only'})
    for name in ('lab-matrix-job.json', 'lab-write-job.json'):
        write(name, {'armed': False})
    write('checkpoints/xiaomi-AF-readonly-plan.json', {'kind': 'xiaomi-AF-scoped-readonly-inspection',
          'armed': False, 'account': 'account', 'items': [{'index': 3, 'target_id': 'known',
          'source_fingerprint': 'source', 'images': {'a': 'image1', 'b': 'image2'}}],
          'target_raw_hashes': {'known': digest(raw), 'other': digest(other)}})
    write('checkpoints/xiaomi-image-AC6-job.json', {'id': 'fixture-20260907-xiaomi-image-AC6',
          'armed': False, 'expected_account': 'account'})
    (tmp_path / '.private/evidence').mkdir()
    if label == 'AG':
        write('evidence/xiaomi-AF-AC6-readonly-reconciliation.json', {'status':'old_entry_unchanged','other_notes_unchanged':True})
    source = SimpleNamespace(fingerprint=lambda: 'source')
    fixture = SimpleNamespace()
    loader = SimpleNamespace(exec_module=lambda module: setattr(module, 'fixture', lambda _: source))
    monkeypatch.setattr(repair.importlib.util, 'spec_from_file_location', lambda *a: SimpleNamespace(loader=loader))
    monkeypatch.setattr(repair.importlib.util, 'module_from_spec', lambda _: fixture)
    monkeypatch.setattr(repair, 'Store', lambda _: SimpleNamespace(receipt=lambda _: {'status': 'confirmed', 'remote_ids': ['known']}))
    monkeypatch.setattr(repair, 'receipt_key', lambda *a: 'receipt')
    monkeypatch.setattr(repair, 'encode_content', lambda *a: (content, []))
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        if method == 'POST':
            assert path == '/note/note/known' and kwargs['write'] is True
            assert json.loads(kwargs['data']['entry']) == (repair.native_update_entry(raw, content) if label == 'AG' else {'content': content, 'tag': 'one'})
            if mode == 'conflict':
                return {'conflict': True}
            raw['content'] = content
            return {'tag': 'two'}
        value = raw if path == '/note/note/known/' else other
        return {'entry': json.loads(json.dumps(value))}

    provider = SimpleNamespace(probe=lambda: 'account', account_id='account', _json=request,
                               transport=SimpleNamespace(cookie=lambda _: 'synthetic', json=lambda *a, **kw: None), close=lambda: None)
    monkeypatch.setattr(repair, 'create_provider', lambda *a: provider)
    if mode == 'changed_before':
        other['content'] = 'user changed it'
        with pytest.raises(AssertionError):
            repair.run([], tmp_path)
        assert not any(method == 'POST' for method, _ in calls)
    else:
        result = repair.run([], tmp_path)
        assert result['status'] == ('conflict_needs_review' if mode == 'conflict' else 'content_repaired_readback_verified')
        assert sum(method == 'POST' for method, _ in calls) == 1
        reread = repair.reconcile([], tmp_path, label)
        assert reread['status'] == ('old_entry_unchanged' if mode == 'conflict' else 'repaired_readback_verified')
        assert sum(method == 'POST' for method, _ in calls) == 1, 'Uncertain outcomes must be reconciled without another write.'
    assert repair.run([], tmp_path) == {'status': 'not_armed'}

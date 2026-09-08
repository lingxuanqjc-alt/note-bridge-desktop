"""Native selectors require the reviewed exact batch, never a title-only match."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('xiaomi_browser_selectors', ROOT / 'scripts/xiaomi_browser_selectors.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def manifest(name):
    batch_id, platform, originals = module.REVIEWED_BATCHES[name]
    return {'id': batch_id, 'source_policy': 'direct_seed_only', 'expected_account': 'target',
            'sources': [{'title': title, 'with_images': images, 'with_group': True,
                         'from_cloud_fixture': {'platform': platform, 'manifest': original}}
                        for original, title, images in originals]}


def save(root, name, data):
    directory = root / '.private/checkpoints'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(data), encoding='utf-8')


@pytest.mark.parametrize('name', tuple(module.REVIEWED_BATCHES))
def test_reviewed_batch_delegates_each_id_to_strict_receipt_resolver(tmp_path, monkeypatch, name):
    data, calls = manifest(name), []
    save(tmp_path, name, data)
    def resolve(scope, root, account):
        assert root == tmp_path and account == 'target'
        calls.append(scope)
        return {'fixtureId': str(100 + scope['index']), 'fixtureTitle': data['sources'][scope['index']]['title']}
    monkeypatch.setattr(module, 'browser_target', resolve)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob('*'))
    result = module.prepare_selectors(tmp_path, name)
    assert calls == [{'manifest': name, 'index': index} for index in range(len(data['sources']))]
    assert len({item['fixtureId'] for item in result['resolved']}) == len(data['sources'])
    assert all(item['armed'] is False and item['mode'] == 'desktop-scoped-fixture' for item in result['requests'])
    assert result['cloud_requests'] == 0 and result['activated'] is False
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob('*')) == before


@pytest.mark.parametrize('change', ['name', 'id', 'policy', 'order', 'extra', 'batch_source', 'title', 'images'])
def test_unreviewed_manifest_never_reaches_receipts_or_browser(tmp_path, monkeypatch, change):
    name = 'honor-xiaomi-BD7-job.json'
    data = manifest(name)
    if change == 'name':
        name = 'honor-xiaomi-arbitrary-job.json'
    elif change == 'id':
        data['id'] = 'matrix-20260907-honor-xiaomi-other'
    elif change == 'policy':
        data['source_policy'] = 'arbitrary'
    elif change == 'order':
        data['sources'].reverse()
    elif change == 'extra':
        data['sources'].append(data['sources'][0])
    elif change == 'batch_source':
        data['sources'][0]['from_cloud_fixture']['batch_manifest'] = 'unreviewed-job.json'
    elif change == 'title':
        data['sources'][0]['title'] = 'unrelated note'
    else:
        data['sources'][0]['with_images'] = False
    save(tmp_path, name, data)
    monkeypatch.setattr(module, 'browser_target', lambda *_, **__: pytest.fail('Scope must fail before receipt resolution'))
    with pytest.raises(ValueError, match='unreviewed_browser_batch'):
        module.prepare_selectors(tmp_path, name)


@pytest.mark.parametrize('failure', ['unconfirmed', 'same_id'])
def test_pending_receipt_or_duplicate_target_cannot_be_activated(tmp_path, monkeypatch, failure):
    name = 'honor-xiaomi-BD7-job.json'
    data = manifest(name)
    save(tmp_path, name, data)
    def resolve(*_, **__):
        if failure == 'unconfirmed':
            raise ValueError('unverified_fixture_scope')
        return {'fixtureId': '100', 'fixtureTitle': data['sources'][0]['title']}
    monkeypatch.setattr(module, 'browser_target', resolve)
    with pytest.raises(ValueError, match='unverified_fixture_scope'):
        module.prepare_selectors(tmp_path, name)


@pytest.mark.parametrize('change', ['title', 'image', 'origin'])
def test_third_raw_empty_fixture_keeps_its_separate_title_and_zero_images(tmp_path, monkeypatch, change):
    name = 'meizu-xiaomi-BD8-job.json'
    data = manifest(name)
    third = data['sources'][2]
    if change == 'title':
        third['title'] = data['sources'][0]['title']
    elif change == 'image':
        third['with_images'] = True
    else:
        third['from_cloud_fixture']['manifest'] = 'meizu-group-L2-job.json'
    save(tmp_path, name, data)
    monkeypatch.setattr(module, 'browser_target', lambda *_, **__: pytest.fail('Unreviewed third source'))
    with pytest.raises(ValueError, match='unreviewed_browser_batch'):
        module.prepare_selectors(tmp_path, name)

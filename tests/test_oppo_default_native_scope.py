"""Only the proven original F2 may carry OPPO's default label into native checks."""
import importlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_fixed_oppo_sources import ROOT, oppo, setup

from note_bridge.errors import BridgeError
from note_bridge.models import account_fingerprint
from note_bridge.operations import receipt_key
from note_bridge.providers.oppo_groups import DEFAULT_GROUP
from note_bridge.storage import Store


def prepare(tmp_path, monkeypatch, platform, source_index=0):
    cached_notes = Store.notes
    ctx = setup(tmp_path, monkeypatch, oppo)
    create = oppo.create_provider
    def named_default(*args):
        provider = create(*args)
        original = provider._json
        def read(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path.endswith('/group-list-new'):
                result = deepcopy(result)
                for row in result:
                    if row['groupGuid'] == DEFAULT_GROUP:
                        row['groupName'] = '未分类'
            return result
        provider._json = read
        return provider
    monkeypatch.setattr(oppo, 'create_provider', named_default)
    proof = oppo.capture([], tmp_path)['proof']
    source = oppo.verify_source(proof, ctx.store, tmp_path)[source_index]
    account = account_fingerprint(platform, 'synthetic-target')
    module = importlib.import_module(platform + '_matrix_scope')
    provider_module = importlib.import_module('note_bridge.providers.' + platform)
    groups = importlib.import_module('note_bridge.providers.' + platform + '_groups')
    target_id, group_id = 'a' * 32, 'b' * 32
    mapping = {asset.id: asset.id for asset in source.attachments}
    if platform == 'honor':
        from note_bridge.providers.honor_html import encode
        actual = provider_module.parse_entry({'uuid': target_id, 'type': 2, 'title': source.title,
            'html_content': encode(source, mapping)[0], 'folder_uuid': group_id}, account,
            {group_id: '未分类'}, source.attachments)
    else:
        actual = provider_module.parse_entry({'uuid': target_id, 'title': source.title,
            'body': json.dumps(provider_module.encode_blocks(source, mapping)[0])}, account, group_id, '未分类')
        actual.attachments = source.attachments
    ctx.store.replace_snapshot(platform, account, [actual], True)
    target = SimpleNamespace(spec=SimpleNamespace(id=platform), account_id=account)
    key = receipt_key(source, target)
    ctx.store.save_receipt(key, 'confirmed', [target_id])
    group_key = groups.folder_key(source, account)
    ctx.store.save_receipt(group_key, 'confirmed', [group_id])
    origin = {'platform': 'oppo', 'account': source.account_id, 'source_id': source.source_id,
        'fingerprint': source.fingerprint(), 'manifest': oppo.MANIFESTS[source_index], 'scoped_proof': proof}
    manifest = {'id': 'matrix-20260907-default-scope', 'kind': 'cloud-matrix-batch', 'target': platform,
        'expected_account': account, 'armed': False, 'oppo_scope': 'OR1_fixed_direct_12', 'source_policy': 'direct_seed_only',
        'sources': [{'title': source.display_title, 'with_images': True, 'with_group': True, 'from_cloud_fixture': origin}]}
    evidence = {'batch_id': manifest['id'], 'target': platform, 'status': 'api_verified',
        'original_target_notes_unchanged': True, 'only_confirmed_additions': True, 'items': [
            {'source_id': source.source_id, 'source_fingerprint': source.fingerprint(), 'receipt_status': 'confirmed',
             'content_verified': True, 'group_verified': True, 'status': 'api_verified'}]}
    (ctx.private / 'checkpoints/default-target-job.json').write_text(json.dumps(manifest), 'utf-8')
    (ctx.private / 'evidence/matrix-20260907-default-scope.json').write_text(json.dumps(evidence), 'utf-8')
    loader = module._module
    monkeypatch.setattr(module, '_module', lambda _, name: loader(ROOT, name))
    def only_target(store, selected, selected_account):
        assert (selected, selected_account) == (platform, account), 'Native scope never reads the source account cache.'
        return cached_notes(store, selected, selected_account)
    monkeypatch.setattr(Store, 'notes', only_target)
    return ctx, module, manifest, account, source, actual, key, group_key


@pytest.mark.parametrize('platform', ['honor', 'meizu'])
@pytest.mark.parametrize('mode', ['verified', 'numbered', 'arbitrary_name', 'forged_source', 'missing_proof',
    'corrupt_proof', 'unknown_receipt', 'wrong_group_receipt', 'named_OR1'])
def test_default_label_requires_original_fixed_proof_and_confirmed_target_mapping(tmp_path, monkeypatch, platform, mode):
    ctx, module, manifest, account, source, actual, key, group_key = prepare(
        tmp_path, monkeypatch, platform, source_index=1 if mode == 'named_OR1' else 0)
    origin = manifest['sources'][0]['from_cloud_fixture']
    if mode == 'numbered':
        actual.source_folder_name = '未分类 (2)'
    elif mode == 'arbitrary_name':
        actual.source_folder_name = '任意私人分组'
    elif mode == 'forged_source':
        origin['manifest'] = oppo.MANIFESTS[1]
    elif mode == 'missing_proof':
        origin.pop('scoped_proof')
    elif mode == 'corrupt_proof':
        origin['scoped_proof']['sha256'] = 'f' * 64
    elif mode == 'unknown_receipt':
        ctx.store.save_receipt(key, 'uncertain')
    elif mode == 'wrong_group_receipt':
        ctx.store.save_receipt(group_key, 'confirmed', ['c' * 32])
    ctx.store.replace_snapshot(platform, account, [actual], True)
    (ctx.private / 'checkpoints/default-target-job.json').write_text(json.dumps(manifest), 'utf-8')
    before = (ctx.private / 'session-lab/notes.sqlite').read_bytes()
    if mode in ('verified', 'numbered'):
        result = module.browser_target({'manifest': 'default-target-job.json', 'index': 0}, tmp_path, account)
        assert result['group_name'] == actual.source_folder_name
        assert result['default_group_binding'] == {'fixture': oppo.FIXTURES[0], 'source_fingerprint': source.fingerprint(),
            'target_id': actual.source_id, 'target_group_id': actual.source_folder_id}
    else:
        with pytest.raises((ValueError, BridgeError)):
            module.browser_target({'manifest': 'default-target-job.json', 'index': 0}, tmp_path, account)
    assert (ctx.private / 'session-lab/notes.sqlite').read_bytes() == before
    assert len(ctx.calls) == 12, 'Qualifying an existing target is entirely local; do not recapture the source.'

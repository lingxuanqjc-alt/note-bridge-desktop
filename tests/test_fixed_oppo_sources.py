"""The remaining OPPO routes may read only the two receipted original seeds."""
import hashlib
import importlib
import io
import json
import sys
from copy import deepcopy
from html import escape
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests
from PIL import Image

from note_bridge.errors import BridgeError
from note_bridge.exporter import stamp
from note_bridge.models import Block, PlatformId, account_fingerprint
from note_bridge.operations import receipt_key
from note_bridge.providers.oppo import OppoProvider
from note_bridge.providers.oppo_groups import DEFAULT_GROUP
from note_bridge.providers.transport import Transport
from note_bridge.richtext import block_html, numbered_blocks, span_html
from note_bridge.storage import Store

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
oppo = importlib.import_module('oppo_scoped_source')
vivo = importlib.import_module('vivo_fixed_source')
huawei = importlib.import_module('huawei_scoped_source')


def setup(tmp_path, monkeypatch, helper, failure=None):
    platform = 'oppo' if helper is oppo else 'vivo'
    private = tmp_path / '.private'
    for part in ('checkpoints', 'evidence', 'session-lab/resources/fixtures'):
        (private / part).mkdir(parents=True)
    real_module = helper._module
    monkeypatch.setattr(helper, '_module', lambda _, name: real_module(ROOT, name))
    monkeypatch.setattr(huawei, '_module', lambda _, name: real_module(ROOT, name))
    images = []
    for index, (fmt, ext) in enumerate((('PNG', 'png'), ('JPEG', 'jpg'))):
        output = io.BytesIO()
        Image.new('RGB', (4, 2), 'purple').save(output, format=fmt)
        images.append(output.getvalue())
        (private / f'session-lab/resources/fixtures/vivo-upload-synthetic-{index}.{ext}').write_bytes(images[-1])
    account = account_fingerprint(platform, 'synthetic-user')
    target = SimpleNamespace(spec=SimpleNamespace(id=platform), account_id=account)
    store = Store(private / 'session-lab/notes.sqlite')
    store.replace_snapshot(platform, account, [], False)
    seeds, remote_ids, cloud_ids = [], [], []
    for index, (name, identifier) in enumerate(zip(helper.MANIFESTS, helper.FIXTURES, strict=True)):
        job = dict(kind='independent-cloud-write-smoke', id=identifier, target=platform, armed=False,
            consumed_at='2026-09-07T00:00:00+00:00', expected_account=account, with_images=True,
            with_group=bool(index) if helper is oppo else True, content_case='complex' if index else None,
            title='笔记互迁验收 · 固定综合' if index else '笔记互迁验收 · 固定基础')
        if helper is oppo:
            consumed = dict(job)
            if index:
                job.pop('consumed_at')
            (private / 'checkpoints' / helper.CONSUMED[index]).write_text(json.dumps(consumed), 'utf-8')
        (private / 'checkpoints' / name).write_text(json.dumps(job), 'utf-8')
        old = dict(kind=job['kind'], fixture_id=identifier, platform=platform, status='verified',
            formal_acceptance=False, receipt_confirmed=True, group_mapping_verified=True,
            image_bytes_verified=True, original_notes_unchanged=True, content_and_style_verified=True,
            expected_images=2, restored_images=2, comparison_policy='strict_matrix_expectations',
            cloud_resource_receipt_states=['linked'] * (6 if helper is oppo else 2))
        (private / 'evidence' / (identifier + '.json')).write_text(json.dumps(old), 'utf-8')
        seed = helper.fixture(tmp_path, job)
        seeds.append(seed)
        identity = f'synthetic-fixed-{index}' if helper is oppo else f'{index + 1:032x}'
        remote_ids.append(identity)
        assets = [f'synthetic-file-{index}-{i}' for i in range(2)]
        cloud_ids.append(assets)
        key = receipt_key(seed, target)
        store.save_receipt(key, 'sending', [identity])
        for i, asset in enumerate(assets):
            store.save_resource_receipt(key, asset, 'uploaded')
            if helper is oppo:
                store.save_resource_receipt(key, f'prepare/{index * 2 + i:032x}', 'uploaded')
                store.save_resource_receipt(key, 'apply/' + str(index) + str(i) + 'x' * 160, 'uploaded')
        store.save_receipt(key, 'confirmed', [identity])
        if job['with_group']:
            store.save_receipt(helper.folder_key(seed, account), 'confirmed', ['synthetic-group'])
    calls, providers = [], []
    if helper is oppo:
        _mock_oppo(tmp_path, monkeypatch, failure, seeds, remote_ids, cloud_ids, images, calls, providers)
    else:
        _mock_vivo(tmp_path, monkeypatch, failure, seeds, remote_ids, cloud_ids, images, calls, providers, account)
    # These sentinels protect the user's private Vivo notes, including an incomplete global cache.
    monkeypatch.setattr(Store, 'notes', lambda *a: pytest.fail('Fixed sources never read account cache notes'))
    return SimpleNamespace(private=private, store=store, account=account, seeds=seeds,
                           ids=remote_ids, calls=calls, providers=providers, platform=platform)


def _mock_oppo(root, monkeypatch, failure, seeds, identities, assets, images, calls, providers):
    details = {}
    for index, seed in enumerate(seeds):
        markup = []
        mapping = {a.id: f'synthetic-attach-{index}-{i}' for i, a in enumerate(seed.attachments)}
        for block, ordinal in numbered_blocks(seed.blocks):
            text = ''.join(span_html(s) for s in block.spans)
            if block.kind == 'attachment':
                markup.append(f'<img src="{mapping[block.attachment_id]}" attachid="{mapping[block.attachment_id]}">')
            elif block.kind == 'todo':
                state = 'true' if block.checked else 'false'
                markup.append(f'<ul data-type="taskList"><li data-type="taskItem" data-checked="{state}"><div><p>{text}</p></div></li></ul>')
            elif block.kind == 'heading':
                markup.append(f'<h{block.level}>{text}</h{block.level}>')
            else:
                markup.append(block_html(block, {}, ordinal))
        markup.append('<p>' + escape(stamp(seed)).replace('\n', '<br>') + '</p>')
        details[identities[index]] = dict(recordId=identities[index], version='version-1', status=0,
            groupGuid='synthetic-group' if index else DEFAULT_GROUP, rawTitle=seed.title, rawText=''.join(markup),
            attachments=[dict(id=mapping[a.id], type=0, url='/' + assets[index][i]) for i, a in enumerate(seed.attachments)])
    class Wire:
        def __init__(self, value):
            self.payload = dict(key='synthetic-key', iv='synthetic-iv', encryptContent=json.dumps(value))
        def decrypt(self, value):
            return dict(code=0, data=value)
        def close(self):
            self.payload.clear()
    monkeypatch.setattr(importlib.import_module('note_bridge.providers.oppo'), 'OppoRequest', Wire)
    def create(platform, jars, resources):
        session = requests.Session()
        session.cookies.set('synthetic', 'private-token', domain='owork-api-cn.oppo.com', path='/')
        def request(method, url, **kwargs):
            path = urlsplit(url).path
            data = json.loads(kwargs['json']['encryptContent']) if method == 'POST' else {}
            calls.append((method, path, data.get('recordId')))
            if path.endswith('/account/v1/userInfo'):
                value = {'ssoId': 'other' if failure == 'account' else 'synthetic-user'}
            elif path.endswith('/group-list-new'):
                value = [dict(groupGuid=DEFAULT_GROUP, groupName=''),
                         dict(groupGuid='synthetic-group', groupName=seeds[1].source_folder_name)]
                if failure in ('optional_names_missing', 'optional_names_null'):
                    value.insert(0, {'groupGuid': ''})
                    if failure == 'optional_names_missing':
                        value[1].pop('groupName')
                    else:
                        value[0]['groupName'] = value[1]['groupName'] = None
                elif failure == 'named_name_missing':
                    value[1].pop('groupName')
                elif failure == 'group_identity_invalid':
                    value[0]['groupGuid'] = None
                elif failure == 'groups_duplicate_same':
                    value.append(dict(value[1]))
                elif failure == 'unrelated_group_conflict':
                    value.extend([dict(groupGuid='unrelated-group', groupName=name) for name in ('other-a', 'other-b')])
                elif failure == 'selected_group_conflict':
                    value.append(dict(groupGuid='synthetic-group', groupName='conflicting name must not be logged'))
                elif failure == 'default_group_duplicate':
                    value.append(dict(groupGuid=DEFAULT_GROUP, groupName=None if
                        sum(p.endswith('/group-list-new') for _, p, _ in calls) == 1 else ''))
            elif path.endswith('/info'):
                assert data['recordId'] in identities, 'Unselected/private IDs must never reach the wire'
                value = deepcopy(details[data['recordId']])
                if failure == 'identity':
                    value['recordId'] = 'private-unselected'
                elif failure == 'resource':
                    value['attachments'][0]['url'] = '/private-unselected-resource'
                elif failure == 'group':
                    value['groupGuid'] = 'wrong-group'
                elif failure == 'version' and sum(i == data['recordId'] for _, _, i in calls) > 1:
                    value['version'] = 'version-2'
            elif '/file-download/' in path:
                resource = path.rsplit('/', 1)[-1]
                assert resource in sum(assets, []), 'Only receipt-bound originals may be downloaded'
                value = b'wrong bytes' if failure == 'image' else images[int(resource[-1])]
                if failure == 'network':
                    raise requests.ConnectionError('private-token secret exception')
            else:
                pytest.fail('Full listings, handwriting and every write are forbidden')
            response = requests.Response()
            response.status_code, response.headers = 200, {}
            response._content = value if isinstance(value, bytes) else json.dumps(dict(code=0, data=value)).encode()
            response._content_consumed = True
            return response
        session.request = request
        provider = OppoProvider(Transport('https://owork-api-cn.oppo.com', ('oppo.com',), session=session), resources)
        providers.append(provider)
        return provider
    monkeypatch.setattr(oppo, 'create_provider', create)


def _mock_vivo(root, monkeypatch, failure, seeds, identities, assets, images, calls, providers, account):
    # The existing read_notes helper has separate HTTP-boundary tests. Here the contract
    # must select the two original receipts and preserve its exact scope and warnings.
    from vivo_scoped_readback import ScopedReadback
    def create(platform, jars, resources):
        provider = SimpleNamespace(resources=resources, account_id=account, closed=False)
        provider.close = lambda: setattr(provider, 'closed', True)
        providers.append(provider)
        return provider
    def verify(provider):
        calls.append(('identity', None))
        return 'wrong-account' if failure == 'account' else account
    def read(provider, exact_ids, context):
        assert exact_ids == tuple(identities), 'Only M2 and V1, never private/cache-based IDs'
        calls.append(('read_notes', exact_ids))
        notes, resource_keys = [], {}
        for index, seed in enumerate(seeds):
            note = seed.model_copy(deep=True, update=dict(platform=PlatformId.VIVO, account_id=account,
                source_id=identities[index], source_folder_id='synthetic-group'))
            mapping = {}
            resource_keys[identities[index]] = {}
            for i, asset in enumerate(note.attachments):
                mapping[asset.id] = f'synthetic-resource-guid-{index}-{i}'
                asset.id = mapping[asset.id]
                resource_keys[identities[index]][asset.id] = assets[index][i]
                path = provider.resources / f'image-{index}-{i}.bin'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'wrong' if failure == 'image' else images[i])
                asset.local_path = path.relative_to(provider.resources).as_posix()
            for block in note.blocks:
                if block.kind == 'attachment':
                    block.attachment_id = mapping[block.attachment_id]
            note.blocks.append(Block(spans=[{'text': stamp(seed)}]))
            if failure == 'identity':
                note.source_id = 'private-unselected'
            elif failure == 'group':
                note.source_folder_id = 'wrong-group'
            elif failure == 'resource':
                note.attachments[0].id = 'private-unselected-resource'
            elif failure == 'warning':
                note.warnings = ['Unknown content omitted']
            elif failure == 'resource_key':
                resource_keys[identities[index]][note.attachments[0].id] = 'unreceipted-meta-id'
            notes.append(note)
        context.update(total=2, completed=2, succeeded=2)
        return ScopedReadback(account, exact_ids, notes, failure != 'incomplete', resource_keys)
    monkeypatch.setattr(vivo, 'create_provider', create)
    monkeypatch.setattr(vivo, 'verify_account', verify)
    monkeypatch.setattr(vivo, 'read_notes', read)


@pytest.mark.parametrize('helper', [oppo, vivo], ids=['oppo-F2-OR1', 'vivo-M2-V1'])
def test_capture_is_receipt_bound_reusable_and_does_not_complete_the_account_cache(tmp_path, monkeypatch, helper):
    ctx = setup(tmp_path, monkeypatch, helper)
    before = (ctx.private / 'session-lab/notes.sqlite').read_bytes()
    outcome = helper.capture([], tmp_path)
    assert outcome['status'] == 'captured', outcome
    assert outcome['whole_account_snapshot'] is False and outcome['scope_complete'] is True
    assert (ctx.private / 'session-lab/notes.sqlite').read_bytes() == before
    assert ctx.store.snapshot(ctx.platform, ctx.account) == {'complete': False, 'count': 0}
    notes = helper.verify_source(outcome['proof'], ctx.store, tmp_path)
    assert [n.source_id for n in notes] == ctx.ids and len(notes) == 2
    request_count = len(ctx.calls)
    assert helper.verify_source(outcome['proof'], ctx.store, tmp_path) == notes
    assert len(ctx.calls) == request_count, 'Each next direction must reuse the same captured scope without rereading'
    if helper is oppo:
        assert request_count == 12 and outcome['physical_requests'] == 12
        assert notes[0].source_folder_id == DEFAULT_GROUP and not notes[0].source_folder_name
        assert notes[1].source_folder_name == ctx.seeds[1].source_folder_name
        assert not ctx.providers[0].transport.session.cookies
    else:
        assert ctx.calls == [('identity', None), ('read_notes', tuple(ctx.ids)), ('identity', None)]
        assert ctx.providers[0].closed
        report = json.loads((tmp_path / outcome['proof']['path']).read_text('utf8'))
        for note, ref in zip(notes, helper.bindings(ctx.store, tmp_path)[0], strict=True):
            assert sorted(a.id for a in note.attachments) != ref['asset_ids']
            assert sorted(report['resource_keys'][note.source_id].values()) == ref['asset_ids']
    assert 'private-token' not in json.dumps(outcome)


@pytest.mark.parametrize('helper,failure', [(h, f) for h in (oppo, vivo)
    for f in ('account', 'identity', 'resource', 'group', 'image')] +
    [(oppo, 'version'), (oppo, 'network'), (vivo, 'warning'), (vivo, 'incomplete'), (vivo, 'resource_key')])
def test_partial_wrong_or_changed_read_cannot_become_a_source(tmp_path, monkeypatch, helper, failure):
    ctx = setup(tmp_path, monkeypatch, helper, failure)
    before = (ctx.private / 'session-lab/notes.sqlite').read_bytes()
    outcome = helper.capture([], tmp_path)
    assert outcome['status'] == 'blocked' and outcome['scope_complete'] is False, outcome
    assert (ctx.private / 'session-lab/notes.sqlite').read_bytes() == before
    notes_path = (tmp_path / outcome['proof']['path']).with_name('notes.json')
    retained = helper is vivo and failure in ('resource', 'group', 'image', 'warning', 'resource_key')
    assert notes_path.exists() is retained
    if retained:
        report = json.loads((tmp_path / outcome['proof']['path']).read_text('utf8'))
        assert hashlib.sha256(notes_path.read_bytes()).hexdigest() == report['notes_sha256']
        assert {note['source_id'] for note in json.loads(notes_path.read_bytes())} == set(ctx.ids)
        assert any(not passed for row in report['checks'] for passed in row['checks'].values())
    assert 'private-token' not in (tmp_path / outcome['proof']['path']).read_text('utf-8')
    with pytest.raises(BridgeError):
        helper.verify_source(outcome['proof'], ctx.store, tmp_path)
    if failure == 'network':
        assert sum('/file-download/' in row[1] for row in ctx.calls) == 1, 'A failed resource cannot trigger an unbounded retry'


@pytest.mark.parametrize('helper', [oppo, vivo], ids=['oppo', 'vivo'])
@pytest.mark.parametrize('tamper', ['unknown', 'manifest', 'consumed', 'whole_scope', 'docs', 'original', 'receipt_resource'])
def test_reuse_recomputes_lineage_and_integrity_instead_of_trusting_status(tmp_path, monkeypatch, helper, tamper):
    ctx = setup(tmp_path, monkeypatch, helper)
    outcome = helper.capture([], tmp_path)
    assert outcome['status'] == 'captured', outcome
    reference, refs = outcome['proof'], helper.bindings(ctx.store, tmp_path)[0]
    path = tmp_path / reference['path']
    if tamper == 'unknown':
        ctx.store.save_receipt(refs[0]['receipt_key'], 'uncertain')
    elif tamper == 'receipt_resource':
        with ctx.store.connection() as db:
            db.execute('UPDATE resource_receipts SET status=? WHERE receipt_key=?', ('uncertain', refs[0]['receipt_key']))
    elif tamper in ('manifest', 'consumed'):
        name = helper.CONSUMED[1] if helper is oppo and tamper == 'consumed' else helper.MANIFESTS[0]
        manifest = ctx.private / 'checkpoints' / name
        job = json.loads(manifest.read_text('utf-8'))
        if tamper == 'manifest':
            job['from_cloud_fixture'] = {'platform': 'other', 'source_id': 'derived'}
        else:
            job.pop('consumed_at', None)
        manifest.write_text(json.dumps(job), 'utf-8')
    elif tamper == 'whole_scope':
        proof = json.loads(path.read_text('utf-8'))
        proof['whole_account_snapshot'] = True
        path.write_text(json.dumps(proof), 'utf-8')
        reference['sha256'] = helper.digest(path.read_bytes())
    elif tamper == 'docs':
        path.with_name('notes.json').write_text('[]', 'utf-8')
    else:
        note = helper.verify_source(reference, ctx.store, tmp_path)[0]
        (ctx.private / 'session-lab/resources' / note.attachments[0].local_path).write_bytes(b'changed')
    with pytest.raises(BridgeError):
        helper.verify_source(reference, ctx.store, tmp_path)


@pytest.mark.parametrize('helper', [oppo, vivo], ids=['oppo', 'vivo'])
def test_unconfirmed_original_is_refused_before_acquiring_a_session(tmp_path, monkeypatch, helper):
    ctx = setup(tmp_path, monkeypatch, helper)
    key = helper.bindings(ctx.store, tmp_path)[0][1]['receipt_key']
    ctx.store.save_receipt(key, 'uncertain')
    outcome = helper.capture([], tmp_path)
    assert outcome['status'] == 'blocked' and ctx.providers == [] and ctx.calls == []


@pytest.mark.parametrize('attempt', ['list', 'private_info', 'write', 'outside_file', 'file_params', 'envelope_without_scope'])
def test_oppo_guard_stops_scope_expansion_before_the_physical_request(tmp_path, monkeypatch, attempt):
    ctx = setup(tmp_path, monkeypatch, oppo)
    refs, _ = oppo.bindings(ctx.store, tmp_path)
    provider = oppo.create_provider('oppo', [], tmp_path / 'unused-resources')
    safe_file = oppo.PREFIX + 'file-download/' + refs[0]['cloud_ids'][0]
    try:
        with oppo.read_guard(provider, refs, {safe_file}, {}), pytest.raises(BridgeError):
            if attempt == 'list':
                provider._json('/web/note/v2/list', {})
            elif attempt == 'private_info':
                provider._json('/web/note/v2/info', {'recordId': 'private-unselected', 'status': 0, 'version': ''},
                               params={'recordId': 'private-unselected', 'version': ''})
            elif attempt == 'write':
                provider._json('/web/note/v2/info', {}, write=True)
            elif attempt == 'envelope_without_scope':
                provider.transport.json('POST', oppo.PREFIX + 'info',
                    json={'key': 'synthetic', 'iv': 'synthetic', 'encryptContent': '{}'})
            else:
                provider.transport.request('GET', safe_file if attempt == 'file_params' else safe_file + '-other',
                    params={'recordId': 'private-unselected'} if attempt == 'file_params' else None)
        assert ctx.calls == []
    finally:
        provider.close()


@pytest.mark.parametrize('shape', ['optional_names_missing', 'optional_names_null'])
def test_oppo_system_group_names_follow_production_contract_without_losing_named_group_binding(tmp_path, monkeypatch, shape):
    ctx = setup(tmp_path, monkeypatch, oppo, shape)
    result = oppo.capture([], tmp_path)
    assert result['status'] == 'captured', result
    notes = oppo.verify_source(result['proof'], ctx.store, tmp_path)
    assert notes[0].source_folder_id == DEFAULT_GROUP and not notes[0].source_folder_name
    assert notes[1].source_folder_name == ctx.seeds[1].source_folder_name
    assert result['physical_requests'] == 12


@pytest.mark.parametrize('shape,code', [('named_name_missing', 'oppo_named_group_mismatch'),
                                      ('group_identity_invalid', 'oppo_group_list_shape')])
def test_oppo_invalid_group_identity_or_missing_original_name_stops_before_body_reads(tmp_path, monkeypatch, shape, code):
    ctx = setup(tmp_path, monkeypatch, oppo, shape)
    result = oppo.capture([], tmp_path)
    assert result['status'] == 'blocked' and result['code'] == code
    assert result['physical_requests'] == 2
    assert not any(row[1].endswith('/info') for row in ctx.calls)


@pytest.mark.parametrize('shape', ['groups_duplicate_same', 'unrelated_group_conflict', 'default_group_duplicate'])
def test_oppo_duplicate_rows_do_not_reject_unambiguous_fixed_source_content(tmp_path, monkeypatch, shape):
    ctx = setup(tmp_path, monkeypatch, oppo, shape)
    result = oppo.capture([], tmp_path)
    assert result['status'] == 'captured', result
    notes = oppo.verify_source(result['proof'], ctx.store, tmp_path)
    assert len(notes) == 2 and notes[1].source_folder_name == ctx.seeds[1].source_folder_name
    assert len(result['group_shapes']) == 2
    counts = result['group_shapes'][0]
    assert counts['other_duplicate_rows' if shape == 'unrelated_group_conflict' else 'selected_duplicate_rows'] == 1
    assert all(value not in json.dumps(result) for value in ('synthetic-group', 'other-a', 'other-b', ctx.seeds[1].source_folder_name))


def test_oppo_conflicting_selected_named_group_is_blocked_before_any_body_or_image(tmp_path, monkeypatch):
    ctx = setup(tmp_path, monkeypatch, oppo, 'selected_group_conflict')
    result = oppo.capture([], tmp_path)
    assert result['status'] == 'blocked' and result['code'] == 'oppo_named_group_mismatch'
    assert result['physical_requests'] == 2
    assert result['group_shapes'][0]['selected'][1] == {
        'index': 1, 'rows': 2, 'name_types': {'str': 2}, 'distinct_name_values': 2}
    assert 'conflicting name must not be logged' not in (tmp_path / result['proof']['path']).read_text('utf-8')
    assert not any(row[1].endswith('/info') for row in ctx.calls)

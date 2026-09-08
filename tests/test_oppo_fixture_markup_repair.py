"""A consumed, receipt-bound repair may edit once, and may never hide semantic loss."""
import importlib
import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests
from PIL import Image

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, account_fingerprint
from note_bridge.providers.oppo import OppoProvider, parse_entry
from note_bridge.providers.transport import Transport

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
helper = importlib.import_module('oppo_fixture_markup_repair')


class Envelope:
    def __init__(self, value):
        self.payload = {'encryptContent': json.dumps(value), 'key': 'RAM_ONLY_SECRET', 'iv': 'RAM_ONLY_SECRET'}

    def decrypt(self, value):
        return {'code': 0, 'data': value}

    def close(self):
        self.payload.clear()


def authorize(ctx):
    job = dict(kind='oppo-fixture-markup-repair', id='oppo-markup-repair-' + uuid4().hex,
        operation=helper.OPERATION, target='oppo', expected_account=ctx.account,
        fixture_scopes=ctx.scopes, armed=False, consumed_at=datetime.now(timezone.utc).isoformat())
    path = ctx.root / '.private/checkpoints' / (job['id'] + '-consumed-job.json')
    helper.exclusive(path, job)
    return {'path': path.relative_to(ctx.root).as_posix(), 'sha256': helper.digest(path.read_bytes())}


def synthetic(tmp_path, monkeypatch, *, noop=False, fault=None, count=1):
    (tmp_path / '.private/checkpoints').mkdir(parents=True)
    (tmp_path / '.private/evidence').mkdir()
    picture = BytesIO()
    Image.new('RGB', (4, 4), 'purple').save(picture, format='PNG')
    image = picture.getvalue()
    assets = [Attachment(id=f'asset-{i}', name=f'cloud-{i}', kind='image', mime='image/png',
        size=len(image), sha256=helper.digest(image), local_path=f'cached-{i}.png') for i in range(2)]
    account = account_fingerprint('oppo', 'synthetic-user')
    markup = ('<p><em>italic</em> <u>under</u> <mark>highlight</mark> <code>inline code</code></p>'
              '<hr><pre><code>literal &lt;em&gt; inside code</code></pre>'
              '<p><img src="asset-0" attachid="asset-0"><img src="asset-1" attachid="asset-1"></p>')
    if noop:
        markup = helper.transform(markup)[0]
    detail = dict(recordId='only-generated-id', status=0, category=2, version='old-version', groupGuid='generated-group',
        rawTitle='笔记互迁验收 · synthetic', rawText=markup, createTime=1700000000000, updateTime=1700000000000,
        attachments=[dict(id=a.id, url='/' + a.name, type=0, checkPayload='synthetic-check') for a in assets])
    note = parse_entry(detail, account, {'generated-group': '笔记互迁分组验收 synthetic'}, assets)
    scopes = [{'manifest': helper.native.OR1, 'index': None}]
    scope = dict(fixtureId=note.source_id, group_id=note.source_folder_id, group_name=note.source_folder_name,
        receipt_key='synthetic-confirmed-receipt', target_fingerprint=note.fingerprint(),
        image_specs=[{'id': a.id, 'cloud_id': a.name} for a in assets], **helper.native.render_expectations(note))
    bindings = [{'account': account, 'scope': scope, 'request': scopes[0]}]
    notes = [note]
    details = {note.source_id: detail}
    if count == 2:
        second = note.model_copy(deep=True, update={'source_id': 'second-generated-id'})
        notes.append(second)
        scopes.append({'manifest': 'wps-oppo-BI1-job.json', 'index': 0})
        second_scope = {**scope, 'fixtureId': second.source_id, 'receipt_key': 'second-confirmed-receipt',
                        'target_fingerprint': second.fingerprint()}
        bindings.append({'account': account, 'scope': second_scope, 'request': scopes[1]})
        details[second.source_id] = {**deepcopy(detail), 'recordId': second.source_id}
    ctx = SimpleNamespace(root=tmp_path, account=account, scopes=scopes, scope=scope, note=note,
        bindings=bindings, note_list=notes, detail=detail, calls=[], edits=0, providers=[])
    monkeypatch.setattr(helper, 'audit', lambda root, supplied: (deepcopy(bindings), deepcopy(notes)))
    monkeypatch.setattr(helper, 'OppoRequest', Envelope)

    def factory(platform, jars, resources):
        assert platform == 'oppo' and jars == ['RAM_ONLY_SECRET']
        session = requests.Session()
        session.cookies.set('session', 'RAM_ONLY_SECRET')
        provider = OppoProvider(Transport('https://owork-api-cn.oppo.com', ('oppo.com',), session=session), resources)
        ctx.providers.append(provider)

        def request(method, url, **kwargs):
            path = url.removeprefix('https://owork-api-cn.oppo.com')
            ctx.calls.append((method, path))
            response = requests.Response()
            response.status_code = 200
            response._content_consumed = True
            if path.endswith('userInfo'):
                data = {'ssoId': 'synthetic-user'}
            elif path.endswith('group-list-new'):
                data = [{'groupGuid': note.source_folder_id, 'groupName': note.source_folder_name}]
            elif path.endswith('/info'):
                identifier = kwargs['params']['recordId']
                assert kwargs['params'] == {'recordId': identifier, 'version': ''} and identifier in details
                data = deepcopy(details[identifier])
                if fault == 'before_body' and not ctx.edits:
                    data['rawText'] += '<p>unrelated newly edited text</p>'
                if fault == 'after_body' and ctx.edits:
                    data['rawText'] += '<p>unexpected loss or mutation</p>'
                if fault == 'missing_time' and ctx.edits:
                    data.pop('updateTime')
            elif path.endswith('/edit'):
                ctx.edits += 1
                payload = json.loads(kwargs['json']['encryptContent'])
                identifier = payload['recordId']
                binding = next(binding for binding in bindings if binding['scope']['fixtureId'] == identifier)
                marker = helper.intent_path(tmp_path, binding)
                assert json.loads(marker.read_text('utf-8'))['state'] == 'write_may_have_been_sent'
                assert set(payload) == {'recordId', 'version', 'operatorType', 'hypertext'}
                assert payload['operatorType'] == 'modify' and identifier in details
                assert kwargs['params'] == {'recordId': identifier, 'version': 'old-version'}
                assert payload['version'] == 'old-version'
                if fault == 'timeout':
                    raise requests.Timeout('RAM_ONLY_SECRET')
                if fault in ('1202', 'nonzero'):
                    response._content = json.dumps({'code': 1202 if fault == '1202' else 998, 'data': {}}).encode()
                    return response
                details[identifier].update(payload['hypertext'], version='new-version-😀', updateTime=1700000001000)
                data = {'recordId': identifier, 'version': 'new-version-😀'}
                if fault == 'unknown_ack':
                    data['recordId'] = 'another-id'
                if fault == 'ack_without_id':
                    data.pop('recordId')
            elif path.startswith(helper.PREFIX + 'file-download/'):
                assert path.rsplit('/', 1)[1] in {'cloud-0', 'cloud-1'}
                response.headers['Content-Type'] = 'image/png'
                response._content = image
                return response
            else:
                pytest.fail('The repair must not list private notes or add/upload/delete anything.')
            response._content = json.dumps({'code': 0, 'data': data}).encode()
            return response

        session.request = request
        return provider

    monkeypatch.setattr(helper, 'create_provider', factory)
    ctx.authorization = authorize(ctx)
    return ctx


@pytest.mark.parametrize('source', [None, 'wps'])
def test_audit_reuses_original_receipt_and_scope_contract_without_modifying_sqlite(tmp_path, monkeypatch, source):
    from test_oppo_native_scope import setup
    ctx = setup(tmp_path, monkeypatch, source)
    before = (ctx.private / 'session-lab/notes.sqlite').read_bytes()
    bindings, notes = helper.audit(tmp_path, [ctx.selected])
    assert bindings[0]['scope']['receipt_key'] == ctx.keys[0]
    assert notes[0].fingerprint() == ctx.restored[0].fingerprint()
    assert (ctx.private / 'session-lab/notes.sqlite').read_bytes() == before
    ctx.store.save_receipt(ctx.keys[0], 'uncertain')
    with pytest.raises((ValueError, BridgeError)):
        helper.audit(tmp_path, [ctx.selected])


def test_transform_changes_only_official_markup_and_keeps_code_text():
    value = '<p><em>A &amp; B</em><code>&lt;em&gt;literal</code></p><hr /><hr class="custom"><pre>X\nY</pre>'
    actual, counts = helper.transform(value)
    assert actual == '<p><i>A &amp; B</i><code>&lt;em&gt;literal</code></p><hr class="hr-style-solid"><hr class="custom"><pre>X\nY</pre>'
    assert counts == {'em_tags': 1, 'bare_hr_tags': 1, 'mark_tags': 0}


def test_official_highlight_class_and_style_preserve_api_highlight_without_changing_underline():
    raw = '<p><mark><u>yellow underline</u></mark><code>plain code</code></p>'
    changed, counts = helper.transform(raw)
    assert changed == ('<p><span class="highlight_color_yellow" style="background-color: rgba(255, 226, 39, 0.4)">'
                       '<u>yellow underline</u></span><code>plain code</code></p>')
    entry = {'recordId': 'synthetic', 'rawText': raw, 'status': 0}
    before = parse_entry(entry, 'a' * 64, {})
    after = parse_entry({**entry, 'rawText': changed}, 'a' * 64, {})
    assert helper.canonical(before) == helper.canonical(after)
    assert counts == {'em_tags': 0, 'bare_hr_tags': 0, 'mark_tags': 1}
    escaped = '<p>&lt;mark&gt;literal&lt;/mark&gt; &lt;em&gt; &lt;hr&gt;</p>'
    assert helper.transform(escaped) == (escaped, {'em_tags': 0, 'bare_hr_tags': 0, 'mark_tags': 0})


@pytest.mark.parametrize('value', ['<em class="unknown">x</em>', '<em>x', '<EM>x</EM>', '<!-- <em>x</em> -->', '<!-- <hr> -->',
    '<mark style="background:red">x</mark>', '<mark>x', '<!-- <mark>x</mark> -->'])
def test_noncanonical_markup_cannot_turn_global_replacement_into_other_changes(value):
    with pytest.raises(BridgeError):
        helper.transform(value)


def test_verified_one_shot_binds_version_and_preserves_archival_code_expectations(tmp_path, monkeypatch):
    ctx = synthetic(tmp_path, monkeypatch)
    result = helper.repair(['RAM_ONLY_SECRET'], tmp_path, ctx.scopes, authorization=ctx.authorization)
    assert result['status'] == 'verified' and result['edit_attempts'] == ctx.edits == 1
    assert len(ctx.calls) == 9  # 2 account, 2 groups, 2 exact detail, 2 originals, 1 edit.
    projected = helper.verify_repair(result['proof'], tmp_path, ctx.account, ctx.scope)
    assert projected['expected_version_sha256'] == helper.digest('new-version-😀')
    assert projected['target_fingerprint'] != ctx.note.fingerprint()
    assert projected['expectedText'] == ctx.note.plain_text
    assert projected['codes'] == ctx.scope['codes'] and projected['codes']
    assert projected['visual_degradations'] == helper.visual_degradations(ctx.note)
    assert all(not provider.transport.session.cookies for provider in ctx.providers)
    for path in (tmp_path / '.private').rglob('*.json'):
        assert 'RAM_ONLY_SECRET' not in path.read_text('utf-8')
    with pytest.raises(BridgeError) as rejected:
        helper.repair(['RAM_ONLY_SECRET'], tmp_path, ctx.scopes, authorization=authorize(ctx))
    assert rejected.value.code == 'markup_repair_intent_exists' and ctx.edits == 1


def test_noop_records_read_only_version_without_edit_or_repair_intent(tmp_path, monkeypatch):
    ctx = synthetic(tmp_path, monkeypatch, noop=True)
    result = helper.repair(['RAM_ONLY_SECRET'], tmp_path, ctx.scopes, authorization=ctx.authorization)
    assert result['status'] == 'verified' and result['edit_attempts'] == ctx.edits == 0
    assert len(ctx.calls) == 6
    assert not helper.intent_path(tmp_path, ctx.bindings[0]).exists()
    projected = helper.verify_repair(result['proof'], tmp_path, ctx.account, ctx.scope)
    assert projected['expected_version_sha256'] == helper.digest('old-version')
    assert projected['target_fingerprint'] == ctx.note.fingerprint()
    assert projected['visual_degradations']['inline_code_runs_plaintext'] == 1


def test_ack_without_id_requires_fresh_exact_id_and_same_new_version_readback(tmp_path, monkeypatch):
    ctx = synthetic(tmp_path, monkeypatch, fault='ack_without_id')
    result = helper.repair(['RAM_ONLY_SECRET'], tmp_path, ctx.scopes, authorization=ctx.authorization)
    assert result['status'] == 'verified' and ctx.edits == 1
    projected = helper.verify_repair(result['proof'], tmp_path, ctx.account, ctx.scope)
    assert projected['fixtureId'] == ctx.note.source_id
    assert projected['expected_version_sha256'] == helper.digest('new-version-😀')


def test_two_notes_sharing_verified_originals_download_each_cloud_file_only_once(tmp_path, monkeypatch):
    ctx = synthetic(tmp_path, monkeypatch, count=2)
    result = helper.repair(['RAM_ONLY_SECRET'], tmp_path, ctx.scopes, authorization=ctx.authorization)
    assert result['status'] == 'verified' and result['edit_attempts'] == ctx.edits == 2
    assert len([path for _, path in ctx.calls if '/file-download/' in path]) == 2
    for binding in ctx.bindings:
        assert helper.verify_repair(result['proof'], tmp_path, ctx.account, binding['scope'])['repair_proof'] == result['proof']


def test_conflicting_shared_cloud_identity_blocks_the_entire_batch_before_first_edit(tmp_path, monkeypatch):
    ctx = synthetic(tmp_path, monkeypatch, count=2)
    ctx.note_list[1].attachments[0].sha256 = 'f' * 64
    with pytest.raises(BridgeError) as rejected:
        helper.repair(['RAM_ONLY_SECRET'], tmp_path, ctx.scopes, authorization=ctx.authorization)
    assert rejected.value.code == 'markup_repair_attachment_changed' and not ctx.calls


@pytest.mark.parametrize('fault', ['timeout', '1202', 'nonzero', 'unknown_ack', 'after_body', 'missing_time'])
def test_ambiguous_write_or_semantic_readback_failure_stays_pending_and_cannot_replay(tmp_path, monkeypatch, fault):
    ctx = synthetic(tmp_path, monkeypatch, fault=fault)
    result = helper.repair(['RAM_ONLY_SECRET'], tmp_path, ctx.scopes, authorization=ctx.authorization)
    assert result['status'] == 'needs_review' and ctx.edits == 1
    with pytest.raises(BridgeError):
        helper.verify_repair(result['proof'], tmp_path, ctx.account, ctx.scope)
    with pytest.raises(BridgeError) as rejected:
        helper.repair(['RAM_ONLY_SECRET'], tmp_path, ctx.scopes, authorization=authorize(ctx))
    assert rejected.value.code == 'markup_repair_intent_exists' and ctx.edits == 1


def test_changed_remote_content_blocks_before_intent_and_edit(tmp_path, monkeypatch):
    ctx = synthetic(tmp_path, monkeypatch, fault='before_body')
    result = helper.repair(['RAM_ONLY_SECRET'], tmp_path, ctx.scopes, authorization=ctx.authorization)
    assert result['status'] == 'blocked' and ctx.edits == 0
    assert not helper.intent_path(tmp_path, ctx.bindings[0]).exists()


@pytest.mark.parametrize('tamper', ['armed', 'account', 'requests', 'hash', 'extra'])
def test_only_consumed_exact_authorization_can_start_account_reads(tmp_path, monkeypatch, tamper):
    ctx = synthetic(tmp_path, monkeypatch)
    ref = ctx.authorization
    path = tmp_path / ref['path']
    job = json.loads(path.read_text('utf-8'))
    if tamper == 'armed':
        job['armed'] = True
    elif tamper == 'account':
        job['expected_account'] = 'other-account'
    elif tamper == 'requests':
        job['fixture_scopes'] = [{'manifest': 'unrelated-job.json', 'index': 0}]
    elif tamper == 'extra':
        job['allow_any_note'] = True
    path.write_text(json.dumps(job), 'utf-8')
    ref['sha256'] = '0' * 64 if tamper == 'hash' else helper.digest(path.read_bytes())
    with pytest.raises(BridgeError):
        helper.repair(['RAM_ONLY_SECRET'], tmp_path, ctx.scopes, authorization=ref)
    assert ctx.calls == [] and ctx.edits == 0


@pytest.mark.parametrize('field,value', [('status', 'needs_review'), ('account', 'other'), ('cache_writes', 1)])
def test_recomputed_file_hash_does_not_make_failed_or_wrong_account_proof_acceptable(tmp_path, monkeypatch, field, value):
    ctx = synthetic(tmp_path, monkeypatch)
    result = helper.repair(['RAM_ONLY_SECRET'], tmp_path, ctx.scopes, authorization=ctx.authorization)
    path = tmp_path / result['proof']['path']
    proof = json.loads(path.read_text('utf-8'))
    proof[field] = value
    path.write_text(json.dumps(proof), 'utf-8')
    result['proof']['sha256'] = helper.digest(path.read_bytes())
    with pytest.raises(BridgeError):
        helper.verify_repair(result['proof'], tmp_path, ctx.account, ctx.scope)


@pytest.mark.parametrize('method,url', [('POST', 'https://owork-api-cn.oppo.com/owork-server/web/note/v2/add'),
    ('GET', 'https://other.invalid/owork-server/web/account/v1/userInfo'),
    ('DELETE', 'https://owork-api-cn.oppo.com/owork-server/web/account/v1/userInfo')])
def test_physical_guard_rejects_unplanned_method_host_or_write_route(tmp_path, monkeypatch, method, url):
    ctx = synthetic(tmp_path, monkeypatch)
    provider = helper.create_provider('oppo', ['RAM_ONLY_SECRET'], tmp_path)
    wire = helper.ScopedWire(provider, ctx.bindings, {}, {})
    path = '/owork-server/web/account/v1/userInfo'
    with wire.guard(), wire.permit('GET', path, None, None, 'account:1'), pytest.raises(BridgeError):
        provider.transport.session.request(method, url, allow_redirects=False)
    assert ctx.calls == []


def test_only_seven_exact_manifests_and_distinct_original_target_indices_are_allowed():
    for manifest in helper.MANIFESTS:
        helper.validate_requests([{'manifest': manifest, 'index': None if manifest == helper.native.OR1 else 0}])
    for scopes in ([{'manifest': 'wps-oppo-BI99-job.json', 'index': 0}],
                   [{'manifest': helper.native.OR1, 'index': None}] * 2,
                   [{'manifest': helper.native.OR1, 'index': None, 'allow_any': True}]):
        with pytest.raises(BridgeError):
            helper.validate_requests(scopes)


@pytest.mark.parametrize('container', [None, [], 'missing'])
def test_zero_image_details_accept_only_official_empty_representations_without_inventing_assets(container):
    entry = {'recordId': 'generated-zero-image', 'version': 'version-1', 'status': 0,
             'groupGuid': 'group', 'rawTitle': 'synthetic', 'rawText': '<p>unchanged</p>'}
    if container != 'missing':
        entry['attachments'] = container
    cached = parse_entry(entry, 'a' * 64, {'group': 'synthetic'})
    binding = {'scope': {'fixtureId': cached.source_id}}
    wire = SimpleNamespace(json=lambda *args, **kwargs: deepcopy(entry))
    diagnostics = []
    detail, result = helper.read_detail(wire, binding, cached, {'group': 'synthetic'}, diagnostics)
    assert detail == entry and helper.canonical(result) == helper.canonical(cached)
    assert diagnostics[0]['cached_count'] == 0 and diagnostics[0]['container'] in ('null', 'list', 'missing')


@pytest.mark.parametrize('container', [None, [], {}, '', [{'id': 'unexpected', 'url': '/other', 'type': 0}]])
def test_empty_or_wrong_remote_assets_never_hide_an_expected_image(container):
    entry = {'recordId': 'generated-image', 'version': 'version-1', 'status': 0,
             'groupGuid': 'group', 'rawTitle': 'synthetic', 'rawText': '<p>unchanged</p>', 'attachments': container}
    cached = parse_entry({**entry, 'attachments': []}, 'a' * 64, {'group': 'synthetic'},
                         [Attachment(id='expected', name='expected-file', kind='image')])
    wire = SimpleNamespace(json=lambda *args, **kwargs: deepcopy(entry))
    with pytest.raises(BridgeError) as error:
        helper.read_detail(wire, {'scope': {'fixtureId': cached.source_id}}, cached, {'group': 'synthetic'})
    assert error.value.code == 'markup_repair_attachment_changed'


@pytest.mark.parametrize('code', ['platform_response', 'protocol_changed', 'login_incomplete'])
def test_account_failure_keeps_safe_protocol_code_and_never_claims_session_expiry(tmp_path, monkeypatch, code):
    ctx = synthetic(tmp_path, monkeypatch)

    def account(_):
        raise BridgeError(code, 'RAM_ONLY_SECRET')

    monkeypatch.setattr(helper.ScopedWire, 'account', account)
    result = helper.repair(['RAM_ONLY_SECRET'], tmp_path, ctx.scopes, authorization=ctx.authorization)
    assert result['status'] == 'blocked' and result['code'] == code and result['error_type'] == 'BridgeError'
    assert ctx.edits == 0 and not ctx.calls
    raw = (tmp_path / result['proof']['path']).read_text('utf-8')
    assert 'RAM_ONLY_SECRET' not in raw and 'session_expired' not in raw


@pytest.mark.parametrize('error,expected', [(ValueError('RAM_ONLY_SECRET'), 'ValueError'),
    (TypeError('RAM_ONLY_SECRET'), 'TypeError'), (BridgeError('RAM_ONLY_SECRET', 'RAM_ONLY_SECRET'), 'BridgeError'),
    (type('RAM_ONLY_SECRET', (Exception,), {})('RAM_ONLY_SECRET'), 'other')])
def test_diagnostics_never_serialize_untrusted_error_text_or_dynamic_class_name(error, expected):
    result = helper.safe_error(error)
    assert result == {'code': 'markup_repair_error', 'error_type': expected}
    assert 'RAM_ONLY_SECRET' not in json.dumps(result)

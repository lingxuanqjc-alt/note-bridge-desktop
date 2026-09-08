"""One-shot lab repair for original OR1 and the six confirmed incoming BI pairs.

No CLI, discovery hook, cache mutation or migration receipt mutation is provided.
repair() requires the dispatcher's immutable consumed-job descriptor. audit() is local
and returns generated note objects for callers, never for stdout. Existing intents
are never replayed, including when an earlier response or readback was uncertain.
"""
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

import oppo_native_scope as native
from lxml import html

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import NoteDocument, account_fingerprint
from note_bridge.paths import confined
from note_bridge.providers.factory import create_provider
from note_bridge.providers.oppo import parse_entry
from note_bridge.providers.oppo_wire import OppoRequest

OPERATION = 'oppo_web_markup_v1'
MANIFESTS = (native.OR1, 'wps-oppo-BI1-job.json', 'huawei-oppo-BI3-job.json',
             'xiaomi-oppo-BI5-job.json', 'honor-oppo-BI7-job.json',
             'meizu-oppo-BI9-job.json', 'vivo-oppo-BI11-job.json')
PROOF_PATTERN = r'\.private/evidence/oppo-markup-repair-[0-9a-f]{32}/proof\.json'
JOB_PATTERN = r'\.private/checkpoints/(oppo-markup-repair-[0-9a-f]{32})-consumed-job\.json'
PREFIX = '/owork-server/web/note/v2/'
ERROR_CODES = {'markup_repair_scope', 'markup_repair_noncanonical_em', 'markup_repair_noncanonical_hr',
    'markup_repair_noncanonical_mark',
    'markup_repair_request_blocked', 'markup_repair_request_repeated', 'markup_repair_response',
    'markup_repair_group_changed', 'markup_repair_attachment_changed', 'markup_repair_intent_exists',
    'markup_repair_cached_content_changed', 'markup_repair_semantics_changed', 'markup_repair_ack_unknown',
    'markup_repair_readback_changed', 'markup_repair_local_binding_changed', 'markup_repair_error',
    'account_changed', 'write_uncertain', 'network_error', 'session_expired', 'request_forbidden', 'rate_limited',
    'platform_response', 'protocol_changed', 'login_incomplete', 'login_required', 'request_rejected',
    'server_error', 'attachment_changed', 'attachment_response', 'attachment_mismatch'}


def safe_error(error):
    code = error.code if isinstance(error, BridgeError) else None
    result = {'code': code if isinstance(code, str) and code in ERROR_CODES else 'markup_repair_error'}
    result['error_type'] = next((name for kind, name in (
        (WriteUncertain, 'WriteUncertain'), (BridgeError, 'BridgeError'), (ValueError, 'ValueError'),
        (TypeError, 'TypeError'), (KeyError, 'KeyError'), (OSError, 'OSError'),
        (RuntimeError, 'RuntimeError'), (AssertionError, 'AssertionError')) if isinstance(error, kind)), 'other')
    return result


def require(condition, code='markup_repair_scope'):
    if not condition:
        raise BridgeError(code, 'OPPO 工具样例修复范围或内容核对未通过，未重复提交。')


def digest(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=True, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def exclusive(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def canonical(note, ignore_updated=False):
    value = note.model_dump(mode='json')
    for asset in value['attachments']:
        asset.pop('local_path', None)
    if ignore_updated:
        value.pop('updated_at')
    return value


def validate_requests(requests):
    require(isinstance(requests, list) and 1 <= len(requests) <= 2)
    for scope in requests:
        require(isinstance(scope, dict) and set(scope) == {'manifest', 'index'}
                and scope['manifest'] in MANIFESTS
                and ((scope['manifest'] == native.OR1 and scope['index'] is None)
                     or (scope['manifest'] != native.OR1 and type(scope['index']) is int and scope['index'] in (0, 1))))
    require(len({(scope['manifest'], scope['index']) for scope in requests}) == len(requests))


def intent_path(root, binding):
    identity = digest(['oppo', binding['account'], binding['scope']['fixtureId'], OPERATION])
    return root / '.private/evidence/oppo-markup-repair-intents' / (identity + '.json')


def audit(root, requests):
    validate_requests(requests)
    bindings, notes = [], []
    for request in requests:
        job = json.loads((root / '.private/checkpoints' / request['manifest']).read_text('utf-8'))
        account = job['expected_account']
        scope = native.browser_target(request, root, account)
        with closing(sqlite3.connect((root / '.private/session-lab/notes.sqlite').resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            row = db.execute('SELECT document FROM notes WHERE platform=? AND account=? AND source_id=?',
                             ('oppo', account, scope['fixtureId'])).fetchone()
        require(row is not None)
        note = NoteDocument.model_validate_json(row[0])
        require(not note.warnings and note.fingerprint() == scope['target_fingerprint'])
        bindings.append({'request': dict(request), 'account': account, 'scope': scope})
        notes.append(note)
    require(len({b['account'] for b in bindings}) == 1
            and len({b['scope']['fixtureId'] for b in bindings}) == len(bindings))
    return bindings, notes


def authorization_job(root, reference, requests, account):
    require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'})
    match = re.fullmatch(JOB_PATTERN, str(reference['path']))
    require(match and re.fullmatch(r'[0-9a-f]{64}', str(reference['sha256'])))
    raw = confined(root, reference['path']).read_bytes()
    require(digest(raw) == reference['sha256'])
    job = json.loads(raw)
    require(set(job) == {'kind', 'id', 'operation', 'target', 'expected_account', 'fixture_scopes', 'armed', 'consumed_at'}
            and job['kind'] == 'oppo-fixture-markup-repair' and job['id'] == match[1]
            and job['operation'] == OPERATION and job['target'] == 'oppo' and job['armed'] is False
            and job['expected_account'] == account and job['fixture_scopes'] == requests)
    consumed = datetime.fromisoformat(job['consumed_at'])
    require(consumed.tzinfo is not None and consumed <= datetime.now(timezone.utc))
    return job


def transform(markup):
    """Change only complete EM/MARK and bare HR; the official class preserves yellow."""
    require(isinstance(markup, str) and len(markup) <= 2000000)
    tree = html.fragment_fromstring(markup or '<p></p>', create_parent='div')
    em = list(tree.iter('em'))
    require(all(not node.attrib for node in em) and markup.count('<em>') == markup.count('</em>') == len(em),
            'markup_repair_noncanonical_em')
    marks = list(tree.iter('mark'))
    require(all(not node.attrib for node in marks) and markup.count('<mark>') == markup.count('</mark>') == len(marks),
            'markup_repair_noncanonical_mark')
    bare_hr = len([node for node in tree.iter('hr') if not node.attrib])
    pattern = r'<hr\s*/?>'
    require(len(re.findall(pattern, markup)) == bare_hr, 'markup_repair_noncanonical_hr')
    changed = re.sub(pattern, '<hr class="hr-style-solid">', markup.replace('<em>', '<i>').replace('</em>', '</i>'))
    changed = changed.replace('<mark>', '<span class="highlight_color_yellow" style="background-color: rgba(255, 226, 39, 0.4)">').replace('</mark>', '</span>')
    return changed, {'em_tags': len(em), 'bare_hr_tags': bare_hr, 'mark_tags': len(marks)}


def visual_degradations(note):
    return {'code_blocks_plaintext': sum(block.kind == 'code' for block in note.blocks),
            'inline_code_runs_plaintext': sum(bool(span.code) for block in note.blocks if block.kind != 'code' for span in block.spans),
            'api_code_semantics_preserved': True}


class ScopedWire:
    """Bind each physical request to this helper's exact plaintext plan; never retry writes."""
    def __init__(self, provider, bindings, plans, counts):
        self.provider, self.bindings, self.plans, self.counts = provider, bindings, plans, counts
        self.ids = {b['scope']['fixtureId'] for b in bindings}
        self.files = {a['cloud_id'] for b in bindings for a in b['scope']['image_specs']}
        self.allowed, self.logical = None, {}
        self.downloads = {}

    @contextmanager
    def guard(self):
        original = self.provider.transport.session.request

        def request(method, url, **kwargs):
            allowed = self.allowed
            parsed = urlsplit(url)
            require(allowed and parsed.scheme == 'https' and parsed.netloc == 'owork-api-cn.oppo.com'
                    and not parsed.query and not parsed.fragment and not kwargs.get('allow_redirects')
                    and method == allowed['method'] and parsed.path == allowed['path']
                    and kwargs.get('params') == allowed['params'] and kwargs.get('json') == allowed['json']
                    and not kwargs.get('data') and not kwargs.get('files'), 'markup_repair_request_blocked')
            key = allowed['key']
            require(self.counts.get(key, 0) == 0, 'markup_repair_request_repeated')
            self.counts[key] = 1
            return original(method, url, **kwargs)

        self.provider.transport.session.request = request
        try:
            yield self
        finally:
            self.allowed = None
            self.provider.transport.session.request = original

    @contextmanager
    def permit(self, method, path, params, payload, key):
        require(self.allowed is None)
        self.allowed = {'method': method, 'path': path, 'params': params, 'json': payload, 'key': key}
        try:
            yield
        finally:
            self.allowed = None

    def json(self, path, data=None, *, params=None, write=False):
        if path == 'group-list-new':
            require(data is None and params is None and not write)
            key, limit = 'groups', 2
        elif path == 'info':
            require(isinstance(data, dict) and data.get('recordId') in self.ids
                    and data == {'recordId': data['recordId'], 'status': 0, 'version': ''}
                    and params == {'recordId': data['recordId'], 'version': ''} and not write)
            key, limit = 'info:' + data['recordId'], 2
        else:
            require(path == 'edit' and write is True and isinstance(data, dict) and data.get('recordId') in self.plans
                    and data == self.plans[data['recordId']]
                    and params == {'recordId': data['recordId'], 'version': data['version']})
            key, limit = 'edit:' + data['recordId'], 1
        require(self.logical.get(key, 0) < limit, 'markup_repair_request_repeated')
        self.logical[key] = self.logical.get(key, 0) + 1
        wire = OppoRequest(data or {})
        try:
            with self.permit('POST', PREFIX + path, params, wire.payload, key + ':' + str(self.logical[key])):
                payload = self.provider.transport.json('POST', PREFIX + path, json=wire.payload, params=params, write=write)
            try:
                return self.provider._data(wire.decrypt(self.provider._data(payload)))
            except Exception:
                if write:
                    raise WriteUncertain() from None
                raise BridgeError('markup_repair_response', 'OPPO 只读响应无法确认。') from None
        finally:
            wire.close()

    def account(self):
        index = self.logical.get('account', 0) + 1
        require(index <= 2)
        self.logical['account'] = index
        path = '/owork-server/web/account/v1/userInfo'
        with self.permit('GET', path, None, None, 'account:' + str(index)):
            user = self.provider._data(self.provider.transport.json('GET', path))
        require(isinstance(user, dict) and type(user.get('ssoId')) in (str, int) and bool(user['ssoId']))
        account = account_fingerprint('oppo', str(user['ssoId']))
        require(account == self.bindings[0]['account'], 'account_changed')
        self.provider.account_id = account
        return account

    def download(self, asset):
        require(asset.name in self.files)
        restored = asset.model_copy(deep=True)
        if asset.name in self.downloads:
            previous = self.downloads[asset.name]
            require((previous.size, previous.sha256) == (asset.size, asset.sha256), 'markup_repair_attachment_changed')
            raw = confined(self.provider.resources, previous.local_path).read_bytes()
            require(len(raw) == asset.size and digest(raw) == asset.sha256, 'markup_repair_attachment_changed')
            restored.local_path = previous.local_path
            return restored
        path = PREFIX + 'file-download/' + asset.name
        context = SimpleNamespace(check_cancel=lambda: None, cancelled=SimpleNamespace(wait=lambda _: False))
        with self.permit('GET', path, None, None, 'download:' + asset.name):
            self.provider._files.download(restored, asset.name, context, expected=asset)
        self.downloads[asset.name] = restored.model_copy(deep=True)
        return restored


def selected_groups(rows, bindings):
    require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows))
    groups = {}
    for binding in bindings:
        scope = binding['scope']
        names = [row.get('groupName') for row in rows if row.get('groupGuid') == scope['group_id']]
        require(names and all(name == scope['group_name'] for name in names), 'markup_repair_group_changed')
        groups[scope['group_id']] = scope['group_name']
    return groups


def read_detail(wire, binding, cached, groups, diagnostics=None):
    identifier = binding['scope']['fixtureId']
    detail = wire.json('info', {'recordId': identifier, 'status': 0, 'version': ''},
                       params={'recordId': identifier, 'version': ''})
    require(isinstance(detail, dict) and detail.get('recordId') == identifier and detail.get('status') == 0
            and isinstance(detail.get('version'), str) and 0 < len(detail['version']) <= 1000
            and detail.get('groupGuid') == cached.source_folder_id and detail.get('rawTitle') == cached.title)
    rows = detail.get('attachments', [])
    if diagnostics is not None:
        diagnostics.append({'container': 'missing' if 'attachments' not in detail else
            'null' if rows is None else 'list' if isinstance(rows, list) else 'other',
            'count': len(rows) if isinstance(rows, list) else None, 'cached_count': len(cached.attachments)})
    if rows is None and not cached.attachments:
        rows = []  # Official empty attachment representation; never hide a missing expected image.
    require(isinstance(rows, list) and len(rows) == len(cached.attachments)
            and all(isinstance(row, dict) and row.get('type') == 0 for row in rows)
            and sorted((str(row.get('id')), row.get('url')) for row in rows) ==
                sorted((asset.id, '/' + asset.name) for asset in cached.attachments), 'markup_repair_attachment_changed')
    return detail, parse_entry(detail, cached.account_id, groups, deepcopy(cached.attachments))


def repair(jars, root, requests, *, authorization):
    bindings, cached_notes = audit(root, requests)
    resources = {}
    for note in cached_notes:
        for asset in note.attachments:
            identity = (asset.size, asset.sha256)
            require(asset.name not in resources or resources[asset.name] == identity, 'markup_repair_attachment_changed')
            resources[asset.name] = identity
    job = authorization_job(root, authorization, requests, bindings[0]['account'])
    require(all(not intent_path(root, binding).exists() for binding in bindings), 'markup_repair_intent_exists')
    directory = root / '.private/evidence' / job['id']
    directory.mkdir(exist_ok=False)  # Also consumes this authorization before the first account request.
    (directory / 'resources').mkdir()
    intents = root / '.private/evidence/oppo-markup-repair-intents'
    intents.mkdir(exist_ok=True)
    report = {'kind': 'oppo-fixture-markup-repair', 'operation': OPERATION, 'formal_acceptance': False,
              'status': 'started', 'authorization': authorization, 'fixture_scopes': requests,
              'account': bindings[0]['account'], 'cache_writes': 0, 'receipt_writes': 0,
              'original_sourcepost': 'historical_evidence_unchanged', 'items': [], 'started_at': datetime.now(timezone.utc).isoformat()}
    provider, counts, plans = None, {}, {}
    try:
        provider = create_provider('oppo', jars, directory / 'resources')
        wire = ScopedWire(provider, bindings, plans, counts)
        with wire.guard():
            wire.account()
            groups = selected_groups(wire.json('group-list-new'), bindings)
            for index, (binding, cached) in enumerate(zip(bindings, cached_notes, strict=True)):
                item = {'index': index, 'request': binding['request'], 'scope_sha256': digest(binding['scope']),
                        'receipt_key': binding['scope']['receipt_key'], 'target_id': cached.source_id,
                        'before_fingerprint': cached.fingerprint(), 'status': 'checking'}
                report['items'].append(item)
                diagnostics = item.setdefault('attachment_containers', [])
                detail, before = read_detail(wire, binding, cached, groups, diagnostics)
                require(canonical(before) == canonical(cached), 'markup_repair_cached_content_changed')
                markup, changes = transform(detail['rawText'])
                projected = parse_entry({**detail, 'rawText': markup}, cached.account_id, groups, deepcopy(cached.attachments))
                require(canonical(projected) == canonical(cached), 'markup_repair_semantics_changed')
                item.update(changes=changes, visual_degradations=visual_degradations(cached),
                            before_version_sha256=digest(detail['version']), before_raw_sha256=digest(detail['rawText']),
                            after_raw_sha256=digest(markup))
                if not any(changes.values()):
                    after_detail, after = read_detail(wire, binding, cached, groups, diagnostics)
                    require(after_detail == detail and canonical(after) == canonical(cached), 'markup_repair_readback_changed')
                    projection = directory / f'note-{index}.json'
                    exclusive(projection, after.model_dump(mode='json'))
                    item.update(status='readback_verified', mode='read_only', after_fingerprint=after.fingerprint(),
                                after_version_sha256=digest(after_detail['version']),
                                projection={'path': projection.name, 'sha256': digest(projection.read_bytes())})
                    continue
                # No saved server attachment metadata proves cache immutability: read each original once.
                for asset in cached.attachments:
                    restored = wire.download(asset)
                    require(restored.size == asset.size and restored.sha256 == asset.sha256)
                hypertext = {'category': 2, 'groupGuid': detail['groupGuid'], 'rawTitle': detail['rawTitle'],
                             'rawText': markup, 'attachments': [{k: v for k, v in row.items() if k != 'extra'}
                                                             for row in detail.get('attachments') or []]}
                for key in ('attachmentExtra', 'extra', 'speechLog'):
                    if key in detail:
                        hypertext[key] = deepcopy(detail[key])
                payload = {'recordId': cached.source_id, 'version': detail['version'],
                           'operatorType': 'modify', 'hypertext': hypertext}
                marker = intent_path(root, binding)
                exclusive(marker, {'kind': 'oppo-markup-repair-intent', 'operation': OPERATION,
                    'authorization': authorization, 'account': cached.account_id, 'target_id': cached.source_id,
                    'receipt_key': item['receipt_key'], 'before_fingerprint': cached.fingerprint(),
                    'before_version_sha256': item['before_version_sha256'], 'after_raw_sha256': item['after_raw_sha256'],
                    'state': 'write_may_have_been_sent', 'created_at': datetime.now(timezone.utc).isoformat()})
                item['intent'] = {'path': marker.relative_to(root).as_posix(), 'sha256': digest(marker.read_bytes())}
                item.update(status='needs_review', mode='edit_once')
                plans[cached.source_id] = payload
                result = wire.json('edit', payload, params={'recordId': cached.source_id, 'version': detail['version']}, write=True)
                require(isinstance(result, dict) and ('recordId' not in result or result['recordId'] == cached.source_id)
                        and isinstance(result.get('version'), str) and bool(result['version'])
                        and result['version'] != detail['version'], 'markup_repair_ack_unknown')
                after_detail, after = read_detail(wire, binding, cached, groups, diagnostics)
                require(after_detail['version'] == result['version'] and after_detail['rawText'] == markup
                        and {k: v for k, v in after_detail.items() if k not in ('version', 'rawText', 'updateTime')} ==
                            {k: v for k, v in detail.items() if k not in ('version', 'rawText', 'updateTime')}
                        and (cached.updated_at is None or after.updated_at is not None)
                        and canonical(after, True) == canonical(cached, True), 'markup_repair_readback_changed')
                projection = directory / f'note-{index}.json'
                exclusive(projection, after.model_dump(mode='json'))
                item.update(status='readback_verified', after_fingerprint=after.fingerprint(),
                            after_version_sha256=digest(after_detail['version']),
                            projection={'path': projection.name, 'sha256': digest(projection.read_bytes())})
            wire.account()
            require(selected_groups(wire.json('group-list-new'), bindings) == groups)
        require(audit(root, requests)[0] == bindings, 'markup_repair_local_binding_changed')
        for item in report['items']:
            if item['status'] == 'readback_verified':
                item['status'] = 'verified'
        report.update(status='verified', account_before_and_after=True)
    except Exception as error:
        report['status'] = 'needs_review' if any('intent' in item for item in report['items']) else 'blocked'
        for item in report['items']:
            if item['status'] in ('checking', 'readback_verified'):
                item['status'] = 'needs_review' if 'intent' in item else 'blocked'
        report.update(safe_error(error))
    finally:
        if provider:
            try:
                provider.close()
            except Exception:
                report['cleanup_code'] = 'provider_close_failed'
        report.update(finished_at=datetime.now(timezone.utc).isoformat(), physical_requests=len(counts),
                      edit_attempts=sum(key.startswith('edit:') for key in counts))
        exclusive(directory / 'proof.json', report)
    return {key: report[key] for key in ('status', 'operation', 'formal_acceptance', 'edit_attempts', 'physical_requests',
                                       'code', 'error_type') if key in report} | {
        'proof': {'path': (directory / 'proof.json').relative_to(root).as_posix(), 'sha256': digest((directory / 'proof.json').read_bytes())}}


def verify_repair(reference, root, account, scope):
    require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'}
            and re.fullmatch(PROOF_PATTERN, str(reference['path'])))
    path = confined(root, reference['path'])
    require(digest(path.read_bytes()) == reference['sha256'])
    proof = json.loads(path.read_text('utf-8'))
    require(proof.get('kind') == 'oppo-fixture-markup-repair' and proof.get('operation') == OPERATION
            and proof.get('status') == 'verified' and proof.get('formal_acceptance') is False
            and proof.get('account') == account and proof.get('account_before_and_after') is True
            and proof.get('cache_writes') == proof.get('receipt_writes') == 0)
    requests = proof['fixture_scopes']
    bindings, notes = audit(root, requests)
    job = authorization_job(root, proof['authorization'], requests, account)
    require(path.parent.name == job['id'])
    matches = [index for index, binding in enumerate(bindings) if binding['scope'] == scope]
    require(len(matches) == 1 and len(proof.get('items', [])) == len(bindings))
    index = matches[0]
    item, before = proof['items'][index], notes[index]
    require(item.get('index') == index and item.get('status') == 'verified' and item.get('request') == requests[index]
            and item.get('scope_sha256') == digest(scope) and item.get('receipt_key') == scope['receipt_key']
            and item.get('target_id') == scope['fixtureId'] and item.get('before_fingerprint') == before.fingerprint()
            and item.get('visual_degradations') == visual_degradations(before))
    require(isinstance(item.get('changes'), dict) and set(item['changes']) == {'em_tags', 'bare_hr_tags', 'mark_tags'}
            and all(type(value) is int and value >= 0 for value in item['changes'].values()))
    require(all(re.fullmatch(r'[0-9a-f]{64}', str(item.get(key))) for key in
                ('before_version_sha256', 'after_version_sha256', 'before_raw_sha256', 'after_raw_sha256')))
    marker = intent_path(root, bindings[index])
    if item.get('mode') == 'edit_once':
        require(any(item['changes'].values()) and item.get('after_version_sha256') != item.get('before_version_sha256'))
        require(item['intent'] == {'path': marker.relative_to(root).as_posix(), 'sha256': digest(marker.read_bytes())})
        intent = json.loads(marker.read_text('utf-8'))
        require(intent.get('operation') == OPERATION and intent.get('authorization') == proof['authorization']
                and intent.get('target_id') == before.source_id and intent.get('account') == account
                and intent.get('receipt_key') == scope['receipt_key'] and intent.get('before_fingerprint') == before.fingerprint()
                and intent.get('before_version_sha256') == item.get('before_version_sha256')
                and intent.get('after_raw_sha256') == item.get('after_raw_sha256'))
    else:
        require(item.get('mode') == 'read_only' and not any(item['changes'].values()) and 'intent' not in item
                and not marker.exists() and item.get('after_version_sha256') == item.get('before_version_sha256')
                and item.get('after_raw_sha256') == item.get('before_raw_sha256')
                and item.get('after_fingerprint') == item.get('before_fingerprint'))
    require(item['projection']['path'] == f'note-{index}.json')
    raw = path.with_name(item['projection']['path']).read_bytes()
    require(digest(raw) == item['projection']['sha256'])
    after = NoteDocument.model_validate_json(raw)
    require(after.fingerprint() == item['after_fingerprint'] and canonical(after, True) == canonical(before, True)
            and (before.updated_at is None or after.updated_at is not None))
    return {**scope, **native.render_expectations(after), 'target_fingerprint': after.fingerprint(),
            'repair_proof': reference, 'visual_degradations': item['visual_degradations'],
            'expected_version_sha256': item['after_version_sha256']}

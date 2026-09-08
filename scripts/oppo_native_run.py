"""Bounded lab-only OPPO renderer entry; callers retain the OPPO platform lock."""
import hashlib
import json
import os
import re
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from oppo_fixture_markup_repair import PROOF_PATTERN, verify_repair
from oppo_native_scope import MATRIX, NATIVE_CHECKS, OR1, browser_target, validate_report

from note_bridge.errors import BridgeError
from note_bridge.providers.factory import create_provider

STRUCTURES = ('headings', 'todos', 'lists', 'quotes', 'codes', 'tables', 'dividers')
STAGES = ('scope', 'probe', 'binding', 'browser', 'navigate', 'select_identity', 'editor_content')
CODES = ('scope', 'ambiguous_identity', 'identity_missing', 'Error', 'TimeoutError', 'TypeError',
         'account_rejected', 'initialization_timeout',
         'request_invalid', 'login_required', 'login_incomplete', 'session_expired', 'network_error',
         'rate_limited', 'platform_response', 'protocol_changed', 'account_changed')
REQUEST_CLASSES = ('info', 'group-list-new', 'group-note-count', 'list', 'handwriting_count',
                   'account', 'selected_image', 'selected_thumbnail')


def _count(value):
    return value if type(value) is int and 0 <= value <= 1000000 else None


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True).encode()).hexdigest()


def _repair_references(references, count):
    if references is None:
        return [None] * count
    if not isinstance(references, list) or len(references) != count:
        raise ValueError('scope')
    for reference in references:
        if reference is not None and not (isinstance(reference, dict) and set(reference) == {'path', 'sha256'}
                and isinstance(reference['path'], str) and re.fullmatch(PROOF_PATTERN, reference['path'])
                and isinstance(reference['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', reference['sha256'])):
            raise ValueError('scope')
    return deepcopy(references)


def _repaired_scope(original, reference, root, account):
    if reference is None:
        return original
    scope = verify_repair(reference, root, account, original)
    if (any(scope.get(key) != value for key, value in original.items() if key != 'target_fingerprint')
            or scope.get('repair_proof') != reference
            or not isinstance(scope.get('expected_version_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', scope['expected_version_sha256'])):
        raise ValueError('repair_projection')
    declaration = scope.get('visual_degradations')
    if (not isinstance(declaration, dict) or set(declaration) !=
            {'code_blocks_plaintext', 'inline_code_runs_plaintext', 'api_code_semantics_preserved'}
            or declaration['api_code_semantics_preserved'] is not True
            or any(type(declaration[key]) is not int or declaration[key] < 0
                   for key in ('code_blocks_plaintext', 'inline_code_runs_plaintext'))
            or declaration['code_blocks_plaintext'] != len(scope['codes'])
            or declaration['inline_code_runs_plaintext'] > sum(bool(run.get('code')) for run in scope['expectedRuns'])):
        raise ValueError('repair_degradation')
    scope = deepcopy(scope)
    scope['archival_code_expectations'] = {'codes': deepcopy(scope['codes']),
                                         'run_code_flags': [bool(run.get('code')) for run in scope['expectedRuns']]}
    if declaration['code_blocks_plaintext']:
        scope['codes'] = []
    if declaration['code_blocks_plaintext'] or declaration['inline_code_runs_plaintext']:
        for run in scope['expectedRuns']:
            if run.get('code'):
                run['code'] = False
    return scope


def _report(raw):
    """Only renderer booleans, counts and fixed vocabulary may reach disk/stdout."""
    safe = {key: raw.get(key) for key in ('kind', 'status', 'cloud_writes', 'formal_acceptance')}
    safe.update({key: _count(raw[key]) for key in ('total', 'checked', 'remaining') if key in raw})
    if 'stage' in raw:
        safe['stage'] = raw['stage'] if raw['stage'] in STAGES else 'browser'
    if 'code' in raw:
        safe['code'] = raw['code'] if raw['code'] in CODES else 'browser_error'
    if isinstance(raw.get('diagnostics'), dict):
        values = raw['diagnostics']
        diagnostic = {key: _count(values.get(key)) for key in
                      ('listContainers', 'listOwners', 'rows', 'visibleRows', 'editors', 'loadedRows',
                       'pageNo', 'pageSize', 'totalCount', 'pageErrors', 'failedRequests') if key in values}
        diagnostic.update({key: values[key] if type(values[key]) is bool else None for key in
                           ('appPresent', 'appContainerPresent', 'qiankunStarted', 'hasMore', 'loading', 'selectedInLoadedList') if key in values})
        for key, allowed in (('readyState', ('loading', 'interactive', 'complete')),
                             ('hostClass', ('cloud', 'api', 'account')),
                             ('pathClass', ('note_list', 'note_share', 'portal'))):
            if key in values:
                diagnostic[key] = values[key] if values[key] in allowed else 'other'
        if isinstance(values.get('requests'), dict):
            diagnostic['requests'] = {key: _count(values['requests'].get(key)) for key in REQUEST_CLASSES}
        if isinstance(values.get('bootstrap'), dict):
            diagnostic['bootstrap'] = {key: _count(values['bootstrap'].get(key)) for key in
                                       ('entryRequested', 'entrySucceeded', 'assetRequested', 'assetSucceeded')}
        if isinstance(values.get('accountResponses'), list):
            diagnostic['accountResponses'] = []
            for response in values['accountResponses'][:3]:
                if not isinstance(response, dict):
                    continue
                status = response.get('httpStatus')
                diagnostic['accountResponses'].append({
                    'httpStatus': status if type(status) is int and 100 <= status <= 599 else None,
                    **{key: response.get(key) if type(response.get(key)) is bool else None
                       for key in ('jsonParsed', 'codeZero', 'ssoIdPresent', 'authRejected')}})
        safe['diagnostics'] = diagnostic
    safe['items'] = []
    for item in raw['items']:
        row = {'index': _count(item.get('index')),
               'status': item.get('status') if item.get('status') in ('content_images_styles_verified', 'needs_review') else 'needs_review'}
        for field, keys in (('checks', NATIVE_CHECKS), ('structures', STRUCTURES)):
            values = item.get(field, {})
            row[field] = {key: values[key] if type(values[key]) is bool else None
                          for key in keys if key in values}
        row['diagnostics'] = {key: _count(item.get('diagnostics', {}).get(key)) for key in ('editors', 'owners')}
        diagnostic = item.get('diagnostics', {})
        for key in ('titleDetailMatched', 'titleParentMatched', 'titleDOMMatched',
                    'titleModelMatched', 'titleProjectionMatched', 'versionMatched'):
            if key in diagnostic:
                row['diagnostics'][key] = diagnostic[key] if type(diagnostic[key]) is bool else None
        for key, allowed in (('titleDOMTag', ('H1', 'P', 'DIV')), ('titleModelType', ('heading', 'paragraph'))):
            if key in diagnostic:
                row['diagnostics'][key] = diagnostic[key] if diagnostic[key] in allowed else 'other'
        if 'titleModelLevel' in diagnostic:
            level = diagnostic['titleModelLevel']
            row['diagnostics']['titleModelLevel'] = level if type(level) is int and 1 <= level <= 6 else None
        if 'styleTextAligned' in diagnostic:
            value = diagnostic['styleTextAligned']
            row['diagnostics']['styleTextAligned'] = value if type(value) is bool else None
        for key in ('styleExpectedCharacters', 'styleRenderedCharacters'):
            if key in diagnostic:
                row['diagnostics'][key] = _count(diagnostic[key])
        for key in ('styleExpectedCounts', 'styleMatchedCounts', 'styleChecks'):
            if isinstance(diagnostic.get(key), dict):
                row['diagnostics'][key] = {}
                for mark in ('bold', 'italic', 'underline', 'strike', 'highlight', 'code', 'link'):
                    if mark in diagnostic[key]:
                        value = diagnostic[key][mark]
                        row['diagnostics'][key][mark] = (
                            value if type(value) is bool else None) if key == 'styleChecks' else _count(value)
        row['bodyCharacters'] = _count(item.get('bodyCharacters'))
        row['images'] = [{'scopeIndex': image.get('scopeIndex') if type(image.get('scopeIndex')) is int and -1 <= image['scopeIndex'] <= 1 else None,
                         **{key: image.get(key) if type(image.get(key)) is bool else None
                            for key in ('identityMatched', 'decoded', 'visible', 'dimensionsMatched')}}
                         for image in item.get('images', [])[:2]]
        safe['items'].append(row)
    safe['blocked'] = []
    for blocked in raw.get('blocked', [])[:50]:
        entry = {
            'method': blocked.get('method') if blocked.get('method') in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS') else 'other',
            'pathClass': blocked.get('pathClass') if blocked.get('pathClass') in
                ('info', 'group-list-new', 'group-note-count', 'list', 'handwriting_count', 'account', 'selected_image', 'selected_thumbnail') else 'other',
            **{key: blocked.get(key) if type(blocked.get(key)) is bool else None
               for key in ('officialHost', 'budgetExceeded')}}
        if 'pathFingerprint' in blocked:
            fingerprint = blocked.get('pathFingerprint')
            entry['pathFingerprint'] = fingerprint if isinstance(fingerprint, str) and re.fullmatch(r'[0-9a-f]{64}', fingerprint) else None
            entry['hasQuery'] = blocked.get('hasQuery') if type(blocked.get('hasQuery')) is bool else None
            entry['hostClass'] = blocked.get('hostClass') if blocked.get('hostClass') in ('cloud', 'api', 'account', 'static_cdn', 'file_alb') else 'other'
            entry['resourceType'] = blocked.get('resourceType') if blocked.get('resourceType') in ('document', 'script', 'stylesheet', 'font', 'image', 'xhr', 'fetch') else 'other'
        safe['blocked'].append(entry)
    return safe


def run(jars, root, requests, repair_references=None):
    # No library print or exception text is a safe lab result, even before Node starts.
    with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
        return _run(jars, root, requests, repair_references)


def _run(jars, root, requests, repair_references=None):
    stage, selected, scopes, cookies = 'scope', [], [], []
    references, original_scope_hashes = [], []
    process_unknown = False
    report = {'kind': 'oppo-matrix-native-batch', 'status': 'blocked', 'cloud_writes': 0,
              'formal_acceptance': False, 'items': []}
    try:
        if not isinstance(requests, list) or not 1 <= len(requests) <= 2:
            raise ValueError('scope')
        for request in requests:
            if not isinstance(request, dict) or set(request) != {'manifest', 'index'}:
                raise ValueError('scope')
            name, index = request['manifest'], request['index']
            if not ((name == OR1 and index is None) or (isinstance(name, str) and re.fullmatch(MATRIX, name)
                    and type(index) is int and index in (0, 1))):
                raise ValueError('scope')
        if len({(r['manifest'], r['index']) for r in requests}) != len(requests):
            raise ValueError('scope')
        references = _repair_references(repair_references, len(requests))
        selected = [dict(request) for request in requests]
        stage = 'probe'
        provider = create_provider('oppo', jars, root / '.private/session-lab/resources')
        try:
            account = provider.probe()
            stage = 'binding'
            for request, reference in zip(selected, references, strict=True):
                original = browser_target(request, root, account)
                original_scope_hashes.append(_digest(original))
                scopes.append(_repaired_scope(original, reference, root, account))
        finally:
            provider.close()
        if len({scope['fixtureId'] for scope in scopes}) != len(scopes):
            raise ValueError('scope')
        cookies = [{'name': m.key, 'value': m.value, 'domain': m['domain'],
                    'path': m['path'] or '/', 'secure': bool(m['secure'])} for jar in jars for m in jar.values()]
        stage = 'browser'
        # No parent timeout/kill: Node owns bounded page operations and finally closes
        # its browser. An abnormal result poisons the caller's existing platform lock.
        process_unknown = True
        process = subprocess.Popen(['node', str(root / 'scripts/oppo-matrix-browser.cjs')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        stdout, _ = process.communicate(input=json.dumps({'scopes': scopes, 'cookies': cookies}), timeout=None)
        cookies.clear()
        if process.returncode != 0:
            raise ValueError('browser_exit')
        raw = json.loads(stdout)
        # Validate the original output before projection; a false success must never
        # become valid by deleting a contradictory or duplicated result field.
        verified = validate_report(raw, scopes)
        if verified and any('expected_version_sha256' in scope and item.get('diagnostics', {}).get('versionMatched') is not True
                            for item, scope in zip(raw['items'], scopes, strict=True)):
            raise ValueError('repair_version_unverified')
        report = _report(raw)
        if verified and not validate_report(report, scopes):
            raise ValueError('browser_result')
        for item, scope in zip(report['items'], scopes):
            if 'repair_proof' in scope:
                declaration = deepcopy(scope['visual_degradations'])
                item['visual_degradations'] = declaration
                item['archival_code_expectation_sha256'] = _digest(scope['archival_code_expectations'])
                item['declared_visual_differences'] = [description for key, description in (
                    ('code_blocks_plaintext', '代码块在官网显示为普通文字，原始代码标记及完整正文保留。'),
                    ('inline_code_runs_plaintext', '行内代码在官网显示为普通文字，原始代码标记及完整文字保留。')) if declaration[key]]
        process_unknown = False
    except Exception as error:
        code = error.code if isinstance(error, BridgeError) and error.code in CODES else 'oppo_native_' + stage + '_failed'
        report = {'kind': 'oppo-matrix-native-batch', 'status': 'needs_review' if process_unknown else 'blocked',
                  'cloud_writes': 0, 'formal_acceptance': False, 'items': [], 'stage': stage, 'code': code}
    finally:
        cookies.clear()
    report.update(fixture_scopes=selected,
        local_evidence_bindings=[{'scope': scope['scopeLabel'], 'target_fingerprint': scope['target_fingerprint'],
            'receipt_key': scope['receipt_key'], 'evidence_sha256': scope['evidence_sha256'],
            'scope_sha256': hashlib.sha256(json.dumps(scope, ensure_ascii=True, sort_keys=True).encode()).hexdigest()}
            for scope in scopes], created_at=datetime.now(timezone.utc).isoformat())
    if any(reference is not None for reference in references):
        report['repair_references'] = references
        for index, binding in enumerate(report['local_evidence_bindings']):
            binding['original_scope_sha256'] = original_scope_hashes[index]
            if references[index] is not None:
                binding['repair_proof'] = references[index]
                binding['expected_version_sha256'] = scopes[index]['expected_version_sha256']
    output = root / '.private/evidence' / ('oppo-target-native-' + uuid4().hex + '.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(report, ensure_ascii=True, indent=2).encode('utf-8')
    with output.open('xb') as stream:
        stream.write(encoded)
    reference = output.relative_to(root).as_posix()
    if process_unknown:
        raise subprocess.SubprocessError('oppo_native_process_review_required; evidence=' + reference) from None
    return {**report, 'evidence': reference, 'evidence_sha256': hashlib.sha256(encoded).hexdigest()}

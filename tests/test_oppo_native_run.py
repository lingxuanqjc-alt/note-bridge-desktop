"""A scoped renderer run must not leak sessions or convert partial checks to success."""
import importlib
import json
import subprocess
import sys
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
helper = importlib.import_module('oppo_native_run')
scope_helper = importlib.import_module('oppo_native_scope')


def request(index=0):
    return {'manifest': 'wps-oppo-BI1-job.json', 'index': index}


def scopes(index=0):
    return {'fixtureId': 'synthetic-' + str(index), 'scopeLabel': 'wps-oppo-BI1:' + str(index),
            'target_fingerprint': 'f' * 64, 'receipt_key': 'd' * 64,
            'evidence_sha256': {'.private/evidence/synthetic.json': 'e' * 64},
            'expectedText': 'synthetic-body-must-not-leak', 'image_specs': [{}, {}]}


def report(count=1):
    return {'kind': 'oppo-matrix-native-batch', 'status': 'verified', 'formal_acceptance': False,
            'cloud_writes': 0, 'total': count, 'checked': count, 'remaining': 0, 'blocked': [],
            'items': [{'index': index, 'status': 'content_images_styles_verified',
                       'checks': dict.fromkeys(scope_helper.NATIVE_CHECKS, True),
                       'structures': dict.fromkeys(helper.STRUCTURES, True),
                       'images': [{'scopeIndex': image, 'identityMatched': True, 'decoded': True,
                                   'visible': True, 'dimensionsMatched': True} for image in range(2)]}
                      for index in range(count)]}


def test_page_diagnostics_preserve_only_counts_flags_and_fixed_route_classes():
    raw = report()
    raw['diagnostics'] = {'readyState': 'complete', 'hostClass': 'cloud', 'pathClass': 'note_list',
                          'rows': 17, 'pageSize': 30, 'hasMore': False, 'selectedInLoadedList': True,
                          'appPresent': 'synthetic-secret', 'pageNo': True, 'totalCount': -1,
                          'body': 'synthetic-secret', 'url': 'https://private.invalid/synthetic-secret',
                          'requests': {'info': 1, 'list': 1, 'info:synthetic-secret': 999}}
    diagnostic = helper._report(raw)['diagnostics']
    assert diagnostic['rows'] == 17 and diagnostic['pageSize'] == 30
    assert diagnostic['hasMore'] is False and diagnostic['selectedInLoadedList'] is True
    assert diagnostic['appPresent'] is None and diagnostic['pageNo'] is None
    assert diagnostic['totalCount'] is None and diagnostic['requests']['info'] == 1
    assert 'synthetic-secret' not in json.dumps(diagnostic)
    raw['diagnostics'].update(readyState='synthetic-secret', hostClass='synthetic-secret', pathClass='synthetic-secret')
    diagnostic = helper._report(raw)['diagnostics']
    assert all(diagnostic[key] == 'other' for key in ('readyState', 'hostClass', 'pathClass'))


def test_blocked_request_fingerprint_cannot_output_path_query_host_or_credential():
    raw = report()
    raw['blocked'] = [{'method': 'GET', 'pathClass': 'other', 'officialHost': True, 'budgetExceeded': False,
                       'hostClass': 'api', 'resourceType': 'fetch', 'hasQuery': True,
                       'pathFingerprint': 'f' * 64, 'url': 'synthetic-secret', 'cookie': 'synthetic-secret'}]
    entry = helper._report(raw)['blocked'][0]
    assert entry['pathFingerprint'] == 'f' * 64 and entry['hostClass'] == 'api'
    assert entry['resourceType'] == 'fetch' and entry['hasQuery'] is True
    raw['blocked'][0]['hostClass'] = 'file_alb'
    assert helper._report(raw)['blocked'][0]['hostClass'] == 'file_alb'
    raw['blocked'][0].update(pathFingerprint='synthetic-secret', hostClass='synthetic-secret', resourceType='synthetic-secret')
    entry = helper._report(raw)['blocked'][0]
    assert entry['pathFingerprint'] is None and entry['hostClass'] == entry['resourceType'] == 'other'
    assert 'synthetic-secret' not in json.dumps(entry)


def test_initialization_diagnostics_cannot_serialize_account_payload_or_unknown_codes():
    raw = report()
    raw.update(status='blocked', stage='select_identity', code='initialization_timeout')
    raw['diagnostics'] = {
        'appContainerPresent': True, 'qiankunStarted': False,
        'bootstrap': {'entryRequested': 1, 'entrySucceeded': 0, 'assetRequested': False,
                      'assetSucceeded': 'synthetic-secret', 'url': 'synthetic-secret'},
        'accountResponses': [{'httpStatus': 200, 'jsonParsed': True, 'codeZero': False,
                              'ssoIdPresent': True, 'authRejected': False,
                              'code': 'synthetic-secret', 'payload': {'token': 'synthetic-secret'}},
                             {'httpStatus': True, 'jsonParsed': 'synthetic-secret'}, 'synthetic-secret']}
    safe = helper._report(raw)
    assert safe['code'] == 'initialization_timeout'
    diagnostic = safe['diagnostics']
    assert diagnostic['appContainerPresent'] is True and diagnostic['qiankunStarted'] is False
    assert diagnostic['bootstrap'] == {'entryRequested': 1, 'entrySucceeded': 0,
                                       'assetRequested': None, 'assetSucceeded': None}
    assert diagnostic['accountResponses'][0]['httpStatus'] == 200
    assert diagnostic['accountResponses'][0]['authRejected'] is False
    assert diagnostic['accountResponses'][1]['httpStatus'] is None
    assert diagnostic['accountResponses'][1]['jsonParsed'] is None
    assert 'synthetic-secret' not in json.dumps(safe)
    raw['code'] = 'account_rejected'
    assert helper._report(raw)['code'] == 'account_rejected'


def test_title_projection_diagnostics_keep_only_fixed_types_and_comparison_flags():
    raw = report()
    raw['items'][0]['diagnostics'] = {'titleDetailMatched': True, 'titleParentMatched': 'synthetic-secret',
        'titleDOMMatched': True, 'titleModelMatched': False, 'titleProjectionMatched': False,
        'titleDOMTag': 'P', 'titleModelType': 'paragraph', 'titleModelLevel': None,
        'titleText': 'synthetic-secret'}
    diagnostic = helper._report(raw)['items'][0]['diagnostics']
    assert diagnostic['titleDetailMatched'] is True and diagnostic['titleParentMatched'] is None
    assert diagnostic['titleDOMTag'] == 'P' and diagnostic['titleModelType'] == 'paragraph'
    assert diagnostic['titleModelLevel'] is None and 'synthetic-secret' not in json.dumps(diagnostic)
    raw['items'][0]['diagnostics'].update(titleDOMTag='synthetic-secret', titleModelType='synthetic-secret',
                                        titleModelLevel=True)
    diagnostic = helper._report(raw)['items'][0]['diagnostics']
    assert diagnostic['titleDOMTag'] == diagnostic['titleModelType'] == 'other'
    assert diagnostic['titleModelLevel'] is None and 'synthetic-secret' not in json.dumps(diagnostic)


def setup(monkeypatch, output=None, *, returncode=0, failure=None):
    calls = []
    jar = SimpleCookie()
    jar['synthetic-session'] = 'synthetic-cookie-must-not-leak'
    jar['synthetic-session']['domain'] = '.oppo.com'
    jar['synthetic-session']['path'] = '/'

    def probe():
        calls.append('probe')
        if failure:
            raise failure
        return 'a' * 64

    def bind(selected, root, account):
        assert account == 'a' * 64
        calls.append('bind')
        return scopes(selected['index'])

    def popen(args, **kwargs):
        calls.append('browser')
        assert 'synthetic-cookie-must-not-leak' not in repr(args) + repr(kwargs)
        assert args[0] == 'node' and args[1].endswith('oppo-matrix-browser.cjs')
        assert kwargs['stdin'] == kwargs['stdout'] == subprocess.PIPE
        assert kwargs['stderr'] == subprocess.DEVNULL
        assert kwargs['creationflags'] == (subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)

        def communicate(*, input, timeout):
            assert timeout is None, 'The parent must not kill a still-owned browser on a timeout.'
            payload = json.loads(input)
            assert payload['cookies'][0]['value'] == 'synthetic-cookie-must-not-leak'
            assert len(payload['scopes']) in (1, 2)
            # Even unexpected library output is suppressed by the callable boundary.
            print('synthetic-cookie-must-not-leak')
            return (json.dumps(output if output is not None else report(len(payload['scopes']))), None)

        return SimpleNamespace(returncode=returncode, communicate=communicate)

    monkeypatch.setattr(helper, 'create_provider', lambda platform, jars, resources:
        SimpleNamespace(probe=probe, close=lambda: calls.append('close')))
    monkeypatch.setattr(helper, 'browser_target', bind)
    monkeypatch.setattr(helper.subprocess, 'Popen', popen)
    return [jar], calls


def load(root, result):
    path = root / result['evidence']
    data = path.read_bytes()
    assert helper.hashlib.sha256(data).hexdigest() == result['evidence_sha256']
    return json.loads(data)


@pytest.mark.parametrize('count', [1, 2])
def test_exact_run_probes_once_reuses_one_browser_and_never_persists_session(count, tmp_path, monkeypatch, capsys):
    jars, calls = setup(monkeypatch)
    result = helper.run(jars, tmp_path, [request(index) for index in range(count)])
    proof = load(tmp_path, result)
    assert calls == ['probe'] + ['bind'] * count + ['close', 'browser']
    assert proof['status'] == 'verified' and proof['cloud_writes'] == 0 and proof['formal_acceptance'] is False
    assert len(proof['local_evidence_bindings']) == count
    assert all(len(item['scope_sha256']) == 64 for item in proof['local_evidence_bindings'])
    for secret in ('synthetic-cookie-must-not-leak', 'synthetic-body-must-not-leak'):
        assert secret not in json.dumps(result) + json.dumps(proof) + capsys.readouterr().out


@pytest.mark.parametrize('selected', [[], [request()] * 2, [request(0), request(1), request(0)],
                                    [{'manifest': '../../secret', 'index': 0}], [request(True)],
                                    [{**request(), 'cookies': 'must-not-be-saved'}]])
def test_invalid_intents_are_rejected_before_probe_or_browser(selected, tmp_path, monkeypatch):
    jars, calls = setup(monkeypatch)
    result = helper.run(jars, tmp_path, selected)
    assert result['status'] == 'blocked' and calls == [] and result['fixture_scopes'] == []
    assert 'must-not-be-saved' not in json.dumps(load(tmp_path, result))


def test_scope_binding_failure_closes_account_without_launching_browser(tmp_path, monkeypatch):
    jars, calls = setup(monkeypatch)
    monkeypatch.setattr(helper, 'browser_target', lambda *args: (_ for _ in ()).throw(ValueError('synthetic-private-id')))
    result = helper.run(jars, tmp_path, [request()])
    assert calls == ['probe', 'close'] and result['status'] == 'blocked'
    assert 'synthetic-private-id' not in json.dumps(load(tmp_path, result))


def test_expired_login_is_reported_once_without_raw_error_or_retry(tmp_path, monkeypatch):
    jars, calls = setup(monkeypatch, failure=BridgeError('session_expired', 'synthetic-private-response'))
    result = helper.run(jars, tmp_path, [request()])
    assert calls == ['probe', 'close'] and result['code'] == 'session_expired'
    assert 'synthetic-private-response' not in json.dumps(result)


def test_two_failed_runs_preserve_each_original_proof(tmp_path, monkeypatch):
    raw = report()
    raw.update(status='needs_review', unexpected='synthetic-cookie-must-not-leak')
    raw['items'][0]['checks']['renderedText'] = False
    jars, calls = setup(monkeypatch, raw)
    first = helper.run(jars, tmp_path, [request()])
    before = (tmp_path / first['evidence']).read_bytes()
    second = helper.run(jars, tmp_path, [request()])
    assert first['evidence'] != second['evidence'] and (tmp_path / first['evidence']).read_bytes() == before
    assert first['status'] == second['status'] == 'needs_review' and calls.count('browser') == 2
    assert load(tmp_path, first)['items'][0]['checks']['renderedText'] is False
    assert 'synthetic-cookie-must-not-leak' not in json.dumps(first)


@pytest.mark.parametrize('reason', ['nonzero', 'malformed', 'false_success'])
def test_unconfirmed_child_exit_or_result_keeps_platform_in_review(reason, tmp_path, monkeypatch):
    raw = report()
    if reason == 'false_success':
        raw['items'][0]['checks']['identity'] = False
    jars, calls = setup(monkeypatch, 'synthetic-private-stdout' if reason == 'malformed' else raw,
                        returncode=17 if reason == 'nonzero' else 0)
    with pytest.raises(subprocess.SubprocessError, match='oppo_native_process_review_required') as error:
        helper.run(jars, tmp_path, [request()])
    paths = list((tmp_path / '.private/evidence').glob('oppo-target-native-*.json'))
    assert len(paths) == 1 and json.loads(paths[0].read_text())['status'] == 'needs_review'
    assert 'synthetic-private-stdout' not in str(error.value) + paths[0].read_text()
    assert calls.count('browser') == 1

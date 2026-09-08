"""Only an exact verified repair can declare code rendering differences."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_oppo_native_run import helper, load, report, request, setup


def reference():
    return {'path': '.private/evidence/oppo-markup-repair-' + '1' * 32 + '/proof.json', 'sha256': 'b' * 64}


def original_scope(index):
    from test_oppo_native_run import scopes
    return {**scopes(index), 'expectedText': 'code inline ordinary', 'codes': ['code'],
            'expectedRuns': [{'text': 'code', 'code': True, 'italic': True},
                             {'text': ' inline', 'code': True, 'bold': True},
                             {'text': ' ordinary', 'underline': True}],
            'todos': [{'text': 'task', 'checked': True}], 'dividers': 1}


def verified(scope, ref):
    return {**deepcopy(scope), 'target_fingerprint': 'c' * 64, 'repair_proof': deepcopy(ref),
            'expected_version_sha256': 'e' * 64, 'visual_degradations': {
                'code_blocks_plaintext': 1, 'inline_code_runs_plaintext': 1, 'api_code_semantics_preserved': True}}


def test_mixed_batch_binds_original_then_repair_once_without_reprobe_and_reports_difference(tmp_path, monkeypatch):
    jars, calls = setup(monkeypatch)
    original = {index: original_scope(index) for index in (0, 1)}

    def bind(selected, root, account):
        calls.append('bind-' + str(selected['index']))
        return original[selected['index']]

    def verify(ref, root, account, scope):
        calls.append('verify')
        assert ref == reference() and account == 'a' * 64 and scope is original[1]
        return verified(scope, ref)

    monkeypatch.setattr(helper, 'browser_target', bind)
    monkeypatch.setattr(helper, 'verify_repair', verify)

    def popen(args, **kwargs):
        calls.append('browser')

        def communicate(*, input, timeout):
            scopes = json.loads(input)['scopes']
            assert scopes[0] == original[0], 'No reference must retain the original strict code assertions.'
            repaired = scopes[1]
            assert repaired['codes'] == [] and all(not run.get('code') for run in repaired['expectedRuns'])
            assert repaired['archival_code_expectations'] == {
                'codes': ['code'], 'run_code_flags': [True, True, False]}
            assert repaired['expectedText'] == original[1]['expectedText']
            assert repaired['expectedRuns'][0]['italic'] and repaired['expectedRuns'][1]['bold']
            assert repaired['expectedRuns'][2]['underline']
            assert repaired['todos'] == original[1]['todos'] and repaired['dividers'] == 1
            result = report(2)
            result['items'][1]['diagnostics'] = {'versionMatched': True}
            return json.dumps(result), None

        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr(helper.subprocess, 'Popen', popen)
    result = helper.run(jars, tmp_path, [request(0), request(1)], repair_references=[None, reference()])
    proof = load(tmp_path, result)
    assert calls == ['probe', 'bind-0', 'bind-1', 'verify', 'close', 'browser']
    assert proof['status'] == 'verified' and proof['repair_references'] == [None, reference()]
    assert 'visual_degradations' not in proof['items'][0]
    assert proof['items'][1]['visual_degradations']['code_blocks_plaintext'] == 1
    assert len(proof['items'][1]['declared_visual_differences']) == 2
    assert len(proof['items'][1]['archival_code_expectation_sha256']) == 64
    assert proof['local_evidence_bindings'][1]['repair_proof'] == reference()
    assert proof['local_evidence_bindings'][1]['expected_version_sha256'] == 'e' * 64
    assert original[1]['codes'] == ['code'] and original[1]['expectedRuns'][0]['code'] is True
    assert original[1]['expectedText'] not in json.dumps(proof)


@pytest.mark.parametrize('refs', [[], [None, None], [True], ['private-secret'],
    [{'path': '../../private-secret', 'sha256': 'a' * 64}], [{**reference(), 'cookies': 'private-secret'}],
    [{**reference(), 'sha256': 'private-secret'}]])
def test_unbound_repair_descriptors_stop_before_any_account_probe(refs, tmp_path, monkeypatch):
    jars, calls = setup(monkeypatch)
    result = helper.run(jars, tmp_path, [request()], repair_references=refs)
    assert result['status'] == 'blocked' and calls == []
    assert 'private-secret' not in json.dumps(result)


def test_a_success_label_cannot_omit_repaired_version_verification(tmp_path, monkeypatch):
    jars, calls = setup(monkeypatch)
    monkeypatch.setattr(helper, 'browser_target', lambda *args: original_scope(0))
    monkeypatch.setattr(helper, 'verify_repair', lambda ref, root, account, scope: verified(scope, ref))
    with pytest.raises(helper.subprocess.SubprocessError, match='oppo_native_process_review_required'):
        helper.run(jars, tmp_path, [request()], [reference()])
    assert calls.count('probe') == calls.count('browser') == 1


@pytest.mark.parametrize('change', ['verification_failed', 'wrong_projection', 'missing_version', 'wrong_count', 'unknown_degradation'])
def test_failed_or_inconsistent_repair_verification_never_opens_browser(change, tmp_path, monkeypatch):
    jars, calls = setup(monkeypatch)
    monkeypatch.setattr(helper, 'browser_target', lambda *args: original_scope(0))

    def verify(ref, root, account, scope):
        if change == 'verification_failed':
            raise ValueError('private-secret')
        value = verified(scope, ref)
        if change == 'wrong_projection':
            value['expectedText'] = 'private-secret'
        elif change == 'missing_version':
            value.pop('expected_version_sha256')
        elif change == 'wrong_count':
            value['visual_degradations']['code_blocks_plaintext'] = 9
        elif change == 'unknown_degradation':
            value['visual_degradations']['ignore_italic'] = True
        return value

    monkeypatch.setattr(helper, 'verify_repair', verify)
    result = helper.run(jars, tmp_path, [request()], [reference()])
    assert result['status'] == 'blocked' and calls == ['probe', 'close']
    assert 'private-secret' not in json.dumps(result)

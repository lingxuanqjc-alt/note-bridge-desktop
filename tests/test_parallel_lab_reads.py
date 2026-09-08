"""Parallel lab reads must preserve account isolation, active tasks and receipts."""
import io
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import parallel_read_dispatch as dispatch  # noqa: E402
from lab_platform_lock import (  # noqa: E402
    PlatformLocks,
    atomic_json,
    file_lock,
    foreground_scope,
    verify_inputs,
)
from lab_store import LabStore  # noqa: E402

from note_bridge.errors import BridgeError  # noqa: E402
from note_bridge.storage import Store  # noqa: E402
from note_bridge.tasks import TaskRunner  # noqa: E402


def job(root, operation='vivo_BD11_capture'):
    directory = root / '.private/parallel-read/jobs' / ('a' * 32)
    manifest = {'kind': 'parallel-lab-read', 'job_id': directory.name, 'platform': dispatch.OPERATIONS.get(operation, 'vivo'),
                'operation': operation, 'cloud_writes': 0, 'formal_acceptance': False, 'receipt_audit': {'fixed': True}}
    atomic_json(directory / 'manifest.json', manifest)
    dispatch.state(directory, 'starting')
    return directory


def run_worker(root, directory):
    output = io.StringIO()
    dispatch.worker(root, directory.name, io.StringIO(json.dumps({'cookies': [
        {'name': 'synthetic', 'value': 'RAM_ONLY_SECRET', 'domain': '.vivo.com.cn', 'path': '/', 'secure': True}]})), output)
    return output.getvalue(), json.loads((directory / 'state.json').read_text())


def test_worker_holds_platform_lock_before_ack_and_preserves_global_intents(tmp_path, monkeypatch):
    directory = job(tmp_path)
    globals_ = [tmp_path / '.private' / name for name in ('lab-discovery.json', 'lab-write-job.json', 'lab-matrix-job.json')]
    for path in globals_:
        path.write_text('opaque global intent', 'utf-8')
    original = Path.read_text

    def guarded_read(path, *args, **kwargs):
        assert path not in globals_, 'Background workers must not enter the foreground intent router'
        return original(path, *args, **kwargs)

    def execute(root, directory, manifest, jars, locks):
        with pytest.raises(BridgeError, match='实验任务'):
            with PlatformLocks(root, ['vivo']):
                pytest.fail('The child must already own its platform before executing any request')
        assert jars[0]['synthetic'].value == 'RAM_ONLY_SECRET'
        print('RAM_ONLY_SECRET')
        return {'status': 'verified', 'notes': 3, 'cloud_writes': 0}

    monkeypatch.setattr(Path, 'read_text', guarded_read)
    monkeypatch.setattr(dispatch, 'execute', execute)
    output, state = run_worker(tmp_path, directory)
    assert json.loads(output) == {'job_id': directory.name, 'status': 'locked'}
    assert state['status'] == 'completed'
    for path in tmp_path.rglob('*.json'):
        assert 'RAM_ONLY_SECRET' not in original(path), 'Credentials or incidental prints must never enter disk evidence'
    assert all(original(path) == 'opaque global intent' for path in globals_)


@pytest.mark.parametrize('operation', ['vivo_fetch', 'migrate', 'write', 'meizu_full_fetch'])
def test_unlisted_operation_cannot_reach_cloud(tmp_path, monkeypatch, operation):
    directory = job(tmp_path, operation)
    monkeypatch.setattr(dispatch, 'execute', lambda *args: pytest.fail('Unlisted operations must be blocked before cloud calls'))
    output, state = run_worker(tmp_path, directory)
    assert not output and state['status'] == 'blocked'


def test_read_job_is_consumed_once_even_after_success(tmp_path, monkeypatch):
    directory = job(tmp_path)
    calls = []
    monkeypatch.setattr(dispatch, 'execute', lambda *args: calls.append(True) or {'status': 'verified'})
    run_worker(tmp_path, directory)
    before = {path.name: path.read_bytes() for path in directory.glob('*.json')}
    output, _ = run_worker(tmp_path, directory)
    assert len(calls) == 1 and not output
    assert before == {path.name: path.read_bytes() for path in directory.glob('*.json')}


@pytest.mark.parametrize('before_start', [True, False])
def test_cancellation_keeps_inflight_result_truthful_and_never_force_kills(tmp_path, monkeypatch, before_start):
    directory = job(tmp_path)
    if before_start:
        dispatch.request_cancel(tmp_path, directory.name)

    def execute(*args):
        assert not before_start, 'A pre-dispatch cancellation must prevent all cloud reads'
        dispatch.request_cancel(tmp_path, directory.name)
        return {'status': 'verified'}

    monkeypatch.setattr(dispatch, 'execute', execute)
    _, state = run_worker(tmp_path, directory)
    assert state['status'] == ('cancelled' if before_start else 'completed')
    if not before_start:
        assert state['cancel_requested'] is True


def test_exception_text_is_never_saved_and_is_not_success(tmp_path, monkeypatch):
    directory = job(tmp_path)

    def fail(*args):
        raise RuntimeError('RAM_ONLY_SECRET')

    monkeypatch.setattr(dispatch, 'execute', fail)
    _, state = run_worker(tmp_path, directory)
    assert state['status'] == 'failed' and state['code'] == 'RuntimeError'
    assert not (directory / 'result.json').exists()
    assert all(b'RAM_ONLY_SECRET' not in path.read_bytes() for path in directory.glob('*.json'))


def test_foreground_migration_locks_source_and_target_and_detects_intent_changes(tmp_path):
    path = tmp_path / '.private/lab-matrix-job.json'
    atomic_json(path, {'armed': True, 'target': 'meizu', 'sources': [
        {'from_cloud_fixture': {'platform': 'wps'}}, {'from_cloud_fixture': {'platform': 'wps'}}]})
    scope, inputs = foreground_scope(tmp_path, 'meizu', 'probe')
    assert scope == {'wps', 'meizu'}
    with PlatformLocks(tmp_path, scope):
        verify_inputs(inputs)
        with PlatformLocks(tmp_path, ['honor']):
            pass  # Unrelated platforms remain available, including for foreground work.
        for platform in ('wps', 'meizu'):
            with pytest.raises(BridgeError):
                with PlatformLocks(tmp_path, [platform]):
                    pytest.fail('Neither source nor target can overlap another worker')
        atomic_json(path, {'armed': False})
        with pytest.raises(BridgeError, match='配置已变化'):
            verify_inputs(inputs)


def test_old_native_route_is_refused_during_background_wave(tmp_path):
    job(tmp_path)
    atomic_json(tmp_path / '.private/lab-discovery.json', {'platform': 'xiaomi', 'operation': 'xiaomi_matrix_browser'})
    with pytest.raises(BridgeError) as caught:
        foreground_scope(tmp_path, 'xiaomi', 'probe')
    assert caught.value.code == 'parallel_route_unsupported'
    assert foreground_scope(tmp_path, 'wps', 'fetch')[0] == {'wps'}


CHILD = '''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from lab_platform_lock import PlatformLocks
from lab_store import LabStore
from note_bridge.tasks import TaskRunner
root=Path(sys.argv[2]); mode=sys.argv[3]
with PlatformLocks(root, [sys.argv[4]]):
    if mode == 'crash':
        os._exit(0)
    store=LabStore(root / 'notes.sqlite')
    runner=TaskRunner(store)
    if mode == 'hold':
        store.save_receipt('f'*64, 'sending')
        task=runner.start('synthetic_read', lambda ctx: (ctx.update(stage='held'), sys.stdin.readline()))
        print(json.dumps({'task_id':task.id}), flush=True)
        runner.join()
    else:
        print(json.dumps({'receipt':store.receipt('f'*64)['status'],
                          'task_status':store.recent_tasks(1)[0].status}), flush=True)
'''


def child(tmp_path, mode, platform):
    script = tmp_path / 'synthetic-worker.py'
    script.write_text(CHILD, 'utf-8')
    return subprocess.Popen([sys.executable, str(script), str(Path(__file__).resolve().parents[1] / 'scripts'),
                             str(tmp_path), mode, platform], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding='utf-8',
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


def test_two_real_worker_processes_preserve_running_task_and_sending_receipt(tmp_path):
    first = child(tmp_path, 'hold', 'wps')
    try:
        assert json.loads(first.stdout.readline())['task_id']
        second = child(tmp_path, 'inspect', 'meizu')
        output, error = second.communicate(timeout=10)
        assert second.returncode == 0, error
        assert json.loads(output) == {'receipt': 'sending', 'task_status': 'running'}
        with pytest.raises(BridgeError):
            with PlatformLocks(tmp_path, ['wps']):
                pytest.fail('OS lock must work across actual Python processes')
    finally:
        first.communicate('\n', timeout=10)
    with PlatformLocks(tmp_path, ['wps']):
        pass
    assert Store(tmp_path / 'notes.sqlite').receipt('f' * 64)['status'] == 'sending'


def test_abrupt_exit_releases_os_lock_but_requires_process_tree_review(tmp_path):
    process = child(tmp_path, 'crash', 'meizu')
    process.communicate(timeout=10)
    assert process.returncode == 0
    with file_lock(tmp_path / '.private/parallel-read/locks/meizu.lock'):
        pass  # Automatic OS cleanup is real, but not evidence of browser-tree closure.
    with pytest.raises(BridgeError) as caught:
        with PlatformLocks(tmp_path, ['meizu']):
            pytest.fail('A stale active owner must not be automatically reused')
    assert caught.value.code == 'lab_process_review_required'


def test_lab_runner_leaves_historical_unknown_unresolved(tmp_path):
    store = LabStore(tmp_path / 'notes.sqlite')
    store.save_receipt('e' * 64, 'uncertain')
    TaskRunner(LabStore(tmp_path / 'notes.sqlite'))
    assert store.receipt('e' * 64)['status'] == 'uncertain'
    # Production recovery remains available on the original Store type.
    store.save_receipt('f' * 64, 'sending')
    TaskRunner(Store(tmp_path / 'notes.sqlite'))
    assert store.receipt('f' * 64)['status'] == 'uncertain'


def test_vivo_dispatch_uses_only_independent_control_file(tmp_path, monkeypatch):
    directory = job(tmp_path)
    manifest = json.loads((directory / 'manifest.json').read_text())
    sentinel = tmp_path / '.private/lab-discovery.json'
    sentinel.write_text('must not change', 'utf-8')
    calls = []

    def run(jars, root, request, *, control_path):
        assert control_path == directory / 'capture-control.json'
        assert request == {'platform': 'vivo', 'operation': 'vivo_BD11_capture', 'armed': True}
        calls.append(True)
        return {'status': 'verified', 'notes': 3}

    monkeypatch.setattr(dispatch, 'audit', lambda *args: {'fixed': True})
    monkeypatch.setattr(dispatch, 'module', lambda *args: SimpleNamespace(run=run))
    with PlatformLocks(tmp_path, ['vivo']) as locks:
        assert dispatch.execute(tmp_path, directory, manifest, [], locks)['notes'] == 3
    assert calls == [True] and sentinel.read_text() == 'must not change'


def test_forged_top_level_native_success_is_not_completed(tmp_path, monkeypatch):
    import meizu_matrix_scope

    monkeypatch.setattr(dispatch, 'create_provider', lambda *args: SimpleNamespace(probe=lambda: 'account',
                      account_id='account', close=lambda: None))
    monkeypatch.setattr(meizu_matrix_scope, 'browser_target', lambda *args: {})
    monkeypatch.setattr(dispatch.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0,
                      stdout=json.dumps({'kind': 'meizu-matrix-native-batch', 'cloud_writes': 0,
                                         'formal_acceptance': False, 'status': 'verified', 'total': 1, 'checked': 1})))
    with PlatformLocks(tmp_path, ['meizu']) as locks:
        with pytest.raises(BridgeError) as caught:
            dispatch._meizu(tmp_path, [], {'account_id': 'account'}, locks)
        assert caught.value.code == 'native_result_invalid' and locks.poisoned


def test_huawei_meizu_read_opens_only_its_two_confirmed_targets(tmp_path, monkeypatch):
    import meizu_matrix_scope

    directory = job(tmp_path, 'huawei_meizu_BD16_native')
    manifest = json.loads((directory / 'manifest.json').read_text())
    manifest['receipt_audit'] = {'account_id': 'account'}
    requested = []
    monkeypatch.setattr(dispatch, 'audit', lambda *args: manifest['receipt_audit'])
    monkeypatch.setattr(dispatch, 'create_provider', lambda *args: SimpleNamespace(
        probe=lambda: 'account', account_id='account', close=lambda: None))

    def scope(item, root, account):
        requested.append(item)
        assert root == tmp_path and account == 'account'
        return {}

    monkeypatch.setattr(meizu_matrix_scope, 'browser_target', scope)
    monkeypatch.setattr(dispatch.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0,
        stdout=json.dumps({'kind': 'meizu-matrix-native-batch', 'cloud_writes': 0,
                           'formal_acceptance': False, 'status': 'needs_review'})))
    with PlatformLocks(tmp_path, ['meizu']) as locks:
        report = dispatch.execute(tmp_path, directory, manifest, [], locks)
    expected = [{'manifest': dispatch.BD16, 'index': i} for i in (0, 1)]
    assert requested == report['fixture_scopes'] == expected
    assert report['status'] == 'needs_review'


@pytest.mark.parametrize('bound_manifest', [dispatch.BD12, dispatch.BD16])
def test_other_meizu_batch_cannot_substitute_for_huawei_native(tmp_path, monkeypatch, bound_manifest):
    directory = job(tmp_path, 'huawei_meizu_BD16_native')
    report = {'kind': 'meizu-matrix-native-batch', 'status': 'verified', 'formal_acceptance': False,
        'cloud_writes': 0, 'total': 2, 'checked': 2, 'remaining': 0,
        'items': [{'status': 'content_images_styles_verified'} for _ in range(2)],
        'fixture_scopes': [{'manifest': bound_manifest, 'index': i} for i in (0, 1)]}
    monkeypatch.setattr(dispatch, 'execute', lambda *args: report)
    _, state = run_worker(tmp_path, directory)
    assert state['status'] == ('completed' if bound_manifest == dispatch.BD16 else 'needs_review')


@pytest.mark.parametrize(('operation', 'result'), [
    ('wps_fetch', {'status': 'succeeded', 'scope_file': 'synthetic-scope.json'}),
    ('meizu_BD12_native', {'status': 'verified'}),
    ('vivo_BD11_capture', {'status': 'verified'}),
    ('huawei_scoped_source_capture', {'status': 'captured', 'scope_complete': True, 'whole_account_snapshot': False}),
])
def test_each_fixed_operation_has_its_own_complete_result(tmp_path, monkeypatch, operation, result):
    directory = job(tmp_path, operation)
    monkeypatch.setattr(dispatch, 'execute', lambda *args: result)
    _, state = run_worker(tmp_path, directory)
    assert state['status'] == 'completed'


@pytest.mark.parametrize(('operation', 'result'), [
    ('wps_fetch', {'status': 'succeeded'}),
    ('vivo_BD11_capture', {'status': 'captured', 'scope_complete': True, 'whole_account_snapshot': False}),
    ('huawei_scoped_source_capture', {'status': 'captured', 'scope_complete': False, 'whole_account_snapshot': False}),
    ('huawei_scoped_source_capture', {'status': 'verified'}),
])
def test_partial_or_other_operations_success_label_cannot_complete(tmp_path, monkeypatch, operation, result):
    directory = job(tmp_path, operation)
    monkeypatch.setattr(dispatch, 'execute', lambda *args: result)
    _, state = run_worker(tmp_path, directory)
    assert state['status'] == 'needs_review'


def test_parent_consumes_intent_once_and_only_pipes_credentials(tmp_path, monkeypatch):
    intent = tmp_path / '.private/lab-parallel-read.json'
    atomic_json(intent, {'kind': 'parallel-lab-read', 'platform': 'vivo', 'operation': 'vivo_BD11_capture', 'armed': True})
    calls = []

    class Pipe(io.StringIO):
        def close(self):
            pass

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs['stderr'] == subprocess.DEVNULL
        assert 'RAM_ONLY_SECRET' not in str(command)
        return SimpleNamespace(stdin=Pipe(), stdout=io.StringIO(json.dumps({'job_id': command[-1], 'status': 'locked'})))

    monkeypatch.setattr(dispatch, 'audit', lambda *args: {'fixed': True})
    monkeypatch.setattr(dispatch.subprocess, 'Popen', popen)
    jars = dispatch.cookie_jars([{'name': 'fixture', 'value': 'RAM_ONLY_SECRET', 'domain': '.vivo.com.cn', 'path': '/', 'secure': True}])
    result = dispatch.dispatch('vivo', jars, tmp_path)
    assert result['status'] == 'dispatched' and json.loads(intent.read_text())['armed'] is False
    assert dispatch.dispatch('vivo', jars, tmp_path) is None and len(calls) == 1
    assert all('RAM_ONLY_SECRET' not in path.read_text() for path in tmp_path.rglob('*.json'))


@pytest.mark.parametrize('armed,platform', [(False, 'vivo'), (None, 'vivo'), (1, 'vivo'), (True, 'wps')])
def test_unarmed_or_other_platform_intent_never_contends_with_an_ordinary_probe(tmp_path, monkeypatch, armed, platform):
    intent = tmp_path / '.private/lab-parallel-read.json'
    atomic_json(intent, {'kind': 'parallel-lab-read', 'platform': platform, 'operation': 'vivo_BD11_capture', 'armed': armed})
    before = intent.read_bytes()
    monkeypatch.setattr(dispatch, 'file_lock', lambda *a: pytest.fail('Unrelated probes must not acquire the global intent lock'))
    monkeypatch.setattr(dispatch, 'audit', lambda *a: pytest.fail('Unselected intents must not inspect receipts'))
    assert dispatch.dispatch('vivo', [], tmp_path) is None
    assert intent.read_bytes() == before


@pytest.mark.parametrize('change', [{'armed': False}, {'platform': 'wps'}])
def test_matching_intent_invalidated_before_lock_is_not_consumed_or_started(tmp_path, monkeypatch, change):
    path = tmp_path / '.private/lab-parallel-read.json'
    intent = {'kind': 'parallel-lab-read', 'platform': 'vivo', 'operation': 'vivo_BD11_capture', 'armed': True}
    atomic_json(path, intent)
    expected = {**intent, **change}

    @contextmanager
    def changed_under_lock(lock_path):
        with file_lock(lock_path):
            atomic_json(path, expected)
            yield

    monkeypatch.setattr(dispatch, 'file_lock', changed_under_lock)
    monkeypatch.setattr(dispatch, 'audit', lambda *a: pytest.fail('Unlocked eligibility is not permission to consume'))
    monkeypatch.setattr(dispatch.subprocess, 'Popen', lambda *a, **k: pytest.fail('Invalidated work must never start'))
    assert dispatch.dispatch('vivo', [], tmp_path) is None
    assert json.loads(path.read_text()) == expected


DISPATCH_CHILD = '''
import io, json, sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
import parallel_read_dispatch as dispatch
from note_bridge.errors import BridgeError
original=dispatch.file_lock
@contextmanager
def synchronized_lock(path):
    print('ready', flush=True)
    sys.stdin.readline()
    with original(path):
        yield
class Pipe(io.StringIO):
    def close(self):
        pass
def popen(command, **kwargs):
    return SimpleNamespace(stdin=Pipe(), stdout=io.StringIO(json.dumps({'job_id':command[-1], 'status':'locked'})))
dispatch.file_lock=synchronized_lock
dispatch.audit=lambda *a: {'fixed':True}
dispatch.subprocess.Popen=popen
try:
    result=dispatch.dispatch('vivo', [], Path(sys.argv[2]))
    print(json.dumps({'status':result['status'] if result else 'unselected'}), flush=True)
except BridgeError as error:
    print(json.dumps({'status':error.code}), flush=True)
'''


def test_two_processes_that_observe_armed_intent_can_only_consume_it_once(tmp_path):
    atomic_json(tmp_path / '.private/lab-parallel-read.json', {'kind': 'parallel-lab-read', 'platform': 'vivo',
        'operation': 'vivo_BD11_capture', 'armed': True})
    script = tmp_path / 'synthetic-dispatch-race.py'
    script.write_text(DISPATCH_CHILD, 'utf-8')
    processes = [subprocess.Popen([sys.executable, str(script), str(Path(__file__).resolve().parents[1] / 'scripts'),
        str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0) for _ in range(2)]
    # Both processes must already have observed the same armed intent before either locks it.
    assert [p.stdout.readline().strip() for p in processes] == ['ready', 'ready']
    for process in processes:
        process.stdin.write('\n')
        process.stdin.flush()
    results = [process.communicate(timeout=10) for process in processes]
    assert all(p.returncode == 0 for p in processes), results
    statuses = [json.loads(output)['status'] for output, _ in results]
    assert statuses.count('dispatched') == 1
    assert set(statuses) <= {'dispatched', 'unselected', 'lab_platform_busy'}
    assert len(list((tmp_path / '.private/parallel-read/jobs').glob('*/manifest.json'))) == 1
    assert json.loads((tmp_path / '.private/lab-parallel-read.json').read_text())['armed'] is False


def huawei_expected(identity='101'):
    marks = ('bold', 'italic', 'underline', 'strike', 'highlight')
    return {'fixture_id': identity, 'styles': {label: {mark: mark == selected for mark in marks}
        for label, selected in (('加粗', 'bold'), ('斜体', 'italic'), ('下划线', 'underline'))}}


def huawei_native_report(target, expected):
    samples = {label: [flags.copy()] for label, flags in expected['styles'].items()}
    if target == 'wps':
        return {'kind': 'wps-matrix-native', 'status': 'content_images_verified', 'formal_acceptance': False,
                'cloud_writes': 0, 'identity_matched': True, 'groupSelected': True, 'bodyTextMatches': True,
                'images': [{'decoded': True, 'visible': True, 'ratio': 640 / 240} for _ in range(2)], 'samples': samples}
    proof = {'matched': 1, 'candidates': [{key: True for key in ('currentTree', 'noteIdentityMatches',
        'loadedCurrentNote', 'widgetOwnsDom', 'widgetReady', 'dataReceived', 'visible', 'matched')}]}
    return {'kind': 'xiaomi-desktop-readonly-discovery', 'status': 'inspected', 'navigation': 'loaded',
            'scopeVersion': 2, 'requestedFixtureId': expected['fixture_id'], 'formal_acceptance': False, 'cloud_writes': 0,
            'fixtureOpened': True, 'locatorProofs': [{'matched': 1}], 'frames': [{'host': 'i.mi.com', 'path': '/note/h5',
                'fixtureTitleInEditor': True, 'editorProof': proof, 'editorProofAfter': proof,
                'decodedImages': [{'insideEditor': True, 'complete': True, 'visible': True, 'width': 640, 'height': 240} for _ in range(2)],
                'styleSamples': samples}]}


@pytest.mark.parametrize('target', ['xiaomi', 'wps'])
@pytest.mark.parametrize('failure', [None, 'wrong_identity', 'extra_image', 'lost_style', 'false_top_level'])
def test_huawei_native_checks_bind_actual_editor_images_and_retained_marks(target, failure):
    expected = huawei_expected()
    report = huawei_native_report(target, expected)
    if failure == 'wrong_identity':
        if target == 'xiaomi':
            report['requestedFixtureId'] = '202'
        else:
            report['identity_matched'] = False
    elif failure == 'extra_image':
        images = report['frames'][0]['decodedImages'] if target == 'xiaomi' else report['images']
        images.append({'width': 0, 'height': 0, 'decoded': False})
    elif failure == 'lost_style':
        samples = report['frames'][0]['styleSamples'] if target == 'xiaomi' else report['samples']
        samples['加粗'][0]['bold'] = False
    elif failure == 'false_top_level':
        report = {'status': 'verified', 'cloud_writes': 0, 'formal_acceptance': False}
    assert all(dispatch._huawei_native_checks(target, report, expected).values()) is (failure is None)


def test_huawei_native_retains_first_result_and_never_repeats_after_second_failure(tmp_path, monkeypatch):
    import xiaomi_browser_scope

    directory = job(tmp_path, 'huawei_xiaomi_BD13_native')
    binding = {'manifest': 'huawei-xiaomi-BD13-job.json', 'account_id': 'account',
               'items': [huawei_expected('101'), huawei_expected('102')]}
    monkeypatch.setattr(dispatch, 'create_provider', lambda *a: SimpleNamespace(probe=lambda: 'account', close=lambda: None))
    monkeypatch.setattr(xiaomi_browser_scope, 'browser_target', lambda scope, *a: {
        'fixtureId': str(101 + scope['index']), 'fixtureTitle': '笔记互迁验收 · fixed'})
    requests = []
    def run(command, **kwargs):
        request = json.loads(kwargs['input'])
        requests.append(request['fixtureId'])
        assert request['mode'] == 'desktop-scoped-fixture' and 'expectedUploadMetadata' not in request
        assert kwargs['stderr'] == subprocess.DEVNULL and 'RAM_ONLY_SECRET' not in str(command)
        report = huawei_native_report('xiaomi', binding['items'][len(requests) - 1])
        if len(requests) == 2:
            report['frames'][0]['editorProofAfter']['matched'] = 0
        return SimpleNamespace(returncode=0, stdout=json.dumps(report))
    monkeypatch.setattr(dispatch.subprocess, 'run', run)
    jars = dispatch.cookie_jars([{'name': 'fixture', 'value': 'RAM_ONLY_SECRET', 'domain': '.mi.com', 'path': '/', 'secure': True}])
    with PlatformLocks(tmp_path, ['xiaomi']) as locks:
        result = dispatch._huawei_native(tmp_path, directory, jars, binding, 'huawei_xiaomi_BD13_native', locks)
    assert requests == ['101', '102'] and result['status'] == 'needs_review'
    assert result['items'][0]['status'] == 'verified' and result['items'][1]['status'] == 'needs_review'
    assert (directory / 'native-0.json').exists() and (directory / 'native-1.json').exists()
    assert all('RAM_ONLY_SECRET' not in path.read_text() for path in directory.glob('*.json'))


@pytest.mark.parametrize('target,label', [('xiaomi', 'BD13'), ('wps', 'BD14')])
@pytest.mark.parametrize('failure', ['pending', 'wrong_batch', 'wrong_seed'])
def test_huawei_native_audit_rejects_unfinished_or_unreviewed_scope_before_source_read(tmp_path, monkeypatch, target, label, failure):
    import huawei_scoped_source

    reference = {'path': 'fixed-proof', 'sha256': 'a' * 64}
    origins = [{'platform': 'huawei', 'manifest': name, 'scoped_proof': reference} for name in huawei_scoped_source.MANIFESTS]
    identifier = f'matrix-20260907-huawei-{target}-{label}'
    job_data = {'kind': 'cloud-matrix-batch', 'id': identifier, 'target': target, 'armed': False,
                'source_policy': 'direct_seed_only', 'sources': [{'from_cloud_fixture': o} for o in origins]}
    evidence = {'batch_id': identifier, 'source': 'huawei', 'target': target, 'status': 'api_verified', 'formal_acceptance': False,
                'original_target_notes_unchanged': True, 'only_confirmed_additions': True,
                'source_capture': reference, 'whole_source_account_snapshot': False, 'items': [{}, {}]}
    if failure == 'pending':
        evidence['status'] = 'running'
    elif failure == 'wrong_batch':
        job_data['id'] = 'matrix-20260907-other'
    else:
        origins[1]['manifest'] = 'unreviewed-source-job.json'
    atomic_json(tmp_path / f'.private/checkpoints/huawei-{target}-{label}-job.json', job_data)
    atomic_json(tmp_path / f'.private/evidence/{identifier}.json', evidence)
    monkeypatch.setattr(huawei_scoped_source, 'verify_source', lambda *a: pytest.fail('Scope must reject before any captured source is opened'))
    with pytest.raises(BridgeError):
        dispatch._audit_huawei_native(tmp_path, f'huawei_{target}_{label}_native', object())


@pytest.mark.parametrize('missing', ['batch', 'count', 'scope', 'writes'])
def test_huawei_source_post_needs_all_five_verified_scopes_not_a_success_label(tmp_path, monkeypatch, missing):
    directory = job(tmp_path, 'huawei_BD13_BD17_source_post')
    result = {'kind': 'huawei_BD13_BD17_source_post', 'status': 'verified', 'scope_complete': True,
              'whole_account_snapshot': False, 'covered_batches': 5, 'source_notes': 2, 'source_images': 4,
              'cloud_writes': 0, 'cache_writes': 0, 'receipt_writes': 0}
    if missing == 'batch':
        result['covered_batches'] = 4
    elif missing == 'count':
        result['source_images'] = 3
    elif missing == 'scope':
        result['scope_complete'] = False
    else:
        result['receipt_writes'] = 1
    monkeypatch.setattr(dispatch, 'execute', lambda *a: result)
    assert run_worker(tmp_path, directory)[1]['status'] == 'needs_review'

"""Only an explicit one-use authorization can reach the OPPO repair writer."""
import ast
import hashlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.models import PlatformId
from note_bridge.paths import AppPaths

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import oppo_markup_repair_dispatch as dispatch  # noqa: E402
from lab_platform_lock import (  # noqa: E402
    MARKUP_REPAIR_CONFLICTS,
    PlatformLocks,
    atomic_json,
    foreground_scope,
    verify_inputs,
)


def job(root, **changes):
    value = {'kind': dispatch.KIND, 'id': 'oppo-markup-repair-' + 'a' * 32,
        'operation': dispatch.OPERATION, 'target': 'oppo', 'expected_account': 'b' * 64,
        'fixture_scopes': [{'manifest': 'oppo-complex-OR1-job.json', 'index': None}], 'armed': True, **changes}
    path = root / '.private' / dispatch.MARKUP_REPAIR_INTENT
    atomic_json(path, value)
    return path, value


def entry(root, jars=()):
    scope, inputs = foreground_scope(root, 'oppo', 'probe')
    with PlatformLocks(root, scope):
        verify_inputs(inputs)
        return dispatch.run_armed_job('oppo', jars, root, inputs=inputs)


def fake_helper(monkeypatch, root, original):
    calls = []

    def audit(_root, requests):
        calls.append('audit')
        assert _root == root and requests == original['fixture_scopes']
        return [{'account': original['expected_account']} for _ in requests], []

    def repair(jars, _root, requests, *, authorization):
        calls.append('repair')
        raw = (root / authorization['path']).read_bytes()
        consumed = json.loads(raw)
        assert hashlib.sha256(raw).hexdigest() == authorization['sha256']
        assert set(consumed) == dispatch.FIELDS | {'consumed_at'} and consumed['armed'] is False
        assert requests == consumed['fixture_scopes']
        assert json.loads((root / '.private' / dispatch.MARKUP_REPAIR_INTENT).read_bytes()) == consumed
        return {'kind': dispatch.KIND, 'status': 'verified', 'cloud_writes': 1}

    helper = SimpleNamespace(audit=audit, repair=repair)
    monkeypatch.setitem(sys.modules, 'oppo_fixture_markup_repair', helper)
    return helper, calls


def test_consumption_and_archive_exist_before_writer_and_cannot_replay(tmp_path, monkeypatch):
    path, original = job(tmp_path)
    _, calls = fake_helper(monkeypatch, tmp_path, original)
    assert entry(tmp_path, ['synthetic-cookie-never-persist'])['status'] == 'verified'
    archive = tmp_path / '.private/checkpoints' / (original['id'] + '-consumed-job.json')
    archived = archive.read_bytes()
    assert entry(tmp_path) is None and calls == ['audit', 'repair']
    atomic_json(path, original)  # Reusing an already consumed id must still never call the writer.
    with pytest.raises(BridgeError) as caught:
        entry(tmp_path)
    assert caught.value.code == 'markup_repair_already_consumed'
    assert json.loads(path.read_bytes())['armed'] is False and archive.read_bytes() == archived
    assert calls == ['audit', 'repair'] and b'synthetic-cookie' not in archived


@pytest.mark.parametrize('platform,armed', [('wps', True), ('oppo', False)])
def test_wrong_platform_or_unarmed_request_does_not_consume_or_lock(tmp_path, monkeypatch, platform, armed):
    path, _ = job(tmp_path, armed=armed)
    before = path.read_bytes()
    monkeypatch.setattr(dispatch, 'file_lock', lambda *_: pytest.fail('Unselected repair must not acquire the write lock'))
    assert dispatch.run_armed_job(platform, [], tmp_path, inputs={}) is None
    assert path.read_bytes() == before and not (tmp_path / '.private/checkpoints').exists()


@pytest.mark.parametrize('other', MARKUP_REPAIR_CONFLICTS)
def test_other_armed_write_recovery_or_parallel_intent_blocks_before_consuming(tmp_path, other):
    path, _ = job(tmp_path)
    before = path.read_bytes()
    atomic_json(tmp_path / '.private' / other, {'armed': True, 'target': 'wps', 'platform': 'wps'})
    with pytest.raises(BridgeError) as caught:
        foreground_scope(tmp_path, 'oppo', 'probe')
    assert caught.value.code == 'lab_repair_conflict' and path.read_bytes() == before


def test_discovery_operation_is_never_an_implicit_repair_authorization(tmp_path):
    path, _ = job(tmp_path)
    before = path.read_bytes()
    atomic_json(tmp_path / '.private/lab-discovery.json', {'platform': 'oppo', 'operation': 'oppo_native_matrix'})
    with pytest.raises(BridgeError) as caught:
        foreground_scope(tmp_path, 'oppo', 'probe')
    assert caught.value.code == 'lab_repair_conflict' and path.read_bytes() == before


@pytest.mark.parametrize('change', ['own_before_lock', 'disabled_before_lock', 'conflict_before_lock', 'during_audit', 'wrong_account'])
def test_config_changes_and_wrong_account_never_reach_repair(tmp_path, monkeypatch, change):
    path, original = job(tmp_path)
    service, calls = fake_helper(monkeypatch, tmp_path, original)
    scope, inputs = foreground_scope(tmp_path, 'oppo', 'probe')
    if change == 'own_before_lock':
        atomic_json(path, {**original, 'fixture_scopes': [{'manifest': 'wps-oppo-BI1-job.json', 'index': 0}]})
    elif change == 'disabled_before_lock':
        atomic_json(path, {**original, 'armed': False})
    elif change == 'conflict_before_lock':
        atomic_json(tmp_path / '.private/lab-write-job.json', {'armed': True, 'target': 'wps'})
    else:
        def audit(*_):
            calls.append('audit')
            if change == 'during_audit':
                atomic_json(tmp_path / '.private/lab-parallel-read.json', {'armed': True})
            return [{'account': 'c' * 64 if change == 'wrong_account' else original['expected_account']}], []
        service.audit = audit
    with PlatformLocks(tmp_path, scope), pytest.raises(BridgeError):
        dispatch.run_armed_job('oppo', [], tmp_path, inputs=inputs)
    assert 'repair' not in calls
    if change in ('during_audit', 'wrong_account'):
        assert json.loads(path.read_bytes())['armed'] is False


@pytest.mark.parametrize('changes', [{'operation': 'em_to_i'}, {'id': '../escape'},
    {'fixture_scopes': [{'manifest': 'vivo-private-job.json', 'index': 0}]},
    {'fixture_scopes': [{'manifest': 'wps-oppo-BI1-job.json', 'index': True}]},
    {'fixture_scopes': [{'manifest': 'oppo-complex-OR1-job.json', 'index': None}] * 2}])
def test_scope_cannot_expand_through_an_editable_intent(tmp_path, changes):
    job(tmp_path, **changes)
    with pytest.raises(BridgeError):
        entry(tmp_path)
    assert not (tmp_path / '.private/checkpoints').exists()


def test_probe_routes_explicit_repair_before_any_background_or_discovery(tmp_path, monkeypatch):
    _, original = job(tmp_path)
    _, calls = fake_helper(monkeypatch, tmp_path, original)
    source = ast.parse((ROOT / 'scripts/probe-session.py').read_text('utf8'))
    selected = ast.Module(body=[node for node in source.body if isinstance(node, ast.FunctionDef)], type_ignores=[])
    from http.cookies import SimpleCookie

    def forbidden(*_args, **_kwargs):
        pytest.fail('Explicit repair must not dispatch background work, discovery, or ordinary provider calls')

    namespace = {'__file__': str(tmp_path / 'scripts/probe-session.py'), 'Path': Path,
        'json': json, 'sys': SimpleNamespace(stdin=io.StringIO(json.dumps({'platform': 'oppo', 'action': 'probe', 'cookies': []}))),
        'PlatformId': PlatformId, 'BridgeError': BridgeError, 'SimpleCookie': SimpleCookie, 'AppPaths': AppPaths,
        'foreground_scope': foreground_scope, 'PlatformLocks': PlatformLocks, 'verify_inputs': verify_inputs,
        'markup_repair_selected': dispatch.selected, 'run_markup_repair': dispatch.run_armed_job,
        'dispatch': forbidden, 'discover': forbidden, 'create_provider': forbidden}
    exec(compile(selected, 'probe-session.py', 'exec'), namespace)
    assert namespace['main']({})['status'] == 'verified' and calls == ['audit', 'repair']


CHILD = r'''
import json,sys,time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[2])
import oppo_markup_repair_dispatch as dispatch
from lab_platform_lock import PlatformLocks,foreground_scope
from note_bridge.errors import BridgeError
root=Path(sys.argv[1])
def audit(*args):return [{'account':'b'*64}],[]
def repair(*args,**kwargs):
 with (root/'only-one-repair.json').open('x') as out:out.write('{}')
 time.sleep(.15)
 return {'status':'verified'}
sys.modules['oppo_fixture_markup_repair']=SimpleNamespace(audit=audit,repair=repair)
(root/('ready-'+sys.argv[3])).touch()
until=time.monotonic()+8
while not (root/'go').exists() and time.monotonic()<until:time.sleep(.01)
try:
 scope,inputs=foreground_scope(root,'oppo','probe')
 with PlatformLocks(root,scope):
  result=dispatch.run_armed_job('oppo',[],root,inputs=inputs)
 print(json.dumps(result))
except BridgeError as error:print(json.dumps({'status':'blocked','code':error.code}))
'''


def test_two_processes_can_consume_only_one_authorization(tmp_path):
    path, _ = job(tmp_path)
    workers = [subprocess.Popen([sys.executable, '-c', CHILD, str(tmp_path), str(ROOT / 'scripts'), str(i)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(2)]
    until = time.monotonic() + 10
    while not all((tmp_path / ('ready-' + str(i))).exists() for i in range(2)) and time.monotonic() < until:
        time.sleep(.01)
    (tmp_path / 'go').touch()
    results = []
    for worker in workers:
        output, error = worker.communicate(timeout=12)
        assert worker.returncode == 0, error
        results.append(json.loads(output))
    assert sum(bool(result and result.get('status') == 'verified') for result in results) == 1
    assert (tmp_path / 'only-one-repair.json').is_file() and json.loads(path.read_bytes())['armed'] is False
    assert len(list((tmp_path / '.private/checkpoints').glob('*-consumed-job.json'))) == 1

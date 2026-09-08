"""Explicit one-use lab write routing. Never called by read-only discovery."""
import hashlib
import json
import re
from datetime import datetime, timezone

from lab_platform_lock import (
    MARKUP_REPAIR_CONFLICTS,
    MARKUP_REPAIR_INTENT,
    atomic_json,
    file_lock,
    verify_inputs,
    verify_markup_repair_conflicts,
)

from note_bridge.errors import BridgeError

KIND = 'oppo-fixture-markup-repair'
OPERATION = 'oppo_web_markup_v1'
FIELDS = {'kind', 'operation', 'target', 'expected_account', 'fixture_scopes', 'armed', 'id'}
SCOPES = {'oppo-complex-OR1-job.json': (None,), 'wps-oppo-BI1-job.json': (0, 1),
    'huawei-oppo-BI3-job.json': (0, 1), 'xiaomi-oppo-BI5-job.json': (0, 1),
    'honor-oppo-BI7-job.json': (0, 1), 'meizu-oppo-BI9-job.json': (0, 1),
    'vivo-oppo-BI11-job.json': (0, 1)}


def require(condition, code='invalid_markup_repair_job'):
    if not condition:
        raise BridgeError(code, 'OPPO 限定修复的授权、账号或调度范围未确认，未继续执行。')


def selected(platform, root):
    path = root / '.private' / MARKUP_REPAIR_INTENT
    if not path.exists():
        return False
    try:
        job = json.loads(path.read_bytes())
        require(isinstance(job, dict))
    except (ValueError, TypeError):
        raise BridgeError('invalid_markup_repair_job', 'OPPO 限定修复指令无法识别。') from None
    return job.get('armed') is True and job.get('target') == platform


def validate_job(job):
    require(isinstance(job, dict) and set(job) == FIELDS and job['kind'] == KIND
            and job['operation'] == OPERATION and job['target'] == 'oppo' and job['armed'] is True
            and isinstance(job['id'], str) and re.fullmatch(r'oppo-markup-repair-[0-9a-f]{32}', job['id'])
            and isinstance(job['expected_account'], str) and re.fullmatch(r'[0-9a-f]{64}', job['expected_account']))
    requests = job['fixture_scopes']
    require(isinstance(requests, list) and 1 <= len(requests) <= 2)
    seen = set()
    for request in requests:
        require(isinstance(request, dict) and set(request) == {'manifest', 'index'})
        name, index = request['manifest'], request['index']
        require(isinstance(name, str) and name in SCOPES and (index is None or type(index) is int)
                and index in SCOPES[name] and (name, index) not in seen)
        seen.add((name, index))


def run_armed_job(platform, jars, root, *, inputs):
    """Caller holds the foreground OPPO platform lock and supplies bound inputs."""
    path = root / '.private' / MARKUP_REPAIR_INTENT
    if path in inputs:
        verify_inputs(inputs)  # A changed/disabled job must not fall through to another router.
    if not selected(platform, root):
        return None
    require(platform == 'oppo' and path in inputs
            and all(root / '.private' / name in inputs for name in MARKUP_REPAIR_CONFLICTS))
    with file_lock(root / '.private/oppo-markup-repair-intent.lock'):
        verify_inputs(inputs)
        job = json.loads(inputs[path])
        validate_job(job)
        verify_markup_repair_conflicts(inputs)
        consumed = {**job, 'armed': False, 'consumed_at': datetime.now(timezone.utc).isoformat()}
        # Consume before any probe. Archive/qualification failure cannot leave a
        # matched write intent available for another ordinary account check.
        atomic_json(path, consumed)
        encoded = path.read_bytes()
        require(json.dumps(json.loads(encoded), sort_keys=True) == json.dumps(consumed, sort_keys=True), 'lab_intent_changed')
        archive = root / '.private/checkpoints' / (job['id'] + '-consumed-job.json')
        archive.parent.mkdir(parents=True, exist_ok=True)
        try:
            with archive.open('xb') as stream:
                stream.write(encoded)
                stream.flush()
        except FileExistsError:
            raise BridgeError('markup_repair_already_consumed', '此修复授权已消费，不能重复执行。') from None
        descriptor = {'path': archive.relative_to(root).as_posix(), 'sha256': hashlib.sha256(encoded).hexdigest()}
        bound = {**inputs, path: encoded}
        verify_inputs(bound)
        from oppo_fixture_markup_repair import audit, repair

        bindings, _ = audit(root, job['fixture_scopes'])
        require(len(bindings) == len(job['fixture_scopes'])
                and all(binding['account'] == job['expected_account'] for binding in bindings), 'account_changed')
        verify_inputs(bound)
        return repair(jars, root, job['fixture_scopes'], authorization=descriptor)

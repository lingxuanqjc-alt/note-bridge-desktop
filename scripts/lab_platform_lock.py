"""Process-owned platform locks for the private lab, never the desktop API."""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from note_bridge.errors import BridgeError

PLATFORMS = frozenset(('xiaomi', 'oppo', 'vivo', 'huawei', 'honor', 'meizu', 'wps'))
MARKUP_REPAIR_INTENT = 'lab-oppo-markup-repair.json'
MARKUP_REPAIR_CONFLICTS = ('lab-matrix-job.json', 'lab-write-job.json', 'lab-parallel-read.json',
    'lab-matrix-recovery.json', 'lab-repair-job.json', 'lab-xiaomi-native-repair.json',
    'lab-xiaomi-complex-repair.json', 'lab-discovery.json')


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2), 'utf-8')
    temporary.replace(path)


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise BridgeError('lab_platform_busy', '该平台仍有实验任务正在执行。') from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class PlatformLocks:
    def __init__(self, root: Path, platforms, owner=None):
        if not platforms or not set(platforms) <= PLATFORMS:
            raise BridgeError('invalid_lab_scope', '实验任务的平台范围未确认。')
        self.folder = root / '.private/parallel-read/locks'
        self.platforms = sorted(set(platforms))
        self.owner = owner or uuid.uuid4().hex
        self.stack = ExitStack()
        self.paths = []
        self.poisoned = False

    def __enter__(self):
        try:
            # Obtain the entire set before creating ownership records.
            for platform in self.platforms:
                self.stack.enter_context(file_lock(self.folder / (platform + '.lock')))
                path = self.folder / (platform + '-owner.json')
                if path.exists() and json.loads(path.read_text('utf-8')).get('state') != 'released':
                    raise BridgeError('lab_process_review_required', '上次实验进程未确认关闭，需要核对进程树后才能继续该平台。')
            for platform in self.platforms:
                path = self.folder / (platform + '-owner.json')
                atomic_json(path, {'owner': self.owner, 'pid': os.getpid(), 'platform': platform, 'state': 'active'})
                self.paths.append(path)
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, kind, value, traceback):
        try:
            # A killed Python owner leaves "active". Never assume its browser
            # descendants died merely because the OS file lock became available.
            unknown = self.poisoned or (kind is not None and issubclass(kind, (KeyboardInterrupt, SystemExit, subprocess.SubprocessError)))
            for path in self.paths:
                record = json.loads(path.read_text('utf-8'))
                record['state'] = 'needs_review' if unknown else 'released'
                atomic_json(path, record)
        finally:
            self.stack.close()


def foreground_scope(root, platform, action):
    """Bind routing inputs before acquiring source+target locks; no cloud access."""
    scope, inputs = {str(platform)}, {}
    selected_write, discovery, markup_repair = False, None, False
    if action != 'probe':
        return scope, inputs
    for name in (MARKUP_REPAIR_INTENT, *MARKUP_REPAIR_CONFLICTS):
        path = root / '.private' / name
        raw = path.read_bytes() if path.exists() else None
        inputs[path] = raw
        if raw is None:
            continue
        job = json.loads(raw)
        if name == MARKUP_REPAIR_INTENT:
            if job.get('armed') is True and job.get('target') == platform:
                if platform != 'oppo':
                    raise BridgeError('invalid_lab_scope', '此修复入口仅允许 OPPO。')
                markup_repair = selected_write = True
            continue
        if name == 'lab-discovery.json':
            discovery = job
            continue  # Discovery performs cloud calls only on its selected platform.
        if name not in ('lab-matrix-job.json', 'lab-write-job.json'):
            continue  # Bound conflict inputs are inspected only for the explicit repair.
        if job.get('armed') is not True or job.get('target') != platform:
            continue
        selected_write = True
        entries = job.get('sources', []) if name == 'lab-matrix-job.json' else [job]
        if not isinstance(entries, list):
            raise BridgeError('invalid_lab_scope', '实验任务来源范围未确认。')
        for entry in entries:
            origin = entry.get('from_cloud_fixture')
            if origin is not None:
                if not isinstance(origin, dict) or origin.get('platform') not in PLATFORMS:
                    raise BridgeError('invalid_lab_scope', '实验任务来源范围未确认。')
                scope.add(origin['platform'])
    if markup_repair:
        verify_markup_repair_conflicts(inputs)
        scope = {'oppo'}
    if (not selected_write and discovery and discovery.get('platform') == platform
            and discovery.get('operation') not in (None, 'none')):
        # Old browser routes do not all track descendant process shutdown. They
        # remain usable serially; this wave adds no generic browser supervisor.
        for path in (root / '.private/parallel-read/jobs').glob('*/state.json'):
            if json.loads(path.read_text('utf-8')).get('status') in ('starting', 'running'):
                raise BridgeError('parallel_route_unsupported', '此旧实验入口尚未接入并行隔离，请等待后台读取结束。')
    return scope, inputs


def verify_markup_repair_conflicts(inputs):
    for path, raw in inputs.items():
        if path.name not in MARKUP_REPAIR_CONFLICTS or raw is None:
            continue
        job = json.loads(raw)
        if (job.get('armed') is True or (path.name == 'lab-discovery.json'
                and job.get('operation') not in (None, 'none'))):
            raise BridgeError('lab_repair_conflict', '请先停用其他实验写入、恢复和调度指令，再执行此限定修复。')


def verify_inputs(inputs):
    if any((path.read_bytes() if path.exists() else None) != raw for path, raw in inputs.items()):
        raise BridgeError('lab_intent_changed', '实验任务调度期间配置已变化，未执行请求。')

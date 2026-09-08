"""One lab-only BD20 optimization: reuse the exact fresh Huawei21 fetch, or refuse."""
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

from fetch_scope import snapshot_binding, task_summary
from lab_platform_lock import file_lock
from xiaomi_browser_scope import _ReadOnlyStore

from note_bridge.errors import BridgeError

BATCH = 'matrix-20260907-wps-huawei-BD20'
BD19_MIGRATION = '820817e1265643599176fbe7a7870ec6'
MAX_AGE_SECONDS = 180


def require(condition):
    if not condition:
        raise BridgeError('BD20_before_scope_invalid', 'BD20 的明确读取范围、账号、时效或原件已不一致；未重新读取或迁入。')


def validate_route(job):
    reference = job.get('target_before_fetch')
    require(job.get('id') == BATCH and job.get('kind') == 'cloud-matrix-batch'
            and job.get('target') == 'huawei' and job.get('source_policy') == 'direct_seed_only'
            and job.get('armed') is False and len(job.get('sources', [])) == 1)
    origin = job['sources'][0].get('from_cloud_fixture', {})
    require(origin.get('platform') == 'wps' and origin.get('manifest') == 'wps-complex-BD1-job.json'
            and 'batch_manifest' not in origin)
    require(isinstance(reference, dict) and set(reference) == {'path', 'sha256', 'task_id'})
    require(isinstance(reference['task_id'], str) and re.fullmatch(r'[a-f0-9]{32}', reference['task_id'])
            and isinstance(reference['sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', reference['sha256'])
            and reference['path'] == '.private/evidence/fetch-' + reference['task_id'] + '-scope.json')
    return reference


def require_owned_locks(root):
    """The foreground caller owns both platforms; stale JSON alone cannot establish a lock."""
    for platform in ('huawei', 'wps'):
        folder = root / '.private/parallel-read/locks'
        try:
            record = json.loads((folder / (platform + '-owner.json')).read_text('utf-8'))
            require(record.get('state') == 'active' and record.get('platform') == platform
                    and record.get('pid') == os.getpid() and (folder / (platform + '.lock')).is_file())
            try:
                with file_lock(folder / (platform + '.lock')):
                    pass
            except BridgeError as error:
                require(error.code == 'lab_platform_busy')
            else:
                require(False)
        except (OSError, ValueError, TypeError) as error:
            raise BridgeError('BD20_before_scope_invalid', 'BD20 复用前尚未持有来源和目标平台锁。') from error


def validate(job, store, root, account, *, now=None):
    """Purely local preflight for the private arm helper; the runner repeats it under its locks."""
    reference = validate_route(job)
    require(account == job.get('expected_account'))
    try:
        payload = (root / reference['path']).read_bytes()
        require(hashlib.sha256(payload).hexdigest() == reference['sha256'])
        scope = json.loads(payload)
        prior = json.loads((root / '.private/checkpoints/wps-huawei-BD18-job.json').read_text('utf-8'))
        require(prior.get('id') == 'matrix-20260907-wps-huawei-BD18' and prior.get('armed') is False
                and prior.get('target') == 'huawei' and prior.get('expected_account') == account
                and len(prior.get('sources', [])) == 2 and job['sources'] == [prior['sources'][1]])
        # One SQLite read transaction binds the task, snapshot, returned documents and original files.
        with store.connection() as db:
            db.row_factory = sqlite3.Row
            if not db.in_transaction:
                db.execute('BEGIN')
            view = _ReadOnlyStore(db)
            report, migration = view.task(reference['task_id']), view.task(BD19_MIGRATION)
            require(report is not None and report.id == reference['task_id'] and report.operation == 'fetch'
                    and report.status == 'succeeded' and report.finished_at and not report.issues
                    and report.total == report.completed == report.succeeded == 21 and report.skipped == 0)
            require(migration is not None and migration.operation == 'matrix_migrate' and migration.finished_at
                    and migration.status == 'partial' and migration.total == migration.completed == migration.succeeded == 2
                    and {issue.code for issue in migration.issues} == {'format_downgrade'})
            finished = datetime.fromisoformat(report.finished_at)
            started = datetime.fromisoformat(report.started_at)
            migrated = datetime.fromisoformat(migration.finished_at)
            require(all(value.utcoffset() is not None for value in (finished, started, migrated)))
            instant = now if now is not None else datetime.now(timezone.utc)
            require(instant.utcoffset() is not None and migrated < started <= finished
                    and 0 <= (instant - finished).total_seconds() <= MAX_AGE_SECONDS)
            require(scope.get('kind') == 'completed-fetch-scope' and scope.get('platform') == 'huawei'
                    and scope.get('account') == account and scope.get('task') == task_summary(report))
            binding = snapshot_binding(root, view, 'huawei', account)
            require(len(binding['notes']) == 21 and all(scope.get(key) == value for key, value in binding.items()))
            notes = {note.source_id: note for note in view.notes('huawei', account)}
            require({key: note.fingerprint() for key, note in notes.items()} == binding['notes'])
        return notes, {'mode': 'BD20_exact_completed_fetch', **reference, 'notes': 21,
                       'validated_at': instant.isoformat(), 'max_age_seconds': MAX_AGE_SECONDS}
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise BridgeError('BD20_before_scope_invalid', 'BD20 读取范围凭据无法完整验证，未重新读取或迁入。') from error


def reuse(job, store, root, account):
    require_owned_locks(root)
    return validate(job, store, root, account)

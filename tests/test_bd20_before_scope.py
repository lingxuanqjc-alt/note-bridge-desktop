"""BD20 may save one repeated read only when the exact recent whole snapshot still holds."""
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, Block, ItemIssue, NoteDocument, PlatformId, Span, TaskReport
from note_bridge.providers.base import CreatedNote, Snapshot
from note_bridge.storage import Store

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import bd20_before_scope as scope  # noqa: E402
from lab_platform_lock import PlatformLocks  # noqa: E402

spec = importlib.util.spec_from_file_location('BD20_matrix_test', SCRIPTS / 'live-matrix-job.py')
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)
ACCOUNT, TASK = 'a' * 64, 'b' * 32


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def save_scope(root, store, job, report):
    store.save_task(report)
    relative = '.private/evidence/fetch-' + TASK + '-scope.json'
    write(root / relative, {'kind': 'completed-fetch-scope', 'platform': 'huawei', 'account': ACCOUNT,
        'task': scope.task_summary(report), **scope.snapshot_binding(root, store, 'huawei', ACCOUNT)})
    job['target_before_fetch'] = {'path': relative, 'task_id': TASK,
        'sha256': hashlib.sha256((root / relative).read_bytes()).hexdigest()}


@pytest.fixture
def example(tmp_path):
    store = Store(tmp_path / '.private/session-lab/notes.sqlite')
    original = tmp_path / '.private/session-lab/resources/original.png'
    original.parent.mkdir(parents=True)
    original.write_bytes(b'synthetic-original')
    notes = [NoteDocument(platform='huawei', account_id=ACCOUNT, source_id=f'old-{i}',
        title=f'Synthetic {i}') for i in range(21)]
    notes[0].attachments = [Attachment(id='original', name='original.png', kind='image',
        local_path=original.name, sha256=hashlib.sha256(original.read_bytes()).hexdigest(), size=original.stat().st_size)]
    store.replace_snapshot('huawei', ACCOUNT, notes, True)
    source = NoteDocument(platform='wps', account_id='source-account', source_id='fixed-BD1',
        title='Synthetic final source', blocks=[Block(spans=[Span(text='Synthetic content')])])
    store.replace_snapshot('wps', source.account_id, [source], True)
    origin = {'from_cloud_fixture': {'platform': 'wps', 'manifest': 'wps-complex-BD1-job.json',
        'account': source.account_id, 'source_id': source.source_id, 'fingerprint': source.fingerprint()}}
    job = {'id': scope.BATCH, 'kind': 'cloud-matrix-batch', 'target': 'huawei', 'armed': False,
        'source_policy': 'direct_seed_only', 'expected_account': ACCOUNT, 'sources': [origin]}
    write(tmp_path / '.private/checkpoints/wps-huawei-BD18-job.json', {
        'id': 'matrix-20260907-wps-huawei-BD18', 'armed': False, 'target': 'huawei',
        'expected_account': ACCOUNT, 'sources': [{}, origin]})
    now = datetime.now(timezone.utc)
    store.save_task(TaskReport(id=scope.BD19_MIGRATION, operation='matrix_migrate', status='partial',
        total=2, completed=2, succeeded=2, issues=[ItemIssue(code='format_downgrade', message='Synthetic report')],
        started_at=(now - timedelta(seconds=100)).isoformat(), finished_at=(now - timedelta(seconds=90)).isoformat()))
    report = TaskReport(id=TASK, operation='fetch', status='succeeded', total=21, completed=21, succeeded=21,
        started_at=(now - timedelta(seconds=60)).isoformat(), finished_at=(now - timedelta(seconds=10)).isoformat())
    save_scope(tmp_path, store, job, report)
    return tmp_path, store, job, report, notes, source, now


def test_local_arm_precheck_is_readonly_but_runtime_requires_actual_platform_locks(example):
    root, store, job, _, notes, _, now = example
    before = store.path.read_bytes()
    acquired, proof = scope.validate(job, store, root, ACCOUNT, now=now)
    assert set(acquired) == {note.source_id for note in notes} and proof['notes'] == 21
    assert before == store.path.read_bytes()
    with pytest.raises(BridgeError):
        scope.reuse(job, store, root, ACCOUNT)
    with PlatformLocks(root, ('huawei', 'wps')):
        assert len(scope.reuse(job, store, root, ACCOUNT)[0]) == 21
    # Released owner JSON must not be accepted as a currently held lock.
    with pytest.raises(BridgeError):
        scope.reuse(job, store, root, ACCOUNT)


@pytest.mark.parametrize('failure', ['other-batch', 'wrong-account', 'expired', 'future', 'wrong-sha',
                                   'changed-cache', 'incomplete-cache', 'changed-original'])
def test_scope_mismatch_refuses_without_reacquisition(example, failure):
    root, store, job, _, notes, _, now = example
    if failure == 'other-batch':
        job['id'] = 'matrix-20260907-wps-huawei-BD21'
    elif failure == 'wrong-account':
        job['expected_account'] = 'c' * 64
    elif failure == 'expired':
        now += timedelta(seconds=180)
    elif failure == 'future':
        now -= timedelta(seconds=20)
    elif failure == 'wrong-sha':
        job['target_before_fetch']['sha256'] = '0' * 64
    elif failure == 'changed-cache':
        notes[1].title = 'Changed original note'
        store.replace_snapshot('huawei', ACCOUNT, notes, True)
    elif failure == 'incomplete-cache':
        store.replace_snapshot('huawei', ACCOUNT, notes, False)
    else:
        (root / '.private/session-lab/resources/original.png').write_bytes(b'Corrupt original')
    with pytest.raises(BridgeError):
        scope.validate(job, store, root, ACCOUNT, now=now)


@pytest.mark.parametrize('valid', [True, False])
def test_runner_reuses_before_but_still_reads_real_after_and_never_falls_back(example, monkeypatch, valid):
    root, _, job, _, rows, source, _ = example
    (root / 'scripts').mkdir()
    (root / 'scripts/live-fixture-job.py').write_bytes((SCRIPTS / 'live-fixture-job.py').read_bytes())
    if not valid:
        job['target_before_fetch']['sha256'] = '0' * 64
    write(root / '.private/lab-matrix-job.json', {**job, 'armed': True})
    calls = []

    def create(note, context):
        calls.append('write')
        rows.append(note.model_copy(update={'platform': PlatformId.HUAWEI, 'account_id': ACCOUNT, 'source_id': 'new'}))
        return CreatedNote(['new'])

    def fetch(context):
        assert calls == ['write'], 'The original full read must not repeat or hide a refused scope.'
        calls.append('after-read')
        return Snapshot(rows, True)

    provider = SimpleNamespace(spec=SimpleNamespace(id='huawei'), account_id=ACCOUNT, probe=lambda: ACCOUNT,
        preflight=lambda notes: None, create=create, fetch=fetch, close=lambda: None)
    monkeypatch.setattr(matrix, 'select_batch', lambda *args: [source])
    monkeypatch.setattr(matrix, 'create_provider', lambda *args: provider)
    with PlatformLocks(root, ('huawei', 'wps')):
        result = matrix.run_armed_job('huawei', [], root)
    assert json.loads((root / '.private/lab-matrix-job.json').read_text('utf-8'))['armed'] is False
    if valid:
        assert calls == ['write', 'after-read']
        assert result['status'] == 'api_verified' and result['before_count'] == 21 and result['after_count'] == 22
        assert result['original_target_notes_unchanged'] and result['only_confirmed_additions']
    else:
        assert calls == [] and result['status'] == 'needs_review' and result['code'] == 'BD20_before_scope_invalid'

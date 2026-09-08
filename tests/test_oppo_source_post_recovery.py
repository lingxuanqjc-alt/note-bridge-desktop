"""Recovery must use a later exact capture, never retry writes or rewrite failed proof."""
import importlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from test_oppo_source_post import completed_batches, oppo, post

from note_bridge.errors import BridgeError

helper = importlib.import_module('oppo_source_post_recovery')


def setup(tmp_path, monkeypatch):
    ctx, bi4, checks = completed_batches(tmp_path, monkeypatch)

    def failure(jars, root):
        directory = root / '.private/evidence' / ('oppo-fixed-source-' + uuid4().hex)
        directory.mkdir()
        proof = {'kind': oppo.OPERATION, 'status': 'blocked', 'scope_complete': False, 'whole_account_snapshot': False,
            'formal_acceptance': False, 'cloud_writes': 0, 'cache_writes': 0, 'receipt_writes': 0,
            'physical_requests': 1, 'group_shapes': [], 'code': 'platform_response',
            'captured_started_at': datetime.now(timezone.utc).isoformat(),
            'captured_finished_at': datetime.now(timezone.utc).isoformat()}
        path = directory / 'proof.json'
        path.write_text(json.dumps(proof), 'utf8')
        reference = {'path': path.relative_to(root).as_posix(), 'sha256': oppo.digest(path.read_bytes())}
        monkeypatch.setattr(helper, 'FAILED_CAPTURE', reference)
        return {key: value for key, value in proof.items() if not key.startswith('captured_')} | {'proof': reference}

    monkeypatch.setattr(post, 'capture', failure)
    assert post.run([], tmp_path, bi4)['status'] == 'needs_review'
    monkeypatch.setattr(helper, 'OLD_SHA256', oppo.digest((tmp_path / post.OUTPUT).read_bytes()))
    fresh = oppo.capture([], tmp_path)['proof']  # Synthetic HTTP fixture, completed before offline recovery.
    return ctx, bi4, checks, fresh


def test_later_fixed_capture_recovers_six_without_changing_old_failure_or_database(tmp_path, monkeypatch):
    ctx, bi4, checks, fresh = setup(tmp_path, monkeypatch)
    database = ctx.private / 'session-lab/notes.sqlite'
    unchanged = {path: path.read_bytes() for path in (database, tmp_path / post.OUTPUT,
                                                    tmp_path / helper.FAILED_CAPTURE['path'])}
    calls = len(ctx.calls)
    monkeypatch.setattr(oppo, 'capture', lambda *a: pytest.fail('Offline recovery cannot capture again'))
    result = helper.run(tmp_path, fresh, bi4)
    proof = json.loads((tmp_path / result['evidence']).read_text('utf8'))
    assert result['status'] == 'verified' and result['covered_batches'] == 6
    assert result['source_notes'] == 2 and result['source_images'] == 4
    assert all(result[k] == 0 for k in ('cloud_writes', 'additional_cloud_reads', 'cache_writes', 'receipt_writes'))
    assert proof['original_failure']['status'] == 'needs_review'
    assert proof['source_fingerprints_unchanged'] is proof['source_versions_unchanged'] is True
    assert proof['huawei_target_original_22_body_integrity'] == 'not_rechecked'
    assert proof['whole_account_snapshot'] is False
    assert len(ctx.calls) == calls and all(p.read_bytes() == raw for p, raw in unchanged.items())
    assert helper.digest((tmp_path / result['evidence']).read_bytes()) == result['sha256']
    with pytest.raises(FileExistsError):
        helper.run(tmp_path, fresh, bi4)
    assert len(ctx.calls) == calls


@pytest.mark.parametrize('fault', ['old_hash', 'body_was_read', 'physical_count', 'old_success', 'wrong_BI4',
    'batch_changed', 'receipt_unknown', 'old_capture', 'before_failure_end', 'version_changed', 'capture_hash', 'asset_changed'])
def test_recovery_rejects_unrelated_failures_replay_changes_and_unconfirmed_routes(tmp_path, monkeypatch, fault):
    ctx, bi4, _, fresh = setup(tmp_path, monkeypatch)
    old_path = tmp_path / post.OUTPUT
    old = json.loads(old_path.read_text('utf8'))
    if fault in ('old_hash', 'old_success', 'body_was_read', 'physical_count'):
        if fault == 'old_hash':
            old['annotation'] = 'changed'
        elif fault == 'old_success':
            old['status'] = 'verified'
        elif fault == 'body_was_read':
            (tmp_path / helper.FAILED_CAPTURE['path']).with_name('notes.json').write_text('[]', 'utf8')
        else:
            old['fresh_capture']['physical_requests'] = 2
        old_path.write_text(json.dumps(old), 'utf8')
        if fault != 'old_hash':
            monkeypatch.setattr(helper, 'OLD_SHA256', helper.digest(old_path.read_bytes()))
    elif fault == 'wrong_BI4':
        bi4 = {**bi4, 'sha256': 'c' * 64}
    elif fault == 'batch_changed':
        p = tmp_path / '.private/evidence/matrix-20260907-oppo-wps-BI2.json'
        value = json.loads(p.read_text('utf8'))
        value['annotation'] = 'a changed audit may never be substituted'
        p.write_text(json.dumps(value), 'utf8')
    elif fault == 'receipt_unknown':
        ctx.store.save_receipt(old['before']['batches'][0]['receipts'][0]['key'], 'uncertain')
    elif fault == 'old_capture':
        fresh = old['before']['source_capture']
    elif fault == 'capture_hash':
        fresh = {**fresh, 'sha256': 'f' * 64}
    elif fault == 'asset_changed':
        note = oppo.verify_source(fresh, ctx.store, tmp_path)[0]
        (tmp_path / '.private/session-lab/resources' / note.attachments[0].local_path).write_bytes(b'damaged')
    else:
        path = tmp_path / fresh['path']
        proof = json.loads(path.read_text('utf8'))
        if fault == 'before_failure_end':
            proof['captured_started_at'] = old['started_at']
        else:
            proof['versions'] = dict.fromkeys(proof['versions'], 'changed-version')
        path.write_text(json.dumps(proof), 'utf8')
        fresh = {**fresh, 'sha256': helper.digest(path.read_bytes())}
    calls = len(ctx.calls)
    with pytest.raises(BridgeError):
        helper.run(tmp_path, fresh, bi4)
    assert len(ctx.calls) == calls and not (tmp_path / helper.RECOVERED_OUTPUT).exists()

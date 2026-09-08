"""Offline recovery of the one identity-only OPPO final source-read failure."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from oppo_scoped_source import OPERATION as CAPTURE_OPERATION
from oppo_scoped_source import digest, require, verify_source
from oppo_source_post import BATCHES, OPERATION, OUTPUT, read_audit
from xiaomi_browser_scope import _ReadOnlyStore

from note_bridge.paths import confined

OLD_SHA256 = '6ed660b80e1d7411f8a0be885a83277e1a5af6af24898c0ea04f364d5a6d6663'
FAILED_CAPTURE = {
    'path': '.private/evidence/oppo-fixed-source-f8369747176e41fbbeca27efce88d291/proof.json',
    'sha256': '41fa8cf6099993b4a0fa1bbbf6b01f8b879b2e2b365f609168589f70439f078e',
}
RECOVERED_OUTPUT = '.private/evidence/oppo-BI2-BI12-source-post-recovered.json'


def _failure(root, files):
    old_raw = (root / OUTPUT).read_bytes()
    require(digest(old_raw) == OLD_SHA256, 'source_post_failure_changed')
    old = json.loads(old_raw)
    require(set(old) == {'kind', 'status', 'formal_acceptance', 'whole_account_snapshot', 'scope_complete',
        'covered_batches', 'source_notes', 'source_images', 'cloud_writes', 'cache_writes', 'receipt_writes',
        'batch_ids', 'huawei_target_original_22_body_integrity', 'started_at', 'before', 'fresh_capture', 'code', 'finished_at'}
        and old['kind'] == OPERATION and old['status'] == 'needs_review' and old['code'] == 'invalid_oppo_source_scope'
        and old['formal_acceptance'] is False and old['whole_account_snapshot'] is False and old['scope_complete'] is False
        and old['covered_batches'] == 6 and old['source_notes'] == 2 and old['source_images'] == 4
        and old['batch_ids'] == [f'matrix-20260907-oppo-{target}-{label}' for target, label in BATCHES]
        and old['huawei_target_original_22_body_integrity'] == 'not_rechecked'
        and all(type(old[k]) is int and old[k] == 0 for k in ('cloud_writes', 'cache_writes', 'receipt_writes')))
    failed_path = confined(root, FAILED_CAPTURE['path'])
    failed_raw = failed_path.read_bytes()
    require(digest(failed_raw) == FAILED_CAPTURE['sha256'] and not failed_path.with_name('notes.json').exists(),
            'source_post_failure_changed')
    failed = json.loads(failed_raw)
    expected = {'kind': CAPTURE_OPERATION, 'status': 'blocked', 'scope_complete': False,
        'whole_account_snapshot': False, 'formal_acceptance': False, 'cloud_writes': 0, 'cache_writes': 0,
        'receipt_writes': 0, 'physical_requests': 1, 'group_shapes': [], 'code': 'platform_response'}
    require(old['fresh_capture'] == {**expected, 'proof': FAILED_CAPTURE}
        and set(failed) == set(expected) | {'captured_started_at', 'captured_finished_at'}
        and {k: failed[k] for k in expected} == expected, 'source_post_failure_not_identity_only')
    start, failed_start, failed_end, finish = map(datetime.fromisoformat,
        (old['started_at'], failed['captured_started_at'], failed['captured_finished_at'], old['finished_at']))
    require(all(t.tzinfo for t in (start, failed_start, failed_end, finish))
            and start <= failed_start <= failed_end <= finish <= datetime.now(timezone.utc))
    files[OUTPUT], files[FAILED_CAPTURE['path']] = OLD_SHA256, FAILED_CAPTURE['sha256']
    return old, finish


def run(root, fresh_reference, bi4_reference):
    """Accept explicit immutable captures only; no session, transport or cache mutation."""
    output = root / RECOVERED_OUTPUT
    if output.exists():
        raise FileExistsError(RECOVERED_OUTPUT)
    files = {}
    old, failed_finish = _failure(root, files)
    before = old['before']
    require(isinstance(before.get('batches'), list) and len(before['batches']) == 6
            and bi4_reference == before['batches'][1].get('target_readback'), 'source_post_BI4_reference_changed')
    require(read_audit(root, bi4_reference) == before, 'source_post_binding_changed')
    with closing(sqlite3.connect((root / '.private/session-lab/notes.sqlite').resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        actual = verify_source(fresh_reference, _ReadOnlyStore(db), root)
    require(len(actual) == 2 and sum(len(n.attachments) for n in actual) == 4
            and {n.source_id: n.fingerprint() for n in actual} == before['source_fingerprints'], 'source_post_changed')
    original_ref = before['source_capture']
    require(fresh_reference['path'] not in (original_ref['path'], FAILED_CAPTURE['path']), 'source_post_not_fresh')
    proofs = []
    for reference in (original_ref, fresh_reference):
        path = confined(root, reference['path'])
        raw = path.read_bytes()
        require(digest(raw) == reference['sha256'], 'source_post_capture_changed')
        files[reference['path']] = reference['sha256']
        proofs.append(json.loads(raw))
    previous, fresh = proofs
    fresh_start, fresh_finish = map(datetime.fromisoformat, (fresh['captured_started_at'], fresh['captured_finished_at']))
    require(failed_finish < fresh_start <= fresh_finish <= datetime.now(timezone.utc), 'source_post_not_fresh')
    require(previous['versions'] == fresh['versions'], 'source_post_changed')
    require(read_audit(root, bi4_reference) == before, 'source_post_binding_changed')
    require(all(digest(confined(root, path).read_bytes()) == sha for path, sha in files.items()), 'source_post_binding_changed')
    report = {'kind': OPERATION + '_recovered', 'status': 'verified', 'scope_complete': True,
        'formal_acceptance': False, 'whole_account_snapshot': False, 'covered_batches': 6,
        'source_notes': 2, 'source_images': 4, 'batch_ids': old['batch_ids'],
        'source_scope': 'fixed_original_F2_OR1_only', 'source_fingerprints_unchanged': True,
        'source_versions_unchanged': True, 'cloud_writes': 0, 'additional_cloud_reads': 0,
        'cache_writes': 0, 'receipt_writes': 0, 'before': before, 'fresh_capture': fresh_reference,
        'huawei_target_original_22_body_integrity': 'not_rechecked',
        'original_failure': {'path': OUTPUT, 'sha256': OLD_SHA256, 'status': 'needs_review'},
        'failed_finished_at': old['finished_at'], 'captured_started_at': fresh['captured_started_at'],
        'captured_finished_at': fresh['captured_finished_at'], 'evidence_sha256': files,
        'verified_at': datetime.now(timezone.utc).isoformat()}
    encoded = json.dumps(report, ensure_ascii=True, indent=2).encode('utf8')
    with output.open('xb') as stream:
        stream.write(encoded)
    return {k: report[k] for k in ('kind', 'status', 'scope_complete', 'covered_batches', 'source_notes', 'source_images',
        'whole_account_snapshot', 'huawei_target_original_22_body_integrity', 'cloud_writes',
        'additional_cloud_reads', 'cache_writes', 'receipt_writes')} | {
            'evidence': RECOVERED_OUTPUT, 'sha256': digest(encoded)}


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Offline exact F2/OR1 source-post recovery; accepts no login data.')
    parser.add_argument('--fresh-capture', required=True)
    parser.add_argument('--fresh-sha256', required=True)
    parser.add_argument('--bi4-readback', required=True)
    parser.add_argument('--bi4-sha256', required=True)
    args = parser.parse_args()
    print(json.dumps(run(Path(__file__).resolve().parents[1],
        {'path': args.fresh_capture, 'sha256': args.fresh_sha256},
        {'path': args.bi4_readback, 'sha256': args.bi4_sha256}), ensure_ascii=True))

"""One-use official UI save probe, scoped to a synthetic note and checked against real readback."""
import json
import os
from pathlib import Path
import subprocess

from lab_store import LabStore as Store

from note_bridge.operations import fetch_snapshot
from note_bridge.providers.factory import create_provider
from note_bridge.tasks import TaskRunner


def run(jars, root: Path):
    path = root / '.private/huawei-native-job.json'
    job = json.loads(path.read_text('utf-8'))
    if job.get('armed') is not True or job.get('title') != '笔记互迁验收 · 官网保存 H4':
        raise ValueError('native_job_not_armed')
    provider = create_provider('huawei', jars, root / '.private/session-lab/resources')
    try:
        if provider.probe() != job['expected_account']:
            raise ValueError('account_changed')
        job['armed'] = False
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(job, ensure_ascii=False), 'utf-8')
        os.replace(temporary, path)
        store = Store(root / '.private/session-lab/notes.sqlite')
        runner = TaskRunner(store)
        runner.start('native_before', lambda ctx: fetch_snapshot(provider, store, ctx))
        runner.join()
        if runner.current().status != 'succeeded':
            raise ValueError('incomplete_before_snapshot')
        before = {n.source_id: n.fingerprint() for n in store.notes('huawei', provider.account_id)}
        cookies = [{'name': m.key, 'value': m.value, 'domain': m['domain'], 'path': m['path'] or '/', 'secure': bool(m['secure'])}
                   for jar in jars for m in jar.values()]
        result = subprocess.run(['node', str(root / 'scripts/huawei-native-save.cjs')],
            input=json.dumps({'cookies': cookies, 'title': job['title']}), text=True, encoding='utf-8', capture_output=True, timeout=145)
        cookies.clear()
        report = json.loads(result.stdout)
        runner.start('native_after', lambda ctx: fetch_snapshot(provider, store, ctx))
        runner.join()
        after = {n.source_id: n for n in store.notes('huawei', provider.account_id)}
        report.update(before_count=len(before), after_count=len(after),
                      original_notes_unchanged=all(k in after and after[k].fingerprint() == v for k, v in before.items()))
        receipt_path = root / '.private/evidence/huawei-native-H4-receipt.json'
        receipt = json.loads(receipt_path.read_text('utf-8')) if receipt_path.exists() else {}
        actual = after.get(receipt.get('remote_id'))
        report['directory_visible'] = actual is not None
        report['title_matches'] = actual is not None and actual.title == job['title']
        report['updated_text_present'] = actual is not None and '第二次保存验证。' in ''.join(
            span.text for block in actual.blocks for span in block.spans)
        report['status'] = ('verified_native_save' if actual is not None and report['title_matches']
            and report['updated_text_present'] and report['original_notes_unchanged']
            and report['status'] == 'save_acknowledged' else
            'blocked_before_write' if not report['requests'] and report['status'] == 'blocked_before_write' else 'needs_review')
        (root / '.private/evidence/huawei-native-H4.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
        return report
    finally:
        provider.close()

"""Read-only inspection of four previously confirmed synthetic Xiaomi notes."""
import hashlib
import importlib.util
import json
from threading import Event
from types import SimpleNamespace
from urllib.parse import quote

from cloud_fixture_source import select_source

from note_bridge.operations import receipt_key
from note_bridge.providers.factory import create_provider
from note_bridge.providers.xiaomi import parse_entry
from note_bridge.storage import Store


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def run(jars, root):
    for name in ('lab-matrix-job.json', 'lab-write-job.json'):
        assert json.loads((root / '.private' / name).read_text('utf-8')).get('armed') is False
    spec = importlib.util.spec_from_file_location('xiaomi_repair_fixture', root / 'scripts/live-fixture-job.py')
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    batch = json.loads((root / '.private/checkpoints/vivo-xiaomi-AD1-job.json').read_text('utf-8'))
    single = json.loads((root / '.private/checkpoints/xiaomi-image-AC6-job.json').read_text('utf-8'))
    assert batch['id'] == 'matrix-20260907-vivo-xiaomi-AD1' and batch['armed'] is False
    assert single['id'] == 'fixture-20260907-xiaomi-image-AC6' and single['armed'] is False
    assert batch['expected_account'] == single['expected_account']
    store = Store(root / '.private/session-lab/notes.sqlite')
    notes = [select_source({**row, 'target': 'xiaomi'}, store, root, fixture.fixture) for row in batch['sources']]
    notes.append(fixture.fixture(single))
    provider = create_provider('xiaomi', jars, root / '.private/session-lab/resources')
    report = {'kind': 'xiaomi-AF-scoped-readonly-inspection', 'formal_acceptance': False,
              'cloud_writes': 0, 'creates': 0, 'uploads': 0, 'status': 'needs_review', 'items': []}
    try:
        assert provider.probe() == batch['expected_account']
        entries, cursor, cursors = [], None, set()
        for _ in range(10000):
            page = provider._page(cursor)
            entries.extend(row for row in page['entries'] if row.get('type') != 'folder'
                           and row.get('status') not in ('deleted', 'purged'))
            if page['lastPage']:
                break
            next_cursor = page.get('syncTag')
            assert next_cursor is not None and str(next_cursor) not in cursors
            cursors.add(str(next_cursor))
            cursor = next_cursor
        else:
            raise ValueError('Pagination limit exceeded')
        assert len({str(row['id']) for row in entries}) == len(entries)
        # All existing notes get raw hashes; no cache rewrite with changed parser semantics.
        raw_hashes = {}
        details = {}
        for row in entries:
            identity = str(row['id'])
            detail = provider._json('GET', '/note/note/' + quote(identity, safe='') + '/')['entry']
            assert str(detail['id']) == identity and not detail.get('encryptInfo')
            raw_hashes[identity] = digest(detail)
            details[identity] = detail
        context = SimpleNamespace(check_cancel=lambda: None, cancelled=Event())
        scoped = []
        for index, source in enumerate(notes):
            receipt = store.receipt(receipt_key(source, provider))
            assert receipt and receipt['status'] == 'confirmed' and len(receipt['remote_ids']) == 1
            identity = receipt['remote_ids'][0]
            assert identity in details
            raw = details[identity]
            parsed = parse_entry(raw, provider.account_id, {})
            matches = {}
            for asset in parsed.attachments:
                provider._download(asset, context)
                sources = [a for a in source.attachments if (a.sha256, a.size) == (asset.sha256, asset.size)]
                assert len(sources) == 1 and sources[0].id not in matches
                matches[sources[0].id] = asset.id
            assert len(matches) == len(source.attachments) == 2
            assert {b.attachment_id for b in parsed.blocks if b.kind == 'attachment'} == set(matches.values())
            scoped.append({'index': index, 'target_id': identity, 'raw_sha256': raw_hashes[identity],
                           'source_fingerprint': source.fingerprint(), 'images': matches})
            report['items'].append({'index': index, 'receipt': 'confirmed', 'images_original_bytes': True,
                                    'images': len(matches), 'decoded_blocks': len(parsed.blocks),
                                    'target_body_differs_from_new_protocol': not raw['content'].startswith('<new-format/>')})
        assert len({row['target_id'] for row in scoped}) == len(notes) == 4
        plan = {'kind': report['kind'], 'account': provider.account_id, 'target_raw_hashes': raw_hashes,
                'items': scoped, 'armed': False}
        (root / '.private/checkpoints/xiaomi-AF-readonly-plan.json').write_text(json.dumps(plan, indent=2), 'utf-8')
        report.update(status='scope_verified', existing_notes=len(details), scoped_notes=len(scoped))
        (root / '.private/evidence/xiaomi-AF-readonly-inspection.json').write_text(json.dumps(report, indent=2), 'utf-8')
        return report
    finally:
        provider.close()

"""Resolve a synthetic fixture's confirmed remote ID without ambiguous title selection."""
import hashlib
import importlib.util
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from types import SimpleNamespace

from cloud_fixture_source import select_source

from note_bridge.operations import receipt_key
from note_bridge.paths import confined
from note_bridge.providers.xiaomi_groups import folder_key
from note_bridge.storage import Store


class _ReadOnlyStore(Store):
    def __init__(self, database):
        database.row_factory = sqlite3.Row
        self.database = database

    @contextmanager
    def connection(self):
        yield self.database


def _require(condition):
    if not condition:
        raise ValueError('unverified_fixture_scope')


def _module(root, name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), root / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def browser_target(scope, root, account):
    name, index = scope.get('manifest'), scope.get('index')
    _require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9-]+-job\.json', name))
    _require(type(index) is int and 0 <= index < 30)
    manifest = json.loads((root / '.private/checkpoints' / name).read_text('utf-8'))
    _require(manifest.get('kind') == 'cloud-matrix-batch' and manifest.get('target') == 'xiaomi')
    _require(manifest.get('armed') is False and manifest.get('expected_account') == account)
    _require(re.fullmatch(r'matrix-\d{8}-[A-Za-z0-9-]{1,30}', manifest.get('id', '')))
    _require(isinstance(manifest.get('sources'), list) and index < len(manifest['sources']))
    evidence = json.loads((root / '.private/evidence' / (manifest['id'] + '.json')).read_text('utf-8'))
    _require(evidence.get('batch_id') == manifest['id'] and evidence.get('target') == 'xiaomi')
    _require(evidence.get('status') in ('api_verified', 'api_verified_with_degradation'))
    _require(evidence.get('original_target_notes_unchanged') is True and evidence.get('only_confirmed_additions') is True)
    _require(isinstance(evidence.get('items'), list) and index < len(evidence['items']))
    item = evidence['items'][index]
    _require(item.get('status') == 'api_verified' and item.get('receipt_status') == 'confirmed')
    _require(item.get('content_verified') is True and item.get('group_verified') is True)
    database = root / '.private/session-lab/notes.sqlite'
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        store = _ReadOnlyStore(db)
        source = select_source({**manifest['sources'][index], 'target': 'xiaomi'}, store, root,
                               _module(root, 'live-fixture-job').fixture)
        _require(item.get('source_id') == source.source_id and item.get('source_fingerprint') == source.fingerprint())
        target = SimpleNamespace(spec=SimpleNamespace(id='xiaomi'), account_id=account)
        receipt = store.receipt(receipt_key(source, target))
        _require(receipt and receipt['status'] == 'confirmed' and len(receipt['remote_ids']) == 1)
        identity = receipt['remote_ids'][0]
        _require(isinstance(identity, str) and re.fullmatch(r'\d{1,30}', identity))
        snapshot = store.snapshot('xiaomi', account)
        notes = store.notes('xiaomi', account)
        _require(snapshot['complete'] and snapshot['count'] == len(notes))
        matches = [note for note in notes if note.source_id == identity]
        _require(len(matches) == 1)
        actual = matches[0]
        _require(actual.display_title.startswith('笔记互迁验收 · '))
        warnings = [issue['message'] for issue in evidence.get('issues', [])
                    if issue.get('note_id') == source.source_id and issue.get('code') == 'format_downgrade']
        _require(_module(root, 'live-matrix-job').compare_note(source, actual, warnings))
        if source.source_folder_name:
            group = store.receipt(folder_key(source, account))
            _require(group and group['status'] == 'confirmed' and group['remote_ids'] == [actual.source_folder_id])
        for asset in actual.attachments:
            path = confined(root / '.private/session-lab/resources', asset.local_path or '')
            _require(asset.kind == 'image' and path.is_file())
            data = path.read_bytes()
            _require(len(data) == asset.size and hashlib.sha256(data).hexdigest() == asset.sha256)
        return {'fixtureId': identity, 'fixtureTitle': actual.display_title}

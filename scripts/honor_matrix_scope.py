"""Resolve only a verified synthetic Honor item; open the existing cache read-only."""
import hashlib
import importlib.util
import io
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from types import SimpleNamespace

from cloud_fixture_source import select_source
from PIL import Image

from note_bridge.operations import receipt_key
from note_bridge.paths import confined
from note_bridge.providers.honor_groups import folder_key
from note_bridge.storage import Store


class _ReadOnlyStore(Store):
    def __init__(self, database):
        database.row_factory = sqlite3.Row
        self.database = database

    @contextmanager
    def connection(self):
        yield self.database


def _module(root, name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), root / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _require(condition, code='unverified_fixture_scope'):
    if not condition:
        raise ValueError(code)


def browser_target(scope, root, account, platform='honor'):
    name, index = scope.get('manifest'), scope.get('index')
    _require(platform == 'honor')
    _require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9-]+-job\.json', name))
    _require(type(index) is int and 0 <= index < 30)
    manifest = json.loads((root / '.private/checkpoints' / name).read_text('utf-8'))
    _require(manifest.get('kind') == 'cloud-matrix-batch' and manifest.get('target') == platform)
    _require(manifest.get('armed') is False and manifest.get('expected_account') == account)
    _require(re.fullmatch(r'matrix-\d{8}-[A-Za-z0-9-]{1,30}', manifest.get('id', '')))
    _require(isinstance(manifest.get('sources'), list) and index < len(manifest['sources']))
    evidence = json.loads((root / '.private/evidence' / (manifest['id'] + '.json')).read_text('utf-8'))
    _require(evidence.get('batch_id') == manifest['id'] and evidence.get('target') == platform)
    _require(evidence.get('status') in ('api_verified', 'api_verified_with_degradation'))
    _require(evidence.get('original_target_notes_unchanged') is True and evidence.get('only_confirmed_additions') is True)
    _require(isinstance(evidence.get('items'), list) and index < len(evidence['items']))
    item = evidence['items'][index]
    _require(all(item.get(key) is True for key in ('content_verified', 'group_verified')))
    _require(item.get('status') == 'api_verified' and item.get('receipt_status') == 'confirmed')
    database = root / '.private/session-lab/notes.sqlite'
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        store = _ReadOnlyStore(db)
        fixture = _module(root, 'live-fixture-job').fixture
        source = select_source({**manifest['sources'][index], 'target': platform}, store, root, fixture)
        _require(item.get('source_id') == source.source_id and item.get('source_fingerprint') == source.fingerprint())
        target = SimpleNamespace(spec=SimpleNamespace(id=platform), account_id=account)
        receipt = store.receipt(receipt_key(source, target))
        _require(receipt and receipt['status'] == 'confirmed' and len(receipt['remote_ids']) == 1)
        identity = receipt['remote_ids'][0]
        _require(isinstance(identity, str) and re.fullmatch(r'[a-f0-9]{32}', identity))
        _require(store.snapshot(platform, account).get('complete') is True)
        notes = store.notes(platform, account)
        _require(store.snapshot(platform, account)['count'] == len(notes))
        matches = [note for note in notes if note.source_id == identity]
        _require(len(matches) == 1)
        actual = matches[0]
        _require(actual.display_title.startswith('笔记互迁验收 · '))
        warnings = [issue['message'] for issue in evidence.get('issues', [])
                    if issue.get('note_id') == source.source_id and issue.get('code') == 'format_downgrade']
        _require(_module(root, 'live-matrix-job').compare_note(source, actual, warnings), 'fixture_content_changed')
        default_group_binding = None
        if source.source_folder_name:
            group = store.receipt(folder_key(source, account))
            _require(group and group['status'] == 'confirmed' and group['remote_ids'] == [actual.source_folder_id])
            origin = manifest['sources'][index].get('from_cloud_fixture', {})
            # select_source already verified the fixed proof, original F2 receipt,
            # account, body and originals. A label alone cannot grant this exception.
            original_default = (manifest.get('oppo_scope') == 'OR1_fixed_direct_12'
                and manifest.get('source_policy') == 'direct_seed_only'
                and origin.get('manifest') == 'fixture-20260906-oppo-image-F2-consumed-job.json'
                and origin.get('platform') == source.platform == 'oppo' and 'scoped_proof' in origin
                and source.source_folder_id == '00000000_0000_0000_0000_000000000000'
                and source.source_folder_name == '未分类')
            pattern = r'未分类(?: \(\d+\))?' if original_default else r'笔记互迁分组验收 [A-Z0-9 -]+(?: \(\d+\)){0,8}'
            _require(actual.source_folder_name and re.fullmatch(pattern, actual.source_folder_name)
                     and len(actual.source_folder_name.encode('utf-16-le')) // 2 <= 50)
            if original_default:
                default_group_binding = {'fixture': 'fixture-20260906-oppo-image-F2',
                    'source_fingerprint': source.fingerprint(), 'target_id': identity,
                    'target_group_id': actual.source_folder_id}
        images = []
        for asset in actual.attachments:
            path = confined(root / '.private/session-lab/resources', asset.local_path or '')
            _require(asset.kind == 'image' and path.is_file())
            data = path.read_bytes()
            _require(len(data) == asset.size and hashlib.sha256(data).hexdigest() == asset.sha256, 'fixture_image_changed')
            with Image.open(io.BytesIO(data)) as picture:
                width, height = picture.size
                picture.verify()
            images.append({'id': asset.id, 'width': width, 'height': height})
        _require(actual.plain_text.strip() and len(actual.plain_text) <= 100000)
        styles = []
        for block_index, block in enumerate(actual.blocks):
            for span in block.spans:
                if span.text.strip() and any((span.bold, span.italic, span.underline, span.strike, span.highlight, span.link)):
                    styles.append({'block': block_index, 'text': span.text, **{
                        key: getattr(span, key) for key in ('bold', 'italic', 'underline', 'strike', 'highlight', 'link')}})
        return {'fixtureId': identity, 'title': actual.display_title, 'expectedText': actual.plain_text,
                'expected_images': len(images), 'image_specs': images, 'expected_styles': styles,
                'default_group_binding': default_group_binding,
                'group_name': actual.source_folder_name if source.source_folder_name else None,
                'group_id': actual.source_folder_id, 'headings': [
                    {'text': b.text, 'level': b.level} for b in actual.blocks if b.kind == 'heading'],
                'todos': [{'text': b.text, 'checked': b.checked} for b in actual.blocks if b.kind == 'todo'],
                'lists': [{'text': b.text, 'ordered': b.ordered} for b in actual.blocks if b.kind == 'list'],
                'limitations': ['Whitespace is ignored in body text comparison.',
                                'Reported source format degradations remain unchanged; this checks the cached target form.']}

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import WriteUncertain

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
spec = importlib.util.spec_from_file_location('xiaomi_complex_repair_test', Path(__file__).parents[1] / 'scripts/repair-xiaomi-complex.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


@pytest.mark.parametrize('mode', ['success', 'conflict', 'uncertain', 'changed_before', 'changed_other'])
def test_complex_repair_stops_on_unknown_or_changed_state_without_duplicate_writes(mode):
    originals = {key: {'id': key, 'content': 'old', 'setting': {'data': []}, 'folderId': 'folder', 'createDate': 1}
                 for key in ('first', 'second')}
    actual = deepcopy(originals)
    if mode == 'changed_before':
        actual['first']['content'] = 'user edit'
    planned = [(i, key, {**originals[key], 'content': 'new'}, []) for i, key in enumerate(originals)]
    report = {'items': [], 'update_requests': 0, 'status': 'needs_review'}
    saved, writes = [], []

    def request(method, path, **kwargs):
        key = path.rstrip('/').rsplit('/', 1)[-1]
        if method == 'POST':
            assert saved[-1]['items'][-1]['status'] == 'write_uncertain'
            assert path == '/note/note/' + key and kwargs['write']
            writes.append(key)
            if mode == 'uncertain':
                raise WriteUncertain()
            if mode == 'conflict':
                return {'conflict': True}
            actual[key]['content'] = 'new'
            if mode == 'changed_other':
                actual['second']['content'] = 'user edit'
            return {'tag': 'next'}
        return {'entry': deepcopy(actual[key])}

    provider = SimpleNamespace(_json=request, transport=SimpleNamespace(cookie=lambda _: 'synthetic'))
    if mode in ('uncertain', 'changed_before', 'changed_other'):
        with pytest.raises(WriteUncertain if mode == 'uncertain' else AssertionError):
            repair.execute_updates(provider, planned, originals, report, lambda: saved.append(deepcopy(report)))
    else:
        repair.execute_updates(provider, planned, originals, report, lambda: saved.append(deepcopy(report)))
    assert writes == ([] if mode == 'changed_before' else ['first', 'second'] if mode == 'success' else ['first'])
    assert (report['status'] == 'all_content_repaired_readback_verified') == (mode == 'success')

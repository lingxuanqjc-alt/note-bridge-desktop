"""BI4 native dispatch accepts only its independent proof and leaves old scopes intact."""
import importlib
import sys
from pathlib import Path

import pytest

from note_bridge.errors import BridgeError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
native = importlib.import_module('huawei_native_scope')
bi4 = importlib.import_module('huawei_bi4_readback')


def test_bi4_dispatch_reuses_the_verified_exact_pair_without_account_cache(tmp_path, monkeypatch):
    from test_huawei_bi4_readback import setup

    ctx = setup(tmp_path, monkeypatch)
    reference = bi4.capture([], tmp_path)['proof']  # Synthetic HTTP fixture only.
    monkeypatch.setattr(native, 'select_source', lambda *a: pytest.fail('BI4 must not enter the full-cache path'))
    for index in (0, 1):
        result = native.browser_target({'manifest': 'oppo-huawei-BI4-job.json', 'index': index,
                                        'readback_proof': reference}, tmp_path, ctx.account)
        assert result['scopeLabel'] == 'oppo-huawei-BI4:' + str(index)
        assert result['fixtureId'] == 'confirmed-BI4-' + str(index)
        assert result['evidence_sha256'][reference['path']] == reference['sha256']
        assert len(result['image_specs']) == 2
    assert len(ctx.calls) == 14, 'Dispatch must not repeat the successful precise readback.'


@pytest.mark.parametrize('selected', [
    {'manifest': 'oppo-huawei-BI4-job.json', 'index': 0},
    {'manifest': 'oppo-huawei-BI4-job.json', 'index': True, 'readback_proof': {}},
    {'manifest': 'oppo-huawei-BI4-job.json', 'index': 2, 'readback_proof': {}},
    {'manifest': 'oppo-huawei-BI4-job.json', 'index': 0, 'readback_proof': {}, 'armed': True},
    {'manifest': 'oppo-huawei-BI40-job.json', 'index': 0, 'readback_proof': {}},
    {'manifest': 'wps-huawei-BD20-job.json', 'index': 0, 'readback_proof': {}},
])
def test_proof_marker_cannot_disguise_an_unrelated_or_expanded_scope(tmp_path, monkeypatch, selected):
    monkeypatch.setattr(native.sqlite3, 'connect', lambda *a, **kw: pytest.fail('Invalid scope reached the database'))
    with pytest.raises((ValueError, BridgeError)):
        native.browser_target(selected, tmp_path, 'synthetic')


def test_legacy_dispatch_keeps_its_original_scope_and_handler(tmp_path, monkeypatch):
    name = next(iter(native.LEGACY_SCOPES))
    selected = {'manifest': name, 'index': 0}
    expected = object()
    calls = []
    monkeypatch.setattr(bi4, 'browser_target', lambda *a: pytest.fail('Legacy scope routed into BI4'))
    monkeypatch.setattr(native, 'legacy_target', lambda scope, root, account:
        (calls.append((scope, root, account)) or expected))
    assert native.browser_target(selected, tmp_path, 'synthetic') is expected
    assert calls == [(selected, tmp_path, 'synthetic')]

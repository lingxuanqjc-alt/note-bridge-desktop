"""A fixed capture cannot be redirected to an account-wide or caller-selected scope."""
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
discover = importlib.import_module("session_discovery").discover


@pytest.mark.parametrize("platform,module,operation", [
    ("oppo", "oppo_scoped_source", "oppo_fixed_source_capture"),
    ("vivo", "vivo_fixed_source", "vivo_fixed_source_capture"),
    ("huawei", "huawei_bi4_readback", "huawei_BI4_target_readback"),
])
def test_fixed_capture_only_passes_the_owned_session_to_the_bound_reader(tmp_path, monkeypatch, platform, module, operation):
    private = tmp_path / ".private"
    private.mkdir()
    (private / "lab-discovery.json").write_text(json.dumps({
        "platform": platform, "operation": operation}), "utf-8")
    session, calls = object(), []
    report = {"scope_complete": True, "whole_account_snapshot": False, "cloud_writes": 0}
    monkeypatch.setitem(sys.modules, module, SimpleNamespace(
        capture=lambda jars, root: calls.append((jars, root)) or report))
    assert discover(platform, session, tmp_path) is report
    assert calls == [(session, tmp_path)]


@pytest.mark.parametrize("platform,module,operation", [
    ("oppo", "oppo_scoped_source", "oppo_fixed_source_capture"),
    ("vivo", "vivo_fixed_source", "vivo_fixed_source_capture"),
    ("huawei", "huawei_bi4_readback", "huawei_BI4_target_readback"),
])
@pytest.mark.parametrize("change", [{"scope": "all"}, {"source_ids": ["private-note"]}, {"platform": "wps"}])
def test_scope_overrides_are_rejected_before_capture(tmp_path, monkeypatch, platform, module, operation, change):
    private = tmp_path / ".private"
    private.mkdir()
    job = {"platform": platform, "operation": operation, **change}
    (private / "lab-discovery.json").write_text(json.dumps(job), "utf-8")
    monkeypatch.setitem(sys.modules, module, SimpleNamespace(
        capture=lambda *args: pytest.fail("Unconfirmed scope reached a cloud reader")))
    with pytest.raises(ValueError, match="invalid_fixed_source_scope"):
        discover(job["platform"], object(), tmp_path)


def test_oppo_native_route_keeps_scope_validation_in_the_bound_runner(tmp_path, monkeypatch):
    private = tmp_path / ".private"
    private.mkdir()
    scopes = [{"manifest": "oppo-complex-OR1-job.json", "index": None}]
    (private / "lab-discovery.json").write_text(json.dumps({
        "platform": "oppo", "operation": "oppo_target_native", "fixture_scopes": scopes}), "utf-8")
    session, calls = object(), []
    monkeypatch.setitem(sys.modules, "oppo_native_run", SimpleNamespace(
        run=lambda jars, root, selected: calls.append((jars, root, selected)) or {"cloud_writes": 0}))
    assert discover("oppo", session, tmp_path) == {"cloud_writes": 0}
    assert calls == [(session, tmp_path, scopes)]


def test_oppo_native_route_passes_optional_repair_proofs_without_enabling_repair_writes(tmp_path, monkeypatch):
    private = tmp_path / '.private'
    private.mkdir()
    scopes = [{'manifest': 'oppo-complex-OR1-job.json', 'index': None}]
    refs = [{'path': '.private/evidence/oppo-markup-repair-' + 'a' * 32 + '/proof.json', 'sha256': 'b' * 64}]
    (private / 'lab-discovery.json').write_text(json.dumps({'platform': 'oppo', 'operation': 'oppo_target_native',
        'fixture_scopes': scopes, 'repair_references': refs}), 'utf-8')
    calls = []
    monkeypatch.setitem(sys.modules, 'oppo_native_run', SimpleNamespace(
        run=lambda jars, root, selected, *, repair_references: calls.append((selected, repair_references)) or {'cloud_writes': 0}))
    assert discover('oppo', object(), tmp_path) == {'cloud_writes': 0}
    assert calls == [(scopes, refs)]


@pytest.mark.parametrize("change", [{"platform": "wps"}, {"all_notes": True}, {"cookies": []}])
def test_oppo_native_route_rejects_cross_platform_and_scope_overrides(tmp_path, monkeypatch, change):
    private = tmp_path / ".private"
    private.mkdir()
    job = {"platform": "oppo", "operation": "oppo_target_native", "fixture_scopes": [], **change}
    (private / "lab-discovery.json").write_text(json.dumps(job), "utf-8")
    monkeypatch.setitem(sys.modules, "oppo_native_run", SimpleNamespace(
        run=lambda *args: pytest.fail("Unbound native request reached the worker")))
    with pytest.raises(ValueError, match="invalid_oppo_native_scope"):
        discover(job["platform"], object(), tmp_path)


def test_final_oppo_source_read_uses_the_six_batch_auditor(tmp_path, monkeypatch):
    private = tmp_path / ".private"
    private.mkdir()
    reference = {"path": ".private/evidence/BI4/proof.json", "sha256": "a" * 64}
    (private / "lab-discovery.json").write_text(json.dumps({
        "platform": "oppo", "operation": "oppo_BI2_BI12_source_post", "bi4_reference": reference}), "utf-8")
    session, calls = object(), []
    monkeypatch.setitem(sys.modules, "oppo_source_post", SimpleNamespace(
        run=lambda jars, root, proof: calls.append((jars, root, proof)) or {"cloud_writes": 0}))
    assert discover("oppo", session, tmp_path) == {"cloud_writes": 0}
    assert calls == [(session, tmp_path, reference)]


@pytest.mark.parametrize("change", [{"platform": "vivo"}, {"source_ids": ["private-note"]}, {"cookies": []}])
def test_final_oppo_read_rejects_unbound_request_before_cloud(tmp_path, monkeypatch, change):
    private = tmp_path / ".private"
    private.mkdir()
    job = {"platform": "oppo", "operation": "oppo_BI2_BI12_source_post", "bi4_reference": {}, **change}
    (private / "lab-discovery.json").write_text(json.dumps(job), "utf-8")
    monkeypatch.setitem(sys.modules, "oppo_source_post", SimpleNamespace(
        run=lambda *args: pytest.fail("Unbound source-post request reached the worker")))
    with pytest.raises(ValueError, match="invalid_oppo_source_post_scope"):
        discover(job["platform"], object(), tmp_path)

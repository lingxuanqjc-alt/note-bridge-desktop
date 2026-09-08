"""An ordinary lab command must not accidentally reacquire private Vivo notes."""
import json
import runpy
import sys
from io import StringIO
from pathlib import Path

import pytest


def test_full_vivo_lab_fetch_stops_before_account_cache_or_network_access(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("A forbidden full-account read must not create a provider or touch the lab cache")

    monkeypatch.setattr("note_bridge.providers.factory.create_provider", forbidden)
    monkeypatch.setattr("note_bridge.paths.AppPaths", forbidden)
    # No cookie field is needed: this boundary precedes credential parsing as well.
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps({"platform": "vivo", "action": "fetch"})))
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/probe-session.py"))
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "blocked" and result["code"] == "synthetic_scope_required"


@pytest.mark.parametrize("arguments", [[], ["--platform", "vivo"]])
def test_legacy_export_sweep_cannot_reopen_the_private_vivo_cache(monkeypatch, capsys, arguments):
    def forbidden(*args, **kwargs):
        raise AssertionError("An unscoped Vivo export must stop before opening the cache")

    monkeypatch.setattr("note_bridge.storage.Store", forbidden)
    script = Path(__file__).resolve().parents[1] / "scripts/verify-live-exports.py"
    monkeypatch.setattr(sys, "argv", [str(script), *arguments])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(script), run_name="__main__")
    assert stopped.value.code == 2
    assert "exact tool-fixture corpus" in capsys.readouterr().err

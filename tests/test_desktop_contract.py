"""Desktop lifecycle checks protect memory sessions and user data during upgrades."""

import json
import os
import sqlite3
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge import __release_channel__, __version__
from note_bridge.bridge import Bridge
from note_bridge.errors import BridgeError
from note_bridge.instance import InstanceLock
from note_bridge.models import PlatformId, TaskStatus
from note_bridge.paths import AppPaths
from note_bridge.providers.base import PendingProvider
from note_bridge.providers.factory import create_provider
from note_bridge.storage import SCHEMA_VERSION, Store
from note_bridge.tasks import TaskRunner


@pytest.mark.parametrize("platform", list(PlatformId))
def test_seven_readers_keep_seven_controlled_write_targets(platform, tmp_path):
    provider = create_provider(platform, [], tmp_path)
    try:
        assert provider.spec.id == platform and provider.read_supported
        assert not isinstance(provider, PendingProvider)
        enabled = platform in {PlatformId.XIAOMI, PlatformId.VIVO, PlatformId.HONOR,
                               PlatformId.MEIZU, PlatformId.WPS, PlatformId.HUAWEI, PlatformId.OPPO}
        assert provider.write_supported is enabled and provider.images_supported is enabled
    finally:
        provider.close()
    assert provider.account_id is None


def test_stable_channel_does_not_rewrite_historical_full_acceptance(tmp_path):
    paths = AppPaths(tmp_path)
    paths.prepare()
    state = Bridge(paths).get_app_state()["data"]
    assert state["version"] == __version__ == "1.0.0"
    assert state["release_channel"] == __release_channel__ == "stable"
    assert state["release_ready"] is False


def test_python_and_frontend_locks_identify_the_same_release():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
    locked = tomllib.loads((root / "uv.lock").read_text("utf-8"))
    python_lock = next(package for package in locked["package"] if package["name"] == "note-bridge-desktop")
    frontend = json.loads((root / "ui/package.json").read_text("utf-8"))
    frontend_lock = json.loads((root / "ui/package-lock.json").read_text("utf-8"))
    assert {project["project"]["version"], python_lock["version"], frontend["version"],
            frontend_lock["version"], frontend_lock["packages"][""]["version"]} == {__version__}


@pytest.mark.parametrize("failure", [RuntimeError("synthetic-private-response"), BridgeError("network_error", "网络不可用")])
def test_failed_account_check_closes_the_temporary_session(monkeypatch, tmp_path, failure):
    paths = AppPaths(tmp_path)
    paths.prepare()
    bridge = Bridge(paths)
    closed = []

    def probe():
        raise failure

    provider = SimpleNamespace(probe=probe, close=lambda: closed.append(True))
    bridge._login_windows[PlatformId.VIVO] = SimpleNamespace(
        capture_session=lambda: []
    )
    monkeypatch.setattr("note_bridge.bridge.create_provider", lambda *args: provider)
    result = bridge.complete_login("vivo")
    assert not result["ok"] and closed == [True] and not bridge._checking
    assert "synthetic-private-response" not in json.dumps(result)
    assert not bridge.get_app_state()["data"]["platforms"][2]["logged_in"]


def test_late_account_check_cannot_restore_a_session_after_shutdown(monkeypatch, tmp_path):
    paths = AppPaths(tmp_path)
    paths.prepare()
    bridge = Bridge(paths)
    closed = []

    def probe():
        bridge._shutdown()
        return "synthetic-account"

    provider = SimpleNamespace(probe=probe, close=lambda: closed.append(True))
    bridge._login_windows[PlatformId.VIVO] = SimpleNamespace(
        capture_session=lambda: [], hide=lambda: None, destroy=lambda: None, closed=False
    )
    monkeypatch.setattr("note_bridge.bridge.create_provider", lambda *args: provider)
    result = bridge.complete_login("vivo")
    assert result["error"]["code"] == "application_closed" and closed == [True]


def test_older_app_does_not_downgrade_a_newer_database(tmp_path):
    path = tmp_path / "state.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(f"PRAGMA user_version={SCHEMA_VERSION + 1}; CREATE TABLE future_data(value TEXT); INSERT INTO future_data VALUES('keep');")
    with pytest.raises(BridgeError) as error:
        Store(path)
    assert error.value.code == "newer_database"
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
        assert db.execute("SELECT value FROM future_data").fetchone()[0] == "keep"


def test_unexpected_error_retains_the_status_of_already_committed_work(tmp_path):
    runner = TaskRunner(Store(tmp_path / "state.sqlite"))

    def work(context):
        context.update(succeeded=1, completed=1, total=2)
        raise RuntimeError("synthetic-private-response")

    runner.start("export", work)
    runner.join(3)
    report = runner.current()
    assert report.status == TaskStatus.PARTIAL and report.succeeded == 1
    assert "synthetic-private-response" not in report.model_dump_json()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows mutex")
def test_second_instance_cannot_share_data_until_every_handle_is_closed(tmp_path):
    first, second, separate = InstanceLock(tmp_path), InstanceLock(tmp_path), InstanceLock(tmp_path / "other")
    try:
        assert not first.already_running and second.already_running and not separate.already_running
    finally:
        first.close()
        second.close()
        separate.close()
    reopened = InstanceLock(tmp_path)
    try:
        assert not reopened.already_running, "Closing the app must allow a later launch on the same cache."
    finally:
        reopened.close()

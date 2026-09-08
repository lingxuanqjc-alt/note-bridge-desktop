import json
import runpy
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from note_bridge.errors import BridgeError
from note_bridge.providers.huawei_transport import HuaweiTransport

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from huawei_session_diagnostics import observe_huawei_session, redirect_kind


def channel(monkeypatch, responses):
    session = requests.Session()
    session.cookies.set("shareToken", "synthetic-initial", domain=".cloud.huawei.com", path="/", secure=True)

    def send(request, **kwargs):
        status, token = responses.pop(0)
        response = requests.Response()
        response.status_code, response.request = status, request
        response._content, response._content_consumed = b"{}", True
        if token:
            response.headers["CSRFToken"] = token
        return response

    monkeypatch.setattr(session, "send", send)
    return HuaweiTransport("https://cloud.huawei.com", ("huawei.com",), session=session)


def test_observer_records_actual_change_without_serializing_values_or_hashes(monkeypatch):
    transport = channel(monkeypatch, [(200, "synthetic/+=next"), (200, "synthetic/+=next"), (200, "synthetic/final")])
    original = transport.session.request
    with observe_huawei_session(transport) as events:
        for _ in range(3):
            transport.json("POST", "/proxyserver/driveFileProxy/preProcess")
    assert transport.session.request == original
    assert len(events) == 3 and all(event["header_matches_share_cookie"] for event in events)
    assert [event["actual_header_changed"] for event in events] == [None, True, False]
    assert [event["response_csrf_changed"] for event in events] == [None, False, True]
    assert all(event["response_needs_uri_encoding"] for event in events)
    serialized = json.dumps(events)
    assert "synthetic" not in serialized and "sha256" not in serialized and "%2F" not in serialized
    transport.close()


@pytest.mark.parametrize("status", [401, 302])
def test_observer_keeps_exact_auth_status_without_retry_or_changing_classification(monkeypatch, status):
    transport = channel(monkeypatch, [(status, None)])
    original = transport.session.request
    with observe_huawei_session(transport) as events:
        with pytest.raises(BridgeError) as error:
            transport.json("POST", "/proxyserver/driveFileProxy/preProcess")
    assert error.value.code == "session_expired" and transport.session.request == original
    assert len(events) == 1 and events[0]["http_status"] == status
    transport.close()


@pytest.mark.parametrize("status,location,known,expected", [
    (200, "https://example.invalid/private", (), "none"),
    (302, None, (), "none"),
    (302, "/private?token=synthetic-private", (), "samehost"),
    (303, "https://regional.huawei.com/private", (), "official_differenthost"),
    (302, "https://regional.huaweicloud.com/private", (), "official_differenthost"),
    (302, "https://login-test.huawei.com/private", ("login-test.huawei.com",), "known_login_host"),
    (302, "https://huawei.com.example.invalid/private", (), "external"),
    (302, "javascript:synthetic-private", (), "external"),
    (302, "https://[broken", (), "external"),
])
def test_redirect_summary_never_returns_an_address_or_guesses_login_hosts(status, location, known, expected):
    response = requests.Response()
    response.status_code = status
    if location:
        response.headers["Location"] = location
    assert redirect_kind("https://cloud.huawei.com/html/getHomeData", response, known) == expected


def test_diagnostic_request_shape_only_reports_presence_and_retains_observer_defaults(monkeypatch):
    transport = channel(monkeypatch, [(200, None), (200, None)])
    header_values = {name: "synthetic-private-" + str(index) for index, name in enumerate((
        "x-hw-device-category", "x-hw-device-type", "x-hw-client-mode", "x-hw-os-brand"))}
    with observe_huawei_session(transport, request_shape=True) as detailed:
        transport.json("POST", "/html/getCommonParam", headers=header_values)
    with observe_huawei_session(transport) as original:
        transport.json("POST", "/html/getHomeData")
    assert detailed[0]["device_headers_present"] == dict.fromkeys(header_values, True)
    assert detailed[0]["redirect_kind"] == "none"
    assert "device_headers_present" not in original[0] and "redirect_kind" not in original[0]
    assert "synthetic-private" not in json.dumps(detailed + original)
    transport.close()


@pytest.mark.parametrize("status", [200, 401, 302])
def test_normal_fetch_records_initial_probe_failure_and_retains_success_scope(monkeypatch, tmp_path, capsys, status):
    import fetch_scope
    import huawei_session_diagnostics
    import lab_platform_lock

    transport = channel(monkeypatch, [(status, None)] + ([(200, "synthetic/+=next")] if status == 200 else []))
    original = transport.session.request
    provider = SimpleNamespace(transport=transport, account_id="synthetic-account", close=transport.close,
                               probe=lambda: transport.json("POST", "/notepad/simplenote/query"))
    monkeypatch.setattr("note_bridge.providers.factory.create_provider", lambda *args: provider)
    paths = SimpleNamespace(resources=tmp_path / "resources", database=tmp_path / "notes.sqlite", prepare=lambda: None)
    monkeypatch.setattr("note_bridge.paths.AppPaths", lambda root: paths)
    monkeypatch.setattr("lab_store.LabStore", lambda path: SimpleNamespace(snapshot=lambda *args: {"complete": True}))
    locks = lab_platform_lock.PlatformLocks
    monkeypatch.setattr(lab_platform_lock, "PlatformLocks", lambda root, platforms: locks(tmp_path, platforms))
    report = SimpleNamespace(status="succeeded", id="synthetic-task", completed=1, total=1, succeeded=1, issues=[])
    runner = SimpleNamespace(start=lambda kind, callback: callback(None), join=lambda: None, current=lambda: report)
    monkeypatch.setattr("note_bridge.tasks.TaskRunner", lambda store: runner)
    monkeypatch.setattr("note_bridge.operations.fetch_snapshot",
                        lambda provider, store, context: transport.json("POST", "/notepad/note/query"))
    monkeypatch.setattr(fetch_scope, "write_fetch_scope", lambda *args: ".private/evidence/synthetic-scope.json")
    record = huawei_session_diagnostics.record_huawei_fetch
    monkeypatch.setattr(huawei_session_diagnostics, "record_huawei_fetch",
                        lambda transport, root, summary: record(transport, tmp_path, summary))
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps({"platform": "huawei", "action": "fetch", "cookies": []})))

    runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/probe-session.py"))

    output = capsys.readouterr().out
    result = json.loads(output)
    evidence = tmp_path / result["session_evidence"]
    events = json.loads(evidence.read_text("utf-8"))["events"]
    assert evidence.name.startswith("huawei-session-fetch-")
    assert result["session_event_count"] == len(events) == (2 if status == 200 else 1)
    assert events[0]["http_status"] == status
    assert transport.session.request == original and not list(transport.session.cookies)
    assert "events" not in result and "synthetic/+=next" not in output + evidence.read_text("utf-8")
    if status == 200:
        assert result["task_id"] == report.id and result["scope_file"] == ".private/evidence/synthetic-scope.json"
        assert result["status"] == "succeeded"
    else:
        assert result["status"] == "blocked" and result["code"] == "session_expired"
        assert "task_id" not in result and "scope_file" not in result

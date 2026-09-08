"""Diagnose missing cookie identity without guessing an account or writing to Huawei."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from note_bridge.errors import BridgeError
from note_bridge.providers.huawei import HuaweiProvider
from note_bridge.providers.huawei_transport import HuaweiTransport

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from session_discovery import discover


def run(monkeypatch, tmp_path, replies, cookies=()):
    private = tmp_path / ".private"
    private.mkdir()
    job = private / "lab-discovery.json"
    job.write_text(json.dumps({"platform": "huawei", "operation": "huawei_identity_bootstrap"}), "utf-8")
    calls, closed = [], []
    pending = iter(replies)

    def request(method, endpoint, **kwargs):
        calls.append(endpoint)
        assert method == "POST" and kwargs["write"] is False
        assert endpoint in ("/html/getCommonParam", "/html/getHomeData")
        assert set(kwargs["json"]) == {"traceId"}
        assert "userId" not in kwargs["headers"], "Bootstrap cannot require the identity being diagnosed."
        value = next(pending)
        if isinstance(value, Exception):
            raise value
        return value

    provider = SimpleNamespace(account_id=None,
        transport=SimpleNamespace(json=request, session=SimpleNamespace(cookies=list(cookies))),
        _files=SimpleNamespace(headers=lambda: {"x-hw-trace-id": "synthetic-trace", "CSRFToken": "synthetic-private-header"}),
        probe=lambda: pytest.fail("A cookie-based probe must not block bootstrap discovery"),
        create=lambda *a: pytest.fail("Discovery cannot create notes"), close=lambda: closed.append(True))
    monkeypatch.setattr("note_bridge.providers.factory.create_provider", lambda *a: provider)
    before = job.read_bytes()
    report = discover("huawei", [], tmp_path)
    assert provider.account_id is None and closed == [True]
    assert report["account_identity_assigned"] is report["formal_acceptance"] is False
    assert report["cloud_writes"] == 0 and job.read_bytes() == before
    assert list(private.iterdir()) == [job], "Discovery must not save response data or open a cache database."
    serialized = json.dumps(report)
    assert "synthetic-private" not in serialized and "secret-body" not in serialized
    return report, calls


def payload(identity="synthetic-private-identity", **changes):
    return {"code": 0, "isLogin": 1, "userid": identity, "ignored": "secret-body", **changes}


def test_server_identity_can_be_observed_without_any_user_id_cookie(monkeypatch, tmp_path):
    report, calls = run(monkeypatch, tmp_path, [payload(), payload()])
    assert calls == ["/html/getCommonParam", "/html/getHomeData"]
    assert report["cookie_scope"] == {"identity_cookie_count": 0, "distinct_identity_count": 0, "case_variant_count": 0}
    assert report["response_userids_equal"] is True
    assert all(row["userid_present"] and row["code"] == 0 and row["isLogin"] == 1 for row in report["results"])
    assert all(row["matches_unique_identity_cookie"] is None for row in report["results"])


@pytest.mark.parametrize("first,second,equal", [
    (None, "synthetic-private-identity", None),
    ("", "", None),
    ("synthetic-private-first", "synthetic-private-second", False),
    (123, 123, None),
])
def test_unknown_or_changed_response_identity_is_not_promoted_to_an_account(monkeypatch, tmp_path, first, second, equal):
    report, _ = run(monkeypatch, tmp_path, [payload(first), payload(second)])
    assert report["response_userids_equal"] is equal
    if equal is None:
        assert not report["results"][0]["userid_present"]


@pytest.mark.parametrize("count", [1, 2])
def test_duplicate_cookie_values_and_case_variants_are_reported_without_values(monkeypatch, tmp_path, count):
    cookies = [SimpleNamespace(name="userId", value="synthetic-private-identity")]
    if count == 2:
        cookies.append(SimpleNamespace(name="userId", value="synthetic-private-conflict"))
    cookies.append(SimpleNamespace(name="userid", value="synthetic-private-wrong-case"))
    report, _ = run(monkeypatch, tmp_path, [payload(), payload()], cookies)
    assert report["cookie_scope"] == {"identity_cookie_count": count, "distinct_identity_count": count, "case_variant_count": 1}
    assert all(row["matches_unique_identity_cookie"] is (True if count == 1 else None) for row in report["results"])


@pytest.mark.parametrize("failure,code", [(BridgeError("session_expired", "secret-body"), "session_expired"),
    (BridgeError("synthetic-private-code", "secret-body"), "request_failed"),
    (RuntimeError("synthetic-private-header secret-body"), "internal_error")])
def test_failed_first_request_stops_without_relogin_extra_reads_or_leaking_exception(monkeypatch, tmp_path, failure, code):
    report, calls = run(monkeypatch, tmp_path, [failure])
    assert calls == ["/html/getCommonParam"] and report["results"] == [{"endpoint": calls[0], "error": code}]
    assert report["response_userids_equal"] is None


def test_arbitrary_code_and_login_fields_are_not_copied_out_of_response(monkeypatch, tmp_path):
    report, _ = run(monkeypatch, tmp_path,
                    [payload(code="synthetic-private-code", isLogin="secret-body"), payload(code=False, isLogin=True)])
    assert all(row["code"] is None and row["isLogin"] is None for row in report["results"])


def test_explicit_diagnostics_distinguish_redirect_from_401_without_extra_requests_or_values(monkeypatch, tmp_path):
    private = tmp_path / ".private"
    private.mkdir()
    (private / "lab-discovery.json").write_text(json.dumps({"platform": "huawei",
        "operation": "huawei_identity_bootstrap", "diagnostics": True}), "utf-8")
    session, sent = requests.Session(), []
    session.cookies.set("shareToken", "synthetic-private-token", domain=".cloud.huawei.com", path="/", secure=True)

    def send(request, **kwargs):
        assert request.method == "POST" and kwargs["allow_redirects"] is False
        assert request.path_url in ("/html/getCommonParam", "/html/getHomeData")
        sent.append(request)
        response = requests.Response()
        response.request = request
        response._content = b'{"code":0,"ignored":"secret-body"}'
        response._content_consumed = True
        response.status_code = 200 if len(sent) == 1 else 302
        if len(sent) == 1:
            response.headers["CSRFToken"] = "synthetic-private/rotated"
        else:
            response.headers["Location"] = "https://regional.huawei.com/private?token=synthetic-private"
        return response

    monkeypatch.setattr(session, "send", send)
    transport = HuaweiTransport("https://cloud.huawei.com", ("huawei.com",), session=session)
    provider = HuaweiProvider(transport, tmp_path)
    original = session.request
    monkeypatch.setattr("note_bridge.providers.factory.create_provider", lambda *a: provider)
    report = discover("huawei", [], tmp_path)
    assert len(sent) == 2 and report["results"][1]["error"] == "session_expired"
    assert [row["http_status"] for row in report["events"]] == [200, 302]
    assert [row["redirect_kind"] for row in report["events"]] == ["none", "official_differenthost"]
    assert all(row["header_matches_share_cookie"] for row in report["events"])
    assert all(not any(row["device_headers_present"].values()) for row in report["events"])
    assert session.request == original and len(session.cookies) == 0 and provider.account_id is None
    assert "synthetic-private" not in json.dumps(report) and "secret-body" not in json.dumps(report)
    assert "regional.huawei.com" not in json.dumps(report) and "https://" not in json.dumps(report)

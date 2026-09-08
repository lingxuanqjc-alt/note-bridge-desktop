import time
from urllib.parse import quote

import pytest
import requests

from note_bridge.errors import BridgeError
from note_bridge.providers.huawei_transport import HuaweiTransport


@pytest.fixture
def channel(monkeypatch):
    session = requests.Session()
    sent, replies = [], []

    def request(method, url, **options):
        sent.append({"method": method, "url": url, **options})
        response = requests.Response()
        response.status_code, headers = replies.pop(0) if replies else (200, {})
        response.headers.update(headers)
        response._content = b"{}"
        response._content_consumed = True
        return response

    monkeypatch.setattr(session, "request", request)
    transport = HuaweiTransport("https://cloud.huawei.com", ("huawei.com",), session=session)
    try:
        yield transport, sent, replies
    finally:
        transport.close()


def share_cookie(transport, value="synthetic-browser", *, domain=".cloud.huawei.com", path="/", **kwargs):
    transport.session.cookies.set_cookie(requests.cookies.create_cookie(
        "shareToken", value, domain=domain, path=path, secure=True, **kwargs
    ))


def test_first_request_uses_the_token_selected_by_the_official_browser(channel):
    transport, sent, _ = channel
    share_cookie(transport)
    headers = {"CSRFToken": "synthetic-legacy", "Origin": transport.origin}
    transport.request("POST", "/notepad/notetag/query", headers=headers)
    assert sent[0]["headers"]["CSRFToken"] == "synthetic-browser", (
        "A newly imported official login must work before any response has initialized rotation."
    )
    assert headers["CSRFToken"] == "synthetic-legacy", "Request preparation must not mutate caller headers."


def test_response_rotation_takes_precedence_over_browser_and_legacy_tokens(channel):
    transport, sent, replies = channel
    share_cookie(transport)
    replies.append((200, {"CSRFToken": "synthetic-rotated"}))
    transport.request("POST", "/notepad/notetag/query")
    share_cookie(transport, "synthetic-other-browser")
    transport.request("POST", "/notepad/simplenote/query", headers={"CSRFToken": "synthetic-legacy"})
    assert [request["headers"]["CSRFToken"] for request in sent] == [
        "synthetic-browser", "synthetic-rotated"
    ], "The next operation must retain the server's newest response token."


def test_browser_cookie_is_reread_until_a_response_token_is_observed(channel):
    transport, sent, _ = channel
    share_cookie(transport)
    transport.request("POST", "/notepad/notetag/query")
    share_cookie(transport, "synthetic-current")
    transport.request("POST", "/notepad/simplenote/query")
    assert sent[-1]["headers"]["CSRFToken"] == "synthetic-current"


@pytest.mark.parametrize("scope", [
    {"domain": "account.huawei.com"},
    {"domain": ".account.huawei.com"},
    {"domain": "cloud.huawei.com.attacker.invalid"},
    {"path": "/account"},
    {"path": "/notepad-extra"},
    {"expires": 1},
])
def test_unrelated_or_expired_browser_cookie_cannot_be_promoted_to_a_header(channel, scope):
    transport, sent, _ = channel
    share_cookie(transport, "synthetic-out-of-scope", **scope)
    transport.request("POST", "/notepad/notetag/query", headers={"CSRFToken": "synthetic-legacy"})
    assert sent[0]["headers"]["CSRFToken"] == "synthetic-legacy", (
        "A cookie that the target URL cannot use must never escape through the CSRF header."
    )


@pytest.mark.parametrize("domain,path", [
    (".huawei.com", "/"),
    ("cloud.huawei.com", "/notepad"),
    (".cloud.huawei.com", "/notepad/"),
    (".cloud.huawei.com", "/notepad/notetag/query"),
])
def test_applicable_domain_and_path_cookies_can_initialize_the_header(channel, domain, path):
    transport, sent, _ = channel
    share_cookie(transport, domain=domain, path=path, expires=int(time.time()) + 300)
    transport.request("POST", "/notepad/notetag/query?index=0")
    assert sent[0]["headers"]["CSRFToken"] == "synthetic-browser"


def test_ambiguous_browser_tokens_preserve_the_existing_header(channel):
    transport, sent, _ = channel
    share_cookie(transport, "synthetic-root")
    share_cookie(transport, "synthetic-scoped", path="/notepad")
    transport.request("POST", "/notepad/notetag/query", headers={"CSRFToken": "synthetic-legacy"})
    assert sent[0]["headers"]["CSRFToken"] == "synthetic-legacy", (
        "Distinct applicable tokens must not be resolved by arbitrary cookie iteration order."
    )


def test_close_clears_browser_and_response_tokens(channel):
    transport, _, replies = channel
    share_cookie(transport)
    replies.append((200, {"CSRFToken": "synthetic-rotated"}))
    transport.request("POST", "/notepad/notetag/query")
    transport.close()
    assert transport._csrf is None and len(transport.session.cookies) == 0


@pytest.mark.parametrize("path", ["https://account.huawei.com/", "//account.huawei.com/"])
def test_rotated_token_cannot_be_sent_to_a_different_host(channel, path):
    transport, sent, replies = channel
    replies.append((200, {"CSRFToken": "synthetic-rotated"}))
    transport.request("POST", "/notepad/notetag/query")
    with pytest.raises(BridgeError) as error:
        transport.request("POST", path)
    assert error.value.code == "unsafe_url" and len(sent) == 1


def test_authentication_rejection_still_stops_without_retry(channel):
    transport, sent, replies = channel
    share_cookie(transport)
    replies.append((401, {"CSRFToken": "synthetic-rejected"}))
    with pytest.raises(BridgeError) as error:
        transport.request("POST", "/notepad/notetag/query")
    assert error.value.code == "session_expired" and len(sent) == 1 and transport._csrf is None


@pytest.mark.parametrize("domain", [".cloud.huawei.com", "cloud.huawei.com", ".huawei.com"])
def test_rotated_response_matches_the_official_encoded_cookie_on_actual_wire(monkeypatch, domain):
    session = requests.Session()
    sent = []
    raw = "synthetic/+=%rotation"
    encoded = quote(raw, safe="~()*!.'-_")

    def send(request, **kwargs):
        sent.append(request)
        response = requests.Response()
        response.status_code, response.request = 200, request
        response._content, response._content_consumed = b"{}", True
        if len(sent) == 1:
            response.headers["CSRFToken"] = raw
        return response

    monkeypatch.setattr(session, "send", send)
    transport = HuaweiTransport("https://cloud.huawei.com", ("huawei.com",), session=session)
    share_cookie(transport, domain=domain)
    share_cookie(transport, "synthetic-other-host", domain="account.huawei.com")
    share_cookie(transport, "synthetic-other-path", path="/account")
    try:
        transport.json("POST", "/notepad/note/query")
        transport.json("POST", "/proxyserver/driveFileProxy/preProcess")
        assert sent[1].headers["CSRFToken"] == encoded != raw
        assert sent[1].headers["Cookie"] == "shareToken=" + encoded, (
            "The official getter does not decode: the next actual Cookie and CSRF header must agree."
        )
        assert len(session.cookies) == 3, "Rotation must replace the imported key instead of adding a second root cookie."
        assert session.cookies.get("shareToken", domain="account.huawei.com", path="/") == "synthetic-other-host"
        assert session.cookies.get("shareToken", domain=".cloud.huawei.com", path="/account") == "synthetic-other-path"
    finally:
        transport.close()
    assert transport._csrf is None and len(session.cookies) == 0


def test_cookie_only_server_rotation_is_not_overwritten_by_an_older_response_header(channel, monkeypatch):
    transport, sent, replies = channel
    share_cookie(transport)
    replies.append((200, {"CSRFToken": "synthetic/first"}))
    transport.json("POST", "/notepad/note/query")
    original = transport.session.request

    def rotate(*args, **kwargs):
        response = original(*args, **kwargs)
        share_cookie(transport, "synthetic%2Fnew-cookie")
        return response

    monkeypatch.setattr(transport.session, "request", rotate)
    transport.json("POST", "/proxyserver/driveFileProxy/preProcess")
    monkeypatch.setattr(transport.session, "request", original)
    transport.json("POST", "/notepad/note/query")
    assert sent[-1]["headers"]["CSRFToken"] == "synthetic%2Fnew-cookie", (
        "A server cookie is already encoded; neither revert nor encode it twice."
    )


def test_blob_download_does_not_inherit_the_axios_csrf_interceptors(channel):
    transport, sent, replies = channel
    share_cookie(transport)
    replies.extend([(200, {"CSRFToken": "synthetic/known"}), (200, {"CSRFToken": "synthetic/blob-only"})])
    transport.json("POST", "/proxyserver/driveFileProxy/preProcess")
    transport.request("GET", "/proxy/v1/download/synthetic", stream=True,
                      headers={"CSRFToken": "synthetic-legacy", "version": "fixture-version"})
    transport.json("POST", "/notepad/note/query")
    assert "CSRFToken" not in sent[1]["headers"] and sent[1]["headers"]["version"] == "fixture-version"
    assert sent[2]["headers"]["CSRFToken"] == "synthetic%2Fknown", (
        "The official bare-XHR loader does not promote a download header into the next JSON request."
    )


def test_scoped_rotation_keeps_unrelated_cookie_paths_unchanged(channel):
    transport, _, replies = channel
    share_cookie(transport, "synthetic-notepad", path="/notepad")
    share_cookie(transport, "synthetic-account", path="/account")
    replies.append((200, {"CSRFToken": "synthetic/new"}))
    transport.request("POST", "/notepad/note/query")
    assert transport.session.cookies.get("shareToken", domain=".cloud.huawei.com", path="/notepad") == "synthetic%2Fnew"
    assert transport.session.cookies.get("shareToken", domain=".cloud.huawei.com", path="/account") == "synthetic-account"
    assert len(transport.session.cookies) == 2

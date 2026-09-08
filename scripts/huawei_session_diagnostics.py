"""Boolean-only observation around an already authorized Huawei request sequence."""
import json
import re
from contextlib import contextmanager
from time import monotonic
from urllib.parse import quote, urljoin, urlsplit
from uuid import uuid4

import requests

from note_bridge.providers.transport import host_matches


@contextmanager
def record_huawei_fetch(transport, root, summary):
    """Persist only safe observations, including an unsuccessful initial probe."""
    with observe_huawei_session(transport) as events:
        try:
            yield
        finally:
            relative = f".private/evidence/huawei-session-fetch-{uuid4().hex}.json"
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as output:
                json.dump({"kind": "huawei_session_fetch", "formal_acceptance": False,
                           "events": events}, output, ensure_ascii=True, indent=2)
            summary.update(session_evidence=relative, session_event_count=len(events))


def request_kind(path):
    if path.startswith("/proxy/v1/download/"):
        return "attachment_download"
    if path.startswith("/proxy/v1/upload/"):
        return "attachment_upload"
    known = {"/notepad/notetag/query", "/notepad/simplenote/query", "/notepad/note/query",
             "/notepad/note/create", "/notepad/note/update", "/html/getCommonParam", "/html/getHomeData",
             "/html/getAboutGet", "/proxyserver/driveFileProxy/preProcess",
             "/driveFileProxy/preUploadAttachmentProcess", "/driveFileProxy/afterUploadAttachmentProcess"}
    return path if path in known else "other"


def redirect_kind(request_url, response, known_login_hosts=()):
    if not 300 <= response.status_code < 400 or not response.headers.get("Location"):
        return "none"
    try:
        destination = urlsplit(urljoin(request_url, response.headers["Location"]))
        host = destination.hostname or ""
        if destination.scheme not in ("http", "https"):
            return "external"
        official = host_matches(host, ("huawei.com", "hicloud.com", "huaweicloud.com"))
        # No guessed login domains. Callers may supply independently confirmed hosts.
        if official and host in known_login_hosts:
            return "known_login_host"
        if host == urlsplit(request_url).hostname:
            return "samehost"
        return "official_differenthost" if official else "external"
    except ValueError:
        return "external"


@contextmanager
def observe_huawei_session(transport, *, request_shape=False, known_login_hosts=()):
    """Return safe events without files, token hashes, retries, or extra requests."""
    session, events = transport.session, []
    original = session.request
    previous_header = previous_response = None
    started = monotonic()

    def observe(method, url, **kwargs):
        nonlocal previous_header, previous_response
        path = urlsplit(url).path
        before_cookie = transport._share_token(path)
        event = {"request_kind": request_kind(path), "sequence": len(events) + 1}
        try:
            response = original(method, url, **kwargs)
        except requests.RequestException:
            events.append({**event, "network_error": True})
            raise
        prepared = getattr(response, "request", None)
        headers = getattr(prepared, "headers", {})
        header = headers.get("CSRFToken")
        values = re.findall(r"(?:^|;\s*)shareToken=([^;]*)", headers.get("Cookie", ""))
        response_token = response.headers.get("CSRFToken")
        after_cookie = transport._share_token(path)
        event.update(http_status=response.status_code, elapsed_ms=int((monotonic() - started) * 1000),
                     prepared_request_available=prepared is not None,
                     csrf_header_present=bool(header), share_cookie_count=len(values),
                     header_matches_share_cookie=bool(header) and all(value == header for value in values) if values else None,
                     actual_header_changed=header != previous_header if events else None,
                     response_csrf_present=bool(response_token),
                     response_csrf_changed=response_token != previous_response if response_token and previous_response else None,
                     response_needs_uri_encoding=quote(response_token, safe="~()*!.'-_") != response_token if response_token else None,
                     response_sets_share_cookie=any(cookie.name == "shareToken" for cookie in response.cookies),
                     share_cookie_changed_by_response=before_cookie != after_cookie)
        if request_shape:
            event["redirect_kind"] = redirect_kind(url, response, known_login_hosts)
            event["device_headers_present"] = {name: bool(headers.get(name)) for name in (
                "x-hw-device-category", "x-hw-device-type", "x-hw-client-mode", "x-hw-os-brand")}
        events.append(event)
        previous_header = header
        if response_token:
            previous_response = response_token
        return response

    session.request = observe
    try:
        yield events
    finally:
        if session.request is observe:
            session.request = original
        previous_header = previous_response = None

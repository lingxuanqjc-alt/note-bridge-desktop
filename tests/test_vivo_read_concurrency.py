"""Two independent body reads must preserve coverage, order and task ownership."""

import base64
import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest
import requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

from note_bridge.errors import BridgeError, Cancelled
from note_bridge.models import Block, NoteDocument, PlatformId, Span, TaskReport, account_fingerprint
from note_bridge.operations import fetch_snapshot
from note_bridge.providers import vivo
from note_bridge.providers.transport import Transport
from note_bridge.providers.vivo_wire import VivoWire
from note_bridge.storage import Store
from note_bridge.tasks import TaskContext, now


@pytest.fixture(scope="module")
def server_key():
    return RSA.generate(2048)


class Endpoint:
    """Synthetic HTTP endpoint; exercises real Transport retries and wire envelopes."""

    def __init__(self, server_key, count):
        self.server_key = server_key
        self.rows = [dict(guid=str(i), userId="fixture-user", type=1, deleted=1) for i in range(count)]
        self.finished = {str(i): threading.Event() for i in range(count)}
        self.lock = threading.Lock()
        self.active = Counter()
        self.maximum = 0
        self.body_sessions = set()
        self.events = []
        self.attempts = Counter()
        self.closed = []
        self.body = lambda guid: (200, f"<p>正文 {guid}</p>")
        self.main_thread = None

    def request(self, session, method, url, **kwargs):
        path = urlsplit(url).path
        data, envelope = kwargs.get("json", {}), None
        if "jvq_param" in data:
            raw = base64.urlsafe_b64decode(data["jvq_param"] + "==")
            size, token_size = int.from_bytes(raw[:2], "big"), raw[17]
            iv = raw[18 + token_size:34 + token_size]
            key = PKCS1_v1_5.new(self.server_key).decrypt(raw[36 + token_size:size], b"invalid")
            data = json.loads(unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(raw[size:]), 16))
            envelope = raw[:size], key, iv
        guid = data.get("guid")
        is_body = path.endswith("/getContent/v2")
        with self.lock:
            self.active[session] += 1
            self.maximum = max(self.maximum, sum(self.active.values()))
            self.events.append(("start", path, guid))
            if is_body:
                self.body_sessions.add(session)
                self.attempts[guid] += 1
            else:
                assert threading.get_ident() == self.main_thread
                assert not any(self.active[s] for s in self.body_sessions)
        try:
            status = 200
            if path.endswith("/statistics/note"):
                value = {"totalNotes": len(self.rows), "encryptedNotes": 0}
            elif path.endswith("/noteBook/getList"):
                value = []
            elif path.endswith("/getAllNote/v2"):
                value = {"notes": self.rows}
            elif is_body:
                status, value = self.body(guid)
            elif path.endswith("/getIncludeItem/v2"):
                value = {"guid": guid, "resources": [{
                    "guid": "asset-" + guid, "userId": "fixture-user", "noteGuid": guid,
                    "name": "fixture.png", "mime": "image/png", "resourceSize": 4,
                    "domainAddr": "https://clouddisk-fixture.vivo.com.cn", "resourceKey": "key-" + guid,
                }]}
            elif path.endswith("/getStsToken.do"):
                value = {"stsToken": "synthetic-scoped-token"}
            elif "/file/webdisk/source/" in path:
                assert not session.cookies and "token" not in session.headers
                response = requests.Response()
                response.status_code, response._content = 200, b"data"
                response._content_consumed = True
                return response
            else:
                pytest.fail("Unexpected synthetic endpoint: " + path)
            payload = json.dumps({"code": 0, "data": value}, ensure_ascii=False).encode()
            if envelope:
                header, key, iv = envelope
                payload = json.dumps({"jvq_response": base64.urlsafe_b64encode(
                    header + AES.new(key, AES.MODE_CBC, iv).encrypt(pad(payload, 16))
                ).decode()}).encode()
            response = requests.Response()
            response.status_code, response._content = status, payload
            response._content_consumed = True
            return response
        finally:
            with self.lock:
                self.active[session] -= 1
                self.events.append(("end", path, guid))
            if is_body:
                self.finished[guid].set()


def setup_fetch(tmp_path, monkeypatch, server_key, count=5):
    endpoint = Endpoint(server_key, count)
    wires = []

    def wire():
        result = VivoWire(server_key.public_key().export_key())
        wires.append(result)
        return result

    close_session = requests.Session.close

    def close(session):
        assert endpoint.active[session] == 0, "An in-flight request must finish before its session closes."
        endpoint.closed.append(session)
        close_session(session)

    monkeypatch.setattr(vivo, "VivoWire", wire)
    monkeypatch.setattr(requests.Session, "request", lambda session, *a, **kw: endpoint.request(session, *a, **kw))
    monkeypatch.setattr(requests.Session, "close", close)
    transport = Transport("https://pc.vivo.com.cn", ("vivo.com.cn",))
    transport.session.headers.update(token="synthetic-token", openId="synthetic-openid")
    transport.session.cookies.set("fixture", "synthetic-cookie", domain=".vivo.com.cn", path="/note-api", secure=True)
    provider = vivo.VivoProvider(transport, tmp_path / "resources")
    provider.account_id = account_fingerprint(PlatformId.VIVO, "fixture-user")
    provider.probe = lambda: provider.account_id
    store = Store(tmp_path / "tasks.sqlite")
    old_note = NoteDocument(platform=PlatformId.VIVO, account_id=provider.account_id, source_id="old",
                            blocks=[Block(kind="paragraph", spans=[Span(text="Old complete snapshot")])])
    store.replace_snapshot(PlatformId.VIVO, provider.account_id, [old_note], True)
    store.save_receipt("other-active-write", "sending", ["synthetic-remote-id"])
    report = TaskReport(id="fixture-fetch", operation="fetch", started_at=now())
    context = TaskContext(report, store, lambda _: None)
    task_threads, save_task = [], store.save_task

    def save_task_on_owner(report):
        task_threads.append(threading.get_ident())
        save_task(report)

    monkeypatch.setattr(store, "save_task", save_task_on_owner)
    endpoint.main_thread = threading.get_ident()
    return provider, context, store, endpoint, wires, task_threads


def test_pairs_keep_listing_order_and_finish_before_serial_details_and_assets(tmp_path, monkeypatch, server_key):
    provider, context, store, endpoint, wires, task_threads = setup_fetch(tmp_path, monkeypatch, server_key)
    endpoint.rows[0]["resources"] = True
    endpoint.rows[3]["resources"] = True

    def body(guid):
        if guid == "0":
            assert endpoint.finished["1"].wait(3), "The second body should run while the first is pending."
        return 200, (f'<vnote-image guid="asset-{guid}"></vnote-image>' if guid in ("0", "3") else f"<p>{guid}</p>")

    endpoint.body = body
    parsed, parse_entry = [], vivo.parse_entry

    def parse(row, *args):
        assert threading.get_ident() == endpoint.main_thread
        parsed.append(row["guid"])
        return parse_entry(row, *args)

    monkeypatch.setattr(vivo, "parse_entry", parse)
    fetch_snapshot(provider, store, context)
    assert parsed == ["0", "1", "2", "3", "4"]
    assert endpoint.maximum == 2 and len(endpoint.body_sessions) == 2
    assert endpoint.attempts == Counter({str(i): 1 for i in range(5)})
    assert store.snapshot(PlatformId.VIVO, provider.account_id) == {"complete": True, "count": 5}
    assert len([a for n in store.notes(PlatformId.VIVO, provider.account_id) for a in n.attachments]) == 2
    assert set(task_threads) == {endpoint.main_thread}
    assert store.receipt("other-active-write")["status"] == "sending", "Readers must not create TaskRunner."
    assert endpoint.events.index(("end", "/api/file/webdisk/source/key-0", None)) < endpoint.events.index(
        ("start", "/note-api/note/getContent/v2", "2")
    )
    assert provider.transport.session not in endpoint.body_sessions
    assert all(s in endpoint.closed and not s.cookies and not s.headers for s in endpoint.body_sessions)
    assert provider.transport.session.headers["token"] == "synthetic-token"
    assert provider.transport.cookie("fixture") == "synthetic-cookie"
    assert wires[0]._key and all(not w._key and not w._iv and not w._header for w in wires[1:])
    provider.close()


def test_reader_cookie_and_header_rotation_cannot_change_other_sessions(tmp_path, monkeypatch, server_key):
    provider, _, _, _, wires, _ = setup_fetch(tmp_path, monkeypatch, server_key, count=0)
    readers = [provider._body_reader(), provider._body_reader()]
    try:
        sessions = [p.transport.session for p in [provider, *readers]]
        cookies = [next(iter(s.cookies)) for s in sessions]
        assert len({id(s) for s in sessions}) == len({id(c) for c in cookies}) == 3
        assert len({w._key for w in wires}) == len({w._iv for w in wires}) == 3
        assert all((c.domain, c.path, c.secure) == (".vivo.com.cn", "/note-api", True) for c in cookies)
        cookies[1].value = "synthetic-rotated"
        sessions[1].headers["token"] = "synthetic-rotated"
        assert [cookies[i].value for i in (0, 2)] == ["synthetic-cookie"] * 2
        assert [sessions[i].headers["token"] for i in (0, 2)] == ["synthetic-token"] * 2
    finally:
        for reader in readers:
            reader.close()
            reader.transport.session.headers.clear()
        provider.close()


@pytest.mark.parametrize("status,code,attempts", [
    (401, "session_expired", 1), (403, "request_forbidden", 1),
    (429, "rate_limited", 3), (None, "network_error", 3),
])
def test_fatal_body_read_stops_next_pair_and_keeps_previous_complete_cache(
    tmp_path, monkeypatch, server_key, status, code, attempts,
):
    provider, context, store, endpoint, _, _ = setup_fetch(tmp_path, monkeypatch, server_key)
    monkeypatch.setattr(context.cancelled, "wait", lambda delay: False)

    def body(guid):
        if guid != "0":
            return 200, "<p>peer</p>"
        if status is None:
            raise requests.ConnectionError("synthetic failure")
        return status, None

    endpoint.body = body
    with pytest.raises(BridgeError) as error:
        fetch_snapshot(provider, store, context)
    assert error.value.code == code
    assert endpoint.attempts["0"] == attempts and set(endpoint.attempts) <= {"0", "1"}
    assert [n.source_id for n in store.notes(PlatformId.VIVO, provider.account_id)] == ["old"]
    assert store.snapshot(PlatformId.VIVO, provider.account_id) == {"complete": True, "count": 1}
    assert all(s in endpoint.closed and not s.cookies and not s.headers for s in endpoint.body_sessions)
    provider.close()


@pytest.mark.parametrize("status,body,code", [(500, None, "server_error"), (200, {}, "protocol_changed")])
def test_nonfatal_body_failure_retains_other_notes_but_never_claims_full_coverage(
    tmp_path, monkeypatch, server_key, status, body, code,
):
    provider, context, store, endpoint, _, task_threads = setup_fetch(tmp_path, monkeypatch, server_key, count=3)
    monkeypatch.setattr(context.cancelled, "wait", lambda delay: False)
    endpoint.body = lambda guid: (status, body) if guid == "0" else (200, f"<p>{guid}</p>")
    fetch_snapshot(provider, store, context)
    assert {n.source_id for n in store.notes(PlatformId.VIVO, provider.account_id)} == {"1", "2"}
    assert store.snapshot(PlatformId.VIVO, provider.account_id) == {"complete": False, "count": 2}
    assert [(i.code, i.note_id) for i in context.report.issues] == [(code, "0"), ("incomplete_snapshot", "")]
    assert context.report.completed == 3 and context.report.succeeded == 2
    assert set(task_threads) == {endpoint.main_thread}
    provider.close()


@pytest.mark.parametrize("failure", [429, 500, "connection"])
def test_body_transport_retries_remain_bounded_and_recovered_text_is_not_lost(
    tmp_path, monkeypatch, server_key, failure,
):
    provider, context, store, endpoint, _, _ = setup_fetch(tmp_path, monkeypatch, server_key, count=2)
    waits = []
    monkeypatch.setattr(context.cancelled, "wait", waits.append)

    def body(guid):
        if guid == "0" and endpoint.attempts[guid] < 3:
            if failure == "connection":
                raise requests.ConnectionError("synthetic failure")
            return failure, None
        return 200, f"<p>Recovered {guid}</p>"

    endpoint.body = body
    fetch_snapshot(provider, store, context)
    assert endpoint.attempts == Counter({"0": 3, "1": 1})
    assert waits == ([2, 4] if failure == 429 else [1, 2])
    assert store.snapshot(PlatformId.VIVO, provider.account_id) == {"complete": True, "count": 2}
    assert all("Recovered" in n.plain_text for n in store.notes(PlatformId.VIVO, provider.account_id))
    provider.close()


def test_cancel_waits_for_both_inflight_reads_without_retry_or_next_pair(tmp_path, monkeypatch, server_key):
    provider, context, store, endpoint, wires, task_threads = setup_fetch(tmp_path, monkeypatch, server_key)
    arrived, release = threading.Barrier(3), threading.Event()

    def body(guid):
        arrived.wait(3)
        assert release.wait(3)
        if guid == "0":
            raise requests.ConnectionError("An in-flight failure after cancellation must not retry.")
        return 200, "<p>Response after cancellation</p>"

    endpoint.body = body

    def run():
        endpoint.main_thread = threading.get_ident()
        fetch_snapshot(provider, store, context)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run)
        try:
            arrived.wait(3)
            context.cancelled.set()
            assert not future.done() and not endpoint.closed
            assert len(wires) == 3 and all(w._key for w in wires)
        finally:
            release.set()
        with pytest.raises(Cancelled):
            future.result(timeout=3)
    assert endpoint.attempts == Counter({"0": 1, "1": 1})
    assert [n.source_id for n in store.notes(PlatformId.VIVO, provider.account_id)] == ["old"]
    assert all(s in endpoint.closed and not s.cookies and not s.headers for s in endpoint.body_sessions)
    assert all(not w._key for w in wires[1:])
    assert set(task_threads) == {endpoint.main_thread}
    provider.close()


def test_cancel_after_a_complete_pair_dispatches_no_next_pair_and_preserves_cache(tmp_path, monkeypatch, server_key):
    provider, context, store, endpoint, _, _ = setup_fetch(tmp_path, monkeypatch, server_key)
    update = context.update

    def cancel_after_pair(**changes):
        update(**changes)
        if changes.get("completed") == 2:
            context.cancelled.set()

    monkeypatch.setattr(context, "update", cancel_after_pair)
    with pytest.raises(Cancelled):
        fetch_snapshot(provider, store, context)
    assert endpoint.attempts == Counter({"0": 1, "1": 1})
    assert [n.source_id for n in store.notes(PlatformId.VIVO, provider.account_id)] == ["old"]
    assert all(s in endpoint.closed and not s.cookies and not s.headers for s in endpoint.body_sessions)
    provider.close()


def test_partial_reader_setup_failure_clears_first_reader_without_any_body_request(tmp_path, monkeypatch, server_key):
    provider, context, store, endpoint, _, _ = setup_fetch(tmp_path, monkeypatch, server_key)
    create_reader, readers = provider._body_reader, []

    def fail_second_reader():
        if readers:
            raise RuntimeError("Synthetic reader initialization failure")
        reader = create_reader()
        readers.append(reader)
        return reader

    monkeypatch.setattr(provider, "_body_reader", fail_second_reader)
    with pytest.raises(RuntimeError):
        fetch_snapshot(provider, store, context)
    assert not endpoint.attempts
    reader = readers[0]
    assert reader.transport.session in endpoint.closed
    assert not reader.transport.session.headers and not reader.transport.session.cookies and not reader._wire._key
    assert provider.transport.cookie("fixture") == "synthetic-cookie"
    assert [n.source_id for n in store.notes(PlatformId.VIVO, provider.account_id)] == ["old"]
    provider.close()


def test_empty_directory_needs_no_extra_authenticated_sessions(tmp_path, monkeypatch, server_key):
    provider, context, store, endpoint, wires, _ = setup_fetch(tmp_path, monkeypatch, server_key, count=0)
    fetch_snapshot(provider, store, context)
    assert not endpoint.body_sessions and len(wires) == 1
    assert store.snapshot(PlatformId.VIVO, provider.account_id) == {"complete": True, "count": 0}
    provider.close()


@pytest.mark.parametrize("change,code", [("account", "account_changed"), ("count", "snapshot_changed")])
def test_final_account_and_count_checks_still_guard_snapshot_commit(tmp_path, monkeypatch, server_key, change, code):
    provider, context, store, endpoint, _, _ = setup_fetch(tmp_path, monkeypatch, server_key, count=2)
    account = provider.account_id

    def probe():
        if change == "account":
            provider.account_id = "different-account"
        else:
            endpoint.rows.append(dict(guid="added-after-content"))
        return provider.account_id

    provider.probe = probe
    with pytest.raises(BridgeError) as error:
        fetch_snapshot(provider, store, context)
    assert error.value.code == code
    assert [n.source_id for n in store.notes(PlatformId.VIVO, account)] == ["old"]
    assert all(s in endpoint.closed and not s.cookies and not s.headers for s in endpoint.body_sessions)
    provider.close()

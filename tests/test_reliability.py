import itertools
import threading

import pytest
import requests

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import Block, NoteDocument, PlatformId, Span, TaskStatus
from note_bridge.operations import migrate, receipt_key
from note_bridge.providers.base import SPECS, CreatedNote, Provider, Snapshot
from note_bridge.providers.transport import Transport, host_matches
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


class MemoryTarget(Provider):
    write_supported = True

    def __init__(self, platform):
        self.spec = SPECS[platform]
        self.account_id = "target-a"
        self.created = []

    def probe(self):
        return self.account_id

    def fetch(self, context):
        return Snapshot([], True)

    def create(self, note, context):
        self.created.append(note.model_copy(deep=True))
        return CreatedNote(["remote-" + str(len(self.created))])


def make_note(platform=PlatformId.XIAOMI, account="source-a"):
    return NoteDocument(
        platform=platform,
        account_id=account,
        source_id="same-id",
        title="原始笔记",
        blocks=[Block(spans=[Span(text="正文")])],
    )


@pytest.mark.parametrize("source,target_id", list(itertools.permutations(PlatformId, 2)))
def test_42_local_directions_resume_without_duplicate_creation(tmp_path, source, target_id):
    store = Store(tmp_path / "state.sqlite")
    runner = TaskRunner(store)
    note, target = make_note(source), MemoryTarget(target_id)
    before = note.model_dump()
    runner.start("migrate", lambda ctx: migrate([note], target, store, ctx))
    runner.join(3)
    assert runner.current().status == TaskStatus.SUCCEEDED
    runner.start("migrate", lambda ctx: migrate([note], target, store, ctx))
    runner.join(3)
    assert len(target.created) == 1 and runner.current().skipped == 1
    assert note.model_dump() == before, "迁移只能新增目标内容，不能改动来源"


def test_uncertain_write_is_persisted_and_never_retried(tmp_path):
    store, target = Store(tmp_path / "state.sqlite"), MemoryTarget(PlatformId.WPS)
    note = make_note()
    calls = []

    def unknown(note, context):
        calls.append(note.source_id)
        raise WriteUncertain()

    target.create = unknown
    runner = TaskRunner(store)
    for _ in range(2):
        runner.start("migrate", lambda ctx: migrate([note], target, store, ctx))
        runner.join(3)
        assert runner.current().status == TaskStatus.NEEDS_REVIEW
    assert len(calls) == 1
    assert store.receipt(receipt_key(note, target))["status"] == "uncertain"


def test_restart_preserves_unacknowledged_intent(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    target, note = MemoryTarget(PlatformId.WPS), make_note()
    key = receipt_key(note, target)
    store.save_receipt(key, "sending")
    runner = TaskRunner(store)
    runner.start("migrate", lambda ctx: migrate([note], target, store, ctx))
    runner.join(3)
    assert runner.current().status == TaskStatus.NEEDS_REVIEW and not target.created


@pytest.mark.parametrize("receipt_status,expected", [("confirmed", TaskStatus.SUCCEEDED),
                                                    ("uncertain", TaskStatus.NEEDS_REVIEW),
                                                    ("rejected", TaskStatus.FAILED)])
def test_resume_resolves_receipts_before_requiring_old_attachment_files(tmp_path, receipt_status, expected):
    from note_bridge.models import Attachment
    from note_bridge.providers.wps import WpsProvider

    store = Store(tmp_path / "state.sqlite")
    first, remaining = make_note(), make_note()
    remaining.source_id = "remaining-note"
    first.attachments = [Attachment(id="old-image", name="missing.png", kind="image", mime="image/png",
                                    local_path="missing.png", size=12, sha256="0" * 64)]
    first.blocks.append(Block(kind="attachment", attachment_id="old-image"))
    target = WpsProvider(None, None, tmp_path, None)
    target.account_id, target.write_supported, target.images_supported = "target-a", True, True
    created = []

    def create(note, context):
        created.append(note.source_id)
        return CreatedNote(["new-remaining-id"])

    target.create = create
    key = receipt_key(first, target)
    store.save_receipt(key, receipt_status, ["prior-id"])
    runner = TaskRunner(store)
    runner.start("migrate", lambda ctx: migrate([first, remaining], target, store, ctx))
    runner.join(3)
    assert runner.current().status == expected
    assert created == ([remaining.source_id] if receipt_status == "confirmed" else [])
    assert store.receipt(key)["status"] == receipt_status
    if receipt_status == "confirmed":
        assert runner.current().skipped == runner.current().succeeded == 1
        assert runner.current().completed == runner.current().total == 2
    elif receipt_status == "uncertain":
        assert any(issue.code == "write_uncertain" for issue in runner.current().issues)


def test_accounts_are_isolated_in_snapshots_and_receipts(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    first, second = make_note(), make_note(account="source-b")
    second.title = "其他账号"
    store.replace_snapshot(first.platform, first.account_id, [first], True)
    store.replace_snapshot(second.platform, second.account_id, [second], True)
    assert store.notes(first.platform, first.account_id)[0].title == "原始笔记"
    assert store.notes(second.platform, second.account_id)[0].title == "其他账号"
    with pytest.raises(ValueError):
        store.replace_snapshot(first.platform, first.account_id, [second], True)
    target = MemoryTarget(PlatformId.WPS)
    key = receipt_key(first, target)
    target.account_id = "target-b"
    assert receipt_key(first, target) != key
    assert receipt_key(second, target) != key


def test_only_one_task_and_cancellation_reports_committed_work(tmp_path):
    runner = TaskRunner(Store(tmp_path / "state.sqlite"))
    ready = threading.Event()

    def work(ctx):
        ctx.update(succeeded=1, completed=1, total=2)
        ready.set()
        ctx.cancelled.wait(3)
        ctx.check_cancel()

    report = runner.start("migrate", work)
    assert ready.wait(2)
    with pytest.raises(BridgeError, match="已有任务"):
        runner.start("export", lambda ctx: None)
    runner.cancel(report.id)
    runner.join(3)
    assert runner.current().status == TaskStatus.CANCELLED
    assert runner.current().succeeded == 1


def test_signed_asset_urls_are_never_persisted(tmp_path):
    from note_bridge.models import Attachment

    store, note = Store(tmp_path / "state.sqlite"), make_note()
    note.attachments = [
        Attachment(id="image", name="x", download_url="https://example.com/private?signature=synthetic")
    ]
    store.replace_snapshot(note.platform, note.account_id, [note], True)
    with store.connection() as db:
        text = db.execute("SELECT document FROM notes").fetchone()[0]
    assert "signature=" not in text and "download_url" not in text


def test_reads_retry_but_unknown_writes_only_send_once():
    class FailingSession:
        headers = {}

        def __init__(self):
            self.calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            raise requests.Timeout("sensitive address must not reach UI")

    session = FailingSession()
    transport = Transport("https://example.com", ("example.com",), session=session)
    with pytest.raises(BridgeError) as read:
        transport.json("GET", "/notes")
    assert session.calls == 3 and "sensitive" not in str(read.value)
    with pytest.raises(WriteUncertain):
        transport.json("POST", "/notes", write=True)
    assert session.calls == 4
    assert not host_matches("mi.com.attacker.example", ("mi.com",))


def test_unhandled_error_does_not_expose_response_or_report_success(tmp_path):
    runner = TaskRunner(Store(tmp_path / "state.sqlite"))

    def fail(ctx):
        raise RuntimeError("secret cookie example")

    runner.start("fetch", fail)
    runner.join(3)
    report = runner.current()
    assert report.status == TaskStatus.FAILED and "secret" not in report.model_dump_json()


@pytest.mark.parametrize("status,expected", [(401, "session_expired"), (403, "request_forbidden")])
@pytest.mark.parametrize("write", [False, True])
def test_forbidden_request_is_not_reported_as_expired_login_or_retried(status, expected, write):
    from types import SimpleNamespace

    calls, closed = [], []
    def request(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(status_code=status, close=lambda: closed.append(True))
    session = SimpleNamespace(headers={}, request=request)
    transport = Transport("https://example.com", ("example.com",), session=session)
    with pytest.raises(BridgeError) as error:
        transport.request("POST" if write else "GET", "/notes", write=write)
    assert error.value.code == expected and len(calls) == len(closed) == 1

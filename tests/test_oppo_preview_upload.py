"""Unsupported multipart uploads must stop before a note or any part is sent."""
from test_oppo_files import setup

from note_bridge.models import TaskStatus
from note_bridge.operations import migrate, receipt_key
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


def test_vendor_threshold_stops_before_parts_or_note_creation_and_keeps_receipts(tmp_path):
    provider, note, data = setup(tmp_path)
    assert provider._files.multipart_supported is False
    requests = []

    def prepare(method, path, **kwargs):
        requests.append(path)
        assert path.endswith("prepare-file-upload"), "No multipart init, part, merge or add is permitted."
        return {"code": 0, "data": {"applyId": "synthetic-upload", "existingFile": False,
                "smallFileThreshold": len(data) - 1, "sliceSize": 37}}

    provider.transport.json = prepare
    provider._json = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("No note add may occur"))
    store = Store(tmp_path / "state.sqlite")
    runner = TaskRunner(store)
    runner.start("migration", lambda context: migrate([note], provider, store, context))
    runner.join(3)
    report = runner.current()
    key = receipt_key(note, provider)
    assert report.status == TaskStatus.NEEDS_REVIEW and report.succeeded == 0
    assert any(issue.code == "multipart_upload_pending" for issue in report.issues)
    assert "未发送笔记新增请求" in report.stage
    assert store.receipt(key)["status"] == "uncertain" and not store.receipt(key)["remote_ids"]
    assert len(store.resource_receipts(key)) == 2
    assert all(row["status"] == "allocated" for row in store.resource_receipts(key))
    runner.start("migration", lambda context: migrate([note], provider, store, context))
    runner.join(3)
    assert len(requests) == 1, "The retained upload scope must not automatically allocate again."

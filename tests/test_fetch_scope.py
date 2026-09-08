"""A successful fetch is useful proof only when account, task and files are bound."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, Block, NoteDocument, Span, TaskReport
from note_bridge.storage import Store

path = Path(__file__).resolve().parents[1] / "scripts/fetch_scope.py"
spec = importlib.util.spec_from_file_location("fetch_scope_test", path)
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


def setup(tmp_path):
    store = Store(tmp_path / ".private/session-lab/notes.sqlite")
    resource = tmp_path / ".private/session-lab/resources/shared.png"
    resource.parent.mkdir(parents=True)
    resource.write_bytes(b"synthetic-shared-image")
    notes = []
    for index in range(2):
        notes.append(NoteDocument(platform="huawei", account_id="a" * 64, source_id=f"note-{index}",
                                  blocks=[Block(spans=[Span(text="Private body must not appear in the scope")])],
                                  attachments=[Attachment(id=f"image-{index}", name="image.png", kind="image",
                                      size=resource.stat().st_size, local_path="shared.png",
                                      sha256=hashlib.sha256(resource.read_bytes()).hexdigest())]))
    store.replace_snapshot("huawei", "a" * 64, notes, True)
    report = TaskReport(id="d" * 32, operation="fetch", status="succeeded", completed=2, total=2, succeeded=2,
                        started_at="2026-09-07T01:00:00+00:00", finished_at="2026-09-07T01:01:00+00:00")
    store.save_task(report)
    return store, notes, resource, report


def test_completed_scope_binds_all_identities_and_deduplicates_verified_resource_paths(tmp_path):
    store, notes, _, report = setup(tmp_path)
    relative = scope.write_fetch_scope(tmp_path, store, "huawei", "a" * 64, report)
    raw = (tmp_path / relative).read_text("utf-8")
    proof = json.loads(raw)
    assert proof["notes"] == {n.source_id: n.fingerprint() for n in notes}
    assert len(proof["resources"]) == 1 and len(proof["attachment_references"]) == 2
    assert proof["task"]["report_sha256"] == scope.digest(store.task(report.id).model_dump(mode="json"))
    assert proof["platform"] == "huawei" and proof["account"] == "a" * 64
    assert "Private body" not in raw and "cookies" not in proof
    assert proof["cloud_writes"] == 0 and proof["formal_acceptance"] is False
    with pytest.raises(FileExistsError):
        scope.write_fetch_scope(tmp_path, store, "huawei", "a" * 64, report)


@pytest.mark.parametrize("failure", ["wrong-account", "failed-task", "unfinished-task", "altered-report",
                                     "wrong-count", "partial-cache", "changed-resource"])
def test_status_or_count_alone_cannot_create_a_complete_fetch_scope(tmp_path, failure):
    store, notes, resource, report = setup(tmp_path)
    account = "a" * 64
    if failure == "wrong-account":
        account = "b" * 64
    elif failure == "failed-task":
        report.status = "failed"
        store.save_task(report)
    elif failure == "unfinished-task":
        report.finished_at = None
        store.save_task(report)
    elif failure == "altered-report":
        report.stage = "Forged summary"
    elif failure == "wrong-count":
        report.total = report.succeeded = report.completed = 1
        store.save_task(report)
    elif failure == "partial-cache":
        store.replace_snapshot("huawei", account, notes, False)
    else:
        resource.write_bytes(b"changed")
    with pytest.raises(BridgeError):
        scope.write_fetch_scope(tmp_path, store, "huawei", account, report)
    assert not list((tmp_path / ".private/evidence").glob("fetch-*-scope.json"))

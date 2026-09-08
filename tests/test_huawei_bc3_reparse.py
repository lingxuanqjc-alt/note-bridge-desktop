import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, Block, NoteDocument, Span
from note_bridge.operations import receipt_key
from note_bridge.providers.huawei import encode_body, parse_entry
from note_bridge.providers.huawei_groups import folder_key
from note_bridge.storage import Store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import huawei_bc3_empty_paragraph as helper
from session_discovery import discover


def setup(tmp_path, monkeypatch, failure=None):
    private = tmp_path / ".private"
    (private / "checkpoints").mkdir(parents=True)
    (private / "evidence").mkdir()
    resources = private / "session-lab/resources/huawei/target-account"
    resources.mkdir(parents=True)
    assets = []
    for index in range(2):
        data = b"synthetic-image-" + bytes([index])
        path = resources / (str(index) + ".png")
        path.write_bytes(data)
        assets.append(Attachment(id=f"asset-{index}", name=f"image-{index}.png", kind="image", mime="image/png",
                                 size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                                 local_path=f"huawei/target-account/{index}.png"))
    blocks = [Block(spans=[Span(text=f"line-{index}")]) for index in range(29)]
    blocks[2], blocks[5], blocks[17] = (Block(kind="attachment", attachment_id=assets[0].id),
                                     Block(kind="attachment", attachment_id=assets[1].id), Block())
    blocks[8] = Block(kind="heading", level=2, spans=[Span(text="heading", strike=True)])
    source = NoteDocument(platform="xiaomi", account_id="source-account", source_id="synthetic-source",
                          title="synthetic BC3", blocks=blocks, attachments=assets,
                          source_folder_id="source-folder", source_folder_name="synthetic group",
                          warnings=["小米超链接使用独立链接卡片，原段落内位置和样式需核对。"])
    _, markup, warnings = encode_body(source, {a.id: a.name for a in assets})
    detail = {"guid": "synthetic-target", "kind": "note", "etag": "current-version",
              "attachments": [{"assetId": a.id, "usage": a.name, "resourceLength": a.size,
                               "versionId": "current-asset-version"} for a in assets],
              "data": json.dumps({"content": {"html_content": markup, "title": source.title, "tag_id": "target-folder"}})}
    restored = parse_entry(detail, "target-account", {"target-folder": "synthetic group"}, assets)
    assert len(restored.blocks) == 32 and restored.blocks[18] == Block()
    cached = restored.model_copy(update={"blocks": restored.blocks[:18] + restored.blocks[19:]})
    store = Store(private / "session-lab/notes.sqlite")
    others = [cached.model_copy(update={"source_id": f"other-{index}"}) for index in range(16)]
    store.replace_snapshot("huawei", cached.account_id, [cached, *others], True)
    store.replace_snapshot("xiaomi", source.account_id, [source], True)
    target = SimpleNamespace(spec=SimpleNamespace(id="huawei"), account_id=cached.account_id)
    key = receipt_key(source, target)
    store.save_receipt(key, "sending", [cached.source_id])
    for asset in assets:
        for value in (asset.id, asset.name):
            store.save_resource_receipt(key, value, "uploaded")
    store.save_receipt(key, "confirmed", [cached.source_id])
    store.save_receipt(folder_key(source, cached.account_id), "confirmed", [cached.source_folder_id])
    entry = {"from_cloud_fixture": {"platform": "xiaomi", "account": source.account_id,
                                   "source_id": source.source_id, "fingerprint": source.fingerprint()}}
    payloads = {
        "checkpoints/xiaomi-huawei-BC3-job.json": {"id": helper.BATCH, "armed": False, "target": "huawei",
                                                 "expected_account": cached.account_id, "sources": [entry] * 4},
        "evidence/BC3-readonly-reconciliation.json": {"batch_id": helper.BATCH, "target_notes": 17, "items": [{
            "index": 0, "source_id": source.source_id, "source_fingerprint": source.fingerprint(),
            "target_fingerprint": cached.fingerprint(), "receipt_key": key, "prior_receipt": "confirmed",
            "remote_ids": [cached.source_id], "content_verified": False, "group_verified": True,
            "attachment_originals_verified": True}]},
        f"evidence/{helper.BATCH}.json": {"batch_id": helper.BATCH, "status": "needs_review", "issues": [
            {"note_id": source.source_id, "code": "format_downgrade", "message": message} for message in warnings]},
        "lab-discovery.json": {"platform": "huawei", "operation": helper.OPERATION},
    }
    for name, value in payloads.items():
        (private / name).write_text(json.dumps(value), "utf-8")
    calls = []
    before = {note.source_id: note.model_dump_json() for note in store.notes("huawei", cached.account_id)}

    class Provider:
        def probe(self):
            calls.append("probe")
            return "other-account" if failure == "account" else cached.account_id

        def _listing(self):
            calls.append("listing")
            final = calls.count("listing") == 2
            if final and failure == "cache_race":
                with store.connection() as db:
                    value = others[0].model_copy(update={"title": "concurrent change"}).model_dump_json()
                    db.execute("UPDATE notes SET document=? WHERE source_id=?", (value, others[0].source_id))
            return {"ctagNoteInfo": "listing-version", "startCursor": "cursor"}, [
                {"guid": cached.source_id, "kind": "note",
                 "etag": "changed-version" if final and failure == "etag_after" else detail["etag"]}]

        def _json(self, path, body):
            calls.append(path)
            assert path == "note/query" and body["guid"] == cached.source_id, "Only the recorded note may be read."
            response = deepcopy(detail)
            if failure == "etag_detail":
                response["etag"] = "unexpected-version"
            elif failure == "guid":
                response["guid"] = "another-note"
            elif failure in ("body", "blank_missing"):
                response["data"] = response["data"].replace("line-3", "changed") if failure == "body" else response["data"].replace(
                    '<element type=\\"Text\\"><hw_font size=\\"1.0\\"></hw_font></element>', "")
            elif failure in ("asset_identity", "asset_size"):
                response["attachments"][0]["assetId" if failure == "asset_identity" else "resourceLength"] = "foreign"
            elif failure == "local_bytes_after":
                (resources / "0.png").write_bytes(b"changed")
            return {"rspInfo": response}

        def close(self):
            calls.append("close")

    monkeypatch.setattr(helper, "create_provider", lambda *args: Provider())
    return store, cached, restored, before, calls, key


def test_exact_read_reparses_one_cache_row_without_new_notes_downloads_or_receipt_changes(tmp_path, monkeypatch):
    store, cached, restored, before, calls, key = setup(tmp_path, monkeypatch)
    receipt, resource_rows = store.receipt(key), store.resource_receipts(key)
    failure = (tmp_path / f".private/evidence/{helper.BATCH}.json").read_bytes()
    report = discover("huawei", [], tmp_path)
    assert report["status"] == "verified" and report["cache_updated"] and report["other_cached_notes_unchanged"] == 16
    assert report["cloud_writes"] == report["attachment_downloads"] == 0
    assert report["historical_etag_verified"] is report["remote_attachment_version_verified"] is False
    assert report["source_warnings"] == ["小米超链接使用独立链接卡片，原段落内位置和样式需核对。"]
    actual = {note.source_id: note for note in store.notes("huawei", cached.account_id)}
    assert actual[cached.source_id] == restored and store.snapshot("huawei", cached.account_id) == {"complete": True, "count": 17}
    assert all(actual[guid].model_dump_json() == document for guid, document in before.items() if guid != cached.source_id)
    assert store.receipt(key) == receipt and store.resource_receipts(key) == resource_rows
    assert (tmp_path / f".private/evidence/{helper.BATCH}.json").read_bytes() == failure
    assert calls == ["probe", "listing", "note/query", "listing", "probe", "close"]
    count = len(calls)
    with pytest.raises(BridgeError):
        discover("huawei", [], tmp_path)
    assert len(calls) == count, "The independent proof must prevent a repeated remote inspection."


@pytest.mark.parametrize("failure", ["account", "guid", "etag_detail", "etag_after", "body", "blank_missing",
                                     "asset_identity", "asset_size", "local_bytes_after", "cache_race"])
def test_unrelated_changes_cannot_be_accepted_as_only_the_missing_empty_paragraph(tmp_path, monkeypatch, failure):
    store, cached, _, before, calls, key = setup(tmp_path, monkeypatch, failure)
    with pytest.raises(BridgeError):
        helper.run([], tmp_path)
    actual = {note.source_id: note for note in store.notes("huawei", cached.account_id)}
    assert actual[cached.source_id] == cached and store.receipt(key)["status"] == "confirmed"
    assert all(actual[guid].model_dump_json() == document for guid, document in before.items()
               if guid != "other-0" or failure != "cache_race")
    proof = json.loads((tmp_path / ".private/evidence" / helper.PROOF).read_text("utf-8"))
    assert proof["status"] == "blocked" and proof["cache_updated"] is False
    assert calls.count("note/query") <= 1 and calls[-1] == "close"


def test_cached_fingerprint_drift_prevents_any_remote_read(tmp_path, monkeypatch):
    store, cached, _, _, calls, _ = setup(tmp_path, monkeypatch)
    with store.connection() as db:
        db.execute("UPDATE notes SET document=? WHERE source_id=?",
                   (cached.model_copy(update={"title": "changed locally"}).model_dump_json(), cached.source_id))
    with pytest.raises(BridgeError):
        helper.run([], tmp_path)
    assert calls == [] and not (tmp_path / ".private/evidence" / helper.PROOF).exists()

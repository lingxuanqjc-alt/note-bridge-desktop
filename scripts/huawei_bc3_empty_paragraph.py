"""Reparse only BC3 entry zero; cloud reads and one guarded local cache update."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

from note_bridge.errors import BridgeError
from note_bridge.models import Block, NoteDocument
from note_bridge.operations import receipt_key
from note_bridge.paths import confined
from note_bridge.providers.factory import create_provider
from note_bridge.providers.huawei import parse_entry
from note_bridge.providers.huawei_groups import folder_key
from note_bridge.storage import Store

BATCH = "matrix-20260907-xiaomi-huawei-BC3"
OPERATION = "huawei_BC3_empty_paragraph"
PROOF = "huawei-BC3-empty-paragraph-reparse.json"


def require(condition):
    if not condition:
        raise BridgeError("huawei_reparse_scope", "华为 BC3 第一条的身份、版本或内容与限定范围不符，未更新缓存。")


def documents(db, account):
    return dict(db.execute("SELECT source_id,document FROM notes WHERE platform='huawei' AND account=?", (account,)))


def verified_assets(note, resources):
    require(len(note.attachments) == 2 and len({a.id for a in note.attachments}) == 2
            and len({a.name for a in note.attachments}) == 2)
    account_root = resources / "huawei" / note.account_id
    for asset in note.attachments:
        require(asset.kind == "image" and asset.local_path and asset.size > 0 and asset.sha256)
        path = confined(resources, asset.local_path)
        require(path.is_relative_to(account_root.resolve()) and path.is_file())
        data = path.read_bytes()
        require(len(data) == asset.size and hashlib.sha256(data).hexdigest() == asset.sha256)


def run(jars, root):
    private = root / ".private"
    output = private / "evidence" / PROOF
    require(not output.exists())
    paths = {
        "manifest": private / "checkpoints/xiaomi-huawei-BC3-job.json",
        "reconciliation": private / "evidence/BC3-readonly-reconciliation.json",
        "original_failure": private / "evidence" / (BATCH + ".json"),
    }
    raw = {key: path.read_bytes() for key, path in paths.items()}
    job, old, failure = (json.loads(raw[key]) for key in ("manifest", "reconciliation", "original_failure"))
    require(job["id"] == BATCH and job["armed"] is False and job["target"] == "huawei"
            and len(job["sources"]) == 4 and old["batch_id"] == BATCH and failure["batch_id"] == BATCH
            and failure["status"] == "needs_review" and old["target_notes"] == 17)
    binding = old["items"][0]
    require(binding["index"] == 0 and binding["prior_receipt"] == "confirmed"
            and binding["content_verified"] is False and binding["group_verified"] is True
            and binding["attachment_originals_verified"] is True and len(binding["remote_ids"]) == 1)
    account, guid = job["expected_account"], binding["remote_ids"][0]
    scope = job["sources"][0]["from_cloud_fixture"]
    require(scope["platform"] == "xiaomi" and scope["source_id"] == binding["source_id"]
            and scope["fingerprint"] == binding["source_fingerprint"])
    store = Store(private / "session-lab/notes.sqlite")
    with store.connection() as db:
        before = documents(db, account)
        row = db.execute("SELECT document FROM notes WHERE platform='xiaomi' AND account=? AND source_id=?",
                         (scope["account"], scope["source_id"])).fetchone()
    require(row is not None and len(before) == 17 and guid in before
            and store.snapshot("huawei", account) == {"complete": True, "count": 17})
    source, cached = NoteDocument.model_validate_json(row[0]), NoteDocument.model_validate_json(before[guid])
    require(source.fingerprint() == scope["fingerprint"] and cached.fingerprint() == binding["target_fingerprint"]
            and not cached.warnings and len(source.blocks) == 29
            and source.blocks[17] == Block() and len(cached.blocks) == 31)
    target = SimpleNamespace(spec=SimpleNamespace(id="huawei"), account_id=account)
    key = receipt_key(source, target)
    require(key == binding["receipt_key"] and store.receipt(key) == {"status": "confirmed", "remote_ids": [guid]}
            and store.receipt(folder_key(source, account)) == {"status": "confirmed", "remote_ids": [cached.source_folder_id]})
    resources = private / "session-lab/resources"
    verified_assets(cached, resources)
    resource_rows = store.resource_receipts(key)
    require(len(resource_rows) == 4 and {r["resource_id"] for r in resource_rows}
            == {value for asset in cached.attachments for value in (asset.id, asset.name)}
            and all(r["status"] == "linked" for r in resource_rows))
    warnings = [issue["message"] for issue in failure["issues"]
                if issue["note_id"] == source.source_id and issue["code"] == "format_downgrade"]
    report = {"kind": "huawei-BC3-empty-paragraph-reparse", "batch_id": BATCH, "entry_index": 0,
              "formal_acceptance": False, "cloud_writes": 0, "attachment_downloads": 0,
              "status": "started", "cache_updated": False, "historical_etag_verified": False,
              "remote_attachment_version_verified": False, "guid": guid, "receipt_key": key,
              "source_fingerprint": source.fingerprint(), "cached_fingerprint": cached.fingerprint(),
              "source_warnings": source.warnings,
              "file_sha256": {key: hashlib.sha256(value).hexdigest() for key, value in raw.items()}}
    # Reserve a separate one-use proof. An interrupted run must be reviewed, not repeated.
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    provider = None
    try:
        provider = create_provider("huawei", jars, resources)
        require(provider.probe() == account)
        listing, rows = provider._listing()
        matches = [r for r in rows if r.get("guid") == guid and r.get("kind") == "note"]
        require(len(matches) == 1 and isinstance(matches[0].get("etag"), str) and matches[0]["etag"])
        etag = matches[0]["etag"]
        detail = provider._json("note/query", {"guid": guid, "kind": "note",
            "ctagNoteInfo": listing.get("ctagNoteInfo", ""), "startCursor": listing.get("startCursor", "")})["rspInfo"]
        require(detail.get("guid") == guid and detail.get("etag") == etag)
        metadata = detail.get("attachments")
        require(isinstance(metadata, list) and len(metadata) == 2 and all(isinstance(item, dict) for item in metadata))
        for asset in cached.attachments:
            matches = [item for item in metadata if item.get("assetId") == asset.id and item.get("usage") == asset.name]
            require(len(matches) == 1 and type(matches[0].get("resourceLength")) is int
                    and matches[0]["resourceLength"] == asset.size
                    and isinstance(matches[0].get("versionId"), str) and matches[0]["versionId"])
        restored = parse_entry(detail, account, {cached.source_folder_id: cached.source_folder_name}, cached.attachments)
        require(not restored.warnings and len(restored.blocks) == 32 and restored.blocks[18] == Block())
        projection = restored.model_copy(update={"blocks": restored.blocks[:18] + restored.blocks[19:]})
        require(projection.fingerprint() == cached.fingerprint())
        spec = importlib.util.spec_from_file_location("huawei_bc3_matrix", Path(__file__).with_name("live-matrix-job.py"))
        matrix = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(matrix)
        require(matrix.compare_note(source, restored, warnings))
        _, final_rows = provider._listing()
        require([(r.get("kind"), r.get("etag")) for r in final_rows if r.get("guid") == guid] == [("note", etag)]
                and provider.probe() == account)
        verified_assets(cached, resources)
        # Guard against another worker changing any cached row while the one remote body was read.
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            require(documents(db, account) == before)
            current_source = db.execute("SELECT document FROM notes WHERE platform='xiaomi' AND account=? AND source_id=?",
                                        (scope["account"], scope["source_id"])).fetchone()
            require(current_source is not None and current_source[0] == row[0])
            changed = db.execute("UPDATE notes SET document=? WHERE platform='huawei' AND account=? AND source_id=?",
                                 (restored.model_dump_json(), account, guid))
            require(changed.rowcount == 1)
            after = documents(db, account)
            require(len(after) == 17 and all(after[note_id] == value for note_id, value in before.items() if note_id != guid))
        report.update(status="verified", cache_updated=True, content_verified=True, group_verified=True,
                      cached_assets_verified=True, response_asset_identity_and_size_verified=True,
                      stable_read_etag_sha256=hashlib.sha256(etag.encode()).hexdigest(),
                      response_asset_metadata_sha256=hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest(),
                      raw_body_sha256=hashlib.sha256(detail["data"].encode()).hexdigest(),
                      restored_fingerprint=restored.fingerprint(), old_projection_verified=True,
                      other_cached_notes_unchanged=16, note_queries=1, restored_blank_index=18, warnings=warnings)
        return report
    except Exception as error:
        report.update(status="blocked", code=error.code if isinstance(error, BridgeError) else type(error).__name__)
        raise
    finally:
        if provider is not None:
            provider.close()
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        os.replace(temporary, output)

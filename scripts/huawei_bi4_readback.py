"""Read exactly the two already confirmed BI4 targets; never resume the write batch."""
import io
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

from huawei_scoped_source import digest, read_guard
from huawei_session_diagnostics import observe_huawei_session
from oppo_scoped_source import verify_source
from xiaomi_browser_scope import _module, _ReadOnlyStore

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, NoteDocument
from note_bridge.operations import receipt_key
from note_bridge.paths import confined
from note_bridge.providers.factory import create_provider
from note_bridge.providers.huawei import encode_body, parse_entry
from note_bridge.providers.huawei_groups import folder_key, parse_tags

OPERATION = "huawei_BI4_target_readback"
BATCH = "matrix-20260907-oppo-huawei-BI4"
TASK = "a7e8980a83df4e3f9a332e485f50fb4a"
SOURCE_REFERENCE = {
    "path": ".private/evidence/oppo-fixed-source-c2e2e31b59d645499729aa9412f03919/proof.json",
    "sha256": "2dc0f428ed55a15eac0c88f6a40713262d280fbe8abdb86f09f35e64d08ed493",
}


def require(condition):
    if not condition:
        raise BridgeError("invalid_BI4_readback_scope", "BI4 已确认回执、来源版本或精确读取范围未通过核对。")


def bindings(store, root):
    paths = {"consumed": f".private/checkpoints/{BATCH}-consumed-job.json",
        "original": f".private/evidence/{BATCH}.json", "baseline": f".private/checkpoints/{BATCH}-before.json"}
    raw = {key: confined(root, path).read_bytes() for key, path in paths.items()}
    job, old, baseline = (json.loads(raw[key]) for key in ("consumed", "original", "baseline"))
    require(job.get("kind") == "cloud-matrix-batch" and job.get("id") == BATCH
        and job.get("target") == "huawei" and job.get("armed") is False
        and job.get("source_policy") == "direct_seed_only" and job.get("oppo_scope") == "OR1_fixed_direct_12"
        and re.fullmatch(r"[0-9a-f]{64}", str(job.get("expected_account", ""))))
    require(old.get("batch_id") == BATCH and old.get("status") == "needs_review"
        and old.get("code") == "matrix_read_incomplete" and old.get("source") == "oppo"
        and old.get("target") == "huawei" and old.get("succeeded") == 2 and old.get("skipped") == 0
        and old.get("before_count") == 22 and old.get("baseline_file") == paths["baseline"]
        and old.get("formal_acceptance") is False and old.get("items") == [])
    for evidence in (old, baseline):
        require(evidence.get("source_capture") == SOURCE_REFERENCE
            and evidence.get("source_scope") == "fixed_oppo_capture"
            and evidence.get("whole_source_account_snapshot") is False)
    sources = verify_source(SOURCE_REFERENCE, store, root)
    require(len(sources) == 2 and len(job.get("sources", [])) == 2
        and sources[0].source_folder_name == "未分类"
        and sources[0].source_folder_id == "00000000_0000_0000_0000_000000000000"
        and sources[1].source_folder_name == "笔记互迁分组验收 I")
    require(baseline.get("source") == {n.source_id: n.fingerprint() for n in sources}
        and isinstance(baseline.get("target"), dict) and len(baseline["target"]) == 22
        and all(re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in baseline["target"].values()))
    task = store.task(TASK)
    require(task and task.id == TASK and task.operation == "matrix_migrate" and task.status == "partial"
        and task.completed == task.total == task.succeeded == 2 and task.skipped == 0 and task.finished_at
        and [i.model_dump(mode="json") for i in task.issues] == old.get("issues"))
    expected_issues = [{"note_id": source.source_id, "code": "format_downgrade", "message": warning}
        for source in sources for warning in encode_body(source, {a.id: "verified" for a in source.attachments})[2]]
    require(old["issues"] == expected_issues)
    target = SimpleNamespace(spec=SimpleNamespace(id="huawei"), account_id=job["expected_account"])
    refs = []
    for index, (source, entry) in enumerate(zip(sources, job["sources"], strict=True)):
        origin = entry.get("from_cloud_fixture", {})
        require(entry.get("title") == source.display_title and entry.get("with_images") is True
            and entry.get("with_group") is True and origin.get("scoped_proof") == SOURCE_REFERENCE
            and (origin.get("platform"), origin.get("account"), origin.get("source_id"), origin.get("fingerprint"))
                == ("oppo", source.account_id, source.source_id, source.fingerprint())
            and origin.get("manifest") == ("oppo-complex-OR1-job.json" if index else
                "fixture-20260906-oppo-image-F2-consumed-job.json")
            and len(source.attachments) == 2 and len({a.size for a in source.attachments}) == 2)
        key = receipt_key(source, target)
        receipt, group = store.receipt(key), store.receipt(folder_key(source, target.account_id))
        with store.connection() as db:
            context = db.execute("SELECT source_platform,source_account,source_id,source_fingerprint,"
                "target_platform,target_account FROM receipt_contexts WHERE key=?", (key,)).fetchone()
            resources = [(row[0], row[1]) for row in db.execute(
                "SELECT resource_id,status FROM resource_receipts WHERE receipt_key=? ORDER BY resource_id", (key,))]
        require(context and tuple(context) == ("oppo", source.account_id, source.source_id,
            source.fingerprint(), "huawei", target.account_id)
            and receipt and receipt["status"] == "confirmed" and len(receipt["remote_ids"]) == 1
            and group and group["status"] == "confirmed" and len(group["remote_ids"]) == 1
            and len(resources) == 4 and all(status == "linked" for _, status in resources))
        asset_ids = sorted(value for value, _ in resources if not value.endswith((".png", ".jpg")))
        usages = sorted(value for value, _ in resources if value.endswith((".png", ".jpg")))
        guid = receipt["remote_ids"][0]
        require(len(asset_ids) == len(usages) == 2 and {Path(v).suffix for v in usages} == {".png", ".jpg"}
            and all(re.fullmatch(r"[A-Za-z0-9_$-]{1,150}", v) for v in [guid, *asset_ids])
            and guid not in baseline["target"])
        refs.append({"account": target.account_id, "source_id": guid, "receipt_key": key,
            "group_id": group["remote_ids"][0], "asset_ids": asset_ids, "usages": usages,
            "oppo_source_id": source.source_id, "oppo_fingerprint": source.fingerprint(),
            "source_group_policy": "named" if index else "default_unclassified"})
    require(len({r["source_id"] for r in refs}) == 2)
    return refs, sources, baseline, {"files": {k: {"path": paths[k], "sha256": digest(v)} for k, v in raw.items()},
        "migration_task": task.model_dump(mode="json"), "source_capture": SOURCE_REFERENCE}


def acquire(provider, refs, sources, baseline, root):
    original_json, tags, downloads, notes = provider._json, [], set(), []
    def read(path, payload, **kwargs):
        result = original_json(path, payload, **kwargs)
        if path == "notetag/query":
            tags.append(result)
        return result
    provider._json = read
    scoped = {r["source_id"] for r in refs}
    all_ids = set(baseline["target"]) | scoped
    def metadata(rows):
        selected = [row for row in rows if row.get("guid") in all_ids]
        require(len(selected) == len(all_ids) and len({r["guid"] for r in selected}) == len(all_ids)
            and all(r.get("kind") == "note" and isinstance(r.get("etag"), str) and r["etag"] for r in selected))
        return {row["guid"]: row["etag"] for row in selected}
    try:
        with read_guard(provider, refs, downloads):
            require(provider.probe() == refs[0]["account"])
            groups = {str(t["uuid"]): t["name"] for t in parse_tags(tags[-1])}
            listing, rows = provider._listing()
            etags = metadata(rows)
            for ref, source in zip(refs, sources, strict=True):
                guid = ref["source_id"]
                detail = provider._json("note/query", {"guid": guid, "kind": "note",
                    "ctagNoteInfo": listing.get("ctagNoteInfo", ""), "startCursor": listing.get("startCursor", "")})["rspInfo"]
                require(detail.get("guid") == guid and detail.get("kind") == "note" and detail.get("etag") == etags[guid])
                rows = detail.get("attachments")
                require(isinstance(rows, list) and len(rows) == 2 and all(isinstance(r, dict) for r in rows)
                    and sorted(str(r.get("assetId")) for r in rows) == ref["asset_ids"]
                    and sorted(str(r.get("usage")) for r in rows) == ref["usages"])
                assets = []
                with provider._files.note_download_scope(guid) as download:
                    for row in rows:
                        # OPPO originals have extensionless cloud names; unique size narrows
                        # the expected image, then the downloader requires its exact SHA-256.
                        expected = [a for a in source.attachments if a.size == row.get("resourceLength")]
                        require(type(row.get("resourceLength")) is int and len(expected) == 1
                            and all(re.fullmatch(r"[A-Za-z0-9_$-]{1,150}", str(v)) for v in
                                (guid, row["assetId"], row.get("versionId", ""))))
                        route = f"/v2/dataSync/callback/v1/1001/kind/note/record/{quote(guid, safe='')}"
                        route += f"/assets/{quote(row['assetId'], safe='')}/revisions/{quote(row['versionId'], safe='')}"
                        downloads.add("/proxy/v1/download/" + quote(route, safe=""))
                        asset = Attachment(id=row["assetId"], name=row["usage"], kind="image")
                        download(asset, row, SimpleNamespace(check_cancel=lambda: None), expected=expected[0])
                        assets.append(asset)
                note = parse_entry(detail, ref["account"], groups, assets)
                require(note.source_folder_id == ref["group_id"] and not note.warnings
                    and isinstance(note.source_folder_name, str)
                    and re.fullmatch(re.escape(source.source_folder_name) + r"(?: \(\d{1,4}\)){0,6}", note.source_folder_name))
                warnings = encode_body(source, {a.id: "verified" for a in source.attachments})[2]
                require(_module(root, "live-matrix-job").compare_note(source, note, warnings))
                notes.append(note)
            require(provider.probe() == refs[0]["account"])
            final_groups = {str(t["uuid"]): t["name"] for t in parse_tags(tags[-1])}
            _, final_rows = provider._listing()
            require(metadata(final_rows) == etags
                and all(final_groups.get(ref["group_id"]) == groups.get(ref["group_id"]) for ref in refs))
            return notes, etags
    finally:
        provider._json = original_json


def capture(jars, root):
    directory = root / ".private/evidence" / ("huawei-BI4-readback-" + uuid4().hex)
    directory.mkdir(parents=True)
    report = {"kind": OPERATION, "batch_id": BATCH, "status": "started", "scope_complete": False,
        "formal_acceptance": False, "official_rendering": "pending", "source_post_readback": "pending",
        "whole_account_snapshot": False, "original_22_body_integrity": "not_rechecked",
        "prewrite_listing_etags_available": False, "cloud_writes": 0, "cache_writes": 0, "receipt_writes": 0,
        "captured_started_at": datetime.now(timezone.utc).isoformat(), "events": []}
    provider = None
    try:
        database = root / ".private/session-lab/notes.sqlite"
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            refs, sources, baseline, evidence = bindings(_ReadOnlyStore(db), root)
        report.update(bindings=refs, evidence_bindings=evidence)
        resource_root = directory / "resources"
        provider = create_provider("huawei", jars, resource_root)
        with observe_huawei_session(provider.transport, request_shape=True) as events:
            report["events"] = events
            notes, etags = acquire(provider, refs, sources, baseline, root)
        payload = json.dumps([n.model_dump(mode="json") for n in notes], ensure_ascii=False).encode("utf-8")
        with (directory / "notes.json").open("xb") as output:
            output.write(payload)
        report.update(status="verified_with_degradation", scope_complete=True,
            target_note_count=2, target_image_count=4, notes_sha256=digest(payload),
            fingerprints={n.source_id: n.fingerprint() for n in notes},
            resource_root=resource_root.relative_to(root).as_posix(),
            etags=etags, original_22_ids_present=True, original_22_metadata_stable_during_readback=True,
            target_content_and_style_verified=True, target_image_bytes_verified=True, target_group_mapping_verified=True,
            issues=[i for i in evidence["migration_task"]["issues"]])
    except BridgeError as error:
        report.update(status="blocked", code=error.code)
    except Exception as error:
        report.update(status="failed", code="BI4_readback_error", error_type=type(error).__name__)
    finally:
        if provider is not None:
            provider.close()
        report["captured_finished_at"] = datetime.now(timezone.utc).isoformat()
        path = directory / "proof.json"
        with path.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
    return {key: report[key] for key in ("kind", "status", "scope_complete", "formal_acceptance",
        "whole_account_snapshot", "original_22_body_integrity", "cloud_writes", "cache_writes", "receipt_writes")} | {
        "code": report.get("code"), "proof": {"path": path.relative_to(root).as_posix(), "sha256": digest(path.read_bytes())}}


def verify_target(reference, store, root):
    """Validate independent evidence and its exact receipts without reading the note cache."""
    require(isinstance(reference, dict) and set(reference) == {"path", "sha256"}
        and re.fullmatch(r"\.private/evidence/huawei-BI4-readback-[0-9a-f]{32}/proof\.json", str(reference.get("path", ""))))
    path = confined(root, reference["path"])
    raw = path.read_bytes()
    require(digest(raw) == reference["sha256"])
    proof = json.loads(raw)
    require(proof.get("kind") == OPERATION and proof.get("batch_id") == BATCH
        and proof.get("status") == "verified_with_degradation" and proof.get("scope_complete") is True
        and proof.get("formal_acceptance") is False and proof.get("whole_account_snapshot") is False
        and proof.get("original_22_body_integrity") == "not_rechecked"
        and proof.get("prewrite_listing_etags_available") is False
        and all(type(proof.get(k)) is int and proof[k] == 0 for k in ("cloud_writes", "cache_writes", "receipt_writes"))
        and all(proof.get(k) is True for k in ("original_22_ids_present", "original_22_metadata_stable_during_readback",
            "target_content_and_style_verified", "target_image_bytes_verified", "target_group_mapping_verified")))
    started, finished = (datetime.fromisoformat(proof[k]) for k in ("captured_started_at", "captured_finished_at"))
    require(started.tzinfo and finished.tzinfo and started <= finished <= datetime.now(timezone.utc))
    refs, sources, baseline, evidence = bindings(store, root)
    require(proof.get("bindings") == refs and proof.get("evidence_bindings") == evidence
        and proof.get("issues") == evidence["migration_task"]["issues"]
        and proof.get("target_note_count") == 2 and proof.get("target_image_count") == 4
        and set(proof.get("etags", {})) == set(baseline["target"]) | {r["source_id"] for r in refs}
        and all(isinstance(v, str) and v for v in proof["etags"].values())
        and proof.get("resource_root") == (path.parent / "resources").relative_to(root).as_posix())
    raw = path.with_name("notes.json").read_bytes()
    require(digest(raw) == proof.get("notes_sha256"))
    notes = [NoteDocument.model_validate(value) for value in json.loads(raw)]
    require(len(notes) == 2 and proof.get("fingerprints") == {n.source_id: n.fingerprint() for n in notes})
    for note, ref, source in zip(notes, refs, sources, strict=True):
        require((note.platform, note.account_id, note.source_id, note.source_folder_id)
            == ("huawei", ref["account"], ref["source_id"], ref["group_id"])
            and not note.warnings and len(note.attachments) == 2
            and sorted(a.id for a in note.attachments) == ref["asset_ids"]
            and sorted(a.name for a in note.attachments) == ref["usages"]
            and isinstance(note.source_folder_name, str)
            and re.fullmatch(re.escape(source.source_folder_name) + r"(?: \(\d{1,4}\)){0,6}", note.source_folder_name))
        for asset in note.attachments:
            data = confined(path.parent / "resources", asset.local_path or "").read_bytes()
            require(asset.kind == "image" and len(data) == asset.size and digest(data) == asset.sha256)
        warnings = encode_body(source, {a.id: "verified" for a in source.attachments})[2]
        require(_module(root, "live-matrix-job").compare_note(source, note, warnings))
    return notes


def browser_target(scope, root, account):
    """Build a renderer scope from the independent readback, never a full cache."""
    from huawei_native_scope import render_expectations
    from PIL import Image

    require(isinstance(scope, dict) and set(scope) == {"manifest", "index", "readback_proof"}
        and scope.get("manifest") == "oppo-huawei-BI4-job.json"
        and type(scope.get("index")) is int and scope["index"] in (0, 1))
    reference = scope["readback_proof"]
    with closing(sqlite3.connect((root / ".private/session-lab/notes.sqlite").resolve().as_uri()
            + "?mode=ro", uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        note = verify_target(reference, _ReadOnlyStore(db), root)[scope["index"]]
    require(note.account_id == account and 0 < len(note.plain_text) <= 100000)
    path = confined(root, reference["path"])
    proof = json.loads(path.read_text("utf-8"))
    images = []
    for asset in note.attachments:
        data = confined(path.parent / "resources", asset.local_path or "").read_bytes()
        with Image.open(io.BytesIO(data)) as picture:
            width, height = picture.size
            picture.verify()
        images.append({"width": width, "height": height})
    source_id = proof["bindings"][scope["index"]]["oppo_source_id"]
    return {"scopeVersion": 1, "scopeLabel": "oppo-huawei-BI4:" + str(scope["index"]),
        "fixtureId": note.source_id, "group_id": note.source_folder_id,
        "group_name": note.source_folder_name, "title": note.display_title,
        **render_expectations(note), "image_specs": images, "target_fingerprint": note.fingerprint(),
        "source_degradation_count": sum(i["note_id"] == source_id for i in proof["issues"]),
        "evidence_sha256": {reference["path"]: reference["sha256"],
            path.with_name("notes.json").relative_to(root).as_posix(): proof["notes_sha256"],
            **{item["path"]: item["sha256"] for item in proof["evidence_bindings"]["files"].values()}}}

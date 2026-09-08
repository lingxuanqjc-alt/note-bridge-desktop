"""Capture only the two confirmed direct Huawei seeds; never create an account snapshot."""
import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from urllib.parse import quote, urlsplit
from uuid import uuid4

from huawei_session_diagnostics import observe_huawei_session
from xiaomi_browser_scope import _module, _ReadOnlyStore

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, Block, NoteDocument, account_fingerprint
from note_bridge.operations import receipt_key
from note_bridge.paths import confined
from note_bridge.providers.factory import create_provider
from note_bridge.providers.huawei import encode_body, parse_entry
from note_bridge.providers.huawei_groups import folder_key, parse_tags

MANIFESTS = ("huawei-group-K2-job.json", "huawei-complex-BD2-job.json")
FIXTURES = ("fixture-20260906-huawei-group-K2", "fixture-20260907-huawei-complex-BD2")
OPERATION = "huawei_scoped_source_capture"
PATH_PATTERN = r"\.private/evidence/huawei-fixed-source-[0-9a-f]{32}/proof\.json"


def require(condition, code="invalid_huawei_source_scope"):
    if not condition:
        raise BridgeError(code, "华为固定两条来源的身份、回执、内容或读取范围未通过核对。")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fixture(root, job):
    # The normal builder can create fixture files. This path only reads existing originals.
    source = _module(root, "live-fixture-job").fixture({**job, "with_images": False})
    for index, extension in enumerate(("png", "jpg")):
        relative = f"fixtures/vivo-upload-synthetic-{index}.{extension}"
        path = confined(root / ".private/session-lab/resources", relative)
        data = path.read_bytes()
        asset = Attachment(id=f"synthetic-image-{index}", name=path.name, kind="image",
            mime="image/png" if index == 0 else "image/jpeg", size=len(data),
            sha256=digest(data), local_path=relative)
        source.attachments.append(asset)
        source.blocks.insert(1 + index * 3, Block(kind="attachment", attachment_id=asset.id))
    return source


def bindings(store, root, fixture_builder=None):
    result, sources = [], []
    for index, (manifest, identifier) in enumerate(zip(MANIFESTS, FIXTURES, strict=True)):
        raw = (root / ".private/checkpoints" / manifest).read_bytes()
        old_raw = (root / ".private/evidence" / (identifier + ".json")).read_bytes()
        job, old = json.loads(raw), json.loads(old_raw)
        require(job.get("kind") == "independent-cloud-write-smoke" and job.get("id") == identifier
            and job.get("target") == "huawei" and job.get("armed") is False
            and job.get("from_cloud_fixture") is None and job.get("with_images") is True
            and job.get("with_group") is True and (job.get("content_case") == "complex") == bool(index)
            and re.fullmatch(r"[0-9a-f]{64}", str(job.get("expected_account", ""))))
        require(old.get("kind") == job["kind"] and old.get("platform") == "huawei"
            and old.get("fixture_id") == identifier and old.get("receipt_confirmed") is True
            and old.get("cloud_resource_receipt_states") == ["linked"] * 4)
        if index:
            require(old.get("status") == "readback_pending")
        else:
            require(old.get("status") in ("verified", "verified_with_degradation")
                and all(old.get(k) is True for k in ("group_mapping_verified", "image_bytes_verified",
                                                    "original_notes_unchanged", "content_and_style_verified")))
        seed = fixture_builder(job) if fixture_builder else fixture(root, job)
        account = job["expected_account"]
        require(seed.source_id == identifier and seed.account_id == "synthetic-fixture-source"
            and len(seed.attachments) == 2 and all(a.kind == "image" for a in seed.attachments))
        key = receipt_key(seed, SimpleNamespace(spec=SimpleNamespace(id="huawei"), account_id=account))
        receipt, group = store.receipt(key), store.receipt(folder_key(seed, account))
        # Browser scope stores use both tuple and sqlite3.Row cursors.
        with store.connection() as db:
            resources = [{"resource_id": row[0], "status": row[1]} for row in db.execute(
                "SELECT resource_id,status FROM resource_receipts WHERE receipt_key=? ORDER BY resource_id", (key,))]
        require(receipt and receipt["status"] == "confirmed" and len(receipt["remote_ids"]) == 1
            and group and group["status"] == "confirmed" and len(group["remote_ids"]) == 1
            and len(resources) == 4 and all(r["status"] == "linked" for r in resources))
        ids = sorted(r["resource_id"] for r in resources if not r["resource_id"].endswith((".png", ".jpg")))
        usages = sorted(r["resource_id"] for r in resources if r["resource_id"].endswith((".png", ".jpg")))
        require(len(set(ids)) == len(set(usages)) == 2
            and {Path(v).suffix for v in usages} == {".png", ".jpg"})
        require(all(re.fullmatch(r"[A-Za-z0-9_$-]{1,150}", str(v)) for v in [receipt["remote_ids"][0], *ids]))
        sources.append(seed)
        result.append({"manifest": manifest, "manifest_sha256": digest(raw), "evidence_sha256": digest(old_raw),
            "account": account, "source_id": receipt["remote_ids"][0], "group_id": group["remote_ids"][0],
            "receipt_key": key, "asset_ids": ids, "usages": usages, "seed_fingerprint": seed.fingerprint()})
    require(len({b["account"] for b in result}) == 1 and len({b["source_id"] for b in result}) == 2
        and len({b["group_id"] for b in result}) == 1 and sources[0].display_title == sources[1].display_title)
    return result, sources


def check_note(note, binding, source, root):
    require((note.platform, note.account_id, note.source_id, note.source_folder_id) ==
        ("huawei", binding["account"], binding["source_id"], binding["group_id"]) and not note.warnings)
    require(isinstance(note.source_folder_name, str) and len(note.source_folder_name) <= 128
        and re.fullmatch(re.escape(source.source_folder_name) + r"(?: \(\d{1,4}\)){0,6}", note.source_folder_name))
    require(len(note.attachments) == 2 and sorted(a.id for a in note.attachments) == binding["asset_ids"]
        and sorted(a.name for a in note.attachments) == binding["usages"])
    for asset in note.attachments:
        path = confined(root / ".private/session-lab/resources", asset.local_path or "")
        require(asset.kind == "image" and path.is_file() and path.stat().st_size == asset.size
            and digest(path.read_bytes()) == asset.sha256)
    _, _, warnings = encode_body(source, {a.id: "verified" for a in source.attachments})
    require(_module(root, "live-matrix-job").compare_note(source, note, warnings))


@contextmanager
def read_guard(provider, refs, downloads):
    """Limit physical requests as well as high-level operations, including automatic retries."""
    original = provider.transport.session.request
    counts, started = {}, monotonic()
    ids = {b["source_id"] for b in refs}
    account = refs[0]["account"]
    limits = {"/notepad/notetag/query": 2, "/notepad/simplenote/query": 2,
        "/html/getAboutGet": 1, "/html/getCommonParam": 1,
        "/proxyserver/driveFileProxy/preProcess": 2}

    def request(method, url, **kwargs):
        parsed, payload = urlsplit(url), kwargs.get("json", {})
        path = parsed.path
        require(parsed.scheme == "https" and parsed.hostname == "cloud.huawei.com"
            and monotonic() - started < 40 and not kwargs.get("files"))
        uid = provider.transport.cookie("userId")
        require(uid and account_fingerprint("huawei", uid) == account, "account_changed")
        if method == "POST" and path == "/notepad/note/query":
            require(payload.get("guid") in ids and payload.get("kind") == "note"
                and set(payload) <= {"guid", "kind", "ctagNoteInfo", "startCursor", "traceId"})
            key, limit = path + ":" + payload["guid"], 1
        elif method == "POST" and path in limits:
            clean = {k: v for k, v in payload.items() if k != "traceId"}
            expected = {"/notepad/notetag/query": {"index": 0},
                "/notepad/simplenote/query": {"index": 0, "status": 0, "guids": ""},
                "/html/getAboutGet": {"moduleType": "notepad"}, "/html/getCommonParam": {},
                "/proxyserver/driveFileProxy/preProcess":
                    {"needToSignUrl": "", "httpMethod": "GET", "generateSignFlag": False}}
            require(clean == expected[path])
            key, limit = path, limits[path]
        else:
            require(method == "GET" and path in downloads)
            key, limit = path, 1
        require(counts.get(key, 0) < limit, "scoped_request_limit")
        counts[key] = counts.get(key, 0) + 1
        kwargs["timeout"] = (3, 6)
        response = original(method, url, **kwargs)
        uid = provider.transport.cookie("userId")
        require(uid and account_fingerprint("huawei", uid) == account, "account_changed")
        return response

    provider.transport.session.request = request
    try:
        yield
    finally:
        provider.transport.session.request = original


def acquire(provider, refs, sources, root, capture_id):
    # Reuse probe's tag response instead of querying the same metadata again.
    original_json, tag_responses = provider._json, []
    def read(path, payload, **kwargs):
        result = original_json(path, payload, **kwargs)
        if path == "notetag/query":
            tag_responses.append(result)
        return result
    provider._json = read
    downloads, notes = set(), []
    try:
        with read_guard(provider, refs, downloads):
            require(provider.probe() == refs[0]["account"], "account_changed")
            groups = {str(t["uuid"]): t["name"] for t in parse_tags(tag_responses[-1])}
            listing, rows = provider._listing()
            selected = {r["guid"]: r for r in rows if r["guid"] in {b["source_id"] for b in refs}}
            require(len(selected) == 2 and all(r.get("kind") == "note" and isinstance(r.get("etag"), str)
                                              and r["etag"] for r in selected.values()))
            etags = {k: r["etag"] for k, r in selected.items()}
            for binding, source in zip(refs, sources, strict=True):
                guid = binding["source_id"]
                detail = provider._json("note/query", {"guid": guid, "kind": "note",
                    "ctagNoteInfo": listing.get("ctagNoteInfo", ""),
                    "startCursor": listing.get("startCursor", "")})["rspInfo"]
                require(detail.get("guid") == guid and detail.get("kind") == "note" and detail.get("etag") == etags[guid])
                metadata = detail.get("attachments")
                require(isinstance(metadata, list) and len(metadata) == 2 and all(isinstance(r, dict) for r in metadata)
                    and sorted(str(r.get("assetId")) for r in metadata) == binding["asset_ids"]
                    and sorted(str(r.get("usage")) for r in metadata) == binding["usages"])
                assets = []
                with provider._files.note_download_scope(guid) as download:
                    for row in metadata:
                        expected = [a for a in source.attachments if Path(a.name).suffix == Path(row["usage"]).suffix]
                        require(len(expected) == 1 and type(row.get("resourceLength")) is int
                            and row["resourceLength"] == expected[0].size
                            and all(re.fullmatch(r"[A-Za-z0-9_$-]{1,150}", str(v)) for v in
                                    (guid, row["assetId"], row.get("versionId", ""))))
                        route = f"/v2/dataSync/callback/v1/1001/kind/note/record/{quote(guid, safe='')}"
                        route += f"/assets/{quote(row['assetId'], safe='')}/revisions/{quote(row['versionId'], safe='')}"
                        downloads.add("/proxy/v1/download/" + quote(route, safe=""))
                        asset = Attachment(id=row["assetId"], name=row["usage"], kind="image")
                        download(asset, row, SimpleNamespace(check_cancel=lambda: None), expected=expected[0])
                        asset.local_path = f"scoped-sources/{capture_id}/" + asset.local_path
                        assets.append(asset)
                note = parse_entry(detail, binding["account"], groups, assets)
                check_note(note, binding, source, root)
                notes.append(note)
            require(provider.probe() == refs[0]["account"], "account_changed")
            final_groups = {str(t["uuid"]): t["name"] for t in parse_tags(tag_responses[-1])}
            _, final_rows = provider._listing()
            require({r["guid"]: (r.get("kind"), r.get("etag")) for r in final_rows if r["guid"] in etags}
                == {guid: ("note", etag) for guid, etag in etags.items()})
            require(all(final_groups.get(b["group_id"]) == groups.get(b["group_id"]) for b in refs))
            return notes, etags
    finally:
        provider._json = original_json


def capture(jars, root):
    capture_id = "huawei-fixed-source-" + uuid4().hex
    directory = root / ".private/evidence" / capture_id
    directory.mkdir(parents=True)
    report = {"kind": OPERATION, "status": "started", "scope_complete": False,
        "whole_account_snapshot": False, "original_17_integrity": "pending", "formal_acceptance": False,
        "cloud_writes": 0, "cache_writes": 0, "receipt_writes": 0,
        "captured_started_at": datetime.now(timezone.utc).isoformat(), "events": []}
    provider = None
    try:
        database = root / ".private/session-lab/notes.sqlite"
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            refs, sources = bindings(_ReadOnlyStore(db), root)
        provider = create_provider("huawei", jars, root / ".private/session-lab/resources/scoped-sources" / capture_id)
        with observe_huawei_session(provider.transport, request_shape=True) as events:
            report["events"] = events
            notes, etags = acquire(provider, refs, sources, root, capture_id)
        payload = json.dumps([n.model_dump(mode="json") for n in notes], ensure_ascii=False).encode("utf-8")
        with (directory / "notes.json").open("xb") as output:
            output.write(payload)
        report.update(status="captured", scope_complete=True, bindings=refs, etags=etags,
            notes_sha256=digest(payload), fingerprints={n.source_id: n.fingerprint() for n in notes})
    except BridgeError as error:
        report.update(status="blocked", code=error.code)
    except Exception as error:
        report.update(status="failed", code="scoped_capture_error", error_type=type(error).__name__)
    finally:
        if provider is not None:
            provider.close()
        report["captured_finished_at"] = datetime.now(timezone.utc).isoformat()
        with (directory / "proof.json").open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
    return {k: report[k] for k in ("kind", "status", "scope_complete", "whole_account_snapshot",
        "original_17_integrity", "cloud_writes", "cache_writes", "receipt_writes")} | {
        "proof": {"path": (directory / "proof.json").relative_to(root).as_posix(),
                  "sha256": digest((directory / "proof.json").read_bytes())}, "event_count": len(report["events"])}


def verify_source(reference, store, root, fixture_builder=None):
    require(isinstance(reference, dict) and set(reference) == {"path", "sha256"}
        and re.fullmatch(PATH_PATTERN, str(reference.get("path", "")))
        and re.fullmatch(r"[0-9a-f]{64}", str(reference.get("sha256", ""))))
    path = confined(root, reference["path"])
    raw = path.read_bytes()
    require(digest(raw) == reference["sha256"])
    proof = json.loads(raw)
    require(proof.get("kind") == OPERATION and proof.get("status") == "captured"
        and proof.get("scope_complete") is True and proof.get("whole_account_snapshot") is False
        and proof.get("original_17_integrity") == "pending" and proof.get("formal_acceptance") is False
        and all(type(proof.get(k)) is int and proof[k] == 0 for k in ("cloud_writes", "cache_writes", "receipt_writes")))
    start, end = (datetime.fromisoformat(proof[k]) for k in ("captured_started_at", "captured_finished_at"))
    require(start.tzinfo is not None and end.tzinfo is not None and start <= end <= datetime.now(timezone.utc))
    refs, sources = bindings(store, root, fixture_builder)
    require(proof.get("bindings") == refs)
    raw_notes = path.with_name("notes.json").read_bytes()
    require(digest(raw_notes) == proof.get("notes_sha256"))
    notes = [NoteDocument.model_validate(n) for n in json.loads(raw_notes)]
    require(len(notes) == 2 and proof.get("fingerprints") == {n.source_id: n.fingerprint() for n in notes}
        and set(proof.get("etags", {})) == {b["source_id"] for b in refs}
        and all(isinstance(v, str) and v for v in proof["etags"].values()))
    for note, binding, source in zip(notes, refs, sources, strict=True):
        require(all(a.local_path and a.local_path.startswith(f"scoped-sources/{path.parent.name}/") for a in note.attachments))
        check_note(note, binding, source, root)
    return notes

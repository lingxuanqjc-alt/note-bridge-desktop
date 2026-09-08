import importlib
import io
import json
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest
import requests
from PIL import Image

from note_bridge.errors import BridgeError
from note_bridge.models import PlatformId, TaskReport, TaskStatus, account_fingerprint
from note_bridge.providers.huawei import HuaweiProvider, encode_body
from note_bridge.providers.huawei_groups import folder_key
from note_bridge.providers.huawei_transport import HuaweiTransport
from note_bridge.storage import Store

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
helper = importlib.import_module("huawei_bi4_readback")
source_helper = importlib.import_module("huawei_scoped_source")


def setup(tmp_path, monkeypatch, failure=None):
    private = tmp_path / ".private"
    for part in ("checkpoints", "evidence", "session-lab/resources/fixtures"):
        (private / part).mkdir(parents=True)
    real_module = helper._module
    monkeypatch.setattr(helper, "_module", lambda _, name: real_module(ROOT, name))
    monkeypatch.setattr(source_helper, "_module", lambda _, name: real_module(ROOT, name))
    images = []
    for index, (fmt, ext) in enumerate((("PNG", "png"), ("JPEG", "jpg"))):
        stream = io.BytesIO()
        Image.new("RGB", (4, 2), "purple").save(stream, format=fmt)
        images.append(stream.getvalue())
        (private / f"session-lab/resources/fixtures/vivo-upload-synthetic-{index}.{ext}").write_bytes(images[-1])
    account = account_fingerprint("huawei", "synthetic-huawei-user")
    target = SimpleNamespace(spec=SimpleNamespace(id="huawei"), account_id=account)
    store = Store(private / "session-lab/notes.sqlite")
    sources, details, entries, issues = [], {}, [], []
    for index in range(2):
        source = source_helper.fixture(tmp_path, {"kind": "independent-cloud-write-smoke", "id": f"synthetic-{index}", "target": "huawei",
            "title": f"笔记互迁验收 · synthetic {index}", "with_group": True, "content_case": "complex" if index else None})
        source.platform, source.account_id = PlatformId.OPPO, "b" * 64
        source.source_folder_id = "named-group-I" if index else "00000000_0000_0000_0000_000000000000"
        source.source_folder_name = "笔记互迁分组验收 I" if index else "未分类"
        for i, asset in enumerate(source.attachments):
            asset.name, asset.mime = f"extensionless-oppo-cloud-{i}", "application/octet-stream"
        sources.append(source)
        key = store.begin_migration_write(source, target)
        guid, group = f"confirmed-BI4-{index}", f"confirmed-group-{index}"
        metadata = [{"assetId": f"synthetic-asset-{index}-{i}", "usage": f"synthetic-upload-{index}-{i}.{ext}",
            "versionId": "version-1", "resourceLength": len(images[i]), "resourceType": 0}
            for i, ext in enumerate(("png", "jpg"))]
        for row in metadata:
            for value in (row["assetId"], row["usage"]):
                store.save_resource_receipt(key, value, "uploaded")
        store.save_receipt(key, "confirmed", [guid])
        store.save_receipt(folder_key(source, account), "confirmed", [group])
        _, markup, warnings = encode_body(source, {a.id: row["usage"] for a, row in zip(source.attachments, metadata)})
        issues.extend({"note_id": source.source_id, "code": "format_downgrade", "message": w} for w in warnings)
        details[guid] = {"guid": guid, "kind": "note", "etag": "synthetic-version-1", "attachments": metadata,
            "data": json.dumps({"content": {"html_content": markup, "title": source.title, "tag_id": group}})}
        entries.append({"title": source.display_title, "with_images": True, "with_group": True,
            "from_cloud_fixture": {"platform": "oppo", "account": source.account_id, "source_id": source.source_id,
                "fingerprint": source.fingerprint(), "scoped_proof": helper.SOURCE_REFERENCE,
                "manifest": "oppo-complex-OR1-job.json" if index else "fixture-20260906-oppo-image-F2-consumed-job.json"}})
    monkeypatch.setattr(helper, "verify_source", lambda *args: [n.model_copy(deep=True) for n in sources])
    common = {"source_scope": "fixed_oppo_capture", "source_capture": helper.SOURCE_REFERENCE,
        "whole_source_account_snapshot": False}
    job = {"kind": "cloud-matrix-batch", "id": helper.BATCH, "target": "huawei", "armed": False,
        "source_policy": "direct_seed_only", "oppo_scope": "OR1_fixed_direct_12", "expected_account": account, "sources": entries}
    baseline = {**common, "target": {f"private-original-{i}": "a" * 64 for i in range(22)},
        "source": {n.source_id: n.fingerprint() for n in sources}}
    old = {**common, "batch_id": helper.BATCH, "status": "needs_review", "code": "matrix_read_incomplete",
        "source": "oppo", "target": "huawei", "succeeded": 2, "skipped": 0, "before_count": 22,
        "baseline_file": f".private/checkpoints/{helper.BATCH}-before.json", "formal_acceptance": False, "items": [], "issues": issues}
    for path, payload in [(private / "checkpoints" / (helper.BATCH + "-consumed-job.json"), job),
        (private / "checkpoints" / (helper.BATCH + "-before.json"), baseline),
        (private / "evidence" / (helper.BATCH + ".json"), old)]:
        path.write_text(json.dumps(payload), "utf-8")
    store.save_task(TaskReport(id=helper.TASK, operation="matrix_migrate", status=TaskStatus.PARTIAL,
        completed=2, total=2, succeeded=2, issues=issues,
        started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:01:00+00:00"))
    calls, providers = [], []
    def create(platform, jars, resources):
        assert platform == "huawei" and resources.parent.parent == private / "evidence"
        session = requests.Session()
        session.cookies.set("userId", "wrong-account" if failure == "account" else "synthetic-huawei-user", domain="cloud.huawei.com", path="/")
        session.cookies.set("shareToken", "synthetic-private-token", domain="cloud.huawei.com", path="/")
        def request(method, url, **kwargs):
            path, payload = urlsplit(url).path, kwargs.get("json", {})
            calls.append((method, path, payload))
            if path == "/notepad/notetag/query":
                value = {"Result": {"code": "0"}, "rspInfo": {"noteList": [{"data": json.dumps({
                    "uuid": f"confirmed-group-{i}", "name": source.source_folder_name, "type": 2, "delete_flag": 0})}
                    for i, source in enumerate(sources)]}}
            elif path == "/notepad/simplenote/query":
                final = sum(p == path for _, p, _ in calls) == 2
                rows = [{"guid": guid, "kind": "note", "etag": "changed" if final and failure == "etag" else "synthetic-version-1"}
                    for guid in [*details, *baseline["target"]]]
                if failure == "missing_original":
                    rows.pop()
                value = {"Result": {"code": "0"}, "rspInfo": {"noteList": rows}}
            elif path == "/notepad/note/query":
                assert payload["guid"] in details, "Original 22 bodies must never be fetched."
                detail = deepcopy(details[payload["guid"]])
                if failure == "detail":
                    detail["guid"] = "private-original-0"
                if failure == "resource":
                    detail["attachments"][0]["assetId"] = "foreign-resource"
                if failure == "size":
                    detail["attachments"][0]["resourceLength"] = 1
                if failure == "group":
                    data = json.loads(detail["data"])
                    data["content"]["tag_id"] = "foreign-group"
                    detail["data"] = json.dumps(data)
                value = {"Result": {"code": "0"}, "rspInfo": detail}
            elif path == "/html/getAboutGet":
                value = {"code": 0, "dataSwitch": 1, "dataVersion": "dataVersion=2.0"}
            elif path == "/html/getCommonParam":
                value = {"code": 0, "fileProxyGrayStrategyVersionValue": "synthetic-version"}
            elif path == "/proxyserver/driveFileProxy/preProcess":
                value = {"code": "0"}
            elif path.startswith("/proxy/v1/download/"):
                route = unquote(path)
                assert route.split("/record/")[1].split("/")[0] in details
                asset = route.split("/assets/")[1].split("/")[0]
                if failure == "network":
                    raise requests.ConnectionError("synthetic-private-token")
                value = b"wrong bytes" if failure == "image" else images[int(asset[-1])]
            else:
                pytest.fail("An unrelated cloud read or any write escaped the scope")
            response = requests.Response()
            response.status_code, response.headers = 200, {}
            response._content = value if isinstance(value, bytes) else json.dumps(value).encode()
            response._content_consumed = True
            response.request = session.prepare_request(requests.Request(method, url, headers=kwargs.get("headers", {})))
            return response
        session.request = request
        provider = HuaweiProvider(HuaweiTransport("https://cloud.huawei.com", ("huawei.com",), session=session), resources)
        providers.append(provider)
        return provider
    monkeypatch.setattr(helper, "create_provider", create)
    return SimpleNamespace(store=store, private=private, sources=sources, calls=calls, account=account, providers=providers)


def test_exact_confirmed_two_images_are_verified_without_refetch_or_cache_change(tmp_path, monkeypatch):
    ctx = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(Store, "notes", lambda *a: pytest.fail("No account cache read"))
    monkeypatch.setattr(Store, "snapshot", lambda *a: pytest.fail("No account snapshot access"))
    original = (ctx.private / "evidence" / (helper.BATCH + ".json")).read_bytes()
    database = (ctx.private / "session-lab/notes.sqlite").read_bytes()
    result = helper.capture([], tmp_path)
    assert result["status"] == "verified_with_degradation", result
    notes = helper.verify_target(result["proof"], ctx.store, tmp_path)
    assert len(notes) == 2 and len(ctx.calls) == 14
    assert sum(p == "/notepad/note/query" for _, p, _ in ctx.calls) == 2
    assert sum(p.startswith("/proxy/v1/download/") for _, p, _ in ctx.calls) == 4
    assert (ctx.private / "session-lab/notes.sqlite").read_bytes() == database
    assert (ctx.private / "evidence" / (helper.BATCH + ".json")).read_bytes() == original
    proof = json.loads((tmp_path / result["proof"]["path"]).read_text("utf-8"))
    assert proof["original_22_body_integrity"] == "not_rechecked"
    assert proof["original_22_metadata_stable_during_readback"] and not proof["prewrite_listing_etags_available"]
    assert [r["source_group_policy"] for r in proof["bindings"]] == ["default_unclassified", "named"]
    assert "synthetic-private-token" not in json.dumps(proof)
    assert not list(ctx.providers[0].transport.session.cookies)
    with sqlite3.connect((ctx.private / "session-lab/notes.sqlite").resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        assert helper.verify_target(result["proof"], helper._ReadOnlyStore(db), tmp_path) == notes


@pytest.mark.parametrize("failure", ["account", "detail", "resource", "size", "group", "image", "network", "etag", "missing_original"])
def test_failed_scoped_read_is_never_promoted_or_retried(tmp_path, monkeypatch, failure):
    ctx = setup(tmp_path, monkeypatch, failure)
    before = (ctx.private / "session-lab/notes.sqlite").read_bytes()
    result = helper.capture([], tmp_path)
    assert result["status"] == "blocked" and not result["scope_complete"], result
    assert (ctx.private / "session-lab/notes.sqlite").read_bytes() == before
    with pytest.raises(BridgeError):
        helper.verify_target(result["proof"], ctx.store, tmp_path)
    if failure == "network":
        assert sum(p.startswith("/proxy/v1/download/") for _, p, _ in ctx.calls) == 1
    if failure in ("account", "missing_original"):
        assert not any(p == "/notepad/note/query" for _, p, _ in ctx.calls)


@pytest.mark.parametrize("tamper", ["armed", "source_fingerprint", "task", "receipt", "context", "resources", "original", "baseline"])
def test_bad_local_binding_blocks_before_network(tmp_path, monkeypatch, tamper):
    ctx = setup(tmp_path, monkeypatch)
    refs = helper.bindings(ctx.store, tmp_path)[0]
    if tamper in ("armed", "source_fingerprint"):
        path = ctx.private / "checkpoints" / (helper.BATCH + "-consumed-job.json")
        job = json.loads(path.read_text("utf-8"))
        if tamper == "armed":
            job["armed"] = True
        else:
            job["sources"][1]["from_cloud_fixture"]["fingerprint"] = "f" * 64
        path.write_text(json.dumps(job), "utf-8")
    elif tamper == "task":
        task = ctx.store.task(helper.TASK)
        task.succeeded = 1
        ctx.store.save_task(task)
    elif tamper == "receipt":
        ctx.store.save_receipt(refs[0]["receipt_key"], "uncertain")
    elif tamper in ("context", "resources"):
        with ctx.store.connection() as db:
            if tamper == "context":
                db.execute("UPDATE receipt_contexts SET target_account=? WHERE key=?", ("f" * 64, refs[0]["receipt_key"]))
            else:
                db.execute("UPDATE resource_receipts SET status='uploaded' WHERE receipt_key=?", (refs[0]["receipt_key"],))
    else:
        path = (ctx.private / "evidence" / (helper.BATCH + ".json") if tamper == "original" else
            ctx.private / "checkpoints" / (helper.BATCH + "-before.json"))
        data = json.loads(path.read_text("utf-8"))
        data["source_capture"] = {"path": "foreign-proof", "sha256": "f" * 64}
        path.write_text(json.dumps(data), "utf-8")
    assert helper.capture([], tmp_path)["status"] == "blocked"
    assert ctx.calls == []


@pytest.mark.parametrize("tamper", ["proof", "notes", "image", "fullbody_claim", "receipt"])
def test_independent_proof_cannot_hide_tampering_or_expand_claim(tmp_path, monkeypatch, tamper):
    ctx = setup(tmp_path, monkeypatch)
    result = helper.capture([], tmp_path)
    assert result["scope_complete"]
    reference = result["proof"]
    path = tmp_path / reference["path"]
    if tamper == "proof":
        path.write_text("{}", "utf-8")
    elif tamper == "notes":
        path.with_name("notes.json").write_text("[]", "utf-8")
    elif tamper == "fullbody_claim":
        proof = json.loads(path.read_text("utf-8"))
        proof["original_22_body_integrity"] = "verified"
        path.write_text(json.dumps(proof), "utf-8")
        reference["sha256"] = helper.digest(path.read_bytes())
    elif tamper == "receipt":
        refs = helper.bindings(ctx.store, tmp_path)[0]
        ctx.store.save_receipt(refs[0]["receipt_key"], "uncertain")
    else:
        notes = helper.verify_target(reference, ctx.store, tmp_path)
        (path.parent / "resources" / notes[0].attachments[0].local_path).write_bytes(b"tampered")
    with pytest.raises(BridgeError):
        helper.verify_target(reference, ctx.store, tmp_path)


def test_native_scope_uses_exact_evidence_resources_and_rejects_other_account_or_index(tmp_path, monkeypatch):
    ctx = setup(tmp_path, monkeypatch)
    result = helper.capture([], tmp_path)
    reference = result["proof"]
    for index in (0, 1):
        request = {"manifest": "oppo-huawei-BI4-job.json", "index": index, "readback_proof": reference}
        scope = helper.browser_target(request, tmp_path, ctx.account)
        assert scope["fixtureId"] == f"confirmed-BI4-{index}"
        assert scope["image_specs"] == [{"width": 4, "height": 2}] * 2
        assert scope["scopeLabel"] == f"oppo-huawei-BI4:{index}"
        assert scope["evidence_sha256"][reference["path"]] == reference["sha256"]
        with pytest.raises(BridgeError):
            helper.browser_target(request, tmp_path, "f" * 64)
    assert len(ctx.calls) == 14, "Native scope construction must only read local verified evidence."
    for index in (-1, 2, True, "0"):
        with pytest.raises(BridgeError):
            helper.browser_target({**request, "index": index}, tmp_path, ctx.account)

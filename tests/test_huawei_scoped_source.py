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
from note_bridge.models import PlatformId, account_fingerprint
from note_bridge.operations import receipt_key
from note_bridge.providers.huawei import HuaweiProvider, encode_body
from note_bridge.providers.huawei_groups import folder_key
from note_bridge.providers.huawei_transport import HuaweiTransport
from note_bridge.storage import Store

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
helper = importlib.import_module("huawei_scoped_source")
select_source = importlib.import_module("cloud_fixture_source").select_source


def setup(tmp_path, monkeypatch, failure=None):
    private = tmp_path / ".private"
    for part in ("checkpoints", "evidence", "session-lab/resources/fixtures"):
        (private / part).mkdir(parents=True)
    real_module = helper._module
    monkeypatch.setattr(helper, "_module", lambda _, name: real_module(ROOT, name))
    image_bytes = []
    for index, (fmt, ext) in enumerate((("PNG", "png"), ("JPEG", "jpg"))):
        stream = io.BytesIO()
        Image.new("RGB", (4, 2), "purple").save(stream, format=fmt)
        image_bytes.append(stream.getvalue())
        (private / f"session-lab/resources/fixtures/vivo-upload-synthetic-{index}.{ext}").write_bytes(stream.getvalue())
    account = account_fingerprint("huawei", "synthetic-huawei-user")
    target = SimpleNamespace(spec=SimpleNamespace(id="huawei"), account_id=account)
    store = Store(private / "session-lab/notes.sqlite")
    store.replace_snapshot("huawei", account, [], False)
    sources, details = [], {}
    for index, (manifest, identifier) in enumerate(zip(helper.MANIFESTS, helper.FIXTURES, strict=True)):
        job = {"kind": "independent-cloud-write-smoke", "id": identifier, "target": "huawei",
            "armed": False, "with_images": True, "with_group": True, "expected_account": account,
            "content_case": "complex" if index else None, "title": "笔记互迁验收 · 华为固定同名"}
        (private / "checkpoints" / manifest).write_text(json.dumps(job), "utf-8")
        old = {"kind": job["kind"], "platform": "huawei", "fixture_id": identifier,
            "status": "readback_pending" if index else "verified_with_degradation",
            "receipt_confirmed": True, "cloud_resource_receipt_states": ["linked"] * 4,
            "group_mapping_verified": True, "image_bytes_verified": True, "original_notes_unchanged": True,
            "content_and_style_verified": True}
        (private / "evidence" / (identifier + ".json")).write_text(json.dumps(old), "utf-8")
        source = helper.fixture(tmp_path, job)
        sources.append(source)
        key = receipt_key(source, target)
        guid = f"synthetic-fixed-{index}"
        metadata = [{"assetId": f"synthetic-asset-{index}-{i}", "usage": f"synthetic-upload-{index}-{i}.{ext}",
            "versionId": "version-1", "resourceLength": len(image_bytes[i]), "resourceType": 0}
            for i, ext in enumerate(("png", "jpg"))]
        store.save_receipt(key, "sending", [guid])
        for row in metadata:
            for value in (row["assetId"], row["usage"]):
                store.save_resource_receipt(key, value, "uploaded")
        store.save_receipt(key, "confirmed", [guid])
        store.save_receipt(folder_key(source, account), "confirmed", ["synthetic-group"])
        _, markup, _ = encode_body(source, {a.id: row["usage"] for a, row in zip(source.attachments, metadata)})
        details[guid] = {"guid": guid, "kind": "note", "etag": "synthetic-version-1", "attachments": metadata,
            "data": json.dumps({"content": {"html_content": markup, "title": source.title,
                                            "tag_id": "synthetic-group"}})}
    calls, providers = [], []
    def create(platform, jars, resources):
        assert platform == "huawei"
        session = requests.Session()
        session.cookies.set("userId", "wrong-account" if failure == "account" else "synthetic-huawei-user",
                            domain="cloud.huawei.com", path="/")
        session.cookies.set("shareToken", "synthetic-private-token", domain="cloud.huawei.com", path="/")
        def request(method, url, **kwargs):
            path, payload = urlsplit(url).path, kwargs.get("json", {})
            calls.append((method, path, payload))
            value, status = {}, 200
            if path == "/notepad/notetag/query":
                value = {"Result": {"code": "0"}, "rspInfo": {"noteList": [{"data": json.dumps({
                    "uuid": "synthetic-group", "name": sources[0].source_folder_name, "type": 2, "delete_flag": 0})}]}}
            elif path == "/notepad/simplenote/query":
                final = sum(p == path for _, p, _ in calls) == 2
                value = {"Result": {"code": "0"}, "rspInfo": {"noteList": [
                    {"guid": guid, "kind": "note", "etag": "changed" if final and failure == "etag" else "synthetic-version-1"}
                    for guid in [*details, "private-other-note-never-read"]]}}
            elif path == "/notepad/note/query":
                assert payload["guid"] in details, "A private or unreceipted note must never be requested."
                detail = deepcopy(details[payload["guid"]])
                if failure == "detail":
                    detail["guid"] = "private-other-note-never-read"
                if failure == "resource":
                    detail["attachments"][0]["assetId"] = "unrelated-resource"
                if failure == "group":
                    data = json.loads(detail["data"])
                    data["content"]["tag_id"] = "unrelated-group"
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
                assert any(asset == r["assetId"] for d in details.values() for r in d["attachments"])
                value = image_bytes[int(asset[-1])]
                if failure == "image":
                    value = b"wrong bytes"
                if failure == "network":
                    raise requests.ConnectionError("secret exception content")
            else:
                pytest.fail("Unexpected cloud operation, including every write, is forbidden")
            response = requests.Response()
            response.status_code, response.headers = status, {}
            response._content = value if isinstance(value, bytes) else json.dumps(value).encode()
            response._content_consumed = True
            response.request = session.prepare_request(requests.Request(method, url, headers=kwargs.get("headers", {})))
            return response
        session.request = request
        provider = HuaweiProvider(HuaweiTransport("https://cloud.huawei.com", ("huawei.com",), session=session), resources)
        providers.append(provider)
        return provider
    monkeypatch.setattr(helper, "create_provider", create)
    return SimpleNamespace(store=store, account=account, sources=sources, calls=calls, providers=providers, private=private)


def test_capture_is_exact_fresh_and_reusable_without_marking_full_cache_complete(tmp_path, monkeypatch):
    ctx = setup(tmp_path, monkeypatch)
    before = (ctx.private / "session-lab/notes.sqlite").read_bytes()
    original = [(ctx.private / "evidence" / (i + ".json")).read_bytes() for i in helper.FIXTURES]
    result = helper.capture([], tmp_path)
    assert result["status"] == "captured", result
    assert len(ctx.calls) == 14
    assert sum(p == "/proxyserver/driveFileProxy/preProcess" for _, p, _ in ctx.calls) == 2
    assert sum(p == "/notepad/note/query" for _, p, _ in ctx.calls) == 2
    assert sum(p.startswith("/proxy/v1/download/") for _, p, _ in ctx.calls) == 4
    assert (ctx.private / "session-lab/notes.sqlite").read_bytes() == before
    assert original == [(ctx.private / "evidence" / (i + ".json")).read_bytes() for i in helper.FIXTURES]
    assert not ctx.store.snapshot("huawei", ctx.account)["complete"]
    assert result["original_17_integrity"] == "pending" and result["whole_account_snapshot"] is False
    notes = helper.verify_source(result["proof"], ctx.store, tmp_path)
    with sqlite3.connect((ctx.private / "session-lab/notes.sqlite").resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        assert helper.verify_source(result["proof"], helper._ReadOnlyStore(db), tmp_path) == notes
    for target in ("wps", "vivo", "xiaomi", "honor", "meizu"):
        for manifest, note in zip(helper.MANIFESTS, notes, strict=True):
            job = {"target": target, "title": note.display_title, "with_images": True, "with_group": True,
                "from_cloud_fixture": {"platform": "huawei", "manifest": manifest, "account": ctx.account,
                    "source_id": note.source_id, "fingerprint": note.fingerprint(), "scoped_proof": result["proof"]}}
            assert select_source(job, ctx.store, tmp_path, lambda _: pytest.fail("No side-effectful builder")) == note
    assert len(ctx.calls) == 14, "All five target preparations reuse the captured version without cloud requests."
    assert not list(ctx.providers[0].transport.session.cookies)
    assert "synthetic-private-token" not in json.dumps(result)


@pytest.mark.parametrize("failure", ["account", "detail", "etag", "resource", "image", "group", "network"])
def test_failed_scope_cannot_become_a_source_or_change_prior_cache(tmp_path, monkeypatch, failure):
    ctx = setup(tmp_path, monkeypatch, failure)
    before = (ctx.private / "session-lab/notes.sqlite").read_bytes()
    result = helper.capture([], tmp_path)
    assert result["scope_complete"] is False and result["status"] == "blocked"
    assert (ctx.private / "session-lab/notes.sqlite").read_bytes() == before
    assert not (tmp_path / result["proof"]["path"]).with_name("notes.json").exists()
    with pytest.raises(BridgeError):
        helper.verify_source(result["proof"], ctx.store, tmp_path)
    if failure in ("detail", "resource", "account"):
        assert not any(path.startswith("/proxy/") for _, path, _ in ctx.calls)
    if failure == "network":
        assert sum(path.startswith("/proxy/") for _, path, _ in ctx.calls) == 1, "Automatic retry cannot escape the exact request budget."


@pytest.mark.parametrize("tamper", ["proof", "docs", "receipt", "image", "manifest", "whole_account"])
def test_frozen_source_revalidates_files_receipts_and_declared_scope(tmp_path, monkeypatch, tamper):
    ctx = setup(tmp_path, monkeypatch)
    result = helper.capture([], tmp_path)
    assert result["status"] == "captured"
    reference = result["proof"]
    path = tmp_path / reference["path"]
    if tamper == "proof":
        path.write_text("{}", "utf-8")
    elif tamper == "whole_account":
        proof = json.loads(path.read_text("utf-8"))
        proof["whole_account_snapshot"] = True
        path.write_text(json.dumps(proof), "utf-8")
        reference["sha256"] = helper.digest(path.read_bytes())
    elif tamper == "docs":
        path.with_name("notes.json").write_text("[]", "utf-8")
    elif tamper == "receipt":
        key = helper.bindings(ctx.store, tmp_path)[0][0]["receipt_key"]
        ctx.store.save_receipt(key, "uncertain")
    elif tamper == "manifest":
        manifest = ctx.private / "checkpoints" / helper.MANIFESTS[0]
        job = json.loads(manifest.read_text("utf-8"))
        job["armed"] = True
        manifest.write_text(json.dumps(job), "utf-8")
    else:
        note = helper.verify_source(reference, ctx.store, tmp_path)[0]
        (ctx.private / "session-lab/resources" / note.attachments[0].local_path).write_bytes(b"changed")
    with pytest.raises(BridgeError):
        helper.verify_source(reference, ctx.store, tmp_path)


def test_uncertain_original_receipt_rejects_capture_before_any_network(tmp_path, monkeypatch):
    ctx = setup(tmp_path, monkeypatch)
    key = helper.bindings(ctx.store, tmp_path)[0][1]["receipt_key"]
    ctx.store.save_receipt(key, "uncertain")
    result = helper.capture([], tmp_path)
    assert result["status"] == "blocked" and ctx.calls == []


def test_batch_rejects_subset_reordering_or_mixed_capture(tmp_path, monkeypatch):
    ctx = setup(tmp_path, monkeypatch)
    capture = helper.capture([], tmp_path)
    notes = helper.verify_source(capture["proof"], ctx.store, tmp_path)
    entries = [{"title": n.display_title, "with_images": True, "with_group": True,
        "from_cloud_fixture": {"platform": "huawei", "manifest": m, "account": n.account_id,
            "source_id": n.source_id, "fingerprint": n.fingerprint(), "scoped_proof": capture["proof"]}}
        for m, n in zip(helper.MANIFESTS, notes, strict=True)]
    matrix = helper._module(ROOT, "live-matrix-job")
    job = {"target": "wps", "source_policy": "direct_seed_only", "sources": entries}
    assert matrix.select_batch(job, ctx.store, tmp_path, lambda _: None) == notes
    for changed in (entries[:1], entries[::-1], deepcopy(entries)):
        if len(changed) == 2 and changed == entries:
            changed[1]["from_cloud_fixture"]["scoped_proof"] = {"path": "other", "sha256": "a" * 64}
        with pytest.raises(BridgeError):
            matrix.select_batch({**job, "sources": changed}, ctx.store, tmp_path, lambda _: None)


@pytest.mark.parametrize("target_state", ["uncertain", "confirmed", "resource_only"])
def test_public_preparation_uses_capture_with_incomplete_cache_and_refuses_known_target_write(
        tmp_path, monkeypatch, target_state):
    ctx = setup(tmp_path, monkeypatch)
    result = helper.capture([], tmp_path)
    prepare = importlib.import_module("direct_batch_preparation")
    monkeypatch.setattr(Store, "notes", lambda *a: pytest.fail("Scoped preparation cannot read the account cache"))
    monkeypatch.setattr(Store, "snapshot", lambda *a: pytest.fail("Scoped preparation cannot depend on global completeness"))
    before = (ctx.private / "session-lab/notes.sqlite").read_bytes()
    # SQLite may remove its WAL/SHM files on the last reader closing; application files must not change.
    sidecars = {ctx.private / ("session-lab/notes.sqlite" + suffix) for suffix in ("-wal", "-shm")}
    def application_files():
        return {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*")
                if path.is_file() and path not in sidecars}
    files_before = application_files()
    captured = helper.verify_source(result["proof"], ctx.store, tmp_path)
    job = prepare.build_prepared_job(captured, result["proof"], target="wps", expected_account="b" * 64,
                                     batch_id="matrix-20260907-huawei-wps-BD999")
    assert job["armed"] is False and job["preparation"]["whole_account_snapshot"] is False
    assert job["preparation"]["original_17_integrity"] == "pending"
    notes = prepare.validate_prepared(job, ctx.store, tmp_path)
    assert notes == captured
    assert (ctx.private / "session-lab/notes.sqlite").read_bytes() == before
    assert application_files() == files_before
    for field, value in (("whole_account_snapshot", True), ("original_17_integrity", "verified")):
        changed = deepcopy(job)
        changed["preparation"][field] = value
        with pytest.raises(ValueError, match="limited source meaning"):
            prepare.validate_prepared(changed, ctx.store, tmp_path)
    destination = SimpleNamespace(spec=SimpleNamespace(id="wps"), account_id="b" * 64)
    key = receipt_key(notes[0], destination)
    if target_state == "resource_only":
        # Simulate an orphan from a legacy record; the normal writer correctly forbids creating one.
        with ctx.store.connection() as db:
            db.execute("INSERT INTO resource_receipts VALUES (?,?,?)", (key, "synthetic-target-resource", "uploaded"))
    else:
        ctx.store.save_receipt(key, target_state, ["synthetic-target-note"] if target_state == "confirmed" else [])
    after_existing_write = (ctx.private / "session-lab/notes.sqlite").read_bytes()
    with pytest.raises(ValueError, match="Existing target"):
        prepare.validate_prepared(job, ctx.store, tmp_path)
    assert (ctx.private / "session-lab/notes.sqlite").read_bytes() == after_existing_write
    assert len(ctx.calls) == 14 and not (ctx.private / "lab-matrix-job.json").exists()


def test_migration_baseline_labels_captured_source_without_reading_global_huawei_cache(tmp_path, monkeypatch):
    from note_bridge.providers.base import CreatedNote, Snapshot
    from note_bridge.providers.meizu_groups import folder_key as meizu_group_key

    ctx = setup(tmp_path, monkeypatch)
    result = helper.capture([], tmp_path)
    notes = helper.verify_source(result["proof"], ctx.store, tmp_path)
    matrix = helper._module(ROOT, "live-matrix-job")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/live-fixture-job.py").write_bytes((ROOT / "scripts/live-fixture-job.py").read_bytes())
    target_account = "c" * 64
    entries = [{"title": n.display_title, "with_images": True, "with_group": True,
        "from_cloud_fixture": {"platform": "huawei", "manifest": m, "account": n.account_id,
            "source_id": n.source_id, "fingerprint": n.fingerprint(), "scoped_proof": result["proof"]}}
        for m, n in zip(helper.MANIFESTS, notes, strict=True)]
    job = {"id": "matrix-20260907-huawei-meizu-BD999", "kind": "cloud-matrix-batch", "target": "meizu",
        "expected_account": target_account, "armed": True, "source_policy": "direct_seed_only", "sources": entries}
    (ctx.private / "lab-matrix-job.json").write_text(json.dumps(job), "utf-8")
    real_notes, rows = Store.notes, []
    def read_cache(store, platform, account):
        assert platform != "huawei", "A source scope must not fall back to reading all cached Huawei notes."
        return real_notes(store, platform, account)
    monkeypatch.setattr(Store, "notes", read_cache)
    def create(note, context):
        remote = "synthetic-target-" + note.source_id
        restored = note.model_copy(update={"platform": PlatformId.MEIZU, "account_id": target_account, "source_id": remote})
        rows.append(restored)
        context.store.save_receipt(meizu_group_key(note, target_account), "confirmed", [restored.source_folder_id])
        return CreatedNote([remote])
    provider = SimpleNamespace(spec=SimpleNamespace(id="meizu"), account_id=target_account, probe=lambda: target_account,
        preflight=lambda _: None, create=create, fetch=lambda _: Snapshot(list(rows), True), close=lambda: None)
    monkeypatch.setattr(matrix, "create_provider", lambda *args: provider)
    report = matrix.run_armed_job("meizu", [], tmp_path)
    assert report["status"] == "api_verified"
    baseline = json.loads((ctx.private / "checkpoints" / (job["id"] + "-before.json")).read_text("utf-8"))
    assert set(baseline["source"]) == {n.source_id for n in notes}
    for value in (baseline, report):
        assert value["source_scope"] == "fixed_huawei_K2_BD2_capture"
        assert value["whole_source_account_snapshot"] is False and value["source_original_17_integrity"] == "pending"
        assert value["source_capture"] == result["proof"]
    assert not ctx.store.snapshot("huawei", ctx.account)["complete"]

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span
from note_bridge.operations import receipt_key
from note_bridge.providers.base import Snapshot
from note_bridge.storage import Store

scripts = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location("huawei_recovery_test", scripts / "recover-huawei-matrix.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


@pytest.mark.parametrize("label", ["../W4", "AA3", None, True, []])
def test_recovery_never_generalizes_to_unreviewed_batches(label):
    with pytest.raises(BridgeError):
        recovery.recovery_scope({"batch": label})


def test_original_recovery_scope_remains_stable_and_new_scope_is_explicit():
    assert recovery.recovery_scope({}) == ("W4", "vivo-huawei-W4-job.json", "matrix-20260906-vivo-huawei-W4")
    assert recovery.recovery_scope({"batch": "AA2"}) == ("AA2", "meizu-huawei-AA2-job.json", "matrix-20260906-meizu-huawei-AA2")
    assert recovery.recovery_scope({"batch": "AA6"}) == ("AA6", "honor-huawei-AA6-job.json", "matrix-20260906-honor-huawei-AA6")
    assert recovery.recovery_scope({"batch": "BC3", "entry_index": 2}) == ("BC3", "xiaomi-huawei-BC3-job.json", "matrix-20260907-xiaomi-huawei-BC3")


@pytest.mark.parametrize("index", [None, True, 0, 1, 3, "2"])
def test_bc3_cannot_repair_a_neighbour_or_an_implicit_index(index):
    with pytest.raises(BridgeError):
        recovery.recovery_scope({"batch": "BC3", "entry_index": index})


def fixture():
    first = Attachment(id="first", name="first.png", kind="image", sha256="a" * 64, size=100)
    second = Attachment(id="second", name="second.jpg", kind="image", sha256="b" * 64, size=200)
    existing = first.model_copy(update={"id": "cloud-first", "name": "prefix_30_20_1700000000000.png"})
    allocated = "prefix_40_50_1700000000001.jpg"
    return (SimpleNamespace(attachments=[first, second]), SimpleNamespace(attachments=[existing]),
            [{"assetId": existing.id, "usage": existing.name}],
            [{"resource_id": existing.id, "status": "uploaded"},
             {"resource_id": existing.name, "status": "uploaded"},
             {"resource_id": allocated, "status": "allocated"}],
            {"first": (30, 20, "png"), "second": (40, 50, "jpg")}, "prefix")


def test_only_missing_image_reuses_the_identified_allocation():
    args = fixture()
    images, missing, usage = recovery.plan_images(*args)
    assert images == {"first": args[1].attachments[0].name}
    assert missing.id == "second" and usage == args[3][-1]["resource_id"]


@pytest.mark.parametrize("uuid", [None, "", "prefix"])
def test_read_detail_uses_body_prefix_with_matching_unstructured_identity(uuid):
    assert recovery.recovery_identity({"luid": "local-id", "uuid": uuid}, {
        "content": {"prefix_uuid": "prefix", "unstruct_uuid": "local-id"}}) == ("prefix", "local-id")


@pytest.mark.parametrize("detail,content", [
    ({"luid": "other"}, {"prefix_uuid": "prefix", "unstruct_uuid": "local-id"}),
    ({"luid": "local-id", "uuid": "other"}, {"prefix_uuid": "prefix", "unstruct_uuid": "local-id"}),
    ({"luid": "local-id"}, {"prefix_uuid": "../prefix", "unstruct_uuid": "local-id"}),
    ({"luid": "local-id"}, {"unstruct_uuid": "local-id"}),
])
def test_conflicting_or_missing_attachment_identity_blocks_recovery(detail, content):
    with pytest.raises(BridgeError):
        recovery.recovery_identity(detail, {"content": content})


@pytest.mark.parametrize("change", ["foreign", "size", "unknown_resource", "uploaded", "metadata", "duplicate"])
def test_ambiguous_or_changed_draft_never_produces_a_repair_plan(change):
    args = fixture()
    if change == "foreign":
        args[3][-1]["resource_id"] = "foreign_40_50_1700000000001.jpg"
    elif change == "size":
        args[1].attachments[0].size += 1
    elif change == "unknown_resource":
        args[3].append({"resource_id": "unknown", "status": "allocated"})
    elif change == "uploaded":
        args[3][-1]["status"] = "uploaded"
    elif change == "metadata":
        args[2][0]["assetId"] = "foreign"
    else:
        args[0].attachments[1] = args[0].attachments[0].model_copy(update={"id": "duplicate"})
    with pytest.raises(BridgeError):
        recovery.plan_images(*args)


def complete_images():
    args = fixture()
    second = args[0].attachments[1].model_copy(update={"id": "cloud-second", "name": args[3][-1]["resource_id"]})
    args[1].attachments.append(second)
    args[2].append({"assetId": second.id, "usage": second.name})
    args[3][-1]["status"] = "uploaded"
    args[3].append({"resource_id": second.id, "status": "uploaded"})
    return args


def test_bc3_reuses_only_the_two_complete_uploaded_images():
    args = complete_images()
    images, missing, usage = recovery.scoped_image_plan("BC3", *args)
    assert set(images) == {"first", "second"} and missing is usage is None


@pytest.mark.parametrize("failure", ["missing_image", "allocated", "unknown_resource", "duplicate_resource", "missing_resource"])
def test_bc3_cannot_fall_through_to_the_older_missing_image_upload_path(failure):
    args = fixture() if failure == "missing_image" else complete_images()
    if failure == "allocated":
        args[3][-1]["status"] = "allocated"
    elif failure == "unknown_resource":
        args[3].append({"resource_id": "unknown", "status": "uploaded"})
    elif failure == "duplicate_resource":
        args[3][-1] = dict(args[3][0])
    elif failure == "missing_resource":
        args[3].pop()
    with pytest.raises(BridgeError):
        recovery.scoped_image_plan("BC3", *args)


def test_existing_w4_missing_image_plan_is_unchanged():
    _, missing, usage = recovery.scoped_image_plan("W4", *fixture())
    assert missing.id == "second" and usage.endswith(".jpg")


@pytest.mark.parametrize("failure", ["missing", "repair_report", "unverified", "wrong_account", "wrong_receipt",
                                     "changed_source", "changed_target", "wrong_index", "upload_required"])
def test_bc3_body_update_requires_the_same_successful_readonly_inspection(tmp_path, failure):
    scope = {"batch_id": "matrix-20260907-xiaomi-huawei-BC3", "entry_index": 2, "account": "account",
             "receipt_key": "receipt", "source_fingerprint": "source", "target_fingerprint": "draft"}
    report = {"kind": "huawei-BC3-scoped-recovery", "mode": "inspect", "status": "plan_verified", "scope": dict(scope),
              "creates": 0, "uploads": 0, "updates": 0, "existing_images": 2, "missing_images": 0}
    if failure == "repair_report":
        report["mode"] = "repair"
    elif failure == "unverified":
        report["status"] = "needs_review"
    elif failure == "upload_required":
        report["missing_images"] = 1
    elif failure != "missing":
        key = {"wrong_account": "account", "wrong_receipt": "receipt_key", "changed_source": "source_fingerprint",
               "changed_target": "target_fingerprint", "wrong_index": "entry_index"}[failure]
        report["scope"][key] = "other"
    path = tmp_path / ".private/evidence/huawei-BC3-recovery-inspect.json"
    path.parent.mkdir(parents=True)
    if failure != "missing":
        path.write_text(json.dumps(report), "utf-8")
    with pytest.raises(BridgeError):
        recovery.require_bc3_inspection(tmp_path, scope)


def bc3_handler_scope(tmp_path, monkeypatch, missing=False):
    private = tmp_path / ".private"
    resources = private / "session-lab/resources"
    resources.mkdir(parents=True)
    (private / "checkpoints").mkdir()
    (private / "evidence").mkdir()
    source = NoteDocument(platform=PlatformId.XIAOMI, account_id="source-account", source_id="source",
                          title="笔记互迁验收 · BC3 第三条", source_folder_id="source-group", source_folder_name="验收分组",
                          blocks=[Block(spans=[Span(text="中文 English 😀", bold=True)])])
    for index, (width, height, extension) in enumerate(((30, 20, "png"), (40, 50, "jpg"))):
        path = resources / f"source-{index}.{extension}"
        Image.new("RGB", (width, height), "purple").save(path)
        payload = path.read_bytes()
        asset = Attachment(id=f"source-image-{index}", name=path.name, local_path=path.name, kind="image",
                           size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        source.attachments.append(asset)
        source.blocks.append(Block(kind="attachment", attachment_id=asset.id))
    draft = source.model_copy(deep=True, update={"platform": PlatformId.HUAWEI, "account_id": "target-account",
                                                "source_id": "existing-cloud-note", "source_folder_id": "target-group"})
    for index, (asset, dimensions) in enumerate(zip(draft.attachments, ((30, 20, "png"), (40, 50, "jpg")), strict=True)):
        width, height, extension = dimensions
        asset.id = f"cloud-image-{index}"
        asset.name = f"prefix_{width}_{height}_{1700000000000 + index}.{extension}"
        draft.blocks[index + 1].attachment_id = asset.id
    all_assets = list(draft.attachments)
    if missing:
        draft.attachments = draft.attachments[:1]
    encoded = {"guid": draft.source_id, "fileList": [], "content": {"title": source.title, "prefix_uuid": "prefix",
                                                                               "unstruct_uuid": "local-id"}}
    calls = []
    class Provider:
        spec = SimpleNamespace(id="huawei")
        account_id = "target-account"
        def __init__(self):
            self.resources = resources
            self._files = SimpleNamespace(upload=self.upload)
        def probe(self):
            return self.account_id
        def fetch(self, context):
            return Snapshot([draft], True)
        def _listing(self):
            return {"ctagNoteInfo": "tag", "startCursor": "cursor"}, [{"guid": draft.source_id}]
        def _json(self, path, data, **kwargs):
            if path == "note/query":
                return {"rspInfo": {"guid": draft.source_id, "etag": "etag", "luid": "local-id", "data": json.dumps(encoded),
                                    "attachments": [{"assetId": a.id, "usage": a.name} for a in draft.attachments]}}
            if path == "notetag/query":
                return {"ctagNoteTag": "tags"}
            assert path == "note/update" and kwargs.get("write") is True
            assert data["reqInfo"]["guid"] == draft.source_id
            calls.append("update-existing")
            return {"code": 0}
        def upload(self, *args, **kwargs):
            calls.append("upload")
            raise AssertionError("BC3 must never upload")
        def close(self):
            pass
    provider = Provider()
    store = Store(private / "session-lab/notes.sqlite")
    key = receipt_key(source, provider)
    store.save_receipt(key, "sending", [draft.source_id])
    for index, asset in enumerate(all_assets):
        if missing and index == 1:
            store.save_resource_receipt(key, asset.name, "allocated")
        else:
            for value in (asset.id, asset.name):
                store.save_resource_receipt(key, value, "uploaded")
    store.save_receipt(key, "uncertain", [draft.source_id])
    job = {"id": "matrix-20260907-xiaomi-huawei-BC3", "target": "huawei", "armed": False,
           "expected_account": provider.account_id, "sources": [{"slot": 0}, {"slot": 1}, {"slot": 2}, {"slot": 3}]}
    (private / "checkpoints/xiaomi-huawei-BC3-job.json").write_text(json.dumps(job), "utf-8")
    for name in ("lab-write-job.json", "lab-matrix-job.json"):
        (private / name).write_text(json.dumps({"armed": False}), "utf-8")
    intent = {"armed": True, "mode": "inspect", "batch": "BC3", "entry_index": 2, "expected_account": provider.account_id,
              "source_fingerprint": source.fingerprint(), "target_fingerprint": draft.fingerprint()}
    (private / "lab-matrix-recovery.json").write_text(json.dumps(intent), "utf-8")
    original_spec = importlib.util.spec_from_file_location
    monkeypatch.setattr(recovery.importlib.util, "spec_from_file_location",
                        lambda name, path: original_spec(name, scripts / Path(path).name))
    def select(entry, *args):
        assert entry == {"slot": 2, "target": "huawei"}
        return source
    monkeypatch.setattr(recovery, "select_source", select)
    monkeypatch.setattr(recovery, "create_provider", lambda *args: provider)
    return SimpleNamespace(store=store, key=key, calls=calls, intent=intent, private=private, draft=draft)


def test_bc3_handler_rejects_missing_image_before_any_write_intent_or_upload(tmp_path, monkeypatch):
    ctx = bc3_handler_scope(tmp_path, monkeypatch, missing=True)
    result = recovery.run([], tmp_path)
    assert result["status"] == "needs_review" and result["code"] == "recovery_scope"
    assert result["creates"] == result["uploads"] == result["updates"] == 0
    assert ctx.calls == [] and ctx.store.receipt(ctx.key + ":BC3-scoped-recovery") is None
    assert ctx.store.receipt(ctx.key)["status"] == "uncertain"


@pytest.mark.parametrize("status,ids", [("confirmed", ["existing-cloud-note"]), ("rejected", []),
                                       ("sending", ["existing-cloud-note"]), ("uncertain", []),
                                       ("uncertain", ["existing-cloud-note", "other-note"])])
def test_bc3_handler_requires_one_existing_uncertain_target_before_fetch_or_write(tmp_path, monkeypatch, status, ids):
    ctx = bc3_handler_scope(tmp_path, monkeypatch)
    ctx.store.save_receipt(ctx.key, status, ids)
    result = recovery.run([], tmp_path)
    assert result["status"] == "needs_review" and result["code"] == "recovery_scope"
    assert ctx.calls == [] and ctx.store.receipt(ctx.key + ":BC3-scoped-recovery") is None
    assert ctx.store.receipt(ctx.key) == {"status": status, "remote_ids": ids}


def test_bc3_handler_inspects_then_updates_only_the_same_existing_note_once(tmp_path, monkeypatch):
    ctx = bc3_handler_scope(tmp_path, monkeypatch)
    result = recovery.run([], tmp_path)
    assert result["status"] == "plan_verified" and ctx.calls == []
    assert ctx.store.receipt(ctx.key)["status"] == "uncertain"
    assert ctx.store.receipt(ctx.key + ":BC3-scoped-recovery") is None
    ctx.intent.update(armed=True, mode="repair")
    (ctx.private / "lab-matrix-recovery.json").write_text(json.dumps(ctx.intent), "utf-8")
    result = recovery.run([], tmp_path)
    assert result["status"] == "verified_with_degradation"
    assert result["creates"] == result["uploads"] == 0 and result["updates"] == 1
    assert ctx.calls == ["update-existing"]
    for key in (ctx.key, ctx.key + ":BC3-scoped-recovery"):
        assert ctx.store.receipt(key) == {"status": "confirmed", "remote_ids": [ctx.draft.source_id]}

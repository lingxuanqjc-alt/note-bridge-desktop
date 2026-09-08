"""No full-cache Vivo source selection; the only new entry is exact historical source-post."""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.models import PlatformId
from note_bridge.operations import receipt_key
from note_bridge.storage import Store

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


MATRIX = module("source_post_matrix", SCRIPTS / "live-matrix-job.py")


def test_vivo_source_selection_cannot_fall_back_to_full_cache(monkeypatch):
    monkeypatch.setattr(MATRIX, "select_source", lambda *a: pytest.fail("Do not load any private cached Vivo note"))
    job = {"target": "meizu", "sources": [{"from_cloud_fixture": {"platform": "vivo"}}]}
    with pytest.raises(BridgeError) as error:
        MATRIX.select_batch(job, None, None, None)
    assert error.value.code == "vivo_source_scope_required"


@pytest.mark.parametrize("change", [None, "private-id", "source-changed", "unknown-target", "partial-read"])
def test_source_post_reads_exact_original_and_preserves_global_incomplete_cache(tmp_path, monkeypatch, change):
    private = tmp_path / ".private"
    for directory in (private / "checkpoints", private / "evidence", tmp_path / "scripts"):
        directory.mkdir(parents=True)
    fixture_path = tmp_path / "scripts/live-fixture-job.py"
    fixture_path.write_bytes((SCRIPTS / "live-fixture-job.py").read_bytes())
    fixture = module("source_post_fixture", fixture_path)
    account = "a" * 64
    original = {"kind": "independent-cloud-write-smoke", "id": "fixture-20260907-vivo-original",
                "target": "vivo", "expected_account": account, "armed": False,
                "title": "笔记互迁验收 · 原始合成样例"}
    original_path = private / "checkpoints/vivo-original-job.json"
    original_path.write_text(json.dumps(original), "utf-8")
    (private / "evidence" / (original["id"] + ".json")).write_text(json.dumps({
        "fixture_id": original["id"], "platform": "vivo", "status": "verified"}), "utf-8")
    seed = fixture.fixture(original)
    actual = seed.model_copy(update={"platform": PlatformId.VIVO, "account_id": account, "source_id": "1" * 32})
    store = Store(private / "session-lab/notes.sqlite")
    provider = SimpleNamespace(spec=SimpleNamespace(id="vivo"), account_id=account, close=lambda: None)
    store.save_receipt(receipt_key(seed, provider), "confirmed", [actual.source_id])
    store.replace_snapshot("vivo", account, [actual.model_copy(update={"source_id": "private-unselected"})], False)
    origin = {"manifest": original_path.name, "platform": "vivo", "account": account,
              "source_id": "9" * 32 if change == "private-id" else actual.source_id, "fingerprint": actual.fingerprint()}
    batch = {"kind": "cloud-matrix-batch", "id": "matrix-20260907-vivo-source-check", "target": "meizu",
             "expected_account": "b" * 64, "armed": False, "sources": [
                 {"title": original["title"], "with_images": False, "with_group": False, "from_cloud_fixture": origin}]}
    manifest = private / "checkpoints/vivo-meizu-job.json"
    manifest.write_text(json.dumps(batch), "utf-8")
    target = SimpleNamespace(spec=SimpleNamespace(id="meizu"), account_id=batch["expected_account"])
    store.save_receipt(receipt_key(actual, target), "uncertain" if change == "unknown-target" else "confirmed", ["target-created"])
    previous_path = private / "evidence" / (batch["id"] + ".json")
    previous_path.write_text(json.dumps({"batch_id": batch["id"], "source": "vivo", "target": "meizu",
        "status": "api_verified", "items": [{"status": "api_verified", "receipt_status": "confirmed",
            "content_verified": True, "group_verified": True,
            "source_id": origin["source_id"], "source_fingerprint": origin["fingerprint"]}]}), "utf-8")
    previous_bytes = previous_path.read_bytes()

    def global_rows():
        with sqlite3.connect(private / "session-lab/notes.sqlite") as db:
            return db.execute("SELECT * FROM notes").fetchall(), db.execute("SELECT * FROM snapshots").fetchall()

    rows_before = global_rows()
    monkeypatch.setattr(Store, "notes", lambda *a: pytest.fail("Never load the private/global note cache"))
    monkeypatch.setattr(Store, "snapshot", lambda *a: pytest.fail("Never consult global completeness"))
    monkeypatch.setattr(Store, "replace_snapshot", lambda *a: pytest.fail("Never certify a partial subset as a full account"))
    calls = []
    if change == "source-changed":
        actual.blocks[0].spans[0].text = "changed synthetic source"

    def read_notes(provider, ids, ctx):
        calls.append(tuple(ids))
        assert ids == ["1" * 32], "Only the exact confirmed original fixture can be requested"
        ctx.update(total=1, completed=1, succeeded=1)
        return SimpleNamespace(account_id=account, exact_ids=tuple(ids), notes=[actual], complete=change != "partial-read")

    import vivo_scoped_readback
    monkeypatch.setattr(vivo_scoped_readback, "read_notes", read_notes)
    monkeypatch.setattr(vivo_scoped_readback, "verify_account", lambda _: account)
    monkeypatch.setattr(MATRIX, "create_provider", lambda *a: provider)
    request = {"platform": "vivo", "operation": "vivo_source_scope", "armed": True,
               "purpose": "source_post", "manifest": manifest.name}
    if change in ("private-id", "unknown-target"):
        with pytest.raises(BridgeError):
            MATRIX.run_vivo_source_scope([], tmp_path, request)
        assert calls == [], "Bad source ancestry or unknown target receipt must stop before cloud reads"
    else:
        result = MATRIX.run_vivo_source_scope([], tmp_path, request)
        assert result["status"] == ("verified" if change is None else "needs_review")
        proof = json.loads((tmp_path / result["proof_file"]).read_text("utf-8"))
        assert proof["synthetic_source_notes_unchanged"] is (change is None)
        assert proof["whole_account_snapshot"] is False and "documents" not in proof
        assert calls == [("1" * 32,)]
        assert MATRIX.run_vivo_source_scope([], tmp_path, request)["status"] == "not_armed"
        assert calls == [("1" * 32,)], "An already consumed source-post cannot repeat network reads"
    assert global_rows() == rows_before and previous_path.read_bytes() == previous_bytes
    assert json.loads((private / "lab-discovery.json").read_text("utf-8"))["armed"] is False

"""Vivo lab checks may read only proven tool-created IDs and never certify a full account."""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.models import Block, NoteDocument, PlatformId, Span
from note_bridge.operations import receipt_key
from note_bridge.providers.base import CreatedNote
from note_bridge.receipt_identity import identity_key
from note_bridge.storage import Store

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("vivo_matrix_scope_test", SCRIPTS / "live-matrix-job.py")
MATRIX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MATRIX)
ACCOUNT = "a" * 64
OTHER_ACCOUNT = "b" * 64


def seed(job):
    return NoteDocument(platform=PlatformId.XIAOMI, account_id="synthetic-fixture-source", source_id=job["id"],
                        title=job["title"], blocks=[Block(spans=[Span(text="synthetic", bold=True)])])


def target(platform="vivo", account=ACCOUNT):
    return SimpleNamespace(spec=SimpleNamespace(id=platform), account_id=account)


def save_manifest(root, name, job):
    path = root / ".private/checkpoints" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job), "utf-8")


def direct_job(platform="vivo", account=ACCOUNT):
    return {"kind": "independent-cloud-write-smoke", "id": "fixture-20260907-synthetic-original",
            "target": platform, "expected_account": account, "armed": False, "title": "synthetic original"}


class ReceiptsOnly:
    def __init__(self):
        self.rows = {}

    def receipt(self, key):
        return self.rows.get(key)

    def notes(self, *args):
        pytest.fail("Synthetic ancestry cannot read private cached notes")

    def snapshot(self, *args):
        pytest.fail("Synthetic ancestry does not depend on whole-account completeness")


def test_scope_is_bound_to_consumed_manifest_and_confirmed_receipt_without_cache(tmp_path):
    store = ReceiptsOnly()
    job = direct_job()
    save_manifest(tmp_path, "original-job.json", job)
    key = receipt_key(seed(job), target())
    store.rows[key] = {"status": "confirmed", "remote_ids": ["1" * 32]}
    scope = MATRIX.vivo_tool_scope(store, tmp_path, seed, ACCOUNT)
    assert scope["exact_ids"] == ("1" * 32,)
    assert scope["whole_account_snapshot"] is False
    assert scope["bindings"][0]["receipt_key"] == key
    assert len(scope["bindings"][0]["manifest_sha256"]) == 64
    job["armed"] = True
    save_manifest(tmp_path, "original-job.json", job)
    assert MATRIX.vivo_tool_scope(store, tmp_path, seed, ACCOUNT)["exact_ids"] == ()


@pytest.mark.parametrize("status,ids", [("uncertain", ["1" * 32]), ("sending", ["1" * 32]),
    ("rejected", ["1" * 32]), ("confirmed", ["1" * 32, "2" * 32]), ("confirmed", ["private-title"]),
    ("confirmed", [])])
def test_unknown_or_ambiguous_target_ids_are_never_read(tmp_path, status, ids):
    store = ReceiptsOnly()
    job = direct_job()
    save_manifest(tmp_path, "original-job.json", job)
    store.rows[receipt_key(seed(job), target())] = {"status": status, "remote_ids": ids}
    result = MATRIX.vivo_tool_scope(store, tmp_path, seed, ACCOUNT)
    assert result["exact_ids"] == () and result["excluded"]


@pytest.mark.parametrize("mode", ["valid", "private-id", "wrong-account", "unconfirmed-ancestor"])
def test_migrated_fixture_needs_exact_confirmed_synthetic_ancestry(tmp_path, mode):
    store = ReceiptsOnly()
    original = direct_job("meizu", OTHER_ACCOUNT)
    save_manifest(tmp_path, "original-job.json", original)
    store.rows[receipt_key(seed(original), target("meizu", OTHER_ACCOUNT))] = {
        "status": "uncertain" if mode == "unconfirmed-ancestor" else "confirmed", "remote_ids": ["tool-source"]}
    origin = {"manifest": "original-job.json", "platform": "meizu", "account": OTHER_ACCOUNT,
              "source_id": "private-id" if mode == "private-id" else "tool-source", "fingerprint": "f" * 64}
    if mode == "wrong-account":
        origin["account"] = "c" * 64
    batch = {"kind": "cloud-matrix-batch", "id": "matrix-20260907-source-vivo-test", "target": "vivo",
             "expected_account": ACCOUNT, "armed": False, "sources": [{"from_cloud_fixture": origin}]}
    save_manifest(tmp_path, "batch-job.json", batch)
    key = identity_key({"source_platform": origin["platform"], "source_account": origin["account"],
        "source_id": origin["source_id"], "source_fingerprint": origin["fingerprint"],
        "target_platform": "vivo", "target_account": ACCOUNT})
    store.rows[key] = {"status": "confirmed", "remote_ids": ["2" * 32]}
    scope = MATRIX.vivo_tool_scope(store, tmp_path, seed, ACCOUNT)
    assert scope["exact_ids"] == (("2" * 32,) if mode == "valid" else ())
    if mode == "valid":
        assert scope["bindings"][0]["lineage"][0]["remote_id"] == "tool-source"


@pytest.mark.parametrize("scenario", ["normal", "empty-before", "partial-read", "wrong-read-account", "uncertain-write"])
def test_vivo_batch_reads_only_scoped_ids_without_changing_global_snapshot(tmp_path, monkeypatch, scenario):
    private = tmp_path / ".private"
    (private / "evidence").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/live-fixture-job.py").write_bytes((SCRIPTS / "live-fixture-job.py").read_bytes())
    incoming = NoteDocument(platform=PlatformId.MEIZU, account_id=OTHER_ACCOUNT, source_id="new-source",
        title="synthetic incoming", blocks=[Block(spans=[Span(text="synthetic", bold=True)])])
    monkeypatch.setattr(MATRIX, "select_batch", lambda *args: [incoming])
    before_ids = () if scenario == "empty-before" else ("1" * 32,)
    monkeypatch.setattr(MATRIX, "vivo_tool_scope", lambda *args: {
        "kind": "confirmed-tool-created-vivo-scope", "account_id": ACCOUNT, "exact_ids": before_ids,
        "bindings": [], "whole_account_snapshot": False})
    database = private / "session-lab/notes.sqlite"
    database.parent.mkdir()
    store = Store(database)
    cached = incoming.model_copy(update={"platform": PlatformId.VIVO, "account_id": ACCOUNT, "source_id": "private-cache"})
    store.replace_snapshot(PlatformId.VIVO, ACCOUNT, [cached], False)

    def cached_rows():
        with sqlite3.connect(database) as db:
            return db.execute("SELECT * FROM notes").fetchall(), db.execute("SELECT * FROM snapshots").fetchall()

    unchanged_cache = cached_rows()
    monkeypatch.setattr(Store, "replace_snapshot", lambda *a: pytest.fail("A scoped read cannot replace the whole account"))
    monkeypatch.setattr(Store, "notes", lambda *a: pytest.fail("This scoped target branch must never read the full cache"))
    monkeypatch.setattr(Store, "snapshot", lambda *a: pytest.fail("Whole-account completeness is irrelevant to this scope"))
    monkeypatch.setattr(MATRIX, "fetch_snapshot", lambda *a: pytest.fail("No full target fetch is allowed"))
    calls, remote = [], {}
    if before_ids:
        remote[before_ids[0]] = incoming.model_copy(update={"platform": PlatformId.VIVO, "account_id": ACCOUNT,
                                                           "source_id": before_ids[0]})
    job = {"id": "matrix-20260907-scoped-test", "kind": "cloud-matrix-batch", "target": "vivo",
           "expected_account": ACCOUNT, "sources": [{}], "armed": True, "source_policy": "direct_seed_only"}
    path = private / "lab-matrix-job.json"
    path.write_text(json.dumps(job), "utf-8")

    def read_notes(provider, ids, context):
        calls.append(("read", ids))
        context.update(total=len(ids), completed=len(ids), succeeded=len(ids))
        return SimpleNamespace(account_id=OTHER_ACCOUNT if scenario == "wrong-read-account" else ACCOUNT,
            exact_ids=ids, complete=scenario != "partial-read", notes=[remote[key] for key in ids])

    def create(note, context):
        assert json.loads(path.read_text("utf-8"))["armed"] is False
        calls.append(("write",))
        if scenario == "uncertain-write":
            from note_bridge.errors import WriteUncertain
            context.record_remote_ids(["3" * 32])
            raise WriteUncertain()
        remote["2" * 32] = note.model_copy(update={"platform": PlatformId.VIVO, "account_id": ACCOUNT,
                                                  "source_id": "2" * 32})
        return CreatedNote(["2" * 32])

    provider = SimpleNamespace(spec=SimpleNamespace(id="vivo"), account_id=ACCOUNT,
        probe=lambda: pytest.fail("The full-account probe includes statistics"),
        preflight=lambda _: None, create=create, close=lambda: None)
    monkeypatch.setattr(MATRIX, "create_provider", lambda *a: provider)
    monkeypatch.setitem(sys.modules, "vivo_scoped_readback", SimpleNamespace(read_notes=read_notes,
                                                                           verify_account=lambda _: ACCOUNT))
    result = MATRIX.run_armed_job("vivo", [], tmp_path)
    assert cached_rows() == unchanged_cache, "A synthetic subset must not certify, remove or overwrite private cached notes"
    assert result["whole_account_snapshot"] is False and result["comparison_scope"] == "confirmed_tool_created_ids_only"
    assert "original_target_notes_unchanged" not in result and "only_confirmed_additions" not in result
    if scenario in ("partial-read", "wrong-read-account"):
        assert result["status"] == "needs_review" and calls == [("read", before_ids)]
        assert store.receipt(receipt_key(incoming, provider)) is None
    elif scenario == "uncertain-write":
        assert result["status"] == "needs_review"
        assert calls == [("read", before_ids), ("write",), ("read", before_ids)]
        assert store.receipt(receipt_key(incoming, provider))["status"] == "uncertain"
    else:
        assert result["status"] == "api_verified" and result["synthetic_scope_originals_unchanged"]
        assert result["synthetic_scope_only_confirmed_additions"]
        assert calls == [("read", before_ids), ("write",), ("read", tuple(sorted((*before_ids, "2" * 32))))]
    assert MATRIX.run_armed_job("vivo", [], tmp_path) is None, "Even a zero-write failed batch must remain consumed"


def test_malformed_or_cyclic_tool_ancestry_cannot_be_selected(tmp_path):
    store = ReceiptsOnly()
    bad = {**direct_job(), "from_cloud_fixture": {"manifest": "cycle-job.json", "platform": "vivo",
            "account": ACCOUNT, "source_id": "1" * 32, "fingerprint": "f" * 64}}
    save_manifest(tmp_path, "cycle-job.json", bad)
    with pytest.raises(BridgeError) as error:
        MATRIX.confirmed_tool_binding("cycle-job.json", None, store, tmp_path, seed)
    assert error.value.code == "invalid_synthetic_scope"
    save_manifest(tmp_path, "malformed-job.json", {**direct_job(), "kind": "cloud-matrix-batch", "sources": None})
    scope = MATRIX.vivo_tool_scope(store, tmp_path, seed, ACCOUNT)
    assert scope["exact_ids"] == ()
    assert {entry["manifest"] for entry in scope["excluded"]} == {"cycle-job.json", "malformed-job.json"}

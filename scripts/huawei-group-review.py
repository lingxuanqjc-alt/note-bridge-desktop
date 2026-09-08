"""Read-only reconciliation of the single acknowledged K1 synthetic category."""
import importlib.util
import json

from note_bridge.paths import AppPaths
from note_bridge.providers.factory import create_provider
from note_bridge.providers.huawei_groups import folder_key, parse_tags
from note_bridge.storage import Store


def run(jars, root):
    manifest = json.loads((root / ".private/lab-write-job.json").read_text("utf-8"))
    if manifest.get("armed") or manifest.get("id") != "fixture-20260906-huawei-group-K1":
        raise ValueError("fixture_scope")
    spec = importlib.util.spec_from_file_location("huawei_group_fixture", root / "scripts/live-fixture-job.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    note = module.fixture(manifest)
    provider = create_provider("huawei", jars, root / ".private/session-lab/resources")
    store = Store(AppPaths(root / ".private/session-lab").database)
    try:
        if provider.probe() != manifest["expected_account"]:
            raise ValueError("account_scope")
        key = folder_key(note, provider.account_id)
        receipt = store.receipt(key)
        if not receipt or receipt["status"] not in ("uncertain", "confirmed") or len(receipt["remote_ids"]) != 1:
            raise ValueError("receipt_scope")
        tags = parse_tags(provider._json("notetag/query", {"index": 0}))
        matches = [t for t in tags if str(t["uuid"]) == receipt["remote_ids"][0] and t["name"] == note.source_folder_name]
        report = {"kind": "huawei-K1-group-readback", "formal_acceptance": False,
                  "status": "verified" if len(matches) == 1 else "needs_review", "cloud_writes": 0}
        if len(matches) == 1:
            store.save_receipt(key, "confirmed", receipt["remote_ids"])
        (root / ".private/evidence/huawei-K1-group-readback.json").write_text(json.dumps(report, indent=2), "utf-8")
        return report
    finally:
        provider.close()

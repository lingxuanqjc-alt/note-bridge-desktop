"""Verify the R1 full target reread without issuing any cloud writes."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from note_bridge.paths import AppPaths
from note_bridge.storage import Store

root = Path(__file__).resolve().parents[1]
paths = AppPaths(root / ".private/session-lab")
job = json.loads((root / ".private/checkpoints/wps-vivo-R1-job.json").read_text("utf-8"))
checkpoint = json.loads((root / ".private/checkpoints/wps-vivo-R1-refetch-task.json").read_text("utf-8"))
baseline = json.loads((root / ".private/checkpoints/wps-vivo-R1-before-refetch.json").read_text("utf-8"))
original = json.loads((root / ".private/checkpoints/wps-vivo-R1-target-before.json").read_text("utf-8"))
assert job["id"] == "fixture-20260906-wps-vivo-R1" and job["armed"] is False
assert len(original) == 543 and len(baseline) == 544
assert all(baseline.get(key) == value for key, value in original.items())
store = Store(paths.database)
task = store.task(checkpoint["task_id"])
assert task and task.operation == "fetch" and task.status in ("succeeded", "partial"), "Full fetch must finish first"
assert task.completed == task.total == task.succeeded == len(baseline)
assert not ({issue.code for issue in task.issues} - {"source_warning"}), "Unexpected full fetch issues require review"
notes = store.notes(job["target"], job["expected_account"])
current = {note.source_id: note.fingerprint() for note in notes}
missing = len(baseline.keys() - current.keys())
extra = len(current.keys() - baseline.keys())
changed = sum(current.get(key) != digest for key, digest in baseline.items() if key in current)
attachments, failed = 0, 0
for note in notes:
    for asset in note.attachments:
        attachments += 1
        if not asset.local_path or not asset.sha256:
            failed += 1
            continue
        path = (paths.resources / asset.local_path).resolve()
        if (not path.is_relative_to(paths.resources.resolve()) or not path.is_file()
                or path.stat().st_size != asset.size or hashlib.sha256(path.read_bytes()).hexdigest() != asset.sha256):
            failed += 1
report = {"kind": "cloud-direction-target-full-refetch", "formal_acceptance": False,
          "source": "wps", "target": "vivo", "fixture_id": job["id"], "task_id": task.id,
          "time": datetime.now(timezone.utc).isoformat(), "notes": len(notes),
          "original_target_notes": len(original), "missing": missing, "extra": extra, "changed": changed,
          "attachment_references": attachments, "attachment_failures": failed,
          "source_warnings": len(task.issues), "cloud_writes": 0,
          "status": "verified" if not (missing or extra or changed or failed) else "needs_review"}
(root / ".private/evidence/wps-vivo-R1-full-refetch.json").write_text(json.dumps(report, indent=2), "utf-8")
print(json.dumps(report))
if report["status"] != "verified":
    raise SystemExit(1)

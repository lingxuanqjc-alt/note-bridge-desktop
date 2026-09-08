"""Compare the completed M2 full fetch with its private fingerprint baseline."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from note_bridge.paths import AppPaths
from note_bridge.storage import Store

root = Path(__file__).resolve().parents[1]
paths = AppPaths(root / ".private/session-lab")
checkpoint = json.loads((root / ".private/implementation-checkpoint.json").read_text("utf-8"))
job = json.loads((root / ".private/checkpoints/vivo-group-M2-job.json").read_text("utf-8"))
baseline = json.loads((root / ".private/checkpoints/vivo-M2-before-refetch.json").read_text("utf-8"))
assert job["id"] == "fixture-20260906-vivo-group-M2" and job["armed"] is False
store = Store(paths.database)
task = store.task(checkpoint["vivo_group_candidate"]["full_refetch_task_id"])
assert task and task.status in ("succeeded", "partial"), "Full fetch must finish before comparison"
assert task.completed == task.total == task.succeeded == len(baseline) == 543
notes = store.notes("vivo", job["expected_account"])
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
        if not path.is_relative_to(paths.resources.resolve()) or not path.is_file():
            failed += 1
            continue
        if path.stat().st_size != asset.size or hashlib.sha256(path.read_bytes()).hexdigest() != asset.sha256:
            failed += 1
report = {"kind": "vivo-M2-full-refetch-comparison", "formal_acceptance": False,
          "time": datetime.now(timezone.utc).isoformat(), "notes": len(notes),
          "baseline_notes": len(baseline), "missing": missing, "extra": extra, "changed": changed,
          "attachment_references": attachments, "attachment_failures": failed,
          "status": "verified" if not (missing or extra or changed or failed) else "needs_review"}
(root / ".private/evidence/vivo-M2-full-refetch.json").write_text(json.dumps(report, indent=2), "utf-8")
print(json.dumps(report))
if report["status"] != "verified":
    raise SystemExit(1)

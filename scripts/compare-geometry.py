"""Compare measured original and independent UI controls at the same CSS viewport."""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1] / ".private/evidence"
reference = json.loads((root / "reference/evidence.json").read_text("utf-8"))
actual = json.loads((root / "ui/evidence.json").read_text("utf-8"))
assert reference["viewport"] == actual["viewport"]
checks = []
for state, key, count in (("initial", "exportCards", 7), ("migration", "migrationCards", 14)):
    originals, copies = reference["states"][state]["cards"], actual[key]
    assert len(originals) == len(copies) == count
    for index, (original, copy) in enumerate(zip(originals, copies)):
        for control in ("card", "name", "logo", "login"):
            deviations = {axis: abs(original[control][axis] - copy[control][axis])
                          for axis in ("x", "y", "width", "height")}
            checks.append({"state": state, "card": index, "control": control,
                           "max_deviation": max(deviations.values()), "deviations": deviations})
result = {"kind": "measured-reference-controls", "formal_acceptance": False, "viewport": actual["viewport"],
          "scope": "7 export cards and 14 initial migration cards; card, label, icon box, login button",
          "remaining": "dialogs, format selection, progress and outcome states; Windows DPI",
          "checks": checks, "passed": sum(c["max_deviation"] <= 2 for c in checks), "total": len(checks),
          "max_deviation": max(c["max_deviation"] for c in checks)}
(root / "geometry-comparison.json").write_text(json.dumps(result, indent=2), "utf-8")
print(json.dumps({k: v for k, v in result.items() if k != "checks"}))
raise SystemExit(0 if result["passed"] == result["total"] else 1)

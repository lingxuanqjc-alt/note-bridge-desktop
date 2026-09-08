"""A green synthetic suite cannot authorize a v1.0.0 release."""
import json
from pathlib import Path

from acceptance_validation import validate

root = Path(__file__).resolve().parents[1]
matrix = json.loads((root / "docs" / "acceptance.json").read_text(encoding="utf8"))
pending = validate(matrix, root)
if pending:
    print(f"Formal release blocked: {len(pending)} acceptance problems.")
    for problem in pending[:8]:
        print(problem)
    raise SystemExit(1)
print("Recorded acceptance evidence is complete; public release still follows the user's publication instruction.")

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("acceptance_validation", ROOT / "scripts/acceptance_validation.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def setup_report(tmp_path):
    matrix = json.loads((ROOT / "docs/acceptance.json").read_text("utf-8"))
    checks = []
    for group in module.FIELDS:
        for row in matrix[group]:
            row.update(status="passed", evidence="formal.json")
            checks.append({"group": group, **{field: row[field] for field in module.FIELDS[group]}, "status": "passed"})
    evidence = {"formal_acceptance": True, "checks": checks}
    (tmp_path / "formal.json").write_text(json.dumps(evidence), "utf-8")
    return matrix, evidence


def test_complete_exact_matrix_can_be_reviewed_but_pending_cases_cannot(tmp_path):
    matrix, _ = setup_report(tmp_path)
    assert module.validate(matrix, tmp_path) == []
    for group in module.FIELDS:
        for row in matrix[group]:
            row["status"] = "pending"
    assert len(module.validate(matrix, tmp_path)) == 112


@pytest.mark.parametrize("group", ["exports", "migrations", "desktop", "visual"])
def test_repeating_a_passed_case_cannot_hide_an_untested_direction_or_environment(tmp_path, group):
    matrix, _ = setup_report(tmp_path)
    matrix[group][-1] = matrix[group][0].copy()
    assert any("duplicate" in problem for problem in module.validate(matrix, tmp_path))


@pytest.mark.parametrize("name", ["missing.json", "../outside.json"])
def test_evidence_path_text_is_not_evidence(tmp_path, name):
    matrix, _ = setup_report(tmp_path)
    matrix["migrations"][0]["evidence"] = name
    assert module.validate(matrix, tmp_path)


@pytest.mark.parametrize("change", ["development", "wrong_direction", "failed", "duplicate"])
def test_development_or_mismatched_reports_cannot_authorize_release(tmp_path, change):
    matrix, evidence = setup_report(tmp_path)
    if change == "development":
        evidence["formal_acceptance"] = False
    else:
        case = next(check for check in evidence["checks"] if check["group"] == "migrations")
        if change == "wrong_direction":
            case["source"], case["target"] = case["target"], case["source"]
        elif change == "failed":
            case["status"] = "failed"
        else:
            evidence["checks"].append(case.copy())
    (tmp_path / "formal.json").write_text(json.dumps(evidence), "utf-8")
    assert module.validate(matrix, tmp_path)

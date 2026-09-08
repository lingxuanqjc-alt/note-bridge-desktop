"""Validate exact acceptance coverage and scoped, existing formal evidence."""
import json
from collections import Counter
from itertools import product
from pathlib import Path

PLATFORMS = ("xiaomi", "oppo", "vivo", "huawei", "honor", "meizu", "wps")
FIELDS = {"exports": ("platform", "format", "mode"), "migrations": ("source", "target"),
          "desktop": ("os", "scale"), "visual": ("page",)}


def validate(matrix, root):
    root = Path(root).resolve()
    expected = {
        "exports": set(product(PLATFORMS, ("txt", "md", "html", "docx"), ("single", "multi"))),
        "migrations": {(source, target) for source in PLATFORMS for target in PLATFORMS if source != target},
        "desktop": set(product(("Windows 10", "Windows 11"), (100, 125, 150))),
        "visual": {(page,) for page in ("initial", "selected", "login", "format", "progress", "success", "failure", "migrate")},
    }
    errors = []
    if not isinstance(matrix, dict):
        return ["matrix: expected an object"]
    for group, fields in FIELDS.items():
        rows = matrix.get(group)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            errors.append(f"{group}: invalid case list")
            continue
        keys = [tuple(row.get(field) for field in fields) for row in rows]
        if any(any(type(value) not in (str, int) for value in key) for key in keys):
            errors.append(f"{group}: invalid case identity")
            continue
        if Counter(keys) != Counter(expected[group]):
            errors.append(f"{group}: missing, duplicate or unexpected cases")
        for row, key in zip(rows, keys):
            label = f"{group}:{'/'.join(map(str, key))}"
            if row.get("status") != "passed":
                errors.append(label + ": not passed")
                continue
            name = row.get("evidence")
            if not isinstance(name, str) or not name:
                errors.append(label + ": missing evidence")
                continue
            path = (root / name).resolve()
            if Path(name).is_absolute() or not path.is_relative_to(root) or not path.is_file():
                errors.append(label + ": evidence must be an existing project file")
                continue
            try:
                evidence = json.loads(path.read_text("utf-8-sig"))
            except (OSError, ValueError, UnicodeError):
                errors.append(label + ": unreadable evidence")
                continue
            if (not isinstance(evidence, dict) or evidence.get("formal_acceptance") is not True
                    or not isinstance(evidence.get("checks"), list)):
                errors.append(label + ": evidence is not a formal acceptance report")
                continue
            matches = [check for check in evidence["checks"] if isinstance(check, dict)
                       and check.get("group") == group and all(check.get(field) == row[field] for field in fields)]
            if len(matches) != 1 or matches[0].get("status") != "passed":
                errors.append(label + ": evidence does not prove this exact case")
    return errors

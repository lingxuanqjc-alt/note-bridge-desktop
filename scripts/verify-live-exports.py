"""Verify exports from real cached snapshots without printing private note contents.

This checks the acquired snapshot, not the completeness of the vendor's cloud adapter.
Evidence remains private and explicitly separates conversion checks from release acceptance.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html as html_std
import json
from pathlib import Path
import re
import uuid
import zipfile
from urllib.parse import unquote

from lxml import etree, html

from note_bridge.exporter import Exporter, package_export
from note_bridge.models import PlatformId
from note_bridge.paths import AppPaths
from note_bridge.storage import Store


def normalized(value):
    return re.sub(r"\s+", "", value)


def extracted(path, format):
    if format == "docx":
        with zipfile.ZipFile(path) as archive:
            tree = etree.fromstring(archive.read("word/document.xml"))
        return "".join(tree.xpath('//*[local-name()="t"]/text()')), tree
    raw = path.read_text("utf-8-sig")
    if format == "html":
        tree = html.fromstring(raw)
        data = tree.xpath('//*[@id="note-data"]/text()')
        records = json.loads(data[0]) if data else None
        if records:
            text = "\n".join("".join(html.fragment_fromstring(r["html"], create_parent="div").itertext())
                             for r in records)
        else:
            text = "".join(tree.xpath("//article")[0].itertext())
        return text, records
    if format == "md":
        # Check each text span independently, so Markdown's inline wrappers do not hide text loss.
        raw = html_std.unescape(re.sub(r"\\([\\`*_{}\[\]|])", r"\1", raw))
    return raw, None


def verify(files, notes, format, multi):
    if len(files) != (len(notes) if multi else 1):
        raise AssertionError("document_count")
    targets = list(zip(files, [[n] for n in notes])) if multi else [(files[0], notes)]
    for path, expected in targets:
        text, structure = extracted(path, format)
        text = normalized(text)
        if format == "html" and not multi:
            if [r["text"] for r in structure] != [n.plain_text for n in notes]:
                raise AssertionError("html_search_corpus")
        if format == "docx" and not multi:
            anchors = structure.xpath('//*[local-name()="hyperlink"]/@*[local-name()="anchor"]')
            if set(anchors) != {f"note_{i}" for i in range(len(notes))}:
                raise AssertionError("docx_navigation")
        for note in expected:
            fragments = [note.display_title]
            for block in note.blocks:
                fragments += [span.text for span in block.spans]
                fragments += [cell for row in block.rows for cell in row]
            fragments += [date.isoformat(sep=" ", timespec="seconds")
                          for date in (note.created_at, note.updated_at) if date]
            if any(normalized(fragment) not in text for fragment in fragments if fragment):
                raise AssertionError("text_or_timestamp")
        expected_assets = {a.sha256 for n in expected for a in n.attachments if a.local_path}
        found_assets, references = set(), []
        if format == "docx":
            with zipfile.ZipFile(path) as archive:
                relationships = etree.fromstring(archive.read("word/_rels/document.xml.rels"))
                references = relationships.xpath('//@Target')
                found_assets.update(hashlib.sha256(archive.read(name)).hexdigest()
                                    for name in archive.namelist() if name.startswith("word/media/"))
        elif format == "html":
            if not multi:
                markup = "".join(r["html"] for r in structure)
            else:
                markup = path.read_text("utf-8")
            references = html.fragment_fromstring(markup, create_parent="div").xpath('//@src|//@href')
        else:
            references = re.findall(r'resources/[^\s)<>\"\']+', path.read_text("utf-8-sig"))
        for reference in references:
            if not reference.startswith("resources/"):
                continue
            resolved = (path.parent / unquote(reference)).resolve()
            if not resolved.is_relative_to(path.parent.resolve()) or not resolved.is_file():
                raise AssertionError("portable_attachment_reference")
            found_assets.add(hashlib.sha256(resolved.read_bytes()).hexdigest())
        if not expected_assets <= found_assets:
            raise AssertionError("attachment_not_reachable_from_document")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=[p.value for p in PlatformId])
    args = parser.parse_args()
    if args.platform in (None, "vivo"):
        parser.error("Full-account Vivo cache exports are outside the accepted test scope; use an exact tool-fixture corpus.")
    root = Path(__file__).resolve().parents[1]
    paths = AppPaths(root / ".private/session-lab")
    store = Store(paths.database)
    with store.connection() as db:
        scopes = db.execute("SELECT platform,account,complete,count FROM snapshots").fetchall()
    if Counter(row["platform"] for row in scopes) != Counter({p.value: 1 for p in PlatformId}):
        raise AssertionError("exactly_one_live_scope_per_platform_required")
    if args.platform:
        scopes = [s for s in scopes if s["platform"] == args.platform]
    run = root / ".private/live-exports" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    run.mkdir(parents=True)
    evidence = {"kind": "real-cloud-snapshot-conversion", "time": datetime.now(timezone.utc).isoformat(),
                "release_acceptance": False, "checks": [], "snapshots": []}
    for scope in scopes:
        notes = store.notes(PlatformId(scope["platform"]), scope["account"])
        evidence["snapshots"].append({"platform": scope["platform"], "count": len(notes),
                                      "complete": bool(scope["complete"]),
                                      "warnings": sum(len(n.warnings) for n in notes),
                                      "attachments": sum(len(n.attachments) for n in notes)})
        destination = run / scope["platform"]
        destination.mkdir()
        for format in ("txt", "md", "html", "docx"):
            for multi in (False, True):
                item = {"platform": scope["platform"], "format": format, "mode": "multi" if multi else "single"}
                try:
                    result = Exporter(paths.resources).export(notes, destination, format, multi)
                    files = sorted(result.path.glob("*." + format))
                    verify(files, notes, format, multi)
                    archive_path = package_export(result.path, destination)
                    with zipfile.ZipFile(archive_path) as archive:
                        if archive.testzip() is not None:
                            raise AssertionError("zip_integrity")
                        if any(name.startswith("/") or ".." in Path(name).parts for name in archive.namelist()):
                            raise AssertionError("zip_paths")
                    item.update({"conversion": "passed", "notes": len(notes), "files": len(files),
                                 "source_complete": bool(scope["complete"]),
                                 "issue_counts": dict(Counter(issue.code for issue in result.issues)),
                                 "output": str(result.path.relative_to(run)), "zip": str(archive_path.relative_to(run))})
                except Exception as error:
                    item.update({"conversion": "failed", "code": type(error).__name__})
                evidence["checks"].append(item)
                (run / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps({"platform": scope["platform"], "notes": len(notes),
                          "passed": sum(c["conversion"] == "passed" for c in evidence["checks"][-8:]),
                          "total": 8, "source_complete": bool(scope["complete"])}, ensure_ascii=True), flush=True)
    print(json.dumps({"evidence": str(run / "evidence.json"),
                      "passed": sum(c["conversion"] == "passed" for c in evidence["checks"]),
                      "total": len(evidence["checks"]), "formal_acceptance": False}, ensure_ascii=True))
    if any(c["conversion"] != "passed" for c in evidence["checks"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

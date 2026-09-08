"""Prepare a deterministic browser fixture and verify one real exported reader offline."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from note_bridge.exporter import Exporter
from note_bridge.models import Block, NoteDocument, PlatformId, Span
from note_bridge.richtext import parse_html


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-evidence", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = root / ".private/evidence/reader"
    out.mkdir(parents=True, exist_ok=True)
    notes = []
    for index, (title, body, month) in enumerate([
        ("Alpha 中文😀", "alpha beta", 8),
        ("Second", "alpha only", 8),
        ("Third", "ALPHA beta", 9),
    ]):
        notes.append(NoteDocument(platform=PlatformId.XIAOMI, account_id="synthetic-reader",
                                  source_id=str(index), title=title,
                                  created_at=datetime(2026, month, 15, tzinfo=timezone.utc),
                                  blocks=[Block(spans=[Span(text=body, bold=True)])]))
    hostile, warnings = parse_html('<p onclick="window.noteExecuted=true">safe text</p>'
                                  '<img src="https://invalid.example/private" onerror="window.noteExecuted=true">'
                                  '<script>window.noteExecuted=true</script>'
                                  '<a href="javascript:window.noteExecuted=true">unsafe link</a>')
    notes.append(NoteDocument(platform=PlatformId.XIAOMI, account_id="synthetic-reader", source_id="3",
                              title="</script><img src=x onerror=window.noteExecuted=true>",
                              blocks=hostile, warnings=warnings))
    fixture = Exporter(out).export(notes, out, "html")
    live = json.loads(args.live_evidence.read_text("utf-8"))
    live_html = next(c for c in live["checks"] if c["platform"] == "vivo" and c["format"] == "html"
                     and c["mode"] == "single" and c["conversion"] == "passed")
    live_path = next((args.live_evidence.parent / live_html["output"]).glob("*.html"))
    command = ["node", str(root / "scripts/reader-evidence.cjs"),
               str(next(fixture.path.glob("*.html"))), str(live_path), str(out / "evidence.json")]
    raise SystemExit(subprocess.call(command))


if __name__ == "__main__":
    main()

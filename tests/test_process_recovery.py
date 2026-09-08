"""Real process termination against a local synthetic target, never a vendor account."""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from note_bridge.models import Block, NoteDocument, PlatformId, Span, TaskStatus
from note_bridge.operations import migrate, receipt_key
from note_bridge.providers.base import SPECS, CreatedNote, Provider, Snapshot
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


def source_notes(account="source-a"):
    return [NoteDocument(platform=PlatformId.XIAOMI, account_id=account, source_id=str(index),
                         title="同名笔记", blocks=[Block(spans=[Span(text=f"正文 {index} 😀")])])
            for index in range(2)]


def pause_at_checkpoint(directory):
    (directory / "worker-pid").write_text(str(os.getpid()), encoding="ascii")
    (directory / "ready").write_text("ready", encoding="ascii")
    threading.Event().wait()


class LocalTarget(Provider):
    spec = SPECS[PlatformId.WPS]
    account_id = "synthetic-target"
    write_supported = True

    def __init__(self, directory, interrupt=False):
        self.directory, self.interrupt = directory, interrupt

    def probe(self):
        return self.account_id

    def fetch(self, context):
        return Snapshot([], True)

    def create(self, note, context):
        # This durable file stands in for an independent target committing a request.
        with (self.directory / "target-creates").open("a", encoding="ascii") as stream:
            stream.write(note.source_id + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        remote_id = "remote-" + note.source_id
        context.record_remote_ids([remote_id])
        context.record_resource("image-" + note.source_id, "uploaded")
        if self.interrupt:
            pause_at_checkpoint(self.directory)
        return CreatedNote([remote_id])


def crash_worker(directory, stage):
    store = Store(directory / "state.sqlite")
    for account in ("source-a", "source-b"):
        store.replace_snapshot(PlatformId.XIAOMI, account, source_notes(account), True)
    if stage == "snapshot":
        with store.connection() as db:
            db.execute("DELETE FROM notes WHERE platform=? AND account=?", ("xiaomi", "source-a"))
            db.execute("UPDATE snapshots SET complete=0,count=0 WHERE platform=? AND account=?",
                       ("xiaomi", "source-a"))
            pause_at_checkpoint(directory)
        return

    def emit(report):
        if stage == "confirmed" and report["succeeded"] == 1:
            pause_at_checkpoint(directory)

    target = LocalTarget(directory, interrupt=stage == "unacknowledged")
    runner = TaskRunner(store, emit)
    runner.start("migrate", lambda ctx: migrate(source_notes(), target, store, ctx))
    runner.join()


def terminate_at_checkpoint(directory, stage):
    # A Windows venv redirector can exit before its actual CPython child releases
    # SQLite mappings. Terminate and wait for the interpreter holding the transaction.
    executable = Path(getattr(sys, "_base_executable", sys.executable))
    assert executable.is_file(), "The crash worker's actual interpreter must exist"
    version = subprocess.run(
        [str(executable), "-c", "import sys; print(sys.version)"],
        capture_output=True, text=True, timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert version.returncode == 0 and version.stdout.strip() == sys.version
    process = subprocess.Popen(
        [str(executable), str(Path(__file__).resolve()), str(directory), stage],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(path for path in sys.path if path)},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        deadline = time.monotonic() + 15
        while not (directory / "ready").exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert (directory / "ready").exists(), "Child must reach the exact durable-write boundary before termination"
        assert int((directory / "worker-pid").read_text("ascii")) == process.pid, (
            "The killed process must be the actual worker holding the SQLite transaction"
        )
        assert process.poll() is None
        process.kill()
        process.wait(timeout=10)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.parametrize("stage", ["unacknowledged", "confirmed", "snapshot"])
def test_abrupt_process_exit_preserves_data_and_prevents_duplicate_writes(tmp_path, stage):
    terminate_at_checkpoint(tmp_path, stage)
    store = Store(tmp_path / "state.sqlite")
    with store.connection() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    for account in ("source-a", "source-b"):
        assert [n.fingerprint() for n in store.notes(PlatformId.XIAOMI, account)] == [
            n.fingerprint() for n in source_notes(account)
        ], "A killed snapshot replacement must preserve both the previous cache and the other account"
        assert store.snapshot(PlatformId.XIAOMI, account) == {"complete": True, "count": 2}
    if stage == "snapshot":
        return

    target = LocalTarget(tmp_path)
    key = receipt_key(source_notes()[0], target)
    assert store.receipt(key)["status"] == ("sending" if stage == "unacknowledged" else "confirmed")
    runner = TaskRunner(store)
    assert runner.current().status == TaskStatus.NEEDS_REVIEW
    assert runner.current().succeeded == (1 if stage == "confirmed" else 0)
    assert store.receipt(key) == {
        "status": "uncertain" if stage == "unacknowledged" else "confirmed", "remote_ids": ["remote-0"]
    }
    assert store.resource_receipts(key) == [{
        "resource_id": "image-0", "status": "uploaded" if stage == "unacknowledged" else "linked"
    }], "Uploaded resource identity must survive so an interrupted multi-stage write can be reviewed"
    for _ in range(2):
        runner.start("migrate", lambda ctx: migrate(source_notes(), target, store, ctx))
        runner.join(5)
        assert not runner.busy
        assert runner.current().status == (
            TaskStatus.NEEDS_REVIEW if stage == "unacknowledged" else TaskStatus.SUCCEEDED
        )
    assert (tmp_path / "target-creates").read_text("ascii").splitlines() == (
        ["0"] if stage == "unacknowledged" else ["0", "1"]
    ), "Unknown writes must never resend; confirmed notes must skip while remaining notes migrate exactly once"


if __name__ == "__main__":
    crash_worker(Path(sys.argv[1]), sys.argv[2])

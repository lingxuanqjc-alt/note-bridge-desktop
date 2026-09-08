import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError

spec = importlib.util.spec_from_file_location("fixture_cases", Path(__file__).resolve().parents[1] / "scripts/live-fixture-job.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def test_empty_title_seed_remains_traceable_by_labelled_body_without_filling_title():
    job = {"target": "meizu", "id": "fixture-20260906-meizu-empty", "title": "笔记互迁验收 · 空标题", "content_case": "empty_title"}
    note = fixture.fixture(job)
    assert note.title == "" and note.display_title == job["title"]
    requests = []
    provider = SimpleNamespace(_json=lambda path, **kwargs: requests.append((path, kwargs)))
    report = {}
    fixture.install_empty_title_seed(provider, job, report)
    provider._json("updatenote", data={"title": note.display_title, "body": "synthetic"}, write=True)
    assert requests[0][1]["data"]["title"] == "" and report["empty_title_requested"]


@pytest.mark.parametrize("data", [{"uuid": "existing", "title": "笔记互迁验收 · 空标题"}, {"title": "unrelated"}])
def test_empty_title_hook_cannot_update_an_existing_or_unlabelled_note(data):
    calls = []
    provider = SimpleNamespace(_json=lambda *args, **kwargs: calls.append(args))
    fixture.install_empty_title_seed(provider, {"target": "meizu", "title": "笔记互迁验收 · 空标题"}, {})
    with pytest.raises(BridgeError):
        provider._json("updatenote", data=data, write=True)
    assert not calls

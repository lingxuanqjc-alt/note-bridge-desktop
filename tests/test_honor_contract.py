import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span, TaskStatus
from note_bridge.operations import migrate, receipt_key
from note_bridge.providers.honor import HonorProvider, parse_entry
from note_bridge.providers.honor_html import encode
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


def source_note():
    return NoteDocument(platform=PlatformId.XIAOMI, account_id="source", source_id="original",
                        title="中文😀", blocks=[Block(spans=[Span(text="bold", bold=True),
                                                Span(text="italic", italic=True), Span(text="underlined", underline=True)]),
                        Block(kind="todo", checked=True, spans=[Span(text="done")]),
                        Block(kind="todo", spans=[Span(text="pending")]),
                        Block(kind="list", ordered=True, spans=[Span(text="first")]),
                        Block(kind="heading", level=2, spans=[Span(text="heading")])])


def test_native_editor_serialization_preserves_styles_and_checkbox_state():
    markup = ('<p class="Nb h_n" hlevel="2"><h-text><font hsize="16" class="La Lb Lc Ld">'
              '<span>中文😀</span></font></h-text></p>'
              '<div class="Na h_n"><svg class="Sc"><path/></svg>'
              '<h-text><font><span>done</span></font></h-text></div>'
              '<ol class="Nd Pa h_n"><li class="Nc h_n"><h-text><font><span>first</span>'
              '</font></h-text></li></ol>')
    note = parse_entry({"uuid": "target", "type": 2, "html_content": markup}, "target-account", {})
    assert not note.warnings, "A native checkbox icon is structural state, not executable note content."
    assert [(block.kind, block.text) for block in note.blocks] == [("heading", "中文😀"), ("todo", "done"), ("list", "first")]
    assert note.blocks[0].level == 2 and note.blocks[1].checked and note.blocks[2].ordered
    span = note.blocks[0].spans[0]
    assert span.bold and span.italic and span.underline and span.strike


def test_honor_candidate_emits_editable_native_blocks_with_literal_text():
    note = source_note()
    note.blocks[0].spans.append(Span(text='<script>&" literal'))
    body, warnings = encode(note)
    restored = parse_entry({"uuid": "target", "type": 2, "html_content": body}, "target-account", {})
    assert not warnings and not restored.warnings
    assert restored.blocks[:len(note.blocks)] == note.blocks
    assert '<script>' not in body


@pytest.mark.parametrize("content_case", ["rich_styles", "rich_styles_plain"])
def test_live_rich_style_case_retains_combined_styles_links_and_literal_markup(content_case):
    spec = importlib.util.spec_from_file_location("rich_live_fixture", Path(__file__).resolve().parents[1] / "scripts/live-fixture-job.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    note = module.fixture({"id": "fixture-20260906-honor-rich-S1", "target": "honor",
                           "title": "笔记互迁验收 · 荣耀富文本 S1", "content_case": content_case})
    body, warnings = encode(note)
    restored = parse_entry({"uuid": "target", "type": 2, "html_content": body}, "account", {})
    assert not warnings and not restored.warnings
    assert restored.blocks[:len(note.blocks)] == note.blocks
    assert "<script>" not in body
    expected = "<script>仅作为文本</script>" if content_case == "rich_styles" else "普通文字 & 中文"
    assert expected in restored.plain_text
    assert any(span.link == "https://example.com/note-bridge" for block in restored.blocks for span in block.spans)


def test_native_format_and_missing_media_losses_are_explicit():
    note = parse_entry({"uuid": "target", "type": 2, "has_attachment": 1, "html_content":
                       '<p class="Nb h_n Aj" hindent="2"><h-text><font hsize="20" hcolor="red">text</font></h-text></p>'
                       '<div class="Nx h_n">unsupported attachment</div>'}, "account", {})
    assert len(note.warnings) == 4
    assert "text" in note.plain_text and "unsupported attachment" in note.plain_text


@pytest.mark.parametrize("invalid", ["image", "missing_image", "long_text"])
def test_unverified_content_is_rejected_before_any_create(invalid):
    note = source_note()
    if invalid == "image":
        note.attachments = [Attachment(id="image", name="image.png", kind="image")]
    elif invalid == "missing_image":
        note.blocks.append(Block(kind="attachment", attachment_id="not-downloaded"))
    else:
        note.blocks = [Block(spans=[Span(text="😀" * 50001)])]
    with pytest.raises(BridgeError):
        encode(note)


@pytest.mark.parametrize("acknowledged", [True, False])
def test_only_matching_new_note_acknowledgement_can_enable_resume_skip(tmp_path, acknowledged):
    requests = []
    def response(method, path, **kwargs):
        assert method == "POST" and path == "/portal/notepad/noteSave" and kwargs["write"]
        assert kwargs["json"][0]["update"] is False
        row = kwargs["json"][0]
        requests.append(row)
        return {"code": 0, "data": [{"uuid": row["uuid"] if acknowledged else "unrelated-note"}]}
    provider = HonorProvider(SimpleNamespace(cookie=lambda _: "synthetic-csrf", json=response), tmp_path)
    provider.account_id, provider.write_supported = "target-account", True
    note, store = source_note(), Store(tmp_path / "tasks.sqlite")
    runner = TaskRunner(store)
    for _ in range(2):
        runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
        runner.join()
        assert runner.current().status == (TaskStatus.SUCCEEDED if acknowledged else TaskStatus.NEEDS_REVIEW)
    assert len(requests) == 1, "Neither confirmed nor unknown writes may create a second note on resume."
    receipt = store.receipt(receipt_key(note, provider))
    assert receipt["status"] == ("confirmed" if acknowledged else "uncertain")
    assert receipt["remote_ids"] == [requests[0]["uuid"]] and requests[0]["uuid"] != note.source_id

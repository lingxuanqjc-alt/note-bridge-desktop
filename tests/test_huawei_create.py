import json
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span, TaskStatus
from note_bridge.operations import migrate, receipt_key
from note_bridge.providers.huawei import HuaweiProvider, encode_body, parse_entry
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


def source_note():
    return NoteDocument(platform=PlatformId.XIAOMI, account_id="source-account", source_id="original-source",
                        title="测试😀", blocks=[Block(spans=[Span(text="literal | < > &", bold=True, italic=True, underline=True)]),
                                              Block(kind="todo", checked=True, spans=[Span(text="done")]),
                                              Block(kind="todo", spans=[Span(text="pending")])])


def test_dual_body_representations_keep_unicode_literal_pipes_and_task_state():
    note = source_note()
    plain, markup, warnings = encode_body(note)
    assert not warnings
    assert "Text|literal | < > &" in plain and "Bullet|1done" in plain and "Bullet|0pending" in plain
    assert "&lt; &gt; &amp;" in markup
    restored = parse_entry({"guid": "new-cloud-note", "kind": "note", "data": json.dumps({"content": {
                           "html_content": markup, "title": note.title}})}, "target-account", {})
    assert restored.blocks[1:4] == note.blocks


def test_empty_legacy_font_rows_preserve_spacing_between_paragraphs():
    note = source_note()
    note.blocks = [Block(spans=[Span(text="before")]), Block(), Block(), Block(spans=[Span(text="after")])]
    _, markup, warnings = encode_body(note)
    restored = parse_entry({"guid": "new-cloud-note", "kind": "note", "data": json.dumps({"content": {
                           "html_content": markup, "title": note.title}})}, "target-account", {})
    assert restored.blocks[1:5] == note.blocks, "Explicit empty rows must retain their count and position."
    assert not warnings and not restored.warnings


@pytest.mark.parametrize("body,expected", [
    ('<hw_font size="1.0">  <b>bold</b> text  </hw_font>',
     [Block(spans=[Span(text="  "), Span(text="bold", bold=True), Span(text=" text  ")])]),
    ('prefix<hw_font size="1.0"></hw_font>', [Block(spans=[Span(text="prefix")])]),
    ('<hw_font size="1.0"></hw_font>suffix', [Block(spans=[Span(text="suffix")])]),
    ('<hw_font size="1.0"><img src="cid:image"/></hw_font>',
     [Block(kind="attachment", attachment_id="image")]),
])
def test_empty_font_fix_does_not_replace_text_spacing_or_media(body, expected):
    assets = [Attachment(id="image", name="image.png", kind="image")] if "cid:image" in body else []
    markup = '<note>\n <element type="Text">' + body + '</element>\n </note>'
    restored = parse_entry({"guid": "synthetic-note", "kind": "note", "data": json.dumps({"content": {
                           "html_content": markup}})}, "target-account", {}, assets)
    assert restored.blocks == expected, "Only a truly empty Text/font row may become an empty paragraph."
    assert not restored.warnings


def test_empty_font_fix_keeps_whitespace_and_active_content_out_of_blank_rows():
    markup = ('<note>\n <element type="Text"><hw_font size="1.0"> \t </hw_font></element>\n '
              '<element type="Text"><hw_font size="1.0"><script>ignored()</script></hw_font></element>'
              '<element type="Text"><hw_font size="1.0">visible</hw_font></element>\n </note>')
    restored = parse_entry({"guid": "synthetic-note", "kind": "note", "data": json.dumps({"content": {
                           "html_content": markup}})}, "target-account", {})
    assert restored.blocks == [Block(spans=[Span(text="visible")])]
    assert restored.warnings == ["已移除正文中的可执行或嵌入式内容。"]


def test_empty_legacy_font_row_still_reports_unsupported_font_attributes():
    markup = '<element type="Text"><hw_font size="2.0" color="red"></hw_font></element>'
    restored = parse_entry({"guid": "synthetic-note", "kind": "note", "data": json.dumps({"content": {
                           "html_content": markup}})}, "target-account", {})
    assert restored.blocks == [Block()]
    assert restored.warnings == ["华为文字字号或颜色已转换为默认排版。"]


def test_ambiguous_legacy_separator_is_blocked_and_list_degradation_is_reported():
    note = source_note()
    note.blocks = [Block(kind="list", ordered=True, spans=[Span(text="first")])]
    plain, markup, warnings = encode_body(note)
    assert "Text|1. first" in plain and "1. first" in markup and warnings
    note.blocks[0].spans[0].text = "would <>><><<< split into another legacy row"
    with pytest.raises(BridgeError) as error:
        encode_body(note)
    assert error.value.code == "legacy_delimiter"


def configured_provider(tmp_path, *, version="19", cloud_id="new-cloud-note", empty=False,
                        common=None, home=None, kind="note"):
    calls = []
    def response(method, path, **kwargs):
        calls.append((path, kwargs))
        if path.endswith("/notetag/query"):
            return {"Result": {"code": "0"}, "rspInfo": {"noteList": []}, "ctagNoteTag": "tag-version"}
        if path.endswith("/simplenote/query"):
            return {"Result": {"code": "0"}, "rspInfo": {"noteList": [] if empty else [{"guid": "existing-target", "kind": kind}]},
                    "ctagNoteInfo": "note-version", "startCursor": "listing-cursor"}
        if path.endswith("/note/query"):
            return {"Result": {"code": "0"}, "rspInfo": {"guid": "existing-target", "data": json.dumps({"content": {"version": version}})},
                    "startCursor": "latest-cursor"}
        if path == "/html/getCommonParam":
            return {"code": 0, **(common or {})}
        if path == "/html/getHomeData":
            return {"code": 0, **(home or {})}
        assert path.endswith("/note/create") and kwargs["write"]
        return {"Result": {"code": "0"}, "rspInfo": {"guid": cloud_id}}
    provider = HuaweiProvider(SimpleNamespace(cookie=lambda _: "synthetic-csrf", json=response), tmp_path)
    provider.account_id, provider.write_supported = "target-account", True
    return provider, calls


@pytest.mark.parametrize("version", ["12", "19"])
def test_create_uses_current_format_and_cursor_but_never_existing_note_ids(tmp_path, version):
    provider, calls = configured_provider(tmp_path, version=version)
    note, store = source_note(), Store(tmp_path / "tasks.sqlite")
    runner = TaskRunner(store)
    for _ in range(2):
        runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
        runner.join()
        assert runner.current().status == TaskStatus.SUCCEEDED
    writes = [kwargs for path, kwargs in calls if path.endswith("/note/create")]
    assert len(writes) == 1
    data = writes[0]["json"]
    assert data["ctagNoteInfo"] == "note-version" and data["ctagNoteTag"] == "tag-version" and data["startCursor"] == "latest-cursor"
    body = json.loads(data["reqInfo"]["data"])
    assert data["guid"].startswith("newNote") and body["guid"] == data["guid"]
    assert body["content"]["version"] == version
    assert body["content"]["prefix_uuid"] == body["content"]["unstruct_uuid"] == ""
    assert body["fileList"] == []
    assert store.receipt(receipt_key(note, provider)) == {"status": "confirmed", "remote_ids": ["new-cloud-note"]}


@pytest.mark.parametrize("cloud_id", [None, "existing-target", "newNote-unconfirmed"])
def test_missing_or_existing_cloud_acknowledgement_remains_uncertain_without_retry(tmp_path, cloud_id):
    provider, calls = configured_provider(tmp_path, cloud_id=cloud_id)
    note, store = source_note(), Store(tmp_path / "tasks.sqlite")
    runner = TaskRunner(store)
    for _ in range(2):
        runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
        runner.join()
        assert runner.current().status == TaskStatus.NEEDS_REVIEW
    assert sum(path.endswith("/note/create") for path, _ in calls) == 1
    assert store.receipt(receipt_key(note, provider))["remote_ids"][0].startswith("newNote")


@pytest.mark.parametrize("options", [{"version": "future"}, {"version": None}, {"kind": "newnote"}])
def test_unverified_account_format_fails_before_remote_creation(tmp_path, options):
    provider, calls = configured_provider(tmp_path, **options)
    store = Store(tmp_path / "tasks.sqlite")
    runner = TaskRunner(store)
    runner.start("migrate", lambda ctx: migrate([source_note()], provider, store, ctx))
    runner.join()
    assert runner.current().status == TaskStatus.FAILED
    assert not any(path.endswith("/note/create") for path, _ in calls)
    assert not any(path.startswith("/html/") for path, _ in calls), "Unknown existing records cannot use the empty-account fallback."


@pytest.mark.parametrize("common,home,version", [
    ({}, {}, "12"),
    ({"notepadRecycleBinOnSwitch": True}, {}, "19"),
    ({"notepadRecycleBinOnSwitch": True}, {"notepadRecycleBinOnSwitch": False}, "12"),
    ({}, {"notepadRecycleBinOnSwitch": 1}, "19"),
    ({}, {"notepadRecycleBinOnSwitch": 0}, "12"),
])
def test_empty_account_uses_official_merged_feature_flags_and_preserves_layout(tmp_path, common, home, version):
    provider, calls = configured_provider(tmp_path, empty=True, common=common, home=home)
    note, store = source_note(), Store(tmp_path / "tasks.sqlite")
    note.blocks.insert(1, Block())
    runner = TaskRunner(store)
    runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
    runner.join()
    assert runner.current().status == TaskStatus.SUCCEEDED
    assert not any(path.endswith("/note/query") for path, _ in calls), "An empty account has no template note to read."
    config = [(path, args) for path, args in calls if path.startswith("/html/")]
    assert [path for path, _ in config] == ["/html/getCommonParam", "/html/getHomeData"]
    assert all(args["json"]["traceId"] == args["headers"]["x-hw-trace-id"] and callable(args["check_cancel"])
               and not args.get("write") for _, args in config)
    created = next(args["json"] for path, args in calls if path.endswith("/note/create"))
    body = json.loads(created["reqInfo"]["data"])
    assert body["content"]["version"] == version and created["startCursor"] == "listing-cursor"
    restored = parse_entry({"guid": "new-cloud-note", "kind": "note", "data": created["reqInfo"]["data"]},
                           "target-account", {})
    assert restored.blocks[1:5] == note.blocks and not restored.warnings


@pytest.mark.parametrize("common,home", [({"code": 8}, {}), ({}, {"code": 8}),
    ({}, {"notepadRecycleBinOnSwitch": "false"}), ({}, {"notepadRecycleBinOnSwitch": 2}),
    ({}, {"notepadRecycleBinOnSwitch": None})])
def test_empty_account_unknown_configuration_stops_before_creation(tmp_path, common, home):
    provider, calls = configured_provider(tmp_path, empty=True, common=common, home=home)
    store = Store(tmp_path / "tasks.sqlite")
    runner = TaskRunner(store)
    runner.start("migrate", lambda ctx: migrate([source_note()], provider, store, ctx))
    runner.join()
    assert runner.current().status == TaskStatus.FAILED
    assert any(issue.code == "format_version_pending" for issue in runner.current().issues)
    assert not any(path.endswith("/note/create") for path, _ in calls)


def test_empty_account_still_reports_unsupported_text_styles(tmp_path):
    provider, calls = configured_provider(tmp_path, empty=True)
    note, store = source_note(), Store(tmp_path / "tasks.sqlite")
    note.blocks[0].spans[0].highlight = True
    runner = TaskRunner(store)
    runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
    runner.join()
    assert runner.current().status == TaskStatus.PARTIAL
    assert any(issue.code == "format_downgrade" and "高亮" in issue.message for issue in runner.current().issues)
    created = next(args["json"] for path, args in calls if path.endswith("/note/create"))
    restored = parse_entry({"guid": "new-cloud-note", "kind": "note", "data": created["reqInfo"]["data"]},
                           "target-account", {})
    assert restored.blocks[1].text == note.blocks[0].text and restored.blocks[1].spans[0].bold


def test_group_uuid_is_used_for_new_note_and_cursor_refreshed_after_group_creation(tmp_path, monkeypatch):
    provider, calls = configured_provider(tmp_path)
    from note_bridge.providers import huawei_groups
    monkeypatch.setattr(huawei_groups, "target_group", lambda *args: ("new-category-uuid", ["group name adjusted"]))
    note, store = source_note(), Store(tmp_path / "group-tasks.sqlite")
    note.source_folder_id, note.source_folder_name = "source-folder", "work"
    runner = TaskRunner(store)
    runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
    runner.join()
    writes = [kwargs["json"] for path, kwargs in calls if path.endswith("/note/create")]
    assert len(writes) == 1
    assert json.loads(writes[0]["reqInfo"]["data"])["content"]["tag_id"] == "new-category-uuid"
    assert writes[0]["startCursor"] == "listing-cursor"
    assert runner.current().succeeded == 1

from datetime import datetime, timezone

import pytest
from PIL import Image

from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span


@pytest.fixture
def corpus(tmp_path):
    resources = tmp_path / "资源"
    resources.mkdir()
    Image.new("RGB", (120, 80), "#7563df").save(resources / "示例.png")
    (resources / "附件.txt").write_text("portable attachment 便携文件", encoding="utf-8")
    notes = [
        NoteDocument(
            platform=PlatformId.XIAOMI,
            account_id="synthetic-account",
            source_id="one",
            title="中文 English 😀",
            created_at=datetime(2026, 8, 15, 10, tzinfo=timezone.utc),
            updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            blocks=[
                Block(kind="heading", spans=[Span(text="正文标题")]),
                Block(
                    spans=[
                        Span(text="中文 English 😀 "),
                        Span(text="加粗", bold=True),
                        Span(text=" 斜体", italic=True),
                        Span(text=" 下划线", underline=True),
                        Span(text=" 高亮", highlight=True),
                    ]
                ),
                Block(kind="list", ordered=True, spans=[Span(text="first")]),
                Block(kind="list", ordered=True, spans=[Span(text="second")]),
                Block(kind="todo", checked=True, spans=[Span(text="complete")]),
                Block(kind="table", rows=[["列 A", "列 B"], ["value", "内容"]]),
                Block(kind="attachment", attachment_id="picture"),
                Block(kind="quote", spans=[Span(text="引文")]),
                Block(kind="code", spans=[Span(text="print('hello')\n```\nlong code")]),
            ],
            attachments=[
                Attachment(
                    id="picture", name="示例.png", kind="image", mime="image/png", local_path="示例.png"
                ),
                Attachment(id="file", name="附件.txt", kind="file", mime="text/plain", local_path="附件.txt"),
            ],
        ),
        NoteDocument(
            platform=PlatformId.XIAOMI,
            account_id="synthetic-account",
            source_id="two",
            title="同名",
            created_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
            blocks=[Block(spans=[Span(text="第二条 English 包含搜索词 中文")])],
        ),
        NoteDocument(
            platform=PlatformId.XIAOMI,
            account_id="synthetic-account",
            source_id="three",
            title="同名",
            blocks=[Block(spans=[Span(text="长文本" * 1500)])],
        ),
        NoteDocument(
            platform=PlatformId.XIAOMI, account_id="synthetic-account", source_id="four", title="", blocks=[]
        ),
    ]
    return resources, notes

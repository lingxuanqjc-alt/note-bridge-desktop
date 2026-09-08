import importlib.util
from pathlib import Path

from note_bridge.models import Block, Span

spec = importlib.util.spec_from_file_location("migration_content_check", Path(__file__).resolve().parents[1] / "scripts/migration_content_check.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_equivalent_span_boundaries_do_not_falsely_report_content_loss():
    expected = [Block(spans=[Span(text="创建时间：2"), Span(text="0"), Span(text="26")])]
    actual = [Block(spans=[Span(text="创建时间：2026")])]
    assert module.contains_content(actual, expected)
    assert len(expected[0].spans) == 3


def test_style_text_order_and_checkbox_changes_still_fail():
    expected = [Block(kind="todo", checked=True, spans=[Span(text="A", bold=True), Span(text="B")])]
    for actual in (
        [Block(kind="todo", checked=True, spans=[Span(text="AB")])],
        [Block(kind="todo", checked=True, spans=[Span(text="B"), Span(text="A", bold=True)])],
        [Block(kind="todo", checked=False, spans=expected[0].spans)],
    ):
        assert not module.contains_content(actual, expected)


def test_image_order_and_count_remain_significant():
    expected = [Block(kind="attachment", attachment_id="a"), Block(kind="attachment", attachment_id="b")]
    assert not module.contains_content(expected[::-1], expected)
    assert not module.contains_content(expected[:1], expected)


def test_explicit_paragraph_downgrade_preserves_each_line_style_and_empty_line():
    expected = [Block(spans=[Span(text="A\n\nB", bold=True)])]
    actual = [Block(spans=[Span(text="A", bold=True)]), Block(), Block(spans=[Span(text="B", bold=True)])]
    assert not module.contains_content(actual, expected)
    assert module.contains_content(actual, expected, paragraph_breaks=True)
    assert not module.contains_content([actual[0], actual[2]], expected, paragraph_breaks=True)
    actual[2].spans[0].bold = False
    assert not module.contains_content(actual, expected, paragraph_breaks=True)


def test_paragraph_downgrade_does_not_erase_task_structure():
    expected = [Block(kind="todo", checked=True, spans=[Span(text="A\nB")])]
    actual = [Block(spans=[Span(text="A")]), Block(spans=[Span(text="B")])]
    assert not module.contains_content(actual, expected, paragraph_breaks=True)

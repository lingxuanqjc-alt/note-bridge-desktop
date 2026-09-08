"""Deterministic rich-text conversion. Provider HTML never executes."""

from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

from lxml import etree
from lxml import html as lhtml

from .models import Block, Span

DEFAULT_LAYOUT_WARNING = "原笔记的文字颜色、字号、字体或对齐方式已转换为默认排版。"

ACTIVE_TAGS = {
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "form",
    "input",
    "button",
    "svg",
    "math",
    "link",
    "meta",
}
STRUCTURAL_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "li",
    "blockquote",
    "pre",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}


def safe_link(value: str | None) -> str | None:
    if not value or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() in ("http", "https") and parsed.hostname and not parsed.username:
            return value
        if parsed.scheme.lower() == "mailto" and parsed.path and not parsed.netloc:
            return value
    except ValueError:
        pass
    return None


def plain_blocks(text: str) -> list[Block]:
    return [Block(spans=[Span(text=line)]) for line in text.replace("\r\n", "\n").split("\n")]


def parse_html(markup: str, image_ids: dict[str, str] | None = None) -> tuple[list[Block], list[str]]:
    image_ids = image_ids or {}
    warnings: list[str] = []
    if not markup.strip():
        return [], warnings
    # recover tolerates vendor HTML; external entities and network access are disabled.
    parser = lhtml.HTMLParser(no_network=True, recover=True)
    try:
        root = lhtml.fragment_fromstring(markup, create_parent="div", parser=parser)
    except (etree.ParserError, ValueError):
        return plain_blocks(markup), ["正文不是有效 HTML，已按纯文本保留。"]
    blocks: list[Block] = []

    def styles(node, inherited: dict) -> dict:
        state = dict(inherited)
        tag = str(node.tag).lower()
        css = dict(
            (k.strip().lower(), v.strip().lower())
            for k, v in re.findall(r"([\w-]+)\s*:\s*([^;]+)", node.get("style", ""))
        )
        if any(key in css for key in ("color", "font-size", "font-family", "text-align", "vertical-align")):
            warnings.append(DEFAULT_LAYOUT_WARNING)
        if tag in ("b", "strong") or css.get("font-weight") in ("bold", "600", "700", "800", "900"):
            state["bold"] = True
        if tag in ("i", "em") or css.get("font-style") == "italic":
            state["italic"] = True
        decoration = css.get("text-decoration", "") + css.get("text-decoration-line", "")
        if tag == "u" or "underline" in decoration:
            state["underline"] = True
        if tag in ("s", "strike", "del") or "line-through" in decoration:
            state["strike"] = True
        if tag == "mark" or css.get("background-color", "transparent") not in (
            "transparent",
            "none",
            "inherit",
            "initial",
        ):
            state["highlight"] = True
        if tag == "code":
            state["code"] = True
        if tag == "a":
            state["link"] = safe_link(node.get("href"))
        return state

    def visit(
        node, inherited: dict, kind: str = "paragraph", level: int = 1,
        ordered: bool = False, checked: bool = False,
    ):
        tag = str(node.tag).lower()
        if tag in ACTIVE_TAGS or not isinstance(node.tag, str):
            if tag in ACTIVE_TAGS:
                warnings.append("已移除正文中的可执行或嵌入式内容。")
            return
        state = styles(node, inherited)
        if tag == "table":
            if node.xpath(".//b|.//strong|.//i|.//em|.//a|.//img|.//td[@rowspan or @colspan or @style]"):
                warnings.append("表格保留基础单元格文本，单元格样式、合并关系或内嵌媒体需要核对。")
            rows = [
                ["".join(cell.itertext()).strip() for cell in row.xpath("./th|./td")]
                for row in node.xpath(".//tr")
            ]
            blocks.append(Block(kind="table", rows=[row for row in rows if row]))
            return
        if tag == "hr":
            blocks.append(Block(kind="divider"))
            return
        if tag in ("img", "audio", "video"):
            src = node.get("src", "")
            if not src:
                src = next(iter(node.xpath("./source/@src")), "")
            attachment_id = image_ids.get(src)
            if attachment_id:
                blocks.append(Block(kind="attachment", attachment_id=attachment_id))
            else:
                warnings.append("正文包含未获取的媒体资源，已保留缺失提示。")
                blocks.append(Block(spans=[Span(text=f"[未获取附件：{node.get('alt', '') or tag}]")]))
            return
        if tag == "li":
            kind = "list"
            parent = node.getparent()
            ordered = parent is not None and parent.tag == "ol"
            level = min(6, max(1, len(node.xpath("ancestor::ul|ancestor::ol"))))
            completion = node.get("data-checked")
            if completion is not None:
                kind = "todo"
                checked = completion == "true"
                if completion not in ("true", "false"):
                    warnings.append("待办完成状态无法识别，请对照原笔记核对。")
        elif re.fullmatch(r"h[1-6]", tag):
            kind, level = "heading", int(tag[1])
        elif tag == "blockquote":
            kind = "quote"
        elif tag == "pre":
            blocks.append(Block(kind="code", spans=[Span(text="".join(node.itertext()))]))
            return
        pending: list[Span] = []

        def add(text, flags):
            if text:
                if pending and pending[-1].model_dump(exclude={"text"}) == Span(**flags).model_dump(
                    exclude={"text"}
                ):
                    pending[-1].text += text
                else:
                    pending.append(Span(text=text, **flags))

        def flush(force=False):
            nonlocal pending
            if force or any(span.text.strip() for span in pending):
                blocks.append(
                    Block(
                        kind=kind,
                        spans=pending,
                        level=level,
                        ordered=ordered,
                        checked=checked,
                    )
                )
            pending = []

        def inline(child, flags):
            child_tag = str(child.tag).lower()
            if child_tag in ACTIVE_TAGS or not isinstance(child.tag, str):
                if child_tag in ACTIVE_TAGS:
                    warnings.append("已移除正文中的可执行或嵌入式内容。")
                return
            if child_tag == "br":
                add("\n", flags)
                return
            if child_tag in STRUCTURAL_TAGS | {"table", "ul", "ol", "img", "audio", "video", "hr"}:
                flush()
                # Editors commonly wrap list-item text in div/p. Those wrappers must
                # not turn a completed task or a numbered item into an ordinary paragraph.
                child_kind = kind if kind == "quote" or (
                    kind in ("list", "todo") and child_tag in ("div", "p")
                ) else "paragraph"
                visit(child, flags, child_kind, level, ordered, checked)
                return
            child_flags = styles(child, flags)
            add(child.text, child_flags)
            for descendant in child:
                inline(descendant, child_flags)
                add(descendant.tail, child_flags)

        add(node.text, state)
        for child in node:
            inline(child, state)
            add(child.tail, state)
        # A sole BR is the editor/export representation of one empty paragraph.
        # Whitespace between container children is still formatting, not content.
        blank_paragraph = (tag == "p" and len(node) == 1 and node[0].tag == "br"
                           and not (node.text or "").strip() and not (node[0].tail or "").strip())
        if blank_paragraph:
            pending = []
        flush(force=blank_paragraph or (tag in ("p", "li") and not len(node) and not node.text))

    visit(root, {})
    return blocks, list(dict.fromkeys(warnings))


def span_html(span: Span) -> str:
    result = html.escape(span.text).replace("\n", "<br>")
    for enabled, tag in (
        (span.code, "code"),
        (span.bold, "strong"),
        (span.italic, "em"),
        (span.underline, "u"),
        (span.strike, "s"),
        (span.highlight, "mark"),
    ):
        if enabled:
            result = f"<{tag}>{result}</{tag}>"
    link = safe_link(span.link)
    if link:
        result = f'<a href="{html.escape(link, quote=True)}" target="_blank" rel="noopener noreferrer">{result}</a>'
    return result


def block_html(block: Block, attachments: dict[str, tuple[str, str, str]], ordinal: int = 1) -> str:
    text = "".join(span_html(span) for span in block.spans)
    match block.kind:
        case "heading":
            return f"<h{min(6, block.level + 1)}>{text}</h{min(6, block.level + 1)}>"
        case "list":
            tag = "ol" if block.ordered else "ul"
            start = f' start="{ordinal}"' if block.ordered else ""
            return f'<{tag}{start} style="margin-left:{(block.level - 1) * 1.4}em"><li>{text}</li></{tag}>'
        case "todo":
            return f'<p class="todo">{"☑" if block.checked else "☐"} {text}</p>'
        case "quote":
            return f"<blockquote>{text}</blockquote>"
        case "code":
            return f"<pre><code>{html.escape(block.text)}</code></pre>"
        case "divider":
            return "<hr>"
        case "table":
            return (
                "<table>"
                + "".join(
                    "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>"
                    for row in block.rows
                )
                + "</table>"
            )
        case "attachment":
            asset = attachments.get(block.attachment_id or "")
            if not asset:
                return '<p class="warning">附件未获取</p>'
            url, name, kind = map(lambda value: html.escape(value, quote=True), asset)
            if kind == "image":
                return f'<figure><img src="{url}" alt="{name}" loading="lazy"><figcaption>{name}</figcaption></figure>'
            if kind in ("audio", "video"):
                return f'<figure><{kind} controls preload="none" src="{url}"></{kind}><figcaption>{name}</figcaption></figure>'
            return f'<p>📎 <a href="{url}" download>{name}</a></p>'
        case _:
            return f"<p>{text or '<br>'}</p>"


def numbered_blocks(blocks: list[Block]):
    counters = {}
    for block in blocks:
        if block.kind == "list":
            counters = {level: value for level, value in counters.items() if level <= block.level}
            counters[block.level] = counters.get(block.level, 0) + 1 if block.ordered else 0
        else:
            counters.clear()
        yield block, counters.get(block.level, 1)


def blocks_html(blocks: list[Block], attachments: dict[str, tuple[str, str, str]]) -> str:
    return "".join(block_html(block, attachments, ordinal) for block, ordinal in numbered_blocks(blocks))


def markdown_span(span: Span) -> str:
    if span.code:
        ticks = "`" * (max((len(m[0]) for m in re.finditer(r"`+", span.text)), default=0) + 1)
        result = f"{ticks} {span.text} {ticks}"
    else:
        # Escape raw HTML first; backslashes alone are not safe inside inline HTML wrappers.
        result = html.escape(span.text, quote=False)
        result = re.sub(r"([\\`*_{}\[\]|])", r"\\\1", result)
    # Markdown emphasis delimiters cannot border whitespace inside the span.
    # Inline HTML preserves both the original spacing and combined emphasis.
    spaced = not span.code and bool(span.text) and (span.text[0].isspace() or span.text[-1].isspace())
    for enabled, prefix, suffix in (
        (span.bold, "<strong>" if spaced else "**", "</strong>" if spaced else "**"),
        (span.italic, "<em>" if spaced else "*", "</em>" if spaced else "*"),
        (span.strike, "<del>" if spaced else "~~", "</del>" if spaced else "~~"),
        (span.underline, "<u>", "</u>"),
        (span.highlight, "<mark>", "</mark>"),
    ):
        if enabled:
            result = prefix + result + suffix
    link = safe_link(span.link)
    if link:
        result = f"[{result}]({link.replace(')', '%29').replace('(', '%28').replace(' ', '%20')})"
    return result


def blocks_markdown(blocks: list[Block], attachments: dict[str, tuple[str, str, str]]) -> str:
    lines = []
    for block, ordinal in numbered_blocks(blocks):
        text = "".join(markdown_span(span) for span in block.spans)
        match block.kind:
            case "heading":
                lines.append("#" * min(6, block.level + 1) + " " + text)
            case "list":
                lines.append("  " * (block.level - 1) + (f"{ordinal}. " if block.ordered else "- ") + text)
            case "todo":
                lines.append(f"- [{'x' if block.checked else ' '}] {text}")
            case "quote":
                lines.append("> " + text.replace("\n", "\n> "))
            case "code":
                fence = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", block.text)), default=0))
                lines.append(f"{fence}\n{block.text}\n{fence}")
            case "divider":
                lines.append("---")
            case "table":
                if block.rows:
                    width = max(map(len, block.rows))
                    rows = [row + [""] * (width - len(row)) for row in block.rows]
                    table = [
                        "| "
                        + " | ".join(
                            html.escape(cell).replace("|", "\\|").replace("\n", "<br>") for cell in row
                        )
                        + " |"
                        for row in rows
                    ]
                    table.insert(1, "| " + " | ".join(["---"] * width) + " |")
                    lines.append("\n".join(table))
            case "attachment":
                asset = attachments.get(block.attachment_id or "")
                if asset:
                    url, name, kind = asset
                    if kind == "image":
                        lines.append(f"![{markdown_span(Span(text=name))}]({url})")
                    elif kind in ("audio", "video"):
                        lines.append(f'<{kind} controls src="{html.escape(url, quote=True)}"></{kind}>')
                    else:
                        lines.append(f"[{markdown_span(Span(text=name))}]({url})")
                else:
                    lines.append("[附件未获取]")
            case _:
                lines.append(text)
    return "\n\n".join(lines)

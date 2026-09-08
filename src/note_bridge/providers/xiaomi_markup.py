"""Parse the newline-delimited Xiaomi dialect, whose h1/h2/h3 are empty markers."""
from html.parser import HTMLParser

from ..models import Block, Span
from ..richtext import ACTIVE_TAGS, safe_link


class LegacyParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.warnings = []
        self.media = []
        self.stack = []
        self.quote_depth = 0
        self.lists = []
        self.skip = []
        self.links = []
        self.link = None
        self.line_closed = False
        self.current = Block()

    def fresh(self):
        self.current = Block(kind="quote" if self.quote_depth else "paragraph")

    def flush(self, force=False):
        if (force and not self.links) or self.current.spans or self.current.kind not in ("paragraph", "quote"):
            self.blocks.append(self.current)
        self.blocks.extend(self.links)
        self.links = []
        self.fresh()

    def newline(self):
        if not self.line_closed:
            self.flush(True)
        self.line_closed = False

    def level(self, value):
        try:
            level = int(value or 1)
        except (TypeError, ValueError):
            level = 1
            self.warnings.append("小米缩进无法识别，已按一级保留文字。")
        if not 1 <= level <= 6:
            self.warnings.append("小米缩进超出支持范围，已限制在一至六级。")
        return min(6, max(1, level))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.skip:
            if tag not in ("br", "img", "input", "hr", "meta", "link"):
                self.skip.append(tag)
            return
        if tag in ACTIVE_TAGS and tag != "input":
            self.warnings.append("已移除正文中的可执行或嵌入式内容。")
            if tag not in ("meta", "link"):
                self.skip.append(tag)
            return
        if tag == "new-format":
            return
        if tag == "br":
            self.warnings.append("小米原生正文不识别 HTML 换行标签，需核对旧笔记换行。")
            return
        if tag == "a":
            self.link = {"href": safe_link(attrs.get("href")), "text": ""}
            self.warnings.append("小米超链接使用独立链接卡片，原段落内位置和样式需核对。")
            return
        if tag in ("img", "sound", "hr"):
            self.flush()
            if tag == "hr":
                self.blocks.append(Block(kind="divider"))
            elif attrs.get("fileid"):
                identity = attrs["fileid"]
                self.media.append((identity, "image" if tag == "img" else "audio"))
                self.blocks.append(Block(kind="attachment", attachment_id=identity))
            else:
                self.warnings.append("发现没有文件标识的媒体内容。")
            self.line_closed = True
            return
        if tag in ("input", "bullet", "order", "h1", "h2", "h3"):
            if tag == "input" and attrs.get("type") != "checkbox":
                self.warnings.append("已移除正文中的可执行或嵌入式内容。")
                return
            self.line_closed = False
            if self.current.spans:
                self.warnings.append("小米段落标记位置异常，已保留文字并拆分段落。")
                self.flush()
            if tag.startswith("h"):
                self.current.kind, self.current.level = "heading", int(tag[1])
            else:
                self.current.kind = "todo" if tag == "input" else "list"
                self.current.checked = tag == "input" and attrs.get("checked") == "true"
                self.current.ordered = tag == "order"
                self.current.level = self.level(attrs.get("indent"))
                if tag == "order" and attrs.get("inputnumber") not in (None, "0", "1"):
                    self.warnings.append("小米列表起始编号需对照原文核对，当前模型按连续列表编号。")
            return
        if tag == "quote":
            self.flush()
            self.quote_depth += 1
            self.fresh()
            return
        if tag in ("ul", "ol"):
            self.flush()
            self.lists.append(tag)
            return
        if tag == "li":
            self.flush()
            self.current = Block(kind="list", ordered=bool(self.lists and self.lists[-1] == "ol"),
                                 level=self.level(len(self.lists)))
            return
        if tag == "del":
            self.current.kind, self.current.checked = "todo", True
        flags = {}
        mark = {"b": "bold", "i": "italic", "u": "underline", "delete": "strike", "background": "highlight"}.get(tag)
        if mark:
            flags[mark] = True
        elif tag in ("strong", "em", "s", "strike", "mark", "code"):
            self.warnings.append("小米正文含原生编辑器不识别的 HTML 样式标记，已保留文字。")
        elif tag in ("size", "mid-size", "h3-size", "center", "right", "left"):
            self.warnings.append("原笔记的字号或段落对齐已转换为默认排版。")
        self.stack.append((tag, flags))

    def handle_endtag(self, tag):
        if self.skip:
            if tag in self.skip:
                del self.skip[len(self.skip) - 1 - self.skip[::-1].index(tag):]
            return
        if tag == "a" and self.link is not None:
            self.links.append(Block(spans=[Span(text=self.link["text"], link=self.link["href"])]))
            self.link = None
            return
        if tag == "quote":
            self.flush()
            self.quote_depth = max(0, self.quote_depth - 1)
            self.fresh()
            self.line_closed = True
        elif tag in ("ul", "ol", "li"):
            self.flush()
            if tag != "li" and self.lists:
                self.lists.pop()
            self.line_closed = True
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.skip:
            return
        if self.link is not None:
            self.link["text"] += data
            return
        flags = {}
        for _, mark in self.stack:
            flags.update(mark)
        for index, part in enumerate(data.split("\n")):
            if index:
                self.newline()
            if part:
                self.line_closed = False
                span = Span(text=part, **flags)
                if self.current.spans and self.current.spans[-1].model_dump(exclude={"text"}) == span.model_dump(exclude={"text"}):
                    self.current.spans[-1].text += part
                else:
                    self.current.spans.append(span)


def parse_legacy(raw):
    parser = LegacyParser()
    parser.feed(raw)
    parser.close()
    if parser.link is not None:
        parser.handle_endtag("a")
        parser.warnings.append("小米链接标记未闭合，已保留链接文字。")
    parser.flush()
    return parser.blocks, list(dict.fromkeys(parser.warnings)), parser.media

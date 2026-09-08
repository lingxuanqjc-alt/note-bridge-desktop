"""Current Honor editor's documented-in-client h_n / h-text dialect, independently encoded.

Source: official note/main.2b996175.js, J0/go/gE and the editor's class enums.
These class names are serialization markers, not copied styles or UI assets.
"""

from html import escape

from lxml import html

from ..errors import BridgeError
from ..exporter import stamp
from ..models import NoteDocument, Span
from ..richtext import numbered_blocks, safe_link


def normalize(markup: str):
    root = html.fragment_fromstring(markup or "", create_parent="div")
    warnings = []
    native = {"Nb", "Na", "Nc", "Nd", "Nh", "Ne", "Nf", "Ng", "Ni", "Nk", "Nm", "Nn", "Nr"}
    for node in list(root.iter()):
        if not isinstance(node.tag, str):
            continue
        classes = set(node.get("class", "").split())
        if "h_n" in classes and not classes.intersection(native):
            warnings.append("荣耀专有内容块尚未完成转换，已保留可读取文本，请核对原笔记。")
        if classes.intersection({"Aj", "Ak", "Am"}) or node.get("hindent") not in (None, "0"):
            warnings.append("荣耀段落对齐或缩进已转换为默认排版。")
        if "Na" in classes:
            checked = "Sc" in classes or bool(node.xpath('.//*[contains(concat(" ",normalize-space(@class)," ")," Sc ")]'))
            # Icons are editor controls; only h-text descendants are note text in J0.
            children = list(node.iter("h-text"))
            node.clear()
            node.tag = "li"
            node.set("data-checked", "true" if checked else "false")
            for child in children:
                node.append(child)
        elif "Nb" in classes:
            level = node.get("hlevel", "0")
            node.tag = "h" + level if level in ("1", "2", "3", "4", "5", "6") else "p"
        elif "Nd" in classes:
            node.tag = "ol"
        elif "Nh" in classes:
            node.tag = "ul"
        elif "Nc" in classes:
            node.tag = "li"
        elif "Nr" in classes:
            node.tag = "hr"
        elif "Ni" in classes:
            identity = node.get("hid", "")
            node.clear()
            node.tag = "img"
            node.set("src", "cid:" + identity)
        if node.tag == "h-text":
            node.tag = "span"
        if node.tag == "font":
            css = node.get("style", "")
            if "La" in classes:
                css += ";font-weight:bold"
            if "Lb" in classes:
                css += ";font-style:italic"
            decoration = ("underline " if "Lc" in classes else "") + ("line-through" if "Ld" in classes else "")
            if decoration:
                css += ";text-decoration:" + decoration
            if node.get("hbgcolor") or "Le" in classes:
                css += ";background-color:yellow"
            if node.get("hsize") not in (None, "16") or node.get("hcolor"):
                warnings.append("荣耀文字字号或颜色已转换为默认排版。")
            if node.get("hlink"):
                node.tag = "a"
                node.set("href", node.get("hlink"))
            else:
                node.tag = "span"
            node.set("style", css.lstrip(";"))
        if classes.intersection({"Pb", "Pc", "Pd", "Pe", "Pg", "Ph"}):
            warnings.append("荣耀自定义列表标记已转换为普通编号或项目符号。")
    return html.tostring(root, encoding="unicode"), list(dict.fromkeys(warnings))


def encode(note: NoteDocument, images: dict[str, str] | None = None):
    references = {block.attachment_id for block in note.blocks if block.kind == "attachment"}
    if (note.attachments or references) and images is None:
        raise BridgeError("attachment_upload_pending", "荣耀图片上传尚未完成验证，未执行创建。")
    if images is not None and (references != set(images) or {a.id for a in note.attachments} != set(images)):
        raise BridgeError("attachment_mismatch", "荣耀图片与正文引用不一致，未执行创建。")
    warnings, markup = [], []

    def spans(values):
        result = []
        for span in values:
            classes = " ".join(code for flag, code in ((span.bold, "La"), (span.italic, "Lb"),
                               (span.underline, "Lc"), (span.strike, "Ld")) if flag)
            attrs = ' hsize="16" class="' + classes + '"'
            if span.highlight:
                attrs += ' hbgcolor="#E5B10533"'
            if safe_link(span.link):
                attrs += ' hlink="' + escape(span.link, quote=True) + '"'
            if span.code:
                warnings.append("荣耀行内代码已保留文本，等宽字体未保留。")
            result.append('<h-text><font' + attrs + '><span>' + escape(span.text) + '</span></font></h-text>')
        return "".join(result)

    def paragraph(text):
        return '<p class="Nb h_n">' + spans([Span(text=text)]) + '</p>'

    for block, ordinal in numbered_blocks(note.blocks):
        content = spans(block.spans)
        if block.kind == "attachment":
            identity = escape(images[block.attachment_id], quote=True)
            markup.append('<div class="Nk h_n"><div class="Ni h_n" void="1" hid="' + identity
                          + '"><img src="cid:' + identity + '"></div></div>')
        elif block.kind in ("paragraph", "heading"):
            level = ' hlevel="' + str(block.level) + '"' if block.kind == "heading" else ""
            markup.append('<p class="Nb h_n"' + level + '>' + content + '</p>')
        elif block.kind == "todo":
            markup.append('<div class="Na h_n ' + ("Sc" if block.checked else "Sb") + '">' + content + '</div>')
        elif block.kind == "list":
            tag, classes = ("ol", "Nd Pa") if block.ordered else ("ul", "Nh Pf")
            start = f' start="{ordinal}"' if block.ordered else ""
            markup.append(f'<{tag} class="{classes} h_n"{start}><li class="Nc h_n">{content}</li></{tag}>')
            if block.level > 1:
                warnings.append("荣耀嵌套列表已展平，列表文本和顺序保留。")
        else:
            text = "\n".join("\t".join(row) for row in block.rows) if block.kind == "table" else block.text
            markup.append(paragraph(text or ("——" if block.kind == "divider" else "")))
            warnings.append("荣耀表格、代码块、引用或分隔线已转换为普通段落。")
    markup.append(paragraph("来源笔记时间\n" + stamp(note)))
    if note.source_folder_name:
        markup.append(paragraph("来源文件夹：" + note.source_folder_name))
    body = "".join(markup)
    if len(note.plain_text.encode("utf-16-le")) // 2 > 100000:
        raise BridgeError("note_too_long", "荣耀超过十万字的正文尚未完成写入验证。")
    return body, list(dict.fromkeys(warnings))

"""Explicit acceptance policy for reported format losses, independent of encoders."""
from note_bridge.models import Block, Span


def expected_blocks(note, target, warnings):
    blocks = [block.model_copy(deep=True) for block in note.blocks]
    required = set()
    list_index = 0
    for index, block in enumerate(blocks):
        list_index = list_index + 1 if block.kind == "list" else 0
        if target == "xiaomi":
            if block.kind == "table":
                required.add("表格转换为制表符分隔文本。")
                blocks[index] = Block(spans=[Span(text="\n".join("\t".join(row) for row in block.rows))])
            elif block.kind == "code":
                required.add("部分段落样式转换为普通文本。")
                blocks[index] = Block(spans=block.spans)
            elif block.kind == "heading" and block.level > 3:
                required.add("小米仅支持三级标题，更深标题已调整为三级。")
                block.level = 3
            if any("\n" in span.text for span in block.spans):
                required.add("小米区块内换行已转换为独立段落，跨行样式需核对。")
        expanded_spans = []
        for span in block.spans:
            if target == "xiaomi" and span.code:
                required.add("小米行内代码已保留文本，等宽字体未保留。")
                span.code = False
            elif target == "wps" and (span.strike or span.highlight or span.code or span.link):
                address = span.link
                address_report = "WPS 超链接已转换为链接文字及明文地址，原链接样式未保留。"
                if span.strike or span.highlight or span.code or (address and address_report not in warnings):
                    required.add("WPS 便签不保留删除线、高亮、行内代码或超链接样式，已保留文字。")
                span.strike = span.highlight = span.code = False
                span.link = None
                if address and address_report in warnings:
                    required.add(address_report)
                    expanded_spans.append(span)
                    if span.text != address:
                        expanded_spans.append(Span(text=" (" + address + ")"))
                    continue
            elif target == "huawei" and (span.strike or span.highlight or span.code or span.link):
                required.add("华为删除线、高亮、代码字体或链接已保留文本，样式未保留。")
                span.strike = span.highlight = span.code = False
                span.link = None
            elif target in ("meizu", "honor") and span.code:
                required.add("行内代码字体已转换为普通字体。" if target == "meizu" else "荣耀行内代码已保留文本，等宽字体未保留。")
                span.code = False
            expanded_spans.append(span)
        block.spans = expanded_spans
        if target == "huawei" and block.kind == "list":
            required.add("华为列表已转换为带编号或符号的普通段落。")
            # Nested numbering requires a separate layout review, not a guessed equivalence.
            if block.level != 1:
                return None
            prefix = f"{list_index}. " if block.ordered else "• "
            spans = [Span(text=prefix), *block.spans]
            blocks[index] = Block(spans=spans)
        elif target == "huawei" and block.kind not in ("paragraph", "todo", "attachment"):
            required.add("华为标题层级、引用、代码块、表格或分隔线已转换为普通段落。")
            if block.kind == "table":
                spans = [Span(text="\n".join("\t".join(row) for row in block.rows))]
            elif block.kind == "divider":
                spans = [Span(text="——")]
            else:
                spans = block.spans
            blocks[index] = Block(spans=spans)
        elif target == "honor" and block.kind in ("table", "code", "quote", "divider"):
            required.add("荣耀表格、代码块、引用或分隔线已转换为普通段落。")
            text = "\n".join("\t".join(row) for row in block.rows) if block.kind == "table" else block.text
            blocks[index] = Block(spans=[Span(text=text or ("——" if block.kind == "divider" else ""))])
        elif target in ("meizu", "wps") and block.kind == "table":
            required.add("表格转换为制表符分隔文本。" if target == "meizu" else "WPS 表格已按行和制表符转换为文字。")
            blocks[index] = Block(spans=[Span(text="\n".join("\t".join(row) for row in block.rows))])
        elif target in ("meizu", "wps") and block.kind in ("quote", "code"):
            required.add("引用或代码块转换为普通段落。" if target == "meizu" else "WPS 引用或代码区块已转换为普通文字。")
            blocks[index] = Block(spans=block.spans)
        elif target in ("meizu", "wps") and block.kind == "divider":
            required.add("分隔线转换为普通文字。" if target == "meizu" else "WPS 分隔线已转换为文字。")
            blocks[index] = Block(spans=[Span(text="────────────" if target == "meizu" else "────────")])
        if block.kind == "list" and block.level > 1 and target in ("meizu", "wps", "honor"):
            required.add({"meizu": "嵌套列表转换为单层列表。", "wps": "WPS 多层列表已转换为单层列表。",
                          "honor": "荣耀嵌套列表已展平，列表文本和顺序保留。"}[target])
            blocks[index].level = 1
        if block.kind == "heading" and target == "wps":
            level = 2 if block.level <= 2 else 3
            if level != block.level:
                required.add("WPS 标题级别已调整为二级或三级标题。")
                blocks[index].level = level
    paragraph_breaks = target == "wps" and any(
        "\n" in span.text for block in blocks if block.kind == "paragraph" for span in block.spans)
    if paragraph_breaks:
        required.add("WPS 区块内换行已转换为独立段落，跨行样式需核对。")
    if target == "xiaomi":
        paragraph_breaks = True
        cards = []
        for block in blocks:
            if not any(span.link for span in block.spans):
                cards.append(block)
                continue
            required.add("小米超链接使用独立链接卡片，原段落内位置和样式需核对。")
            # The independently checked native contract lifts inline anchors after
            # their paragraph. Multiline/structural cards need separate evidence.
            if block.kind != "paragraph" or any("\n" in span.text for span in block.spans):
                return None
            text = block.model_copy(update={"spans": [span for span in block.spans if not span.link]})
            if text.spans:
                cards.append(text)
            cards.extend(Block(spans=[Span(text=span.text, link=span.link)]) for span in block.spans if span.link)
        blocks = cards
    if not required.issubset(set(warnings)):
        return None
    return blocks, paragraph_breaks

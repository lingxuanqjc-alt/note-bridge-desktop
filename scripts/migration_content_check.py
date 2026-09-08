"""Ignore only redundant boundaries between adjacent identically styled spans."""


def comparable_blocks(blocks, paragraph_breaks=False):
    result = []
    for block in blocks:
        value = block.model_dump(mode="json")
        lines = [[]]
        for span in value["spans"]:
            parts = span["text"].split("\n") if paragraph_breaks and value["kind"] == "paragraph" else [span["text"]]
            for index, text in enumerate(parts):
                if index:
                    lines.append([])
                if not text:
                    continue
                merged = lines[-1]
                style = {key: item for key, item in span.items() if key != "text"}
                if merged and {key: item for key, item in merged[-1].items() if key != "text"} == style:
                    merged[-1]["text"] += text
                else:
                    merged.append({**span, "text": text})
        result.extend({**value, "spans": line} for line in lines)
    return result


def contains_content(actual, expected, *, paragraph_breaks=False):
    actual, expected = comparable_blocks(actual, paragraph_breaks), comparable_blocks(expected, paragraph_breaks)
    return bool(expected) and any(actual[index:index + len(expected)] == expected for index in range(len(actual)))

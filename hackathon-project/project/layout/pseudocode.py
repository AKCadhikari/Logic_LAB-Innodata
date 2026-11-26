"""Preserve physical lines in explicitly labeled pseudocode regions."""
from dataclasses import replace
import re


LABEL = re.compile(r"^\s*(?:algorithm|pseudocode|pseudo-code)\s*(?:\d+\b|[:.])", re.I)
CONTROL = re.compile(r"^\s*(?:while\b.+\bdo\b|for\b.+\bdo\b|if\b.+\bthen\b|end\s*(?:if|for|while)\b)", re.I)
END = re.compile(r"^\s*(?:return\b|end\s*(?:algorithm|procedure)\b)", re.I)


def preserve_pseudocode(page):
    index = 0
    while index < len(page.blocks):
        label = page.blocks[index]
        index += 1
        if label.tag not in {"p", "h2", "h3", "h4", "h5", "h6"} or not LABEL.match(label.text):
            continue
        lines, boxes = [], []
        last = label
        for end in range(index, min(len(page.blocks), index + 60)):
            block = page.blocks[end]
            if (block.tag != "p" or block.kind not in {"body", ""} or not (last.bbox and block.bbox)
                    or not -2 <= block.bbox[1] - last.bbox[3] <= max(13, last.size * 2)):
                break
            parts = (block.original_text or block.text).splitlines()
            physical = block.line_boxes or [block.bbox]
            if len(parts) != len(physical):
                break
            lines.extend(parts)
            boxes.extend(physical)
            last = block
            if not END.match(parts[-1]):
                continue
            if sum(bool(CONTROL.match(line)) for line in lines) < 3:
                break
            edges = []
            for x in sorted(box[0] for box in boxes):
                if not edges or x - edges[-1] > 4:
                    edges.append(x)
            text = "\n".join("  " * min(range(len(edges)), key=lambda rank: abs(edges[rank] - box[0]))
                             + line.strip() for line, box in zip(lines, boxes))
            bbox = (min(box[0] for box in boxes), min(box[1] for box in boxes),
                    max(box[2] for box in boxes), max(box[3] for box in boxes))
            reference = dict(page.blocks[index].source_reference)
            ids = list(dict.fromkeys(key for item in page.blocks[index:end + 1]
                                      for key in item.source_reference.get("execution_ids", [])))
            if ids:
                reference["execution_ids"] = ids
            label.tag, label.kind = "p", "algorithm_caption"
            page.blocks[index:end + 1] = [
                replace(page.blocks[index], tag="p", kind="pseudocode", text=text, spans=[],
                        original_text="\n".join(lines), bbox=bbox, line_boxes=boxes, source_reference=reference)
            ]
            break

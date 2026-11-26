"""Compare final XML to the source-linked intermediate representation."""
from collections import Counter, defaultdict
import re

from lxml import etree

from utils.textutil import norm_ws


NUMBER = re.compile(r"[+−-]?\d+(?:[.,٫٬]\d+)*(?:[eE][+−-]?\d+)?(?:\s*[%٪‰])?")
TEXT_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "reference", "td", "th",
             "caption", "figcaption", "aside", "pre", "blockquote", "header", "footer", "page_header", "page_footer"}


def numeric_values(text):
    return Counter("".join(match[0].split()) for match in NUMBER.finditer(text))


def compare_content(data, document, locations, *, sample=False):
    root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    by_path = {root.getroottree().getpath(node): node for node in root.iter() if isinstance(node.tag, str)}
    order = {id(node): i for i, node in enumerate(by_path.values())}
    totals = Counter()
    issues, uncertain, styles, mathematics = [], [], [], []
    claims = defaultdict(list)
    used = set()
    compared_numbers = defaultdict(Counter)
    last_position = -1
    last_path = None

    def problem(page, path, kind, **details):
        issues.append({"page": page.n if page else None, "node": path, "kind": kind, **details})

    def claim(page, key, expected, kind, *, original=None):
        path = locations.get(key)
        if not expected:
            return
        totals[f"{kind}_expected"] += 1
        node = by_path.get(path)
        if node is None:
            totals["numeric_expected"] += sum(numeric_values(expected).values())
            problem(page, path, f"missing_{kind}", expected=expected)
            return
        used.add(path)
        claims[path].append((page, expected, kind))
        if original and numeric_values(original) != numeric_values(expected):
            uncertain.append({"page": page.n, "node": path, "kind": "source_numeric_disagreement",
                              "original": dict(numeric_values(original)), "ir": dict(numeric_values(expected))})

    def visit(page, block):
        nonlocal last_position, last_path
        path = locations.get(id(block))
        node = by_path.get(path)
        if node is not None and path != last_path:
            index = order[id(node)]
            if index < last_position:
                problem(page, path, "reordered_content")
            last_position, last_path = index, path
        if block.kind == "equation":
            totals["equations_expected"] += 1
            image = node.find("img") if node is not None else None
            carrier = image if image is not None else node
            mathematics.append({"page": page.n, "node": path, "label": block.marker or None,
                                "encoding": carrier.get("formula_encoding") if carrier is not None else None,
                                "verification": carrier.get("formula_status") if carrier is not None else None,
                                "has_source_crop": bool(image is not None and image.get("src"))})
            if carrier is None or (carrier.tag == "img" and not carrier.get("src")):
                problem(page, path, "missing_equation")
            else:
                totals["equations_represented"] += 1
                if carrier.get("formula_encoding"):
                    totals["equations_transcribed"] += 1
                    if image is not None:
                        expected_values = numeric_values(block.text)
                        actual_values = numeric_values(image.get("alt", ""))
                        compared_numbers[page.n].update(actual_values)
                        totals["numeric_expected"] += sum(expected_values.values())
                        totals["numeric_represented"] += sum((expected_values & actual_values).values())
                        if image.get("alt") != block.text:
                            problem(page, path, "formula_text_changed")
                    else:
                        claim(page, id(block), block.text, "formula")
                else:
                    uncertain.append({"page": page.n, "node": path, "kind": "unverified_formula_transcription"})
        elif block.tag in {"figure", "table"}:
            category = "figures" if block.tag == "figure" else "tables"
            totals[f"{category}_expected"] += 1
            if node is None or (block.tag == "table" and node.tag != "table"):
                problem(page, path, f"missing_{block.tag}")
            elif block.tag == "figure" and not node.xpath("./img[@src]"):
                problem(page, path, "missing_figure")
            else:
                totals[f"{category}_represented"] += 1
                if block.tag == "table" and not block.cells:
                    uncertain.append({"page": page.n, "node": path, "kind": "table_without_source_cells"})
        else:
            kind = "heading" if block.tag in {"h1", "h2", "h3", "h4", "h5", "h6"} else "paragraph"
            expected = f"{block.marker} {block.text}" if sample and block.tag == "li" and block.marker else block.text
            claim(page, id(block), expected, kind, original=block.original_text if block.tag != "li" else None)
        claim(page, (id(block), "caption"), block.caption, "caption")
        for cell in block.cells:
            claim(page, id(cell), cell.text, "cell", original=cell.original_text)
            target = by_path.get(locations.get(id(cell)))
            if target is not None and (target.tag != ("th" if cell.header else "td")
                    or target.get("rowspan", "1") != str(cell.rowspan)
                    or target.get("colspan", "1") != str(cell.colspan)):
                problem(page, locations.get(id(cell)), "table_cell_structure_changed")
        for child in block.children:
            visit(page, child)
        if node is not None and (block.bbox or block.spans or block.cells):
            native = [{"font_family": span.font, "font_size_pt": span.size, "bold": span.bold,
                       "italic": span.italic, "underline": span.underline, "color": span.color,
                       "superscript": span.sup, "subscript": span.sub}
                      for span in block.spans if span.font and span.text.strip()] if block.source == "native" else []
            line_spacing = [round((right[1] + right[3] - left[1] - left[3]) / 2, 3)
                            for left, right in zip(block.line_boxes, block.line_boxes[1:])
                            if right[1] > left[1]]
            styles.append({"page": page.n, "node": path, "bbox": block.bbox, "line_boxes": block.line_boxes,
                           "line_center_spacing_pt": line_spacing, "direction": node.get("dir"),
                           "cells": [{"row": cell.row, "column": cell.col, "bbox": cell.bbox}
                                     for cell in block.cells if cell.bbox],
                           "source_styles": native, "alignment": node.get("align"),
                           "alignment_basis": "inferred_from_layout" if node.get("align") else None,
                           "font_size_basis": "native_text_measurement" if native else
                           "estimated_from_layout" if node.get("font_size") and block.source == "ocr" else None})

    for page in document.pages:
        if page.source != "native" or page.requires_review:
            uncertain.append({"page": page.n, "kind": "source_extraction_requires_review",
                              "source": page.source, "reasons": page.diag.get("review_reasons", [])})
        if not page.blocks:
            uncertain.append({"page": page.n, "kind": "no_source_blocks"})
        for block in page.blocks:
            visit(page, block)

    for path, entries in claims.items():
        page = entries[0][0]
        represented = norm_ws("".join(by_path[path].itertext()))
        expected = norm_ws(" ".join(text for _, text, _ in entries))
        expected_values, actual_values = numeric_values(expected), numeric_values(represented)
        compared_numbers[page.n].update(actual_values)
        totals["numeric_expected"] += sum(expected_values.values())
        totals["numeric_represented"] += sum((expected_values & actual_values).values())
        if expected_values != actual_values:
            problem(page, path, "numerical_discrepancy", missing=dict(expected_values - actual_values),
                    added=dict(actual_values - expected_values))
        if expected != represented:
            problem(page, path, "content_changed", expected=expected, actual=represented)
        elif by_path[path].get("kind") == "pseudocode" and (
                len(entries) != 1 or "".join(by_path[path].itertext()) != entries[0][1]):
            problem(page, path, "pseudocode_whitespace_changed")
        else:
            totals.update(f"{kind}_represented" for _, _, kind in entries)

    mapped_text = {(entries[0][0].n, norm_ws("".join(by_path[path].itertext())))
                   for path, entries in claims.items()}
    for path, node in by_path.items():
        text = norm_ws("".join(node.itertext())) if node.tag in TEXT_TAGS and not (
            sample and node.getparent() is root and node.tag == "header") else ""
        if not text or path in used or node.get("kind") == "equation":
            continue
        marker = node.get("page") if sample else node.xpath("string(ancestor::page[1]/@n | ancestor::page[1]/@num)")
        page = document.pages[int(marker) - 1] if marker and marker.isdecimal() and 1 <= int(marker) <= len(document.pages) else None
        if node.tag == "p" and page:
            signature = (page.n, text)
            if signature in mapped_text:
                problem(page, path, "duplicate_paragraph")
                continue
            mapped_text.add(signature)
        problem(page, path, "unmatched_xml_content")
    for page in document.pages:
        if native := page.diag.get("native_numeric_inventory"):
            missing = Counter(native) - compared_numbers[page.n]
            if missing:
                uncertain.append({"page": page.n, "kind": "unmatched_native_pdf_number", "values": dict(missing)})
    return {"status": "review_required" if issues or uncertain else "matched_extraction",
            "scope": "Final XML compared with source-linked IR. Independent PDF coverage requires separate evidence.",
            "source_pages": len(document.pages), "xml_paragraphs": len(root.findall(".//p")),
            "missing_paragraphs": sum(row["kind"] == "missing_paragraph" for row in issues),
            "duplicate_paragraphs": sum(row["kind"] == "duplicate_paragraph" for row in issues),
            "numerical_discrepancies": sum(row["kind"] == "numerical_discrepancy" for row in issues),
            "totals": dict(totals), "issues": issues, "source_review": uncertain,
            "mathematics": mathematics,
            "formatting": {"source": "PDF text spans and positioned blocks", "blocks": styles}}

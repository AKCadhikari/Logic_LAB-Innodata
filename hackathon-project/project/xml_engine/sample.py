"""Serialize the legacy conventions observed in the organizer's example XML."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from lxml import etree

from layout.kinds import ABSTRACT, ACK, REFS
from layout.model import Block, DocIR, Span
from utils.textutil import lang_attr, xml_safe

NUMERIC_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+")
NUMBERED_ITEM = re.compile(r"^\d+\.\s+")
LABELED_SCALAR = re.compile(r"^(?:[+•\-]\s*)?[^:]{4,65}:\s*[+-]?\d+(?:[.,]\d+)*\s*[%\w°]*$")
REFERENCE = re.compile(r"^\s*(?:\[(\d+)\]|(\d+)\.)\s*")
YEAR = re.compile(r"\b(?:18|19|20)\d{2}[a-z]?\b", re.I)


def sample_blocks(blocks: list[Block]):
    """Recover an OCR list when several aligned field/value lines have the same shape."""
    index = 0
    while index < len(blocks):
        first = blocks[index]
        end = index
        if first.bbox:
            while end < len(blocks):
                item = blocks[end]
                if (item.tag != "p" or item.kind != "body" or not item.bbox
                        or abs(item.bbox[0] - first.bbox[0]) > 16 or not LABELED_SCALAR.fullmatch(item.text)):
                    break
                end += 1
        if end - index >= 3:
            yield Block(tag="ul", kind="body", children=blocks[index:end])
            index = end
        else:
            yield first
            index += 1


def sample_xml(doc: DocIR, settings, locations: dict | None, assets: dict | None) -> bytes:
    from xml_engine.serializer import _emit, _inline, direction_attributes, formula_attributes

    root = etree.Element("document", lang=lang_attr(doc.lang, "name"), reading_order_layout_source="corrected",
                         **direction_attributes(doc.language))
    columns = [str(page.n) for page in doc.pages if has_columns(page)]
    if columns:
        root.set("reading_order_multicolumn_pages", ",".join(columns))
    header = etree.SubElement(root, "header")
    title = next((block.text for page in doc.pages for block in page.blocks if block.kind == "title"),
                 doc.meta.get("pdf_metadata", {}).get("title", ""))
    if title:
        etree.SubElement(header, "title").text = xml_safe(title)
    body = etree.SubElement(root, "body")
    sections = []
    counters = Counter()
    nodes = {}
    front = True
    top_number = 0
    last_reference = None
    last_paragraph = None
    name = doc.meta.get("asset_stem") or Path(doc.path).stem or doc.doc_id

    def section(depth, kind, level):
        while sections and sections[-1][0] >= depth:
            sections.pop()
        parent = sections[-1][1] if sections else body
        element = etree.SubElement(parent, "section", level=str(level), kind=kind, id=identifier("S"))
        sections.append((depth, element))
        return element

    def parent():
        if front:
            return header
        return sections[-1][1] if sections else section(1, "body", 2)

    def identifier(prefix):
        counters[prefix] += 1
        return f"{prefix}{counters[prefix]}"

    def geometry(box):
        return {key: f"{value:.2f}" for key, value in zip(("x0", "y0", "x1", "y1"), box)} if box else {}

    def text_attributes(block, page, prefix, kind):
        attributes = {"page": str(page.n), "id": identifier(prefix), **direction_attributes(block.language)}
        if kind:
            attributes["kind"] = kind
        if block.lang and block.lang != doc.lang:
            attributes["lang"] = lang_attr(block.lang, "name")
        if block.bbox:
            attributes.update(geometry(block.bbox))
            x0, _, x1, _ = block.bbox
            if abs(x0 + x1 - page.width) < page.width * 0.025:
                attributes["align"] = "center"
            else:
                attributes["align"] = "right" if block.language.get("base_direction") == "rtl" else "left"
        if block.size > 0:
            attributes["font_size"] = f"{block.size:.1f}"
        measured = [span for span in block.spans if span.font and span.text.strip()]
        if measured and all(span.serif for span in measured):
            attributes["serif"] = "true"
        if len(block.line_boxes) > 1:
            attributes["sub_bboxes"] = "|".join(
                ",".join(f"{value:.2f}" for value in box) for box in block.line_boxes
            )
        return attributes

    def text_node(element, text, spans):
        _inline(element, text, spans, sample=True)

    for page in doc.pages:
        if page.n > 1:
            front = False
        page_language = page.language.get("language")
        attributes = direction_attributes(page.language)
        if page_language not in {None, "und", doc.lang}:
            attributes["lang"] = lang_attr(page_language, "name")
        etree.SubElement(parent(), "page", num=str(page.n), height=f"{page.height:.2f}", width=f"{page.width:.2f}", **attributes)
        for block in sample_blocks(page.blocks):
            text, kind, tag = block.text, block.kind, block.tag
            heading = tag in {"h2", "h3", "h4", "h5", "h6"}
            if kind != "reference_entry" or heading:
                last_reference = None
            numbered = NUMERIC_HEADING.match(text) if heading else None
            abstract = kind == "abstract" and len(text.split()) <= 4 and ABSTRACT.fullmatch(text.strip())
            numbered_item = bool(numbered and NUMBERED_ITEM.match(text)
                                 and top_number and int(numbered[1]) <= top_number)
            if (abstract or heading) and not numbered_item:
                front = False
                if abstract:
                    depth, level, category, tag, kind = 1, 1, "abstract", "h2", "abstract"
                elif REFS.match(text):
                    depth, level, category, kind = 1, 3, "references", "reference_entry"
                elif ACK.match(text):
                    depth, level, category, kind = 1, 3, "acknowledgment", "acknowledgment"
                else:
                    level = int(tag[1])
                    depth = level - 1
                    category = "body"
                    if numbered:
                        number = numbered[1]
                        major = int(number.split(".")[0])
                        if "." not in number and major == top_number + 1:
                            top_number = major
                section(depth, category, level)
            elif front and (page.n > 1 or (tag not in {"header", "footer", "h1"}
                    and kind not in {"title", "publication_metadata", "rights", "byline", "affiliation"})):
                front = False
            target = parent()
            if tag in ("header", "footer"):
                tag = f"page_{tag}" if not front else "p"
            if tag == "figure" or (kind == "equation" and block.bbox):
                formula = kind == "equation"
                figure_kind = "equation" if formula else "figure_caption"
                figure = etree.SubElement(target, "figure_wrap", kind=figure_kind, id=identifier("F"))
                image_id = identifier(f"P{page.n}_{'FORMULA' if formula else 'IMG'}")
                source = f"output_images/{name}/{name}_{image_id}.png"
                attributes = {"id": image_id, "page": str(page.n), "kind": figure_kind, **geometry(block.bbox)}
                if block.bbox:
                    attributes["src"] = source
                if formula:
                    attributes["type"] = "formula"
                    attributes.update(formula_attributes(block))
                    description = block.text if attributes.get("formula_encoding") else "Equation from source PDF"
                    attributes["alt"] = xml_safe(description)
                elif block.text or block.alt:
                    attributes["alt"] = xml_safe(block.text or block.alt)
                etree.SubElement(figure, "img", **attributes)
                if assets is not None and block.bbox:
                    assets[source] = (page.n, tuple(float(value) for value in geometry(block.bbox).values()))
                if block.caption:
                    caption = etree.SubElement(figure, "figcaption", **text_attributes(block, page, "FC", "figure_caption"))
                    text_node(caption, block.caption, [])
                    nodes[(id(block), "caption")] = caption
                nodes[id(block)] = figure
            elif kind == "reference_entry" and not heading:
                spans = block.spans
                if not text and block.children:
                    text = " ".join(f"{child.marker} {child.text}" if child.marker else child.text
                                    for child in block.children)
                previous = last_reference
                continued = (previous and previous[0] == page.n and block.bbox and previous[1].bbox
                             and (block.bbox[0] - previous[1].bbox[0] > max(8, block.size)
                                  or not YEAR.search(text) and block.bbox[1] - previous[3] < max(5, block.size)))
                if continued:
                    element = previous[2]
                    fragment = etree.Element("fragment")
                    text_node(fragment, text, spans)
                    if len(element):
                        element[-1].tail = (element[-1].tail or "") + " " + (fragment.text or "")
                    else:
                        element.text = (element.text or "") + " " + (fragment.text or "")
                    element.extend(list(fragment))
                    box = (min(float(element.get("x0")), block.bbox[0]), float(element.get("y0")),
                           max(float(element.get("x1")), block.bbox[2]), max(float(element.get("y1")), block.bbox[3]))
                    element.attrib.update(geometry(box))
                    sub_boxes = element.get("sub_bboxes") or ",".join(f"{value:.2f}" for value in previous[1].bbox)
                    element.set("sub_bboxes", sub_boxes + "|" + ",".join(f"{value:.2f}" for value in block.bbox))
                    last_reference = (page.n, previous[1], element, box[3])
                else:
                    attrs = text_attributes(block, page, "R", kind)
                    number = REFERENCE.match(text)
                    attrs["num"] = (number[1] or number[2]) if number else str(counters["R"])
                    element = etree.SubElement(target, "reference", **attrs)
                    text_node(element, text, spans)
                    last_reference = (page.n, block, element, block.bbox[3] if block.bbox else 0)
                nodes[id(block)] = element
                for child in block.children:
                    nodes[id(child)] = element
            elif numbered_item:
                listing = etree.SubElement(target, "ol", id=identifier("OL"), kind=kind or "body")
                item = etree.SubElement(listing, "li", **text_attributes(block, page, "LI", kind))
                text_node(item, text, block.spans)
                nodes[id(block)] = item
            elif tag in ("ol", "ul"):
                listing = etree.SubElement(target, tag, id=identifier(tag.upper()), kind=kind or "body")
                for child in block.children:
                    item = etree.SubElement(listing, "li", **text_attributes(child, page, "LI", child.kind))
                    prefix = child.marker + " " if child.marker else ""
                    spans = child.spans
                    if prefix and spans:
                        spans = [Span(text=prefix), *spans]
                    text_node(item, prefix + child.text, spans)
                    nodes[id(child)] = item
                nodes[id(block)] = listing
            elif tag == "table":
                _emit(target, block, {}, doc.lang, settings, nodes)
                nodes[id(block)].attrib.update({"page": str(page.n), "id": identifier("T")})
            else:
                prefixes = {"p": "P", "page_header": "PH", "page_footer": "PF", "reference": "R"}
                attrs = text_attributes(block, page, prefixes.get(tag, "H" if tag[0] == "h" else tag.upper()), kind)
                if kind == "equation":
                    attrs.update(formula_attributes(block))
                if tag in {"h2", "h3", "h4", "h5", "h6"}:
                    attrs["level"] = tag[1]
                element = etree.SubElement(target, tag, **attrs)
                if tag == "p" and kind == "body" and block.bbox and text.strip() and target is not header:
                    if continues_paragraph(last_paragraph, page, block, target):
                        element.set("continues_from", last_paragraph[1].get("id"))
                    last_paragraph = (page, element, block, target)
                if kind == "pseudocode":
                    element.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                    element.text = xml_safe(text)
                else:
                    text_node(element, text, block.spans)
                nodes[id(block)] = element
    if locations is not None:
        tree = root.getroottree()
        locations.update((key, tree.getpath(node)) for key, node in nodes.items())
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def has_columns(page):
    middle = page.width / 2
    boxes = [block.bbox for block in page.blocks if block.kind in {"body", "reference_entry"} and block.bbox]
    left = [box for box in boxes if middle - box[2] >= page.width * .01]
    right = [box for box in boxes if box[0] - middle >= page.width * .01]
    return any(min(a[3], b[3]) - max(a[1], b[1]) >= 0.04 * page.height for a in left for b in right)


def continues_paragraph(previous, page, block, section):
    if previous is None:
        return False
    earlier, _, paragraph, parent = previous
    return (earlier.n + 1 == page.n and section is parent
            and paragraph.bbox[3] >= earlier.height * .76 and block.bbox[1] <= page.height * .22
            and bool(re.search(r"[\w,;–—-]$", paragraph.text.rstrip()))
            and block.text.lstrip()[:1].islower())

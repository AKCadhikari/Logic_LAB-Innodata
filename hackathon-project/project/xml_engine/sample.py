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
CAPS = re.compile(r"\b[A-Z][A-Z0-9-]{1,}\b(?:[ \t]+[A-Z][A-Z0-9-]{1,}\b)*")


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
    from xml_engine.serializer import _emit, _inline, direction_attributes

    root = etree.Element("document", lang=lang_attr(doc.lang, "name"), reading_order_layout_source="corrected",
                         **direction_attributes(doc.language))
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
    name = doc.meta.get("asset_stem") or Path(doc.path).stem or doc.doc_id

    def section(depth, kind, level):
        while sections and sections[-1][0] >= depth:
            sections.pop()
        parent = sections[-1][1] if sections else body
        element = etree.SubElement(parent, "section", level=str(level), kind=kind)
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
        return attributes

    def text_node(element, text, spans):
        _inline(element, text, spans)
        if len(element) or not element.text:
            return
        value = element.text
        matches = list(CAPS.finditer(value))
        if not matches:
            return
        element.text = value[:matches[0].start()]
        for index, match in enumerate(matches):
            cap = etree.SubElement(element, "all-caps")
            cap.text = match[0]
            cap.tail = value[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(value)]

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
            elif front and (page.n > 1 or kind in ("abstract", "keywords")):
                front = False
            target = parent()
            if tag in ("header", "footer"):
                tag = f"page_{tag}" if not front else "p"
            if tag == "figure" or (kind == "equation" and block.bbox):
                formula = kind == "equation"
                figure_kind = "equation" if formula else "figure_caption"
                figure = etree.SubElement(target, "figure_wrap", kind=figure_kind)
                image_id = identifier(f"P{page.n}_{'FORMULA' if formula else 'IMG'}")
                source = f"output_images/{name}/{name}_{image_id}.png"
                attributes = {"id": image_id, "page": str(page.n), "kind": figure_kind, **geometry(block.bbox)}
                if block.bbox:
                    attributes["src"] = source
                if formula:
                    attributes["type"] = "formula"
                if block.text or block.alt:
                    label = f"({block.marker}) " if formula and block.marker else ""
                    description = "Equation from source PDF" if block.source == "pdf_crop" else block.text or block.alt
                    attributes["alt"] = xml_safe(label + description)
                etree.SubElement(figure, "img", **attributes)
                if assets is not None and block.bbox:
                    assets[source] = (page.n, tuple(float(value) for value in geometry(block.bbox).values()))
                if block.caption:
                    caption = etree.SubElement(figure, "figcaption", **text_attributes(block, page, "FC", "figure_caption"))
                    text_node(caption, block.caption, [])
                    nodes[(id(block), "caption")] = caption
                nodes[id(block)] = figure
            elif kind == "reference_entry" and not heading:
                text = text or " ".join(child.text for child in block.children)
                previous = last_reference
                continued = (previous and previous[0] == page.n and block.bbox and previous[1].bbox
                             and (block.bbox[0] - previous[1].bbox[0] > max(8, block.size)
                                  or not YEAR.search(text) and block.bbox[1] - previous[3] < max(5, block.size)))
                if continued:
                    element = previous[2]
                    value = "".join(element.itertext()).rstrip()
                    for child in list(element):
                        element.remove(child)
                    text_node(element, value + " " + text, [])
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
                    text_node(element, text, block.spans)
                    last_reference = (page.n, block, element, block.bbox[3] if block.bbox else 0)
                nodes[id(block)] = element
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
                nodes[id(block)].set("page", str(page.n))
            else:
                prefixes = {"p": "P", "page_header": "PH", "page_footer": "PF", "reference": "R"}
                attrs = text_attributes(block, page, prefixes.get(tag, "H" if tag[0] == "h" else tag.upper()), kind)
                if tag in {"h2", "h3", "h4", "h5", "h6"}:
                    attrs["level"] = tag[1]
                element = etree.SubElement(target, tag, **attrs)
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

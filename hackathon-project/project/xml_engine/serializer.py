"""XML serializer (lxml). Configurable schema profiles; always escapes/filters illegal characters."""
from __future__ import annotations
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from lxml import etree
from layout.model import DocIR, PageIR, Block, Span
from utils.textutil import xml_safe, lang_attr

BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "table", "caption", "thead", "tbody", "tr", "th", "td",
              "figure", "img", "figcaption", "blockquote", "pre", "hr", "header", "footer", "aside"}
INLINE_TAGS = {"strong", "em", "u", "del", "ins", "sup", "sub", "br", "code", "a", "span"}
STRUCT_TAGS = {"document", "page"}
ALLOWED = BLOCK_TAGS | INLINE_TAGS | STRUCT_TAGS

PROFILES = {
    "brief": {"page_attr": "n", "page_dims": False},
    "example": {"page_attr": "num", "page_dims": True},
}


def _style_key(s: Span):
    return (s.bold, s.italic, s.sup, s.sub, s.strike, s.link, s.formula, s.small_caps, s.underline)


def _runs(spans: list[Span]) -> list[Span]:
    out: list[Span] = []
    for s in spans:
        if out and _style_key(out[-1]) == _style_key(s):
            out[-1] = replace(s, text=out[-1].text + s.text)
        else:
            out.append(Span(**{**s.__dict__}))
    return out


CAPS = re.compile(r"(?<!\w)(?:[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+|[A-Z][A-Z0-9]+|[A-Z]\.|"
                  r"[A-Z]*\d[A-Z0-9]*[A-Z][A-Z0-9]*)(?!\w)(?:[ \t]+[A-Z][A-Z0-9]+(?!\w))*")
SUPERSCRIPTS = re.compile(r"[\u00b9\u00b2\u00b3\u2070\u2074-\u2079]+")


def _styled_segments(runs, sample):
    for run in runs:
        if not sample:
            yield run, ""
            continue
        for segment in re.split(f"({SUPERSCRIPTS.pattern})", run.text):
            if SUPERSCRIPTS.fullmatch(segment):
                yield replace(run, text=segment, sup=True), ""
                continue
            if run.small_caps:
                yield replace(run, text=segment), "small-caps"
                continue
            offset = 0
            for match in CAPS.finditer(segment):
                if match.start() > offset:
                    yield replace(run, text=segment[offset:match.start()]), ""
                yield replace(run, text=match[0]), "all-caps"
                offset = match.end()
            if offset < len(segment):
                yield replace(run, text=segment[offset:]), ""


def _inline(el: etree._Element, text: str, spans: list[Span], *, sample: bool = False):
    text = xml_safe(text)
    if not spans or "".join(s.text for s in spans) != text:
        if not sample:
            el.text = text
            return
        spans = [Span(text=text)]
    if sample:
        el.text = ""
    runs = _runs(spans)
    tot = sum(len(r.text.strip()) for r in runs) or 1
    base_bold = sum(len(r.text.strip()) for r in runs if r.bold) / tot > 0.85
    base_ital = sum(len(r.text.strip()) for r in runs if r.italic) / tot > 0.85
    last = None
    for r, caps in _styled_segments(runs, sample):
        t = xml_safe(r.text)
        if not t:
            continue
        wrappers = []
        if r.formula: wrappers.append(("span", {"kind": "equation"}))
        if r.link: wrappers.append(("a", {"href": xml_safe(r.link)}))
        if r.bold and not base_bold and t.strip(): wrappers.append(("strong", {}))
        if r.italic and not base_ital and t.strip(): wrappers.append(("em", {}))
        if r.underline and t.strip(): wrappers.append(("u", {}))
        if r.sup: wrappers.append(("sup", {}))
        if r.sub: wrappers.append(("sub", {}))
        if r.strike: wrappers.append(("del", {}))
        if caps: wrappers.append((caps, {}))
        if not wrappers:
            if last is None: el.text = (el.text or "") + t
            else: last.tail = (last.tail or "") + t
            continue
        parent = el; node = None
        for tag, attrs in wrappers:
            node = etree.SubElement(parent, tag, **attrs); parent = node
        node.text = t
        last = el[-1]


def _attrs(b: Block, cfg, doc_lang: str, settings) -> dict:
    a = {}
    if b.lang and b.lang != doc_lang:
        a["lang"] = lang_attr(b.lang, settings.lang_format)
    a.update(direction_attributes(b.language))
    if b.cls and not b.cls.startswith("__") and b.cls != "caption":
        a["class"] = b.cls
    if settings.emit_kind and b.kind and b.kind not in ("table", "figure", "list", "ul", "ol"):
        a["kind"] = b.kind
    if settings.emit_bbox and b.bbox:
        a["data-bbox"] = ",".join(f"{v:.1f}" for v in b.bbox)
    if b.kind == "equation":
        a.update(formula_attributes(b))
    return a


def formula_attributes(block):
    evidence = block.source_reference.get("formula", {})
    attributes = {"formula_status": evidence.get("status", "review_required")}
    if evidence.get("encoding"):
        attributes["formula_encoding"] = evidence["encoding"]
    if block.marker:
        attributes["equation_number"] = block.marker
    return attributes


def direction_attributes(language: dict) -> dict:
    base = language.get("base_direction")
    return {"dir": base} if base in {"rtl", "ltr"} else {}


def _emit(parent, b: Block, cfg, doc_lang, settings, nodes, image_source=None):
    t = b.tag
    if t in ("ul", "ol"):
        el = etree.SubElement(parent, t, **_attrs(b, cfg, doc_lang, settings))
        for li in b.children:
            e = etree.SubElement(el, "li", **_attrs(li, cfg, doc_lang, settings)); _inline(e, li.text, li.spans)
            if nodes is not None:
                nodes[id(li)] = e
    elif t == "table":
        el = etree.SubElement(parent, "table", **_attrs(b, cfg, doc_lang, settings))
        if b.caption:
            caption = etree.SubElement(el, "caption")
            caption.text = xml_safe(b.caption)
            if nodes is not None:
                nodes[(id(b), "caption")] = caption
        rows: dict[int, list] = {}
        for c in b.cells:
            rows.setdefault(c.row, []).append(c)
        head_rows = sorted(r for r, cs in rows.items() if all(c.header for c in cs))
        thead = etree.SubElement(el, "thead") if head_rows else None
        tbody = etree.SubElement(el, "tbody")
        for r in sorted(rows):
            tr = etree.SubElement(thead if (thead is not None and r in head_rows) else tbody, "tr")
            rtl_grid = b.source == "native" and b.language.get("base_direction") == "rtl"
            for c in sorted(rows[r], key=lambda c: c.col, reverse=rtl_grid):
                at = direction_attributes(c.language)
                if c.language.get("language") not in {None, "und", doc_lang}:
                    at["lang"] = lang_attr(c.language["language"], settings.lang_format)
                if c.rowspan > 1: at["rowspan"] = str(c.rowspan)
                if c.colspan > 1: at["colspan"] = str(c.colspan)
                cell = etree.SubElement(tr, "th" if c.header else "td", **at)
                cell.text = xml_safe(c.text)
                if nodes is not None:
                    nodes[id(c)] = cell
    elif t == "figure":
        el = etree.SubElement(parent, "figure", **_attrs(b, cfg, doc_lang, settings))
        image = etree.SubElement(el, "img", alt=xml_safe(b.alt or "Figure"))
        if image_source:
            image.set("src", image_source)
        if b.caption:
            fc = etree.SubElement(el, "figcaption")
            if settings.emit_kind: fc.set("kind", "figure_caption")
            fc.text = xml_safe(b.caption)
            if nodes is not None:
                nodes[(id(b), "caption")] = fc
    elif t in ("header", "footer"):
        el = etree.SubElement(parent, t, **_attrs(b, cfg, doc_lang, settings)); _inline(el, b.text, b.spans)
    else:
        tag = t if t in BLOCK_TAGS else "p"
        el = etree.SubElement(parent, tag, **_attrs(b, cfg, doc_lang, settings)); _inline(el, b.text, b.spans)
    if nodes is not None:
        nodes[id(b)] = el


def to_xml(doc: DocIR, settings, locations: dict | None = None, assets: dict | None = None) -> bytes:
    if settings.schema_profile == "sample":
        from xml_engine.sample import sample_xml
        return sample_xml(doc, settings, locations, assets)
    cfg = PROFILES.get(settings.schema_profile, PROFILES["brief"])
    root = etree.Element("document", lang=lang_attr(doc.lang, settings.lang_format), **direction_attributes(doc.language))
    nodes = {} if locations is not None else None
    name = doc.meta.get("asset_stem") or Path(doc.path).stem or doc.doc_id
    for p in doc.pages:
        at = {cfg["page_attr"]: str(p.n), **direction_attributes(p.language)}
        if p.language.get("language") not in {None, "und", doc.lang}:
            at["lang"] = lang_attr(p.language["language"], settings.lang_format)
        if cfg["page_dims"]:
            at.update(height=f"{p.height:.2f}", width=f"{p.width:.2f}")
        pe = etree.SubElement(root, "page", **at)
        image_number = 0
        identifiers = Counter()
        for b in p.blocks:
            image_source = None
            if b.tag == "figure" and b.bbox:
                image_number += 1
                image_source = f"output_images/{name}/{name}_P{p.n}_IMG{image_number}.png"
                if assets is not None:
                    assets[image_source] = (p.n, b.bbox)
            before = len(pe)
            _emit(pe, b, cfg, doc.lang, settings, nodes, image_source)
            if len(pe) > before:
                element = pe[-1]
                identifiers[element.tag] += 1
                element.set("id", f"P{p.n}_{element.tag}_{identifiers[element.tag]}")
                if image_source:
                    element.find("img").set("id", f"P{p.n}_IMG{image_number}")
    if locations is not None:
        tree = root.getroottree()
        locations.update((key, tree.getpath(node)) for key, node in nodes.items())
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)

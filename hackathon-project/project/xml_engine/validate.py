"""Post-serialization validation: well-formedness, vocabulary, page numbering, language, heading sanity."""
from __future__ import annotations
import re
from functools import lru_cache
from math import isfinite
from pathlib import Path
from lxml import etree
from utils.textutil import norm_ws, xml_script_digits
from xml_engine.serializer import ALLOWED, PROFILES


DECLARATION = re.compile(rb"""^<\?xml\s+version=(["'])1\.0\1\s+encoding=(["'])UTF-8\2(?:\s+standalone=(["'])(?:yes|no)\3)?\s*\?>""", re.I)


@lru_cache
def profile_schema(profile):
    source = Path(__file__).with_name("schemas") / f"{profile}.xsd"
    return etree.XMLSchema(etree.parse(str(source), parser=etree.XMLParser(resolve_entities=False, no_network=True)))


def _valid_box(values, bounds=None):
    try:
        x0, y0, x1, y1 = map(float, values)
        return (all(map(isfinite, (x0, y0, x1, y1))) and x0 <= x1 and y0 <= y1
                and (bounds is None or (0 <= x0 <= x1 <= bounds[0] and 0 <= y0 <= y1 <= bounds[1])))
    except (TypeError, ValueError):
        return False


def validate_xml(data: bytes, expected_pages: int, settings) -> dict:
    issues: list[str] = []
    profile = settings.schema_profile if settings.schema_profile in {*PROFILES, "sample"} else "brief"
    schema = {"profile": profile, "file": f"{profile}.xsd", "authority": "DocuSync local schema",
              "valid": False, "errors": []}
    cfg = PROFILES.get(settings.schema_profile, PROFILES["brief"])
    try:
        root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError as e:
        return {"well_formed": False, "issues": [f"not well-formed: {e}"], "pages": 0, "xsd": schema}
    declaration_ok = bool(DECLARATION.match(data.removeprefix(b"\xef\xbb\xbf")))
    if not declaration_ok:
        issues.append("An XML 1.0 declaration with UTF-8 encoding is required.")
    if root.getroottree().docinfo.doctype:
        issues.append("Document type declarations are unsupported.")
    else:
        try:
            profile_schema(profile).assertValid(root)
            schema["valid"] = True
        except etree.DocumentInvalid as error:
            schema["errors"] = [{"line": item.line, "path": item.path, "message": item.message}
                                for item in error.error_log]
            issues.append(f"XSD {profile}.xsd rejected the XML structure.")
    if root.tag != "document":
        issues.append("root element is not <document>")
    if not root.get("lang"):
        issues.append("<document> has no lang attribute")
    if any(node.get("dir") not in {None, "ltr", "rtl", "auto"} for node in root.iter()):
        issues.append("A text direction attribute is invalid.")
    sample = settings.schema_profile == "sample"
    if settings.lang_format == "iso" and not sample:
        if any(not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", el.get("lang", ""))
               for el in root.iter() if el.get("lang") is not None):
            issues.append("A language attribute is not a supported ISO-style language code.")
    if sample:
        pages = root.findall(".//page")
        page_attr = "num"
        allowed = ALLOWED | {"title", "body", "section", "page_header", "page_footer", "figure_wrap",
                             "reference", "all-caps", "small-caps"}
        if [child.tag for child in root if isinstance(child.tag, str)] != ["header", "body"]:
            issues.append("The sample format uses a document header followed by a body.")
        if any(len(page) or (page.text or "").strip() for page in pages):
            issues.append("Sample-format page milestones must be empty.")
        valid_pages = {str(number) for number in range(1, expected_pages + 1)}
        if any(node.get("page") not in valid_pages for node in root.iter() if node.get("page") is not None):
            issues.append("A sample-format element refers to a page outside the source PDF.")
        ids = {}
        previous_body = previous_table = None
        for node in root.iter():
            if anchor := node.get("continues_from"):
                earlier = ids.get(anchor)
                targets = earlier is not None and (node.getparent() is earlier.getparent() and node.get("page") in valid_pages
                           and earlier.get("page") in valid_pages and int(node.get("page")) == int(earlier.get("page")) + 1)
                if node.tag == "table":
                    if not targets or earlier is not previous_table:
                        issues.append("A table continuation must refer to the preceding table in the same section on the previous page.")
                elif not targets or node.tag != "p" or node.get("kind") != "body" or earlier is not previous_body:
                    issues.append("A continuation must refer to the preceding body paragraph in the same section on the previous page.")
            if identifier := node.get("id"):
                if identifier in ids:
                    issues.append("Sample-format element IDs must be unique.")
                ids[identifier] = node
            if node.tag == "p" and node.get("kind") == "body":
                previous_body = node
            elif node.tag == "table":
                previous_table = node
        bounds = {}
        for page in pages:
            try:
                bounds[page.get("num")] = (float(page.get("width")), float(page.get("height")))
            except (TypeError, ValueError):
                continue
        for node in root.iter():
            box = [node.get(key) for key in ("x0", "y0", "x1", "y1")]
            limits = bounds.get(node.get("page"))
            if any(value is not None for value in box) and not _valid_box(box, limits):
                issues.append("A sample-format bounding box is incomplete or invalid.")
                break
            if "sub_bboxes" in node.attrib and not all(
                    _valid_box(box.split(","), limits) for box in node.get("sub_bboxes").split("|")):
                issues.append("A sample-format line bounding box is invalid.")
                break
        for node in root.iter("section"):
            parent = node.getparent()
            if (parent.tag == "section" and (node.get("level") or "").isdigit()
                    and (parent.get("level") or "").isdigit() and int(node.get("level")) <= int(parent.get("level"))):
                issues.append("A nested section must have a deeper level than its parent.")
                break
    else:
        pages = root.findall("page")
        page_attr = cfg["page_attr"]
        allowed = ALLOWED
        if any(isinstance(child.tag, str) and child.tag != "page" for child in root):
            issues.append("The document root may only contain page elements.")
        if len(root.findall(".//page")) != len(pages):
            issues.append("Every page must be a direct child of <document>.")
    nums = [p.get(page_attr) for p in pages]
    if nums != [str(i) for i in range(1, expected_pages + 1)]:
        issues.append(f"page numbering {nums[:5]}... does not match 1..{expected_pages}")
    bad = sorted({e.tag for e in root.iter() if isinstance(e.tag, str) and e.tag not in allowed})
    if bad:
        issues.append(f"elements outside the allowed vocabulary: {bad}")
    for cell in root.iter("th", "td"):
        if cell.getparent().tag != "tr":
            issues.append("Table cells must be inside a table row.")
            break
        if any(not re.fullmatch(r"[1-9][0-9]*", cell.get(attr, "1")) for attr in ("rowspan", "colspan")):
            issues.append("Table cell row and column spans must be positive integers.")
            break
    content_pages = {e.get("page") for e in root.iter() if e.get("page")} if sample else set()
    empty = ([number for number in nums if number not in content_pages] if sample else
             [p.get(page_attr) for p in pages if not "".join(p.itertext()).strip() and p.find(".//figure") is None])
    formulas = root.xpath(".//img[@type='formula']|.//p[@kind='equation']")
    return {"well_formed": True, "declaration_utf8": declaration_ok, "xsd": schema,
            "issues": issues, "pages": len(pages), "empty_pages": empty,
            "tables": len(root.findall(".//table")), "figures": len(root.findall(".//figure_wrap" if sample else ".//figure")),
            "formulas": len(formulas), "formula_transcriptions": sum(bool(node.get("formula_encoding")) for node in formulas),
            "formulas_for_review": sum(node.get("formula_status") == "review_required" for node in formulas),
            "headings": sum(len(root.findall(f".//h{i}")) for i in range(1, 7)), "chars": len("".join(root.itertext()))}


def verify_unicode(data: bytes, document, locations: dict) -> dict:
    """Compare logical IR text with the serialized nodes without Unicode normalization."""
    root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    count, issues = 0, []

    def check(page, key, expected, label):
        nonlocal count
        if not expected:
            return
        count += 1
        path = locations.get(key)
        values = root.xpath(path) if path else []
        if values:
            value = values[0]
            rendered = norm_ws("".join(value.itertext()))
            if root.find("body") is not None and next(value.iter("sup", "sub"), None) is not None:
                expected = xml_script_digits(expected)
            # Sample XML can add list labels and use formula image alternate text.
            if norm_ws(expected) in rendered or any(
                    norm_ws(expected) in norm_ws(img.get("alt", "")) for img in value.iter("img")):
                return
        issues.append({"page": page, "node": path, "kind": label})

    def visit(block, page):
        check(page, id(block), block.text, block.kind or block.tag)
        check(page, (id(block), "caption"), block.caption, "caption")
        for cell in block.cells:
            check(page, id(cell), cell.text, "table_cell")
        for child in block.children:
            visit(child, page)

    for page in document.pages:
        for block in page.blocks:
            visit(block, page.n)
    return {"status": "validation_partial" if issues else "text_preserved", "nodes_checked": count,
            "unconfirmed_nodes": issues}

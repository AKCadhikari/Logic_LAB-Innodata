"""Post-serialization validation: well-formedness, vocabulary, page numbering, language, heading sanity."""
from __future__ import annotations
import re
from lxml import etree
from xml_engine.serializer import ALLOWED, PROFILES


def validate_xml(data: bytes, expected_pages: int, settings) -> dict:
    issues: list[str] = []
    cfg = PROFILES.get(settings.schema_profile, PROFILES["brief"])
    try:
        root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError as e:
        return {"well_formed": False, "issues": [f"not well-formed: {e}"], "pages": 0}
    if root.getroottree().docinfo.doctype:
        issues.append("Document type declarations are unsupported.")
    if root.tag != "document":
        issues.append("root element is not <document>")
    if not root.get("lang"):
        issues.append("<document> has no lang attribute")
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
    return {"well_formed": True, "issues": issues, "pages": len(pages), "empty_pages": empty,
            "tables": len(root.findall(".//table")), "figures": len(root.findall(".//figure_wrap" if sample else ".//figure")),
            "headings": sum(len(root.findall(f".//h{i}")) for i in range(1, 7)), "chars": len("".join(root.itertext()))}

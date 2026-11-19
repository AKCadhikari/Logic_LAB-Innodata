"""Post-serialization validation: well-formedness, vocabulary, page numbering, language, heading sanity."""
from __future__ import annotations
from lxml import etree
from xml_engine.serializer import ALLOWED, PROFILES


def validate_xml(data: bytes, expected_pages: int, settings) -> dict:
    issues: list[str] = []
    cfg = PROFILES.get(settings.schema_profile, PROFILES["brief"])
    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError as e:
        return {"well_formed": False, "issues": [f"not well-formed: {e}"], "pages": 0}
    if root.tag != "document":
        issues.append("root element is not <document>")
    if not root.get("lang"):
        issues.append("<document> has no lang attribute")
    pages = root.findall("page")
    nums = [p.get(cfg["page_attr"]) for p in pages]
    if nums != [str(i) for i in range(1, expected_pages + 1)]:
        issues.append(f"page numbering {nums[:5]}... does not match 1..{expected_pages}")
    bad = sorted({e.tag for e in root.iter() if isinstance(e.tag, str) and e.tag not in ALLOWED})
    if bad:
        issues.append(f"elements outside the allowed vocabulary: {bad}")
    empty = [p.get(cfg["page_attr"]) for p in pages if not "".join(p.itertext()).strip() and p.find(".//figure") is None]
    return {"well_formed": True, "issues": issues, "pages": len(pages), "empty_pages": empty,
            "tables": len(root.findall(".//table")), "figures": len(root.findall(".//figure")),
            "headings": sum(len(root.findall(f".//h{i}")) for i in range(1, 7)), "chars": len("".join(root.itertext()))}

from lxml import etree
import pytest

from config.settings import Settings
from layout.model import Block, DocIR, Line, PageIR
from layout.reconstruct import DocContext, build_page
from xml_engine.completeness import compare_content
from xml_engine.serializer import to_xml
from xml_engine.validate import validate_xml


@pytest.mark.parametrize("profile", ["brief", "example", "sample"])
def test_local_schema_rejects_nested_paragraph_and_unknown_attributes(profile):
    settings = Settings(schema_profile=profile)
    doc = DocIR("d", "d.pdf", "en", [PageIR(1, 400, 600, [Block("p", "A paragraph.", kind="body")])])
    data = to_xml(doc, settings)
    assert validate_xml(data, 1, settings)["xsd"]["valid"]
    root = etree.fromstring(data)
    assert not validate_xml(etree.tostring(root), 1, settings)["declaration_utf8"]
    paragraph = root.find(".//p")
    etree.SubElement(paragraph, "p").text = "Invalid nested block."
    paragraph.set("unexpected_attribute", "invalid")
    result = validate_xml(etree.tostring(root, encoding="UTF-8", xml_declaration=True), 1, settings)
    assert result["well_formed"] and not result["xsd"]["valid"] and len(result["xsd"]["errors"]) >= 2


def test_completeness_reports_changed_unicode_numbers_and_duplicates():
    settings = Settings(schema_profile="sample")
    block = Block("p", "Change −5.25% × 10 µg", kind="body")
    doc = DocIR("d", "d.pdf", "en", [PageIR(1, 400, 600, [block])])
    locations = {}
    data = to_xml(doc, settings, locations)
    assert compare_content(data, doc, locations, sample=True)["status"] == "matched_extraction"
    root = etree.fromstring(data)
    paragraph = root.find(".//p")
    paragraph.text = "Change -5.52% x 10 µg"
    report = compare_content(etree.tostring(root), doc, locations, sample=True)
    assert report["status"] == "review_required"
    discrepancy = next(item for item in report["issues"] if item["kind"] == "numerical_discrepancy")
    assert discrepancy["page"] == 1 and discrepancy["missing"] == {"−5.25%": 1}
    assert discrepancy["added"] == {"-5.52%": 1}
    etree.SubElement(paragraph.getparent(), "p", page="1").text = paragraph.text
    assert compare_content(etree.tostring(root), doc, locations, sample=True)["duplicate_paragraphs"] == 1


def test_layout_preserves_single_digit_surrounded_by_symbols():
    settings = Settings(schema_profile="sample")
    page = build_page(1, 400, 600, [Line("• . 1", (70, 200, 110, 210))],
                      [], [], DocContext(), settings, "native")
    xml = to_xml(DocIR("d", "d.pdf", "en", [page]), settings)
    assert "1" in "".join(etree.fromstring(xml).itertext())

from lxml import etree
from pptx import Presentation
from pptx.util import Inches

from layout.model import Block, DocIR, PageIR
from quality.conformance import compare_nodes, compare_pptx, tree
from xml_engine.serializer import to_xml
from xml_engine.validate import validate_xml


def test_sample_profile_emits_milestones_sections_and_local_assets(settings):
    settings.schema_profile = "sample"
    formula = Block("p", "x + y = z (1.1)", bbox=(80, 70, 105, 80), kind="equation")
    reference = Block("p", "Author, A. 2026. A verified result.", bbox=(24, 78, 310, 85), kind="reference_entry")
    continuation = Block("p", "Local journal.", bbox=(42, 88, 150, 95), kind="reference_entry")
    doc = DocIR("article", "article.pdf", "en", [
        PageIR(1, 500, 600, [
            Block("h1", "Article title", kind="title"),
            Block("p", "ABSTRACT", kind="abstract"),
            Block("h2", "1 Methods", kind="section_heading"),
            formula,
        ]),
        PageIR(2, 500, 600, [Block("h2", "References", kind="reference_entry"), reference, continuation]),
    ])
    locations, assets = {}, {}
    data = to_xml(doc, settings, locations, assets)
    root = etree.fromstring(data)
    assert root.get("lang") == "english"
    assert [el.get("num") for el in root.findall(".//page")] == ["1", "2"]
    assert root.findtext("header/title") == "Article title"
    assert [el.get("kind") for el in root.find("body")] == ["abstract", "body", "references"]
    assert assets == {"output_images/article/article_P1_FORMULA1.png": (1, formula.bbox)}
    assert root.xpath(locations[id(formula)])[0].tag == "figure_wrap"
    assert locations[id(reference)] == locations[id(continuation)]
    assert "".join(root.xpath(locations[id(reference)])[0].itertext()) == reference.text + " " + continuation.text
    assert validate_xml(data, 2, settings)["issues"] == []


def test_xml_comparison_reports_structure_in_addition_to_text():
    expected = tree(b'<doc xmlns:p="urn:old"><p:block id="r1">Same text</p:block><extra/></doc>')
    actual = tree(b'<doc xmlns:p="urn:new"><p:block id="r2">Same text</p:block></doc>')
    result = compare_nodes(expected, actual)
    assert not result["match"]
    assert result["diff_counts"]["namespaces"] == 1
    assert result["diff_counts"]["missing_element"] == 2
    assert result["diff_counts"]["additional_element"] == 1


def test_comparison_preserves_inline_text_spacing():
    expected = tree(b"<doc><p><b>First</b> Second</p></doc>")
    actual = tree(b"<doc>\n  <p><b>First</b>Second</p>\n</doc>")
    assert compare_nodes(expected, actual)["diff_counts"] == {"tail": 1}


def test_deck_comparison_detects_movement_with_unchanged_text(tmp_path):
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
    shape.text = "Same across presentations"
    expected, actual = tmp_path / "expected.pptx", tmp_path / "actual.pptx"
    deck.save(expected)
    shape.left = Inches(2)
    deck.save(actual)
    report = compare_pptx(expected, actual)
    assert report["slide_order_match"] and not report["match"]
    assert report["changed_slides"] == [1]
    assert report["slides"][0]["diff_counts"] == {"attributes": 1}

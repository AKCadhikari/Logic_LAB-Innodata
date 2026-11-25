from lxml import etree
from pptx import Presentation
from pptx.oxml.ns import qn
from pptx.util import Inches

from comparison.pairing import match_presentations
from config.settings import Settings
from diagnostics.profile import decorate_document, quality_gate
from layout.model import Block, Cell, DocIR, PageIR
from powerpoint.language import plain_text, shape_paragraph
from reporting.exports import publish_document, write_report
from utils.cache import file_key
from utils.textutil import language_profile
from xml_engine.serializer import to_xml
from xml_engine.validate import validate_xml


def test_multiscript_ir_and_xml_keep_logical_text_and_rtl_cell_order():
    arabic = "هذه قراءة في التقرير (Valve 9) بقيمة 13.4."
    hebrew = "המסמך כולל תוצאות מהמעבדה"
    cells = [Cell("19.3", 0, 0), Cell("القياس", 0, 1)]
    table = Block("table", cells=cells)
    doc = DocIR("fresh", "fixture.pdf", pages=[PageIR(1, 500, 600, [
        Block("p", arabic), Block("p", hebrew), table
    ])])
    profile = decorate_document(doc)
    assert profile["language"]["language"] == "mul"
    assert profile["language"]["languages"] == ["ar", "he"]
    assert language_profile(arabic)["direction"] == "mixed"
    assert [block.reading_order for block in doc.pages[0].blocks] == [1, 2, 3]
    assert doc.pages[0].blocks[0].original_text == arabic
    assert doc.pages[0].blocks[0].source_reference["page"] == 1
    settings = Settings(schema_profile="brief")
    data = to_xml(doc, settings)
    assert validate_xml(data, 1, settings)["issues"] == []
    root = etree.fromstring(data)
    assert root.find(".//p").text == arabic
    assert root.find(".//p").get("dir") == "rtl"
    assert [node.text for node in root.findall(".//td")] == ["القياس", "19.3"]


def test_native_bidi_risk_blocks_deck_and_keeps_draft_xml(tmp_path):
    doc = DocIR("risk", str(tmp_path / "fixture.pdf"), pages=[
        PageIR(1, 500, 600, [Block("p", "هذه قيمة في الملف (Unit 6).")], diag={"native_decoding_risk": True})
    ])
    decorate_document(doc)
    gate = quality_gate(doc)
    assert gate["blocking_pages"] == [1]

    def unexpected_transform(_):
        raise AssertionError("A blocked source must not update the slide deck")

    result = publish_document(tmp_path / "export", "risk", doc, Settings(schema_profile="brief"),
                              {"ok": True, "multilingual_validation": gate}, {"document_id": doc.doc_id}, unexpected_transform)
    assert result["status"] == "manual_review_required"
    assert result["outputs"]["xml"]["draft"]
    assert result["outputs"]["pptx"] is None


def test_presentation_direction_does_not_modify_logical_unicode():
    draft = Presentation()
    slide = draft.slides.add_slide(draft.slide_layouts[6])
    frame = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(2)).text_frame
    arabic = "هذه تجربة في المختبر (ID 16) مع تصحيحٍ."
    rtl, ltr = frame.paragraphs[0], frame.add_paragraph()
    rtl.text, ltr.text = arabic, "Use pump 42 for the next experiment."
    shape_paragraph(rtl._p)
    shape_paragraph(ltr._p)
    assert plain_text(rtl._p) == arabic
    assert rtl._p.find(qn("a:pPr")).get("rtl") == "1"
    assert ltr._p.find(qn("a:pPr")) is None


def test_batch_matches_by_content_and_rejects_stale_source(tmp_path):
    work = tmp_path / "work"
    (work / "ir").mkdir(parents=True)
    text = "A narrow bronze channel carries cold water from the mountain spring into a collection tank near the orchard."
    matching, decoy = tmp_path / "alpha.pdf", tmp_path / "orchard.pdf"
    deck_path = tmp_path / "orchard.pptx"
    matching.write_bytes(b"fixture a")
    decoy.write_bytes(b"fixture b")
    for pdf, content in ((matching, text), (decoy, "This document covers a conference schedule.")):
        ir = DocIR(pdf.stem, str(pdf), pages=[PageIR(1, 500, 600, [Block("p", content)])],
                   meta={"ocr_metadata_version": 2, "source_sha256": file_key(pdf)})
        write_report(work / "ir" / f"{pdf.stem}.json", ir.to_dict())
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(2)).text_frame.text = text
    deck.save(deck_path)
    assert match_presentations([matching, decoy], [deck_path], work) == [(matching, deck_path)]
    matching.write_bytes(b"fixture c")
    assert match_presentations([matching, decoy], [deck_path], work) == []

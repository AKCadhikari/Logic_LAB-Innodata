from lxml import etree
import pymupdf

from layout.equations import position_numbered_equations, recognise_formulas
from layout.model import Block, DocIR, Line, PageIR
from ocr import glm
from ocr.recognition import Recognition
from xml_engine.serializer import to_xml
from xml_engine.validate import validate_xml


def test_numbered_displays_anchor_unnumbered_ocr_formulas_between_text_blocks():
    first, second = Block("p", r"\vec D=4", kind="equation"), Block("p", r"\vec X=5", kind="equation")
    page = PageIR(1, 500, 700, [
        Block("p", "Earlier text", bbox=(50, 70, 250, 95)), first, second,
        Block("p", "Following text", bbox=(50, 190, 250, 220)),
    ], source="ocr")
    lines = [Line("B=4 (2.1)", (150, 110, 435, 130), conf=35), Line("K=5 (2.2)", (150, 140, 435, 160))]
    assert position_numbered_equations(page, lines, ["geometry"]) == 2
    assert (first.marker, second.marker) == ("2.1", "2.2")
    assert first.bbox == lines[0].bbox and second.bbox == lines[1].bbox


def test_xml_keeps_formula_crops_and_only_marks_agreed_transcriptions(monkeypatch, settings):
    agree = r"\vec{D}=|\vec{C}\cdot\vec{X}^{*}(t)-\vec{X}(t)|"
    responses = iter([agree, agree.replace("=", " = "), r"\vec{X}(t+1)=D", r"\vec{X}(t+1)=B"])
    monkeypatch.setattr(glm, "model_info", lambda _settings: {"ready": True})
    monkeypatch.setattr(glm, "recognise_image", lambda image, page, *_args, **_kwargs: Recognition(
        page, "glm-ocr", blocks=[Block("p", next(responses), kind="equation")]))
    settings.schema_profile = "sample"
    settings.ocr_backend = "glm-ocr"
    page = PageIR(1, 500, 700, [
        Block("p", "B=4 (2.1)", bbox=(120, 90, 430, 105), source="ocr", kind="equation"),
        Block("p", "K=5 (2.2)", bbox=(120, 120, 430, 135), source="ocr", kind="equation"),
    ], source="ocr")
    with pymupdf.open() as document:
        source = document.new_page(width=500, height=700)
        recognise_formulas(source, page, settings)
    assets = {}
    xml = to_xml(DocIR("paper", "paper.pdf", "en", [page]), settings, assets=assets)
    one, two = etree.fromstring(xml).xpath("//img[@type='formula']")
    assert one.get("formula_encoding") == "latex" and one.get("alt") == agree
    assert two.get("formula_status") == "review_required" and two.get("alt") == "Equation from source PDF"
    assert one.get("equation_number") == "2.1" and len(assets) == 2
    validation = validate_xml(xml, 1, settings)
    assert validation["formulas"] == 2 and validation["formula_transcriptions"] == 1
    assert validation["formulas_for_review"] == 1 and not validation["issues"]

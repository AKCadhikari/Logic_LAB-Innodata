import json
from io import BytesIO
from zipfile import ZipFile

from lxml import etree
from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt
import pymupdf

from layout.formula_validation import polynomial_equivalence
from layout.math_structure import equation_catalog, mathml, native_latex, signature
from layout.model import Block, DocIR, PageIR, Span
from powerpoint.amend import repair_presentation
from powerpoint.formulas import latex_tokens, office_tokens
from powerpoint.package import MC, parse
from reporting.exports import materialize_assets
from tests.test_grounded_pptx import append_math
from xml_engine.serializer import to_xml
from xml_engine.validate import validate_xml


def make_case(directory):
    path = directory / "evidence.pdf"
    facts = ["Collect the first water sample.", "Calibrate the pressure sensor.",
             "Record the tank temperature.", "Seal the sample in its container."]
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((100, 100), "q=", fontsize=16)
        page.insert_text((125, 91), "7", fontsize=11)
        page.draw_line((123, 95), (135, 95), width=.7)
        page.insert_text((126, 106), "y", fontsize=11)
        page.insert_text((141, 100), "+4", fontsize=16)
        page.insert_text((100, 200), "x=12*u+5", fontsize=16)
        for index, fact in enumerate(facts):
            page.insert_text((100, 260 + index*25), "- " + fact)
        document.save(path)

    blocks = [Block("p", r"q=\frac{7}{y}+4", kind="equation", marker="2", bbox=(97, 77, 176, 112),
                    source_reference={"formula": {"status": "source_reviewed", "encoding": "latex"}}),
              Block("p", "x=12*u+5", kind="equation", marker="3", bbox=(97, 182, 213, 206),
                    source_reference={"formula": {"status": "native_text", "encoding": "unicode"}}),
              Block("ul", children=[Block("li", fact, bbox=(100, 247 + index*25, 300, 267 + index*25))
                                    for index, fact in enumerate(facts)])]
    ir = DocIR("evidence", str(path), pages=[PageIR(1, 595, 842, blocks, diag={"health": "healthy"})])
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    formula = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1))
    paragraph = formula.text_frame.paragraphs[0]
    append_math(paragraph._p, "q=7+y+4")
    paragraph.add_run().text = " (2)"
    textbox = slide.shapes.add_textbox(Inches(1), Inches(3), Inches(7), Inches(3))
    for index, fact in enumerate([facts[0], *facts[2:]]):
        bullet = textbox.text_frame.add_paragraph() if index else textbox.text_frame.paragraphs[0]
        etree.SubElement(bullet._p.get_or_add_pPr(), qn("a:buChar"), char="•")
        run = bullet.add_run()
        run.text, run.font.size, run.font.bold = fact, Pt(14), True
    picture = Image.new("RGB", (400, 120), "white")
    ImageDraw.Draw(picture).text((25, 35), "x=99*u+5", fill="black", font_size=44)
    buffer = BytesIO()
    picture.save(buffer, "PNG")
    frame = slide.shapes.add_picture(buffer, Inches(2), Inches(5.8), Inches(3), Inches(.9))
    frame.name = "Equation (3)"
    original = directory / "submitted.pptx"
    deck.save(original)
    return ir, facts, original


def test_structural_equation_missing_bullet_and_image_replacement(tmp_path, settings, monkeypatch):
    from powerpoint import equation_images
    from quality.slide_preview import renderer

    ir, facts, original = make_case(tmp_path)
    monkeypatch.setattr(equation_images, "transcribe", lambda *_args: "x=99*u+5")
    settings.amend_render_validation = bool(renderer())
    target = tmp_path / "repaired.pptx"
    result = repair_presentation(ir, original, target, settings)
    assert {item["change_type"] for item in result["applied"]} == {"equation", "missing_bullet", "equation_image"}, result["review"]
    deck = Presentation(target)
    shapes = deck.slides[0].shapes
    assert [paragraph.text for paragraph in shapes[1].text_frame.paragraphs] == facts
    assert shapes[1].text_frame.paragraphs[1].runs[0].font.bold
    picture, = [row for row in result["applied"] if row["change_type"] == "equation_image"]
    original_frame = (Inches(2), Inches(5.8), Inches(3), Inches(.9))
    with ZipFile(target) as package:
        root = parse(package.read("ppt/slides/slide1.xml"))
        assert office_tokens(next(root.iter(qn("m:oMath")))).tokens == latex_tokens(r"q=\frac{7}{y}+4")
        if picture.get("native_equation_generated"):
            alt = next(root.iter(f"{{{MC}}}AlternateContent"))
            choice, fallback = list(alt)
            assert choice.nsmap[choice.get("Requires")].endswith("/drawing/2010/main")
            assert office_tokens(next(choice.iter(qn("m:oMath")))).tokens == list("x=12*u+5")
            assert next(fallback.iter(qn("a:blip"))).get(qn("r:embed"))
            for branch in (choice, fallback):
                transform = next(branch.iter(qn("a:xfrm")))
                offset, extents = transform.find(qn("a:off")), transform.find(qn("a:ext"))
                assert tuple(int(value) for value in [offset.get("x"), offset.get("y"),
                                                      extents.get("cx"), extents.get("cy")]) == original_frame
        else:
            assert (shapes[2].left, shapes[2].top, shapes[2].width, shapes[2].height) == original_frame
    report = json.loads((tmp_path / "repaired_reports" / "pptx_validation.json").read_bytes())
    assert report["structure"]["preserved"] and report["summary"]["editable_equations_rebuilt"] == 1
    assert report["summary"]["equation_image_fallbacks"] + report["summary"]["editable_equations_from_pictures"] == 1
    if settings.amend_render_validation:
        assert report["render"]["slides_rendered"] == 1
        assert report["visible_verified"] >= 2

    settings.schema_profile = "sample"
    assets, locations = {}, {}
    xml = to_xml(ir, settings, assets=assets, locations=locations)
    xml = materialize_assets(xml, assets, ir.path, tmp_path)
    assert not validate_xml(xml, 1, settings)["issues"]
    rows = equation_catalog(ir, xml, locations)["equations"]
    assert len(rows) == 2 and all(item["xml_id"] and (tmp_path / item["image"]).is_file() for item in rows)
    assert rows[0]["latex"] == r"q=\frac{7}{y}+4" and "mfrac" in rows[0]["mathml"]


def test_mathematical_structure_does_not_equate_rearranged_symbols():
    assert polynomial_equivalence("y=x+2", "y=2+x") is True
    assert polynomial_equivalence("y=x+2", "y=2-x") is False
    assert polynomial_equivalence("y=x**100", "y=x") is None
    assert signature(mathml("y=x+2")) != signature(mathml("y=2+x"))
    assert signature(mathml(r"x^{2}")) != signature(mathml(r"x_{2}"))
    assert "msup" in etree.tostring(mathml(native_latex("x²=α₃")), encoding="unicode")
    assert native_latex("b2=9", [Span("b"), Span("2", sub=True), Span("=9")]) == "b_{2}=9"


def test_conflicting_ocr_keeps_equation_image_unresolved(monkeypatch, settings):
    from layout import equations, formula_ocr
    from ocr import glm
    from ocr.recognition import Recognition

    monkeypatch.setattr(glm, "model_info", lambda _settings: {"ready": True})
    monkeypatch.setattr(glm, "recognise_image", lambda _data, page, *_args, **_kwargs: Recognition(
        page, "glm-ocr", blocks=[Block("p", r"x=\frac{2}{3}", kind="equation")]))
    monkeypatch.setattr(formula_ocr, "recognise", lambda *_args: r"x=\frac{7}{3}")
    settings.ocr_enabled, settings.ocr_backend, settings.formula_pix2tex_checkpoint = True, "automatic", "local-weights"
    block = Block("p", "x=2/3", kind="equation", source="ocr", bbox=(200, 100, 400, 130))
    with pymupdf.open() as doc:
        source = doc.new_page()
        page = PageIR(1, 595, 842, [block], source="ocr")
        equations.recognise_formulas(source, page, settings)
    assert block.source_reference["formula"]["status"] == "review_required"
    assert "disagreed" in block.source_reference["formula"]["reason"]


def test_picture_repair_leaves_shared_media_unchanged(tmp_path, settings, monkeypatch):
    from powerpoint import equation_images

    ir, _, original = make_case(tmp_path)
    deck = Presentation(original)
    slide = deck.slides[0]
    submitted_picture = slide.shapes[2].image.blob
    slide.shapes.add_picture(BytesIO(submitted_picture), Inches(6), Inches(5.8), Inches(3), Inches(.9))
    deck.save(original)
    monkeypatch.setattr(equation_images, "transcribe", lambda *_args: "x=99*u+5")
    settings.amend_render_validation = False
    target = tmp_path / "shared.pptx"
    result = repair_presentation(ir, original, target, settings)
    image_issue, = [row for row in result["review"] if row["change_type"] == "equation_image"]
    assert "shared" in image_issue["reason"]
    assert [shape.image.blob for shape in list(Presentation(target).slides[0].shapes)[2:]] == [submitted_picture] * 2


def test_failed_native_picture_render_retries_the_verified_raster(tmp_path, settings, monkeypatch):
    from powerpoint import equation_images, validation
    from quality import slide_preview

    ir, _, original = make_case(tmp_path)
    monkeypatch.setattr(equation_images, "transcribe", lambda *_args: "x=99*u+5")
    monkeypatch.setattr(slide_preview, "renderer", lambda: "controlled render validation")
    settings.amend_render_validation = True
    attempts = []

    def verify_layout(_source, _target, records, media):
        image, = [row for row in records if row["change_type"] == "equation_image"]
        assert media
        native = image["native_equation_generated"]
        attempts.append(native)
        image["render_verification"] = "equation_image_mismatch" if native else "confirmed"
        return {"status": "completed", "visible_mismatches": [image["error_id"]] if native else []}

    monkeypatch.setattr(validation, "rendered_check", verify_layout)
    target = tmp_path / "fallback.pptx"
    result = repair_presentation(ir, original, target, settings)
    assert attempts == [True, False]
    assert result["presentation_validation"]["iterations"] == 2
    assert result["presentation_validation"]["structure"]["preserved"]
    after = Presentation(target).slides[0].shapes[2]
    assert after.image.blob != Presentation(original).slides[0].shapes[2].image.blob

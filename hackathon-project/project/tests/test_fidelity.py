from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile

from config.settings import Settings
from lxml import etree
import pymupdf
import pytest
from pptx import Presentation
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches

from layout.model import Block, DocIR, PageIR
from powerpoint.package import prepare_picture, write_deck
from powerpoint.sync import synchronize
from quality.ground_truth import compare_ground_truth, preservation
from reporting.exports import publish_document
from xml_engine.assets import inspect_assets, validate_archive


def create_source(path):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 80), "The verified document contains deterministic evidence.")
        page.insert_text((60, 130), "x + y = z (1.1)")
        doc.save(path)


def create_deck(path: Path):
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    body = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(7), Inches(1))
    body.text = "Leave this existing shape and its formatting intact."
    deck.save(path)


def test_export_validates_assets_and_preformatted_pseudocode_after_extracting_zip(tmp_path, settings):
    pdf, pptx = tmp_path / "paper.pdf", tmp_path / "source.pptx"
    create_source(pdf)
    create_deck(pptx)
    settings.schema_profile = "sample"
    code = "while i < N do\n  i ← i + 1\nend while"
    doc = DocIR("paper", str(pdf), "en", [PageIR(1, 595, 842, [
        Block("p", "The verified document contains deterministic evidence."),
        Block("p", "x + y = z (1.1)", bbox=(58, 115, 160, 137), kind="equation"),
        Block("p", code, kind="pseudocode"),
    ])])
    folder = tmp_path / "output"
    report = publish_document(folder, "paper", doc, settings,
                              {"ok": True, "source_review_approved": True}, {"document_id": "paper"},
                              lambda out: synchronize(pptx, out, []))
    assert report["status"] == "completed"
    archive = folder / "paper.zip"
    verified = validate_archive(archive, "paper.xml", "paper_updated.pptx")
    assert verified["assets"]["resolved_references"] == verified["assets"]["total_references"] == 1
    assert verified["valid"]
    with ZipFile(archive) as bundle:
        xml = etree.fromstring(bundle.read("paper.xml"))
        assert xml.findtext(".//p[@kind='pseudocode']") == code
        assert xml.find(".//p[@kind='pseudocode']").get("{http://www.w3.org/XML/1998/namespace}space") == "preserve"
        image = xml.find(".//img[@type='formula']").get("src")
        assert bundle.read(image) == (folder / image).read_bytes()
        assert {"correction_report.json", "validation_report.json", "ai_execution_report.json"} <= set(bundle.namelist())
    truth = compare_ground_truth(pdf, pptx, folder / "paper.xml", pptx, folder / "paper.xml",
                                 folder / "paper_updated.pptx", archive=archive)
    assert truth["text_accuracy"]["exact_native_matches"] == 1
    assert truth["equations"][0]["status"] == "CORRECT"
    assert truth["original_deck_preservation"]["slides"][0]["shapes"][0]["status"] == "CORRECT"


def test_ocr_source_review_keeps_xml_draft_until_approved(tmp_path):
    source_deck = tmp_path / "source.pptx"
    create_deck(source_deck)
    doc = DocIR("ocr-review", str(tmp_path / "source.pdf"), pages=[
        PageIR(1, 595, 842, [Block("p", "The audited report records exactly 240 units.", source="ocr")],
               source="ocr", requires_review=True)
    ])

    def unexpected_transform(_):
        pytest.fail("A deck must not be generated from an unreviewed OCR source.")

    blocked = publish_document(tmp_path / "unreviewed", "ocr-review", doc, Settings(schema_profile="brief"),
                               {"ok": True}, {"document_id": doc.doc_id}, unexpected_transform)
    assert blocked["status"] == "manual_review_required"
    assert blocked["outputs"]["xml"]["draft"] and blocked["outputs"]["pptx"] is None
    assert blocked["validation"]["pptx"]["status"] == "blocked_by_source_review"

    approved = publish_document(
        tmp_path / "approved", "ocr-review", doc, Settings(schema_profile="brief"),
        {"ok": True, "source_review_approved": True}, {"document_id": doc.doc_id},
        lambda output: synchronize(source_deck, output, []),
    )
    assert approved["status"] == "completed" and approved["outputs"]["pptx"]["valid"]
    assert not approved["outputs"]["xml"].get("draft")
    assert approved["content_validation"]["source_review_approved"]


def test_asset_validator_reports_missing_duplicate_and_unsafe_paths(tmp_path):
    xml = (b'<document><img src="assets/missing.png" kind="figure"/>'
           b'<img src="../missing.png" type="formula"/><img src="assets/missing.png"/></document>')
    status = inspect_assets(xml, tmp_path)
    assert status["total_references"] == 3 and not status["valid"] and status["resolved_references"] == 0
    assert len(status["missing_figure_assets"]) == 2
    assert len(status["missing_equation_assets"]) == len(status["invalid_paths"]) == 1
    assert status["duplicate_references"] == {"assets/missing.png": 2}
    archive = tmp_path / "traversal.zip"
    with ZipFile(archive, "w") as bundle:
        bundle.writestr("../paper.xml", xml)
    with pytest.raises(ValueError, match="unsafe paths"):
        validate_archive(archive, "paper.xml")


def test_slide_field_cache_repair_preserves_field_and_unrelated_literal(tmp_path):
    source, final = tmp_path / "source.pptx", tmp_path / "final.pptx"
    deck = Presentation()
    deck._element.set("firstSlideNum", "7")
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    content = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(7), Inches(1))
    content.text = "A literal ‹#› inside other content."
    number = slide.shapes.add_textbox(Inches(9), Inches(5), Inches(1), Inches(1))
    number.name = "slide-number"
    field = OxmlElement("a:fld")
    field.set("id", "{00000000-0000-0000-0000-000000000007}")
    field.set("type", "slidenum")
    child = OxmlElement("a:t")
    child.text = "‹#›"
    field.append(child)
    number.text_frame.paragraphs[0]._p.append(field)
    deck.save(source)
    report = synchronize(source, final, [])
    exported = Presentation(final)
    assert exported.slides[0].shapes[0].text == content.text
    cached = next(exported.slides[0].shapes[1]._element.iter(qn("a:fld")))
    assert cached.get("type") == "slidenum" and cached.find(qn("a:t")).text == "7"
    assert report["slide_numbering"][0]["status"] == "CORRECT"
    assert report["applied"][0]["change_type"] == "slide_number"


def test_verified_picture_preserves_original_shapes_master_and_layout(tmp_path):
    source, output, pdf = (tmp_path / name for name in ("source.pptx", "output.pptx", "source.pdf"))
    create_deck(source)
    create_source(pdf)
    deck = Presentation(source)
    slide = deck.slides[0]
    with pymupdf.open(pdf) as doc:
        image = doc[0].get_pixmap(clip=pymupdf.Rect(58, 115, 160, 137), alpha=False)
    picture, patch = prepare_picture(slide, image.tobytes("png"), [1, 2, 5, 2],
                                    (image.width, image.height), "Verified formula")
    write_deck(source, output, defaultdict(list), 1, {str(slide.part.partname).lstrip("/"): [patch]})
    assert Presentation(output).slides[0].shapes[1].image.blob == image.tobytes("png")
    report = preservation(source, output)
    assert report["changed_original_master_layout_notes_theme_or_media_parts"] == []
    assert report["slides"][0]["shapes"][-1]["status"] == "EXTRA"
    checked = preservation(source, output, [{"slide": 1, "shape_id": picture.shape_id}])
    assert checked["slides"][0]["shapes"][-1]["status"] == "CORRECT"

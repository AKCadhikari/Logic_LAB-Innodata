from __future__ import annotations

from base64 import b64decode

import pymupdf
import pytest
from lxml import etree
from pydantic import ValidationError

import agents.orchestrator as orchestrator
from agents.orchestrator import process_pdf
from layout.docling_adapter import apply_regions
from layout.model import Block, DocIR, PageIR, ir_from_dict
from ocr import glm, paddle
from ocr.recognition import Recognition, RecognitionError
from ocr.router import OCRRouter


def fixture_pdf(path, *, scanned):
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((60, 70), "Verification of a scanned document and its scientific details.", fontsize=16)
        if scanned:
            with pymupdf.open() as output:
                output.new_page().insert_image(page.rect, pixmap=page.get_pixmap(dpi=150))
                output.save(path)
        else:
            pdf.save(path)


def test_native_pages_bypass_both_engines(tmp_path, settings, audit, monkeypatch):
    settings.docling_enabled = False
    def fail(*args):
        pytest.fail("Native text must not invoke OCR.")
    monkeypatch.setattr(glm, "model_info", fail)
    monkeypatch.setattr(paddle.PaddleOCR, "recognise", fail)
    source = tmp_path / "native.pdf"
    fixture_pdf(source, scanned=False)
    report = process_pdf(source, settings, tmp_path / "out", tmp_path / "run", audit)
    assert report["ok"] and not report["page_log"][0]["ocr_required"]


@pytest.mark.parametrize(("ocr_value", "review_required"), [("170", True), ("120", False)])
def test_native_vs_ocr_numeric_comparison_preserves_text_and_review_status(
    tmp_path, settings, audit, monkeypatch, ocr_value, review_required
):
    settings.docling_enabled = False
    native_text = (
        "This carefully reviewed laboratory report records 120 samples. Each sample was checked twice, "
        "and all measurements were independently reviewed before approval by the laboratory team."
    )
    ocr_text = native_text.replace("120", ocr_value)
    source = tmp_path / "numeric-conflict.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox(pymupdf.Rect(60, 70, 535, 260), native_text, fontsize=11)
        pdf.save(source)

    def diagnostics(page, settings, metadata_language="und"):
        return {
            "page": 1, "width": page.rect.width, "height": page.rect.height, "rotation": 0,
            "native_chars": len(page.get_text()), "health": "suspicious", "decision": "compare",
            "ocr_required": True, "native_decoding_risk": False, "mixed_content": False,
            "image_coverage": 0, "lang_hint": "en", "native_language": {},
            "reasons": [], "review_reasons": [],
        }

    def recognize(self, page, language, dpi, diag, status, **kwargs):
        return Recognition(1, "glm-ocr", blocks=[Block("p", ocr_text, source="ocr")]), {
            "engine": "glm-ocr", "status": "accepted", "requires_review": False,
            "attempts": [], "confidence": None,
        }

    monkeypatch.setattr(orchestrator, "analyze_page", diagnostics)
    monkeypatch.setattr(OCRRouter, "recognise", recognize)
    report = process_pdf(source, settings, tmp_path / "out", tmp_path / "run", audit)

    page_result = report["page_log"][0]
    xml = etree.parse(str(tmp_path / "out" / "numeric-conflict.xml"))
    extracted = " ".join(xml.getroot().itertext())
    assert page_result["source"] == "native"
    assert page_result["ocr"]["requires_review"] is review_required
    assert page_result["review_required"] is review_required
    assert "records 120 samples" in extracted
    if review_required:
        assert f"records {ocr_value} samples" not in extracted


def test_glm_page_flows_through_ir_and_xml(tmp_path, settings, audit, monkeypatch):
    settings.docling_enabled = False
    settings.audit_private_responses = True
    monkeypatch.setattr(glm, "model_info", lambda settings: {"ready": True})
    def recognise(page, dpi, settings):
        raw = {"done": True, "model": "glm-ocr:q8_0",
               "response": "# Sample heading\n\nA documented result.\n\n"
                           "<table><tr><th>Metric</th><th>Value</th></tr><tr><td>μ</td><td>42</td></tr></table>"
                           "\n\n$$E = mc^2$$"}
        return glm.parse(page.number + 1, raw)
    monkeypatch.setattr(glm, "recognise", recognise)
    monkeypatch.setattr(paddle.PaddleOCR, "recognise", lambda *args: pytest.fail("Accepted GLM result already exists."))
    source = tmp_path / "scan.pdf"
    fixture_pdf(source, scanned=True)
    stages = []
    result = process_pdf(source, settings, tmp_path / "out", tmp_path / "run", audit, processing=stages.append)
    assert result["ok"] and result["page_log"][0]["ocr"]["engine"] == "glm-ocr"
    assert [step["stage"] for step in stages] == ["diagnostics", "ocr", "ocr_attempt", "extracted", "layout", "validation"]
    assert stages[3]["counts"] == {"required": 1, "glm_ocr": 1, "paddle_ocr": 0, "failed": 0, "review": 0}
    xml = etree.parse(str(tmp_path / "out" / "scan.xml"))
    assert xml.findtext(".//table/tbody/tr/td") == "μ"
    assert xml.findtext(".//p[@kind='equation']") == "E = mc^2"
    assert (tmp_path / "run" / "recognition" / "scan" / "page-0001-1-glm-ocr.json").is_file()
    assert result["page_log"][0]["confidence"] is None


@pytest.mark.parametrize("failure", ["timeout", "empty", "table"])
def test_paddle_runs_after_failed_glm_only(tmp_path, settings, monkeypatch, failure):
    monkeypatch.setattr(glm, "model_info", lambda settings: {"ready": True})
    def recognise(page, dpi, settings):
        if failure == "timeout":
            raise RecognitionError("GLM-OCR timed out.", status="timeout")
        content = "" if failure == "empty" else "<table><tr><td>Incomplete</td></tr>"
        return glm.parse(1, {"done": True, "response": content})
    monkeypatch.setattr(glm, "recognise", recognise)
    monkeypatch.setattr(paddle.PaddleOCR, "recognise", lambda *args:
                        paddle.parse(1, [["Verified words"], [.98], [[[10, 12], [80, 12], [80, 23], [10, 23]]]], 144, "test"))
    calls = []
    with pymupdf.open() as pdf:
        result, info = OCRRouter(settings, tmp_path).recognise(pdf.new_page(), "en", 180, {}, calls.append)
    assert calls == ["glm-ocr", "paddleocr"] and info["engine"] == "paddleocr"
    assert info["attempts"][0]["status"] == ("timeout" if failure == "timeout" else "rejected")
    assert info["attempts"][0]["model"] == settings.glm_ocr_model
    assert result.lines[0].bbox == (5, 6, 40, 11.5)


def test_failed_page_remains_in_xml_and_is_marked_for_review(tmp_path, settings, audit, monkeypatch):
    settings.docling_enabled = False
    monkeypatch.setattr(glm, "model_info", lambda settings: {"ready": False, "reason": "GLM-OCR offline"})
    def unavailable(*args):
        raise RecognitionError("PaddleOCR unavailable.", unavailable=True)
    monkeypatch.setattr(paddle.PaddleOCR, "recognise", unavailable)
    source = tmp_path / "scan.pdf"
    fixture_pdf(source, scanned=True)
    result = process_pdf(source, settings, tmp_path / "out", tmp_path / "run", audit)
    assert result["ok"] and result["page_log"][0]["review_required"] and result["quality"]["review_pages"] == [1]
    xml = etree.parse(str(tmp_path / "out" / "scan.xml"))
    assert xml.find("page").get("n") == "1"


def test_evidence_write_failure_does_not_trigger_fallback(tmp_path, settings, monkeypatch):
    settings.audit_private_responses = True
    monkeypatch.setattr(glm, "model_info", lambda settings: {"ready": True})
    monkeypatch.setattr(glm, "recognise", lambda *args: glm.parse(1, {"done": True, "response": "Valid OCR text"}))
    monkeypatch.setattr(paddle.PaddleOCR, "recognise", lambda *args: pytest.fail("GLM-OCR produced valid text."))
    evidence = tmp_path / "blocked"
    evidence.touch()
    with pymupdf.open() as pdf:
        result, info = OCRRouter(settings, evidence).recognise(pdf.new_page(), "en", 180, {}, lambda _: None)
    assert result.text() == "Valid OCR text" and info["engine"] == "glm-ocr"
    assert info["attempts"][0]["evidence_error"]


def test_docling_enrichment_keeps_ocr_text_and_uses_matched_geometry():
    page = PageIR(1, 612, 792, [Block("p", "Same extracted title", source="ocr")], source="ocr")
    region = {"kind": "title", "text": "Same extracted title", "bbox": (40, 42, 100, 65)}
    assert apply_regions(page, [region]) == 1
    assert (page.blocks[0].text, page.blocks[0].tag, page.blocks[0].bbox) == (
        "Same extracted title", "h1", region["bbox"])


def test_ir_rejects_disordered_pages_and_invalid_geometry():
    doc = DocIR("example", "example.pdf", pages=[PageIR(1, 612, 792)]).to_dict()
    doc["pages"][0]["n"] = 2
    with pytest.raises(ValidationError, match="numbered"):
        ir_from_dict(doc)
    with pytest.raises(ValidationError, match="Bounding box"):
        Block("p", "invalid", bbox=(30, 20, 10, 40))


@pytest.mark.parametrize(("mode", "content", "tag"), [
    ("text", "Sample PDF text", "p"),
    ("table", "<table><tr><td>Value</td><td>42</td></tr></table>", "table"),
    ("formula", "$$E = mc^2$$", "p"),
])
def test_glm_uses_native_image_endpoint_and_configured_model(settings, monkeypatch, mode, content, tag):
    def respond(config, path, payload):
        assert config is settings and path == "/api/generate"
        assert payload["model"] == "glm-ocr:latest"
        assert payload["keep_alive"] == settings.glm_ocr_keep_alive and not payload["stream"]
        assert payload["prompt"] == f"{mode.capitalize()} Recognition:"
        assert b64decode(payload["images"][0]) == b"test-image"
        return {"response": content, "done": True}
    settings.glm_ocr_model = "glm-ocr:latest"
    monkeypatch.setattr(glm, "request_json", respond)
    result = glm.recognise_image(b"test-image", 3, settings, mode)
    assert result.page == 3 and result.blocks[0].tag == tag and not result.issues

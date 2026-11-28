import pymupdf
from lxml import etree

from agents.orchestrator import process_pdf
from config.settings import Settings
from diagnostics.profile import BLOCK_LANGUAGE_WARNING, decorate_document
from layout.model import Block, Cell, DocIR, Line, PageIR
from layout.reconstruct import join_arabic_fragments
from ocr import engine, glm, paddle
from ocr.recognition import Recognition
from ocr.router import OCRRouter
from reporting.exports import classify_source_review, requires_manual_review
from utils.logs import AuditLog
from xml_engine.serializer import to_xml
from xml_engine.validate import validate_xml


def test_arabic_fragments_follow_positions_without_reordering_characters():
    left = Line("الوطنية", (85, 39, 124, 58), source="ocr", rtl=True)
    right = Line("الهيئة (API 42)", (126, 43, 219, 59), source="ocr", rtl=True)
    other_column = Line("النتيجة ١٢٣", (10, 42, 38, 58), source="ocr", rtl=True)
    result = join_arabic_fragments([left, other_column, right])
    assert [line.text for line in result] == ["الهيئة (API 42) الوطنية", "النتيجة ١٢٣"]
    assert result[0].bbox == (85, 39, 219, 59)

    region = {"id": "table", "bbox": (50, 35, 230, 65), "class_name": "table"}
    assert len(join_arabic_fragments([left, right], [region])) == 2


def test_mixed_pdf_preserves_native_english_and_routes_scan_to_arabic(tmp_path, monkeypatch):
    pdf = tmp_path / "mixed.pdf"
    with pymupdf.open() as doc:
        native = doc.new_page(width=600, height=420)
        native.insert_text((35, 90), "The system records the audited total of 42 units.")
        doc.new_page(width=600, height=420)
        doc.new_page(width=420, height=600).set_rotation(90)
        doc.save(pdf)

    calls = []
    arabic_text = "هذا تقريرٌ بالعربية (API 42) بقيمة ١٢٣."

    def recognise(_, page, language, dpi, status):
        calls.append((page.number + 1, language, dpi))
        lines = [
            Line("هذا نص في الجانب الأيسر.", (50, 92, 230, 109), source="ocr", rtl=True),
            Line("العمود الأيسر يكتمل هنا.", (50, 118, 230, 134), source="ocr", rtl=True),
            Line(arabic_text, (325, 93, 560, 110), source="ocr", rtl=True),
            Line("العمود الأيمن يكتمل هنا.", (325, 120, 560, 136), source="ocr", rtl=True),
        ]
        raw = {"model": paddle.ARABIC_RECOGNIZER, "dpi": dpi}
        if page.rotation:
            raw["coordinate_space"] = "rendered-page-points"
        return Recognition(page.number + 1, "paddleocr", lines=lines, confidence=97, raw=raw)

    def unexpected(_):
        raise AssertionError("The accepted Arabic OCR result should finish before GLM is contacted.")

    monkeypatch.setattr(paddle.PaddleOCR, "recognise", recognise)
    monkeypatch.setattr(glm, "model_info", unexpected)
    monkeypatch.setattr(engine, "available_langs", lambda _: set())
    settings = Settings(ocr_language="ar-en", doclayout_enabled=False, docling_enabled=False,
                        schema_profile="brief")
    result = process_pdf(pdf, settings, tmp_path / "xml", tmp_path, AuditLog(tmp_path / "audit.jsonl"),
                         processing=lambda _: None)
    assert result["ok"]
    assert calls == [(2, "ar", 300), (3, "ar", 300)]
    xml = next((tmp_path / "xml").glob("*.xml")).read_bytes()
    assert validate_xml(xml, 3, settings)["issues"] == []
    pages = etree.fromstring(xml).findall("page")
    assert "audited total of 42" in "".join(pages[0].itertext())
    assert pages[1].get("dir") == "rtl"
    text = "".join(pages[1].itertext())
    assert arabic_text in text
    assert text.index(arabic_text) < text.index("هذا نص في الجانب الأيسر.")
    assert "".join(pages[2].itertext()) == text
    assert (tmp_path / "recognition" / "mixed" / "page-0002-2-paddleocr.json").is_file()


def test_arabic_table_keeps_rtl_grid_with_english_leftmost_cell():
    cells = [Cell("API 42", 0, 0, header=True), Cell("الرمز", 0, 1, header=True),
             Cell("12.5", 1, 0), Cell("القيمة ١٢٣", 1, 1)]
    table = Block("table", cells=cells)
    doc = DocIR("fixture", "fixture.pdf", pages=[PageIR(1, 500, 600, [
        Block("p", "هذا تقرير عن القياسات في المختبر."), table,
    ])])
    decorate_document(doc)
    root = etree.fromstring(to_xml(doc, Settings(schema_profile="brief")))
    assert root.find(".//table").get("dir") == "rtl"
    assert [node.text for node in root.findall(".//th")] == ["الرمز", "API 42"]
    assert [node.text for node in root.findall(".//td")] == ["القيمة ١٢٣", "12.5"]


def test_mixed_scan_tries_combined_packs_first(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "available_langs", lambda _: {"ara", "eng"})
    calls = []

    def recognise(page, language, dpi, settings, packs, *, orient, deskew):
        calls.append((language, dpi, packs, orient, deskew))
        return [Line("هذا تقرير باللغة العربية (API 42).", (20, 50, 200, 65),
                     source="ocr", conf=91, rtl=True)], {"langs": "ara+eng"}

    monkeypatch.setattr(engine, "ocr_page", recognise)
    router = OCRRouter(Settings(ocr_language="ar-en"), tmp_path)
    with pymupdf.open() as pdf:
        result, info = router.recognise(pdf.new_page(), "ar", 200, {"appears_scanned": True}, lambda _: None)
    assert calls == [("ar", 200, {"ara", "eng"}, True, True)]
    assert result.engine == "tesseract"
    assert info["langs"] == "ara+eng"
    assert [attempt["engine"] for attempt in info["attempts"]] == ["tesseract"]


def test_automatic_recovers_arabic_scan_when_script_probe_is_inconclusive(tmp_path, monkeypatch):
    text = "تقرير المراجعة يؤكد العدد ١٢٣ وحدة."
    monkeypatch.setattr(engine, "available_langs", lambda _: {"ara", "eng"})
    monkeypatch.setattr(engine, "pick_language", lambda *args: "und")
    monkeypatch.setattr(engine, "ocr_page", lambda *args, **kwargs: (
        [Line(text, (20, 50, 200, 65), source="ocr", conf=94, rtl=True)], {"langs": "ara+eng"}))

    def unexpected(_):
        raise AssertionError("Reliable Arabic OCR should finish before GLM is contacted.")

    monkeypatch.setattr(glm, "model_info", unexpected)
    router = OCRRouter(Settings(), tmp_path)
    with pymupdf.open() as pdf:
        result, info = router.recognise(pdf.new_page(), "und", 200, {"appears_scanned": True}, lambda _: None)
    assert result.text() == text and info["engine"] == "tesseract"
    assert not info["requires_review"]


def test_automatic_arabic_probe_does_not_replace_unverified_non_arabic_scan(tmp_path, monkeypatch):
    text = "The audit recorded exactly 123 entries."
    monkeypatch.setattr(engine, "available_langs", lambda _: {"ara", "eng"})
    monkeypatch.setattr(engine, "pick_language", lambda *args: "und")
    monkeypatch.setattr(engine, "ocr_page", lambda *args, **kwargs: (
        [Line("Different words printed here.", (20, 50, 200, 65), source="ocr", conf=94)], {"langs": "ara+eng"}))
    monkeypatch.setattr(glm, "model_info", lambda _: {"ready": True})
    monkeypatch.setattr(glm, "recognise", lambda *args: glm.parse(1, {"done": True, "response": text}))
    router = OCRRouter(Settings(), tmp_path)
    with pymupdf.open() as pdf:
        result, info = router.recognise(pdf.new_page(), "und", 200, {"appears_scanned": True}, lambda _: None)
    assert result.text() == text
    assert [(attempt["engine"], attempt["status"]) for attempt in info["attempts"]] == [
        ("tesseract", "rejected"), ("glm-ocr", "accepted")]


def test_arabic_language_metadata_warning_does_not_block_a_valid_export():
    page = PageIR(1, 500, 600, source="ocr", requires_review=True, diag={
        "ocr": {"status": "accepted", "requires_review": False},
        "review_reasons": [BLOCK_LANGUAGE_WARNING],
    })
    document = DocIR("fixture", "fixture.pdf", pages=[page])
    metadata_warning = {"page": 1, "kind": "source_extraction_requires_review"}
    completeness = {"source_review": [metadata_warning]}
    gate, warnings = classify_source_review(document, completeness)
    assert warnings == [metadata_warning]
    assert not requires_manual_review({}, gate)
    assert requires_manual_review({"multilingual_validation": {"blocking_pages": [1]}}, gate)

    numeric_disagreement = {"page": 1, "kind": "source_numeric_disagreement"}
    gate, warnings = classify_source_review(document, {"source_review": [metadata_warning, numeric_disagreement]})
    assert gate["source_review"] == [numeric_disagreement]
    assert requires_manual_review({}, gate)

    page.diag["ocr"]["requires_review"] = True
    gate, warnings = classify_source_review(document, completeness)
    assert gate["source_review"] == [metadata_warning] and not warnings

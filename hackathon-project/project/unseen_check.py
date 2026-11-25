"""Process an independently authored local holdout through extraction and export."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import subprocess

from lxml import etree
from playwright.sync_api import sync_playwright
from pptx import Presentation
from pptx.util import Inches, Pt
import pymupdf

from agents.orchestrator import process_pdf
from comparison.pairing import match_presentations
from config.settings import Settings
from language_check import measure
from layout.model import ir_from_dict
from powerpoint.amend import repair_presentation
from powerpoint.language import covering_font, presentation_language_report, shape_paragraph
from reporting.exports import publish_document, write_report
from tests.language_corpus import SAMPLES
from utils.logs import AuditLog
from utils.textutil import language_profile, norm_ws
from xml_engine.serializer import to_xml
from xml_engine.validate import validate_xml


LEFT = "The maintenance team recorded a steady pressure rise across the northern chamber during the overnight inspection. Calibrated sensors confirmed every observation."
RIGHT = "After the morning handover the field supervisor compared the reservoir readings with the baseline from last winter. The safety committee signed the revised worksheet."
CAPTION = "Figure 1. Reservoir outlet geometry"
FORMULA = "V = I × R"
CELLS = ["Station", "Pressure", "Harbor", "18 kPa", "Terminal", "31 kPa"]
ARABIC = "تقرير اختبار الخزّان في المصنع (Pump A) بتاريخ 2026-09-17، الضغط 48.5 kPa؛ إذا كان x ≥ 3."
HEBREW = "מדידת הלחץ במעבדה (Unit B) נערכה בתאריך 2026-09-18 והערך היה 62.7."
SINHALA = "කම්හලේ උතුරු කොටසෙහි නව මිනුම් වාර්තාව පරීක්ෂා කරන ලදී."
TAMIL = "தொழிற்சாலையின் வடக்குப் பகுதியில் புதிய அளவீட்டு அறிக்கையைச் சரிபார்த்தனர்."
SCAN = "The replacement filter arrived on Tuesday. The laboratory logged the shipment under receipt 7428."


def authored_pdfs(folder: Path) -> list[tuple[Path, list[dict]]]:
    table = "<table>" + "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in CELLS[i:i + 2])
                                + "</tr>" for i in range(0, len(CELLS), 2)) + "</table>"
    css = ("body{font:17px Arial,sans-serif;margin:0}p{margin:0 0 18px}"
           "table{border-collapse:collapse;margin:18px 0}td{border:1px solid;padding:8px 25px}"
           ".cols{display:grid;grid-template-columns:1fr 1fr;gap:44px}"
           ".arab{font-family:'Noto Sans Arabic',Arial}.hebr{font-family:'Noto Sans Hebrew',Arial}"
           ".indic{font-family:'Nirmala UI',sans-serif}")
    documents = [
        ("ledger-q", "620pt 680pt", f'<div class="cols"><p>{LEFT}</p><p>{RIGHT}</p></div>{table}'
         '<svg width="190" height="48"><rect x="1" y="8" width="110" height="34" fill="#487984"/>'
         '<path d="M111 25H185" stroke="#253442" stroke-width="8"/></svg>'
         f'<p>{CAPTION}</p><p>{FORMULA}</p>',
         [{"language": "en", "type": "digital", "segments": [LEFT, RIGHT, *CELLS, CAPTION, FORMULA],
           "table": CELLS, "figure": CAPTION, "equation": FORMULA}]),
        ("blue-index", "495pt 430pt", f'<h2 dir="rtl" class="arab">نتائج المعايرة</h2><p class="arab" dir="rtl">{ARABIC}</p>'
         f'<p class="hebr" dir="rtl">{HEBREW}</p>'
         '<table class="arab" dir="rtl"><tr><td>العينة</td><td>القيمة</td></tr><tr><td>المضخة</td><td>48.5</td></tr></table>',
         [{"language": "mul", "type": "digital", "segments": ["نتائج المعايرة", ARABIC, HEBREW, "العينة", "القيمة", "المضخة", "48.5"],
           "table": ["العينة", "القيمة", "المضخة", "48.5"],
           "regions": {"ar": ARABIC, "he": HEBREW}, "rtl": True},
          {"language": "en", "type": "scanned", "segments": [SCAN]}]),
        ("delta", "360pt 590pt", f'<p class="indic" lang="si">{SINHALA}</p><aside style="margin:32px 0 0 40px">'
         f'<p class="indic" lang="ta">{TAMIL}</p></aside>',
         [{"language": "mul", "type": "digital", "segments": [SINHALA, TAMIL], "regions": {"si": SINHALA, "ta": TAMIL}}]),
    ]
    outputs = []
    with sync_playwright() as browser:
        chromium = browser.chromium.launch(channel="chrome")
        page = chromium.new_page()
        for stem, size, content, expected in documents:
            path = folder / f"{stem}.pdf"
            page.set_content(f'<meta charset="utf-8"><style>@page{{size:{size};margin:46pt}}{css}</style>{content}')
            page.evaluate("document.fonts.ready")
            digital = page.pdf(prefer_css_page_size=True, print_background=True)
            with pymupdf.open(stream=digital, filetype="pdf") as source:
                if len(expected) > 1:
                    page.set_content(f'<style>body{{font:21px Arial;margin:64px}}</style><p>{SCAN}</p>')
                    with pymupdf.open(stream=page.pdf(), filetype="pdf") as scan:
                        source.new_page(width=530, height=410).insert_image(
                            pymupdf.Rect(0, 0, 530, 410), pixmap=scan[0].get_pixmap(dpi=145), keep_proportion=True)
                source.save(path)
            outputs.append((path, expected))
        chromium.close()
    return outputs


def page_result(page, expected: dict) -> dict:
    blocks = page.blocks if page else []
    def cells(block):
        if block.source == "native" and block.language.get("base_direction") == "rtl":
            return sorted(block.cells, key=lambda cell: (cell.row, -cell.col))
        return block.cells

    actual = [cell.text for block in blocks for cell in cells(block)]
    text = " ".join(block.caption if block.tag == "figure" else block.text or " ".join(
        [*(child.text for child in block.children), *(cell.text for cell in cells(block))]) for block in blocks)
    result = measure(" ".join(expected["segments"]), text, expected["language"])
    positions = [norm_ws(text).find(norm_ws(segment)) for segment in expected["segments"]]
    ordered = sum(0 <= x < y for x, y in zip(positions, positions[1:]))
    result.update(language=expected["language"], page_type=expected["type"], detected=page.language if page else {},
                  extraction_method=page.source if page else None, engine=page.ocr_engine if page else None,
                  reading_order_accuracy=ordered / max(1, len(positions) - 1) if len(positions) > 1 else None,
                  review_required=page.requires_review if page else True)
    if expected.get("table"):
        result["table"] = {"expected": expected["table"], "actual": actual,
                           "exact_sequence": expected["table"] == actual}
    if expected.get("figure"):
        result["figure_reference"] = any(block.bbox and norm_ws(block.caption) == expected["figure"]
                                         for block in blocks if block.tag == "figure")
    if expected.get("equation"):
        result["equation"] = any(block.kind == "equation" and norm_ws(block.text) == expected["equation"]
                                 for block in blocks)
    if expected.get("regions"):
        result["regions"] = {lang: measure(value, " ".join(block.text for block in blocks
                           if block.language.get("script") == language_profile(value, lang)["script"]
                           and block.kind != "title" and block.tag != "table"), lang)
                             for lang, value in expected["regions"].items()}
    return result


def render_matrix(folder: Path, capabilities: dict, soffice: Path | None, ir, settings) -> dict:
    from layout.model import Block, DocIR, PageIR
    from diagnostics.profile import decorate_document

    deck = Presentation()
    samples = list(SAMPLES.items()) + [("mixed-bidi", ARABIC)]
    for language, text in samples:
        deck_slide = deck.slides.add_slide(deck.slide_layouts[6])
        frame = deck_slide.shapes.add_textbox(Inches(.35), Inches(1.1), Inches(9.3), Inches(2)).text_frame
        frame.word_wrap = True
        run = frame.paragraphs[0].add_run()
        run.font.size = Pt(24)
        run.text = text
        shape_paragraph(frame.paragraphs[0]._p)
    deck_path = folder / "script-rendering.pptx"
    deck.save(deck_path)
    pdf_path = deck_path.with_suffix(".pdf")
    rendering = {"deck": deck_path.name, "renderer": "LibreOffice PDF export", "status": "renderer_unavailable"}
    if soffice and soffice.is_file():
        profile = (folder / "lo-profile").resolve().as_uri()
        proc = subprocess.run([str(soffice), f"-env:UserInstallation={profile}", "--headless", "--convert-to", "pdf", "--outdir",
            str(folder.resolve()), str(deck_path.resolve())], capture_output=True, timeout=120)
        rendering.update(status="rendered" if pdf_path.is_file() else "render_failed", exit_code=proc.returncode)
    rendered = {}
    if pdf_path.is_file():
        with pymupdf.open(pdf_path) as pdf:
            for index, (language, expected) in enumerate(samples):
                rendered[language] = measure(expected, pdf[index].get_text(), language)
            pdf[-1].get_pixmap(dpi=108).save(folder / "bidi-render.png")
        rendering["pdf"] = pdf_path.name
        rendering["application_verification"] = presentation_language_report(deck_path)
    for language, expected in samples:
        profile = language_profile(expected, language)
        doc = DocIR(doc_id="font-probe", path=ir.path,
                    pages=[PageIR(n=1, width=720, height=540, blocks=[Block(tag="p", text=expected)])])
        decorate_document(doc)
        xml = to_xml(doc, settings)
        root = etree.fromstring(xml)
        row = capabilities.setdefault("languages", {}).setdefault(language, {})
        row.update(fonts={script: covering_font(script, expected) for script in profile["scripts"]},
                   xml={"structural": validate_xml(xml, 1, settings),
                        "unicode_round_trip": expected in list(root.itertext())},
                   powerpoint={"rendered_pdf_text": rendered.get(language), "visual_fidelity": "requires_visual_review"},
                   coverage=[*(["labelled text extraction"] if "native" in row else []), "Unicode XML",
                             "font coverage", "presentation render when available"],
                   limitations=["Single labelled sentence per language. Per-language layout reconstruction is not established.",
                                "PDF text recovery from a rendered deck does not certify visual fidelity."])
    return rendering


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--capabilities", type=Path)
    parser.add_argument("--soffice", type=Path, default=Path(r"C:\Program Files\LibreOffice\program\soffice.com"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    fixtures = authored_pdfs(args.out)
    settings = Settings.load(use_cache=False)
    work = args.out / "work"
    audit = AuditLog(args.out / "audit.jsonl")
    profiles, validation, documents, extractions, irs = {}, {}, {}, {}, {}
    for path, truth in fixtures:
        result = process_pdf(path, settings, args.out / "xml", work, audit)
        ir_path = work / "ir" / f"{path.stem}.json"
        ir = ir_from_dict(json.loads(ir_path.read_text(encoding="utf-8"))) if ir_path.is_file() else None
        extractions[path.name] = result
        irs[path.name] = ir
        profiles[path.name] = result.get("document_profile", {})
        validation[path.name] = result.get("multilingual_validation", {})
        documents[path.name] = {"ok": result["ok"], "seconds": result.get("seconds"), "structural_xml": result.get("validation"),
                                "pages": [page_result(ir.pages[i] if ir and i < len(ir.pages) else None, expected)
                                          for i, expected in enumerate(truth)]}
        print(path.name, result["ok"], validation[path.name].get("status"), flush=True)
    source = fixtures[0][0]
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(2)).text_frame.text = LEFT + "\n" + RIGHT
    pptx = args.out / "unrelated-title.pptx"
    deck.save(pptx)
    pairs = match_presentations([path for path, _ in fixtures], [pptx], work)
    ir = irs[source.name]
    export_settings = replace(settings, schema_profile="sample")
    published = publish_document(args.out / "paired-export", source.stem, ir, export_settings, extractions[source.name],
                                 {"document_id": source.stem},
                                 lambda output: repair_presentation(ir, pptx, output, export_settings))
    archive = published.get("archive")
    copied = args.out / "paired-export" / f"{source.stem}_updated.pptx"
    fidelity = None
    if copied.is_file():
        updated = Presentation(copied)
        fidelity = {"slide_count": len(updated.slides),
                    "source_text_exact": updated.slides[0].shapes[0].text_frame.text == LEFT + "\n" + RIGHT,
                    "applied": published["applied"], "review": published["review"]}
    report = {"corpus": "Independently authored synthetic holdout. Official finale documents were unavailable.",
              "cache_enabled": settings.use_cache, "documents": documents,
              "matching": {"expected_source": source.name, "deck": pptx.name,
                           "actual": [[pdf.name, deck.name] for pdf, deck in pairs]},
              "paired_export": {"status": published["status"], "validation": published["validation"],
                                "archive_path": archive["path"] if archive else None, "fidelity": fidelity}}
    blocked = [entry for entry in validation if validation[entry].get("blocking_pages")]
    report["review_exports"] = []
    for filename in blocked:
        reviewed_ir = irs[filename]
        gated = publish_document(args.out / "review-export" / reviewed_ir.doc_id, reviewed_ir.doc_id, reviewed_ir,
                                 export_settings, extractions[filename], {"document_id": reviewed_ir.doc_id},
                                 lambda output: repair_presentation(reviewed_ir, pptx, output, export_settings))
        report["review_exports"].append({"document": filename, "status": gated["status"], "outputs": gated["outputs"]})
    write_report(args.out / "document_profile.json", profiles)
    write_report(args.out / "multilingual_validation_report.json", validation)
    write_report(args.out / "unseen_document_test_report.json", report)
    if args.capabilities:
        capabilities = json.loads(args.capabilities.read_text(encoding="utf-8"))
        capabilities.setdefault("required_language_source", {"document": "Innodata_AI_Catalyst.pdf", "page": 8})
        capabilities["presentation_rendering"] = render_matrix(args.out, capabilities, args.soffice, ir, settings)
        write_report(args.out / "language_support_report.json", capabilities)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

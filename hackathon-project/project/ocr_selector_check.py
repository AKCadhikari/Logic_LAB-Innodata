"""Benchmark strict OCR selection and actual Automatic fallback against labelled scans."""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pymupdf
from pptx import Presentation
from pptx.util import Inches

from agents.orchestrator import process_pdf
from config.settings import Settings
from diagnostics.profile import block_text
from language_check import measure
from layout.model import ir_from_dict
from powerpoint.amend import repair_presentation
from reporting.exports import publish_document, write_report
from utils import execution_audit as audit
from utils.logs import AuditLog
from utils.textutil import norm_ws

TEXT = {
    "en": "The team checked the pressure before the pump was switched on. The reading stayed steady.",
    "fr": "Le technicien a vérifié la pression avant le départ de la pompe. La mesure reste stable.",
}
NATIVE = ("The maintenance team recorded a steady pressure rise across the northern chamber during the overnight "
          "inspection. Calibrated sensors confirmed every observation.")
CELLS = ["Zone", "Pressure", "North", "42 kPa", "South", "35 kPa"]


def sample(path, language, nonce):
    with pymupdf.open() as digital, pymupdf.open() as pdf:
        page = digital.new_page(width=550, height=260)
        heading = f"Record {nonce}"
        page.insert_text((35, 38), heading, fontsize=20)
        page.insert_textbox((35, 52, 520, 110), TEXT[language], fontsize=18)
        for index, value in enumerate(CELLS):
            x, y = 35 + index % 2 * 180, 112 + index // 2 * 35
            page.draw_rect((x, y, x + 180, y + 35), width=1)
            page.insert_text((x + 10, y + 23), value, fontsize=17)
        pdf.new_page(width=550, height=260).insert_image(page.rect, pixmap=page.get_pixmap(dpi=175))
        pdf.new_page().insert_textbox((65, 100, 530, 240), NATIVE, fontsize=12)
        pdf.save(path)
    return " ".join([heading, TEXT[language], *CELLS])


def run(path, settings, folder, language, expected):
    work = folder / "work"
    result = process_pdf(path, settings, folder / "xml", work, AuditLog(folder / "decisions.jsonl"))
    ir_path = work / "ir" / f"{path.stem}.json"
    ir = ir_from_dict(json.loads(ir_path.read_text(encoding="utf-8")))
    page = ir.pages[0]
    actual = " ".join(block_text(block) for block in page.blocks)
    cells = [norm_ws(cell.text) for block in page.blocks for cell in block.cells]
    calls = [row for row in result["execution_evidence"]["executions"] if row["task"] == "page_ocr"]
    selected = page.diag.get("ocr", {})
    quality = measure(expected, actual, language)
    row = {key: quality[key] for key in ("cer", "wer", "exact_logical_order")}
    row.update(engine=page.ocr_engine or settings.ocr_backend, selected=settings.ocr_backend, language=language,
               ocr_seconds=round(sum(call["duration_ms"] for call in calls) / 1000, 3),
               document_seconds=result.get("seconds"), table_cells_detected=len(cells),
               table_cell_accuracy=sum(a == b for a, b in zip(CELLS, cells)) / max(len(CELLS), len(cells)),
               failed_requests=sum(call["validation"] != "passed" for call in calls), requests=len(calls),
               fallback_reason=selected.get("fallback_reason"), attempts=selected.get("attempts", []),
               xml_valid=result["ok"], executions=calls)
    print(language, settings.ocr_backend, page.ocr_engine, row["cer"], row["table_cell_accuracy"], flush=True)
    return row, ir, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dashboard-file", type=Path)
    args = parser.parse_args()
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    settings = Settings.load(use_cache=False, docling_enabled=False)
    audit.test_mode.set("LIVE_MODEL_TEST")
    rows = []
    for language in TEXT:
        path = root / f"sample-{language}.pdf"
        nonce = str(1000000 + uuid4().int % 9000000)
        expected = sample(path, language, nonce)
        for engine in ("glm-ocr", "paddleocr"):
            config = replace(settings, ocr_backend=engine, paddle_ocr_lang=language)
            row, ir, result = run(path, config, root / f"{language}-{engine}", language, expected)
            rows.append(row)
            assert all(call["ocr_engine"] == engine for call in row["executions"])
            assert any(call["executed"] and call["response_sha256"] for call in row["executions"]), row
            if language == "en" and engine == "paddleocr":
                deck = Presentation()
                slide = deck.slides.add_slide(deck.slide_layouts[6])
                slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(2)).text_frame.text = NATIVE.replace("northern", "norrthern")
                source = root / "source.pptx"
                deck.save(source)
                published = publish_document(root / "paired-export", path.stem, ir, config, result,
                                             {"document_id": ir.doc_id},
                                             lambda target: repair_presentation(ir, source, target, config))
                assert published["status"] == "completed" and published["applied"], published
        if language == "en":
            fallback_config = replace(settings, ocr_backend="automatic", ocr_automatic_primary="glm-ocr",
                                      glm_ocr_model=f"ocr-check-uninstalled:{nonce}", paddle_ocr_lang="")
            fallback, _, _ = run(path, fallback_config, root / "automatic-fallback", language, expected)
            assert fallback["engine"] == "paddleocr" and fallback["attempts"][0]["status"] == "unavailable"
            assert any(call["executed"] and call.get("fallback_reason") for call in fallback["executions"]), fallback
    report = {"mode": "LIVE_MODEL_TEST", "fixture": "Labelled English and French scans with a six-cell table",
              "benchmarks": rows, "automatic_fallback": fallback, "paired_export": "verified"}
    write_report(root / "ocr-comparison.json", report)
    if args.dashboard_file:
        args.dashboard_file.parent.mkdir(parents=True, exist_ok=True)
        write_report(args.dashboard_file, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

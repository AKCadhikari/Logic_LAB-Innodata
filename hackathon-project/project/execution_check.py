"""Run local inference and end-to-end checks on newly generated, nonconfidential fixtures."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4

from lxml import etree
import pymupdf
from pptx import Presentation
from pptx.util import Inches

from agents.orchestrator import process_pdf
from comparison.ollama import annotate
from config.settings import Settings
from layout.model import ir_from_dict
from ocr import glm, paddle
from ocr.router import OCRRouter
from powerpoint.amend import repair_presentation
from reporting.execution import reports
from reporting.exports import publish_document, write_report
from utils import execution_audit as audit, ollama
from utils.cache import file_key
from utils.logs import AuditLog


def fixture(path, nonce, body=None):
    with pymupdf.open() as output, pymupdf.open() as digital:
        scan = digital.new_page(width=480, height=220)
        scan.insert_text((40, 65), f"Laboratory record {nonce}", fontsize=22)
        scan.insert_text((40, 105), "Record the observed pressure.", fontsize=20)
        output.new_page(width=480, height=220).insert_image(scan.rect, pixmap=scan.get_pixmap(dpi=160))
        if body:
            page = output.new_page()
            page.insert_textbox((65, 100, 530, 235), body, fontsize=12)
        output.save(path)


def check():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    settings = Settings.load(use_cache=False)
    log = AuditLog(out / "decisions.jsonl")
    inventory = ollama.installed_models(ollama.request_json(settings.glm_ocr_url, "/api/tags", None, 5))
    status = {"available_local_models": [row["name"] for row in inventory if "name" in row],
              "glm": glm.model_info(settings), "paddle": paddle.readiness(settings, "en")}
    assert status["glm"]["ready"], status["glm"]
    audit.test_mode.set("LIVE_MODEL_TEST")
    live_pdf = out / "live-probe.pdf"
    nonce = str(1000000 + uuid4().int % 9000000)
    fixture(live_pdf, nonce)
    with audit.scope(live_pdf.stem, file_key(live_pdf)) as live, pymupdf.open(live_pdf) as pdf:
        with audit.details(model_digest=status["glm"]["digest"]):
            result = glm.recognise(pdf[0], 180, settings)
        assert nonce in result.text(), "GLM-OCR did not recover the unique number."
        audit.mark(result.execution_ids, validation="passed")
        calls = live["executions"]
        assert calls and calls[0]["executed"] and calls[0]["response_sha256"] and calls[0]["http_status"] == 200
    status["live_glm_nonce_verified"] = True
    comparison = [{"category": "text", "change_type": "modified", "old_content": f"Probe {nonce} recorded a pressure of 240 kPa.",
                   "new_content": f"Probe {nonce} recorded a pressure of 260 kPa."}]
    with audit.scope(live_pdf.stem, file_key(live_pdf), trace_id=live["trace_id"]) as review:
        analysis = annotate(comparison, settings, log)
        assert analysis["status"] == "completed" and analysis["execution"]["executed"], analysis
        assert comparison[0]["assistance"]["classification"] in {"numerical", "uncertain"}
    live["executions"].extend(review["executions"])
    status["live_qwen_schema_verified"] = True
    if status["paddle"]["ready"]:
        fallback_pdf = out / "fallback-probe.pdf"
        fall_nonce = str(1000000 + uuid4().int % 9000000)
        fixture(fallback_pdf, fall_nonce)
        fallback_settings = replace(settings, glm_ocr_model=f"audit-absent:{fall_nonce}")
        with audit.scope(fallback_pdf.stem, file_key(fallback_pdf)) as fallback, pymupdf.open(fallback_pdf) as pdf:
            router = OCRRouter(fallback_settings, out / "fallback-recognition")
            try:
                recognized, details = router.recognise(pdf[0], "en", 210, {"health": "absent"}, lambda _: None)
                assert recognized is not None and recognized.engine == "paddleocr", details
                assert fall_nonce in recognized.text() and any(row["executed"] for row in fallback["executions"])
            finally:
                router.close()
        status["live_paddle_nonce_verified"] = True
        status["paddle_probe"] = reports(None, fallback)["ai_execution_report.json"]
    write_report(out / "live_model_test.json", {**status, **reports(None, live)["ai_execution_report.json"]})

    audit.test_mode.set("FULL_PIPELINE_TEST")
    body = ("The maintenance team recorded a steady pressure rise across the northern chamber during the overnight "
            "inspection. Calibrated sensors confirmed every observation.")
    source = out / "pipeline-probe.pdf"
    pipeline_nonce = str(1000000 + uuid4().int % 9000000)
    fixture(source, pipeline_nonce, body)
    work = out / "work"
    result = process_pdf(source, settings, out / "xml", work, log)
    assert result["ok"], result.get("error")
    ir = ir_from_dict(json.loads((work / "ir" / "pipeline-probe.json").read_text(encoding="utf-8")))
    assert any(pipeline_nonce in block.text and block.source_reference.get("execution_ids") for block in ir.pages[0].blocks)
    xml = etree.parse(str(out / "xml" / "pipeline-probe.xml"))
    assert pipeline_nonce in "".join(xml.getroot().itertext())
    review_changes = [{"category": "text", "change_type": "modified",
                       "old_content": f"Inspection {pipeline_nonce} recorded a pressure of 240 kPa.",
                       "new_content": f"Inspection {pipeline_nonce} recorded a pressure of 260 kPa."}]
    with audit.scope(source.stem, file_key(source), trace_id=result["execution_evidence"]["trace_id"]):
        pipeline_review = annotate(review_changes, settings, log)
        assert pipeline_review["status"] == "completed" and pipeline_review["execution"]["executed"], pipeline_review
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(2))
    shape.text_frame.text = body.replace("northern", "norrthern")
    input_deck = out / "pipeline-input.pptx"
    deck.save(input_deck)
    export = out / "export"
    published = publish_document(export, source.stem, ir, replace(settings, schema_profile="sample"), result,
                                 {"document_id": source.stem},
                                 lambda target: {**repair_presentation(ir, input_deck, target, settings),
                                                 "analysis": pipeline_review})
    assert published["status"] == "completed", published.get("error")
    trace = json.loads((export / "processing_trace.json").read_text(encoding="utf-8"))
    evidence = json.loads((export / "ai_execution_report.json").read_text(encoding="utf-8"))
    assert trace["pptx"], "No validated slide correction was linked to the source IR."
    assert trace["qwen_execution_id"] == pipeline_review["execution_id"]
    assert any(row["model"] == settings.glm_ocr_model and row["executed"] and
               {"pydantic_ir", "xml"} <= {item["consumer"] for item in row["downstream"]}
               for row in evidence["executions"]), "No live OCR-to-XML chain."
    print("Live GLM and Qwen checks and full pipeline passed. Reports:", out)


if __name__ == "__main__":
    check()

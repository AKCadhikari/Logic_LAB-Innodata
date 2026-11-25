"""DocuSync AI - single documented entry point.

    python main.py --input ../phase-1/input --output ../phase-1/output

Reads PDFs and paired decks in --input and publishes versioned XML and PPTX bundles in --output.
Logs, per-document IR, aggregate reports and caches go to --work-dir (default: <output>/../docsync_run).
"""
from __future__ import annotations
import argparse, json, multiprocessing, sys, time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from config.settings import Settings
from ingestion.discover import discover, describe, duplicates
from utils.logs import AuditLog, get_logger
from comparison.pairing import match_presentations


def build_parser():
    ap = argparse.ArgumentParser(description="DocuSync AI: PDF -> XML and presentation proof-reading")
    ap.add_argument("--input", default=str(HERE.parent / "phase-1" / "input"), help="folder with PDFs (+ PPTX)")
    ap.add_argument("--output", default=str(HERE.parent / "phase-1" / "output"), help="folder for XML + corrected PPTX")
    ap.add_argument("--work-dir", default=None, help="logs, IR, reports, cache (default: <output>/../docsync_run)")
    ap.add_argument("--config", default=None, help="JSON file overriding config.settings.Settings")
    ap.add_argument("--workers", type=int, default=None, help="parallel documents (0 = auto)")
    ap.add_argument("--profile", choices=["brief", "example", "sample"], default=None,
                    help="XML profile for extraction and explicit XML-only exports")
    ap.add_argument("--no-ocr", action="store_true", help="disable OCR fallback (diagnostics only)")
    ap.add_argument("--ocr-backend", choices=["automatic", "glm-ocr", "paddleocr", "tesseract"],
                    help="select the OCR pipeline")
    ap.add_argument("--skip-pptx", action="store_true", help="only generate XML")
    ap.add_argument("--time-budget", type=int, default=None, help="seconds before the run switches to fast mode")
    return ap


def publish_batch_document(pdf: Path, extraction: dict, decks: list[Path], out: Path, work: Path,
                           settings: Settings, audit: AuditLog, *, xml_only: bool = False) -> tuple[dict, dict | None]:
    from layout.model import ir_from_dict
    from powerpoint.amend import amend_deck
    from reporting.exports import output_stem, publish_document

    export_settings = settings if xml_only else replace(settings, schema_profile="sample")
    ir = (ir_from_dict(json.loads((work / "ir" / f"{pdf.stem}.json").read_text(encoding="utf-8")))
          if extraction.get("ok") else None)
    stem = output_stem(pdf.name)
    export_id = uuid4()
    amendment = None

    def transform(target):
        nonlocal amendment
        if len(decks) != 1:
            raise ValueError("Exactly one associated PowerPoint is required.")
        amendment = amend_deck(pdf, decks[0], target, work, export_settings, audit)
        return amendment

    report = publish_document(out / stem / export_id.hex, stem, ir, export_settings, extraction,
                              {"id": str(export_id), "document_id": pdf.stem, "filename": pdf.name,
                               "created": datetime.now(timezone.utc).isoformat()}, transform, xml_only=xml_only)
    result = {**extraction, "extraction_ok": bool(extraction.get("ok")), "ok": report["status"] in {"completed", "xml_only"},
              "status": report["status"], "outputs": report["outputs"], "output_folder": report["output_folder"],
              "error": report.get("error", ""), "validation": report["validation"]["xml"]}
    if not xml_only and report["status"] != "completed":
        amendment = {"pptx": decks[0].name if len(decks) == 1 else "", "pdf": pdf.name, "valid": False,
                     "status": "failed", "output": None, "applied": 0, "review": 0, "error": report["error"]}
    return result, amendment


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.load(args.config, workers=args.workers, schema_profile=args.profile, time_budget_s=args.time_budget,
                             ocr_enabled=False if args.no_ocr else None, ocr_backend=args.ocr_backend)
    inp, out = Path(args.input).resolve(), Path(args.output).resolve()
    work = Path(args.work_dir).resolve() if args.work_dir else out.parent / "docsync_run"
    log = get_logger()
    if not inp.is_dir():
        log.error("input folder not found: %s", inp); return 2
    out.mkdir(parents=True, exist_ok=True); work.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(work / "audit.jsonl")
    t0 = time.time()
    found = discover(inp)
    infos = [describe(p) for p in found["pdfs"] + found["pptxs"]]
    dups = duplicates(infos)
    (work / "manifest.json").write_text(__import__("json").dumps({"files": infos, "duplicates": dups, "skipped": [str(s) for s in found["skipped"]]}, indent=2), encoding="utf-8")
    pdfs = [p for p in found["pdfs"] if next(i for i in infos if i["path"] == str(p))["valid"]]
    log.info("found %d PDF, %d PPTX (%d duplicate(s))", len(found["pdfs"]), len(found["pptxs"]), len(dups))
    from agents.orchestrator import run_pdfs
    stem_counts = Counter(pdf.stem.casefold() for pdf in pdfs)
    unique = [pdf for pdf in pdfs if stem_counts[pdf.stem.casefold()] == 1]
    results = [{"doc_id": pdf.stem, "file": pdf.name, "pages": 0, "ok": False, "status": "failed",
                "error": "Multiple input PDFs have the same document name.", "outputs": {"xml": None, "pptx": None}}
               for pdf in pdfs if stem_counts[pdf.stem.casefold()] > 1]
    amend = []
    with TemporaryDirectory(prefix="extract-", dir=work) as staging:
        extracted = run_pdfs(unique, settings, Path(staging), work, audit, t0)
    by_id = {pdf.stem: pdf for pdf in unique}
    eligible = [by_id[item["doc_id"]] for item in extracted if item.get("ok")]
    pairs = (match_presentations(eligible, found["pptxs"], work, explicit_single=len(unique) == 1)
             if not args.skip_pptx else [])
    for extraction in extracted:
        pdf = by_id[extraction["doc_id"]]
        decks = [deck for source, deck in pairs if source == pdf]
        try:
            result, amendment = publish_batch_document(pdf, extraction, decks, out, work, settings, audit,
                                                       xml_only=args.skip_pptx)
        except (OSError, ValueError) as error:
            audit.event("export", "failed", document=pdf.name, error=str(error))
            result, amendment = {**extraction, "ok": False, "status": "failed", "error": str(error),
                                  "outputs": {"xml": None, "pptx": None}}, None
        results.append(result)
        if amendment:
            amend.append(amendment)
        log.info("%-28s pages=%-3s %s", pdf.name, result.get("pages", 0), result["status"])
    if not args.skip_pptx:
        associated = {deck for pdf, deck in pairs if pdf in unique}
        for deck in found["pptxs"]:
            if deck not in associated:
                amend.append({"pptx": deck.name, "valid": False, "status": "failed", "output": None,
                              "error": "No uniquely matched source PDF."})
    from reporting.report import write_reports
    write_reports(work, results, amend, settings, {"wall": time.time() - t0})
    log.info("done in %.1fs; report: %s", time.time() - t0, work / "report.html")
    return 0 if results and all(r.get("ok") for r in results) and all(a["valid"] for a in amend) else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())

"""DocuSync AI - single documented entry point.

    python main.py --input ../phase-1/input --output ../phase-1/output

Reads every PDF (and PPTX) in --input, writes <name>.xml for each PDF and the corrected PPTX files to --output.
Logs, per-document IR, reports and caches go to --work-dir (default: <output>/../docsync_run) so --output stays clean.
"""
from __future__ import annotations
import argparse, multiprocessing, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from config.settings import Settings
from ingestion.discover import discover, describe, duplicates
from utils.logs import AuditLog, get_logger
from utils.naming import pair_documents, pptx_out_name


def build_parser():
    ap = argparse.ArgumentParser(description="DocuSync AI: PDF -> XML and presentation proof-reading")
    ap.add_argument("--input", default=str(HERE.parent / "phase-1" / "input"), help="folder with PDFs (+ PPTX)")
    ap.add_argument("--output", default=str(HERE.parent / "phase-1" / "output"), help="folder for XML + corrected PPTX")
    ap.add_argument("--work-dir", default=None, help="logs, IR, reports, cache (default: <output>/../docsync_run)")
    ap.add_argument("--config", default=None, help="JSON file overriding config.settings.Settings")
    ap.add_argument("--workers", type=int, default=None, help="parallel documents (0 = auto)")
    ap.add_argument("--profile", choices=["brief", "example"], default=None, help="XML schema profile")
    ap.add_argument("--no-ocr", action="store_true", help="disable OCR fallback (diagnostics only)")
    ap.add_argument("--skip-pptx", action="store_true", help="only generate XML")
    ap.add_argument("--time-budget", type=int, default=None, help="seconds before the run switches to fast mode")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.load(args.config, workers=args.workers, schema_profile=args.profile, time_budget_s=args.time_budget,
                             ocr_enabled=False if args.no_ocr else None)
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
    results = run_pdfs(pdfs, settings, out, work, audit, t0)
    for r in results:
        log.info("%-28s pages=%-3s ocr=%-3s %s", r.get("file"), r.get("pages"), r.get("ocr_pages", 0), "OK" if r.get("ok") else "FAILED: " + r.get("error", ""))
    amend = []
    if found["pptxs"] and not args.skip_pptx:
        from powerpoint.amend import amend_deck
        for pdf, pptx in pair_documents(found["pdfs"], found["pptxs"]):
            target = out / pptx_out_name(pptx, settings.pptx_suffix_rule)
            try:
                ar = amend_deck(pdf, pptx, target, work, settings, audit)
            except Exception as e:                       # a failed deck must never stop the batch
                audit.event("powerpoint", "deck_failed", deck=pptx.name, error=str(e))
                import shutil; shutil.copyfile(pptx, target)
                ar = {"pptx": pptx.name, "output": target.name, "error": str(e), "applied": 0, "review": 0}
            amend.append(ar)
            log.info("deck %-40s -> %s (applied=%s review=%s)", pptx.name, target.name, ar.get("applied"), ar.get("review"))
    from reporting.report import write_reports
    write_reports(work, results, amend, settings, {"wall": time.time() - t0})
    log.info("done in %.1fs; report: %s", time.time() - t0, work / "report.html")
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())

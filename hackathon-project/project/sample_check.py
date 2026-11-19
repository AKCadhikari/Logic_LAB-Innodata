"""Process a local sample and compare the generated files to the supplied references."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from subprocess import TimeoutExpired
from zipfile import BadZipFile

from lxml.etree import XMLSyntaxError
from pptx.exc import PackageNotFoundError

from agents.orchestrator import process_pdf
from config.settings import Settings
from powerpoint.amend import amend_deck
from quality.conformance import compare_pptx, compare_xml
from quality.evaluate import compare as score_xml
from quality.evaluate_pptx import score as score_deck
from quality.slide_preview import compare_slides, renderer
from utils.logs import AuditLog
from utils.naming import pptx_out_name, xml_name


def write_report(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("pdf", "source-deck", "expected-xml", "expected-deck", "out"):
        parser.add_argument(f"--{argument}", type=Path, required=True)
    parser.add_argument("--previous-deck", type=Path, action="append", default=[])
    parser.add_argument("--profile", choices=("brief", "sample", "example"), default="sample")
    parser.add_argument("--ocr-backend", choices=("glm-ocr", "tesseract"))
    parser.add_argument("--renderer", help="Path to a local LibreOffice executable")
    args = parser.parse_args()
    for item in (args.pdf, args.source_deck, args.expected_xml, args.expected_deck, *args.previous_deck):
        if not item.is_file():
            parser.error(f"File not found: {item}")

    out = args.out.resolve()
    if out.exists():
        parser.error("Choose a new output directory.")
    out.mkdir(parents=True)
    generated, work = out / "generated", out / "run"
    audit = AuditLog(work / "audit.jsonl")
    settings = Settings.load(schema_profile=args.profile, workers=1, ocr_backend=args.ocr_backend)
    actual_xml = generated / xml_name(args.pdf)
    actual_deck = generated / pptx_out_name(args.source_deck)
    summary = {"profile": args.profile, "pdf": str(args.pdf.resolve()), "errors": [], "warnings": []}
    summary["extraction"] = process_pdf(args.pdf.resolve(), settings, generated, work, audit)
    if not summary["extraction"]["ok"]:
        summary["errors"].append("Source PDF extraction failed. See run/audit.jsonl.")
        write_report(out / "summary.json", summary)
        return 2
    xml_report = compare_xml(args.expected_xml, actual_xml)
    xml_report["text_metrics"] = score_xml(actual_xml, args.expected_xml)
    write_report(out / "xml-comparison.json", xml_report)
    summary["xml"] = {"match": xml_report["match"], "diff_counts": xml_report["diff_counts"],
                      "text_metrics": xml_report["text_metrics"]}
    try:
        summary["amendment"] = amend_deck(args.pdf, args.source_deck, actual_deck, work, settings, audit)
        main_comparison = compare_pptx(args.expected_deck, actual_deck)
        deck_report = {"expected_vs_actual": main_comparison,
                       "original_vs_actual": compare_pptx(args.source_deck, actual_deck),
                       "original_vs_expected": compare_pptx(args.source_deck, args.expected_deck),
                       "text_metrics": {"original": score_deck(args.source_deck, args.expected_deck),
                                        "actual": score_deck(actual_deck, args.expected_deck)},
                       "previous_outputs": {str(path): compare_pptx(args.expected_deck, path)
                                            for path in args.previous_deck}}
        write_report(out / "powerpoint-comparison.json", deck_report)
        summary["powerpoint"] = {"match": main_comparison["match"], "actual_slides": main_comparison["actual_slides"],
                                 "different_slides": len(main_comparison["changed_slides"]),
                                 "text_metrics": deck_report["text_metrics"]}
    except (OSError, ValueError, BadZipFile, XMLSyntaxError, PackageNotFoundError) as error:
        summary["errors"].append(f"PowerPoint processing or validation failed: {error}")
    executable = args.renderer or renderer()
    if executable and actual_deck.is_file():
        try:
            summary["visual"] = compare_slides({"original": args.source_deck, "expected": args.expected_deck,
                                                "actual": actual_deck}, out / "visual", executable)
        except (OSError, ValueError, TimeoutExpired) as error:
            summary["errors"].append(f"Slide rendering failed: {error}")
    elif not executable:
        summary["warnings"].append("LibreOffice is unavailable. Slide previews were skipped.")
    visuals = summary.get("visual", {}).get("slides", [])
    summary["match"] = (summary["xml"]["match"] and summary.get("powerpoint", {}).get("match", False)
                        and not summary["errors"] and bool(visuals)
                        and all(row["expected_matches_actual"] for row in visuals))
    write_report(out / "summary.json", summary)
    print(f'{"Match" if summary["match"] else "Differences found"} | {out / "summary.json"}')
    return 2 if summary["errors"] else 0 if summary["match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

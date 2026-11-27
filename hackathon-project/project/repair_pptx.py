"""Repair one explicitly paired PowerPoint against its original PDF."""
import argparse
from dataclasses import replace
from pathlib import Path

from config.settings import Settings
from powerpoint.amend import amend_deck
from reporting.exports import write_report
from utils.logs import AuditLog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--pptx", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--ai-review", action="store_true")
    args = parser.parse_args()
    source, slides, target = (path.resolve() for path in (args.pdf, args.pptx, args.out))
    if not source.is_file() or source.suffix.lower() != ".pdf" or not slides.is_file() or slides.suffix.lower() != ".pptx":
        parser.error("Supply a readable source PDF and presentation.")
    if target.suffix.lower() != ".pptx" or target == slides or (target.exists() and target.samefile(slides)):
        parser.error("Choose a different PPTX output path.")
    work = args.work_dir.resolve() if args.work_dir else target.parent / f"{target.stem}_work"
    work.mkdir(parents=True, exist_ok=True)
    settings = Settings.load(args.config, default_schema_profile="sample")
    if args.ai_review:
        settings = replace(settings, amend_ai_review=True)
    report = amend_deck(source, slides, target, work, settings, AuditLog(work / "repair-audit.jsonl"))
    write_report(work / "repair-result.json", report)
    print(f"{report['slides']} slides, {report['applied']} saved corrections, {report['review']} review candidates")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

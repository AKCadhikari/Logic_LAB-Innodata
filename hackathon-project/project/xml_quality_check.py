"""Evaluate generated XML against reference XML and optionally compare a baseline."""
import argparse
from pathlib import Path
import json

from quality.xml_alignment import evaluate_xml
from reporting.exports import write_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("reference", "actual", "report"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--equation-evidence", type=Path, help="Optional source-reviewed equation labels JSON.")
    args = parser.parse_args()
    reference = args.reference.read_bytes()
    labels = json.loads(args.equation_evidence.read_text(encoding="utf-8"))["equations"] if args.equation_evidence else None
    paths = {"actual": args.actual, **({"baseline": args.baseline} if args.baseline else {})}
    results = {name: {"path": str(path.resolve()), **evaluate_xml(reference, path.read_bytes(), labels=labels)}
               for name, path in paths.items()}
    write_report(args.report, {"reference": str(args.reference.resolve()), **results})
    return 0 if not results["actual"]["validation"]["issues"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

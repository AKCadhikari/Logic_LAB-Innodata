"""Benchmark any input corpus and optionally compare a supplied pair with reference outputs."""
from __future__ import annotations
import argparse, json, os, platform, shutil, sys, tempfile, time
from pathlib import Path
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_measured(args: list[str]) -> dict:
    started = time.perf_counter()
    peak = 0
    process = psutil.Popen(args)
    while process.poll() is None:
        try:
            processes = [process, *process.children(recursive=True)]
        except psutil.NoSuchProcess:
            break
        total = 0
        for child in processes:
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        peak = max(peak, total)
        time.sleep(0.05)
    return {"seconds": round(time.perf_counter() - started, 2), "exit_code": process.wait(),
            "peak_sampled_process_tree_rss_mib": round(peak / (1024 * 1024), 1)}


def run_corpus(files, work, args):
    inp, out, run = work / "in", work / "out", work / "run"
    inp.mkdir()
    for path in files:
        shutil.copyfile(path, inp / path.name)
    command = [sys.executable, str(ROOT / "main.py"), "--input", str(inp), "--output", str(out),
               "--work-dir", str(run), "--workers", "1"]
    if not any(path.suffix.lower() == ".pptx" for path in files):
        command.append("--skip-pptx")
    res: dict = {}
    res["cold_run"] = {
        **run_measured(command),
        "workers": 1, "cpu_count": os.cpu_count(), "platform": platform.platform(),
    }
    report_file = run / "run_report.json"
    if not report_file.is_file():
        return {**res, "error": "The pipeline did not produce a run report."}
    rep = json.loads(report_file.read_text(encoding="utf-8"))
    res["corpus"] = {"documents": rep["documents"], "succeeded": rep["succeeded"], "pages": rep["pages"], "ocr_pages": rep["ocr_pages"],
                     "decks": len(rep["amendments"]), "decks_valid": sum(1 for x in rep["amendments"] if x.get("valid")),
                     "xml_issues": {d["doc_id"]: d["validation"]["issues"] for d in rep["per_document"] if d.get("validation", {}).get("issues")}}
    if args.reference_xml or args.reference_pptx:
        document = next((row for row in rep["per_document"] if row["doc_id"] == args.source_pdf.stem), {})
        outputs = document.get("outputs", {})
        if args.reference_xml:
            from quality.evaluate import compare
            from tests.baseline_native import extract_pdf

            baseline = work / "baseline.xml"
            extract_pdf(args.source_pdf, baseline)
            gold = args.reference_xml.read_bytes()
            xml = outputs.get("xml")
            res["xml_vs_reference"] = {
                "baseline_native_text_layer": compare(baseline.read_bytes(), gold),
                "docsync": compare(Path(xml["path"]).read_bytes(), gold) if xml and not xml.get("draft") else None,
            }
        if args.reference_pptx:
            from quality.evaluate_pptx import score

            deck = outputs.get("pptx")
            paired = any(row.get("valid") and row.get("pdf") == args.source_pdf.name
                         and row.get("pptx") == args.source_pptx.name for row in rep["amendments"])
            res["deck_vs_reference"] = {
                "before_repair": score(args.source_pptx, args.reference_pptx),
                "after_repair": score(Path(deck["path"]), args.reference_pptx) if deck and paired else None,
            }
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", type=Path, required=True, help="Input directory containing PDFs and presentations")
    ap.add_argument("--out", type=Path, required=True, help="Path for a new benchmark report")
    ap.add_argument("--source-pdf", type=Path, help="Source PDF for optional reference comparisons")
    ap.add_argument("--source-pptx", type=Path, help="Source presentation for an optional reference comparison")
    ap.add_argument("--reference-xml", type=Path)
    ap.add_argument("--reference-pptx", type=Path)
    a = ap.parse_args()
    if a.out.exists() or not a.corpus.is_dir():
        ap.error("Choose an existing corpus directory and a new output path.")
    if (a.reference_xml or a.reference_pptx) and not a.source_pdf:
        ap.error("Reference comparisons require --source-pdf.")
    if a.reference_pptx and not a.source_pptx:
        ap.error("A presentation reference requires --source-pptx.")
    explicit = ((a.source_pdf, ".pdf"), (a.source_pptx, ".pptx"),
                (a.reference_xml, ".xml"), (a.reference_pptx, ".pptx"))
    for path, suffix in explicit:
        if path is not None and (not path.is_file() or path.suffix.lower() != suffix):
            ap.error(f"Expected an existing {suffix} file at {path}.")
    references = {path.resolve() for path in (a.reference_xml, a.reference_pptx) if path}
    files = {path.resolve() for path in a.corpus.rglob("*") if path.is_file()
             and path.suffix.lower() in {".pdf", ".pptx"}}
    files.update(path.resolve() for path in (a.source_pdf, a.source_pptx) if path)
    if any(path.resolve() in references for path in (a.source_pdf, a.source_pptx) if path):
        ap.error("Choose distinct source files and reference files.")
    files.difference_update(references)
    if not any(path.suffix.lower() == ".pdf" for path in files):
        ap.error("Provide at least one source PDF.")
    if len({path.name.casefold() for path in files}) != len(files):
        ap.error("The corpus contains duplicate input filenames.")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="benchmark-", dir=a.out.parent) as temp:
        res = run_corpus(sorted(files), Path(temp).resolve(), a)
    with a.out.open("x", encoding="utf-8") as stream:
        json.dump(res, stream, indent=2)
    print(json.dumps(res, indent=2))
    return res["cold_run"]["exit_code"] or int("error" in res)


if __name__ == "__main__":
    raise SystemExit(main())

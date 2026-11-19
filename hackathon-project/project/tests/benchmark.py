"""Reproducible benchmark. Only reports numbers it measured; reference data exists for ONE document (the example).

    python tests/benchmark.py --corpus "<Hackathon-Internal-Track>/Data" --out ../storage/windows-benchmark.json
"""
from __future__ import annotations
import argparse, glob, json, os, platform, shutil, subprocess, sys, tempfile, time
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--out", required=True, help="Path for a new benchmark report")
    a = ap.parse_args(); corpus = Path(a.corpus)
    if Path(a.out).exists():
        ap.error("the output report already exists; supply a new filename")
    ex = corpus / "Example Data Set" / "20239005532129"
    work = Path(tempfile.mkdtemp()); inp = work / "in"; out = work / "out"; inp.mkdir()
    for f in glob.glob(str(corpus / "Data Set" / "*" / "*.pdf")) + glob.glob(str(corpus / "Data Set" / "*" / "*.pptx")):
        shutil.copy(f, inp)
    shutil.copy(ex / "input" / "20239005532129.pdf", inp)
    for f in glob.glob(str(ex / "input" / "*.pptx")):
        shutil.copy(f, inp)
    res: dict = {}
    # --- cold run (no cache), 1 worker: the conservative number
    res["cold_run"] = {
        **run_measured([sys.executable, str(ROOT / "main.py"), "--input", str(inp), "--output", str(out), "--workers", "1"]),
        "workers": 1, "cpu_count": os.cpu_count(), "platform": platform.platform(),
    }
    rep = json.loads((work / "docsync_run" / "run_report.json").read_text(encoding="utf-8"))
    res["corpus"] = {"documents": rep["documents"], "succeeded": rep["succeeded"], "pages": rep["pages"], "ocr_pages": rep["ocr_pages"],
                     "decks": len(rep["amendments"]), "decks_valid": sum(1 for x in rep["amendments"] if x.get("valid")),
                     "xml_issues": {d["doc_id"]: d["validation"]["issues"] for d in rep["per_document"] if d.get("validation", {}).get("issues")}}
    # --- XML vs golden (example only): baseline vs DocuSync
    from quality.evaluate import compare
    gold = (ex / "output" / "20239005532129.xml").read_bytes()
    bdir = work / "base_out"; bdir.mkdir(); (work / "base_in").mkdir(); shutil.copy(ex / "input" / "20239005532129.pdf", work / "base_in")
    subprocess.run([sys.executable, str(ROOT / "tests" / "baseline_native.py"), "--input", str(work / "base_in"), "--output", str(bdir)], capture_output=True)
    base = next(iter(bdir.glob("*.xml")), None)
    res["xml_vs_golden_example"] = {"baseline_native_text_layer": compare(base.read_bytes(), gold) if base else None,
                                    "docsync": compare((out / "20239005532129.xml").read_bytes(), gold)}
    # --- deck repair vs reference deck (example only)
    from quality.evaluate_pptx import score
    ref = glob.glob(str(ex / "output" / "*.pptx"))[0]; errs = glob.glob(str(ex / "input" / "*.pptx"))[0]
    fixed = out / "20239005532129_transformed_QA.pptx"
    res["deck_vs_reference_example"] = {"before_repair": score(errs, ref), "after_repair": score(fixed, ref)}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()

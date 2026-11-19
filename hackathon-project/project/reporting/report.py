"""Run reports: machine-readable JSON + a small static HTML summary."""
from __future__ import annotations
import html, json, platform, time
from pathlib import Path


def write_reports(work_dir: Path, results: list[dict], amend: list[dict], settings, timing: dict) -> dict:
    work_dir.mkdir(parents=True, exist_ok=True)
    ok = [r for r in results if r.get("ok")]
    summary = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"), "platform": platform.platform(), "python": platform.python_version(),
        "documents": len(results), "succeeded": len(ok), "failed": len(results) - len(ok),
        "pages": sum(r.get("pages", 0) for r in results), "ocr_pages": sum(r.get("ocr_pages", 0) for r in results),
        "wall_seconds": round(timing.get("wall", 0), 2), "settings": settings.to_dict(),
        "per_document": results, "amendments": amend,
        "note": "Confidence values are heuristics; they are not benchmark accuracy.",
    }
    (work_dir / "run_report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    rows = "".join(
        f"<tr><td>{html.escape(r.get('file',''))}</td><td>{r.get('pages',0)}</td><td>{r.get('ocr_pages',0)}</td><td>{r.get('lang','')}</td>"
        f"<td>{r.get('seconds','')}</td><td>{r.get('quality',{}).get('mean_page_confidence','')}</td>"
        f"<td>{html.escape('; '.join(r.get('validation',{}).get('issues',[])) or r.get('error','') or 'ok')}</td></tr>" for r in results)
    arows = "".join(
        f"<tr><td>{html.escape(a.get('pptx',''))}</td><td>{a.get('slides','')}</td><td>{a.get('applied',0)}</td><td>{a.get('review',0)}</td><td>{html.escape(a.get('output') or '')}</td><td>{html.escape(a.get('error') or a.get('status',''))}</td></tr>" for a in amend)
    page = f"""<!doctype html><meta charset=utf-8><title>DocuSync AI run report</title>
<style>body{{font:14px system-ui;margin:2rem;background:#0b1220;color:#dbe4f3}}table{{border-collapse:collapse;width:100%;margin:1rem 0}}
td,th{{border-bottom:1px solid #22304a;padding:.45rem .6rem;text-align:left}}th{{color:#7fa6ff}}h1,h2{{font-weight:600}}</style>
<h1>DocuSync AI - run report</h1><p>{summary['succeeded']}/{summary['documents']} documents, {summary['pages']} pages ({summary['ocr_pages']} OCR), {summary['wall_seconds']} s wall clock.</p>
<h2>Documents</h2><table><tr><th>File<th>Pages<th>OCR pages<th>Lang<th>Seconds<th>Mean confidence (heuristic)<th>Validation</tr>{rows}</table>
<h2>Presentation amendments</h2><table><tr><th>Deck<th>Slides<th>Auto-applied<th>Flagged for review<th>Output<th>Status</tr>{arows}</table>"""
    (work_dir / "report.html").write_text(page, encoding="utf-8")
    return summary

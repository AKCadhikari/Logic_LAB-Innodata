"""DocuSync review dashboard (Flask, fully offline).

    python ui/server.py --run-dir ../phase-1/docsync_run --input ../phase-1/input --output ../phase-1/output
"""
from __future__ import annotations
import argparse, json, sys, threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from flask import Flask, abort, jsonify, request, send_file, send_from_directory, Response

app = Flask(__name__, static_folder=str(HERE / "static"))
CFG: dict = {}
_img_cache: dict = {}
_lock = threading.Lock()


def _ids() -> list[str]:
    d = CFG["run"] / "ir"
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def _ir(doc_id: str) -> dict:
    if doc_id not in _ids():
        abort(404)
    return json.loads((CFG["run"] / "ir" / f"{doc_id}.json").read_text(encoding="utf-8"))


def _pdf_path(doc_id: str, ir: dict) -> Path:
    p = Path(ir.get("path", ""))
    if p.exists():
        return p
    cand = list(CFG["input"].rglob(f"{doc_id}.pdf"))
    if cand:
        return cand[0]
    abort(404)


def _report() -> dict:
    p = CFG["run"] / "run_report.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _changes_for(doc_id: str) -> list[dict]:
    out = []
    d = CFG["run"] / "changes"
    if d.exists():
        for f in sorted(d.glob("*.json")):
            ch = json.loads(f.read_text(encoding="utf-8"))
            if ch and ch[0].get("document") == doc_id:
                out += [{**c, "id": f"{f.stem}:{i}"} for i, c in enumerate(ch)]
    return out


def _reviews() -> dict:
    p = CFG["run"] / "review_decisions.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/summary")
def summary():
    rep = _report()
    docs = rep.get("per_document", [])
    return jsonify({"documents": rep.get("documents", len(_ids())), "succeeded": rep.get("succeeded"), "failed": rep.get("failed"),
                    "pages": rep.get("pages"), "ocr_pages": rep.get("ocr_pages"), "wall_seconds": rep.get("wall_seconds"),
                    "amendments": rep.get("amendments", []),
                    "mean_confidence": round(sum(d.get("quality", {}).get("mean_page_confidence", 0) for d in docs) / max(len(docs), 1), 3),
                    "docs": [{"id": d["doc_id"], "file": d.get("file"), "pages": d.get("pages"), "ocr_pages": d.get("ocr_pages", 0), "lang": d.get("lang"),
                              "seconds": d.get("seconds"), "conf": d.get("quality", {}).get("mean_page_confidence"), "ok": d.get("ok"),
                              "issues": d.get("validation", {}).get("issues", []), "changes": len(_changes_for(d["doc_id"]))} for d in docs]})


@app.get("/api/doc/<doc_id>")
def doc(doc_id):
    ir = _ir(doc_id)
    def blk(b, i):
        txt = b.get("text") or " ".join(c["text"] for c in b.get("children", [])) or " | ".join(c["text"] for c in b.get("cells", [])[:12]) or b.get("caption", "") or b.get("alt", "")
        return {"i": i, "tag": b["tag"], "kind": b.get("kind", ""), "bbox": b.get("bbox"), "text": txt[:600], "conf": b.get("conf", 100.0), "lang": b.get("lang", ""),
                "caption": b.get("caption", ""), "n_items": len(b.get("children", [])) or len(b.get("cells", []))}
    pages = [{"n": p["n"], "w": p["width"], "h": p["height"], "source": p["source"], "diag": {k: p["diag"].get(k) for k in ("health", "decision", "reasons", "ocr", "confidence", "compare_similarity", "native_chars", "image_coverage", "scripts") if k in p["diag"]},
              "blocks": [blk(b, i) for i, b in enumerate(p["blocks"])]} for p in ir["pages"]]
    title = next((b["text"] for p in ir["pages"] for b in p["blocks"] if b.get("kind") == "title"), "")
    rep = next((d for d in _report().get("per_document", []) if d["doc_id"] == doc_id), {})
    return jsonify({"id": doc_id, "lang": ir["lang"], "title": title, "pages": pages, "validation": rep.get("validation", {}), "quality": rep.get("quality", {}), "seconds": rep.get("seconds")})


@app.get("/api/doc/<doc_id>/page/<int:n>.png")
def page_png(doc_id, n):
    ir = _ir(doc_id)
    dpi = min(max(int(request.args.get("dpi", 110)), 40), 220)
    rot = int(request.args.get("rot", 0)) % 360
    key = (doc_id, n, dpi, rot)
    with _lock:
        if key not in _img_cache:
            import pymupdf
            d = pymupdf.open(str(_pdf_path(doc_id, ir)))
            if not 1 <= n <= d.page_count:
                abort(404)
            pg = d[n - 1]
            if rot:
                pg.set_rotation((pg.rotation + rot) % 360)
            _img_cache[key] = pg.get_pixmap(dpi=dpi).tobytes("png"); d.close()
            if len(_img_cache) > 80:
                _img_cache.pop(next(iter(_img_cache)))
    return Response(_img_cache[key], mimetype="image/png")


@app.get("/api/doc/<doc_id>/xml")
def xml(doc_id):
    _ir(doc_id)
    p = CFG["output"] / f"{doc_id}.xml"
    if not p.exists():
        abort(404)
    if request.args.get("download"):
        return send_file(p, as_attachment=True, download_name=p.name)
    return Response(p.read_text(encoding="utf-8"), mimetype="text/plain; charset=utf-8")


@app.get("/api/doc/<doc_id>/changes")
def changes(doc_id):
    _ir(doc_id)
    rv = _reviews()
    return jsonify([{**c, "decision": rv.get(c["id"], "")} for c in _changes_for(doc_id)])


@app.post("/api/review")
def review():
    body = request.get_json(force=True)
    cid, dec = str(body.get("id", "")), body.get("decision", "")
    if dec not in ("", "accepted", "rejected") or ":" not in cid:
        abort(400)
    rv = _reviews(); rv[cid] = dec
    (CFG["run"] / "review_decisions.json").write_text(json.dumps(rv, indent=1), encoding="utf-8")
    return jsonify({"ok": True})


@app.get("/api/doc/<doc_id>/deck")
def deck(doc_id):
    for c in _changes_for(doc_id):
        stem = c["id"].split(":")[0]
        from utils.naming import pptx_out_name
        for pp in list(CFG["input"].rglob(f"{stem}.pptx")):
            out = CFG["output"] / pptx_out_name(pp)
            if out.exists():
                return send_file(out, as_attachment=True, download_name=out.name)
    abort(404)


@app.post("/api/reprocess/<doc_id>")
def reprocess(doc_id):
    ir = _ir(doc_id)
    from config.settings import Settings
    from agents.orchestrator import process_pdf
    from utils.logs import AuditLog
    res = process_pdf(_pdf_path(doc_id, ir), Settings.load(), CFG["output"], CFG["run"], AuditLog(CFG["run"] / "audit.jsonl"))
    with _lock:
        _img_cache.clear()
    return jsonify({"ok": res.get("ok"), "seconds": res.get("seconds")})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default=str(HERE.parents[1] / "phase-1" / "docsync_run"))
    ap.add_argument("--input", default=str(HERE.parents[1] / "phase-1" / "input"))
    ap.add_argument("--output", default=str(HERE.parents[1] / "phase-1" / "output"))
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    CFG.update(run=Path(a.run_dir).resolve(), input=Path(a.input).resolve(), output=Path(a.output).resolve())
    print(f"DocuSync review UI on http://127.0.0.1:{a.port}  (run dir: {CFG['run']})")
    app.run(host="127.0.0.1", port=a.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

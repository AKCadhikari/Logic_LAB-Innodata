"""DocuSync review dashboard (Flask, fully offline).

    python ui/server.py --run-dir ../phase-1/docsync_run --input ../phase-1/input --output ../phase-1/output
"""
from __future__ import annotations
import argparse, json, sys, threading
from hmac import compare_digest
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from flask import Flask, abort, g, jsonify, request, send_file, send_from_directory, Response

app = Flask(__name__, static_folder=str(HERE / "static"))
CFG: dict = {}
_img_cache: dict = {}
_lock = threading.Lock()


@app.before_request
def reject_cross_site_writes():
    origin = request.headers.get("Origin")
    if request.path.startswith("/api/") and request.headers.get("Sec-Fetch-Site") == "cross-site":
        return jsonify(error="Open DocuSync directly to access the workspace."), 403
    if (request.method in ("POST", "PATCH", "PUT", "DELETE") and origin
            and origin.rstrip("/") != request.host_url.rstrip("/")):
        return jsonify(error="Open DocuSync directly to change the workspace."), 403
    if request.path in {"/api/summary", "/api/review"} or request.path.startswith(("/api/doc/", "/api/reprocess/")):
        from ui.workspace_routes import browser_owner

        workspace = app.extensions.get("workspace")
        if workspace is None or not workspace.owns_batch(browser_owner()):
            return jsonify(error="Batch review belongs to the local workspace's original browser session."), 404


@app.before_request
def track_managed_requests():
    lifecycle = app.extensions.get("launcher_lifecycle")
    if lifecycle is not None and request.path != "/api/startup/shutdown":
        with lifecycle["lock"]:
            if lifecycle["draining"]:
                return jsonify(error="DocuSync is shutting down."), 503
            lifecycle["active"] += 1
            g.managed_request = True


@app.teardown_request
def release_managed_request(_error):
    if g.pop("managed_request", False):
        lifecycle = app.extensions["launcher_lifecycle"]
        with lifecycle["lock"]:
            lifecycle["active"] -= 1


@app.get("/api/startup/identity")
def startup_identity():
    from startup import settings_id, workspace_id

    ws = app.extensions.get("workspace")
    if ws is None:
        return jsonify(backend_ready=False), 503
    return jsonify(backend_ready=True, workspace=workspace_id(ws.root), settings=settings_id(ws.settings), pid=os.getpid())


@app.post("/api/startup/shutdown")
def managed_shutdown():
    lifecycle = app.extensions.get("launcher_lifecycle")
    key = request.headers.get("X-DocuSync-Launcher-Key", "")
    if not lifecycle or not compare_digest(key.encode(), lifecycle["key"].encode()):
        abort(403)
    with lifecycle["lock"]:
        ws = app.extensions["workspace"]
        with ws.db() as db:
            busy = db.execute("SELECT 1 FROM jobs WHERE status IN ('queued', 'running') LIMIT 1").fetchone()
        retry = app.extensions.get("startup_retry")
        if lifecycle["active"] or busy or (retry and retry.is_alive()):
            return jsonify(error="Finish active workspace requests and processing before shutdown."), 409
        lifecycle["draining"] = True
    response = jsonify(status="stopping")
    response.call_on_close(lambda: threading.Thread(target=lifecycle["server"].shutdown, daemon=True).start())
    return response


def _ids() -> list[str]:
    d = CFG["run"] / "ir"
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def _ir(doc_id: str) -> dict:
    if doc_id not in _ids():
        abort(404)
    return json.loads((CFG["run"] / "ir" / f"{doc_id}.json").read_text(encoding="utf-8"))


def _pdf_path(ir: dict) -> Path:
    p = Path(ir.get("path", ""))
    if p.is_file():
        return p
    abort(404)


def _report() -> dict:
    p = CFG["run"] / "run_report.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _document_report(doc_id: str) -> dict:
    return next((entry for entry in _report().get("per_document", []) if entry["doc_id"] == doc_id), {})


def _batch_file(report: dict, kind: str) -> Path | None:
    item = report.get("outputs", {}).get(kind)
    if item and item["valid"]:
        path = Path(item["path"]).resolve()
        if path.is_relative_to(CFG["output"].resolve()) and path.is_file():
            return path
    return None


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
    rep = _document_report(doc_id)
    return jsonify({"id": doc_id, "lang": ir["lang"], "title": title, "pages": pages, "validation": rep.get("validation", {}),
                    "quality": rep.get("quality", {}), "seconds": rep.get("seconds"), "outputs": rep.get("outputs"),
                    "output_folder": rep.get("output_folder"), "status": rep.get("status"), "error": rep.get("error")})


@app.get("/api/doc/<doc_id>/page/<int:n>.png")
def page_png(doc_id, n):
    ir = _ir(doc_id)
    return render_page(_pdf_path(ir), n)


@app.get("/api/documents/<document_id>/page/<int:n>.png")
def workspace_page(document_id, n):
    from ui.workspace_routes import browser_owner

    workspace = app.extensions.get("workspace")
    if workspace is None or not workspace.owns(document_id, browser_owner()):
        abort(404)
    return render_page(workspace.source(document_id), n)


def render_page(path: Path, n: int):
    dpi = min(max(request.args.get("dpi", 110, type=int), 40), 220)
    rot = request.args.get("rot", 0, type=int) % 360
    if rot % 90:
        abort(400, "Rotation must be a multiple of 90 degrees.")
    key = (path, n, dpi, rot)
    with _lock:
        if key not in _img_cache:
            import pymupdf
            try:
                with pymupdf.open(str(path)) as d:
                    if d.needs_pass:
                        abort(422, "This PDF requires a password.")
                    if not 1 <= n <= d.page_count:
                        abort(404)
                    pg = d[n - 1]
                    if rot:
                        pg.set_rotation((pg.rotation + rot) % 360)
                    if pg.rect.width * pg.rect.height * (dpi / 72) ** 2 > 20_000_000:
                        abort(422, "The page is too large to preview at this scale.")
                    _img_cache[key] = pg.get_pixmap(dpi=dpi).tobytes("png")
            except (pymupdf.FileDataError, OSError):
                abort(422, "This PDF cannot be rendered.")
            if len(_img_cache) > 24:
                _img_cache.pop(next(iter(_img_cache)))
        data = _img_cache[key]
    return Response(data, mimetype="image/png")


@app.get("/api/doc/<doc_id>/xml")
def xml(doc_id):
    _ir(doc_id)
    report = _document_report(doc_id)
    p = _batch_file(report, "xml") if "outputs" in report else CFG["output"] / f"{doc_id}.xml"
    if not p or not p.is_file():
        abort(404)
    if request.args.get("download"):
        from ui.workspace_routes import download_saved_xml
        return download_saved_xml(p)
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
    _ir(doc_id)
    report = _document_report(doc_id)
    if "outputs" in report:
        if path := _batch_file(report, "pptx"):
            return send_file(path, as_attachment=True, download_name=path.name)
        abort(404)
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
    from tempfile import TemporaryDirectory

    from config.settings import Settings
    from agents.orchestrator import process_pdf
    from ingestion.discover import discover
    from main import publish_batch_document
    from reporting.report import write_reports
    from utils.logs import AuditLog
    from comparison.pairing import match_presentations

    with _lock:
        from time import monotonic

        started = monotonic()
        pdf = _pdf_path(ir).resolve()
        prior = _report()
        current = _document_report(doc_id)
        settings = Settings(**prior["settings"]) if prior.get("settings") else Settings.load()
        audit = AuditLog(CFG["run"] / "audit.jsonl")
        inputs = discover(CFG["input"])
        with TemporaryDirectory(prefix="extract-", dir=CFG["run"]) as staging:
            extraction = process_pdf(pdf, settings, Path(staging), CFG["run"], audit)
        decks = [deck for source, deck in match_presentations(inputs["pdfs"], inputs["pptxs"], CFG["run"])
                 if source.resolve() == pdf]
        res, amendment = publish_batch_document(pdf, extraction, decks, CFG["output"], CFG["run"], settings, audit,
                                                xml_only=current.get("status") == "xml_only")
        documents = [entry for entry in prior.get("per_document", []) if entry["doc_id"] != doc_id] + [res]
        amendments = [entry for entry in prior.get("amendments", []) if entry.get("pdf") != pdf.name]
        if amendment:
            amendments.append(amendment)
        write_reports(CFG["run"], documents, amendments, settings, {"wall": monotonic() - started})
        _img_cache.clear()
    return jsonify(res)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default=str(HERE.parents[1] / "phase-1" / "docsync_run"))
    ap.add_argument("--input", default=str(HERE.parents[1] / "phase-1" / "input"))
    ap.add_argument("--output", default=str(HERE.parents[1] / "phase-1" / "output"))
    ap.add_argument("--storage", default=str(HERE.parents[1] / "storage"))
    ap.add_argument("--config", help="Path to local document processing settings")
    ap.add_argument("--startup-logs", type=Path, default=HERE.parents[1] / "logs")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    CFG.update(run=Path(a.run_dir).resolve(), input=Path(a.input).resolve(), output=Path(a.output).resolve())
    configure_workspace(Path(a.storage).resolve(), a.config, CFG["output"])
    app.extensions["startup_logs"] = a.startup_logs.resolve()
    app.extensions["startup_port"] = a.port
    print(f"DocuSync review UI on http://127.0.0.1:{a.port}  (run dir: {CFG['run']})")
    key = os.environ.pop("DOCUSYNC_LAUNCHER_KEY", "")
    if not key:
        app.run(host="127.0.0.1", port=a.port, debug=False, threaded=True)
        return
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", a.port, app, threaded=True)
    app.extensions["launcher_lifecycle"] = {"server": server, "key": key, "lock": threading.Lock(),
                                            "active": 0, "draining": False}
    try:
        server.serve_forever()
    finally:
        server.server_close()
        app.extensions["workspace"].worker.shutdown(wait=True)


def configure_workspace(storage: Path, config: str | None = None, output: Path | None = None):
    from config.settings import Settings
    from ui.workspace import Workspace
    from ui.workspace_routes import api

    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
    app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost", "[::1]"]
    app.extensions["workspace"] = Workspace(storage, Settings.load(config, default_schema_profile="sample"), output)
    if api.name not in app.blueprints:
        app.register_blueprint(api)


if __name__ == "__main__":
    main()

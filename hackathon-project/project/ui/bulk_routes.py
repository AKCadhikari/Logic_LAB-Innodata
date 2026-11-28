"""Bulk intake, queue and validated downloads for the local workspace."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
from tempfile import SpooledTemporaryFile
from zipfile import ZipFile

from flask import Blueprint, current_app, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from ui.bulk import MAX_FILE_BYTES, pairing
from ui.workspace_routes import (browser_owner, download_saved_xml, export_file, fail, http_error,
                                 requested_ocr, set_browser_cookie, validate_presentation_upload, workspace)
from xml_engine.assets import relative_path

bulk_api = Blueprint("bulk", __name__, url_prefix="/api/bulk")
bulk_api.after_request(set_browser_cookie)
bulk_api.register_error_handler(HTTPException, http_error)


def validate_file(path, kind):
    import pymupdf
    from lxml.etree import XMLSyntaxError

    if kind == "pdf":
        with path.open("rb") as stream:
            is_pdf = stream.read(5) == b"%PDF-"
        if is_pdf:
            try:
                with pymupdf.open(path) as document:
                    if document.is_pdf and (document.needs_pass or document.page_count):
                        return None
            except (RuntimeError, ValueError):
                pass
        return "This file is not a readable PDF."
    try:
        with path.open("rb") as stream:
            if stream.read(2) == b"PK":
                stream.seek(0)
                return validate_presentation_upload(stream)
    except (XMLSyntaxError, RuntimeError, NotImplementedError):
        pass
    return "This file is not a valid PowerPoint presentation."


@bulk_api.get("/batches")
def batches():
    return jsonify(batches=workspace().bulk.batches(browser_owner(create=True)))


@bulk_api.post("/batches")
def create_batch():
    request.max_content_length = 4 * 1024 * 1024
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not re.fullmatch("[a-f0-9]{32}", body["id"]):
        return fail("Select a valid folder collection.", 400)
    phase = body.get("phase", "1")
    if phase not in ("1", "2"):
        return fail("Choose Phase 1 or Phase 2.", 400)
    worker = workspace().bulk
    owner = browser_owner(create=True)
    engine, language = body.get("ocr_engine"), body.get("ocr_language")
    if engine is not None and not isinstance(engine, str):
        return fail("Choose a supported OCR engine.", 400)
    try:
        engine, language = requested_ocr(engine, language)
        if not worker.create(body["id"], owner, phase, engine, language, body.get("folders")):
            return fail("Batch not found.", 404)
    except ValueError as error:
        return fail(str(error), 400)
    return jsonify(batch_view(body["id"], owner)[0]), 201


@bulk_api.put("/batches/<batch_id>/files/<file_id>")
def upload(batch_id, file_id):
    request.max_content_length = MAX_FILE_BYTES + 1024 * 1024
    files = request.files.getlist("file")
    if len(files) != 1 or len(request.files) != 1:
        return fail("Upload one selected file at a time.", 400)
    try:
        status = workspace().bulk.save_file(batch_id, file_id, browser_owner(), files[0].stream, validate_file)
    except OSError:
        current_app.logger.exception("Could not save a file for bulk processing")
        return fail("The selected file could not be saved. Check available disk space and try again.", 500)
    return jsonify(status=status) if status else fail("File not found.", 404)


@bulk_api.post("/batches/<batch_id>/start")
def start(batch_id):
    try:
        result = workspace().bulk.start(batch_id, browser_owner())
    except ValueError as error:
        return fail(str(error), 409)
    return (jsonify(started=True), 202) if result else fail("Batch not found.", 404)


@bulk_api.post("/batches/<batch_id>/cancel")
@bulk_api.post("/batches/<batch_id>/jobs/<job_id>/cancel")
def cancel(batch_id, job_id=None):
    worker = workspace().bulk
    owner = browser_owner()
    if job_id and not worker.owns_job(batch_id, job_id, owner):
        return fail("Folder not found.", 404)
    count = worker.cancel(batch_id, owner, job_id)
    return jsonify(canceled=count) if count is not None else fail("Batch not found.", 404)


@bulk_api.post("/batches/<batch_id>/jobs/<job_id>/retry")
def retry(batch_id, job_id):
    worker = workspace().bulk
    if not worker.owns_job(batch_id, job_id, browser_owner()):
        return fail("Folder not found.", 404)
    if worker.retry(job_id):
        return jsonify(queued=True), 202
    public, _ = batch_view(batch_id, browser_owner())
    repair_missing = any(job["id"] == job_id and job["can_retry"] for job in public["jobs"])
    if not repair_missing or not worker.retry(job_id, missing_outputs=True):
        return fail("This job cannot be retried in its current state.", 409)
    return jsonify(queued=True), 202


def validated_exports(root, attempts):
    """Find the newest published, finalized artifacts, keeping a prior valid version if needed."""
    root = root.resolve()
    for attempt in attempts:
        folder = Path(attempt["folder"]).resolve()
        if folder.parent != root:
            continue
        saved = json.loads(attempt["outputs"])
        outputs = {}
        for kind in ("xml", "pptx"):
            item = saved.get(kind)
            if not item or not item["valid"] or item.get("draft"):
                continue
            target = export_file(folder, item["filename"])
            try:
                if not target or target.stat().st_size != item["size_bytes"]:
                    continue
            except OSError:
                continue
            related = json.loads(attempt["assets"]) if kind == "xml" else []
            assets = {part: export_file(folder, part) for part in related if relative_path(part)}
            if len(assets) == len(related) and all(assets.values()):
                outputs[kind] = (target, assets)
        if outputs:
            yield outputs


def job_status(batch, row, issues, pending, outputs):
    if not batch["started"]:
        return "checking" if pending else "needs_review" if issues else "ready"
    if issues:
        return "needs_review"
    if row["pending"] or row["active"] and row["attempt_status"] == "queued":
        return "queued"
    if row["attempt_status"] == "running":
        return "processing"
    if row["canceled"]:
        return "canceled"
    if row["attempt_status"] == "review" and not row["error"]:
        return "source_review"
    if not row["error"] and row["attempt_status"] == "completed" and len(outputs) == 2:
        return "completed"
    return "partial" if outputs else "failed"


def batch_view(batch_id, owner):
    ws = workspace()
    snapshot = ws.bulk.snapshot(batch_id, owner)
    if not snapshot:
        return None
    batch, rows, all_files, exports = snapshot
    summary, counts = Counter(), Counter()
    jobs, artifacts = [], {}
    for row in rows:
        key = row["id"]
        files = all_files[key]
        groups, issues, pending = pairing(row, files)
        summary.update({"folders": 1, "complete_pairs": int(not pending and not issues),
                        "missing_pdf": int(not groups["pdf"]), "missing_pptx": int(not groups["pptx"]),
                        "review": int(bool(issues))})
        for file in files:
            summary.update({file["kind"] if file["status"] == "valid" else file["status"]: 1})
        previous = exports.get(key)
        outputs = {}
        if previous:
            root = ws.document_export_path(ws.output, batch["phase"], row["source_pdf"], row["bulk_path"])
            for candidate in validated_exports(root, previous):
                if len(candidate) == 2:
                    outputs = candidate
                    break
                if not outputs:
                    outputs = candidate
        status = job_status(batch, row, issues, pending, outputs)
        counts[status] += 1
        base = f"/api/bulk/batches/{batch_id}/jobs/{key}"
        error = row["error"] or row["export_error"]
        if status in {"failed", "partial"} and not error:
            if row["attempt_status"] == "completed":
                error = "One or more saved outputs are missing or have changed."
            elif row["attempt_error"] in {"PDF is encrypted and requires a password.",
                                         "The PDF could not be processed. Check that the original opens.",
                                         "One or more PDF pages could not be read.",
                                         "The extracted document failed XML validation.",
                                         "The processing worker stopped. Restart the workspace and retry.",
                                         "Processing was interrupted. Retry to start a new attempt."}:
                error = row["attempt_error"]
            else:
                error = "The processing attempt failed. Check the source files and retry."
        jobs.append({"id": key, "folder": row["folder"], "status": status, "issues": issues,
                     "files": [{k: file[k] for k in ("id", "name", "kind", "status", "error")} for file in files],
                     "pdf": [file["name"] for file in groups["pdf"]],
                     "pptx": [file["name"] for file in groups["pptx"]],
                     "stage": row["stage"] if row["attempt_status"] != "queued" else None,
                     "pages": row["done"], "page_count": row["total"],
                     "slide": row["slide"], "slide_count": row["slides"],
                     "started": row["started"], "finished": row["finished"] or row["attempt_finished"],
                     "error": error, "warnings": row["warnings"] or 0,
                     "view": f"/?job={key}" if row["bulk_path"] else None,
                     "downloads": {kind: f"{base}/download/{kind}" for kind in outputs},
                     "can_retry": status in {"failed", "source_review", "canceled", "partial"}})
        if outputs:
            artifacts[key] = outputs
    count_keys = ("queued", "processing", "completed", "failed", "partial", "canceled", "source_review")
    summary_keys = ("folders", "pdf", "pptx", "complete_pairs", "missing_pdf", "missing_pptx", "invalid", "review", "pending")
    public = {**batch, "summary": {key: summary[key] for key in summary_keys},
              "counts": {"total": len(rows), **{key: counts[key] for key in count_keys}},
              "jobs": jobs, "download": f"/api/bulk/batches/{batch_id}/download" if artifacts else None}
    return public, artifacts


@bulk_api.get("/batches/<batch_id>")
def batch(batch_id):
    found = batch_view(batch_id, browser_owner())
    return jsonify(found[0]) if found else fail("Batch not found.", 404)


@bulk_api.get("/batches/<batch_id>/jobs/<job_id>/download/<kind>")
def download(batch_id, job_id, kind):
    found = batch_view(batch_id, browser_owner())
    item = found[1].get(job_id, {}).get(kind) if found else None
    if not item:
        return fail("The validated output is not available.", 404)
    path, _ = item
    return download_saved_xml(path) if kind == "xml" else send_file(path, as_attachment=True)


@bulk_api.get("/batches/<batch_id>/download")
def download_all(batch_id):
    found = batch_view(batch_id, browser_owner())
    if not found:
        return fail("Batch not found.", 404)
    public, artifacts = found
    if not artifacts:
        return fail("There are no validated results to download yet.", 409)
    archive = SpooledTemporaryFile(max_size=1024 * 1024)
    try:
        with ZipFile(archive, "w") as zip_file:
            manifest = []
            names = {job["folder"].casefold() for job in public["jobs"] if job["id"] in artifacts}
            report_name = "bulk-report.json"
            number = 1
            while report_name in names:
                number += 1
                report_name = f"bulk-report-{number}.json"
            for job in public["jobs"]:
                saved = []
                for kind, (path, assets) in artifacts.get(job["id"], {}).items():
                    target = f"{job['folder']}/{path.name}"
                    zip_file.write(path, target)
                    saved.append({"kind": kind, "path": target})
                    for relative, asset in assets.items():
                        zip_file.write(asset, f"{job['folder']}/{relative}")
                manifest.append({key: job[key] for key in
                                 ("id", "folder", "pdf", "pptx", "files", "status", "issues", "error", "warnings",
                                  "started", "finished")} | {"outputs": saved})
            zip_file.writestr(report_name, json.dumps({"batch": batch_id, "phase": public["phase"],
                                                      "jobs": manifest}, ensure_ascii=False, indent=2))
        archive.seek(0)
    except (OSError, ValueError):
        archive.close()
        current_app.logger.exception("Could not package the bulk results")
        return fail("The bulk ZIP could not be created. Check available disk space and retry.", 500)
    return send_file(archive, mimetype="application/zip", as_attachment=True, download_name="Bulk_Results.zip")

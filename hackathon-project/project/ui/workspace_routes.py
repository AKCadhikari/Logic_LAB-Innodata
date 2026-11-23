"""HTTP routes for the single-document workspace."""
from __future__ import annotations

import io
import json
import re
from dataclasses import replace
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from secrets import token_hex
from tempfile import SpooledTemporaryFile, TemporaryDirectory
from uuid import UUID, uuid4

from flask import Blueprint, Response, current_app, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from comparison.differ import compare_ir
from layout.model import DocIR
from reporting.exports import output_stem, publish_document, write_zip
from ui.workspace import Workspace, now
from utils.textutil import xml_safe

api = Blueprint("workspace", __name__, url_prefix="/api")
SESS_COOKIE = "docusync_workspace"


def workspace() -> Workspace:
    return current_app.extensions["workspace"]


def fail(message: str, status: int):
    return jsonify(error=message), status


def browser_owner(*, create: bool = False) -> str | None:
    token = g.get("workspace_token") or request.cookies.get(SESS_COOKIE, "")
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        if not create:
            return None
        token = g.workspace_token = token_hex(32)
    return sha256(token.encode()).hexdigest()


@api.before_request
def authorize_document():
    document_id = (request.view_args or {}).get("document_id")
    if document_id is not None and not workspace().owns(document_id, browser_owner()):
        return fail("Document not found.", 404)


@api.after_request
def set_browser_cookie(response):
    if token := g.get("workspace_token"):
        response.set_cookie(SESS_COOKIE, token, httponly=True, samesite="Strict", max_age=365 * 24 * 3600)
    response.cache_control.no_store = True
    return response


@api.errorhandler(HTTPException)
def http_error(error: HTTPException):
    return fail(error.description, error.code)


def upload_file(field: str, extension: str, magic: bytes | None = None):
    files = request.files.getlist(field)
    if len(files) != 1 or sum(len(values) for _, values in request.files.lists()) != 1:
        return None, "Select exactly one file."
    file = files[0]
    filename = PureWindowsPath(file.filename or "").name
    if (Path(filename).suffix.lower() != extension or len(filename) > 255
            or any(ord(char) < 32 for char in filename)):
        return None, f"Select a valid {extension} file."
    if magic is not None and file.stream.read(len(magic)) != magic:
        return None, f"The file does not have a valid {extension} header."
    file.stream.seek(0)
    return (filename, file.stream), None


@api.get("/health")
def health():
    from ocr.engine import available_langs
    from comparison.ollama import model_info as qwen_info
    from ocr import glm, paddle
    from layout.docling_adapter import readiness as layout_readiness
    ws = workspace()
    glm_info, qwen = glm.model_info(ws.settings), qwen_info(ws.settings)
    return jsonify(status="ok", runtime="local CPU", ocr_enabled=ws.settings.ocr_enabled,
                   ocr_languages=sorted(available_langs(ws.settings)), xml_profile=ws.settings.schema_profile,
                   ollama="connected" if glm_info["connected"] or qwen["connected"] else "unavailable",
                   ocr_backend=ws.settings.ocr_backend, qwen={**qwen, "enabled": ws.settings.ollama_enabled},
                   glm_ocr=glm_info, paddle_ocr=paddle.readiness(ws.settings),
                   docling=layout_readiness(ws.settings))


@api.get("/documents")
def documents():
    return jsonify(workspace().documents(browser_owner(create=True)))


@api.route("/profile", methods=["GET", "PATCH"])
def profile():
    ws = workspace()
    if request.method == "GET":
        return jsonify(name=ws.reviewer())
    body = request.get_json(silent=True)
    name = body.get("name") if isinstance(body, dict) else None
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(char) < 32 for char in name):
        return fail("Enter a reviewer name of up to 80 characters.", 400)
    return jsonify(name=ws.reviewer(name.strip()))


@api.post("/documents")
def create_document():
    uploaded, error = upload_file("pdf", ".pdf", b"%PDF-")
    if error:
        return fail(error, 400)
    ws = workspace()
    previous = request.form.get("previous_id") or None
    if previous and not ws.owns(previous, browser_owner()):
        return fail("The previous document version does not exist.", 400)
    filename, stream = uploaded
    document_id = ws.upload(filename, stream, previous, browser_owner(create=True))
    return jsonify(ws.get(document_id)), 202


def view(ir: DocIR) -> dict:
    pages = []
    title = None
    warnings = []
    for page in ir.pages:
        # The OCR engine can correct image rotation and skew. Those coordinates
        # need an inverse transform before they can be overlaid on the source.
        ocr = page.diag.get("ocr", {})
        aligned = not page.diag.get("rotation") and not (page.source == "ocr" and (ocr.get("rotate") or ocr.get("skew")))
        if not aligned:
            warnings.append(f"Page {page.n}: source overlays are unavailable for this rotation or skew.")
        if page.requires_review or page.diag.get("error") or (page.diag.get("decision") == "ocr" and not page.blocks):
            warnings.append(f"Page {page.n}: review the source. " + " ".join(page.diag.get("review_reasons", []) or
                                                                         page.diag.get("reasons", [])))
        blocks = []
        for index, block in enumerate(page.blocks):
            text = block.text or "\n".join(child.text for child in block.children)
            blocks.append({"i": index, "tag": block.tag, "kind": block.kind, "text": text,
                           "bbox": block.bbox if aligned else None, "source": block.source,
                           "conf": block.recognition_confidence if block.ocr_engine else block.conf,
                           "ocr_engine": block.ocr_engine, "lang": block.lang, "caption": block.caption, "alt": block.alt,
                           "cells": [vars(cell) for cell in block.cells],
                           "n_items": len(block.children) or len(block.cells)})
            if block.kind == "title" and title is None:
                title = {"value": block.text, "page": page.n, "bbox": block.bbox if aligned else None,
                         "confidence": None if block.ocr_engine == "glm-ocr" else block.conf}
        diag = {key: page.diag[key] for key in ("health", "decision", "reasons", "ocr", "confidence", "rotation",
                                              "layout", "native_chars", "has_native_text", "short_native_text",
                                              "appears_scanned", "has_images", "ocr_required")
                if key in page.diag}
        pages.append({"n": page.n, "w": page.width, "h": page.height, "source": page.source,
                      "diag": diag, "blocks": blocks, "ocr_engine": page.ocr_engine,
                      "requires_review": page.requires_review})
    return {"pages": pages, "title": title, "lang": ir.lang, "warnings": warnings}


@api.get("/documents/<document_id>")
def document(document_id: str):
    ws = workspace()
    info = ws.get(document_id)
    if info is None:
        return fail("Document not found.", 404)
    extracted = ws.extracted(document_id)
    if extracted:
        ir, _, result = extracted
        content = view(ir)
        info.update(content, validation=result["validation"], quality=result["quality"], seconds=result["seconds"],
                    processing_counts=result.get("processing_counts"))
    return jsonify(info)


@api.post("/documents/<document_id>/retry")
def retry(document_id: str):
    ws = workspace()
    if ws.get(document_id) is None:
        return fail("Document not found.", 404)
    if not ws.enqueue(document_id):
        return fail("A processing attempt is already active.", 409)
    return jsonify(ws.get(document_id)), 202


@api.get("/documents/<document_id>/pdf")
def pdf(document_id: str):
    ws = workspace()
    info = ws.get(document_id)
    if info is None:
        return fail("Document not found.", 404)
    return send_file(ws.source(document_id), mimetype="application/pdf", download_name=info["filename"])


@api.patch("/documents/<document_id>/metadata")
def metadata(document_id: str):
    ws = workspace()
    if ws.get(document_id) is None:
        return fail("Document not found.", 404)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return fail("Send a title, language and approval status.", 400)
    title, lang, approved = body.get("title"), body.get("lang"), body.get("approved")
    if (not isinstance(title, str) or len(title) > 1000 or xml_safe(title) != title or not isinstance(lang, str)
            or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", lang) or len(lang) > 35
            or not isinstance(approved, bool)):
        return fail("Use a title up to 1000 characters and a language code such as en, ar or und.", 400)
    if not ws.review(document_id, title.strip(), lang, approved):
        return fail("Wait until processing finishes before reviewing.", 409)
    return document(document_id)


@api.get("/documents/<document_id>/xml")
def xml(document_id: str):
    ws = workspace()
    info = ws.get(document_id)
    if info is None:
        return fail("Document not found.", 404)
    download = request.args.get("download") == "1"
    if download and not info["approved"]:
        return fail("Approve the review before exporting the XML.", 409)
    result = ws.xml(document_id)
    if result is None:
        return fail("Extracted XML is unavailable.", 409)
    data, validation = result
    if not validation["well_formed"] or validation["issues"]:
        return fail("XML validation failed. " + " ".join(validation["issues"]), 422)
    if download:
        return send_file(io.BytesIO(data), mimetype="application/xml", as_attachment=True,
                         download_name=f'{Path(info["filename"]).stem}.xml')
    return Response(data, mimetype="text/plain; charset=utf-8")


def version_changes(document_id: str, previous_id: str, current=None, previous=None) -> list[dict] | None:
    from xml_engine.serializer import to_xml

    ws = workspace()
    if not ws.owns(previous_id, browser_owner()):
        return None
    old, new = previous or ws.extracted(previous_id), current or ws.extracted(document_id)
    if not old or not new or previous_id == document_id:
        return None
    ir, settings, _ = new
    locations = {}
    to_xml(ir, settings, locations)
    changes = compare_ir(old[0], ir)
    for change in changes:
        target = change["new_location"]
        if target and target.get("page") and target.get("idx") is not None:
            block = ir.pages[target["page"] - 1].blocks[target["idx"]]
            change["source"] = {"page": target["page"], "xml_node": locations.get(id(block)),
                                "text": change["new_content"], "extraction": block.source,
                                "ocr_engine": block.ocr_engine or None}
    return changes


@api.get("/documents/<document_id>/compare")
def compare(document_id: str):
    changes = version_changes(document_id, request.args.get("previous_id", ""))
    if changes is None:
        return fail("Choose two different processed documents.", 409)
    changes, analysis = assess_changes(document_id, changes)
    return jsonify(changes=changes, analysis=analysis) if request.args.get("include_analysis") == "1" else jsonify(changes)


def assess_changes(document_id: str, changes: list[dict]) -> tuple[list[dict], dict]:
    ws = workspace()
    audit = ws.source(document_id).parent / "comparison.jsonl"
    return ws.worker.submit(run_assessment, changes, ws.settings, audit).result()


def run_assessment(changes, settings, audit_path):
    from comparison.ollama import annotate
    from utils.logs import AuditLog

    return changes, annotate(changes, settings, AuditLog(audit_path))


def export_root(document_id: str) -> Path:
    ws = workspace()
    return ws.output / output_stem(ws.get(document_id)["filename"])


@api.post("/documents/<document_id>/presentations")
def sync_deck(document_id: str):
    from powerpoint.amend import repair_presentation
    from powerpoint.sync import synchronize

    ws = workspace()
    previous_id = request.form.get("previous_id") or None
    mode = request.form.get("mode", "version")
    if mode not in {"source", "version"}:
        return fail("Choose source repair or version synchronization.", 400)
    info = ws.get(document_id)
    extracted = ws.extracted(document_id)
    if not extracted or info["jobs"][0]["status"] != "completed":
        return fail("Finish processing the PDF before exporting.", 409)
    ir, settings, extraction = extracted
    settings = replace(settings, schema_profile="sample")
    previous = None
    if mode == "version":
        if previous_id and previous_id != document_id and ws.owns(previous_id, browser_owner()):
            previous = ws.extracted(previous_id)
        if previous is None:
            return fail("Choose two different processed documents.", 409)
    uploaded, error = upload_file("pptx", ".pptx")
    if error:
        return fail(error, 400)
    filename, stream = uploaded
    export_id = uuid4()
    stem = output_stem(info["filename"])

    def transform(output: Path) -> dict:
        with TemporaryDirectory(prefix="presentation-", dir=ws.source(document_id).parent) as temp:
            source = Path(temp) / "source.pptx"
            with source.open("xb") as file:
                while chunk := stream.read(1024 * 1024):
                    file.write(chunk)
            if mode == "source":
                return ws.worker.submit(repair_presentation, ir, source, output, settings).result()
            changes = version_changes(document_id, previous_id, (ir, settings, extraction), previous)
            reviewed, analysis = assess_changes(document_id, changes)
            return {**synchronize(source, output, reviewed), "analysis": analysis}

    try:
        summary = publish_document(ws.output / stem / export_id.hex, stem, ir, settings, extraction,
                                   {"id": str(export_id), "document_id": document_id, "filename": filename,
                                    "previous_id": previous_id, "mode": mode, "created": now()}, transform)
    except OSError:
        current_app.logger.exception("Could not finalize the export report")
        return fail("The output folder could not be written. Check its location and available space.", 500)
    return jsonify(summary), 201 if summary["status"] == "completed" else 422


@api.get("/documents/<document_id>/presentations")
def presentations(document_id: str):
    reports = []
    for path in export_root(document_id).glob("*/processing_report.json"):
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("document_id") == document_id:
            report.pop("metadata", None)
            reports.append(available_outputs(path.parent, report))
    legacy = workspace().source(document_id).parent / "presentations"
    reports.extend(json.loads(path.read_text(encoding="utf-8")) for path in legacy.glob("*/report.json"))
    return jsonify(sorted(reports, key=lambda report: report["created"], reverse=True))


def export_file(folder: Path, relative: str) -> Path | None:
    file = (folder / relative).resolve()
    return file if file.is_relative_to(folder.resolve()) and file.is_file() else None


def available_outputs(folder: Path, report: dict) -> dict:
    missing = False
    for kind, item in report["outputs"].items():
        if item and not export_file(folder, item["filename"]):
            report["outputs"][kind] = None
            missing = True
    if missing:
        report["status"] = "partial" if report["outputs"]["xml"] else "failed"
        report["error"] = "An exported file is missing from the output folder."
    return report


def bundle(document_id: str, export_id: UUID):
    folder = export_root(document_id) / export_id.hex
    manifest = folder / "processing_report.json"
    if manifest.is_file():
        report = json.loads(manifest.read_text(encoding="utf-8"))
        if report.get("document_id") == document_id and report.get("id") == str(export_id):
            return folder, available_outputs(folder, report)
    return None


@api.get("/documents/<document_id>/presentations/<uuid:export_id>")
@api.get("/documents/<document_id>/presentations/<uuid:export_id>/xml")
@api.get("/documents/<document_id>/presentations/<uuid:export_id>/evidence.xml")
def download_export(document_id: str, export_id: UUID):
    kind = "xml" if request.path.endswith(("/xml", "/evidence.xml")) else "pptx"
    if found := bundle(document_id, export_id):
        folder, report = found
        item = report["outputs"].get(kind)
        if item and item["valid"] and (file := export_file(folder, item["filename"])):
            return send_file(file, as_attachment=True, download_name=item["filename"])
    else:
        legacy = workspace().source(document_id).parent / "presentations" / export_id.hex
        report_file = legacy / "report.json"
        file = legacy / ("evidence.xml" if kind == "xml" else "output.pptx")
        if report_file.is_file() and file.is_file():
            name = output_stem(json.loads(report_file.read_text(encoding="utf-8"))["filename"])
            return send_file(file, as_attachment=True, download_name="source-evidence.xml" if kind == "xml"
                             else f"{name}_updated.pptx")
    return fail("Requested export file not found.", 404)


@api.get("/documents/<document_id>/presentations/<uuid:export_id>/zip")
def download_bundle(document_id: str, export_id: UUID):
    found = bundle(document_id, export_id)
    if not found or found[1]["status"] != "completed":
        return fail("A complete export is required to download the ZIP.", 404)
    folder, report = found
    if info := report.get("archive"):
        archive = export_file(folder, info["filename"])
        if not archive:
            return fail("The validated ZIP is missing from the output folder.", 404)
        return send_file(archive, mimetype="application/zip", as_attachment=True, download_name=archive.name)

    from xml_engine.assets import inspect_assets, validate_archive

    xml_name, deck_name = (report["outputs"][kind]["filename"] for kind in ("xml", "pptx"))
    assets = inspect_assets((folder / xml_name).read_bytes(), folder)
    if not assets["valid"]:
        return fail("The XML has unresolved assets. Re-export this document to rebuild the ZIP.", 422)
    files = [xml_name, deck_name, *assets["files"]]
    if not all(export_file(folder, name) for name in files):
        return fail("An export file is missing.", 404)
    archive = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    try:
        write_zip(archive, folder, files)
        if not validate_archive(archive, xml_name, deck_name)["valid"]:
            archive.close()
            return fail("ZIP validation failed.", 422)
        archive.seek(0)
        return send_file(archive, mimetype="application/zip", as_attachment=True,
                         download_name=f"{folder.parent.name}.zip")
    except BaseException:
        archive.close()
        raise

"""HTTP routes for the single-document workspace."""
from __future__ import annotations

import io
import json
import re
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from flask import Blueprint, Response, current_app, jsonify, request, send_file
from lxml.etree import XMLSyntaxError
from werkzeug.exceptions import HTTPException

from comparison.differ import compare_ir
from layout.model import DocIR
from ui.workspace import Workspace, now
from utils.textutil import xml_safe

api = Blueprint("workspace", __name__, url_prefix="/api")


def workspace() -> Workspace:
    return current_app.extensions["workspace"]


def fail(message: str, status: int):
    return jsonify(error=message), status


@api.errorhandler(HTTPException)
def http_error(error: HTTPException):
    return fail(error.description, error.code)


def upload_file(field: str, extension: str, magic: bytes):
    files = request.files.getlist(field)
    if len(files) != 1 or sum(len(values) for _, values in request.files.lists()) != 1:
        return None, "Select exactly one file."
    file = files[0]
    filename = PureWindowsPath(file.filename or "").name
    if (Path(filename).suffix.lower() != extension or len(filename) > 255
            or any(ord(char) < 32 for char in filename)):
        return None, f"Select a valid {extension} file."
    if file.stream.read(len(magic)) != magic:
        return None, f"The file does not have a valid {extension} header."
    file.stream.seek(0)
    return (filename, file.stream), None


@api.get("/health")
def health():
    from ocr.engine import available_langs
    from comparison.ollama import health as ollama_health
    from ocr import glm, paddle
    from layout.docling_adapter import readiness as layout_readiness
    ws = workspace()
    return jsonify(status="ok", runtime="local CPU", ocr_enabled=ws.settings.ocr_enabled,
                   ocr_languages=sorted(available_langs(ws.settings)), xml_profile=ws.settings.schema_profile,
                   ollama=ollama_health(ws.settings), ocr_backend=ws.settings.ocr_backend,
                   glm_ocr=glm.model_info(ws.settings), paddle_ocr=paddle.readiness(ws.settings),
                   docling=layout_readiness(ws.settings))


@api.get("/documents")
def documents():
    return jsonify(workspace().documents())


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
    if previous and workspace().get(previous) is None:
        return fail("The previous document version does not exist.", 400)
    filename, stream = uploaded
    document_id = ws.upload(filename, stream, previous)
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
        info.update(content, validation=result["validation"], quality=result["quality"], seconds=result["seconds"])
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


def version_changes(document_id: str, previous_id: str, current=None) -> list[dict] | None:
    from xml_engine.serializer import to_xml

    ws = workspace()
    old, new = ws.extracted(previous_id), current or ws.extracted(document_id)
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
    from comparison.ollama import annotate
    from utils.logs import AuditLog
    ws = workspace()
    annotate(changes, ws.settings, AuditLog(ws.source(document_id).parent / "comparison.jsonl")
             if ws.settings.ollama_enabled else AuditLog())
    return jsonify(changes)


@api.post("/documents/<document_id>/presentations")
def sync_deck(document_id: str):
    from zipfile import BadZipFile
    from powerpoint.amend import repair_presentation
    from powerpoint.sync import synchronize
    from xml_engine.serializer import to_xml

    previous_id = request.form.get("previous_id") or None
    mode = request.form.get("mode", "version")
    ws = workspace()
    if mode == "source":
        extracted = ws.extracted(document_id, reviewed=False)
        if extracted is None:
            return fail("Process the source PDF before repairing a presentation.", 409)
    elif mode == "version":
        extracted = ws.extracted(document_id)
        if not extracted:
            return fail("Process the current PDF before synchronizing a presentation.", 409)
        changes = version_changes(document_id, previous_id or "", extracted)
        if changes is None:
            return fail("Choose two different processed documents.", 409)
    else:
        return fail("Choose source repair or version synchronization.", 400)
    uploaded, error = upload_file("pptx", ".pptx", b"PK")
    if error:
        return fail(error, 400)
    filename, stream = uploaded
    export_id = uuid4()
    target = ws.source(document_id).parent / "presentations" / export_id.hex
    target.mkdir(parents=True)
    source, output = target / "source.pptx", target / "output.pptx"
    try:
        with source.open("xb") as file:
            while chunk := stream.read(1024 * 1024):
                file.write(chunk)
        ir, settings, _ = extracted
        if mode == "source":
            report = ws.worker.submit(repair_presentation, ir, source, output, settings).result()
            report.pop("records")
        else:
            report = synchronize(source, output, changes)
        (target / "evidence.xml").write_bytes(to_xml(ir, settings))
    except (BadZipFile, KeyError, ValueError, XMLSyntaxError):
        return fail("The presentation could not be read or validated.", 422)
    summary = {"id": str(export_id), "filename": filename, "previous_id": previous_id, "mode": mode,
               "created": now(), **report}
    pending = target / "report.tmp"
    pending.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    pending.replace(target / "report.json")
    return jsonify(summary), 201


@api.get("/documents/<document_id>/presentations")
def presentations(document_id: str):
    ws = workspace()
    if ws.get(document_id) is None:
        return fail("Document not found.", 404)
    root = ws.source(document_id).parent / "presentations"
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in root.glob("*/report.json")]
    return jsonify(sorted(reports, key=lambda report: report["created"], reverse=True))


@api.get("/documents/<document_id>/presentations/<uuid:export_id>")
def download_deck(document_id: str, export_id):
    ws = workspace()
    if ws.get(document_id) is None:
        return fail("Document not found.", 404)
    target = ws.source(document_id).parent / "presentations" / export_id.hex
    report_file = target / "report.json"
    if not report_file.is_file():
        return fail("Presentation export not found.", 404)
    name = Path(json.loads(report_file.read_text(encoding="utf-8"))["filename"]).stem
    return send_file(target / "output.pptx", as_attachment=True, download_name=f"{name}_updated.pptx")


@api.get("/documents/<document_id>/presentations/<uuid:export_id>/evidence.xml")
def presentation_evidence(document_id: str, export_id):
    ws = workspace()
    if ws.get(document_id) is None:
        return fail("Document not found.", 404)
    target = ws.source(document_id).parent / "presentations" / export_id.hex
    evidence = target / "evidence.xml"
    if not (target / "report.json").is_file() or not evidence.is_file():
        return fail("Presentation evidence not found.", 404)
    return send_file(evidence, as_attachment=True, download_name="source-evidence.xml")

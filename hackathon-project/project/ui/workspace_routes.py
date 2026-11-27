"""HTTP routes for the single-document workspace."""
from __future__ import annotations

import io
import json
import re
from threading import Lock, Thread
from dataclasses import replace
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from secrets import token_hex
from tempfile import SpooledTemporaryFile, TemporaryDirectory
from subprocess import TimeoutExpired
from uuid import UUID, uuid4
from zipfile import BadZipFile, ZipFile

from flask import Blueprint, Response, current_app, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from comparison.differ import compare_ir
from layout.model import DocIR
from reporting.exports import (materialize_assets, output_stem, publish_document, requires_manual_review,
                               save_original_images, write_zip)
from ui.workspace import Workspace, now
from utils.textutil import xml_safe
from xml_engine.assets import inspect_assets, validate_archive
from xml_engine.completeness import compare_content
from xml_engine.serializer import to_xml
from xml_engine.validate import validate_xml, verify_unicode

api = Blueprint("workspace", __name__, url_prefix="/api")
SESS_COOKIE = "docusync_workspace"
WORKSPACE_XML_PROFILE = "sample"
_retry_lock = Lock()


def workspace() -> Workspace:
    return current_app.extensions["workspace"]


def fail(message: str, status: int):
    return jsonify(error=message), status


def browser_owner(*, create: bool = False) -> str | None:
    token = request.cookies.get(SESS_COOKIE, "")
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        # Keep a newly issued token in this request's WSGI environ, not g:
        # preserved Flask test contexts can otherwise carry it into another
        # client that does not have the matching cookie.
        token = request.environ.get("docusync.workspace_token", "")
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        if not create:
            return None
        token = token_hex(32)
    if not request.cookies.get(SESS_COOKIE):
        request.environ["docusync.workspace_token"] = token
    return sha256(token.encode()).hexdigest()


@api.before_request
def authorize_document():
    document_id = (request.view_args or {}).get("document_id")
    if document_id is not None and not workspace().owns(document_id, browser_owner()):
        return fail("Document not found.", 404)


@api.after_request
def set_browser_cookie(response):
    if token := request.environ.get("docusync.workspace_token"):
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
                   ocr_languages=sorted(available_langs(ws.settings)), xml_profile=WORKSPACE_XML_PROFILE,
                   ollama="connected" if glm_info["connected"] or qwen["connected"] else "unavailable",
                   ocr_backend=ws.settings.ocr_backend, qwen={**qwen, "enabled": ws.settings.ollama_enabled},
                   glm_ocr=glm_info, paddle_ocr=paddle.readiness(ws.settings),
                   docling=layout_readiness(ws.settings))


@api.get("/startup")
def startup_status():
    from startup import ROOT, StartupManager, configured_models

    ws = workspace()
    manager = StartupManager(ws.settings, ws.root, current_app.extensions.get("startup_logs", ROOT / "logs"),
                             current_app.extensions.get("startup_port", 8765))
    report = manager.report or {"status": "not_run", "models": configured_models(ws.settings)}
    if report["status"] != "warming":
        manager.refresh_residency(report.get("models", []), {item["url"]: item for item in report.get("services", [])},
                                  live=True)
    retry = current_app.extensions.get("startup_retry")
    return jsonify(**{**report, "backend_ready": True, "frontend_ready": True,
                      "retrying": bool(retry and retry.is_alive())})


@api.post("/startup/retry")
def retry_startup_models():
    from startup import ROOT, StartupBusy, StartupManager, startup_lock

    if not request.is_json:
        return fail("Request startup retry from the local workspace.", 415)
    app = current_app._get_current_object()
    ws = workspace()
    folder = app.extensions.get("startup_logs", ROOT / "logs")
    with ws.db() as db:
        if db.execute("SELECT 1 FROM jobs WHERE status IN ('queued', 'running') LIMIT 1").fetchone():
            return fail("Wait until document processing finishes before warming models again.", 409)

    def prepare():
        try:
            with startup_lock(folder):
                manager = StartupManager(ws.settings, ws.root, folder, app.extensions.get("startup_port", 8765))
                manager.event("startup", "retry_requested")
                try:
                    manager.prepare()
                except (OSError, RuntimeError, ValueError) as error:
                    manager.event("startup", "retry_failed", error=str(error))
                    manager.publish(status="failed", error=str(error))
        except StartupBusy:
            return

    with _retry_lock:
        thread = app.extensions.get("startup_retry")
        if thread and thread.is_alive():
            return fail("Model startup is already being checked.", 409)
        thread = Thread(target=prepare, daemon=True)
        app.extensions["startup_retry"] = thread
        thread.start()
    return jsonify(status="checking"), 202


@api.get("/documents")
def documents():
    return jsonify(workspace().documents(browser_owner(create=True)))


@api.route("/layout-health", methods=["GET", "POST"])
def layout_health():
    return jsonify(workspace().layout_health(retry=request.method == "POST"))


@api.route("/ocr-settings", methods=["GET", "PATCH"])
def ocr_settings():
    from ocr.recognition import ENGINE_MODES
    from ocr.summary import health, observed_runs
    from startup import ROOT, read_json

    ws, owner = workspace(), browser_owner(create=True)
    if request.method == "PATCH":
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or body.get("engine") not in ENGINE_MODES:
            return fail("Choose a supported OCR engine or Automatic.", 400)
        ws.set_ocr_preference(owner, body["engine"])
    data = {"engine": ws.ocr_preference(owner), "automatic_primary": ws.settings.ocr_automatic_primary,
            "automatic_secondary": ws.settings.ocr_automatic_secondary,
            "automatic_tertiary": ws.settings.ocr_automatic_tertiary}
    if request.method == "PATCH" or request.args.get("detail") != "1":
        return jsonify(data)
    with ws.db() as db:
        jobs = db.execute("""SELECT j.result, j.finished FROM jobs j JOIN documents d ON d.id=j.document_id
                             WHERE d.owner=? AND j.finished IS NOT NULL AND j.result IS NOT NULL
                             ORDER BY j.finished DESC LIMIT 50""", (owner,)).fetchall()
        probes = {row["engine"]: json.loads(row["result"]) for row in db.execute(
            "SELECT engine, result FROM ocr_probes WHERE owner=?", (owner,))}
    results = [{**json.loads(row["result"]), "finished": row["finished"]} for row in jobs]
    runs = observed_runs(results)
    logs = current_app.extensions.get("startup_logs", ROOT / "logs")
    return jsonify(**data, engines=health(ws.settings, runs, probes), measurements=runs, jobs_sampled=len(jobs),
                   benchmark=read_json(logs / "ocr-comparison.json"))


@api.post("/ocr-test")
def test_ocr_engine():
    from ocr.probe import test_engine
    from ocr.recognition import OCR_ENGINES

    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get("engine") not in OCR_ENGINES:
        return fail("Choose one OCR engine to test.", 400)
    owner, ws = browser_owner(create=True), workspace()
    engine = body["engine"]
    try:
        result = ws.worker.submit(test_engine, ws.settings, engine).result()
    except (OSError, RuntimeError, ValueError):
        current_app.logger.exception("The local OCR engine test could not be completed.")
        return fail("The OCR test worker stopped. See the local application log.", 503)
    with ws.db() as db:
        db.execute("INSERT INTO ocr_probes VALUES (?, ?, ?) ON CONFLICT(owner,engine) DO UPDATE SET result=excluded.result",
                   (owner, engine, json.dumps(result)))
    return jsonify(result)


def requested_ocr(engine, language):
    from ocr.paddle import LANGUAGES
    from ocr.recognition import ENGINE_MODES

    if engine is not None and engine not in ENGINE_MODES:
        raise ValueError("Choose a supported OCR engine or Automatic.")
    if language is not None and (not isinstance(language, str) or language not in {"", *LANGUAGES}):
        raise ValueError("Choose a supported PaddleOCR language.")
    return engine, language


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
    try:
        engine, language = requested_ocr(request.form.get("ocr_engine"), request.form.get("ocr_language"))
    except ValueError as error:
        return fail(str(error), 400)
    phase = request.form.get("phase", "1")
    if phase not in {"1", "2"}:
        return fail("Choose Phase 1 or Phase 2.", 400)
    uploaded, error = upload_file("pdf", ".pdf", b"%PDF-")
    if error:
        return fail(error, 400)
    ws = workspace()
    previous = request.form.get("previous_id") or None
    if previous and not ws.owns(previous, browser_owner()):
        return fail("The previous document version does not exist.", 400)
    filename, stream = uploaded
    document_id = ws.upload(filename, stream, previous, browser_owner(create=True), phase=phase,
                            engine=engine, language=language, defer=request.form.get("defer") == "1")
    return jsonify(ws.get(document_id)), 202


@api.get("/history")
def processing_history():
    owner = browser_owner(create=True)
    ws = workspace()
    pdfs, decks = ws.documents(owner), ws.presentations(owner)
    linked = {}
    for deck in decks:
        if deck.get("document_id") and deck.get("pptx_download_url"):
            linked.setdefault(deck["document_id"], deck["pptx_download_url"])
    reports_by_folder = {}
    for pdf in pdfs:
        folder = ws.phase_output_dir(pdf["phase"]) / output_stem(pdf["filename"])
        if folder not in reports_by_folder:
            reports_by_folder[folder] = list(export_manifests(folder))
        reports = [(path, report) for path, report in reports_by_folder[folder] if report.get("document_id") == pdf["id"]]
        for path, report in sorted(reports, key=lambda entry: entry[1].get("created", ""), reverse=True):
            output = (report.get("outputs") or {}).get("pptx")
            if (not output or not output.get("valid") or not output.get("filename")
                    or not export_file(path.parent, output["filename"])):
                continue
            try:
                export_id = UUID(report["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if export_id.hex == path.parent.name and str(export_id) == report["id"]:
                pdf["pptx_download_url"] = f"/api/documents/{pdf['id']}/presentations/{export_id}"
                break
        if pdf["id"] in linked:
            pdf.setdefault("pptx_download_url", linked[pdf["id"]])
    return jsonify(pdfs=pdfs, presentations=decks)


@api.post("/presentations")
def create_standalone_presentation():
    phase = request.form.get("phase", "1")
    if phase not in {"1", "2"}:
        return fail("Choose Phase 1 or Phase 2.", 400)
    owner = browser_owner(create=True)
    ws = workspace()
    document_id = request.form.get("document_id") or None
    if document_id and not ws.owns(document_id, owner):
        return fail("The paired PDF was not found in this workspace.", 404)
    uploaded, error = upload_file("pptx", ".pptx", b"PK")
    if error:
        return fail(error, 400)
    filename, stream = uploaded
    try:
        with ZipFile(stream) as archive:
            names = set(archive.namelist())
            if ("ppt/presentation.xml" not in names or "[Content_Types].xml" not in names
                    or not any(name.startswith("ppt/slides/slide") and name.endswith(".xml") for name in names)
                    or archive.testzip() is not None):
                return fail("This file is not a valid PowerPoint presentation. Choose a .pptx file.", 400)
        stream.seek(0)
        from pptx import Presentation

        if not Presentation(stream).slides:
            return fail("The PowerPoint presentation must contain at least one slide.", 400)
        stream.seek(0)
    except (BadZipFile, KeyError, OSError, ValueError):
        return fail("This file is not a valid PowerPoint presentation. Choose a .pptx file.", 400)
    try:
        job_id = ws.enqueue_presentation(filename, stream, owner, phase, document_id=document_id)
    except OSError:
        current_app.logger.exception("Could not archive the standalone PowerPoint input")
        return fail("The presentation could not be saved in the selected phase folder.", 500)
    return jsonify(ws.get_presentation(job_id, owner)), 202


@api.get("/presentations/<job_id>")
def standalone_presentation_status(job_id: str):
    item = workspace().get_presentation(job_id, browser_owner())
    return (jsonify(item), 200) if item else fail("Presentation job not found.", 404)


@api.get("/presentations/<job_id>/download")
def download_standalone_presentation(job_id: str):
    ws, owner = workspace(), browser_owner()
    item = ws.get_presentation(job_id, owner)
    if not item:
        return fail("Presentation job not found.", 404)
    if item["status"] != "completed":
        return fail("The corrected presentation is not ready yet.", 409)
    target = ws.presentation_download(job_id, owner)
    if not target:
        return fail("The validated PowerPoint output is missing.", 404)
    return send_file(target, as_attachment=True, download_name=target.name,
                     mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation")


@api.get("/presentations/<job_id>/source")
def standalone_presentation_source(job_id: str):
    source = workspace().presentation_source(job_id, browser_owner())
    if not source:
        return fail("Presentation not found.", 404)
    return send_file(source, as_attachment=True)


@api.get("/presentations/<job_id>/preview")
@api.get("/presentations/<job_id>/preview/<int:page>.png")
def standalone_presentation_preview(job_id: str, page: int | None = None):
    import pymupdf
    from powerpoint.validation import render_pdf
    from quality.slide_preview import renderer

    ws = workspace()
    source = ws.presentation_source(job_id, browser_owner())
    if not source:
        return fail("Presentation not found.", 404)
    folder = ws.root / "presentation-previews"
    target = folder / f"{job_id}.pdf"
    try:
        if not target.is_file():
            with ws.preview_lock:
                if not target.is_file():
                    executable = renderer()
                    if not executable:
                        return fail("Install LibreOffice locally to preview slides. The original is available to download.", 503)
                    folder.mkdir(exist_ok=True)
                    with TemporaryDirectory(dir=folder, ignore_cleanup_errors=True) as temp:
                        staging = Path(temp).resolve()
                        rendered = render_pdf(executable, source, staging / "slides", staging / "profile")
                        rendered.replace(target)
        if page is not None:
            from ui.server import render_page
            return render_page(target, page)
        with pymupdf.open(target) as pdf:
            return jsonify(slides=len(pdf))
    except (OSError, RuntimeError, TimeoutExpired, ValueError):
        current_app.logger.exception("Could not render the PowerPoint preview")
        return fail("The presentation could not be rendered locally. The original is available to download.", 422)


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
            warnings.append(f"Page {page.n}: reconstructed text coordinates require review for rotation or skew.")
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
                           "cells": [vars(cell) for cell in block.cells], "language": block.language,
                           "reading_order": block.reading_order, "validation_status": block.validation_status,
                           "writing_mode": block.writing_mode, "source_reference": block.source_reference,
                           "n_items": len(block.children) or len(block.cells)})
            if block.kind == "title" and title is None:
                title = {"value": block.text, "page": page.n, "bbox": block.bbox if aligned else None,
                         "confidence": None if block.ocr_engine in {"glm-ocr", "paddleocr-vl"} else block.conf}
        diag = {key: page.diag[key] for key in ("health", "decision", "reasons", "ocr", "confidence", "rotation",
                                              "layout", "native_chars", "has_native_text", "short_native_text",
                                              "appears_scanned", "has_images", "ocr_required", "review_reasons",
                                              "selected_ocr_engine")
                if key in page.diag}
        layout = page.diag.get("layout", {})
        detections = []
        for region in layout.get("detections", []):
            linked = [block for block in blocks if block["source_reference"].get("layout_region_id") == region["id"]]
            caption = [block for block in blocks if block["source_reference"].get("caption_region_id") == region["id"]]
            text = [block["text"] or "\n".join(cell["text"] for cell in block["cells"]) for block in linked]
            ocr_text = [(block["text"] or "\n".join(cell["text"] for cell in block["cells"])) for block in linked
                        if block["ocr_engine"]]
            text.extend(block["caption"] for block in caption)
            ocr_text.extend(block["caption"] for block in caption if block["ocr_engine"])
            engines = set(block["ocr_engine"] for block in linked + caption if block["ocr_engine"])
            fragment_blocks = []
            for block in blocks:
                fragments = block["source_reference"].get("layout_fragments", {}).get(region["id"], [])
                if fragments:
                    fragment_blocks.append(block)
                    text.extend(fragments)
                    ocr_text.extend(fragments)
                    if block["ocr_engine"]:
                        engines.add(block["ocr_engine"])
            if region.get("ocr_lines") and page.ocr_engine:
                engines.add(page.ocr_engine)
            detections.append({**region, "text": "\n".join(filter(None, text)),
                               "ocr_text": "\n".join(filter(None, ocr_text or region.get("ocr_lines", []))),
                               "ocr_engines": sorted(engines),
                               "block_ids": sorted({block["i"] for block in linked + caption + fragment_blocks})})
        if "layout" in diag:
            diag["layout"] = {key: value for key, value in layout.items() if key != "detections"}
        pages.append({"n": page.n, "w": page.width, "h": page.height, "source": page.source,
                      "diag": diag, "blocks": blocks, "ocr_engine": page.ocr_engine,
                      "layout_regions": detections,
                      "requires_review": page.requires_review, "language": page.language})
    return {"pages": pages, "title": title, "lang": ir.lang, "language": ir.language, "warnings": warnings}


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
                    submission_export=result.get("submission_export"),
                    processing_counts=result.get("processing_counts"),
                    content_validation=result.get("multilingual_validation", {}))
    return jsonify(info)


@api.post("/documents/<document_id>/retry")
def retry(document_id: str):
    ws = workspace()
    if ws.get(document_id) is None:
        return fail("Document not found.", 404)
    body = request.get_json(silent=True) if request.is_json else {}
    if not isinstance(body, dict):
        return fail("Send OCR options as an object.", 400)
    try:
        engine, language = requested_ocr(body.get("ocr_engine"), body.get("ocr_language"))
    except ValueError as error:
        return fail(str(error), 400)
    if not ws.enqueue(document_id, engine=engine, language=language):
        return fail("A processing attempt is already active.", 409)
    return jsonify(ws.get(document_id)), 202


@api.post("/documents/<document_id>/layout")
def refresh_document_layout(document_id: str):
    ws = workspace()
    if not ws.enqueue(document_id, layout_only=True):
        return fail("Layout refresh requires a completed extraction and an idle document.", 409)
    return jsonify(ws.get(document_id)), 202


@api.get("/documents/<document_id>/pdf")
def pdf(document_id: str):
    ws = workspace()
    info = ws.get(document_id)
    if info is None:
        return fail("Document not found.", 404)
    return send_file(ws.source(document_id), mimetype="application/pdf", download_name=info["filename"])


@api.get("/documents/<document_id>/source-info")
def source_info(document_id: str):
    import pymupdf

    try:
        with pymupdf.open(workspace().source(document_id)) as source:
            if source.needs_pass:
                return fail("This PDF requires a password.", 422)
            if not source.page_count:
                return fail("This PDF contains no pages.", 422)
            metadata = {key: value for key, value in source.metadata.items()
                        if value and key in {"title", "author", "subject", "keywords", "creator", "producer"}}
            return jsonify(pages=[{"n": page.number + 1, "w": page.rect.width, "h": page.rect.height}
                                  for page in source], metadata=metadata)
    except (OSError, RuntimeError, ValueError):
        return fail("This PDF cannot be rendered.", 422)


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
    response = document(document_id).get_json()
    if approved:
        info = ws.get(document_id)
        snapshot = ws.extracted(document_id)
        if snapshot:
            ir, settings, extraction = snapshot
            extraction = {**extraction, "source_review_approved": info["approved"]}
            settings = replace(settings, schema_profile=WORKSPACE_XML_PROFILE)
            stem = output_stem(info["filename"])
            export_id = uuid4()
            try:
                export = publish_document(
                    ws.phase_output_dir(info["phase"]) / stem / export_id.hex,
                    stem, ir, settings, extraction,
                    {"id": str(export_id), "document_id": document_id, "filename": info["filename"],
                     "phase": info["phase"], "mode": "approved_xml", "created": now(),
                     "job_id": info["jobs"][0]["id"], "source_review_approved": info["approved"]},
                    None, xml_only=True,
                )
                response["phase_export"] = {
                    "phase": info["phase"], "output_folder": export["output_folder"],
                    "outputs": export["outputs"], "status": export["status"],
                    "error": export.get("error", ""),
                }
            except (OSError, ValueError, RuntimeError):
                current_app.logger.exception("Could not save the approved XML to its phase output folder")
                response["phase_export"] = {"phase": info["phase"], "status": "failed",
                                             "error": "The review was saved, but the XML could not be written to the phase output folder."}
    return jsonify(response)


@api.get("/documents/<document_id>/xml")
def xml(document_id: str):
    ws = workspace()
    info = ws.get(document_id)
    if info is None:
        return fail("Document not found.", 404)
    download = request.args.get("download") == "1"
    profile = request.args.get("profile", WORKSPACE_XML_PROFILE)
    download_format = request.args.get("format", "auto")
    if profile not in {"sample", "brief", "example"}:
        return fail("Choose the sample, brief, or example XML profile.", 400)
    if download_format not in {"auto", "xml", "zip"}:
        return fail("Choose XML or ZIP for the download format.", 400)
    if download and not info["approved"]:
        return fail("Approve the review before exporting the XML.", 409)
    snapshot = ws.extracted(document_id)
    if snapshot is None:
        return fail("Extracted XML is unavailable.", 409)
    ir, settings, extraction = snapshot
    settings = replace(settings, schema_profile=profile)
    stem = output_stem(info["filename"])
    ir.meta["asset_stem"] = stem
    assets, locations = {}, {}
    data = to_xml(ir, settings, locations=locations, assets=assets)
    validation = validate_xml(data, len(ir.pages), settings)
    if not validation["well_formed"] or validation["issues"]:
        return fail("XML validation failed. " + " ".join(validation["issues"]), 422)
    if download:
        completeness = compare_content(data, ir, locations, sample=profile == WORKSPACE_XML_PROFILE)
        extraction = {**extraction, "source_review_approved": info["approved"]}
        draft = requires_manual_review(extraction, completeness)
        filename = f"{stem}{'_draft' if draft else ''}.xml"
        if download_format == "zip" or (download_format == "auto" and assets):
            try:
                with TemporaryDirectory(prefix="xml-export-", dir=ws.source(document_id).parent) as temp:
                    folder = Path(temp)
                    if assets:
                        data = materialize_assets(data, assets, ir.path, folder, deduplicate=False)
                    path = folder / filename
                    path.write_bytes(data)
                    images = inspect_assets(data, folder)
                    save_original_images(data, ir, locations, folder, images)
                    reports = {
                        "manifest.json": {"document_id": document_id, "xml_profile": profile, "xml": filename,
                                          "images": images["files"], "image_originals": images["original_files"],
                                          "status": "review_required" if draft else completeness["status"]},
                        "reports/xml_validation_report.json": {**validation, "unicode": verify_unicode(data, ir, locations)},
                        "reports/content_completeness_report.json": {
                            key: value for key, value in completeness.items() if key != "formatting"},
                        "reports/formatting_report.json": completeness["formatting"],
                        "reports/image_asset_report.json": images,
                    }
                    return download_saved_xml(path, force_zip=True, images=images, reports=reports)
            except (OSError, ValueError, RuntimeError):
                current_app.logger.exception("Could not package the document XML")
                return fail("The XML and its images could not be packaged.", 422)
        return send_file(io.BytesIO(data), mimetype="application/xml", as_attachment=True,
                         download_name=filename)
    return Response(data, mimetype="text/plain; charset=utf-8")


def download_saved_xml(path, *, force_zip=False, images=None, reports=None):
    """Download saved XML together with its exact existing asset files."""
    if request.args.get("format") != "xml":
        assets = images if images is not None else inspect_assets(path.read_bytes(), path.parent)
        if not assets["valid"]:
            return fail("The XML has unresolved pictures. Re-export it from the source PDF.", 422)
        if assets["files"] or force_zip or request.args.get("format") == "zip":
            archive = SpooledTemporaryFile(max_size=8 * 1024 * 1024)
            try:
                write_zip(archive, path.parent, [path.name, *assets["files"], *assets.get("original_files", [])], reports)
                if not validate_archive(archive, path.name)["valid"]:
                    raise ValueError("The extracted XML has unresolved image references.")
                archive.seek(0)
            except (OSError, ValueError, RuntimeError):
                archive.close()
                current_app.logger.exception("Could not package the saved XML")
                return fail("The XML and its images could not be packaged.", 422)
            return send_file(archive, mimetype="application/zip", as_attachment=True,
                             download_name=f"{path.stem}.zip")
    return send_file(path, as_attachment=True, download_name=path.name)


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
    to_xml(ir, replace(settings, schema_profile=WORKSPACE_XML_PROFILE), locations)
    changes = compare_ir(old[0], ir)
    for change in changes:
        target = change["new_location"]
        if target and target.get("page") and target.get("idx") is not None:
            block = ir.pages[target["page"] - 1].blocks[target["idx"]]
            change["source"] = {"page": target["page"], "xml_node": locations.get(id(block)),
                                "text": change["new_content"], "extraction": block.source,
                                "ocr_engine": block.ocr_engine or None,
                                "block_id": block.source_reference.get("block_id"),
                                "execution_ids": block.source_reference.get("execution_ids", [])}
    return changes


@api.get("/documents/<document_id>/compare")
def compare(document_id: str):
    changes = version_changes(document_id, request.args.get("previous_id", ""))
    if changes is None:
        return fail("Choose two different processed documents.", 409)
    if request.args.get("include_analysis") != "1":
        return jsonify(changes)
    changes, analysis = assess_changes(document_id, changes)
    return jsonify(changes=changes, analysis=analysis)


@api.get("/documents/<document_id>/execution-audit")
def document_execution_audit(document_id: str):
    from reporting.execution import reports
    from xml_engine.serializer import to_xml

    ws = workspace()
    snapshot = ws.extracted(document_id, reviewed=False)
    if snapshot is None:
        return fail("Finish extraction before opening the execution audit.", 409)
    ir, settings, result = snapshot
    job = next(item for item in ws.get(document_id)["jobs"] if item["status"] == "completed")
    folder = ws.source(document_id).parent
    evidence = result.get("execution_evidence") or {}
    comparisons = []
    for file in sorted((folder / "comparison-audit").glob("*.json")):
        record = json.loads(file.read_text(encoding="utf-8"))
        if record.get("trace_id") == evidence.get("trace_id") or (
                not evidence and record.get("document_sha256") == ir.meta.get("source_sha256")):
            comparisons.append(record)
    stored_xml = folder / str(job["id"]) / "output" / "source.xml"
    locations = {}
    to_xml(ir, settings, locations)
    data = reports(ir, {**evidence, "executions": [*evidence.get("executions", []), *comparisons]},
                   stored_xml.read_bytes() if stored_xml.is_file() else None, locations)
    if name := request.args.get("report"):
        if name not in data:
            return fail("Unknown audit report.", 404)
        return send_file(io.BytesIO(json.dumps(data[name], ensure_ascii=False, indent=2).encode("utf-8")),
                         mimetype="application/json", as_attachment=True, download_name=name)
    return jsonify(data)


def assess_changes(document_id: str, changes: list[dict]) -> tuple[list[dict], dict]:
    ws = workspace()
    audit = ws.source(document_id).parent / "comparison.jsonl"
    ir, _, extraction = ws.extracted(document_id)
    return ws.worker.submit(run_assessment, changes, ws.settings, audit, document_id, ir.meta.get("source_sha256"),
                            extraction.get("execution_evidence", {}).get("trace_id")).result()


def run_assessment(changes, settings, audit_path, document_id=None, source_hash=None, trace_id=None):
    from comparison.ollama import annotate
    from utils.logs import AuditLog
    from utils.execution_audit import scope
    from reporting.exports import write_report

    with scope(document_id, source_hash,
               audit_path.parent / "private-model-responses" if settings.audit_private_responses else None, trace_id):
        analysis = annotate(changes, settings, AuditLog(audit_path))
        if record := analysis.get("execution"):
            folder = audit_path.parent / "comparison-audit"
            folder.mkdir(parents=True, exist_ok=True)
            write_report(folder / (record["execution_id"] + ".json"), record)
        return changes, analysis


def export_root(document_id: str) -> Path:
    ws = workspace()
    info = ws.get(document_id)
    return ws.phase_output_dir(info["phase"]) / output_stem(info["filename"])


@api.post("/documents/<document_id>/presentations")
def sync_deck(document_id: str):
    from powerpoint.amend import repair_presentation
    from powerpoint.from_pdf import render_presentation
    from powerpoint.sync import synchronize

    ws = workspace()
    previous_id = request.form.get("previous_id") or None
    mode = request.form.get("mode", "version")
    if mode not in {"source", "version", "generate"}:
        return fail("Choose PDF slides, source repair or version synchronization.", 400)
    info = ws.get(document_id)
    extracted = ws.extracted(document_id)
    if not extracted or info["jobs"][0]["status"] != "completed":
        return fail("Finish processing the PDF before exporting.", 409)
    ir, settings, extraction = extracted
    if ir.meta.get("ocr_metadata_version") != 2:
        return fail("Retry PDF extraction to generate current language and source validation before exporting.", 409)
    extraction = {**extraction, "source_review_approved": info["approved"]}
    settings = replace(settings, schema_profile=WORKSPACE_XML_PROFILE)
    previous = None
    if mode == "version":
        if previous_id and previous_id != document_id and ws.owns(previous_id, browser_owner()):
            previous = ws.extracted(previous_id)
        if previous is None:
            return fail("Choose two different processed documents.", 409)
        if previous[0].meta.get("ocr_metadata_version") != 2:
            return fail("Retry extraction for the previous PDF before applying version changes.", 409)
    if mode == "generate":
        if not info["approved"]:
            return fail("Submit the metadata review before generating PowerPoint.", 409)
        filename = info["filename"]
    else:
        uploaded, error = upload_file("pptx", ".pptx")
        if error:
            return fail(error, 400)
        filename, stream = uploaded
        try:
            ws.save_phase_input(filename, stream, info["phase"])
        except OSError:
            current_app.logger.exception("Could not archive the uploaded PowerPoint input")
            return fail("The PowerPoint input could not be saved in the selected phase folder.", 500)
    export_id = uuid4()
    stem = output_stem(info["filename"])

    def transform(output: Path) -> dict:
        if mode == "generate":
            return ws.worker.submit(render_presentation, ir, output).result()
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

    with ws.export_lock:
        if document_id in ws.export_progress:
            return fail("An export for this PDF is already running.", 409)
        ws.export_progress[document_id] = {}
    try:
        summary = publish_document(ws.phase_output_dir(info["phase"]) / stem / export_id.hex,
                                   stem, ir, settings, extraction,
                                   {"id": str(export_id), "document_id": document_id, "filename": filename,
                                    "previous_id": previous_id, "mode": mode, "created": now(),
                                    "job_id": info["jobs"][0]["id"],
                                    "source_review_approved": info["approved"]}, transform,
                                   progress=lambda phases: ws.export_progress.update({document_id: phases}))
    except OSError:
        current_app.logger.exception("Could not finalize the export report")
        return fail("The output folder could not be written. Check its location and available space.", 500)
    finally:
        with ws.export_lock:
            ws.export_progress.pop(document_id, None)
    return jsonify(summary), 201 if summary["status"] == "completed" else 422


@api.get("/documents/<document_id>/export-progress")
def export_progress(document_id: str):
    return jsonify(workspace().export_progress.get(document_id, {}))


@api.get("/documents/<document_id>/presentations")
def presentations(document_id: str):
    reports = []
    for path, report in export_manifests(export_root(document_id)):
        if report.get("document_id") == document_id:
            report.pop("metadata", None)
            reports.append(available_outputs(path.parent, report))
    legacy = workspace().source(document_id).parent / "presentations"
    reports.extend(json.loads(path.read_text(encoding="utf-8")) for path in legacy.glob("*/report.json"))
    return jsonify(sorted(reports, key=lambda report: report["created"], reverse=True))


def export_manifests(folder: Path):
    for path in folder.glob("*/processing_report.json"):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(report, dict) and isinstance(report.get("outputs"), dict) and isinstance(report.get("created"), str):
            yield path, report


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
            if kind == "xml" and request.args.get("preview") == "1":
                return send_file(file, mimetype="text/plain", as_attachment=False)
            if kind == "xml" and request.args.get("format") in {"auto", "zip"}:
                return download_saved_xml(file)
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

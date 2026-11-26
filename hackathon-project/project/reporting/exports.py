"""Publish versioned document outputs and a report for each export attempt."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from time import monotonic
from zipfile import ZIP_DEFLATED, ZipFile

import pymupdf

from powerpoint.package import validate_deck
from powerpoint.language import presentation_language_report
from xml_engine.assets import inspect_assets, relative_path, validate_archive
from quality.conformance import tree
from lxml import etree
from xml_engine.serializer import to_xml
from xml_engine.validate import validate_xml, verify_unicode
from xml_engine.completeness import compare_content
from reporting.execution import reports as execution_reports


def output_stem(filename: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", PureWindowsPath(filename).stem).strip(" .")
    stem = stem[:100].rstrip(" .") or "document"
    return f"_{stem}" if PureWindowsPath(stem).is_reserved() else stem


def requires_manual_review(extraction: dict, completeness: dict) -> bool:
    quality = extraction.get("multilingual_validation", {})
    unresolved_source = completeness.get("source_review") and not extraction.get("source_review_approved")
    return bool(quality.get("blocking_pages") or completeness.get("issues") or unresolved_source)


def write_report(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(".tmp")
    staged.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    staged.replace(path)


def file_info(path: Path) -> dict:
    length = path.stat().st_size
    if length == 0:
        raise ValueError(f"{path.name} is empty.")
    return {"filename": path.name, "path": str(path), "size_bytes": length, "valid": True}


def correction_record(document_id: str, item: dict) -> dict:
    source = item.get("source") or {}
    review = item.get("action") == "review" or (
        item.get("action") is None and not item.get("new") and "change_type" in item)
    return {"document_id": document_id, "page": source.get("page"), "slide": item.get("slide"),
            "shape_id": item.get("shape_id"), "paragraph": item.get("paragraph"),
            "original_content": item.get("old", item.get("old_content", "")),
            "updated_content": item.get("new", "" if review else item.get("new_content", "")),
            "source_pdf_reference": source or None, "reason": item.get("reason"),
            "confidence": item.get("confidence"), "status": "MANUAL_REVIEW_REQUIRED" if review else "CORRECT",
            "manual_review_required": review, "change_type": item.get("change_type")}


def materialize_assets(data: bytes, assets: dict, pdf_path: str, folder: Path, *, deduplicate: bool = True) -> bytes:
    seen, aliases = {}, {}
    with pymupdf.open(pdf_path) as pdf:
        for name, (page, box) in assets.items():
            if not relative_path(name):
                raise ValueError("Generated asset path is not portable.")
            source = pdf[page - 1]
            clip = pymupdf.Rect(box)
            if clip.is_empty or not source.rect.contains(clip):
                raise ValueError("An image crop lies outside its source page.")
            pixels = source.get_pixmap(dpi=180, clip=clip, alpha=False).tobytes("png")
            fingerprint = sha256(pixels).digest()
            if deduplicate and fingerprint in seen:
                aliases[name] = seen[fingerprint]
                continue
            seen[fingerprint] = name
            asset = folder / name
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_bytes(pixels)
    if not aliases:
        return data
    root = tree(data)
    for image in root.xpath("//img[@src]"):
        if image.get("src") in aliases:
            image.set("src", aliases[image.get("src")])
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, pretty_print=True)


def save_original_images(data, ir, locations, folder, asset_report):
    """Keep embedded picture bytes beside the positioned render used by the XML."""
    candidates = [(block, locations.get(id(block))) for page in ir.pages for block in page.blocks
                  if block.tag == "figure" and block.source_reference.get("image", {}).get("xref")]
    asset_report["original_files"] = []
    if not candidates:
        return
    root = tree(data)
    references = {item["xml_node"]: item for item in asset_report["references"]}
    with pymupdf.open(ir.path) as pdf:
        for block, path in candidates:
            nodes = root.xpath(path + "/img") if path else []
            if not nodes:
                continue
            image = nodes[0]
            entry = references.get(root.getroottree().getpath(image))
            if not entry or not entry["src"]:
                continue
            xref = block.source_reference["image"]["xref"]
            try:
                info = pdf.extract_image(xref)
                ext = info.get("ext") if info else None
                if ext not in {"png", "jpeg", "jpg", "jp2", "webp", "tiff", "bmp", "gif"} or not info["image"]:
                    raise ValueError("The embedded image encoding has no portable original file.")
                asset = Path(entry["src"])
                original = asset.parent / "originals" / (asset.stem + "." + ext)
                if not relative_path(original.as_posix()):
                    raise ValueError("The original image path is invalid.")
                target = folder / original
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(info["image"])
                entry["original"] = {"file": original.as_posix(), "pdf_object": xref, "format": ext,
                                     "width": info["width"], "height": info["height"],
                                     "sha256": sha256(info["image"]).hexdigest()}
                asset_report["original_files"].append(original.as_posix())
            except (OSError, RuntimeError, ValueError) as error:
                entry["original"] = {"status": "unavailable", "pdf_object": xref, "reason": str(error)}


def publish_document(folder: Path, stem: str, ir, settings, extraction: dict, report: dict,
                     transform, *, xml_only: bool = False, progress=None) -> dict:
    """Write validated XML first, then publish the deck and the final status."""
    folder.mkdir(parents=True, exist_ok=False)
    xml_path = folder / f"{stem}.xml"
    deck_path = folder / f"{stem}_updated.pptx"
    validation = {"xml": {"valid": False}, "pptx": {"valid": False}}
    outputs = {"xml": None, "pptx": None}
    result = {**report, "output_folder": str(folder), "outputs": outputs, "applied": [], "review": [],
              "status": "failed", "xml_profile": settings.schema_profile}
    phases = result["export_phases"] = {}

    def stage(name, status, started):
        phases[name] = {"status": status, "seconds": round(monotonic() - started, 2)}
        if status == "running":
            phases[name]["at"] = datetime.now(timezone.utc).isoformat()
        if progress:
            progress(phases.copy())

    assets = {}
    completeness = {"status": "unavailable"}
    corrections = []
    audited_edits = []
    verification_executions = []
    quality = extraction.get("multilingual_validation", {})
    if extraction.get("source_review_approved"):
        quality = {**quality, "source_review_approved": True}
    staged = xml_path.with_suffix(".tmp")
    xml_started = monotonic()
    stage("validation", "running", xml_started)
    try:
        if not extraction.get("ok"):
            raise ValueError("The source PDF extraction did not pass validation.")
        ir.meta["asset_stem"] = stem
        locations = {}
        data = to_xml(ir, settings, locations=locations, assets=assets)
        staged.write_bytes(data)
        validation["xml"].update(validate_xml(staged.read_bytes(), len(ir.pages), settings))
        if not validation["xml"]["well_formed"] or validation["xml"]["issues"]:
            raise ValueError("The document XML failed structural validation.")
        unicode_check = verify_unicode(data, ir, locations)
        quality = {**quality, "xml": unicode_check}
        if unicode_check["unconfirmed_nodes"] and quality.get("status") != "manual_review_required":
            quality["status"] = "validation_partial"
        if assets:
            data = materialize_assets(data, assets, ir.path, folder)
            staged.write_bytes(data)
        asset_report = inspect_assets(data, folder)
        validation["xml"]["assets"] = asset_report
        validation["xml"]["source_geometry_review"] = [
            {"page": page.n, "reason": reason, "status": "MANUAL_REVIEW_REQUIRED"}
            for page in ir.pages for reason in page.diag.get("equation_review", [])
        ]
        if not asset_report["valid"]:
            raise ValueError("The generated XML refers to unresolved image assets.")
        save_original_images(data, ir, locations, folder, asset_report)
        completeness = compare_content(data, ir, locations, sample=settings.schema_profile == "sample")
        quality["completeness"] = {"status": completeness["status"], "issues": len(completeness["issues"]),
                                   "source_review": len(completeness["source_review"])}
        if completeness["status"] == "review_required" and quality.get("status") != "manual_review_required":
            quality["status"] = "validation_partial"
        if completeness["issues"]:
            quality["blocking_pages"] = sorted(set(quality.get("blocking_pages", [])) |
                                               {row["page"] for row in completeness["issues"] if row["page"]})
        staged.replace(xml_path)
        outputs["xml"] = file_info(xml_path)
        validation["xml"]["valid"] = True
    except Exception:
        logging.getLogger(__name__).exception("XML export failed")
        result["error"] = "XML generation or validation failed."
        validation["xml"]["error"] = result["error"]
    finally:
        staged.unlink(missing_ok=True)
        stage("validation", "completed" if outputs["xml"] else "failed", xml_started)

    if outputs["xml"] and requires_manual_review(extraction, completeness):
        outputs["xml"]["draft"] = True
        result.update(status="manual_review_required",
                      error="The XML is a draft. Resolve source review and content differences before exporting the PowerPoint.")
        validation["pptx"]["status"] = "blocked_by_source_review"
    elif outputs["xml"] and xml_only:
        result["status"] = "xml_only"
        validation["pptx"]["status"] = "skipped"
    elif outputs["xml"]:
        pptx_started = monotonic()
        stage("pptx", "running", pptx_started)
        try:
            presentation = transform(deck_path)
            slide_count = validate_deck(deck_path, presentation["slides"])
            outputs["pptx"] = file_info(deck_path)
            validation["pptx"].update(valid=True, slides=slide_count)
            validation["pptx"]["slide_numbering"] = presentation.get("slide_numbering", [])
            quality = {**quality, "presentation": presentation_language_report(deck_path)}
            result.update({key: value for key, value in presentation.items()
                           if key not in {"status", "records", "verification_executions"}})
            records = presentation.get("records")
            if records is None:
                records = [*({**item, "action": "applied"} for item in presentation["applied"]),
                           *({**item, "action": "review"} for item in presentation["review"])]
            audited_edits = [item for item in records if item.get("action") == "applied"]
            verification_executions = presentation.get("verification_executions", [])
            corrections = [correction_record(report["document_id"], item) for item in records]
            result["presentation_status"] = presentation["status"]
            result["status"] = "completed"
        except Exception:
            logging.getLogger(__name__).exception("PowerPoint export failed")
            result.update(status="partial", error="PowerPoint generation or validation failed. The XML is available.")
            validation["pptx"]["error"] = result["error"]
            deck_path.unlink(missing_ok=True)
        finally:
            stage("pptx", "completed" if outputs["pptx"] else "failed", pptx_started)

    result["validation"] = validation
    result["content_validation"] = quality
    result["xml_assets"] = validation["xml"]["assets"]["files"] if outputs["xml"] else []
    originals = validation["xml"]["assets"].get("original_files", []) if outputs["xml"] else []
    result["archive"] = None
    reports = {
        "ocr_report.json": {"summary": extraction.get("processing_counts"), "pages": extraction.get("page_log", [])},
        "validation_report.json": validation,
        "correction_report.json": {"document_id": report["document_id"], "corrections": corrections},
        "document_profile.json": extraction.get("document_profile", {}),
        "multilingual_validation_report.json": quality,
        "reports/xml_validation_report.json": validation["xml"],
        "reports/content_completeness_report.json": {key: value for key, value in completeness.items() if key != "formatting"},
        "reports/formatting_report.json": completeness.get("formatting", {}),
        "reports/image_asset_report.json": validation["xml"].get("assets", {"valid": False}),
        "manifest.json": {"document_id": report["document_id"], "xml_profile": settings.schema_profile,
                          "status": result["status"], "outputs": {key: item["filename"] if item else None
                                                                 for key, item in outputs.items()},
                          "images": result["xml_assets"], "image_originals": originals},
    }
    evidence = extraction.get("execution_evidence", {})
    evidence = {**evidence, "executions": [*evidence.get("executions", []), *verification_executions]}
    reports.update(execution_reports(ir, evidence,
                                     xml_path.read_bytes() if outputs["xml"] else None,
                                     locations if outputs["xml"] else None,
                                     edits=audited_edits, analysis=result.get("analysis"),
                                     pptx=deck_path if outputs["pptx"] else None))
    complete = result["status"] == "completed"
    if complete or (outputs["xml"] and result["xml_assets"] and not outputs["pptx"]):
        archive = folder / f"{stem}.zip"
        temp_zip = archive.with_suffix(".tmp")
        deck_name = deck_path.name if complete else None
        files = [xml_path.name, *([deck_name] if deck_name else []), *result["xml_assets"], *originals]
        try:
            write_zip(temp_zip, folder, files)
            validation["zip"] = validate_archive(temp_zip, xml_path.name, deck_name)
            if not validation["zip"]["valid"]:
                raise ValueError("Image references are missing after extracting the ZIP.")
            reports["processing_report.json"] = {**result, "metadata": ir.meta,
                                                  "extraction_seconds": extraction.get("seconds")}
            validation["zip"]["entries"] = len(files) + len(reports)
            write_zip(temp_zip, folder, files, reports)
            final_check = validate_archive(temp_zip, xml_path.name, deck_name)
            if not final_check["valid"]:
                raise ValueError("Final ZIP validation failed.")
            temp_zip.replace(archive)
            result["archive"] = file_info(archive)
        except Exception:
            logging.getLogger(__name__).exception("ZIP export failed")
            message = "ZIP validation failed. The validated XML and its image folder are available."
            if complete:
                result.update(status="partial", error=message)
            validation["zip"] = {"valid": False, "error": message}
        finally:
            temp_zip.unlink(missing_ok=True)
    reports["processing_report.json"] = {**result, "metadata": ir.meta if ir else {},
                                          "extraction_seconds": extraction.get("seconds")}
    for name, values in reports.items():
        write_report(folder / name, values)
    return result


def write_zip(target, folder: Path, files: list[str], reports: dict | None = None) -> None:
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for relative in dict.fromkeys(files):
            if not relative_path(relative) or not (folder / relative).resolve().is_relative_to(folder.resolve()):
                raise ValueError("An archive member is outside its output folder.")
            archive.write(folder / relative, relative)
        for name, data in (reports or {}).items():
            archive.writestr(name, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

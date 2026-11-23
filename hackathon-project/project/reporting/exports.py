"""Publish versioned document outputs and a report for each export attempt."""
from __future__ import annotations

import json
import logging
import re
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from zipfile import ZIP_DEFLATED, ZipFile

import pymupdf

from powerpoint.package import validate_deck
from layout.verified import apply_verified_content
from xml_engine.assets import inspect_assets, relative_path, validate_archive
from quality.conformance import tree
from lxml import etree
from xml_engine.serializer import to_xml
from xml_engine.validate import validate_xml


def output_stem(filename: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", PureWindowsPath(filename).stem).strip(" .")
    stem = stem[:100].rstrip(" .") or "document"
    return f"_{stem}" if PureWindowsPath(stem).is_reserved() else stem


def write_report(path: Path, data: dict) -> None:
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


def materialize_assets(data: bytes, assets: dict, pdf_path: str, folder: Path) -> bytes:
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
            if fingerprint in seen:
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


def publish_document(folder: Path, stem: str, ir, settings, extraction: dict, report: dict,
                     transform, *, xml_only: bool = False) -> dict:
    """Write validated XML first, then publish the deck and the final status."""
    folder.mkdir(parents=True, exist_ok=False)
    xml_path = folder / f"{stem}.xml"
    deck_path = folder / f"{stem}_updated.pptx"
    validation = {"xml": {"valid": False}, "pptx": {"valid": False}}
    outputs = {"xml": None, "pptx": None}
    result = {**report, "output_folder": str(folder), "outputs": outputs, "applied": [], "review": [],
              "status": "failed", "xml_profile": settings.schema_profile}
    assets = {}
    corrections = []
    staged = xml_path.with_suffix(".tmp")
    try:
        if not extraction.get("ok"):
            raise ValueError("The source PDF extraction did not pass validation.")
        apply_verified_content(ir)
        ir.meta["asset_stem"] = stem
        data = to_xml(ir, settings, assets=assets)
        staged.write_bytes(data)
        validation["xml"].update(validate_xml(staged.read_bytes(), len(ir.pages), settings))
        if not validation["xml"]["well_formed"] or validation["xml"]["issues"]:
            raise ValueError("The document XML failed structural validation.")
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
        staged.replace(xml_path)
        outputs["xml"] = file_info(xml_path)
        validation["xml"]["valid"] = True
    except Exception:
        logging.getLogger(__name__).exception("XML export failed")
        result["error"] = "XML generation or validation failed."
        validation["xml"]["error"] = result["error"]
    finally:
        staged.unlink(missing_ok=True)

    if outputs["xml"] and xml_only:
        result["status"] = "xml_only"
        validation["pptx"]["status"] = "skipped"
    elif outputs["xml"]:
        try:
            presentation = transform(deck_path)
            slide_count = validate_deck(deck_path, presentation["slides"])
            outputs["pptx"] = file_info(deck_path)
            validation["pptx"].update(valid=True, slides=slide_count)
            validation["pptx"]["slide_numbering"] = presentation.get("slide_numbering", [])
            result.update({key: value for key, value in presentation.items() if key not in {"status", "records"}})
            records = presentation.get("records")
            if records is None:
                records = [*({**item, "action": "applied"} for item in presentation["applied"]),
                           *({**item, "action": "review"} for item in presentation["review"])]
            corrections = [correction_record(report["document_id"], item) for item in records]
            result["presentation_status"] = presentation["status"]
            result["status"] = "completed"
        except Exception:
            logging.getLogger(__name__).exception("PowerPoint export failed")
            result.update(status="partial", error="PowerPoint generation or validation failed. The XML is available.")
            validation["pptx"]["error"] = result["error"]
            deck_path.unlink(missing_ok=True)

    result["validation"] = validation
    result["xml_assets"] = validation["xml"]["assets"]["files"] if outputs["xml"] else []
    result["archive"] = None
    reports = {
        "ocr_report.json": {"summary": extraction.get("processing_counts"), "pages": extraction.get("page_log", [])},
        "validation_report.json": validation,
        "correction_report.json": {"document_id": report["document_id"], "corrections": corrections},
        "ai_execution_report.json": {
            "ocr": [{"page": page["page"], "attempts": page.get("ocr", {}).get("attempts", [])}
                    for page in extraction.get("page_log", [])],
            "semantic_comparison": result.get("analysis"),
        },
    }
    if result["status"] == "completed":
        archive = folder / f"{stem}.zip"
        temp_zip = archive.with_suffix(".tmp")
        files = [xml_path.name, deck_path.name, *result["xml_assets"]]
        try:
            write_zip(temp_zip, folder, files)
            validation["zip"] = validate_archive(temp_zip, xml_path.name, deck_path.name)
            if not validation["zip"]["valid"]:
                raise ValueError("Image references are missing after extracting the ZIP.")
            reports["processing_report.json"] = {**result, "metadata": ir.meta,
                                                  "extraction_seconds": extraction.get("seconds")}
            validation["zip"]["entries"] = len(files) + len(reports)
            write_zip(temp_zip, folder, files, reports)
            final_check = validate_archive(temp_zip, xml_path.name, deck_path.name)
            if not final_check["valid"]:
                raise ValueError("Final ZIP validation failed.")
            temp_zip.replace(archive)
            result["archive"] = file_info(archive)
        except Exception:
            logging.getLogger(__name__).exception("ZIP export failed")
            result.update(status="partial", error="The validated XML and PowerPoint are available. ZIP validation failed.")
            validation["zip"] = {"valid": False, "error": result["error"]}
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

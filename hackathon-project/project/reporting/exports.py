"""Publish versioned document outputs and a report for each export attempt."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path, PureWindowsPath
from zipfile import ZIP_DEFLATED, ZipFile

import pymupdf

from powerpoint.package import validate_deck
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
    staged = xml_path.with_suffix(".tmp")
    try:
        if not extraction.get("ok"):
            raise ValueError("The source PDF extraction did not pass validation.")
        data = to_xml(ir, settings, assets=assets)
        staged.write_bytes(data)
        validation["xml"].update(validate_xml(staged.read_bytes(), len(ir.pages), settings))
        if not validation["xml"]["well_formed"] or validation["xml"]["issues"]:
            raise ValueError("The document XML failed structural validation.")
        if assets:
            with pymupdf.open(ir.path) as pdf:
                for name, (page, box) in assets.items():
                    asset = folder / name
                    asset.parent.mkdir(parents=True, exist_ok=True)
                    pdf[page - 1].get_pixmap(dpi=180, clip=pymupdf.Rect(box), alpha=False).save(asset)
                    file_info(asset)
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
            result.update({key: value for key, value in presentation.items() if key not in {"status", "records"}})
            result["presentation_status"] = presentation["status"]
            result["status"] = "completed"
        except Exception:
            logging.getLogger(__name__).exception("PowerPoint export failed")
            result.update(status="partial", error="PowerPoint generation or validation failed. The XML is available.")
            validation["pptx"]["error"] = result["error"]
            deck_path.unlink(missing_ok=True)

    write_report(folder / "ocr_report.json", {"summary": extraction.get("processing_counts"),
                                              "pages": extraction.get("page_log", [])})
    write_report(folder / "validation_report.json", validation)
    result["validation"] = validation
    result["xml_assets"] = sorted(assets) if outputs["xml"] else []
    write_report(folder / "processing_report.json", {**result, "metadata": ir.meta if ir else {},
                                                      "extraction_seconds": extraction.get("seconds")})
    return result


def write_zip(target, folder: Path, files: list[str]) -> None:
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for relative in files:
            archive.write(folder / relative, relative)

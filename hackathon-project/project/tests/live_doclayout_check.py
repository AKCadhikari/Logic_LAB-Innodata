"""Exercise the local checkpoint, real OCR, and the review UI against existing fixtures."""
import argparse
from dataclasses import replace
from math import isclose
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from lxml import etree
from playwright.sync_api import expect, sync_playwright
from pptx import Presentation
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.orchestrator import process_pdf
from config.settings import Settings
from layout.doclayout import Detector, render
from utils.logs import AuditLog


CLASSES = """text title header footer page_number image image_caption image_footnote image_block
table table_caption table_footnote equation equation_block list ref_text aside_text page_footnote
code code_caption algorithm phonetic""".split()


def upload(page, base, pdf, engine, phase):
    page.get_by_role("button", name="Home", exact=True).click()
    settings = page.locator("#home [aria-labelledby=pdf-card-heading] .processing-settings")
    if settings.get_attribute("open") is None:
        settings.locator("summary").click()
    page.locator("#home-ocr-engine").select_option(engine)
    page.locator("#phase-select").select_option(phase)
    page.locator("#home-pdf-file").set_input_files(pdf)
    expect(page.locator("#pdf-selected-name")).to_have_text(pdf.name)
    inspection = page.locator("#pdf-inspection")
    if inspection.get_attribute("open") is None:
        inspection.locator("summary").click()
    expect(inspection.get_by_role("status")).to_have_text("Process the PDF first to see inspection results.")
    with page.expect_response(lambda response: response.url == base + "/api/documents"
                              and response.request.method == "POST") as submitted:
        page.get_by_role("button", name="Process PDF").click()
    assert submitted.value.status == 202, submitted.value.text()
    document_id = submitted.value.json()["id"]
    expect(page.locator("#pdf-inspection-results")).to_be_visible(timeout=360_000)
    address = base + "/api/documents/" + document_id
    result = page.request.get(address).json()
    assert result["phase"] == phase
    assert f"phase-{phase}" in result["submission_export"]["output_folder"].lower()
    return address, result


def check_document(page, args):
    page.on("dialog", lambda dialog: dialog.dismiss())
    page.goto(args.base)
    expect(page.locator("#layout-home-status")).to_have_text("Layout Detection: Ready", timeout=90_000)
    health = page.request.get(args.base + "/api/layout-health").json()
    assert health["device"] == "cpu"
    assert [health["classes"][str(i)] for i in range(len(CLASSES))] == CLASSES
    address, result = upload(page, args.base, args.single, "glm-ocr", args.phase)
    source, = result["pages"]
    assert_regions(source)
    inspection = page.locator("#pdf-inspection")
    expect(inspection.locator(".legend")).to_contain_text("DocLayout-YOLO")
    expect(inspection.locator(".layout-box")).to_have_count(len(source["layout_regions"]))
    inspection.get_by_role("button", name="Reading Order").click()
    expect(inspection.locator(".tb small")).to_have_count(len(source["blocks"]))
    expect(inspection.locator(".tb small").first).to_contain_text("1.")
    inspection.get_by_role("button", name="Metadata").click()
    expect(inspection.locator("dl")).to_contain_text(args.single.name)
    expect(inspection.locator(".sec")).to_have_text("Page 1 diagnostics")
    page.get_by_role("button", name="Review extracted document").click()
    expect(page.locator("#sbody")).to_contain_text("DocLayout-YOLO | Completed")
    expect(page.locator("#sbody")).to_contain_text("detections")
    expect(page.locator("#sbody")).to_contain_text("inference")
    saved = result["submission_export"]
    download_xml = page.get_by_role("button", name="Export XML", exact=True)
    if saved["status"] == "xml_only":
        with page.expect_download() as downloaded:
            download_xml.click()
        assert Path(downloaded.value.suggested_filename).suffix in {".xml", ".zip"}
    elif saved["status"] == "manual_review_required":
        expect(download_xml).to_be_disabled()
    page.get_by_role("button", name="Zoning", exact=True).click()
    box = page.locator("#viewer svg .layout-box").first
    expect(box).to_be_visible()
    box.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#region-inspector .region-detail")).to_contain_text("DocLayout-YOLO")
    page.locator("#show-boxes").uncheck()
    expect(page.locator("#viewer svg .layout-box")).to_have_count(0)
    page.locator("#show-boxes").check()
    page.get_by_role("button", name="Locate in source PDF").click()
    expect(page.locator("svg .highlight")).to_be_visible()
    page.get_by_role("button", name="Fullscreen").click()
    expect(page.locator("#fullscreen")).to_have_text("Exit fullscreen")
    page.locator("#fullscreen").click()
    page.locator("#vtabs").get_by_role("button", name="Reading order").click()
    expect(page.locator(".region-list button").first).to_contain_text("1.")
    orders = [block["reading_order"] for block in source["blocks"]]
    assert orders == sorted(orders) and len(set(orders)) == len(orders)
    page.get_by_role("button", name="Submit review").click()
    expect(page.locator("#sbar")).to_have_text("Review approved")
    with page.expect_download(timeout=90_000) as downloaded:
        page.locator("#dlpptx").click()
    with TemporaryDirectory() as folder:
        deck = Path(folder) / "export.pptx"
        downloaded.value.save_as(deck)
        assert len(Presentation(deck).slides) == 1
        with ZipFile(deck) as package:
            assert package.testzip() is None
    report = page.request.get(address + "/presentations").json()[0]
    assert all(report["validation"][kind]["valid"] for kind in ("xml", "pptx"))
    xml = page.request.get(address + "/presentations/" + report["id"] + "/xml")
    assert etree.fromstring(xml.body()).tag == "document"
    page.locator("#stabs [data-t=meta]").click()
    page.locator("#review-title").fill("Verified edited title")
    page.get_by_role("button", name="Re-edit").click()
    expect(page.locator("#sbar")).to_have_text("Review pending")
    expect(page.locator("#review-title")).to_have_value("Verified edited title")
    print("CPU checkpoint, all 22 class mappings, single-page zoning, reading order, XML, PPTX and re-edit passed.")


def assert_regions(page):
    regions = page["layout_regions"]
    assert regions and page["diag"]["layout"]["engine"] == "DocLayout-YOLO"
    for region in regions:
        assert region["page"] == page["n"] and CLASSES[region["class_id"]] == region["class_name"]
        assert 0 <= region["confidence"] <= 1
        x0, y0, x1, y1 = region["display_bbox"]
        assert 0 <= x0 < x1 <= page["w"] and 0 <= y0 < y1 <= page["h"]


def check_ocr(page, args, engine):
    _, result = upload(page, args.base, args.scan, engine, args.phase)
    assert result["jobs"][0]["ocr_selection"]["ocr_backend"] == engine
    assert len(result["pages"]) > 1
    for source in result["pages"]:
        assert_regions(source)
    scanned = next(source for source in result["pages"] if source["diag"].get("ocr_required"))
    evidence = scanned["diag"]["ocr"]
    print(engine, evidence["status"], [(attempt["status"], attempt["issues"]) for attempt in evidence["attempts"]],
          flush=True)
    assert scanned["ocr_engine"] == engine
    assert any(region["ocr_text"] and engine in region["ocr_engines"] for region in scanned["layout_regions"])
    print(f"{engine} passed multi-page detection and region association.", flush=True)


def check_coordinates_and_missing_checkpoint(pdf):
    settings = Settings.load()
    with TemporaryDirectory() as scratch:
        folder = Path(scratch)
        with pymupdf.open(pdf) as source, Detector(settings, folder) as detector:
            detector.start()
            page = source[0]
            page.set_rotation((page.rotation + 90) % 360)
            regions = detector.detect(render(page, folder, settings.doclayout_image_size))
            assert regions
            for region in regions:
                display = pymupdf.Rect(region["bbox"]) * page.rotation_matrix
                assert all(isclose(a, b, abs_tol=.001) for a, b in zip(display, region["display_bbox"]))
        unavailable = replace(settings, doclayout_checkpoint=str(folder / "missing.pt"),
                              docling_enabled=False, ocr_enabled=False)
        result = process_pdf(pdf.resolve(), unavailable, folder / "output", folder,
                             AuditLog(folder / "audit.jsonl"))
        page, = result["page_log"]
        assert result["ok"] and page["review_required"] and page["layout"]["status"] == "failed"
        assert "Checkpoint missing" in page["layout"]["error"]
    print("Rotated PDF coordinates round-tripped; missing checkpoint flagged and XML validated.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--phase", choices=["1", "2"], default="1")
    parser.add_argument("--single", type=Path, required=True)
    parser.add_argument("--scan", type=Path, required=True)
    parser.add_argument("--engine", action="append", choices=["glm-ocr", "paddleocr"])
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome")
        page = browser.new_page(accept_downloads=True, viewport={"width": 1560, "height": 960})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        check_document(page, args)
        for engine in args.engine or ("glm-ocr", "paddleocr"):
            check_ocr(page, args, engine)
        assert not errors, errors
        browser.close()
    check_coordinates_and_missing_checkpoint(args.single)


if __name__ == "__main__":
    main()

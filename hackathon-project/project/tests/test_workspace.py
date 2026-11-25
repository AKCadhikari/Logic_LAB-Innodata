from __future__ import annotations

import io
import json
import time
from pathlib import Path
from zipfile import ZipFile

import pymupdf
import pytest
from lxml import etree
from pptx import Presentation

from ui.server import CFG, app, configure_workspace


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    root = tmp_path_factory.mktemp("workspace")
    configure_workspace(root)
    app.extensions["workspace"].settings.ollama_enabled = False
    app.extensions["workspace"].settings.docling_enabled = False
    app.config["TESTING"] = True
    with pytest.MonkeyPatch.context() as patch, app.test_client() as client:
        for key in ("run", "input", "output"):
            patch.setitem(CFG, key, root / key)
        yield client, root
    app.extensions["workspace"].worker.shutdown()


def pdf_with_text(text: str) -> bytes:
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Quarterly review", fontsize=20)
        page.insert_text((72, 130), text)
        return document.tobytes()


def upload(client, data: bytes, filename="report.pdf", previous=None):
    form = {"pdf": (io.BytesIO(data), filename)}
    if previous:
        form["previous_id"] = previous
    response = client.post("/api/documents", data=form)
    assert response.status_code == 202, response.data
    return "/api/documents/" + response.json["id"]


def wait_for_attempt(client, url):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = client.get(url).json
        if result["jobs"][0]["status"] in ("completed", "failed"):
            return result
        time.sleep(0.05)
    pytest.fail("The processing attempt did not finish.")


def test_upload_review_export_compare_and_sync(workspace):
    client, root = workspace
    assert client.get("/api/documents").status_code == 200
    assert client.get("/api/summary").status_code == 200
    before = "The report records an audited total of 240 units."
    after = "The report records an audited total of 260 units."
    old_data = pdf_with_text(before)
    old = upload(client, old_data)
    old_doc = wait_for_attempt(client, old)
    assert old_doc["jobs"][0]["status"] == "completed", old_doc
    new = upload(client, pdf_with_text(after), previous=old_doc["id"])
    doc = wait_for_attempt(client, new)
    assert doc["jobs"][0]["status"] == "completed", doc
    assert doc["previous_id"] == old_doc["id"] and doc["id"] != old_doc["id"]
    assert client.get(old + "/pdf").data == old_data
    assert (root / doc["id"] / "source.pdf").exists()
    assert doc["pages"][0]["source"] == "native"
    execution = client.get(new + "/execution-audit")
    assert execution.status_code == 200
    assert execution.json["model_usage_report.json"]["requests"] == 0
    trace = execution.json["processing_trace.json"]
    assert trace["pages"][0]["method"] == "NATIVE_EXTRACTION_USED"
    assert trace["xml_sha256"] and any(block["xml_corresponds_to_ir"] for block in trace["blocks"])
    download = client.get(new + "/execution-audit?report=ai_execution_report.json")
    assert download.status_code == 200 and "attachment" in download.headers["Content-Disposition"]
    assert doc["processing_counts"] == {"required": 0, "glm_ocr": 0, "paddle_ocr": 0, "failed": 0, "review": 0}
    assert doc["title"]["bbox"] and doc["title"]["page"] == 1
    assert str(root) not in client.get(new).text
    assert client.get(new + "/page/1.png").data.startswith(b"\x89PNG")
    assert client.get(new + "/page/99.png").status_code == 404
    assert client.get(new + "/xml?download=1").status_code == 409
    assert client.patch("/api/profile", json={"name": "Fixture reviewer"}).json["name"] == "Fixture reviewer"

    response = client.patch(new + "/metadata", json={"title": "Reviewed title", "lang": "de", "approved": True})
    assert response.status_code == 200 and response.json["approved"]
    assert response.json["review"]["reviewer"] == "Fixture reviewer"
    response = client.get(new + "/xml?download=1")
    assert response.status_code == 200
    document_xml = etree.fromstring(response.data)
    assert document_xml.get("lang") == "de"
    assert document_xml.findtext("page/h1") == "Reviewed title"
    assert document_xml.find("page").get("n") == "1"

    delta = client.get(new + "/compare", query_string={"previous_id": old_doc["id"], "include_analysis": 1})
    assert delta.status_code == 200
    assert delta.json["analysis"]["status"] == "disabled"
    change = next(change for change in delta.json["changes"] if change["old_content"] == before)
    assert change["new_content"] == after and change["numerical_change"] == {"before": ["240"], "after": ["260"]}
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    from pptx.util import Inches

    text_frame = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1)).text_frame
    run = text_frame.paragraphs[0].add_run()
    run.text, run.font.bold = before, True
    other_slide = deck.slides.add_slide(deck.slide_layouts[6])
    other_slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1)).text = "Keep this slide"
    contents = io.BytesIO()
    deck.save(contents)
    contents.seek(0)
    response = client.post(new + "/presentations", data={
        "previous_id": old_doc["id"], "pptx": (contents, "summary.pptx")
    })
    assert response.status_code == 201, response.data
    report = response.json
    assert report["status"] == "completed" and len(report["applied"]) == 1
    files = report["outputs"]
    folder = Path(report["output_folder"])
    assert folder.parent.name == "report"
    for kind, filename in (("xml", "report.xml"), ("pptx", "report_updated.pptx")):
        item = files[kind]
        path = folder / filename
        assert item == {"filename": filename, "path": str(path), "valid": True, "size_bytes": path.stat().st_size}
    for name in ("processing_report", "validation_report", "ocr_report"):
        assert json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))
    url = new + "/presentations/" + report["id"]
    xml = client.get(url + "/xml")
    assert xml.data == (folder / "report.xml").read_bytes()
    assert "report.xml" in xml.headers["Content-Disposition"]
    exported_xml = etree.fromstring(xml.data)
    assert exported_xml.findtext("header/title") == "Reviewed title"
    assert exported_xml.find(".//page").get("num") == "1"
    assert exported_xml.get("lang") == "german"
    exported = client.get(url)
    assert exported.status_code == 200
    final = Presentation(io.BytesIO(exported.data))
    assert final.slides[0].shapes[0].text == after
    assert final.slides[0].shapes[0].text_frame.paragraphs[0].runs[0].font.bold
    assert final.slides[1].shapes[0].text == "Keep this slide"
    assert "report_updated.pptx" in exported.headers["Content-Disposition"]
    with ZipFile(io.BytesIO(client.get(url + "/zip").data)) as archive:
        assert archive.read("report.xml") == xml.data
        assert archive.read("report_updated.pptx") == exported.data
    assert client.get(new + "/presentations").json[0]["outputs"] == files
    assert client.get(old + "/presentations/" + report["id"] + "/xml").status_code == 404
    other = app.test_client()
    assert other.get("/api/documents").json == []
    assert other.get("/api/summary").status_code == 404
    for suffix in ("", "/xml", "/zip"):
        assert other.get(url + suffix).status_code == 404

    repair = io.BytesIO()
    deck.save(repair)
    repair.seek(0)
    response = client.post(new + "/presentations", data={"mode": "source", "pptx": (repair, "summary.pptx")})
    assert response.status_code == 201, response.data
    result = response.json
    assert result["mode"] == "source" and result["status"] == "completed" and result["presentation_status"] == "updated"
    assert result["output_folder"] != report["output_folder"]
    assert len(result["applied"]) == 1 and result["applied"][0]["source"]["page"] == 1
    evidence = client.get(f'{new}/presentations/{result["id"]}/evidence.xml')
    corrected = etree.fromstring(evidence.data).xpath(result["applied"][0]["source"]["xml_node"])[0]
    assert "".join(corrected.itertext()) == after
    assert client.post(new + "/retry").status_code == 202
    assert wait_for_attempt(client, new)["jobs"][0]["status"] == "completed"
    assert client.get(url).data == exported.data
    assert client.get(url + "/xml").data == xml.data


def test_invalid_deck_preserves_verified_xml(workspace):
    client, _ = workspace
    url = upload(client, pdf_with_text("The extraction supplies verified document evidence."), "actual.pdf")
    assert wait_for_attempt(client, url)["jobs"][0]["status"] == "completed"
    result = client.post(url + "/presentations", data={"mode": "source", "pptx": (io.BytesIO(b"bad archive"), "bad.pptx")})
    assert result.status_code == 422
    report = result.json
    assert report["status"] == "partial" and report["outputs"]["pptx"] is None
    xml = Path(report["outputs"]["xml"]["path"])
    assert xml.name == "actual.xml" and xml.is_file()
    saved = json.loads((xml.parent / "validation_report.json").read_text(encoding="utf-8"))
    assert saved["xml"]["valid"] and not saved["pptx"]["valid"]
    download = url + "/presentations/" + report["id"]
    assert client.get(download + "/xml").data == xml.read_bytes()
    assert client.get(download).status_code == client.get(download + "/zip").status_code == 404


def test_invalid_xml_stops_powerpoint_export(workspace, monkeypatch):
    from reporting import exports
    from powerpoint import amend

    client, _ = workspace
    url = upload(client, pdf_with_text("Invalid export XML is never published."), "invalid.xml.pdf")
    assert wait_for_attempt(client, url)["jobs"][0]["status"] == "completed"
    monkeypatch.setattr(exports, "to_xml", lambda *args, **kwargs: b"<invalid")
    monkeypatch.setattr(amend, "repair_presentation", lambda *args: pytest.fail("Deck work must wait for valid XML."))
    response = client.post(url + "/presentations", data={"mode": "source", "pptx": (io.BytesIO(b"PK"), "deck.pptx")})
    assert response.status_code == 422 and response.json["status"] == "failed"
    assert response.json["outputs"] == {"xml": None, "pptx": None}
    folder = Path(response.json["output_folder"])
    assert not list(folder.glob("*.xml")) and not list(folder.glob("*.pptx"))


def test_reject_invalid_upload_and_retry_corrupt_pdf(workspace):
    client, _ = workspace
    invalid = client.post("/api/documents", data={"pdf": (io.BytesIO(b"not a PDF"), "report.pdf")})
    assert invalid.status_code == 400
    assert client.post("/api/documents", headers={"Origin": "https://other.example"}).status_code == 403
    url = upload(client, b"%PDF-1.7\ncorrupt", "broken.pdf")
    first = wait_for_attempt(client, url)
    assert first["jobs"][0]["status"] == "failed"
    assert first["jobs"][0]["error"]
    assert client.post(url + "/retry").status_code == 202
    second = wait_for_attempt(client, url)
    assert [job["status"] for job in second["jobs"]] == ["failed", "failed"]
    assert second["jobs"][1]["id"] == first["jobs"][0]["id"]
    assert client.get(url + "/xml").status_code == 409


def test_reports_encrypted_pdf_without_exposing_local_paths(workspace):
    client, _ = workspace
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Confidential synthetic test")
        encrypted = pdf.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="fixture-password",
                                owner_pw="fixture-owner")
    url = upload(client, encrypted, "encrypted.pdf")
    document = wait_for_attempt(client, url)
    assert document["jobs"][0]["status"] == "failed"
    assert "requires a password" in document["jobs"][0]["error"]
    assert client.get(url + "/page/1.png").status_code == 422

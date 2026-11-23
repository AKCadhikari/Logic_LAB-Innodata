from __future__ import annotations

import io
import time

import pymupdf
import pytest
from lxml import etree
from pptx import Presentation

from ui.server import app, configure_workspace


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    root = tmp_path_factory.mktemp("workspace")
    configure_workspace(root)
    app.extensions["workspace"].settings.ollama_enabled = False
    app.config["TESTING"] = True
    with app.test_client() as client:
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
    assert len(response.json["applied"]) == 1
    exported = client.get(new + "/presentations/" + response.json["id"])
    assert exported.status_code == 200
    final = Presentation(io.BytesIO(exported.data))
    assert final.slides[0].shapes[0].text == after
    assert final.slides[0].shapes[0].text_frame.paragraphs[0].runs[0].font.bold
    assert final.slides[1].shapes[0].text == "Keep this slide"
    assert client.get(new + "/presentations").json[0]["id"] == response.json["id"]

    repair = io.BytesIO()
    deck.save(repair)
    repair.seek(0)
    response = client.post(new + "/presentations", data={"mode": "source", "pptx": (repair, "summary.pptx")})
    assert response.status_code == 201, response.data
    result = response.json
    assert result["mode"] == "source" and result["status"] == "updated"
    assert len(result["applied"]) == 1 and result["applied"][0]["source"]["page"] == 1
    evidence = client.get(f'{new}/presentations/{result["id"]}/evidence.xml')
    corrected = etree.fromstring(evidence.data).xpath(result["applied"][0]["source"]["xml_node"])[0]
    assert "".join(corrected.itertext()) == after


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

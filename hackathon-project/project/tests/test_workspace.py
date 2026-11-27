from __future__ import annotations

import io
import json
import time
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zipfile import ZipFile

import pymupdf
import pytest
from lxml import etree
from pptx import Presentation
from pptx.util import Inches

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
        client.get("/api/documents")
        yield client, root
    app.extensions["workspace"].worker.shutdown()


def pdf_with_text(text: str) -> bytes:
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Quarterly review", fontsize=20)
        page.insert_text((72, 130), text)
        return document.tobytes()


def upload(client, data: bytes, filename="report.pdf", previous=None, phase="1"):
    form = {"pdf": (io.BytesIO(data), filename)}
    if previous:
        form["previous_id"] = previous
    if phase != "1":
        form["phase"] = phase
    response = client.post("/api/documents", data=form)
    assert response.status_code == 202, response.data
    return "/api/documents/" + response.json["id"]


def wait_for_attempt(client, url):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = client.get(url).json
        if result["jobs"][0]["status"] in ("completed", "failed", "review"):
            return result
        time.sleep(0.05)
    pytest.fail("The processing attempt did not finish.")


def test_paired_job_reuses_inputs_after_review_and_exports_one_valid_zip(workspace, monkeypatch):
    from reporting import exports

    _, root = workspace
    client = app.test_client()
    client.get("/api/documents")
    ws = app.extensions["workspace"]
    source = "The regional inventory contains 260 units verified by the audit coordinator."
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(7), Inches(1))
    run = shape.text_frame.paragraphs[0].add_run()
    run.text = source.replace("260", "987")
    run.font.bold = True
    stream = io.BytesIO()
    deck.save(stream)
    original = stream.getvalue()
    original_compare = exports.compare_content

    def require_review(data, ir, locations, **options):
        result = original_compare(data, ir, locations, **options)
        result["source_review"].append({"page": 1, "kind": "source_numeric_disagreement"})
        return result

    with ThreadPoolExecutor(max_workers=1) as worker:
        monkeypatch.setattr(ws, "worker", worker)
        monkeypatch.setattr(ws.settings, "amend_render_validation", False)
        monkeypatch.setattr(exports, "compare_content", require_review)
        key = str(uuid4())

        def submit():
            return client.post("/api/paired-jobs", data={"upload_key": key, "phase": "2",
                "pdf": (io.BytesIO(pdf_with_text(source)), "unified-review.pdf"),
                "pptx": (io.BytesIO(original), "unified-original.pptx")})

        first = submit()
        assert first.status_code == 202, first.data
        document_id = first.json["id"]
        assert submit().json["id"] == document_id
        base = f"/api/documents/{document_id}"
        paused = wait_for_attempt(client, base)
        job, = paused["jobs"]
        assert job["status"] == "review", job
        assert paused["pages"] and job["final_export"]["xml_ready"]
        assert client.get(base + "/final-output").status_code == 409
        assert client.get("/api/history").json["presentations"] == []
        assert (root / document_id / "source.pptx").read_bytes() == original
        assert (root / "phase-2/input/unified-original.pptx").read_bytes() == original
        approval = client.patch(base + "/metadata", json={"title": "Quarterly review", "lang": "en", "approved": True})
        assert approval.status_code == 200, approval.data
        finished = wait_for_attempt(client, base)
        resumed, = finished["jobs"]
        assert resumed["id"] == job["id"] and resumed["status"] == "completed", resumed
        assert all(resumed["processing"]["phases"][stage]["status"] == "completed" for stage in (
            "pdf_extraction", "xml_generation", "assets", "validation", "pptx_analysis", "pptx_correction",
            "final_validation", "zip"))
        download = client.get(base + "/final-output")
        assert download.status_code == 200
        with ZipFile(io.BytesIO(download.data)) as archive:
            assert set(archive.namelist()) == {"unified-review.xml", "unified-review_repaired.pptx",
                                              "output_images/unified-review/"}
            corrected = Presentation(io.BytesIO(archive.read("unified-review_repaired.pptx")))
            assert corrected.slides[0].shapes[0].text == source
            assert corrected.slides[0].shapes[0].text_frame.paragraphs[0].runs[0].font.bold
        assert app.test_client().get(base + "/final-output").status_code == 404

        def block_content(data, ir, locations, **options):
            result = original_compare(data, ir, locations, **options)
            result["issues"].append({"page": 1, "kind": "missing_paragraph"})
            return result

        monkeypatch.setattr(exports, "compare_content", block_content)
        key = str(uuid4())
        other = submit()
        assert other.json["id"] != document_id
        other_url = f"/api/documents/{other.json['id']}"
        blocked = wait_for_attempt(client, other_url)["jobs"][0]
        assert blocked["status"] == "review"
        assert blocked["final_export"]["source_review"]["blocking_pages"] == [1]
        assert client.patch(other_url + "/metadata", json={"title": "Quarterly review", "lang": "en",
                                                            "approved": True}).status_code == 422
        assert client.get(other_url + "/final-output").status_code == 409


def test_deferred_pdf_upload_can_be_inspected_before_processing(workspace):
    client, _ = workspace
    original = pdf_with_text("Original sample pressure measurements were recorded before calibration.")
    response = client.post("/api/documents", data={"defer": "1", "pdf": (io.BytesIO(original), "inspect-first.pdf")})
    assert response.status_code == 202
    base = "/api/documents/" + response.json["id"]
    pending, = response.json["jobs"]
    assert pending["status"] == "uploaded"
    assert "pages" not in client.get(base).json
    assert client.get(base + "/pdf").data == original
    assert client.get(base + "/source-info").json["pages"][0]["n"] == 1
    assert client.get(base + "/page/1.png").mimetype == "image/png"
    assert client.get(base + "/xml").status_code == 409
    assert app.test_client().get(base + "/source-info").status_code == 404
    assert client.post(base + "/retry", json={}).status_code == 202
    result = wait_for_attempt(client, base)
    assert result["jobs"][0]["status"] == "completed"
    assert [job["id"] for job in result["jobs"]] == [pending["id"]]
    saved = result["submission_export"]
    assert saved["status"] in {"xml_only", "manual_review_required"}
    assert saved["outputs"]["xml"]["valid"]
    report, = client.get(base + "/presentations").json
    export = base + "/presentations/" + report["id"] + "/xml"
    preview = client.get(export + "?preview=1")
    assert preview.status_code == 200 and preview.mimetype == "text/plain"
    assert preview.data == client.get(export).data
    assert etree.fromstring(preview.data).tag == "document"


def test_export_xml_defaults_to_zip_when_there_is_a_picture(workspace):
    client, _ = workspace
    with pymupdf.open() as art:
        sample = art.new_page(width=180, height=120)
        sample.draw_circle((90, 60), 47, fill=(0.15, 0.30, 0.75))
        picture = sample.get_pixmap().tobytes("png")
    with pymupdf.open(stream=pdf_with_text("The source document contains a diagram.")) as source:
        source[0].insert_image(pymupdf.Rect(72, 200, 252, 320), stream=picture)
        url = upload(client, source.tobytes(), "picture report.pdf")
    assert wait_for_attempt(client, url)["jobs"][0]["status"] == "completed"
    assert client.patch(url + "/metadata", json={
        "title": "Picture report", "lang": "en", "approved": True
    }).status_code == 200
    for profile in ("sample", "brief"):
        result = client.get(url + f"/xml?download=1&profile={profile}")
        assert result.mimetype == "application/zip"
        with ZipFile(io.BytesIO(result.data)) as archive:
            data = archive.read("picture report.xml")
            src, = etree.fromstring(data).xpath("//img/@src")
            assert src.startswith("output_images/picture report/")
            assert archive.read(src).startswith(b"\x89PNG\r\n\x1a\n")
            assert archive.testzip() is None
        assert client.get(url + f"/xml?download=1&profile={profile}&format=xml").data == data


def test_ocr_source_review_blocks_ppt_until_approved(workspace, monkeypatch):
    client, _ = workspace
    text = "The audited report records exactly 240 units."
    url = upload(client, pdf_with_text(text), "source-review.pdf")
    assert wait_for_attempt(client, url)["jobs"][0]["status"] == "completed"

    ws = app.extensions["workspace"]
    original_extracted = ws.extracted

    def unreviewed_ocr(document_id):
        ir, settings, extraction = original_extracted(document_id)
        for page in ir.pages:
            page.source = "ocr"
            page.requires_review = True
            for block in page.blocks:
                block.source = "ocr"
        return ir, settings, extraction

    monkeypatch.setattr(ws, "extracted", unreviewed_ocr)
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1)).text = text
    deck_stream = io.BytesIO()
    deck.save(deck_stream)
    deck_data = deck_stream.getvalue()

    def export_deck():
        return client.post(url + "/presentations", data={
            "mode": "source", "pptx": (io.BytesIO(deck_data), "source.pptx")
        })

    blocked = export_deck()
    assert blocked.status_code == 422 and blocked.json["status"] == "manual_review_required"
    assert blocked.json["outputs"]["xml"]["draft"] and blocked.json["outputs"]["pptx"] is None

    reviewed = client.patch(url + "/metadata", json={"title": "Reviewed source", "lang": "en", "approved": True})
    assert reviewed.status_code == 200 and reviewed.json["approved"]
    assert reviewed.json["phase_export"]["status"] == "xml_only"
    xml = client.get(url + "/xml?download=1&format=xml")
    assert xml.status_code == 200 and "_draft" not in xml.headers["Content-Disposition"]

    completed = export_deck()
    assert completed.status_code == 201 and completed.json["status"] == "completed"
    assert completed.json["source_review_approved"]


def test_ocr_preference_and_independent_job_snapshots(workspace):
    client, _ = workspace
    profile = "/api/ocr-settings"
    assert client.patch(profile, json={"engine": "paddleocr"}).json["engine"] == "paddleocr"
    assert client.get(profile).json["engine"] == "paddleocr"
    try:
        def submit(engine, language=""):
            return client.post("/api/documents", data={"pdf": (io.BytesIO(pdf_with_text(
                "The audited pressure remained within tolerance.")), "job.pdf"),
                "ocr_engine": engine, "ocr_language": language})

        first = submit("glm-ocr")
        second = submit("paddleocr", "fr")
        assert first.status_code == second.status_code == 202
        assert submit("unrecognised").status_code == 400
        assert client.get(profile).json["engine"] == "paddleocr"
        first_url = "/api/documents/" + first.json["id"]
        second_url = "/api/documents/" + second.json["id"]
        assert wait_for_attempt(client, first_url)["jobs"][0]["ocr_selection"]["ocr_backend"] == "glm-ocr"
        stored = wait_for_attempt(client, second_url)["jobs"][0]["ocr_selection"]
        assert stored["ocr_backend"] == "paddleocr" and stored["paddle_ocr_lang"] == "fr"
        assert client.post(first_url + "/retry", json={"ocr_engine": "automatic"}).status_code == 202
        history = wait_for_attempt(client, first_url)["jobs"]
        assert [job["ocr_selection"]["ocr_backend"] for job in history] == ["automatic", "glm-ocr"]
    finally:
        client.patch(profile, json={"engine": "automatic"})


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
    usage = execution.json["model_usage_report.json"]
    assert usage["requests"] == usage["responses"] == usage["used_responses"] == 1
    layout_execution = next(item for item in execution.json["ai_execution_report.json"]["executions"]
                            if item["provider"] == "doclayout-yolo")
    assert "USED_DOWNSTREAM" in layout_execution["statuses"]
    assert layout_execution["downstream"][0]["consumer"] == "layout_regions"
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
    assert document_xml.get("lang") == "german"
    assert document_xml.findtext("header/title") == "Reviewed title"
    assert document_xml.find("header/page").get("num") == "1"
    legacy = etree.fromstring(client.get(new + "/xml?profile=brief").data)
    assert legacy.get("lang") == "de" and legacy.findtext("page/h1") == "Reviewed title"
    packed = client.get(new + "/xml?download=1&format=zip")
    assert packed.status_code == 200
    with ZipFile(io.BytesIO(packed.data)) as archive:
        assert archive.testzip() is None
        assert archive.read(archive.namelist()[0]) == response.data

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
    assert (root / "phase-1" / "input" / "summary.pptx").is_file()
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


def test_phase_two_upload_is_archived_and_xml_is_published_to_phase_two(workspace):
    client, root = workspace
    url = upload(client, pdf_with_text("Phase two reports an audited total of 260 units."),
                 "phase-two.pdf", phase="2")
    document = wait_for_attempt(client, url)

    assert document["phase"] == "2"
    assert document["jobs"][0]["status"] == "completed", document["jobs"][0]
    assert (root / "phase-2" / "input" / "phase-two.pdf").is_file()
    export = document["submission_export"]
    assert export["phase"] == "2" and export["outputs"]["xml"]
    output = Path(export["outputs"]["xml"]["path"])
    assert output.is_file()
    assert output.is_relative_to(root / "phase-2" / "output")
    assert etree.parse(str(output)).getroot().tag == "document"

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    from pptx.util import Inches

    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1)).text = (
        "Phase two reports an audited total of 250 units.")
    contents = io.BytesIO()
    deck.save(contents)
    contents.seek(0)
    response = client.post(url + "/presentations", data={
        "mode": "source", "pptx": (contents, "phase-two.pptx")
    })
    assert response.status_code == 201, response.data
    assert (root / "phase-2" / "input" / "phase-two.pptx").is_file()
    deck_output = Path(response.json["outputs"]["pptx"]["path"])
    assert deck_output.is_file() and deck_output.is_relative_to(root / "phase-2" / "output")
    corrected = Presentation(deck_output)
    assert "260 units" in corrected.slides[0].shapes[0].text
    history_row = next(item for item in client.get("/api/history").json["pdfs"] if item["id"] == document["id"])
    link = history_row["pptx_download_url"]
    assert client.get(link).data == deck_output.read_bytes()
    assert app.test_client().get(link).status_code == 404
    deck_output.unlink()
    history_row = next(item for item in client.get("/api/history").json["pdfs"] if item["id"] == document["id"])
    assert "pptx_download_url" not in history_row


def test_standalone_pptx_job_needs_no_pdf_and_downloads_corrected_output(workspace, monkeypatch):
    client, root = workspace
    invalid = app.test_client().post("/api/presentations", data={
        "phase": "2", "pptx": (io.BytesIO(b"PKgarbage"), "invalid.pptx")
    })
    assert invalid.status_code == 400 and "valid PowerPoint" in invalid.json["error"]
    ws = app.extensions["workspace"]
    prior_enabled = ws.settings.ollama_enabled
    ws.settings.ollama_enabled = True
    thread_worker = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(ws, "worker", thread_worker)
    try:
        from powerpoint import proofread

        monkeypatch.setattr(proofread, "model_info", lambda _settings: {
            "ready": True, "model": "qwen3:test", "digest": "fixture-digest"})

        def response(_url, _endpoint, payload, _timeout):
            passages = json.loads(payload["messages"][1]["content"])
            item, = passages
            return {"model": payload["model"], "done": True, "done_reason": "stop",
                    "message": {"content": json.dumps({"corrections": [{
                        "paragraph_id": item["paragraph_id"],
                        "corrected_text": "The document contains a clear sentence.",
                        "issue": "spelling", "confidence": 0.99,
                    }]})}}

        monkeypatch.setattr(proofread.ollama, "request_json", response)
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        paragraph = slide.shapes.add_textbox(914400, 914400, 8000000, 900000).text_frame.paragraphs[0]
        run = paragraph.add_run()
        run.text = "The documet contains a clear sentnce."
        run.font.bold = True
        contents = io.BytesIO()
        deck.save(contents)
        deck_bytes = contents.getvalue()

        fresh = app.test_client()
        submitted = fresh.post("/api/presentations", data={
            "phase": "2", "pptx": (io.BytesIO(deck_bytes), "standalone.pptx")
        })
        assert submitted.status_code == 202, submitted.json
        job_id = submitted.json["id"]
        assert submitted.json["phase"] == "2"
        assert not fresh.get("/api/history").json["pdfs"]
        assert (root / "phase-2" / "input" / "standalone.pptx").is_file()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = fresh.get(f"/api/presentations/{job_id}").json
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert job["status"] == "completed", job
        assert job["result"]["validation"]["valid"]
        assert len(job["result"]["applied"]) == 1
        download = fresh.get(job["result"]["download_url"])
        assert download.status_code == 200
        corrected = Presentation(io.BytesIO(download.data))
        assert corrected.slides[0].shapes[0].text == "The document contains a clear sentence."
        assert corrected.slides[0].shapes[0].text_frame.paragraphs[0].runs[0].font.bold
        item = fresh.get("/api/history").json["presentations"][0]
        assert item["id"] == job_id and item["pptx_download_url"] == job["result"]["download_url"]
        unauthorized = app.test_client()
        unauthorized.set_cookie("docusync_workspace", "0" * 64)
        assert unauthorized.get(f"/api/presentations/{job_id}").status_code == 404
        assert unauthorized.get(f"/api/presentations/{job_id}/preview").status_code == 404
        assert unauthorized.get(f"/api/presentations/{job_id}/source").status_code == 404
        assert Presentation(io.BytesIO(fresh.get(f"/api/presentations/{job_id}/source").data)).slides

        pending_pdf = fresh.post("/api/documents", data={"defer": "1", "pdf": (io.BytesIO(
            pdf_with_text("The document contains a clear sentence.")), "paired.pdf")}).json
        document_id = pending_pdf["id"]
        form = {"document_id": document_id, "phase": "2"}
        wrong_owner = unauthorized.post("/api/presentations", data={**form, "pptx": (
            io.BytesIO(deck_bytes), "wrong-owner.pptx")})
        assert wrong_owner.status_code == 404
        paired = fresh.post("/api/presentations", data={**form, "pptx": (
            io.BytesIO(deck_bytes), "paired.pptx")})
        assert paired.status_code == 202 and paired.json["document_id"] == document_id
        thread_worker.shutdown(wait=True)
        pdf_row, = fresh.get("/api/history").json["pdfs"]
        assert pdf_row["pptx_download_url"] == f"/api/presentations/{paired.json['id']}/download"
        assert fresh.get(pdf_row["pptx_download_url"]).status_code == 200
    finally:
        thread_worker.shutdown(wait=True)
        ws.settings.ollama_enabled = prior_enabled

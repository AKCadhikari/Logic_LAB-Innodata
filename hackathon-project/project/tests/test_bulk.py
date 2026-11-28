from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from io import BytesIO
import json
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from uuid import uuid4
from zipfile import ZipFile

import pymupdf
import pytest
from lxml import etree
from pptx import Presentation
from pptx.util import Inches

from ui.server import app, configure_workspace
from xml_engine.assets import validate_archive

SOURCE = "The regional inventory contains 260 units verified by the audit coordinator."
BASE = "/api/bulk/batches"


def documents(*, encrypted=False):
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Quarterly review", fontsize=20)
        page.insert_text((72, 130), SOURCE)
        options = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "user_pw": "test"} if encrypted else {}
        pdf = document.tobytes(**options)
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    run = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(7), Inches(1)).text_frame.paragraphs[0].add_run()
    run.text = SOURCE.replace("260", "987")
    run.font.bold = True
    data = BytesIO()
    deck.save(data)
    return pdf, data.getvalue()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    settings = ("ollama_enabled", "ocr_enabled", "doclayout_enabled", "docling_enabled", "amend_render_validation")
    config.write_text(json.dumps(dict.fromkeys(settings, False)), encoding="utf-8")

    def open_workspace(*, process_pool=False):
        configure_workspace(tmp_path / "storage", str(config), output=tmp_path / "phase-1" / "output")
        ws = app.extensions["workspace"]
        if not process_pool:
            ws.worker.shutdown()
            ws.worker = ThreadPoolExecutor(max_workers=1)
        return ws

    monkeypatch.setitem(app.config, "TESTING", True)
    open_workspace()
    yield app.test_client(), open_workspace
    app.extensions["workspace"].worker.shutdown()


def stage(client, folders, phase="2", *, upload=True):
    client.get(BASE)
    key = uuid4().hex
    body = {"id": key, "phase": phase, "ocr_engine": "paddleocr",
            "folders": [{"name": folder, "files": [{"name": name, "size": len(data)} for name, data in files]}
                        for folder, files in folders]}
    response = client.post(BASE, json=body)
    assert response.status_code == 201, response.data
    url = f"{BASE}/{key}"
    batch = response.json
    assert client.post(BASE, json=body).json == batch
    if upload:
        for job, (_, files) in zip(batch["jobs"], folders):
            for entry, (name, data) in zip(job["files"], files):
                if entry["status"] == "pending":
                    path = f"{url}/files/{entry['id']}"
                    response = client.put(path, data={"file": (BytesIO(data), name)})
                    assert response.status_code == 200, response.data
                    assert client.put(path, data={"file": (BytesIO(data), name)}).json == response.json
    return url, client.get(url).json


def wait_for(client, url, condition):
    deadline = monotonic() + 30
    while monotonic() < deadline:
        data = client.get(url).json
        if condition(data):
            return data
        sleep(0.025)
    pytest.fail(f"Job did not settle: {data}")


def wait_batch(client, url):
    return wait_for(client, url, lambda batch: not (batch["counts"]["queued"] or batch["counts"]["processing"]))


def test_folder_upload_pairing_and_ownership(workspace):
    client, _ = workspace
    assert client.get("/bulk").status_code == 200
    assert b"Bulk Processing" in client.get("/").data
    pdf, deck = documents()
    valid = [("Different.PDF", pdf), ("Errata.PPTX", deck)]
    collection = [
        ("2019001878574X", valid), ("PL202611162", valid),
        ("Missing PDF", [("deck.pptx", deck)]), ("Missing PPTX", [("text.pdf", pdf)]),
        ("Unexpected", [*valid, ("notes.txt", b"notes")]),
        ("Two PDFs", [*valid, ("other.pdf", pdf)]), ("Two decks", [*valid, ("other.pptx", deck)]),
        ("Wrong type", [("fake.pdf", b"%PDF-invalid"), ("fake.pptx", pdf)]),
        ("Empty", []), ("Casefold", valid), ("CASEFOLD", valid),
    ]
    url, report = stage(client, collection)
    assert report["summary"] == {"folders": 11, "pdf": 9, "pptx": 9, "complete_pairs": 2,
                                 "missing_pdf": 2, "missing_pptx": 2, "invalid": 3, "review": 9, "pending": 0}
    assert [row["folder"] for row in report["jobs"]] == [folder for folder, _ in collection]
    assert [row["status"] for row in report["jobs"]] == ["ready"] * 2 + ["needs_review"] * 9
    assert "Multiple PDF" in report["jobs"][5]["issues"][0]
    assert "Multiple PPTX" in report["jobs"][6]["issues"][0]
    assert app.test_client().get(url).status_code == 404
    assert app.test_client().post(url + "/start").status_code == 404
    assert app.test_client().get(BASE).json["batches"] == []
    assert client.post(url + "/start").status_code == 202
    result = wait_batch(client, url)
    assert result["counts"]["completed"] == 2
    assert result["counts"]["total"] == 11
    assert client.get("/api/history").json["pdfs"] == []


def test_sequential_progress_failure_retry_and_downloads(workspace, monkeypatch):
    from agents import orchestrator
    from powerpoint import amend

    client, restart = workspace
    pdf, deck = documents()
    bad_pdf, _ = documents(encrypted=True)
    pair = [("source.pdf", pdf), ("error.pptx", deck)]
    url, _ = stage(client, [
        ("2019001878574X", pair), ("PL202611161", [("locked.pdf", bad_pdf), pair[1]]),
        ("PL202611162", [("retry.pdf", pdf), pair[1]]), ("20190016080822", pair), ("20259008801743", pair),
    ])
    ws = app.extensions["workspace"]
    first_progress, proceed = Event(), Event()
    process_pdf = orchestrator.process_pdf
    repair = amend.repair_presentation
    calls = []
    retry_id = client.get(url).json["jobs"][2]["id"]

    def track(*args, **kwargs):
        calls.append(kwargs["document_id"])
        original = kwargs["progress"]

        def progress(done, total):
            original(done, total)
            if len(calls) == 1 and done > 0:
                first_progress.set()
                assert proceed.wait(10)

        kwargs["progress"] = progress
        return process_pdf(*args, **kwargs)

    def fail_once(ir, deck, *args, **kwargs):
        if ir.doc_id == retry_id and calls.count(retry_id) == 1:
            raise ValueError("Injected PPTX failure")
        return repair(ir, deck, *args, **kwargs)

    monkeypatch.setattr("ui.workspace.process_pdf", track)
    monkeypatch.setattr(amend, "repair_presentation", fail_once)
    assert client.post(url + "/start").status_code == 202
    try:
        assert first_progress.wait(10)
        processing = client.get(url).json
        assert processing["counts"]["processing"] == 1 and processing["counts"]["queued"] == 4
        assert processing["jobs"][0]["stage"] and processing["jobs"][0]["pages"] == 1
        cancel_id = processing["jobs"][-1]["id"]
        assert client.post(f"{url}/jobs/{cancel_id}/cancel").json == {"canceled": 1}
        assert client.post(url + "/start").status_code == 202
        assert client.post(f"{url}/jobs/{processing['jobs'][0]['id']}/retry").status_code == 409
    finally:
        proceed.set()
    result = wait_batch(client, url)
    first, failed, partial, following, canceled = result["jobs"]
    assert [job["status"] for job in result["jobs"]] == ["completed", "failed", "partial", "completed", "canceled"]
    assert [job["id"] for job in result["jobs"][:-1]] == calls
    assert "encrypted" in failed["error"] and "PowerPoint" in partial["error"]
    assert set(partial["downloads"]) == {"xml"}
    assert canceled["downloads"] == {}
    assert all(Path(ws.phase_output_dir("2") / "bulk" / result["id"] / job["folder"]).is_dir()
               for job in (first, partial, following))
    assert result["jobs"][0]["started"] <= result["jobs"][0]["finished"]
    with ZipFile(BytesIO(client.get(result["download"]).data)) as zip_file:
        assert zip_file.testzip() is None
        assert set(zip_file.namelist()) == {
            "2019001878574X/source.xml", "2019001878574X/source_repaired.pptx", "PL202611162/retry.xml",
            "20190016080822/source.xml", "20190016080822/source_repaired.pptx", "bulk-report.json",
        }
        manifest = json.loads(zip_file.read("bulk-report.json"))
        assert manifest["jobs"][2]["status"] == "partial" and len(manifest["jobs"][2]["outputs"]) == 1
    xml_bytes = client.get(first["downloads"]["xml"]).data
    deck_bytes = client.get(first["downloads"]["pptx"]).data
    assert SOURCE in "".join(etree.fromstring(xml_bytes).itertext())
    repaired = Presentation(BytesIO(deck_bytes))
    assert repaired.slides[0].shapes[0].text == SOURCE
    assert repaired.slides[0].shapes[0].text_frame.paragraphs[0].runs[0].font.bold
    assert client.get(first["view"]).status_code == 200
    document = client.get(f"/api/documents/{first['id']}").json
    assert document["jobs"][0]["ocr_selection"]["ocr_backend"] == "paddleocr"
    assert client.get(f"/api/documents/{first['id']}/final-output").status_code == 200
    assert app.test_client().get(first["downloads"]["xml"]).status_code == 404
    assert client.post(f"{url}/jobs/{failed['id']}/retry").status_code == 202
    assert client.post(f"{url}/jobs/{partial['id']}/retry").status_code == 202
    recovered = wait_batch(client, url)
    assert recovered["jobs"][1]["status"] == "failed"
    assert recovered["jobs"][2]["status"] == "completed" and len(recovered["jobs"][2]["downloads"]) == 2
    assert len(list((ws.phase_output_dir("2") / "bulk" / result["id"] / partial["folder"]).iterdir())) == 2
    assert client.post(f"{url}/jobs/{first['id']}/retry").status_code == 409
    ws.worker.shutdown()
    before = list(calls)
    restart()
    assert client.get(url).json == recovered
    assert client.get(BASE).json["batches"][0]["id"] == result["id"]
    assert client.get(first["downloads"]["xml"]).data == xml_bytes
    assert before == calls

    paired = client.post("/api/paired-jobs", data={"upload_key": str(uuid4()), "phase": "2",
                         "ocr_engine": "paddleocr", "pdf": (BytesIO(pdf), "source.pdf"),
                         "pptx": (BytesIO(deck), "error.pptx")})
    assert paired.status_code == 202
    single_url = f"/api/documents/{paired.json['id']}"
    single = wait_for(client, single_url, lambda data: data["jobs"][0]["status"] in {"completed", "failed", "review"})
    assert single["jobs"][0]["status"] == "completed"
    with ZipFile(BytesIO(client.get(single_url + "/final-output").data)) as zip_file:
        assert etree.tostring(etree.fromstring(zip_file.read("source.xml"))) == etree.tostring(etree.fromstring(xml_bytes))
        with ZipFile(BytesIO(zip_file.read("source_repaired.pptx"))) as single_deck, ZipFile(BytesIO(deck_bytes)) as bulk_deck:
            assert single_deck.read("ppt/slides/slide1.xml") == bulk_deck.read("ppt/slides/slide1.xml")
    assert [row["id"] for row in client.get("/api/history").json["pdfs"]] == [paired.json["id"]]


def test_pending_upload_and_cancel_claimed_but_not_running(workspace):
    client, _ = workspace
    ws = app.extensions["workspace"]
    pdf, deck = documents()
    collection = [("PL202611165", [("sample.pdf", pdf), ("error.pptx", deck)])]
    url, info = stage(client, collection, phase="1", upload=False)
    assert info["summary"]["pending"] == 2
    assert client.post(url + "/start").status_code == 409
    release = Event()
    ws.worker.submit(release.wait, 10)
    try:
        url, _ = stage(client, collection, phase="1")
        assert client.post(url + "/start").status_code == 202
        assert client.post(url + "/cancel").json == {"canceled": 1}
        canceled = client.get(url).json["jobs"][0]
        assert canceled["status"] == "canceled" and not canceled["started"]
    finally:
        release.set()
    ws.worker.submit(lambda: None).result(timeout=10)
    assert client.post(f"{url}/jobs/{canceled['id']}/retry").status_code == 202
    result = wait_batch(client, url)
    assert result["counts"]["completed"] == 1
    assert (ws.phase_output_dir("1") / "bulk" / result["id"] / canceled["folder"]).is_dir()


def test_source_review_keeps_drafts_out_of_downloads_and_preserves_xml_assets(workspace, monkeypatch):
    from reporting import exports

    client, _ = workspace
    pdf, deck = documents()
    with pymupdf.open() as art:
        page = art.new_page(width=180, height=120)
        page.draw_circle((90, 60), 47, fill=(0.15, 0.30, 0.75))
        picture = page.get_pixmap().tobytes("png")
    with pymupdf.open(stream=pdf) as source:
        source[0].insert_image(pymupdf.Rect(72, 200, 252, 320), stream=picture)
        pdf = source.tobytes()

    compare = exports.compare_content

    def flag(data, ir, locations, **options):
        result = compare(data, ir, locations, **options)
        result["source_review"].append({"page": 1, "kind": "source_numeric_disagreement"})
        return result

    with monkeypatch.context() as patch:
        patch.setattr(exports, "compare_content", flag)
        url, _ = stage(client, [("PL202611161", [("illustrated.pdf", pdf), ("review.pptx", deck)])])
        assert client.post(url + "/start").status_code == 202
        waiting = wait_batch(client, url)
        job, = waiting["jobs"]
        assert job["status"] == "source_review" and not job["downloads"]
        assert job["source_review"]["items"] == [{"page": 1, "kind": "source_numeric_disagreement"}]
        assert client.get(url + "/download").status_code == 409

    response = client.patch(f"/api/documents/{job['id']}/metadata",
                            json={"title": "Quarterly review", "lang": "en", "approved": True})
    assert response.status_code == 200, response.data
    finished = wait_batch(client, url)
    job, = finished["jobs"]
    assert job["status"] == "completed"
    xml = client.get(job["downloads"]["xml"])
    assert xml.mimetype == "application/zip"
    assert validate_archive(BytesIO(xml.data), "illustrated.xml")["valid"]
    with ZipFile(BytesIO(client.get(finished["download"]).data)) as archive:
        data = archive.read("PL202611161/illustrated.xml")
        assets = etree.fromstring(data).xpath("//img/@src")
        assert assets
        for asset in assets:
            assert archive.read(f"PL202611161/{asset}").startswith(b"\x89PNG\r\n\x1a\n")
    assert client.post(f"/api/documents/{job['id']}/layout").status_code == 202
    assert wait_batch(client, url)["jobs"][0]["status"] == "completed"
    assert len(client.get(f"/api/documents/{job['id']}").json["jobs"]) == 2


def test_restart_recovers_pending_jobs_and_reports_interrupted_attempt(workspace, monkeypatch):
    client, restart = workspace
    pdf, deck = documents()
    pair = [("sample.pdf", pdf), ("error.pptx", deck)]
    url, _ = stage(client, [("Interrupted", pair), ("Waiting", pair)])
    ws = app.extensions["workspace"]
    abandoned = Future()
    monkeypatch.setattr(ws.worker, "submit", lambda *args, **kwargs: abandoned)
    assert client.post(url + "/start").status_code == 202
    snapshot = client.get(url).json
    assert snapshot["counts"]["queued"] == 2
    interrupted, waiting = [job["id"] for job in snapshot["jobs"]]
    ws.worker.shutdown()
    restart(process_pool=True)
    result = wait_batch(client, url)
    assert [job["id"] for job in result["jobs"]] == [interrupted, waiting]
    assert [job["status"] for job in result["jobs"]] == ["failed", "completed"]
    assert "interrupted" in result["jobs"][0]["error"]
    assert client.post(f"{url}/jobs/{interrupted}/retry").status_code == 202
    assert wait_batch(client, url)["counts"]["completed"] == 2

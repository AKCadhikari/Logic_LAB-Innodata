import json
from pathlib import Path

from lxml import etree
from pptx import Presentation

from main import main
from tests.test_workspace import pdf_with_text


def test_batch_publishes_both_outputs_and_preserves_previous_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUSYNC_DOCLING_ENABLED", "false")
    inputs = tmp_path / "input"
    inputs.mkdir()
    pdf = inputs / "document.pdf"
    pdf.write_bytes(pdf_with_text("The source report documents an audited total of 240 units."))
    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[6])
    original = inputs / "document_errors.pptx"
    deck.save(original)
    out, work = tmp_path / "output", tmp_path / "work"
    argv = ["--input", str(inputs), "--output", str(out), "--work-dir", str(work)]
    assert main(argv) == 0
    run_report = work / "run_report.json"
    row = json.loads(run_report.read_text(encoding="utf-8"))["per_document"][0]
    assert row["ok"] and row["status"] == "completed"
    xml, pptx = (Path(row["outputs"][kind]["path"]) for kind in ("xml", "pptx"))
    assert xml.parent == pptx.parent and xml.parent.parent == out / "document"
    assert xml.name == "document.xml" and pptx.name == "document_updated.pptx"
    assert etree.parse(xml).find(".//page").get("num") == "1"
    assert len(Presentation(pptx).slides) == 1
    original.unlink()
    assert main(argv) == 1
    row = json.loads(run_report.read_text(encoding="utf-8"))["per_document"][0]
    assert not row["ok"] and row["status"] == "partial"
    assert row["outputs"]["pptx"] is None and Path(row["outputs"]["xml"]["path"]).is_file()
    assert xml.is_file() and pptx.is_file() and Path(row["output_folder"]) != xml.parent

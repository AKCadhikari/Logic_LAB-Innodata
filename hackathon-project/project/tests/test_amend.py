import json, shutil
from pathlib import Path
from pptx import Presentation
from pptx.util import Inches, Pt
from tests.helpers import make_pdf, paragraph
from comparison.differ import align_correct, compare_documents
from powerpoint.amend import amend_deck, apply_text_edit
from collections import Counter

SRC = ("The Whale Optimization Algorithm guides each agent toward the best solution found so far. "
       "The population size was set to 150 in all experiments and the maximum number of iterations was 500.")


def vocab(t):
    return Counter(w.lower() for w in __import__("re").findall(r"\w+", t))


def test_typo_and_number_are_corrected_but_style_is_not():
    v = vocab(SRC)
    al = align_correct("The Whale Optimizaton Algorithm guides each agent toward the best solution found so far", SRC, v)
    assert "Optimization" in al.new_text and al.mode == "high"
    al = align_correct("The population size was set to 200 in all experiments and the maximum number of iterations was 500", SRC, v)
    assert "150" in al.new_text
    al = align_correct("The population size was set to 150 in all experiments, and the maximum number of iterations was 500!", SRC, v)
    assert "," in al.new_text and "!" in al.new_text                      # punctuation is never rewritten


def test_superscripts_and_non_latin_are_not_auto_corrected():
    v = vocab("area b2 and more words here for the vocabulary")
    assert align_correct("area b² and more words here for the vocabulary", "area b2 and more words here for the vocabulary", v).new_text.count("²") == 1
    src = "日本語の文章であり，言語を判定するために十分な長さがあります"
    al = align_correct("日本語の文章であり，言語を判定するために十分な長さがありました", src, vocab(src))
    assert al.new_text.endswith("ました")                                 # review-only for non-Latin text
    al = align_correct("売上は200万円でした", "売上は150万円でした", vocab("売上は150万円でした"))
    assert "150" in al.new_text                                          # numbers are still corrected


def test_apply_text_edit_preserves_run_formatting():
    prs = Presentation(); s = prs.slides.add_slide(prs.slide_layouts[6])
    tb = s.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1)); p = tb.text_frame.paragraphs[0]
    r1 = p.add_run(); r1.text = "Normal text with a "; r2 = p.add_run(); r2.text = "bolt"; r2.font.bold = True; r3 = p.add_run(); r3.text = " ending"
    apply_text_edit(p, "Normal text with a bold ending")
    runs = p.runs
    assert "".join(r.text for r in runs) == "Normal text with a bold ending"
    assert [r.font.bold for r in runs] == [None, True, None] and runs[1].text == "bold"


def _deck(path, footer_texts, slide_text, title="Method"):
    prs = Presentation(); layout = prs.slide_layouts[5]
    for i, ft in enumerate(footer_texts, 1):
        s = prs.slides.add_slide(layout); s.shapes.title.text = title
        tb = s.shapes.add_textbox(Inches(1), Inches(2), Inches(7), Inches(2)); tb.name = "box1"
        p = tb.text_frame.paragraphs[0]; r = p.add_run(); r.text = slide_text if i == 1 else "Other slide content that is not in the source at all."; r.font.bold = True
        f = s.shapes.add_textbox(Inches(1), Inches(6), Inches(7), Inches(0.5)); f.name = "footer"; f.text_frame.text = ft
    prs.save(str(path))


def test_end_to_end_deck_repair_and_report(tmp_path, settings, audit):
    pdf = make_pdf(tmp_path / "paper.pdf", [lambda pg: paragraph(pg, 100, SRC, size=11)])
    deck = tmp_path / "paper_errors.pptx"
    _deck(deck, ["Whale Optimization Algorithm Guided", "Whale Optimization Algorithm Guided by Networks", "Whale Optimization Algorithm Guided by Networks"],
          "The population size was set to 200 in all experiments and the maximum number of iterations was 500.")
    from agents.orchestrator import process_pdf
    process_pdf(pdf, settings, tmp_path / "out", tmp_path / "work", audit)
    out = tmp_path / "out" / "paper_transformed_QA.pptx"
    res = amend_deck(pdf, deck, out, tmp_path / "work", settings, audit)
    assert res["valid"] and res["slides"] == 3
    prs = Presentation(str(out)); s1 = prs.slides[0]
    box = next(sh for sh in s1.shapes if sh.name == "box1")
    assert "150" in box.text_frame.text and box.text_frame.paragraphs[0].runs[0].font.bold      # corrected, formatting kept
    assert next(sh for sh in s1.shapes if sh.name == "footer").text_frame.text == "Whale Optimization Algorithm Guided"
    changes = json.loads((tmp_path / "work" / "changes" / "paper_errors.json").read_text())
    for c in changes:
        assert {"document", "original_location", "new_location", "change_type", "old_content", "new_content", "confidence", "recommended_action"} <= set(c)
    assert any(c["change_type"] == "number_error" and c["action"] == "applied" for c in changes)
    correction = next(c for c in changes if c["action"] == "applied")
    assert correction["original_location"]["shape_id"] == box.shape_id
    from lxml import etree
    xml = etree.parse(str(tmp_path / "out" / "paper.xml"))
    assert "150" in "".join(xml.xpath(correction["source"]["xml_node"])[0].itertext())
    from quality.conformance import compare_pptx
    preservation = compare_pptx(deck, out)
    assert preservation["slide_order_match"]
    assert preservation["changed_slides"] == [1]
    assert not any(preservation["other_parts"].values())
    assert set(preservation["slides"][0]["diff_counts"]) == {"text"}
    assert deck.read_bytes() != out.read_bytes() and Presentation(str(deck))                  # input is never overwritten


def test_compare_documents_reports_added_deleted_modified():
    old = [{"text": "Alpha beta gamma delta epsilon", "page": 1, "idx": 0}, {"text": "To be removed entirely from here", "page": 1, "idx": 1}, {"text": "Stable paragraph about nothing", "page": 2, "idx": 2}]
    new = [{"text": "Alpha beta gamma delta epsilon zeta", "page": 1, "idx": 0}, {"text": "Brand new content appears now", "page": 3, "idx": 1}, {"text": "Stable paragraph about nothing", "page": 3, "idx": 2}]
    kinds = {c["change_type"] for c in compare_documents(old, new, "d")}
    assert kinds == {"modified", "deleted", "added", "moved"}


def test_version_sync_leaves_repeated_deck_paragraphs_for_review(tmp_path):
    from powerpoint.sync import synchronize

    before = "This document reports a total of 240 units."
    after = "This document reports a total of 260 units."
    deck = Presentation()
    for _ in range(2):
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(2)).text = before
    source, output = tmp_path / "source.pptx", tmp_path / "output.pptx"
    deck.save(source)
    report = synchronize(source, output, [{"category": "text", "change_type": "modified",
                                           "old_content": before, "new_content": after, "can_sync": True}])
    assert not report["applied"]
    assert report["review"][0]["reason"] == "More than one deck paragraph matches."
    assert all(slide.shapes[0].text == before for slide in Presentation(output).slides)


def test_batch_reports_invalid_deck_without_publishing_copy(tmp_path):
    from main import main

    folder = tmp_path / "input"
    folder.mkdir()
    make_pdf(folder / "paper.pdf", [lambda page: paragraph(page, 100, SRC, size=11)])
    source = folder / "paper_errors.pptx"
    source.write_bytes(b"Invalid deck.")
    out, work = tmp_path / "out", tmp_path / "work"
    code = main(["--input", str(folder), "--output", str(out), "--work-dir", str(work), "--no-ocr"])
    assert code == 1
    assert not list(out.glob("*.pptx"))
    assert source.read_bytes() == b"Invalid deck."
    report = json.loads((work / "run_report.json").read_text(encoding="utf-8"))
    assert report["amendments"][0]["status"] == "failed" and report["amendments"][0]["output"] is None

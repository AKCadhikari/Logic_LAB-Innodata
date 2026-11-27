import pymupdf
from lxml import etree
from tests.helpers import make_pdf, paragraph
from agents.orchestrator import process_pdf
from diagnostics.page_diagnostics import analyze_page, text_health
from layout.model import Line
from layout.reconstruct import xy_cut
from xml_engine.validate import validate_xml
from xml_engine.serializer import ALLOWED
from pathlib import Path

BODY = ("The system reads documents and reconstructs their structure. " * 6)


def _doc(tmp_path, settings, audit, draws, name="doc.pdf"):
    pdf = make_pdf(tmp_path / name, draws)
    out = tmp_path / "out"
    res = process_pdf(pdf, settings, out, tmp_path / "work", audit)
    return res, etree.parse(str(out / (pdf.stem + ".xml"))).getroot()


def page1(pg):
    pg.insert_text((60, 90), "Document Intelligence Study", fontsize=22, fontname="hebo")
    paragraph(pg, 120, BODY)
    pg.insert_text((60, 260), "1 Introduction", fontsize=14, fontname="hebo")
    paragraph(pg, 275, BODY)
    for i, t in enumerate(["first bullet point text", "second bullet point text"]):
        pg.insert_text((70, 420 + i * 16), "\u2022 " + t, fontsize=11)
    pg.insert_text((290, 815), "1", fontsize=9)


def page2(pg):
    paragraph(pg, 100, BODY)
    pg.insert_text((290, 815), "2", fontsize=9)


def test_digital_pdf_to_valid_xml(tmp_path, settings, audit):
    res, root = _doc(tmp_path, settings, audit, [page1, page2])
    assert res["ok"] and res["pages"] == 2
    assert [p.get("n") for p in root.findall("page")] == ["1", "2"]
    assert root.get("lang") == "en"
    assert {e.tag for e in root.iter() if isinstance(e.tag, str)} <= ALLOWED
    assert root.find(".//h1").get("kind") == "title" and "Intelligence" in "".join(root.find(".//h1").itertext())
    assert root.findall(".//ul/li") and "first bullet" in "".join(root.find(".//ul/li").itertext())
    assert root.find(".//footer") is not None or root.find(".//page[@n='2']/footer") is not None
    assert res["validation"]["issues"] == []


def test_two_column_reading_order():
    def L(t, x0, y0, x1, y1):
        return Line(text=t, bbox=(x0, y0, x1, y1))
    left = [L(f"L{i}", 50, 100 + 14 * i, 280, 112 + 14 * i) for i in range(5)]
    right = [L(f"R{i}", 320, 100 + 14 * i, 550, 112 + 14 * i) for i in range(5)]
    mixed = [x for pair in zip(left, right) for x in pair]
    regions = xy_cut(mixed, 12.0, 595, False)
    order = [l.text for r in regions for l in sorted(r, key=lambda l: l.bbox[1])]
    assert order == [l.text for l in left] + [l.text for l in right]
    rtl = [l.text for r in xy_cut(mixed, 12.0, 595, True) for l in sorted(r, key=lambda l: l.bbox[1])]
    assert rtl == [l.text for l in right] + [l.text for l in left]


def test_corrupted_text_layer_detected():
    h = text_health("\x01\x02\x03\x04 abc \x05\x06\x07\x08" * 10)
    assert h["ctrl"] > 0.3
    assert text_health("Perfectly normal English text here.")["ctrl"] == 0


def test_diagnostics_decisions(tmp_path, settings):
    pdf = make_pdf(tmp_path / "d.pdf", [page1, lambda p: None])
    d = pymupdf.open(str(pdf))
    assert analyze_page(d[0], settings)["decision"] == "native"
    blank = analyze_page(d[1], settings)
    assert blank["health"] == "absent" and blank["decision"] == "ocr"


def test_unmapped_arabic_pdf_uses_visual_ocr_and_keeps_rtl(tmp_path, settings, audit, monkeypatch):
    from ocr import engine
    from utils.textutil import script_of

    sentence = "هذا المستند يحتوي على معلومات في التقرير بتاريخ 28/3/1446."
    source = tmp_path / "arabic.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        pdf.xref_set_key(pdf.pdf_catalog(), "Lang", "(ar)")
        page.insert_htmlbox(page.rect + (45, 60, -45, -55), f'<p dir="rtl">{sentence}</p>')
        arabic = next(span for span in page.get_texttrace() if any(script_of(chr(char)) == "arab" for char, *_ in span["chars"]))
        font = next(font for font in page.get_fonts() if "Arabic" in font[3])
        _, reference = pdf.xref_get_key(font[0], "ToUnicode")
        glyphs = {glyph: char for char, glyph, *_ in arabic["chars"] if glyph > 0}
        glyphs[next(glyph for glyph, char in glyphs.items() if script_of(chr(char)) == "arab")] = 0xfffd
        pairs = "\n".join(f"<{glyph:04x}> <{char:04x}>" for glyph, char in glyphs.items())
        cmap = f"""begincmap
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
{len(glyphs)} beginbfchar
{pairs}
endbfchar
endcmap"""
        pdf.update_stream(int(reference.split()[0]), cmap.encode("ascii"))
        pdf.save(source)

    with pymupdf.open(source) as pdf:
        diagnostic = analyze_page(pdf[0], settings, "ar")
        assert diagnostic["native_unmapped_characters"] > 0
        assert diagnostic["decision"] == "ocr" and diagnostic["lang_hint"] == "ar"

    monkeypatch.setattr(engine, "available_langs", lambda _settings: {"ara", "eng"})
    monkeypatch.setattr(engine, "ocr_page", lambda *args, **options: (
        [Line(sentence, (120, 58, 540, 76), source="ocr", rtl=True, conf=94)], {"langs": "ara"}))
    settings.doclayout_enabled = settings.docling_enabled = False
    out = tmp_path / "out"
    result = process_pdf(source, settings, out, tmp_path / "work", audit)
    root = etree.parse(str(out / "arabic.xml")).getroot()
    assert result["ok"] and result["processing_counts"]["tesseract"] == 1
    assert not result["multilingual_validation"]["blocking_pages"]
    assert root.get("dir") == "rtl" and sentence in "".join(root.itertext())
    assert "\ufffd" not in "".join(root.itertext())


def test_never_emits_invalid_xml_for_hostile_text(tmp_path, settings, audit):
    def hostile(pg):
        pg.insert_text((60, 100), "ok text before <tag> & \"quotes\" and ampersands &amp; more words here", fontsize=11)
    res, root = _doc(tmp_path, settings, audit, [hostile])
    assert res["ok"]
    assert "<tag>" in "".join(root.itertext())


def test_unreadable_pdf_does_not_stop_batch(tmp_path, settings, audit):
    from agents.orchestrator import run_pdfs
    bad = tmp_path / "in" / "broken.pdf"; bad.parent.mkdir()
    bad.write_bytes(b"%PDF-1.4 this is not a real pdf")
    good = make_pdf(bad.parent / "good.pdf", [page1])
    results = run_pdfs([bad, good], settings, tmp_path / "out", tmp_path / "work", audit, 0.0)
    by = {r["doc_id"]: r for r in results}
    assert by["good"]["ok"] and not by["broken"]["ok"]
    assert (tmp_path / "out" / "broken.xml").exists() and (tmp_path / "out" / "good.xml").exists()


def test_ocr_fallback_on_image_only_page(tmp_path, settings, audit):
    settings.ocr_backend = "tesseract"
    from ocr.engine import available_langs
    if "eng" not in available_langs(settings):
        import pytest; pytest.skip("tesseract not installed")
    src = make_pdf(tmp_path / "t.pdf", [lambda p: paragraph(p, 100, "Scanned documents require optical character recognition to be readable.", size=16)])
    pix = pymupdf.open(str(src))[0].get_pixmap(dpi=200)
    d = pymupdf.open(); pg = d.new_page(width=595, height=842); pg.insert_image(pg.rect, pixmap=pix)
    img_pdf = tmp_path / "scan.pdf"; d.save(str(img_pdf)); d.close()
    res = process_pdf(img_pdf, settings, tmp_path / "out", tmp_path / "work", audit)
    txt = "".join(etree.parse(str(tmp_path / "out" / "scan.xml")).getroot().itertext()).lower()
    assert res["ocr_pages"] == 1 and "optical character recognition" in txt


def test_validate_flags_bad_numbering(settings):
    bad = b'<document lang="en"><page n="1"><p>x</p></page><page n="3"><blink>y</blink></page></document>'
    v = validate_xml(bad, 2, settings)
    assert v["well_formed"] and any("numbering" in i for i in v["issues"]) and any("vocabulary" in i for i in v["issues"])
    assert not validate_xml(b"<document><p></document>", 1, settings)["well_formed"]

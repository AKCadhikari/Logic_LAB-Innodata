import json
from zipfile import ZipFile

from lxml import etree
from pymupdf import LINK_URI, Rect
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

from layout.model import Block, Cell, DocIR, PageIR, Span
from powerpoint.advisory import review
from powerpoint.amend import repair_presentation
from powerpoint.formulas import office_tokens
from powerpoint.package import parse
from quality.repair_metrics import labeled_metrics
from tests.helpers import make_pdf, paragraph


def append_math(parent, text):
    office = etree.SubElement(parent, "{http://schemas.microsoft.com/office/drawing/2010/main}m")
    formula = etree.SubElement(office, qn("m:oMath"))
    node = etree.SubElement(etree.SubElement(formula, qn("m:r")), qn("m:t"))
    node.text = text


def paired_case(folder, settings, *, clean=False):
    good = "The station recorded a maximum flow of 240 units during the monitored period."
    url = "https://example.test/reports/field-measurements"
    cells = [Cell(value, row, col, header=row == 0)
             for row, values in enumerate([["Site", "Cost"], ["North", "120"], ["South", "170"]])
             for col, value in enumerate(values)]

    def draw_source(page):
        paragraph(page, 90, good)
        page.insert_text((60, 151), url)
        page.insert_link({"kind": LINK_URI, "from": Rect(60, 138, 325, 154), "uri": url})
        for row in range(4):
            page.draw_line((60, 210 + 30 * row), (300, 210 + 30 * row))
        for x in (60, 180, 300):
            page.draw_line((x, 210), (x, 300))
        for cell in cells:
            page.insert_text((65 + 120 * cell.col, 230 + 30 * cell.row), cell.text)
        page.insert_text((210, 350), "P=2×q+10")

    pdf = make_pdf(folder / "station.pdf", [draw_source])
    ir = DocIR("site", str(pdf), "en", [PageIR(1, 595, 842, [
        Block("p", good, bbox=(50, 75, 500, 125), kind="body"),
        Block("p", url, bbox=(60, 138, 325, 154), spans=[Span(url, link=url)]),
        Block("table", cells=cells, bbox=(60, 210, 300, 300)),
        Block("p", "P=2×q+10", bbox=(190, 330, 420, 360), kind="equation", marker="1",
              source_reference={"formula": {"status": "native_text", "encoding": "unicode"}})
    ], diag={"health": "healthy"})])

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    textbox = slide.shapes.add_textbox(Inches(.8), Inches(.6), Inches(8), Inches(.6))
    run = textbox.text_frame.paragraphs[0].add_run()
    run.font.size, run.font.bold = Pt(15), True
    run.text = good if clean else good.replace("240", "450")
    link = slide.shapes.add_textbox(Inches(.8), Inches(1.5), Inches(8), Inches(.6)).text_frame.paragraphs[0].add_run()
    link.text = url if clean else url.rsplit("/", 1)[0]
    link.hyperlink.address = link.text
    table = slide.shapes.add_table(3, 2, Inches(.8), Inches(2.1), Inches(4), Inches(1)).table
    for cell in cells:
        table.cell(cell.row, cell.col).text = "1700" if not clean and (cell.row, cell.col) == (2, 1) else cell.text
    equation = slide.shapes.add_textbox(Inches(.8), Inches(4), Inches(4), Inches(.7))
    append_math(equation.text_frame.paragraphs[0]._p, "P=2×q+10" if clean else "P=3×q+10")
    chart = CategoryChartData()
    chart.categories = ["North", "South"]
    chart.add_series("Cost", (120, 170 if clean else 17))
    slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(5), Inches(2.1), Inches(3.5), Inches(3), chart)
    before, after = folder / "original.pptx", folder / "corrected.pptx"
    deck.save(before)
    settings.amend_render_validation = False
    result = repair_presentation(ir, before, after, settings)
    return result, before, after


def test_text_table_hyperlink_and_native_equation_are_verified_in_saved_deck(tmp_path, settings):
    report, before, after = paired_case(tmp_path, settings)
    assert {row["change_type"] for row in report["applied"]} == {"number_error", "equation", "hyperlink", "table_cell"}
    chart, = [row for row in report["review"] if row["change_type"] == "chart_value"]
    assert chart["new"] == "170"
    assert report["presentation_validation"]["structure"]["preserved"]
    actual = Presentation(after)
    shapes = actual.slides[0].shapes
    assert "240 units" in shapes[0].text
    assert shapes[0].text_frame.paragraphs[0].runs[0].font.bold
    assert shapes[1].text.endswith("field-measurements")
    assert shapes[1].text_frame.paragraphs[0].runs[0].hyperlink.address.endswith("field-measurements")
    assert shapes[2].table.cell(2, 1).text == "170"
    with ZipFile(after) as package:
        root = parse(package.read("ppt/slides/slide1.xml"))
        assert office_tokens(next(root.iter(qn("m:oMath")))).tokens == list("P=2×q+10")
    assert (tmp_path / "corrected_reports/quality_report.html").is_file()
    qa = json.loads((tmp_path / "corrected_reports/pptx_validation.json").read_bytes())
    assert qa["summary"]["package_verified"] == 4
    assert before.read_bytes() != after.read_bytes()


def test_clean_objects_keep_every_package_part_unchanged(tmp_path, settings):
    report, before, after = paired_case(tmp_path, settings, clean=True)
    assert not report["applied"]
    assert not report["review"]
    with ZipFile(before) as left, ZipFile(after) as right:
        assert {name: left.read(name) for name in left.namelist()} == {name: right.read(name) for name in right.namelist()}


def test_advisory_rejects_fabricated_source_quote(monkeypatch, settings):
    settings.amend_ai_review = True
    records = [{"error_id": "E0001", "slide": 1, "shape_id": 2, "old": "The report records 45.",
                "action": "review", "source": {"text": "The report records 40."}}]
    monkeypatch.setattr("powerpoint.advisory.ollama.request_json",
                        lambda *args: {"done": True, "message": {"content": json.dumps({"assessments": [{
                            "error_id": "E0001", "classification": "incorrect", "source_quote": "The report records 100.",
                            "explanation": "Mismatch", "confidence": .99}]})}})
    assert review(records, settings)["status"] == "unavailable_or_invalid"
    assert "assistance" not in records[0]


def test_native_interval_needs_a_unique_source_context(tmp_path, settings):
    linked = "Variable k is bounded by the interval [-1, 1]."
    ambiguous = "Variable w is bounded by the interval [2, 7]."
    conflicting = "Variable w is bounded by the interval [3, 9]."
    contents = [linked, ambiguous, conflicting]

    def draw(page):
        for index, content in enumerate(contents):
            page.insert_text((60, 100 + 30 * index), content)

    pdf = make_pdf(tmp_path / "intervals.pdf", [draw])
    ir = DocIR("intervals", str(pdf), "en", [PageIR(1, 595, 842, [
        Block("p", content, kind="body", bbox=(55, 85 + 30 * index, 400, 110 + 30 * index))
        for index, content in enumerate(contents)
    ], diag={"health": "healthy"})])
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    for index, variable in enumerate(("k", "w")):
        p = slide.shapes.add_textbox(Inches(1), Inches(1 + index), Inches(8), Inches(.8)).text_frame.paragraphs[0]
        p.add_run().text = f"Variable {variable} is bounded by the interval "
        append_math(p._p, "[-3, 3]")
    original, output = tmp_path / "input.pptx", tmp_path / "output.pptx"
    deck.save(original)
    settings.amend_render_validation = False
    result = repair_presentation(ir, original, output, settings)
    applied, = result["applied"]
    assert applied["new"] == "[-1,1]"
    with ZipFile(output) as archive:
        nodes = parse(archive.read("ppt/slides/slide1.xml")).iter(qn("m:oMath"))
        assert ["".join(office_tokens(node).tokens) for node in nodes] == ["[-1,1]", "[-3,3]"]


def test_labeled_metrics_do_not_infer_precision_from_partial_labels():
    cases = [{"expected_error": True, "detected": True, "saved": False, "correct_after": False, "category": "chart"}]
    assert labeled_metrics(cases, exhaustive=False)["detection_precision"] is None
    evaluated = labeled_metrics(cases, exhaustive=True)
    assert evaluated["detection_precision"] == 1
    assert evaluated["remaining_labeled_errors"] == 1
    assert evaluated["correction_accuracy"] is None


def test_technical_document_repairs_words_citation_and_caption(tmp_path, settings):
    paragraphs = [
        "A facility uses a redundant measurement system to record daily inflow totals at each station.",
        "The experiment uses calibrated sensors to record the rate of change in the vertical channel.",
        "Papers in the controlled experiment use a sensitivity threshold described by Carter (2017).",
        "Figure 2. An automated controller measured voltage in the western panel during each test.",
    ]
    before_texts = [
        paragraphs[0].replace("redundant ", ""),
        paragraphs[1].replace("calibrated", "calibrated spurious"),
        paragraphs[2].replace("2017", "2029"),
        paragraphs[3].replace("voltage", "volatge"),
    ]
    boxes = [(50, 110, 280, 185), (310, 110, 545, 185), (50, 240, 280, 320), (310, 240, 545, 320)]

    def draw(page):
        for box, content in zip(boxes, paragraphs):
            page.insert_textbox(box, content, fontsize=11)

    pdf = make_pdf(tmp_path / "technical.pdf", [draw])
    blocks = [Block("p", item, kind="body", bbox=box) for item, box in zip(paragraphs[:3], boxes)]
    blocks.append(Block("figure", caption=paragraphs[3], kind="figure", bbox=boxes[3]))
    ir = DocIR("technical", str(pdf), "en", [PageIR(1, 595, 842, blocks, diag={"health": "healthy"})])
    deck = Presentation()
    for index in range(2):
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = "Facility Control Measurements"
        for y, item in enumerate(before_texts[index * 2:index * 2 + 2], 2):
            slide.shapes.add_textbox(Inches(.8), Inches(y), Inches(8), Inches(.7)).text = item
    original, output = tmp_path / "input.pptx", tmp_path / "output.pptx"
    deck.save(original)
    settings.amend_render_validation = False
    result = repair_presentation(ir, original, output, settings)
    fixed = Presentation(output)
    assert [shape.text for slide in fixed.slides for shape in list(slide.shapes)[1:]] == paragraphs
    assert {row["change_type"] for row in result["applied"]} == {"missing_words", "extra_text", "number_error", "character_error"}

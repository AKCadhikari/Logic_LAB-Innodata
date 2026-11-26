from lxml import etree

from config.settings import Settings
from diagnostics.profile import decorate_document
from layout.kinds import assign_kinds
from layout.model import Block, Cell, DocIR, Line, PageIR
from layout.reconstruct import _attach_captions, xy_cut
from layout.running import apply
from xml_engine.serializer import to_xml
from xml_engine.validate import validate_xml


def test_structured_ocr_recovers_running_components_without_losing_body():
    pages, sources = [], []
    for n in range(1, 4):
        lines = [
            Line("Operations Bulletin", (45, 20, 170, 30), size=9, bold=True),
            Line("Distribution Copy", (45, 754, 125, 764), size=9, bold=True),
            Line(str(30 + n), (244, 754, 254, 764), size=9, bold=True),
            Line("Revision B", (392, 754, 452, 764), size=9, bold=True),
            Line(f"This page contains a different instruction {n}.", (40, 62, 395, 74)),
        ]
        body = Block("p", f"Instruction recorded on this page.\n{lines[1].text}\n{lines[2].text}\n{lines[3].text}",
                     bbox=(40, 700, 452, 764), source="ocr", kind="body",
                     line_boxes=[(40, 700, 310, 712), *(line.bbox for line in lines[1:4])])
        pages.append(PageIR(n, 500, 800, [body], source="ocr"))
        sources.append(lines)
    apply(pages, sources, .075)
    for n, page in enumerate(pages, 1):
        assert [(block.tag, block.text) for block in page.blocks] == [
            ("header", "Operations Bulletin"), ("p", "Instruction recorded on this page."),
            ("footer", "Distribution Copy"), ("footer", str(30 + n)), ("footer", "Revision B"),
        ]
        assert page.blocks[1].bbox == (40, 700, 310, 712)
    doc = DocIR("bulletin", "bulletin.pdf", "en", pages)
    settings = Settings(schema_profile="sample")
    data = to_xml(doc, settings)
    assert not validate_xml(data, 3, settings)["issues"]
    xml = etree.fromstring(data)
    assert len(xml.findall(".//page_header")) == 3
    assert len(xml.findall(".//page_footer")) == 9


def test_persistent_gutter_preserves_order_across_aligned_column_gaps_and_spanning_heading():
    def line(text, x, y, width=185):
        return Line(text, (x, y, x + width, y + 11))

    heading = line("Spanning heading", 50, 22, 480)
    left = [line("Left first", 50, 60), line("Left second", 50, 160)]
    right = [line("Right first", 320, 60), line("Right second", 320, 160)]
    items = [right[0], left[1], heading, left[0], right[1]]
    assert [item for group in xy_cut(items, 11, 595, False) for item in group] == [heading, *left, *right]
    assert [item for group in xy_cut(items, 11, 595, True) for item in group] == [heading, *right, *left]


def test_numbered_ocr_headings_build_nested_sections_and_formula_is_not_a_language():
    headings = [Block("h1", "Field Study of Water Storage", size=24),
                Block("h1", "1 Measurements", size=15),
                Block("h2", "1.1 Instruments", size=12),
                Block("h2", "1.1.1 Calibration", size=11)]
    body = Block("p", "The team measured the flow of water through the channel and recorded the values in this report.")
    formula = Block("p", r"\cos(t) + \sin(t) = \int \cos(b)\, db", kind="equation")
    doc = DocIR("study", "study.pdf", pages=[PageIR(1, 500, 700, [*headings, body, formula])])
    assign_kinds(doc)
    decorate_document(doc)
    assert doc.lang == "en" and formula.lang == "und"
    settings = Settings(schema_profile="sample")
    data = to_xml(doc, settings)
    xml = etree.fromstring(data)
    assert not validate_xml(data, 1, settings)["issues"]
    assert xml.find(".//section/h2").text == "1 Measurements"
    assert xml.find(".//section/section/h3").text == "1.1 Instruments"
    assert xml.find(".//section/section/section/h4").text == "1.1.1 Calibration"


def test_captions_link_by_column_above_and_below_without_associating_equations():
    left = Block("figure", bbox=(40, 120, 230, 280))
    right = Block("figure", bbox=(265, 120, 460, 280))
    equation = Block("figure", kind="equation", bbox=(265, 284, 460, 296))
    a = Block("p", "Figure 1. Pump connection", bbox=(44, 100, 214, 111))
    b = Block("p", "Figure 2. Flow direction", bbox=(266, 304, 440, 320))
    assert _attach_captions([a, left, right, equation, b]) == [left, right, equation]
    assert left.caption == a.text and right.caption == b.text and not equation.caption


def test_sample_rejects_non_body_continuation_and_out_of_bounds_coordinates():
    pages = [PageIR(1, 500, 700, [Block("header", "Bulletin", bbox=(30, 20, 100, 30)),
                                Block("p", "The previous run found that", kind="body", bbox=(40, 630, 440, 650))]),
             PageIR(2, 500, 700, [Block("p", "the valve was closed.", kind="body", bbox=(40, 70, 440, 90))])]
    settings = Settings(schema_profile="sample")
    data = to_xml(DocIR("test", "test.pdf", "en", pages), settings)
    xml = etree.fromstring(data)
    assert not validate_xml(data, 2, settings)["issues"]
    continuation = xml.find(".//p[@continues_from]")
    assert continuation is not None
    continuation.set("continues_from", xml.find(".//page_header").get("id"))
    continuation.set("x1", "550")
    data = etree.tostring(xml, xml_declaration=True, encoding="UTF-8")
    issues = validate_xml(data, 2, settings)["issues"]
    assert any("preceding body paragraph" in issue for issue in issues)
    assert any("bounding box" in issue for issue in issues)


def test_table_continuation_links_positioned_tables_with_the_same_headers():
    def table(top, bottom, amounts):
        cells = [Cell("Account", 0, 0, header=True), Cell("Balance", 0, 1, header=True)]
        cells.extend(Cell(value, 1, index) for index, value in enumerate(amounts))
        return Block("table", bbox=(55, top, 450, bottom), cells=cells)
    pages = [PageIR(1, 500, 700, [table(480, 665, ["Reserve", "245"])]),
             PageIR(2, 500, 700, [table(55, 200, ["Supplies", "90"])])]
    settings = Settings(schema_profile="sample")
    data = to_xml(DocIR("ledger", "ledger.pdf", "en", pages), settings)
    assert not validate_xml(data, 2, settings)["issues"]
    first, second = etree.fromstring(data).findall(".//table")
    assert second.get("continues_from") == first.get("id")


def test_ocr_prose_markup_is_structural_and_multiplication_stays_literal():
    from ocr.glm import text_block

    block = text_block("p", r"We **measured** the *flow* using 2 * 4 and \(x^{2}\).", "body")
    assert block.text == "We measured the flow using 2 * 4 and x^{2}."
    assert [span.text for span in block.spans if span.bold] == ["measured"]
    assert [span.text for span in block.spans if span.italic] == ["flow"]
    assert [span.text for span in block.spans if span.formula] == ["x^{2}"]


def test_verified_display_replaces_only_its_matching_unlocated_ocr_copy():
    from layout.equations import reconcile_displays

    before = Block("p", "The following equation predicts the flow.", bbox=(40, 100, 440, 130))
    after = Block("p", "The next paragraph explains the boundary.", bbox=(40, 200, 440, 230))
    candidate = Block("p", r"\vec{V} = 12x^{2}", source="ocr", kind="equation")
    unrelated = Block("p", r"\vec{V} = 13x^{2}", source="ocr", kind="equation")
    marker = Block("p", "(7.2)", source="ocr")
    verified = Block("figure", r"\overrightarrow {V} = 1 2 x ^ {2}", bbox=(100, 150, 430, 169),
                     marker="7.2", kind="equation",
                     source_reference={"formula": {"status": "two_pass_agreement", "number": "7.2"}})
    page = PageIR(1, 500, 700, [before, candidate, marker, unrelated, after, verified])
    reconcile_displays(page)
    assert page.blocks == [before, verified, unrelated, after]
    assert verified.source_reference["alternate_transcriptions"] == [candidate.text]


def test_compound_image_groups_only_panels_without_independent_captions():
    from layout.region_association import merge_figure_panels

    left = Block("figure", bbox=(50, 100, 230, 250))
    right = Block("figure", bbox=(265, 105, 440, 247))
    region = {"class_name": "image_block", "bbox": (45, 95, 450, 260),
              "confidence": .88, "source": "DocLayout-YOLO"}
    page = PageIR(1, 500, 700, [left, right])
    merge_figure_panels(page, [region])
    combined, = page.blocks
    assert combined.bbox == (50, 100, 440, 250)
    assert combined.source_reference["figure_panels"] == [left.bbox, right.bbox]
    right.caption = "Figure 2. Independent panel"
    page.blocks = [left, right]
    merge_figure_panels(page, [region])
    assert page.blocks == [left, right]


def test_rotated_cropped_page_exports_source_coordinates_and_a_real_image(tmp_path):
    import pymupdf
    from PIL import Image

    from reporting.exports import materialize_assets

    path = tmp_path / "rotated.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=410, height=640)
        page.draw_rect((80, 140, 220, 220), fill=(.1, .2, .8))
        page.set_cropbox(pymupdf.Rect(25, 30, 385, 610))
        page.set_rotation(90)
        pdf.save(path)
    block = Block("figure", bbox=(55, 110, 195, 190))
    page = PageIR(1, 580, 360, [block], diag={"rotation": 90, "source_width": 360, "source_height": 580})
    assets = {}
    settings = Settings(schema_profile="sample")
    data = to_xml(DocIR("turn", str(path), "en", [page]), settings, assets=assets)
    assert not validate_xml(data, 1, settings)["issues"]
    generated = etree.fromstring(data)
    assert generated.find(".//page").get("width") == "360.00"
    image = generated.find(".//img")
    assert image.get("x0") == "55.00"
    materialize_assets(data, assets, str(path), tmp_path)
    with Image.open(tmp_path / image.get("src")) as pixels:
        assert all(abs(actual - expected) <= 1 for actual, expected in zip(pixels.size, (200, 350)))
        blue = pixels.getpixel((100, 175))[2]
        assert blue > pixels.getpixel((100, 175))[0]


def test_text_inferred_tables_do_not_emit_spacing_as_empty_rows():
    from layout.native import _remove_table_spacers

    cells = [Cell("Year", 0, 0), Cell("Balance", 0, 1), Cell("", 1, 0), Cell("", 1, 1),
             Cell("2026", 2, 0), Cell("245", 2, 1)]
    assert [(cell.row, cell.col, cell.text) for cell in _remove_table_spacers(cells)] == [
        (0, 0, "Year"), (0, 1, "Balance"), (1, 0, "2026"), (1, 1, "245")
    ]

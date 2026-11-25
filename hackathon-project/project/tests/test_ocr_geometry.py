from layout.model import Block, Line, PageIR
from layout.ocr_geometry import align_blocks


def line(text, top, conf=96):
    return Line(text, (45, top, 360, top + 10), 10, conf=conf, source="ocr")


def test_ocr_geometry_restores_masthead_even_with_issn_repeated_in_footer():
    title = "Heat Transfer Through a Ceramic Barrier"
    body = "Temperature readings changed gradually over the first hour. ISSN 4242-7516"
    blocks = [Block("p", title, source="ocr"), Block("p", body, source="ocr")]
    page = PageIR(1, 500, 700, blocks, source="ocr")
    masthead = ["Journal of Materials Testing", "ISSN 4242-7516", "Volume 5, Issue 2", "Copyright © 2026"]
    lines = [line(text, 30 + index * 12) for index, text in enumerate(masthead)] + [
        line("Heat Transfer Through", 105),
        line("a Ceramic Barrier", 120),
        line("Temperature readings changed gradually over the first hour.", 183),
        line("ISSN 4242-7516", 200),
    ]
    result = align_blocks(page, lines)
    assert result == {"aligned": 2, "recovered_headers": 1}
    header, heading, paragraph = page.blocks
    assert header.tag == "header" and header.text == "\n".join(masthead)
    assert header.ocr_engine == "tesseract" and header.bbox == (45, 30, 360, 76)
    assert heading.text == title and heading.bbox == (45, 105, 360, 130)
    assert paragraph.text == body


def test_repeated_or_uncorroborated_text_gets_no_guessed_box_or_header():
    text = "Quarterly lab notes"
    page = PageIR(1, 500, 700, [Block("p", text, source="ocr")], source="ocr")
    assert align_blocks(page, [line("Volume 3, Issue 2", 30), line("Copyright © 2026", 45),
                               line(text, 125), line(text, 400)]) == {"aligned": 0, "recovered_headers": 0}
    assert page.blocks[0].bbox is None

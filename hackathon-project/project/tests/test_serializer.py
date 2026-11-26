from lxml import etree
from layout.model import DocIR, PageIR, Block, Span, Cell
from xml_engine.serializer import to_xml


def _xml(blocks, settings, lang="en"):
    return etree.fromstring(to_xml(DocIR("d", "d.pdf", lang, [PageIR(1, 595, 842, blocks)]), settings))


def test_inline_formatting(settings):
    spans = [Span("H"), Span("2", sub=True), Span("O is "), Span("vital", bold=True), Span(" and "), Span("old", strike=True), Span(" x"), Span("2", sup=True)]
    b = Block(tag="p", text="H2O is vital and old x2", spans=spans, kind="body")
    p = _xml([b], settings).find(".//p")
    assert p.find("sub").text == "2" and p.find("strong").text == "vital" and p.find("del").text == "old" and p.find("sup").text == "2"
    assert "".join(p.itertext()) == "H2O is vital and old x2"


def test_table_structure_spans_and_header(settings):
    cells = [Cell("Group", 0, 0, colspan=2, header=True), Cell("C", 0, 2, header=True),
             Cell("a", 1, 0), Cell("b", 1, 1), Cell("tall", 1, 2, rowspan=2), Cell("c", 2, 0), Cell("d", 2, 1)]
    t = _xml([Block(tag="table", cells=cells, caption="Table 1. Demo")], settings).find(".//table")
    assert t.find("caption").text == "Table 1. Demo"
    assert t.find("thead/tr/th").get("colspan") == "2"
    assert t.xpath(".//td[@rowspan='2']")[0].text == "tall"
    assert len(t.findall("tbody/tr")) == 2


def test_figure_alt_and_caption_and_lang(settings):
    fig = Block(tag="figure", alt="Diagram of a pipeline", caption="Figure 1. Pipeline")
    ar = Block(tag="p", text="نص عربي", lang="ar", kind="body")
    r = _xml([fig, ar], settings)
    assert r.find(".//figure/img").get("alt") == "Diagram of a pipeline" and r.find(".//figcaption").text == "Figure 1. Pipeline"
    assert r.find(".//p").get("lang") == "ar"


def test_profile_and_lang_format(settings):
    settings.schema_profile = "example"; settings.lang_format = "name"
    r = _xml([Block(tag="p", text="x")], settings)
    assert r.get("lang") == "english" and r.find("page").get("num") == "1" and r.find("page").get("width")
def test_sample_profile_preserves_mixed_case_markup_and_line_boxes(settings):
    from xml_engine.validate import validate_xml

    settings.schema_profile = "sample"
    block = Block("p", "ALEX PATEL¹ and Ada Lee", bbox=(50, 80, 160, 105), kind="byline",
                  size=9, line_boxes=[(50, 80, 160, 89), (60, 95, 90, 105)],
                  spans=[Span("ALEX PATEL"), Span("¹"), Span(" and "), Span("Ada Lee", small_caps=True)])
    doc = DocIR("paper", "paper.pdf", "en", [PageIR(1, 500, 700, [block])])
    data = to_xml(doc, settings)
    node = etree.fromstring(data).find("header/p")
    assert node.get("sub_bboxes") == "50.00,80.00,160.00,89.00|60.00,95.00,90.00,105.00"
    assert [child.tag for child in node] == ["all-caps", "sup", "small-caps"]
    assert node.findtext("sup") == "1"
    assert "".join(node.itertext()) == block.text.replace("¹", "1")
    assert not validate_xml(data, 1, settings)["issues"]

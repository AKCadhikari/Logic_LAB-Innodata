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

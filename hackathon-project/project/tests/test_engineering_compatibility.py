from config.settings import Settings
from layout.model import Block, DocIR, PageIR
from layout.pseudocode import preserve_pseudocode
from layout.reconstruct import place_visuals
from ocr.glm import parse
from quality.xml_alignment import evaluate_xml
from xml_engine.serializer import to_xml


def test_evaluation_cannot_match_duplicate_unit_twice_and_handles_splits():
    cfg = Settings(schema_profile="sample")

    def render(strings):
        return to_xml(DocIR("paper", "p.pdf", "en", [PageIR(1, 500, 700,
                      [Block("p", text, kind="body") for text in strings])]), cfg)

    ref = render(["Original document with the complete research title.", "The second unit is unique."])
    actual = render(["The second unit is unique.", "Original document with",
                     "the complete research title.", "The second unit is unique."])
    result = evaluate_xml(ref, actual)
    assert result["content_recall"] == 1 and result["content_precision"] == .75
    assert result["counts"]["order_pairs"] == 1 and result["counts"].get("order_preserved", 0) == 0
    assert len(result["pages"][0]["unmatched_generated"]) == 1


def test_pseudocode_uses_physical_line_positions_without_rewriting_text():
    strings = ["Algorithm 3: Update routine", "while active do", "if ready then", "step X",
               "end if", "end while", "return X", "Normal text following the algorithm."]
    edges = [55, 65, 77, 89, 77, 65, 65, 55]
    blocks = [Block("p", text, kind="body", bbox=(x, 20 + i * 10, x + 200, 28 + i * 10), size=10)
              for i, (x, text) in enumerate(zip(edges, strings))]
    page = PageIR(1, 500, 700, blocks)
    preserve_pseudocode(page)
    assert [b.kind for b in page.blocks] == ["algorithm_caption", "pseudocode", "body"]
    assert page.blocks[1].text.splitlines()[2] == "    step X"
    assert page.blocks[2].text == strings[-1]


def test_glm_fenced_code_preserves_indentation_and_literal_markup():
    fence = chr(96) * 3
    content = f"Caption\n\n{fence}pseudocode\nwhile X do\n  emit '<table>'\n  emit '$$'\n{fence}"
    result = parse(1, {"response": content, "done": True})
    assert not result.issues
    assert result.blocks[-1].text == "while X do\n  emit '<table>'\n  emit '$$'"


def test_glm_figure_reinserted_at_its_caption_anchor():
    caption = Block("p", "Figure 2: Sketch", bbox=(60, 197, 240, 207), source="ocr")
    preceding = Block("p", "Earlier", bbox=(55, 35, 450, 100), source="ocr")
    following = Block("p", "Later", bbox=(55, 230, 450, 250), source="ocr")
    figure = Block("figure", bbox=(60, 115, 300, 190), kind="figure")
    page = PageIR(1, 500, 700, [preceding, caption, following, figure], source="ocr", ocr_engine="glm-ocr")
    place_visuals(page)
    assert page.blocks == [preceding, figure, following]
    assert figure.caption == caption.text and figure.source_reference["caption_bbox"] == caption.bbox

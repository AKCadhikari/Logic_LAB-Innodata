"""Local GLM-OCR over Ollama's native image inference endpoint."""
from __future__ import annotations

import base64
import re
from typing import Literal

from lxml import etree, html

from config.settings import Settings
from layout.model import Block, Cell, Span
from ocr.recognition import Recognition, RecognitionError
from utils import ollama
from utils import execution_audit as audit


def request_json(settings: Settings, path: str, payload: dict | None = None) -> dict:
    try:
        return ollama.request_json(settings.glm_ocr_url, path, payload,
                                   max(1, settings.glm_ocr_timeout_s) if payload is not None else 3)
    except ollama.OllamaError as error:
        raise RecognitionError(str(error), status=error.status) from error


def model_info(settings: Settings) -> dict:
    model = settings.glm_ocr_model
    try:
        models = ollama.installed_models(request_json(settings, "/api/tags"))
        return {"connected": True, **ollama.model_info(model, models, capability="vision")}
    except (RecognitionError, ollama.OllamaError, ValueError) as error:
        return {"connected": False, "ready": False, "model": model, "reason": str(error)}


TABLE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.I | re.S)
FORMULA = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]", re.S)
UNORDERED = re.compile(r"^\s*([-*+•])\s+(.+)$")
FENCED_CODE = re.compile(r"^\x60{3}[A-Za-z][\w+-]*[ \t]*\r?\n[\s\S]*?^\x60{3}[ \t]*$", re.M)
ORDERED = re.compile(r"^\s*(\d+[.)])\s+(.+)$")
MARKDOWN_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
INLINE_FORMULA = re.compile(r"(?<!\\)\$([^$\n]+)\$|\\\((.+?)\\\)")
INLINE_EMPHASIS = re.compile(
    r"(?<![\w\\*_])(?P<mark>\*\*|__|\*|_)(?P<value>[^\s*_](?:[^\n*_]*?[^\s*_])?)(?P=mark)(?![\w*_])"
)


def prose_spans(text):
    start = 0
    for match in INLINE_EMPHASIS.finditer(text):
        value = match["value"]
        if not any(character.isalpha() for character in value):
            continue
        if start < match.start():
            yield Span(text[start:match.start()])
        bold = len(match["mark"]) == 2
        yield Span(value, bold=bold, italic=not bold)
        start = match.end()
    if start < len(text):
        yield Span(text[start:])


def text_block(tag: str, text: str, kind: str) -> Block:
    spans = []
    consumed = 0
    for match in INLINE_FORMULA.finditer(text):
        formula = match[1] or match[2]
        if match[1] and not re.search(r"[=^_\\{}∑∫√]", formula):
            continue
        spans.extend(prose_spans(text[consumed:match.start()]))
        spans.append(Span(formula, formula=True))
        consumed = match.end()
    spans.extend(prose_spans(text[consumed:]))
    if spans:
        text = "".join(span.text for span in spans)
    styled = [span for span in spans if span.bold or span.italic or span.formula]
    return Block(tag, text, kind=kind, spans=spans if styled else [])


def table_block(raw: str) -> Block:
    try:
        table = html.fromstring(raw, parser=html.HTMLParser(no_network=True))
        rows = table.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr")
        occupied = set()
        cells = []
        widths = []
        for row, row_element in enumerate(rows):
            col = 0
            for cell in row_element.xpath("./td|./th"):
                while (row, col) in occupied:
                    col += 1
                rowspan, colspan = int(cell.get("rowspan", "1")), int(cell.get("colspan", "1"))
                if not (1 <= rowspan <= len(rows) - row and 1 <= colspan <= 100):
                    raise ValueError
                area = {(r, c) for r in range(row, row + rowspan) for c in range(col, col + colspan)}
                if occupied & area:
                    raise ValueError
                occupied.update(area)
                cells.append(Cell(cell.text_content().strip(), row, col, rowspan, colspan, cell.tag == "th"))
                col += colspan
            widths.append(sum(r == row for r, _ in occupied))
        if not cells or len(set(widths)) != 1:
            raise ValueError
        return Block("table", kind="table", cells=cells)
    except (ValueError, etree.ParserError) as error:
        raise RecognitionError("GLM-OCR returned an inconsistent table grid.") from error


def prose(text: str):
    """Read the model's documented text/Markdown-like output without trusting it as markup."""
    lines = text.splitlines()
    index = 0
    paragraph: list[str] = []
    listing = None
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        unordered, ordered = UNORDERED.match(line), ORDERED.match(line)
        pipe_table = ("|" in line and index < len(lines) and MARKDOWN_TABLE_RULE.fullmatch(lines[index]))
        if not line or heading or unordered or ordered or pipe_table:
            if paragraph:
                yield text_block("p", "\n".join(paragraph), "body")
                paragraph = []
        if pipe_table:
            index += 1
            grid = [line.strip("|").split("|")]
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                grid.append(lines[index].strip().strip("|").split("|"))
                index += 1
            if len(grid[0]) < 2 or len({len(row) for row in grid}) > 1:
                raise RecognitionError("GLM-OCR returned an inconsistent Markdown table grid.")
            yield Block("table", kind="table", cells=[Cell(item.strip(), r, c, header=r == 0)
                        for r, row in enumerate(grid) for c, item in enumerate(row)])
        elif heading:
            yield text_block("h" + str(len(heading[1])), heading[2], "section_heading")
        elif unordered or ordered:
            tag, marker, value = ("ol", ordered[1], ordered[2]) if ordered else ("ul", unordered[1], unordered[2])
            if listing is None or listing.tag != tag:
                listing = Block(tag, kind="body")
                yield listing
            item = text_block("li", value, "body")
            item.marker = marker
            listing.children.append(item)
            continue
        elif line:
            paragraph.append(line)
        if line:
            listing = None
    if paragraph:
        yield text_block("p", "\n".join(paragraph), "body")


def parse(page: int, data: dict, *, engine: str = "glm-ocr") -> Recognition:
    result = Recognition(page, engine, raw=data)
    text = data.get("response")
    if not isinstance(text, str):
        result.issues.append("The model did not return recognised text.")
        return result
    if data.get("done") is not True or data.get("done_reason") not in {"stop", None}:
        result.issues.append("The model did not finish the page cleanly.")
    if fence := re.fullmatch(r"\s*```(?:markdown|text|html)?\s*\n(.*?)\n```\s*", text, re.I | re.S):
        text = fence[1]
    pattern = re.compile(f"(?P<code>{FENCED_CODE.pattern})|(?P<table>{TABLE.pattern})|(?P<formula>{FORMULA.pattern})",
                         re.I | re.S | re.M)
    cursor = 0
    try:
        for match in pattern.finditer(text):
            result.blocks.extend(prose(text[cursor:match.start()]))
            if match.group("code"):
                value = "\n".join(match[0].splitlines()[1:-1])
                if value:
                    result.blocks.append(Block("p", value, kind="pseudocode", original_text=value))
            elif match.group("table"):
                result.blocks.append(table_block(match[0]))
            else:
                formula = match[0][2:-2].strip()
                if not formula:
                    raise RecognitionError("GLM-OCR returned an empty formula.")
                result.blocks.append(Block("p", formula, kind="equation"))
            cursor = match.end()
        result.blocks.extend(prose(text[cursor:]))
    except RecognitionError as error:
        result.issues.append(str(error))
    markup = FENCED_CODE.sub("", text)
    if len(re.findall(r"<table\b", markup, re.I)) != len(TABLE.findall(markup)):
        result.issues.append("GLM-OCR returned unfinished table markup.")
    if markup.count("$$") % 2 or markup.count(r"\[") != markup.count(r"\]"):
        result.issues.append("GLM-OCR returned unfinished formula delimiters.")

    def mark(block: Block) -> None:
        block.source = "ocr"
        block.ocr_engine = engine
        block.conf = 0  # Legacy numeric readers must not treat absent model confidence as 100%.
        for child in block.children:
            mark(child)

    for block in result.blocks:
        mark(block)
    return result


RecognitionMode = Literal["text", "table", "formula"]
PROMPTS = {"text": "Text Recognition:", "table": "Table Recognition:", "formula": "Formula Recognition:"}


def recognise_image(image: bytes, page: int, settings: Settings, mode: RecognitionMode = "text", *,
                    input_reference: str | None = None) -> Recognition:
    payload = {"model": settings.glm_ocr_model, "prompt": PROMPTS[mode], "stream": False,
               "images": [base64.b64encode(image).decode("ascii")], "keep_alive": settings.glm_ocr_keep_alive,
               "options": {"temperature": 0, "num_gpu": 0,
                           "num_ctx": max(8192, settings.glm_ocr_tokens + 4096),
                           "num_predict": settings.glm_ocr_tokens}}
    with audit.execution("page_ocr", "ollama", settings.glm_ocr_model, page=page, input_type="rendered_png",
                         input_sha256=audit.digest(image), input_reference=input_reference or f"pdf/page/{page}/render",
                         prompt_template=f"glm-ocr/{mode}/v1") as event:
        data = request_json(settings, "/api/generate", payload)
        result = parse(page, data)
        if isinstance(data.get("model"), str) and ollama.tagged_model(data["model"]) != ollama.tagged_model(settings.glm_ocr_model):
            result.issues.append("Ollama returned a different model identity.")
        if event:
            result.execution_ids.append(event["execution_id"])
    if mode == "table" and not any(block.tag == "table" for block in result.blocks):
        result.issues.append("GLM-OCR did not return a recognised table.")
    if mode == "formula" and not any(block.kind == "equation" for block in result.blocks):
        result.issues.append("GLM-OCR did not return a recognised formula.")
    return result


def recognise(page, dpi: int, settings: Settings) -> Recognition:
    import pymupdf

    scale = min(dpi / 72, max(256, settings.glm_ocr_max_edge) / max(page.rect.width, page.rect.height))
    raster = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csGRAY, alpha=False)
    return recognise_image(raster.tobytes("png"), page.number + 1, settings)

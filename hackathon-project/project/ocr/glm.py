"""Local GLM-OCR over Ollama's native image inference endpoint."""
from __future__ import annotations

import base64
import json
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from lxml import etree, html

from config.settings import Settings
from layout.model import Block, Cell, Span
from ocr.recognition import Recognition, RecognitionError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request_json(settings: Settings, path: str, payload: dict | None = None) -> dict:
    endpoint = urlsplit(settings.glm_ocr_url)
    if (endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "::1", "localhost"}
            or endpoint.username or endpoint.password or endpoint.path not in {"", "/"}
            or endpoint.query or endpoint.fragment):
        raise RecognitionError("GLM-OCR requires a local Ollama HTTP endpoint.", unavailable=True)
    request = Request(f"http://{endpoint.netloc}{path}",
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Content-Type": "application/json"})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=max(1, settings.glm_ocr_timeout_s) if payload is not None else 3) as response:
            data = json.loads(response.read(1024 * 1024 + 1))
    except HTTPError as error:
        raise RecognitionError(f"Local Ollama returned HTTP {error.code}.", unavailable=error.code == 404) from error
    except TimeoutError as error:
        raise RecognitionError("GLM-OCR timed out.") from error
    except OSError as error:
        raise RecognitionError("Local Ollama is unreachable.", unavailable=True) from error
    except ValueError as error:
        raise RecognitionError("Ollama returned invalid JSON.") from error
    if not isinstance(data, dict):
        raise RecognitionError("Ollama returned an unexpected payload.")
    return data


def model_info(settings: Settings) -> dict:
    model = settings.glm_ocr_model
    target = model if ":" in model else model + ":latest"
    try:
        models = request_json(settings, "/api/tags").get("models", [])
        match = next((item for item in models if isinstance(item, dict) and item.get("name") == target
                      and not item.get("remote_host") and "cloud" not in target.casefold()), None)
        return {"ready": bool(match), "model": model, "digest": match.get("digest") if match else None,
                "reason": None if match else "The configured local GLM-OCR model is not installed."}
    except (RecognitionError, ValueError, TypeError) as error:
        return {"ready": False, "model": model, "reason": str(error)}


TABLE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.I | re.S)
FORMULA = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]", re.S)
UNORDERED = re.compile(r"^\s*[-*+]\s+(.+)$")
ORDERED = re.compile(r"^\s*(\d+[.)])\s+(.+)$")
MARKDOWN_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
INLINE_FORMULA = re.compile(r"(?<!\\)\$([^$\n]+)\$|\\\((.+?)\\\)")


def text_block(tag: str, text: str, kind: str) -> Block:
    spans = []
    consumed = 0
    for match in INLINE_FORMULA.finditer(text):
        formula = match[1] or match[2]
        if match[1] and not re.search(r"[=^_\\{}∑∫√]", formula):
            continue
        spans.append(Span(text[consumed:match.start()]))
        spans.append(Span(formula, formula=True))
        consumed = match.end()
    if spans:
        spans.append(Span(text[consumed:]))
        text = "".join(span.text for span in spans)
    return Block(tag, text, kind=kind, spans=spans)


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
            tag, marker, value = ("ol", ordered[1], ordered[2]) if ordered else ("ul", "•", unordered[1])
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


def parse(page: int, data: dict) -> Recognition:
    result = Recognition(page, "glm-ocr", raw=data)
    text = data.get("response")
    if not isinstance(text, str):
        result.issues.append("GLM-OCR did not return recognised text.")
        return result
    if data.get("done") is not True or data.get("done_reason") not in {"stop", None}:
        result.issues.append("GLM-OCR did not finish the page cleanly.")
    if fence := re.fullmatch(r"\s*```(?:markdown|text|html)?\s*\n(.*?)\n```\s*", text, re.I | re.S):
        text = fence[1]
    pattern = re.compile(f"(?P<table>{TABLE.pattern})|(?P<formula>{FORMULA.pattern})", re.I | re.S)
    cursor = 0
    try:
        for match in pattern.finditer(text):
            result.blocks.extend(prose(text[cursor:match.start()]))
            if match.group("table"):
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
    if len(re.findall(r"<table\b", text, re.I)) != len(TABLE.findall(text)):
        result.issues.append("GLM-OCR returned unfinished table markup.")
    if text.count("$$") % 2 or text.count(r"\[") != text.count(r"\]"):
        result.issues.append("GLM-OCR returned unfinished formula delimiters.")

    def mark(block: Block) -> None:
        block.source = "ocr"
        block.ocr_engine = "glm-ocr"
        block.conf = 0  # Legacy numeric readers must not treat absent model confidence as 100%.
        for child in block.children:
            mark(child)

    for block in result.blocks:
        mark(block)
    return result


def recognise(page, dpi: int, settings: Settings) -> Recognition:
    import pymupdf

    scale = min(dpi / 72, max(256, settings.glm_ocr_max_edge) / max(page.rect.width, page.rect.height))
    raster = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csGRAY, alpha=False)
    image = base64.b64encode(raster.tobytes("png")).decode("ascii")
    payload = {"model": settings.glm_ocr_model, "prompt": "Text Recognition:", "stream": False,
               "images": [image], "keep_alive": "45s", "options": {"temperature": 0, "num_gpu": 0,
               "num_ctx": max(8192, settings.glm_ocr_tokens + 4096), "num_predict": settings.glm_ocr_tokens}}
    return parse(page.number + 1, request_json(settings, "/api/generate", payload))

"""Recover running matter from positioned source text and document-wide evidence."""
from collections import defaultdict
from dataclasses import dataclass
import re

from layout.model import Block, Line
from layout.reconstruct import PAGENUM_RE, band_sig
from layout.region_association import coverage
from utils.textutil import norm_cmp, xml_safe


@dataclass
class Candidate:
    page: int
    tag: str
    line: Line
    position: tuple[float, float]
    page_number: bool
    region: dict | None


def native_candidates(source, lines):
    """Reject undecodable native layers while retaining reliably mapped margin fonts."""
    opaque = {font[3].split("+")[-1] for font in source.get_fonts() if font[2] == "Type3"}
    return [line for line in lines if line.spans and "\ufffd" not in line.text
            and xml_safe(line.original_text or line.text) == (line.original_text or line.text)
            and not any(span.font.split("+")[-1] in opaque for span in line.spans)]


def candidates(page, lines, band):
    width, height = (page.diag.get(key, default) for key, default in
                     (("source_width", page.width), ("source_height", page.height)))
    detections = page.diag.get("layout", {}).get("detections", [])
    for line in lines:
        x0, y0, x1, y1 = line.bbox
        y = (y0 + y1) / (2 * height)
        if line.writing_mode != "horizontal" or not (y < band or y > 1 - band):
            continue
        text = line.text.strip()
        if not 0 < len(text) <= 160 or (line.source == "ocr" and line.conf < 78):
            continue
        tag = "header" if y < .5 else "footer"
        number = bool(PAGENUM_RE.fullmatch(text))
        allowed = {tag, "page_number"} if number else {tag}
        region = max((region for region in detections if region["class_name"] in allowed
                      and coverage(line.bbox, region["bbox"]) >= .55),
                     key=lambda region: region["confidence"], default=None)
        yield Candidate(page.n, tag, line, ((x0 + x1) / (2 * width), y), number, region)


def recurring(candidate, group, threshold):
    def agrees(other):
        return (abs(candidate.position[0] - other.position[0]) <= .04
                and abs(candidate.position[1] - other.position[1]) <= .018
                and abs(candidate.line.size - other.line.size) <= .25 * max(candidate.line.size, 1)
                and candidate.line.bold == other.line.bold)

    peers = [other for other in group if other.page != candidate.page and agrees(other)]
    if not candidate.page_number:
        return len({item.page for item in peers}) + 1 >= threshold
    digits = re.findall(r"\d+", candidate.line.text)
    if not digits:
        return False
    return any((values := re.findall(r"\d+", item.line.text)) and
               int(values[0]) - int(digits[0]) == item.page - candidate.page for item in peers)


def merge_page(page, found):
    selected = []
    for match in sorted(found, key=lambda item: (item.line.source != "native", -item.line.conf)):
        line = match.line
        if not any(norm_cmp(item.line.text) == norm_cmp(line.text)
                   and coverage(line.bbox, item.line.bbox) >= .6 for item in selected):
            selected.append(match)
    if not selected and not any(block.tag in {"header", "footer"} for block in page.blocks):
        return

    running, body = [], []
    for block in page.blocks:
        if block.tag in {"header", "footer"}:
            if not any(block.tag == item.tag and norm_cmp(block.text) == norm_cmp(item.line.text)
                       and (not block.bbox or coverage(block.bbox, item.line.bbox) >= .55) for item in selected):
                running.append(block)
        elif not _remove_running_fragment(block, selected):
            body.append(block)
    for item in selected:
        line = item.line
        evidence = {"running_matter": "page_number" if item.page_number else item.tag}
        if item.region:
            evidence.update(layout_region_id=item.region["id"], layout_detection=item.region["source"])
        if line.source == "ocr":
            evidence["geometry_method"] = "tesseract-render" if page.ocr_engine in {"glm-ocr", "paddleocr-vl"} else "positioned-ocr"
        block = Block(item.tag, line.text.strip(), bbox=line.bbox, size=line.size, spans=line.spans,
                      conf=line.conf, source=line.source, original_text=line.original_text or line.text,
                      ocr_engine=("tesseract" if evidence.get("geometry_method") == "tesseract-render"
                                  else page.ocr_engine) if line.source == "ocr" else "",
                      recognition_confidence=line.conf if line.source == "ocr" else None,
                      source_reference=evidence)
        running.append(block)
    running.sort(key=lambda block: (round(block.bbox[1] / 3) * 3, block.bbox[0]) if block.bbox else (0, 0))
    page.blocks = ([block for block in running if block.tag == "header"] + body
                   + [block for block in running if block.tag == "footer"])
    page.diag["running_matter"] = {tag: sum(block.tag == tag for block in running) for tag in ("header", "footer")}


def _remove_running_fragment(block, selected):
    """Remove only exact, positioned repetitions from OCR paragraph boundaries."""
    if not (block.tag == "p" and block.bbox and block.text and not block.spans):
        return False
    matches = [item for item in selected if coverage(item.line.bbox, block.bbox) >= .52]
    if not matches:
        return False
    updated = block.text.strip()
    removed = []
    for item in sorted(matches, key=lambda m: (m.tag == "header", -m.line.bbox[1], -m.line.bbox[0])):
        phrase = r"\s+".join(re.escape(word) for word in item.line.text.split())
        if item.tag == "footer":
            pattern = rf"(?:^|\s){phrase}$"
        else:
            pattern = rf"^{phrase}(?:\s|$)"
        text = re.sub(pattern, "", updated, flags=re.IGNORECASE).strip()
        if text != updated:
            removed.append(item.line.bbox)
            updated = text
    if not removed:
        return False
    if not updated:
        return True
    # Rebuild the paragraph box only from retained source lines.
    lines = [box for box in block.line_boxes if all(coverage(box, margin) < .3 for margin in removed)]
    block.text = updated
    block.line_boxes = lines
    block.bbox = (min(b[0] for b in lines), min(b[1] for b in lines),
                  max(b[2] for b in lines), max(b[3] for b in lines)) if lines else None
    return False


def apply(pages, source_lines, band):
    groups, per_page = defaultdict(list), defaultdict(list)
    for page, lines in zip(pages, source_lines):
        for candidate in candidates(page, lines, min(.12, band * 1.25)):
            key = (candidate.tag, band_sig(candidate.line.text), candidate.page_number)
            groups[key].append(candidate)
            per_page[page.n].append((candidate, key))
    threshold = max(2, (len(pages) + 4) // 5)
    for page in pages:
        found = [candidate for candidate, key in per_page[page.n]
                 if (candidate.region and candidate.region["confidence"] >= .55)
                 or recurring(candidate, groups[key], threshold)]
        merge_page(page, found)

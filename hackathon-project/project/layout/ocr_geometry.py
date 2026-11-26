"""Match OCR paragraphs to positioned text from an independent local render."""

import re
from dataclasses import replace
from statistics import median

from rapidfuzz import fuzz

from diagnostics.profile import block_text
from layout.equations import position_numbered_equations
from layout.kinds import ADDRESS, AFFIL, CLASSIFICATION, KEYW, metadata_kind
from layout.model import Block
from ocr import engine
from utils.textutil import ISO_TO_TESS, language_profile, norm_cmp
from utils import execution_audit as audit


def text_key(text):
    # Ignore line-wrap hyphens and spacing for matching only.
    normalized = norm_cmp(re.sub(r"(?<=\w)[-‐]\s*\n\s*(?=\w)", "", text))
    return "".join(char for char in normalized if char.isalnum())


def box_of(lines):
    return (min(line.bbox[0] for line in lines), min(line.bbox[1] for line in lines),
            max(line.bbox[2] for line in lines), max(line.bbox[3] for line in lines))


KEYWORD_BOUNDARY = re.compile(r"\s+(?=(?:Mathematics Subject Classification|Computing Classification System)\b)", re.I)


def split_classifications(page):
    for index, block in reversed(list(enumerate(page.blocks))):
        if not block.spans and block.source == "ocr" and (KEYW.match(block.text) or CLASSIFICATION.match(block.text)):
            parts = KEYWORD_BOUNDARY.split(block.text)
            if len(parts) > 1:
                page.blocks[index:index + 1] = [
                    replace(block, text=text, original_text=text, kind="keywords", bbox=None,
                            line_boxes=[], source_reference=dict(block.source_reference))
                    for text in parts
                ]


def split_affiliations(page, lines, anchors):
    """Break a multiline affiliation at a source-backed change from organization to address."""
    if page.n != 1:
        return
    for index, (start, end) in sorted(anchors.items(), reverse=True):
        block = page.blocks[index]
        text_lines = block.text.splitlines()
        matched = lines[start:end]
        if (len(text_lines) != len(matched) or len(text_lines) < 2 or block.spans
                or not AFFIL.search(text_lines[0]) or block.source != "ocr"
                or any(fuzz.ratio(text_key(text), text_key(line.text)) < 85 for text, line in zip(text_lines, matched))):
            continue
        cuts = [i for i in range(1, len(text_lines)) if ADDRESS.search(text_lines[i])]
        if not cuts:
            continue
        address = cuts[0]
        for i in range(address + 1, len(text_lines)):
            if "@" in text_lines[i] and abs(matched[i].bbox[0] - matched[address].bbox[0]) > page.width * .025:
                cuts.append(i)
        bounds = [0, *cuts, len(text_lines)]
        page.blocks[index:index + 1] = [
            replace(block, text=" ".join(text_lines[first:last]), original_text="\n".join(text_lines[first:last]),
                    kind="affiliation", bbox=box_of(matched[first:last]), size=median(line.size for line in matched[first:last]),
                    line_boxes=[line.bbox for line in matched[first:last]], source_reference=dict(block.source_reference))
            for first, last in zip(bounds, bounds[1:])
        ]


def recover_masthead(page, lines, anchors):
    """Use independently read lines when they form an identified masthead above anchored content."""
    if page.n != 1:
        return 0
    leading = []
    for line in lines:
        if line.bbox[3] > page.height * .20 or line.conf < 78 or not metadata_kind(line.text):
            break
        leading.append(line)
    if not 2 <= len(leading) <= 8:
        return 0
    keys = [text_key(line.text) for line in leading]
    combined = "".join(keys)
    before_title = []
    for index, block in enumerate(page.blocks):
        text = text_key(block_text(block))
        if text and (text in keys or (metadata_kind(block.text) and fuzz.ratio(text, combined) >= 93)):
            before_title.append(index)
        else:
            break
    first = len(before_title)
    anchor = anchors.get(first, -1)
    if anchor < len(leading) or max(line.bbox[3] for line in leading) >= lines[anchor].bbox[1]:
        return 0
    page.blocks[:first] = [
        Block(tag="header", kind=metadata_kind(line.text), text=line.text.strip(), bbox=line.bbox,
              source="ocr", ocr_engine="tesseract", recognition_confidence=line.conf,
              conf=line.conf, size=line.size, line_boxes=[line.bbox],
              source_reference={"geometry_method": "tesseract-render", "recovery": "leading-publication-metadata"})
        for line in leading
    ]
    return len(leading)


def align_blocks(page, lines):
    """Attach only unique text matches and retain the primary recognizer's order and words."""
    split_classifications(page)
    keys = [text_key(line.text) for line in lines]
    candidates = []
    paragraphs = {index: text_key(block_text(block)) for index, block in enumerate(page.blocks)
                  if block.source == "ocr" and not block.cells and block.kind != "equation"}
    for index, target in paragraphs.items():
        if len(target) < 5:
            continue
        choices = []
        for start in range(len(lines)):
            words = ""
            for end in range(start, len(lines)):
                words += keys[end]
                if len(words) > len(target) / .82:
                    break
                if len(words) >= len(target) * .82:
                    score = fuzz.ratio(target, words)
                    if score >= (94 if len(target) < 18 else 88):
                        choices.append((score, start, end + 1))
        if not choices:
            continue
        score, start, end = max(choices)
        # Alternative windows on the same text are common when line breaks differ.
        if any(score - other < 5 and (right <= start or left >= end)
               for other, left, right in choices):
            continue
        candidates.append((score, index, start, end))

    used = set()
    anchors = {}
    for score, index, start, end in sorted(candidates, reverse=True):
        if used.intersection(range(start, end)):
            continue
        used.update(range(start, end))
        anchors[index] = (start, end)
        block = page.blocks[index]
        if block.bbox is None:
            matched = lines[start:end]
            block.bbox = box_of(matched)
            block.line_boxes = [line.bbox for line in matched]
            block.size = max(line.size for line in matched) if block.tag == "h1" else median(line.size for line in matched)
            block.source_reference.update(geometry_method="tesseract-text-alignment", geometry_score=round(score, 2))

    aligned = sum(page.blocks[index].source_reference.get("geometry_method") ==
                  "tesseract-text-alignment" for index in anchors)
    starts = {index: start for index, (start, _) in anchors.items()}
    split_affiliations(page, lines, anchors)
    return {"aligned": aligned, "recovered_headers": recover_masthead(page, lines, starts)}


def position_page(source, page, settings, packs):
    if (page.source != "ocr" or not page.blocks or source.rotation or
            not any(block.bbox is None and block.source == "ocr" for block in page.blocks)):
        return None
    profile = language_profile("\n".join(block_text(block) for block in page.blocks))
    language = profile["language"]
    if ISO_TO_TESS.get(language) not in packs:
        return {"status": "No matching local OCR pack for coordinate alignment."}
    try:
        with audit.details(purpose="independent_geometry_and_masthead_check"):
            lines, detail = engine.ocr_page(source, language, max(225, settings.ocr_dpi), settings, packs,
                                   orient=False, deskew=False)
    except (OSError, RuntimeError, ValueError, engine.subprocess.TimeoutExpired) as error:
        return {"status": "Local coordinate alignment failed.", "reason": str(error)}
    result = align_blocks(page, lines)
    ids = detail.get("selected_execution_ids", [])
    located = position_numbered_equations(page, lines, ids)
    if located:
        result["positioned_equations"] = located
    if ids:
        for block in page.blocks[:result["recovered_headers"]]:
            block.source_reference["execution_ids"] = ids
    if result["aligned"] or located:
        audit.mark(ids, downstream=[{"consumer": "pydantic_layout", "reference": f"pdf/page/{page.n}",
                                     "matched_blocks": result["aligned"] + located}])
    return {"status": "checked", **result}

"""Match OCR paragraphs to positioned text from an independent local render."""

import re
from statistics import median

from rapidfuzz import fuzz

from diagnostics.profile import block_text
from layout.kinds import PUBMETA, RIGHTS
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


def align_blocks(page, lines):
    """Attach only unique text matches and retain the primary recognizer's order and words."""
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
        anchors[index] = start
        block = page.blocks[index]
        if block.bbox is None:
            block.bbox = box_of(lines[start:end])
            block.size = median(line.size for line in lines[start:end])
            block.source_reference.update(geometry_method="tesseract-text-alignment", geometry_score=round(score, 2))

    recovered = 0
    if anchors and 0 in anchors:
        leading = lines[:anchors[0]]
        # Treat the crop as a masthead only when its identifiers or rights copy
        # corroborate the role and the complete region precedes the first body block.
        if (2 <= len(leading) <= 8 and max(line.bbox[3] for line in leading) <= min(
                page.height * .24, lines[anchors[0]].bbox[1] - 2)
                and min(line.conf for line in leading) >= 78):
            text = "\n".join(line.text.strip() for line in leading)
            leading_keys = [text_key(line.text) for line in leading]
            novel = sum(len(key) >= 12 and not any(key in target for target in paragraphs.values())
                        for key in leading_keys)
            if (sum(bool(PUBMETA.search(line.text) or RIGHTS.search(line.text)) for line in leading) >= 2
                    and novel >= 2 and novel > len(leading) / 2):
                confidence = min(line.conf for line in leading)
                page.blocks.insert(0, Block(tag="header", kind="publication_metadata", text=text, bbox=box_of(leading),
                                           source="ocr", ocr_engine="tesseract", recognition_confidence=confidence,
                                           conf=confidence, size=median(line.size for line in leading),
                                           source_reference={"geometry_method": "tesseract-render",
                                                             "recovery": "leading-publication-metadata"}))
                recovered = 1
    return {"aligned": sum(page.blocks[index + recovered].source_reference.get("geometry_method") ==
                           "tesseract-text-alignment" for index in anchors), "recovered_headers": recovered}


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
    if result["recovered_headers"] and ids:
        page.blocks[0].source_reference["execution_ids"] = ids
    if result["aligned"]:
        audit.mark(ids, downstream=[{"consumer": "pydantic_layout", "reference": f"pdf/page/{page.n}",
                                     "matched_blocks": result["aligned"]}])
    return {"status": "checked", **result}

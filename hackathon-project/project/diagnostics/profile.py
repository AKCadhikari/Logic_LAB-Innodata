"""Language, reading order and validation evidence from the shared IR."""
from collections import Counter
import re

from utils.textutil import direction_of, language_profile, xml_safe


def block_text(block):
    return block.text or " ".join([*(child.text for child in block.children), *(cell.text for cell in block.cells)])


NON_PROSE = {"equation", "pseudocode", "publication_metadata", "rights", "byline", "reference_entry"}
EQUATION_WITHOUT_GEOMETRY = "At least one equation has no verified source coordinates."
EQUATION_WITHOUT_TRANSCRIPTION = "At least one equation has no corroborated text transcription."


def language_text(block):
    if block.kind in NON_PROSE or block.tag in {"header", "footer"}:
        return ""
    if block.children:
        return " ".join(map(language_text, block.children))
    if block.spans and "".join(span.text for span in block.spans) == block.text:
        text = " ".join(span.text for span in block.spans if not span.formula)
    else:
        text = block_text(block)
    return re.sub(r"https?://\S+|www\.\S+|\bdoi:\s*\S+|(?<!\w)[A-Z][A-Z0-9-]+\b", " ", text)


def has_text_loss(block) -> bool:
    raw = [block.original_text or "", *(cell.original_text or cell.text for cell in block.cells)]
    return any(xml_safe(text) != text or "\ufffd" in text for text in raw) or any(map(has_text_loss, block.children))


BLOCK_LANGUAGE_WARNING = "At least one block has an uncertain language or unsupported script."
PAGE_LANGUAGE_WARNING = "The page language or script could not be confidently identified."


def decorate_document(ir) -> dict:
    document_languages = set()
    page_profiles = []
    document_types = Counter()
    for page in ir.pages:
        hint = page.diag.get("native_language", {}).get("language", "und")
        if hint in {"und", "mul"} and page.source == "ocr":
            hint = page.diag.get("ocr_language_hint", "und")
        page.language = language_profile("\n".join(map(language_text, page.blocks)), hint)
        languages = set()
        reading_order = 0
        reasons = page.diag.setdefault("review_reasons", [])

        def visit(block):
            nonlocal reading_order
            raw = block.original_text if block.original_text is not None else block.text
            block.original_text = raw
            reading_order += 1
            block.reading_order = reading_order
            sample = language_text(block)
            block.language = language_profile(sample, page.language["language"])
            if not sample.strip():
                base, direction = direction_of(block_text(block))
                block.language.update(base_direction=base, direction=direction)
            if (block.tag == "table" and block.source == "native" and block.cells
                    and (page.language["base_direction"] == "rtl" or block.language["script"] == "arab")):
                first_row_right = min(block.cells, key=lambda cell: (cell.row, -cell.col))
                if direction_of(first_row_right.text)[0] == "rtl":
                    block.language["base_direction"] = "rtl"
            block.lang = block.language["language"]
            if block.lang != "und" and block.language["confidence"] >= 0.55:
                languages.add(block.lang)
            block.source_reference = {**block.source_reference, "page": page.n, "bbox": block.bbox, "extraction_method": block.source,
                                      "recognizer": block.ocr_engine or (page.ocr_engine if block.source == "ocr" else None),
                                      "alignment": "unverified" if block.source == "ocr" and (
                                          page.diag.get("rotation") or page.diag.get("ocr", {}).get("rotate")
                                          or page.diag.get("ocr", {}).get("skew")) else "source_page"}
            issues = []
            if block.writing_mode == "vertical":
                issues.append("Vertical text order requires visual review.")
            if block.language["review_required"]:
                issues.append(BLOCK_LANGUAGE_WARNING)
            if xml_safe(raw) != raw or "\ufffd" in raw:
                issues.append("At least one source block contains replacement or XML-incompatible characters.")
            if block.kind == "equation" and not block.bbox:
                issues.append(EQUATION_WITHOUT_GEOMETRY)
            if block.source_reference.get("formula", {}).get("status") == "review_required":
                issues.append(EQUATION_WITHOUT_TRANSCRIPTION)
            reasons.extend(issues)
            for cell in block.cells:
                cell.language = language_profile(cell.text, block.lang)
                if cell.original_text is None:
                    cell.original_text = cell.text
            block.validation_status = ("manual_review_required" if issues else
                                       "unverified_ocr" if block.source == "ocr" else "native_extraction")
            for child in block.children:
                visit(child)

        for block in page.blocks:
            visit(block)
        if len(languages) > 1:
            page.language["language"] = "mul"
        if page.language["language"] not in {"und", "mul"}:
            languages.add(page.language["language"])
        page.language["languages"] = sorted(languages)
        document_languages.update(languages)
        if page.language["review_required"]:
            reasons.append(PAGE_LANGUAGE_WARNING)
        page.diag["review_reasons"] = list(dict.fromkeys(reasons))
        page.requires_review |= bool(reasons)
        page_type = ("mixed" if page.diag.get("mixed_content") else "scanned" if page.diag.get("appears_scanned")
                     else "digital" if page.diag.get("has_native_text") else "image_or_vector")
        document_types[page_type] += 1
        page_profiles.append({"page": page.n, "width": page.width, "height": page.height,
                              "orientation": "landscape" if page.width > page.height else "portrait",
                              "type": page_type, "language": page.language, "diagnostics": page.diag,
                              "counts": dict(Counter(block.kind or block.tag for block in page.blocks)),
                              "requires_review": page.requires_review})
    ir.language = language_profile("\n".join(language_text(block) for page in ir.pages for block in page.blocks), ir.lang)
    ir.language["languages"] = sorted(document_languages)
    if len(document_languages) > 1:
        ir.language.update(language="mul", method="page-and-block-evidence")
    elif len(document_languages) == 1 and ir.language["language"] == "und":
        ir.language.update(language=next(iter(document_languages)), method="page-and-block-evidence")
    ir.lang = ir.language["language"]
    return {"page_count": len(ir.pages), "page_types": dict(document_types), "language": ir.language, "pages": page_profiles}


def quality_gate(ir) -> dict:
    blocking, review = [], []
    for page in ir.pages:
        diagnostics = page.diag
        failed = diagnostics.get("ocr_required") and diagnostics.get("ocr", {}).get("status") in {"review_required", "skipped"}
        loss = any(map(has_text_loss, page.blocks))
        native_risk = diagnostics.get("native_decoding_risk") and not diagnostics.get("native_order_corroborated")
        if (diagnostics.get("error") or loss or (page.source == "native" and native_risk)
                or (failed and diagnostics.get("health") in {"absent", "corrupt"})):
            blocking.append(page.n)
        if page.requires_review or page.source == "ocr" or page.n in blocking:
            review.append(page.n)
    return {"status": "manual_review_required" if blocking else "validation_partial" if review else "structural_validation_passed",
            "blocking_pages": blocking, "review_pages": review, "ground_truth_verified": False}

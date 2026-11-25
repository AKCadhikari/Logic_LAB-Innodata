"""Language, reading order and validation evidence from the shared IR."""
from collections import Counter

from utils.textutil import language_profile, xml_safe


def block_text(block):
    return block.text or " ".join([*(child.text for child in block.children), *(cell.text for cell in block.cells)])


def has_text_loss(block) -> bool:
    raw = [block.original_text or "", *(cell.original_text or cell.text for cell in block.cells)]
    return any(xml_safe(text) != text or "\ufffd" in text for text in raw) or any(map(has_text_loss, block.children))


def decorate_document(ir) -> dict:
    document_languages = set()
    page_profiles = []
    document_types = Counter()
    for page in ir.pages:
        hint = page.diag.get("native_language", {}).get("language", "und")
        page.language = language_profile("\n".join(map(block_text, page.blocks)), hint)
        languages = set()
        reading_order = 0
        reasons = page.diag.setdefault("review_reasons", [])

        def visit(block):
            nonlocal reading_order
            raw = block.original_text if block.original_text is not None else block.text
            block.original_text = raw
            reading_order += 1
            block.reading_order = reading_order
            block.language = language_profile(block_text(block), page.language["language"])
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
                issues.append("At least one block has an uncertain language or unsupported script.")
            if xml_safe(raw) != raw or "\ufffd" in raw:
                issues.append("At least one source block contains replacement or XML-incompatible characters.")
            if block.kind == "equation" and not block.bbox:
                issues.append("At least one equation has no verified source coordinates.")
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
            reasons.append("The page language or script could not be confidently identified.")
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
    ir.language = language_profile("\n".join(block_text(block) for page in ir.pages for block in page.blocks))
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

"""Quality engine: heuristic confidence scores (NOT benchmark accuracy) + validation roll-ups."""
from __future__ import annotations
from layout.model import PageIR, DocIR


def _text_len(p: PageIR) -> int:
    n = 0
    for b in p.blocks:
        n += len(b.text) + sum(len(c.text) for c in b.children) + sum(len(c.text) for c in b.cells)
    return n


def page_confidence(p: PageIR) -> float | None:
    """0..1 heuristic: OCR confidence / native health / presence of content. Documented as a heuristic only."""
    d = p.diag or {}
    if _text_len(p) == 0 and not any(b.tag == "figure" for b in p.blocks):
        return 0.0
    if p.source == "ocr":
        ocr = d.get("ocr", {})
        score = ocr.get("confidence", ocr.get("conf"))
        if score is None:
            return None
        c = score / 100.0
        return round(max(0.05, min(0.95, c * 0.97)), 3)
    base = {"healthy": 0.97, "suspicious": 0.8, "corrupt": 0.4, "absent": 0.3, "error": 0.2}.get(d.get("health", "healthy"), 0.8)
    if d.get("pua_ratio", 0) > 0:
        base -= min(0.25, d["pua_ratio"] * 5)
    return round(max(0.05, base), 3)


def doc_quality(doc: DocIR, val: dict) -> dict:
    confs = [score for p in doc.pages if (score := p.diag.get("confidence")) is not None]
    return {
        "mean_page_confidence": round(sum(confs) / len(confs), 3) if confs else 0.0,
        "low_confidence_pages": [p.n for p in doc.pages if (score := p.diag.get("confidence")) is not None and score < 0.6],
        "review_pages": [p.n for p in doc.pages if p.requires_review],
        "empty_pages": val.get("empty_pages", []),
        "xml_issues": val.get("issues", []),
        "note": "Confidence is a heuristic, not a measured accuracy.",
    }

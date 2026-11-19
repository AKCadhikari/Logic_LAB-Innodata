"""Alt text for figures. No vision model is shipped (offline, CPU-first): a geometry/colour heuristic gives a
truthful, non-captioning description. A local VLM can be plugged in through `describer` (see TECHNICAL_APPROACH.md)."""
from __future__ import annotations
from layout.model import Block


def describe(b: Block, page_w: float, page_h: float, n: int, describer=None) -> str:
    if describer:
        try:
            out = describer(b, n)
            if out:
                return out
        except Exception:
            pass
    x0, y0, x1, y1 = b.bbox
    w, h = x1 - x0, y1 - y0
    area = w * h / max(page_w * page_h, 1)
    vert = "top" if y1 < 0.4 * page_h else "bottom" if y0 > 0.6 * page_h else "middle"
    horiz = "left" if x1 < 0.45 * page_w else "right" if x0 > 0.55 * page_w else "centre"
    if b.cls == "vector":
        what = "Diagram, chart or vector illustration"
    elif area < 0.02 or max(w, h) < 90:
        what = "Small graphic, logo, seal or inline symbol"
    elif area > 0.35:
        what = "Large photograph or illustration"
    else:
        what = "Photograph, scanned image or illustration"
    return f"{what} in the {vert} {horiz} of the page ({w:.0f} by {h:.0f} points)"

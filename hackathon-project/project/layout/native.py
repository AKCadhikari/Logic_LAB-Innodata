"""Extraction from the native PDF layer (PyMuPDF): styled lines, tables, figures, strike-through, links."""
from __future__ import annotations
import bisect
from layout.model import Line, Span, Cell, Block
from utils.textutil import xml_safe, is_rtl


def _is_bold(font: str, flags: int) -> bool:
    f = font.lower()
    return bool(flags & 16) or any(k in f for k in ("bold", "black", "heavy", "semibold", "demi"))


def _is_italic(font: str, flags: int) -> bool:
    f = font.lower()
    return bool(flags & 2) or "italic" in f or "oblique" in f


def _hsegments(page, limit=4000):
    """Thin horizontal strokes (candidate strike-through / rules): list of (x0, x1, y)."""
    segs = []
    try:
        for d in page.get_drawings()[:limit]:
            for it in d.get("items", []):
                if it[0] == "l":
                    p1, p2 = it[1], it[2]
                    if abs(p1.y - p2.y) < 0.8 and abs(p1.x - p2.x) > 2:
                        segs.append((min(p1.x, p2.x), max(p1.x, p2.x), p1.y))
                elif it[0] == "re":
                    r = it[1]
                    if r.height < 1.2 and r.width > 2:
                        segs.append((r.x0, r.x1, (r.y0 + r.y1) / 2))
    except Exception:
        pass
    return segs


def extract_lines(page) -> list[Line]:
    import pymupdf
    d = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    links = []
    try:
        links = [(pymupdf.Rect(l["from"]), l["uri"]) for l in page.get_links() if l.get("uri") and l.get("from")]
    except Exception:
        pass
    segs = _hsegments(page)
    out: list[Line] = []
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            spans, texts = [], []
            sizes, bold_chars, ital_chars, tot = [], 0, 0, 0
            lh = ln["bbox"][3] - ln["bbox"][1]
            base_y = ln.get("bbox")[3]
            main = max(ln["spans"], key=lambda s: (s["size"], len(s["text"].strip())), default=None)
            maxsize = main["size"] if main else 10
            baseline = main["origin"][1] if main else ln["bbox"][3]
            for s in ln["spans"]:
                t = xml_safe(s["text"])
                if not t:
                    continue
                n = len(t.strip()) or 0
                bold, ital = _is_bold(s["font"], s["flags"]), _is_italic(s["font"], s["flags"])
                small = s["size"] < 0.85 * maxsize and len(t.strip()) <= 6 and len(ln["spans"]) > 1
                sup = small and s["origin"][1] < baseline - 0.2 * maxsize
                sub = small and (not sup) and s["origin"][1] > baseline + 0.1 * maxsize
                sb = pymupdf.Rect(s["bbox"])
                strike = False
                if sb.width > 2:
                    mid_lo, mid_hi = sb.y0 + 0.38 * sb.height, sb.y0 + 0.68 * sb.height
                    for x0, x1, y in segs:
                        if mid_lo <= y <= mid_hi and min(x1, sb.x1) - max(x0, sb.x0) >= 0.7 * sb.width:
                            strike = True; break
                href = ""
                for r, uri in links:
                    if r.intersects(sb):
                        href = uri; break
                font_name = s["font"].lower().replace("-", "").replace(" ", "")
                sans = any(name in font_name for name in ("sans", "verdana", "arial", "helvetica", "calibri"))
                spans.append(Span(text=t, size=s["size"], bold=bold, italic=ital, sup=sup, sub=sub,
                                  strike=strike, link=href, font=s["font"], serif=bool(s["flags"] & 4) and not sans,
                                  small_caps="smallcap" in font_name, underline=bool(s.get("char_flags", 0) & 2),
                                  color=f"#{s['color']:06x}" if isinstance(s.get("color"), int) else None))
                texts.append(t)
                tot += n; sizes.append((s["size"], n))
                bold_chars += n if bold else 0; ital_chars += n if ital else 0
            text = "".join(texts)
            if not text.strip():
                continue
            wsz = sum(a * b for a, b in sizes) / max(sum(b for _, b in sizes), 1)
            out.append(Line(text=text.rstrip(), bbox=tuple(ln["bbox"]), size=wsz, bold=tot > 0 and bold_chars / tot > 0.6,
                            italic=tot > 0 and ital_chars / tot > 0.6, spans=spans, source="native", rtl=is_rtl(text),
                            original_text="".join(span["text"] for span in ln["spans"]),
                            writing_mode="vertical" if ln.get("wmode") == 1 else "horizontal"))
    distinct = {}
    for line in out:
        key = (line.text, tuple(round(value * 2) for value in line.bbox))
        distinct.setdefault(key, line)
    return list(distinct.values())


def recover_running_text(page, recognized: list[Line], band: float) -> list[Line]:
    """Replace corroborated OCR in the margins with the original decoded lines."""
    from rapidfuzz import fuzz
    from utils.textutil import norm_cmp

    opaque_fonts = {entry[3] for entry in page.get_fonts() if entry[2] == "Type3"}
    height = page.rect.height
    threshold = height * band
    native = [line for line in extract_lines(page)
              if (line.bbox[1] < threshold or line.bbox[3] > height - threshold)
              and line.spans and all(span.font not in opaque_fonts for span in line.spans)]
    if not native:
        return recognized
    used = set()
    merged = []
    for line in recognized:
        x0, y0, x1, y1 = line.bbox
        near = [(i, other) for i, other in enumerate(native) if i not in used
                and min(y1, other.bbox[3]) > max(y0, other.bbox[1])
                and min(x1, other.bbox[2]) > max(x0, other.bbox[0])]
        if line.rtl:
            merged.append(line)
            continue
        combined = " ".join(other.text for _, other in sorted(near, key=lambda pair: pair[1].bbox[0]))
        if combined and fuzz.ratio(norm_cmp(combined), norm_cmp(line.text)) >= 88:
            merged.extend(other for _, other in near)
            used.update(i for i, _ in near)
        else:
            merged.append(line)
    return merged


def extract_tables(page, settings) -> list[Block]:
    """Real tables with row/col spans derived from the cell grid. Conservative to avoid false positives."""
    if len(page.get_drawings()) < 4:
        return []
    try:
        try:
            found = page.find_tables(use_layout=False)
            if not found.tables:
                found = page.find_tables()
        except TypeError:
            found = page.find_tables()
    except Exception:
        return []
    W, H = page.rect.width, page.rect.height
    res = []
    for t in found.tables:
        cells = [c for c in (t.cells or []) if c]
        if len(cells) < 4:
            continue
        x0, y0, x1, y1 = t.bbox
        if (x1 - x0) * (y1 - y0) > 0.9 * W * H:
            continue
        xs = sorted({round(c[0], 0) for c in cells} | {round(c[2], 0) for c in cells})
        ys = sorted({round(c[1], 0) for c in cells} | {round(c[3], 0) for c in cells})
        def merge(vals):
            out = []
            for v in vals:
                if not out or v - out[-1] > 2:
                    out.append(v)
            return out
        xs, ys = merge(xs), merge(ys)
        if len(xs) < 3 or len(ys) < 3:
            continue
        def idx(edges, v):
            i = bisect.bisect_left(edges, v - 2.5)
            return min(max(i, 0), len(edges) - 1)
        grid_cells = []
        for c in cells:
            c0, c1 = idx(xs, c[0]), idx(xs, c[2])
            r0, r1 = idx(ys, c[1]), idx(ys, c[3])
            raw = page.get_text("text", clip=(c[0], c[1], c[2], c[3]))
            txt = " ".join(raw.split())
            grid_cells.append(Cell(text=xml_safe(txt), row=r0, col=c0, rowspan=max(1, r1 - r0),
                                   colspan=max(1, c1 - c0), original_text=raw, bbox=tuple(c)))
        nonempty = sum(1 for c in grid_cells if c.text) / len(grid_cells)
        nrows = max(c.row + c.rowspan for c in grid_cells)
        if nonempty < 0.3 or nrows < 2:
            continue
        for c in grid_cells:
            if c.row == 0 and nrows >= 2:
                c.header = True
        grid_cells.sort(key=lambda c: (c.row, c.col))
        res.append(Block(tag="table", bbox=tuple(t.bbox), cells=grid_cells, kind="table"))
    return res


def extract_figures(page, settings, table_boxes, lines=None) -> list[Block]:
    from hashlib import sha256
    import pymupdf
    from layout.reconstruct import CAPTION_RE, TABLE_WORDS
    W, H = page.rect.width, page.rect.height
    figs: list[Block] = []
    mn = settings.figure_min_pt
    def inside_table(r):
        return any(pymupdf.Rect(tb).contains(r) or pymupdf.Rect(tb).intersect(r).get_area() > 0.6 * r.get_area() for tb in table_boxes)
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        infos = []
    page_rect = page.rect
    captions = [line for line in lines or [] if CAPTION_RE.match(line.text) and not TABLE_WORDS.match(line.text)]

    def compact_figure(rect):
        return min(rect.width, rect.height) >= 16 and any(
            rect.x0 < line.bbox[2] and rect.x1 > line.bbox[0]
            and (0 <= line.bbox[1] - rect.y1 <= max(24, 2 * line.size)
                 or 0 <= rect.y0 - line.bbox[3] <= max(24, 2 * line.size))
            for line in captions)

    def text_overlap(r):
        if not lines:
            return 0.0, 0
        tot, n = 0.0, 0
        for l in lines:
            ir = pymupdf.Rect(l.bbox).intersect(r)
            if not ir.is_empty:
                tot += ir.get_area(); n += 1
        return tot / max(r.get_area(), 1.0), n
    for i in infos:
        r = pymupdf.Rect(i["bbox"]).intersect(page_rect)         # clip to the visible page
        if r.is_empty:
            continue
        ov, nl = text_overlap(r)
        if nl >= 3 and ov > 0.10:                                # image under real text = scanned text strip, not a figure
            continue
        if ((min(r.width, r.height) < mn and not compact_figure(r)) or r.get_area() > 0.92 * W * H
                or inside_table(r)):
            continue
        figs.append(Block(tag="figure", bbox=tuple(r), kind="figure", cls="raster",
                          fingerprint=i.get("digest", b"").hex(),
                          source_reference={"image": {"xref": i.get("xref"), "width": i.get("width"), "height": i.get("height")}}))
    try:
        for r in page.cluster_drawings():
            r = pymupdf.Rect(r).intersect(page_rect)
            if (r.is_empty or (min(r.width, r.height) < mn * 1.5 and not compact_figure(r)) or r.get_area() > 0.85 * W * H
                    or inside_table(r)):
                continue
            if any(pymupdf.Rect(f.bbox).intersect(r).get_area() > 0.5 * r.get_area() for f in figs):
                continue
            ov, nl = text_overlap(r)
            if nl >= 6 and ov > 0.25:                            # frame/box around running text
                continue
            preview = page.get_pixmap(matrix=pymupdf.Matrix(0.5, 0.5), clip=r,
                                      colorspace=pymupdf.csGRAY) if r.get_area() < 4_000_000 else None
            figs.append(Block(tag="figure", bbox=tuple(r), kind="figure", cls="vector",
                              fingerprint=sha256(preview.samples).hexdigest() if preview else ""))
    except Exception:
        pass
    return figs

"""Layout reconstruction: lines (+tables/figures) -> ordered semantic blocks.  Deterministic, no model required."""
from __future__ import annotations
import math, re, statistics
from dataclasses import dataclass, field
from layout.model import Line, Block, PageIR, Span, Cell
from utils.textutil import norm_cmp, norm_ws, is_rtl, xml_safe

CAPTION_RE = re.compile(r"^\s*(figure|fig\.?|table|tab\.?|chart|diagram|plate|abbildung|abb\.?|tabelle|obr\.?|obrázok|obrázek|tabulka|tabuľka|figura|tabella|tableau|figuur|tabel|afbeelding|schéma|图|図|表|그림|표|الشكل|شكل|جدول|الجدول)\s*[\dIVXivx]+", re.I)
TABLE_WORDS = re.compile(r"^\s*(table|tab\.?|tabelle|tabulka|tabuľka|tabella|tableau|tabel|表|표|جدول|الجدول)", re.I)
PAGENUM_RE = re.compile(r"^[\W_]*(?:page|pg\.?|p\.?|seite|strana|stránka|pagina|página|صفحة)?\s*\d{1,4}(?:\s*(?:/|of|von|z|di|sur)\s*\d{1,4})?[\W_]*$|^[\W_]*[ivxlcdm]{1,6}[\W_]*$", re.I)
NUMHEAD_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+([A-ZÀ-ÝĀ-Žا-ي\u3040-\u9fff\uac00-\ud7af])")
EQ_TAIL = re.compile(r"\(\s*\d{1,2}(?:\.\d{1,2})?\s*\)\s*$")
BULLET_RE = re.compile(r"^\s*([\u2022\u25cf\u25e6\u25aa\u25a0\u2023\u2043\u00b7•·▪◦○●■□➢➤►–—\-\*])\s+(?=\S)")
NUM_RE = re.compile(r"^\s*(\(?(?:\d{1,3}|[a-zA-Z]|[ivxIVX]{1,5})[.)])\s+(?=\S)")


@dataclass
class DocContext:
    body: dict = field(default_factory=dict)        # source -> body font size
    heading_sizes: dict = field(default_factory=dict)  # source -> [sizes desc]
    repeated: set = field(default_factory=set)      # signatures of running header/footer text
    npages: int = 1
    lang: str = "und"


def line_class(text: str) -> str:
    from utils.textutil import script_counts
    sc = script_counts(text)
    if not sc:
        return "latin"
    top = sc.most_common(1)[0][0]
    return "arab" if top == "arab" else "cjk" if top in ("hangul", "kana", "han") else "latin"


def size_key(src: str, cls: str) -> str:
    return f"{src}|{cls}"


def band_sig(text: str) -> str:
    return re.sub(r"\d+", "#", norm_cmp(text))[:80]


def compute_context(raw_pages: list[dict], settings) -> DocContext:
    ctx = DocContext(npages=len(raw_pages))
    by_src: dict[str, list[tuple[float, int]]] = {}
    sig_pages: dict[str, set] = {}
    for rp in raw_pages:
        H = rp["h"]
        for ln in rp["lines"]:
            by_src.setdefault(size_key(ln.source, line_class(ln.text)), []).append((round(ln.size * 2) / 2, max(len(ln.text), 1)))
            by_src.setdefault(ln.source, []).append((round(ln.size * 2) / 2, max(len(ln.text), 1)))
            cy = (ln.bbox[1] + ln.bbox[3]) / 2
            if cy < H * settings.header_band or cy > H * (1 - settings.header_band):
                sig_pages.setdefault(band_sig(ln.text), set()).add(rp["n"])
    for src, items in by_src.items():
        tot = sum(c for _, c in items)
        acc, body = 0, items[0][0]
        for s, c in sorted(items):
            acc += c
            if acc >= tot / 2:
                body = s; break
        ctx.body[src] = body
        share: dict[float, int] = {}
        for s, c in items:
            share[s] = share.get(s, 0) + c
        ctx.heading_sizes[src] = sorted([s for s, c in share.items() if s >= body * settings.heading_ratio and c / tot < 0.15], reverse=True)[:5]
    thr = max(2, math.ceil(0.2 * len(raw_pages)))
    ctx.repeated = {s for s, pgs in sig_pages.items() if len(pgs) >= thr and s.strip()}
    return ctx


# ---------------------------------------------------------------- reading order (XY-cut)
def _bb(it):
    return it.bbox


def xy_cut(items: list, med_h: float, page_w: float, rtl: bool, depth: int = 0) -> list[list]:
    if len(items) <= 1 or depth > 12:
        return [items]
    its = sorted(items, key=lambda i: (_bb(i)[1], _bb(i)[0]))
    # horizontal cut (top -> bottom)
    cuts, cur_max = [], _bb(its[0])[3]
    for k in range(1, len(its)):
        if _bb(its[k])[1] - cur_max > 0.9 * med_h:
            cuts.append(k)
        cur_max = max(cur_max, _bb(its[k])[3])
    if cuts:
        out, prev = [], 0
        for k in cuts + [len(its)]:
            out += xy_cut(its[prev:k], med_h, page_w, rtl, depth + 1); prev = k
        return out
    # vertical cut (columns)
    its = sorted(items, key=lambda i: _bb(i)[0])
    thr = max(5.0, 0.012 * page_w)
    cuts, cur_max = [], _bb(its[0])[2]
    for k in range(1, len(its)):
        if _bb(its[k])[0] - cur_max > thr:
            cuts.append(k)
        cur_max = max(cur_max, _bb(its[k])[2])
    if cuts:
        groups, prev = [], 0
        for k in cuts + [len(its)]:
            groups.append(its[prev:k]); prev = k
        if len(groups) > 1 and all(len(g) >= 1 for g in groups):
            if rtl:
                groups = groups[::-1]
            out = []
            for g in groups:
                out += xy_cut(g, med_h, page_w, rtl, depth + 1)
            return out
    # wide-item aware cut: a few full-width items (rules, spanning lines) must not hide a real column gutter
    rx0 = min(_bb(i)[0] for i in items); rx1 = max(_bb(i)[2] for i in items)
    wide = [i for i in items if (_bb(i)[2] - _bb(i)[0]) > 0.62 * (rx1 - rx0)]
    if wide and len(items) - len(wide) >= 4 and len(wide) < len(items):
        segs, cur = [], []
        wide_ids = {id(w) for w in wide}
        for it in sorted(items, key=lambda i: (_bb(i)[1], _bb(i)[0])):
            if id(it) in wide_ids:
                if cur: segs.append(cur); cur = []
                segs.append([it])
            else:
                cur.append(it)
        if cur: segs.append(cur)
        if len(segs) > 1:
            out = []
            for sgm in segs:
                out += xy_cut(sgm, med_h, page_w, rtl, depth + 1) if len(sgm) > 1 else [sgm]
            return out
    return [items]


# ---------------------------------------------------------------- paragraph building
def _merge_same_row(lines: list[Line]) -> list[Line]:
    out: list[Line] = []
    for ln in sorted(lines, key=lambda l: (round(l.bbox[1] / 2), l.bbox[0])):
        if out:
            p = out[-1]
            ov = min(p.bbox[3], ln.bbox[3]) - max(p.bbox[1], ln.bbox[1])
            h = min(p.bbox[3] - p.bbox[1], ln.bbox[3] - ln.bbox[1])
            gap = ln.bbox[0] - p.bbox[2]
            if h > 0 and ov > 0.55 * h and -2 < gap < 1.6 * max(p.size, 6):
                a, b = (ln, p) if ln.rtl and p.rtl else (p, ln)
                sep = "" if (not a.text.endswith(" ") and gap < 0.15 * max(p.size, 6)) else " "
                out[-1] = Line(text=a.text + sep + b.text, bbox=(min(p.bbox[0], ln.bbox[0]), min(p.bbox[1], ln.bbox[1]), max(p.bbox[2], ln.bbox[2]), max(p.bbox[3], ln.bbox[3])),
                               size=max(p.size, ln.size), bold=p.bold and ln.bold, italic=p.italic and ln.italic,
                               spans=a.spans + ([Span(text=sep)] if sep else []) + b.spans, conf=min(p.conf, ln.conf), source=p.source, rtl=p.rtl, grp=p.grp)
                continue
        out.append(ln)
    return out


def _is_marker(text: str):
    m = BULLET_RE.match(text)
    if m:
        return "ul", m.end()
    m = NUM_RE.match(text)
    if m:
        return "ol", m.end()
    return None, 0


def _group_paragraphs(lines: list[Line], settings) -> list[list[Line]]:
    paras: list[list[Line]] = []
    left = min((l.bbox[0] for l in lines), default=0)
    right = max((l.bbox[2] for l in lines), default=0)
    for ln in lines:
        if not paras:
            paras.append([ln]); continue
        cur = paras[-1]; prev = cur[-1]
        ph = max(prev.bbox[3] - prev.bbox[1], 1.0)
        gap = ln.bbox[1] - prev.bbox[3]
        new = False
        ocr_src = ln.source == "ocr" or prev.source == "ocr"
        size_tol = 0.30 if ocr_src else 0.14           # OCR line heights are noisy (ascender/descender dependent)
        if ln.grp != -1 and prev.grp != -1 and ln.grp != prev.grp and gap > 0.40 * ph:
            new = True
        elif gap > (0.62 if ocr_src else 0.55) * ph:
            new = True
        elif abs(ln.size - prev.size) > size_tol * max(prev.size, 1):
            new = True
        elif _is_marker(ln.text)[0]:
            new = True
        elif not ocr_src and ln.bold != prev.bold and (prev.bold or ln.bold) and len(prev.text) < 110:
            new = True
        elif (ln.bbox[0] - left) > 0.9 * max(ln.size, 6) and (prev.bbox[2] > right - 0.08 * (right - left)) and not _is_marker(cur[0].text)[0] and abs(prev.bbox[0] - left) < 2:
            new = True
        if new:
            paras.append([ln])
        else:
            cur.append(ln)
    return paras


_CJK_END = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]$")


def join_lines(lines: list[Line], dehyphenate: bool = True) -> tuple[str, list[Span]]:
    text = ""
    spans: list[Span] = []
    for i, ln in enumerate(lines):
        t = ln.text.strip() if i else ln.text.lstrip()
        t = t.rstrip()
        sp = [Span(**{**s.__dict__}) for s in ln.spans] if ln.spans else [Span(text=t)]
        if i:
            prev = text
            if dehyphenate and prev.endswith("-") and len(prev) > 2 and prev[-2].isalpha() and t[:1].islower():
                text = prev[:-1]
                if spans:
                    spans[-1].text = spans[-1].text.rstrip()[:-1]
                sep = ""
            elif _CJK_END.search(prev) or (t and _CJK_END.match(t[:1]) and not prev.endswith(" ")):
                sep = ""
            else:
                sep = " "
            if sep:
                spans.append(Span(text=sep))
            text += sep
        # make span text consistent with trimmed line text
        if sp and ln.spans:
            sp[0].text = sp[0].text.lstrip() if i == 0 else sp[0].text.lstrip()
            sp[-1].text = sp[-1].text.rstrip()
        spans += [s for s in sp if s.text != ""] if ln.spans else sp
        text += t
    return xml_safe(text), spans


def _marker_strip(text: str, spans: list[Span], n: int) -> tuple[str, list[Span]]:
    """Remove a list marker (n chars) from text and the first spans."""
    out, left = [], n
    for s in spans:
        if left <= 0:
            out.append(s); continue
        if len(s.text) <= left:
            left -= len(s.text); continue
        s2 = Span(**{**s.__dict__}); s2.text = s.text[left:]; left = 0; out.append(s2)
    return text[n:].lstrip(), out


# ---------------------------------------------------------------- page assembly
def build_page(n: int, w: float, h: float, lines: list[Line], tables: list[Block], figures: list[Block], ctx: DocContext, settings, source: str, doc_rtl: bool = False) -> PageIR:
    page = PageIR(n=n, width=w, height=h, source=source)
    body = ctx.body.get(source) or ctx.body.get("native") or ctx.body.get("ocr") or 10.0
    hsizes = ctx.heading_sizes.get(source, [])
    hdr, ftr, flow = [], [], []
    band = settings.header_band
    tboxes = [t.bbox for t in tables]

    def in_box(ln, boxes):
        cx, cy = (ln.bbox[0] + ln.bbox[2]) / 2, (ln.bbox[1] + ln.bbox[3]) / 2
        return any(b[0] - 1 <= cx <= b[2] + 1 and b[1] - 1 <= cy <= b[3] + 1 for b in boxes)

    for ln in lines:
        cy = (ln.bbox[1] + ln.bbox[3]) / 2
        sig = band_sig(ln.text)
        in_band = cy < h * band or cy > h * (1 - band)
        is_run = in_band and (sig in ctx.repeated or PAGENUM_RE.match(ln.text.strip()) is not None) and len(ln.text) < 160
        if is_run:
            (hdr if cy < h / 2 else ftr).append(ln); continue
        if in_box(ln, tboxes):
            continue
        if in_box(ln, [f.bbox for f in figures if f.bbox and (f.cls == "vector" or source == "ocr")]) and not CAPTION_RE.match(ln.text):
            continue
        flow.append(ln)

    for grp, tag, kind in ((hdr, "header", "publication_metadata"), (ftr, "footer", "publication_metadata")):
        lines_in_band = sorted(grp, key=lambda l: (l.bbox[1], l.bbox[0]))
        if settings.schema_profile != "sample":
            lines_in_band = _merge_same_row(lines_in_band)
        for ln in lines_in_band:
            page.blocks.append(Block(tag=tag, text=xml_safe(ln.text.strip()), bbox=ln.bbox, kind=kind,
                                     spans=ln.spans, source=ln.source, size=ln.size, conf=ln.conf))
    if ftr:   # footers go last
        page.blocks = [b for b in page.blocks if b.tag == "header"] + [b for b in page.blocks if b.tag == "footer"]
    head_blocks = [b for b in page.blocks if b.tag == "header"]
    foot_blocks = [b for b in page.blocks if b.tag == "footer"]

    # items for XY cut: lines + pseudo items (tables, figures)
    items: list = list(flow) + list(tables) + [f for f in figures]
    heights = [l.bbox[3] - l.bbox[1] for l in flow] or [10.0]
    med_h = statistics.median(heights)
    regions = xy_cut(items, med_h, w, doc_rtl) if items else []

    blocks: list[Block] = []
    for reg in regions:
        reg_lines = [i for i in reg if isinstance(i, Line)]
        pseudo = [i for i in reg if isinstance(i, Block)]
        seq: list = [("L", l) for l in reg_lines] + [("B", b) for b in pseudo]
        seq.sort(key=lambda t: (t[1].bbox[1], t[1].bbox[0]))
        run: list[Line] = []
        def flush():
            nonlocal run
            if run:
                blocks.extend(_lines_to_blocks(_merge_same_row(run), ctx, settings, body, hsizes, w, h, source))
                run = []
        for kind, it in seq:
            if kind == "L":
                run.append(it)
            else:
                flush(); blocks.append(it)
        flush()

    blocks = _attach_captions(blocks)
    page.blocks = head_blocks + blocks + foot_blocks
    return page


def _lines_to_blocks(lines: list[Line], ctx, settings, body: float, hsizes: list[float], w: float, h: float, source: str) -> list[Block]:
    out: list[Block] = []
    body_default, hsizes_default = body, hsizes
    for para in _group_paragraphs(lines, settings):
        text, spans = join_lines(para)
        if not text.strip():
            continue
        first = para[0]
        size = statistics.mean(l.size for l in para)
        key = size_key(source, line_class(" ".join(l.text for l in para)))
        body = ctx.body.get(key, body_default); hsizes = ctx.heading_sizes.get(key, hsizes_default)
        x0 = min(l.bbox[0] for l in para); y0 = min(l.bbox[1] for l in para)
        x1 = max(l.bbox[2] for l in para); y1 = max(l.bbox[3] for l in para)
        bbox = (x0, y0, x1, y1)
        conf = min(l.conf for l in para)
        bold = all(l.bold for l in para)
        nwords = len(text.split())
        if sum(c.isalnum() for c in text) < 2 and len(text.strip()) >= 3:
            out.append(Block(tag="hr", bbox=bbox, kind="decoration", source=source)); continue
        mk, mlen = _is_marker(text)
        alpha = [c for c in text if c.isalpha()]
        caps = bool(alpha) and sum(c.isupper() for c in alpha) / len(alpha) > 0.85 and len(alpha) >= 3
        blk = Block(tag="p", text=text, bbox=bbox, spans=spans, conf=conf, source=source, size=size)
        sym = sum(1 for c in text if not (c.isalpha() or c.isspace()))
        numbered_formula = EQ_TAIL.search(text) or re.search(r"\(\d+\.\d+\)", text)
        if not numbered_formula and x0 > 0.26 * w and "=" in text:
            numbered_formula = re.search(r"(?<![\d.])\d{2}\)$", text)
        is_eq = bool(numbered_formula and len(para) <= 5 and nwords <= 36 and (
            sym / max(len(text), 1) > 0.22 or (x0 > 0.20 * w and re.search(r"[=±×÷√∑∫]", text))))
        mnum = NUMHEAD_RE.match(text)
        # footnote: small text in the lower part of the page
        if is_eq:
            blk.kind = "equation"
        elif size < 0.88 * body and y0 > 0.68 * h and re.match(r"^\s*([\d*†‡§¹²³]+[\s.)]|\[\d+\])", text):
            blk.tag = "aside"; blk.kind = "footnote"
        elif mnum and len(para) == 1 and nwords <= 14 and not text.rstrip().endswith((".", ",", ";", ":")) and size >= body * 0.9 and (mnum.group(1).count(".") >= 1 or bold or size >= body * 1.05 or nwords <= 8):
            depth = mnum.group(1).count(".") + 1
            blk.tag = f"h{min(depth + 1, 6)}"; blk.kind = "section_heading"
        elif CAPTION_RE.match(text) and nwords < 80:
            blk.kind = "figure_caption"; blk.cls = "caption"
        elif hsizes and size >= hsizes[-1] * 0.97 and nwords <= 40 and len(para) <= 4 and size >= body * settings.heading_ratio:
            lvl = 1 + min(range(len(hsizes)), key=lambda i: abs(hsizes[i] - size))
            blk.tag = f"h{min(lvl, 5)}"; blk.kind = "section_heading"
        elif (bold or (caps and bold)) and len(para) == 1 and nwords <= 12 and size >= body * 0.98 and (text[:1].isupper() or text[:1].isdigit() or ord(text[:1]) > 0x250) and not text.rstrip().endswith((".", ",", ";", ":")) and not mk:
            blk.tag = f"h{min(len(hsizes) + 1, 6)}"; blk.kind = "section_heading"
        elif mk:
            blk.tag = "li"; blk.kind = mk
            blk.marker = text[:mlen].strip()
            blk.text, blk.spans = _marker_strip(text, spans, mlen)
        out.append(blk)
    # group consecutive li into lists
    grouped: list[Block] = []
    for b in out:
        if b.tag == "li":
            if grouped and grouped[-1].tag in ("ul", "ol") and grouped[-1].kind == b.kind:
                grouped[-1].children.append(b)
                gb = grouped[-1].bbox
                grouped[-1].bbox = (min(gb[0], b.bbox[0]), min(gb[1], b.bbox[1]), max(gb[2], b.bbox[2]), max(gb[3], b.bbox[3]))
            else:
                grouped.append(Block(tag=b.kind, kind="list", bbox=b.bbox, children=[b], source=b.source))
                grouped[-1].kind = b.kind
        else:
            grouped.append(b)
    for g in grouped:
        if g.tag in ("ul", "ol"):
            g.kind = "body"
            for li in g.children:
                li.kind = "body"
    return grouped


def _attach_captions(blocks: list[Block]) -> list[Block]:
    used = set()
    objs = [i for i, b in enumerate(blocks) if b.tag in ("table", "figure")]
    for i, b in enumerate(blocks):
        if not (b.tag == "p" and b.kind == "figure_caption"):
            continue
        is_tab = bool(TABLE_WORDS.match(b.text))
        best, bd = None, 1e9
        for j in objs:
            o = blocks[j]
            if j in used or o.caption:
                continue
            if is_tab != (o.tag == "table") and not (o.tag == "figure" and not is_tab):
                continue
            ox0, oy0, ox1, oy1 = o.bbox
            bx0, by0, bx1, by1 = b.bbox
            ov = min(ox1, bx1) - max(ox0, bx0)
            if ov < 0.3 * min(ox1 - ox0, bx1 - bx0):
                continue
            d = min(abs(by0 - oy1), abs(oy0 - by1))
            if d < bd and d < 70:
                best, bd = j, d
        if best is not None:
            blocks[best].caption = b.text
            used.add(best); b.cls = "__consumed__"
    return [b for b in blocks if b.cls != "__consumed__"]

"""Layout reconstruction: lines (+tables/figures) -> ordered semantic blocks.  Deterministic, no model required."""
from __future__ import annotations
import math, re, statistics
from dataclasses import dataclass, field
from layout.model import Line, Block, PageIR, Span, Cell
from layout.kinds import ABSTRACT, CLASSIFICATION, KEYW, metadata_kind
from utils.textutil import norm_cmp, norm_ws, is_rtl, xml_safe

CAPTION_RE = re.compile(r"^\s*(figure|fig\.?|table|tab\.?|chart|diagram|plate|abbildung|abb\.?|tabelle|obr\.?|obrázok|obrázek|tabulka|tabuľka|figura|tabella|tableau|figuur|tabel|afbeelding|schéma|图|図|表|그림|표|الشكل|شكل|جدول|الجدول)\s*[\dIVXivx]+", re.I)
TABLE_WORDS = re.compile(r"^\s*(table|tab\.?|tabelle|tabulka|tabuľka|tabella|tableau|tabel|表|표|جدول|الجدول)", re.I)
PAGENUM_RE = re.compile(r"^[\W_]*(?:page|pg\.?|p\.?|seite|strana|stránka|pagina|página|صفحة)?\s*\d{1,4}(?:\s*(?:/|of|von|z|di|sur)\s*\d{1,4})?[\W_]*$|^[\W_]*[ivxlcdm]{1,6}[\W_]*$", re.I)
NUMHEAD_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+([A-ZÀ-ÝĀ-Žا-ي\u3040-\u9fff\uac00-\ud7af])")
EQ_TAIL = re.compile(r"\(\s*\d{1,2}(?:\.\d{1,2})?\s*\)\s*$")
SIMPLE_EQUATION = re.compile(r"(?u)\s*[^\W_](?:[\d₀-₉]*)\s*=\s*[\w\s.+−×÷*/^()√≤≥]+")
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
    key = norm_cmp(text)
    return (re.sub(r"\d+", "#", key) if PAGENUM_RE.fullmatch(text.strip()) else key)[:160]


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


def column_groups(items, med_h, page_w):
    """Find persistent gutters with text on both sides over the same vertical band."""
    thr = max(5.0, 0.012 * page_w)
    groups, right = [], 0
    for item in sorted(items, key=lambda item: _bb(item)[0]):
        box = _bb(item)
        if not groups or box[0] - right > thr:
            groups.append([])
            right = box[2]
        groups[-1].append(item)
        right = max(right, box[2])
    if len(groups) < 2:
        return []
    ranges = [(min(_bb(item)[1] for item in group), max(_bb(item)[3] for item in group)) for group in groups]
    if any(min(last, end) - max(first, start) < med_h
           for (first, last), (start, end) in zip(ranges, ranges[1:])):
        return []
    if any(len(group) == 1 and _bb(group[0])[3] - _bb(group[0])[1] < med_h * 2 for group in groups):
        return []
    return groups


def xy_cut(items: list, med_h: float, page_w: float, rtl: bool, depth: int = 0) -> list[list]:
    if len(items) <= 1 or depth > 12:
        return [items]
    if columns := column_groups(items, med_h, page_w):
        if rtl:
            columns.reverse()
        return [part for group in columns for part in xy_cut(group, med_h, page_w, rtl, depth + 1)]
    # wide-item aware cut: a few full-width items (rules, spanning lines) must not hide a real column gutter
    rx0 = min(_bb(i)[0] for i in items); rx1 = max(_bb(i)[2] for i in items)
    wide = [i for i in items if (_bb(i)[2] - _bb(i)[0]) > 0.62 * (rx1 - rx0)]
    wide_ids = {id(item) for item in wide}
    narrowed = [item for item in items if id(item) not in wide_ids]
    if wide and len(narrowed) >= 4 and column_groups(narrowed, med_h, page_w):
        segs, cur = [], []
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
    # Horizontal gaps remain useful for isolated headings, paragraphs and figures.
    ordered = sorted(items, key=lambda item: (_bb(item)[1], _bb(item)[0]))
    cuts, cur_max = [], _bb(ordered[0])[3]
    for index, item in enumerate(ordered[1:], 1):
        if _bb(item)[1] - cur_max > .9 * med_h:
            cuts.append(index)
        cur_max = max(cur_max, _bb(item)[3])
    if not cuts:
        return [ordered]
    return [part for start, end in zip([0, *cuts], [*cuts, len(ordered)])
            for part in xy_cut(ordered[start:end], med_h, page_w, rtl, depth + 1)]


# ---------------------------------------------------------------- paragraph building
def _merge_same_row(lines: list[Line]) -> list[Line]:
    out: list[Line] = []
    for ln in sorted(lines, key=lambda l: (round(l.bbox[1] / 2), -l.bbox[2] if l.rtl else l.bbox[0])):
        if out:
            p = out[-1]
            ov = min(p.bbox[3], ln.bbox[3]) - max(p.bbox[1], ln.bbox[1])
            h = min(p.bbox[3] - p.bbox[1], ln.bbox[3] - ln.bbox[1])
            gap = ln.bbox[0] - p.bbox[2]
            if (h > 0 and ov > 0.55 * h and -2 < gap < 1.6 * max(p.size, 6) and not (p.rtl or ln.rtl)
                    and p.writing_mode == ln.writing_mode == "horizontal"):
                a, b = p, ln
                sep = "" if (not a.text.endswith(" ") and gap < 0.15 * max(p.size, 6)) else " "
                out[-1] = Line(text=a.text + sep + b.text, bbox=(min(p.bbox[0], ln.bbox[0]), min(p.bbox[1], ln.bbox[1]), max(p.bbox[2], ln.bbox[2]), max(p.bbox[3], ln.bbox[3])),
                               size=max(p.size, ln.size), bold=p.bold and ln.bold, italic=p.italic and ln.italic,
                               spans=a.spans + ([Span(text=sep)] if sep else []) + b.spans, conf=min(p.conf, ln.conf),
                               source=p.source, rtl=p.rtl, grp=p.grp,
                               original_text=sep.join(line.original_text if line.original_text is not None else line.text for line in (p, ln)))
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


def _group_paragraphs(lines: list[Line], first_page: bool, page_height: float) -> list[list[Line]]:
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
        running_metadata = (first_page and ln.bbox[1] < 0.20 * page_height
                            and (metadata_kind(ln.text) or metadata_kind(prev.text)))
        if running_metadata or KEYW.match(ln.text) or CLASSIFICATION.match(ln.text) or ABSTRACT.fullmatch(ln.text.strip()):
            new = True
        elif ln.grp != -1 and prev.grp != -1 and ln.grp != prev.grp and gap > 0.40 * ph:
            new = True
        elif gap > (0.62 if ocr_src else 0.55) * ph:
            new = True
        elif abs(ln.size - prev.size) > size_tol * max(prev.size, 1):
            new = True
        elif ln.rtl != prev.rtl or ln.writing_mode != prev.writing_mode:
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
        flow.append(ln)

    for grp, tag, kind in ((hdr, "header", "publication_metadata"), (ftr, "footer", "publication_metadata")):
        lines_in_band = sorted(grp, key=lambda l: (l.bbox[1], l.bbox[0]))
        if settings.schema_profile != "sample":
            lines_in_band = _merge_same_row(lines_in_band)
        for ln in lines_in_band:
            page.blocks.append(Block(tag=tag, text=xml_safe(ln.text.strip()), bbox=ln.bbox, kind=kind,
                                     spans=ln.spans, source=ln.source, size=ln.size, conf=ln.conf,
                                     original_text=ln.original_text or ln.text, writing_mode=ln.writing_mode))
    if ftr:   # footers go last
        page.blocks = [b for b in page.blocks if b.tag == "header"] + [b for b in page.blocks if b.tag == "footer"]
    head_blocks = [b for b in page.blocks if b.tag == "header"]
    foot_blocks = [b for b in page.blocks if b.tag == "footer"]

    # items for XY cut: lines + pseudo items (tables, figures)
    items: list = list(flow) + list(tables) + [f for f in figures]
    heights = [l.bbox[3] - l.bbox[1] for l in flow] or [10.0]
    med_h = statistics.median(heights)
    regions = xy_cut(items, med_h, w, doc_rtl) if items else []
    page.diag["reading_regions"] = len(regions)

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
                blocks.extend(_lines_to_blocks(_merge_same_row(run), ctx, settings, body, hsizes, w, h, source, n == 1))
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


def _lines_to_blocks(lines: list[Line], ctx, settings, body: float, hsizes: list[float], w: float, h: float, source: str, first_page: bool) -> list[Block]:
    out: list[Block] = []
    body_default, hsizes_default = body, hsizes
    for para in _group_paragraphs(lines, first_page, h):
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
        if re.fullmatch(r"[\s._*=─—-]{3,}", text) and len(text.strip()) >= 3:
            out.append(Block(tag="hr", bbox=bbox, kind="decoration", source=source)); continue
        mk, mlen = _is_marker(text)
        alpha = [c for c in text if c.isalpha()]
        caps = bool(alpha) and sum(c.isupper() for c in alpha) / len(alpha) > 0.85 and len(alpha) >= 3
        blk = Block(tag="p", text=text, bbox=bbox, spans=spans, conf=conf, source=source, size=size,
                    original_text="\n".join(line.original_text or line.text for line in para),
                    writing_mode=first.writing_mode, line_boxes=[line.bbox for line in para])
        sym = sum(1 for c in text if not (c.isalpha() or c.isspace()))
        numbered_formula = EQ_TAIL.search(text) or re.search(r"\(\d+\.\d+\)", text)
        if not numbered_formula and x0 > 0.26 * w and "=" in text:
            numbered_formula = re.search(r"(?<![\d.])\d{2}\)$", text)
        is_eq = bool(numbered_formula and len(para) <= 5 and nwords <= 36 and (
            sym / max(len(text), 1) > 0.22 or (x0 > 0.20 * w and re.search(r"[=±×÷√∑∫]", text))))
        is_eq |= (len(para) == 1 and nwords <= 10 and SIMPLE_EQUATION.fullmatch(text) is not None
                  and (re.search(r"[×÷√^]", text) is not None or
                       (x0 >= .20 * w and re.search(r"[+−*/²³₀-₉]|(?<==)\s*\d", text) is not None)))
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
            lvl = 2 + min(range(len(hsizes)), key=lambda i: abs(hsizes[i] - size))
            blk.tag = f"h{min(lvl, 6)}"; blk.kind = "section_heading"
        elif (bold or (caps and bold)) and len(para) == 1 and nwords <= 12 and size >= body * 0.98 and (text[:1].isupper() or text[:1].isdigit() or ord(text[:1]) > 0x250) and not text.rstrip().endswith((".", ",", ";", ":")) and not mk:
            blk.tag = f"h{min(len(hsizes) + 2, 6)}"; blk.kind = "section_heading"
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


def place_visuals(page):
    """Place adjunct PDF figures using positioned text without changing text order."""
    if page.ocr_engine not in {"glm-ocr", "paddleocr-vl"}:
        return
    blocks = page.blocks
    for visual in [b for b in blocks if b.tag in {"figure", "table"} and b.bbox and b.source == "native"]:
        x0, y0, x1, y1 = visual.bbox
        relevant = [(i, block) for i, block in enumerate(blocks)
                    if block.bbox and block is not visual and block.tag not in {"header", "footer", "figure", "table"}
                    and min(x1, block.bbox[2]) - max(x0, block.bbox[0]) > .3 * min(x1 - x0, block.bbox[2] - block.bbox[0])]
        after = [i for i, block in relevant if block.bbox[1] >= y1 - 2]
        before = [i for i, block in relevant if block.bbox[3] <= y0 + 2]
        if after and (not before or max(before) < min(after)):
            old = blocks.index(visual)
            dest = min(after)
            blocks.pop(old)
            blocks.insert(dest - (old < dest), visual)
    page.blocks = _attach_captions(blocks)


def order_positioned_blocks(page, rtl):
    """Prefer a verified column geometry when every flow block can be placed."""
    running = {"header", "footer", "aside"}
    flow = [block for block in page.blocks if block.tag not in running]
    if len(flow) < 4 or any(not block.bbox for block in flow):
        return
    prose = [block for block in flow if block.tag == "p" and block.kind in {"", "body"}
             and block.bbox[2] - block.bbox[0] < .60 * page.width]
    if len(prose) < 4:
        return
    line_height = statistics.median([
        (block.bbox[3] - block.bbox[1]) / max(1, len(block.line_boxes)) for block in prose])
    if not column_groups(prose, line_height, page.width):
        return
    ordered = [block for group in xy_cut(flow, line_height, page.width, rtl) for block in group]
    page.blocks = ([block for block in page.blocks if block.tag == "header"] + ordered
                   + [block for block in page.blocks if block.tag in {"aside", "footer"}])
    page.diag["reading_order_method"] = "positioned-column-gutters"


def _attach_captions(blocks: list[Block]) -> list[Block]:
    used = set()
    for caption in blocks:
        if not (caption.tag == "p" and caption.bbox
                and (caption.kind == "figure_caption" or CAPTION_RE.match(caption.text))):
            continue
        target = caption_target(caption.bbox, bool(TABLE_WORDS.match(caption.text)), blocks)
        if target is not None:
            target.caption = caption.text
            target.source_reference.update(caption_bbox=caption.bbox, caption_size=caption.size,
                                           caption_source=caption.source)
            used.add(id(caption))
    return [block for block in blocks if id(block) not in used]


def caption_target(box, is_table, blocks):
    """Use overlap and separation to resolve local captions and reject near-ties."""
    x0, y0, x1, y1 = box
    choices = []
    for visual in blocks:
        if (visual.tag != ("table" if is_table else "figure") or not visual.bbox
                or visual.caption or visual.kind == "equation"):
            continue
        left, top, right, bottom = visual.bbox
        smaller = min(right - left, x1 - x0)
        overlap = min(x1, right) - max(x0, left)
        distance = max(y0 - bottom, top - y1)
        if smaller <= 0 or overlap < .35 * smaller or not -3 <= distance <= 70:
            continue
        mismatch = abs((x0 + x1) - (left + right)) / max(right - left, x1 - x0)
        choices.append((max(distance, 0) + 12 * (1 - overlap / smaller) + 6 * mismatch, visual))
    choices.sort(key=lambda item: item[0])
    if choices and (len(choices) == 1 or choices[1][0] - choices[0][0] > 4):
        return choices[0][1]
    return None

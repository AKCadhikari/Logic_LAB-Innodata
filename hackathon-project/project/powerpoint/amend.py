"""PowerPoint amendment engine: proof-read a deck against the source PDF and apply targeted, formatting-preserving fixes.

Evidence = the PDF's reconstructed text (OCR'd where the text layer is broken).  Every change is logged with a
confidence and a recommended action; low-confidence differences are flagged for review, never applied silently."""
from __future__ import annotations
import copy, json, re, statistics, time, zipfile
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
from rapidfuzz import fuzz, process

from comparison.differ import align_correct, ws_lower
from layout.model import DocIR, ir_from_dict

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
FURNITURE = {"slide-number", "footer", "rule", "date"}


@dataclass
class Unit:
    text: str
    norm: str
    page: int
    idx: int
    kind: str
    conf: float = 100.0
    source: str = "native"


def load_ir(pdf: Path, work: Path, settings, audit) -> DocIR:
    p = work / "ir" / f"{pdf.stem}.json"
    if not p.exists():                                        # deck processed without its PDF run: process it now
        from agents.orchestrator import process_pdf
        import tempfile
        process_pdf(pdf, settings, Path(tempfile.mkdtemp()), work, audit)
    return ir_from_dict(json.loads(p.read_text(encoding="utf-8")))


def build_evidence(ir: DocIR) -> tuple[list[Unit], list[tuple[str, str, int, int]]]:
    units: list[Unit] = []
    heads: list[tuple[str, str, int, int]] = []     # (tag, text, page, unit index of next unit)
    k = 0
    def add(text, page, kind, conf=100.0, src="native"):
        nonlocal k
        t = re.sub(r"\s+", " ", text).strip()
        if len(t) < 2:
            return
        units.append(Unit(t, ws_lower(t), page, k, kind, conf, src)); k += 1
    for pg in ir.pages:
        for b in pg.blocks:
            conf = b.conf; bsrc = b.source
            if b.tag in ("header", "footer"):
                continue
            if b.tag.startswith("h") and b.tag[1:].isdigit():
                heads.append((b.tag, re.sub(r"\s+", " ", b.text).strip(), pg.n, k)); add(b.text, pg.n, "heading", conf, bsrc)
            elif b.tag in ("ul", "ol"):
                for li in b.children:
                    add(li.text, pg.n, "li", li.conf, bsrc)
            elif b.tag == "table":
                for c in b.cells:
                    add(c.text, pg.n, "cell", 100.0, bsrc)
                if b.caption: add(b.caption, pg.n, "caption")
            elif b.tag == "figure":
                if b.caption: add(b.caption, pg.n, "caption")
            else:
                if b.kind == "equation":
                    add(b.text, pg.n, "equation", conf, bsrc); continue
                t = re.sub(r"\s+", " ", b.text).strip()
                if re.match(r"^\d{1,2}(?:\.\d{1,2}){0,3}\.?\s+\S", t) and len(t) <= 90 and len(t.split()) <= 12 and not t.endswith((".", ",")):
                    heads.append(("h?", t, pg.n, k)); add(b.text, pg.n, "heading", conf, bsrc)
                else:
                    add(b.text, pg.n, b.kind or "p", conf, bsrc)
    return units, heads


# ------------------------------------------------------------------------------------------ pptx helpers
def _is_group(sh):
    return sh.shape_type == MSO_SHAPE_TYPE.GROUP


def iter_shapes(shapes):
    for sh in shapes:
        if _is_group(sh):
            yield from iter_shapes(sh.shapes)
        else:
            yield sh


def _ph_type(sh):
    try:
        return sh.placeholder_format.type if sh.is_placeholder else None
    except Exception:
        return None


def is_furniture(sh) -> bool:
    t = _ph_type(sh)
    return sh.name.lower() in FURNITURE or t in (PP_PLACEHOLDER.SLIDE_NUMBER, PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.DATE)


def para_text(p) -> str:
    return "".join(r.text for r in p.runs)


def text_paragraphs(sh):
    """Yield (kind, paragraph) for a shape: text-frame paragraphs and table cell paragraphs."""
    if getattr(sh, "has_table", False) and sh.has_table:
        for r in sh.table.rows:
            for c in r.cells:
                for p in c.text_frame.paragraphs:
                    yield "cell", p
    elif sh.has_text_frame:
        for p in sh.text_frame.paragraphs:
            yield "text", p


def apply_text_edit(para, new: str):
    """Rewrite paragraph text by editing only the runs that contain changed characters (formatting preserved)."""
    runs = list(para.runs)
    old = "".join(r.text for r in runs)
    if old == new:
        return
    if not runs:
        para.add_run().text = new; return
    ops = SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in reversed(ops):
        if tag == "equal":
            continue
        seg = new[j1:j2]
        bounds, pos = [], 0
        for r in runs:
            bounds.append((pos, pos + len(r.text))); pos += len(r.text)
        def run_at(ch, prefer_left=False):
            for ri, (s, e) in enumerate(bounds):
                if s <= ch < e or (prefer_left and s < ch <= e):
                    return ri
            return len(runs) - 1
        first = run_at(i1, prefer_left=(i1 == i2 and i1 > 0)) if i1 < i2 or i1 == i2 else 0
        remaining = i2 - i1
        s0, _ = bounds[first]
        r0 = runs[first]
        local = i1 - s0
        take = min(remaining, len(r0.text) - local) if remaining else 0
        r0.text = r0.text[:local] + seg + r0.text[local + take:]
        remaining -= take
        ri = first + 1
        while remaining > 0 and ri < len(runs):
            t = runs[ri].text
            cut = min(remaining, len(t))
            runs[ri].text = t[cut:]; remaining -= cut; ri += 1


def _to_slide_number_field(para) -> bool:
    ps = para._p
    if ps.findall(f"{{{A_NS}}}fld"):
        return False
    rs = ps.findall(f"{{{A_NS}}}r")
    if not rs or not re.fullmatch(r"\s*\d+\s*", "".join((r.find(f"{{{A_NS}}}t").text or "") for r in rs)):
        return False
    r0 = rs[0]
    fld = etree.Element(f"{{{A_NS}}}fld", id="{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}", type="slidenum")
    rpr = r0.find(f"{{{A_NS}}}rPr")
    if rpr is not None:
        fld.append(copy.deepcopy(rpr))
    t = etree.SubElement(fld, f"{{{A_NS}}}t"); t.text = "‹#›"
    r0.addprevious(fld)
    for r in rs:
        ps.remove(r)
    return True


# ------------------------------------------------------------------------------------------ main engine
def amend_deck(pdf: Path, pptx: Path, target: Path, work: Path, settings, audit) -> dict:
    t0 = time.time()
    ir = load_ir(pdf, work, settings, audit)
    units, heads = build_evidence(ir)
    from comparison.differ import TOK
    vocab = Counter(t.lower() for u in units for t in TOK.findall(u.text) if re.match(r"\w", t))
    prs = Presentation(str(pptx))
    doc_id = pdf.stem
    changes: list[dict] = []
    def log(slide, shape, typ, old, new, conf, action, reason="", para=None):
        rec = {"document": doc_id, "deck": pptx.name, "original_location": {"slide": slide, "shape": shape, "paragraph": para},
               "new_location": {"slide": slide, "shape": shape, "paragraph": para}, "change_type": typ, "old_content": old, "new_content": new,
               "confidence": round(conf, 3), "action": action, "recommended_action": "none" if action == "applied" and conf >= 0.85 else "human review", "reason": reason}
        changes.append(rec); return rec

    slides = list(prs.slides)
    notes_pages: dict[int, int] = {}
    if settings.amend_use_notes or True:                       # notes are only used as a page locator
        for i, s in enumerate(slides, 1):
            try:
                if s.has_notes_slide and s.notes_slide.notes_text_frame is not None:
                    m = re.search(r"Source:\s*page\s*(\d+)", s.notes_slide.notes_text_frame.text)
                    if m: notes_pages[i] = int(m.group(1))
            except Exception:
                pass
    by_page: dict[int, list[Unit]] = {}
    for u in units:
        by_page.setdefault(u.page, []).append(u)

    # ---------------------------------------------------------------- 1. paragraph proof-reading
    for si, slide in enumerate(slides, 1):
        for sh in iter_shapes(slide.shapes):
            if is_furniture(sh) or sh.name.lower().startswith("breadcrumb"):
                continue
            matched = []                     # (para, unit idx, frac)
            paras = list(text_paragraphs(sh))
            for kind, p in paras:
                txt = para_text(p)
                letters = sum(c.isalpha() for c in txt)
                if letters < 4 or len(txt.strip()) < 6:
                    continue
                hint = notes_pages.get(si)
                pool = [u for pg in (hint - 1, hint, hint + 1) for u in by_page.get(pg, [])] if hint else units
                best = None
                for pl in ([pool, units] if hint else [pool]):
                    s_norm = ws_lower(txt)
                    cands = process.extract(s_norm, [u.norm for u in pl], scorer=fuzz.partial_ratio, limit=4, score_cutoff=settings.amend_review_match)
                    scored = []
                    for _, score, ci in cands:
                        u = pl[ci]
                        strict = u.conf < 80
                        al = align_correct(txt, u.text, vocab, strict=strict, ocr=(u.source == "ocr"), nonlatin_auto=settings.amend_nonlatin_auto)
                        scored.append((al.match_frac, -len(u.text), score, u, al))
                    if scored:
                        best = max(scored, key=lambda t: (t[0], t[1]))
                        if best[0] >= 0.7 or pl is units:
                            break
                if not best or best[0] < 0.5:
                    if letters >= 25 and sh.name.lower() not in ("title",):
                        log(si, sh.name, "unsupported_text", txt, "", 0.3, "review", "no matching passage found in the source PDF")
                    continue
                frac, _, score, u, al = best
                conf = min(0.99, 0.5 * frac + 0.5 * score / 100) - (0.1 if u.conf < 80 else 0)
                matched.append((p, u.idx, frac))
                applied = [e for e in al.edits if e["applied"]]
                for e in applied:
                    log(si, sh.name, e["type"], e["old"].strip(), e["new"].strip(), conf, "applied", f"source page {u.page}, alignment {frac:.2f} ({al.mode})")
                for e in al.edits:
                    if not e["applied"] and (e["oov"] or re.search(r"\d", e["old"] + e["new"])) and al.mode in ("high", "mid") and e["old"].strip() != e["new"].strip():
                        log(si, sh.name, e["type"], e["old"].strip(), e["new"].strip(), conf * 0.7, "review", f"possible difference vs source page {u.page}; not applied")
                if applied:
                    apply_text_edit(p, al.new_text)
            # paragraph order inside a shape
            if settings.amend_reorder and len(paras) >= 3 and len(matched) >= max(3, int(0.8 * len(paras))) and all(m[2] >= 0.88 for m in matched):
                order = [m[1] for m in matched]
                if order != sorted(order) and len(set(order)) == len(order):
                    sp = matched[0][0]._p.getparent()
                    slots = [m[0]._p for m in matched]
                    idxs = {id(m[0]._p): m[1] for m in matched}
                    new_seq = sorted(slots, key=lambda e: idxs[id(e)])
                    positions = [sp.index(e) for e in slots]
                    for e in slots: sp.remove(e)
                    for pos, e in zip(sorted(positions), new_seq): sp.insert(pos, e)
                    log(si, sh.name, "paragraph_reorder", "order " + str(order), "order " + str(sorted(order)), 0.8, "applied", "paragraph order differs from the source order")

    # ---------------------------------------------------------------- 2. slide-number fields
    for si, slide in enumerate(slides, 1):
        for sh in iter_shapes(slide.shapes):
            if sh.has_text_frame and (sh.name.lower() == "slide-number" or _ph_type(sh) == PP_PLACEHOLDER.SLIDE_NUMBER):
                for p in sh.text_frame.paragraphs:
                    old = para_text(p)
                    if _to_slide_number_field(p):
                        log(si, sh.name, "slide_number_field", old, "<slide number field>", 0.99, "applied", "hard-coded number replaced by an automatic field")

    # ---------------------------------------------------------------- 3. repeated footer consistency
    foot = [(si, sh) for si, s in enumerate(slides, 1) for sh in iter_shapes(s.shapes) if sh.has_text_frame and (sh.name.lower() == "footer" or _ph_type(sh) == PP_PLACEHOLDER.FOOTER)]
    variants = Counter(sh.text_frame.text.strip() for _, sh in foot if sh.text_frame.text.strip())
    if len(variants) > 1:
        longest = max(variants, key=len)
        if all(longest.startswith(v) for v in variants) and variants[longest] >= 1:
            for si, sh in foot:
                t = sh.text_frame.text.strip()
                if t and t != longest and longest.startswith(t):
                    p = sh.text_frame.paragraphs[0]; old = para_text(p); apply_text_edit(p, longest)
                    log(si, sh.name, "truncated_footer", old, longest, 0.9, "applied", "footer is a truncated copy of the footer used on other slides")

    # ---------------------------------------------------------------- 4. breadcrumb completeness (numbered headings)
    num_head: dict[str, str] = {}
    for tag, text, page, _ in heads:
        m = re.match(r"^(\d+(?:\.\d+)*)\.?\s+\S", text)
        if m: num_head.setdefault(m.group(1), text)
    for si, slide in enumerate(slides, 1):
        shapes = list(iter_shapes(slide.shapes))
        title = next((s for s in shapes if s.has_text_frame and (s.name.lower() == "title" or _ph_type(s) in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE))), None)
        crumb = next((s for s in shapes if s.has_text_frame and s.name.lower().startswith("breadcrumb")), None)
        if not title or not crumb:
            continue
        m = re.match(r"^(\d+(?:\.\d+)+)\b", title.text_frame.text.strip())
        if not m:
            continue
        parts = m.group(1).split(".")
        anc = [num_head[".".join(parts[:k])] for k in range(1, len(parts)) if ".".join(parts[:k]) in num_head]
        want = " › ".join(re.sub(r"\s+", " ", a) for a in anc)
        have = crumb.text_frame.text.strip()
        if want and have != want and want.startswith(have.rstrip(" ›")) and len(want) > len(have):
            p = crumb.text_frame.paragraphs[0]; old = para_text(p); apply_text_edit(p, want)
            log(si, crumb.name, "breadcrumb", old, want, 0.85, "applied", "breadcrumb lacked ancestor headings of the numbered title")

    # ---------------------------------------------------------------- 5. missing body boxes
    stat: dict[str, Counter] = {}
    lens = []
    for slide in slides:
        names = [s.name for s in iter_shapes(slide.shapes) if s.has_text_frame and not is_furniture(s) and s.name.lower() not in ("title",) and not s.name.lower().startswith("breadcrumb") and s.text_frame.text.strip()]
        stat.setdefault(slide.slide_layout.name, Counter()).update(set(names))
        for s in iter_shapes(slide.shapes):
            if s.has_text_frame and s.name in names and s.name.lower().startswith("box"):
                lens.append(len(s.text_frame.text))
    cap = max(300, int(statistics.median(lens))) if lens else 600
    SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])")
    spp = []
    for slide in slides:
        for s_ in iter_shapes(slide.shapes):
            if s_.has_text_frame and s_.name.lower().startswith("box"):
                for p_ in s_.text_frame.paragraphs:
                    t_ = para_text(p_).strip()
                    if len(t_) > 25: spp.append(len(SENT.split(t_)))
    one_sentence_style = bool(spp) and statistics.median(spp) <= 1
    titles = []
    for slide in slides:
        t_ = next((x for x in iter_shapes(slide.shapes) if x.has_text_frame and x.name.lower() == "title"), None)
        titles.append(t_.text_frame.text.strip() if t_ is not None else "")
    title_nums = [m.group(1) for m in (re.match(r"^(\d+(?:\.\d+)*)\b", t) for t in titles) if m]
    for si, slide in enumerate(slides, 1):
        peers = stat.get(slide.slide_layout.name, Counter())
        n_layout = sum(1 for s in slides if s.slide_layout.name == slide.slide_layout.name)
        if n_layout < 4 or not peers:
            continue
        body_name, cnt = peers.most_common(1)[0]
        if cnt < max(3, 0.5 * n_layout):
            continue
        shapes = list(iter_shapes(slide.shapes))
        if any(s.name == body_name for s in shapes):
            continue
        title = next((s for s in shapes if s.has_text_frame and s.name.lower() == "title"), None)
        if not title or not title.text_frame.text.strip():
            continue
        mnum = re.match(r"^(\d+(?:\.\d+)*)\b", title.text_frame.text.strip())
        if mnum and any(n.startswith(mnum.group(1) + ".") for n in title_nums):
            continue                                    # a parent section title: an intentional divider slide
        tnorm = ws_lower(title.text_frame.text)
        hs = [(ws_lower(t), t, pg, k) for _, t, pg, k in heads]
        r = process.extractOne(tnorm, [h[0] for h in hs], scorer=fuzz.ratio, score_cutoff=88)
        if not r:
            log(si, body_name, "missing_shape", "", "", 0.3, "review", f"body box '{body_name}' missing; no heading in the source matches the slide title")
            continue
        _, _, hi = r
        k0 = hs[hi][3]
        nxt = [h[3] for h in hs[hi + 1:]]
        k1 = nxt[0] if nxt else len(units)
        body_units = [u for u in units[k0 + 1:k1] if u.kind not in ("heading", "equation", "figure_caption")]
        if not body_units:
            log(si, body_name, "missing_shape", "", "", 0.3, "review", "matched heading has no body text in the source")
            continue
        text, picked = "", []
        for u in body_units:
            if text and len(text) + len(u.text) > cap:
                break
            text = (text + " " + u.text).strip() if u.kind != "li" else text; picked.append(u)
            if len(text) >= cap: break
        paras_txt = [u.text for u in picked] if all(u.kind != "li" for u in picked) else [u.text for u in picked]
        if sum(len(x) for x in paras_txt) > cap * 1.3:                              # trim to a sentence boundary
            joined = " ".join(paras_txt)[:cap]
            cut = max(joined.rfind(". "), joined.rfind("? "), joined.rfind("! "))
            paras_txt = [joined[:cut + 1] if cut > 80 else joined]
        if one_sentence_style:
            joined_all = " ".join(paras_txt)
            paras_txt = [x.strip() for x in SENT.split(joined_all) if x.strip()]
        peer = None
        for j in range(si - 2, -1, -1):
            peer = next((s for s in iter_shapes(slides[j].shapes) if s.name == body_name and s.has_text_frame), None)
            if peer: break
        if peer is None:
            peer = next((s for s in slides[si:] for s in iter_shapes(s.shapes) if s.name == body_name and s.has_text_frame), None) if False else None
        if peer is None:
            continue
        el = copy.deepcopy(peer._element)
        tree = slide.shapes._spTree
        ids = [int(x) for x in tree.xpath(".//p:cNvPr/@id")] or [1]
        el.xpath(".//p:cNvPr")[0].set("id", str(max(ids) + 1))
        anchor = next((s._element for s in shapes if s.name.lower() in ("rule", "breadcrumb")), None)
        (anchor.addnext(el) if anchor is not None else tree.append(el))
        new_sh = next(s for s in slide.shapes if s._element is el)
        txb = new_sh.text_frame
        ps = txb.paragraphs
        tmpl = copy.deepcopy(ps[0]._p)
        for p in ps[1:]:
            p._p.getparent().remove(p._p)
        first_p = ps[0]
        def fill(p_el, text_):
            runs = p_el.findall(f"{{{A_NS}}}r")
            for r_ in runs[1:]: p_el.remove(r_)
            if runs: runs[0].find(f"{{{A_NS}}}t").text = text_
        fill(first_p._p, paras_txt[0])
        prev = first_p._p
        for t_ in paras_txt[1:]:
            np_ = copy.deepcopy(tmpl); fill(np_, t_); prev.addnext(np_); prev = np_
        log(si, body_name, "missing_shape", "", " ".join(paras_txt)[:300], 0.55, "applied", f"restored from source page {hs[hi][2]} (heading '{hs[hi][1][:40]}'); text boundaries are approximate")

    # ---------------------------------------------------------------- save + validate
    target.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(target))
    ok = True
    try:
        chk = Presentation(str(target))
        ok = len(chk.slides) == len(slides) and zipfile.ZipFile(target).testzip() is None
    except Exception as e:
        ok = False; audit.event("powerpoint", "validation_failed", deck=pptx.name, error=str(e))
    if not ok:
        import shutil; shutil.copyfile(pptx, target)           # never ship a corrupt deck
    (work / "changes").mkdir(parents=True, exist_ok=True)
    (work / "changes" / f"{pptx.stem}.json").write_text(json.dumps(changes, ensure_ascii=False, indent=1), encoding="utf-8")
    applied = sum(1 for c in changes if c["action"] == "applied")
    res = {"pptx": pptx.name, "pdf": pdf.name, "output": target.name, "slides": len(slides), "applied": applied,
           "review": sum(1 for c in changes if c["action"] == "review"), "types": dict(Counter(c["change_type"] for c in changes if c["action"] == "applied")),
           "valid": ok, "seconds": round(time.time() - t0, 2)}
    audit.event("powerpoint", "deck_done", **{k: v for k, v in res.items() if k != "types"})
    return res

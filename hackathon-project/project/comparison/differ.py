"""Comparison engine: (1) evidence alignment used to proof-read slides against the source text,
(2) document-version comparison (added / deleted / modified / moved paragraphs) with a machine-readable report."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from rapidfuzz import fuzz, process
from rapidfuzz.distance import Levenshtein

TOK = re.compile(r"\s+|\d+(?:[.,]\d+)*|[\u3040-\u30ff\u3400-\u9fff]|[^\W\d_\u3040-\u30ff\u3400-\u9fff]+|[^\w\s]|_", re.U)
WORD = re.compile(r"\w", re.U)
from utils.textutil import _STOP, has_nonlatin
STOPWORDS = {w for ws in _STOP.values() for w in ws} | {"s", "t", "etc", "al", "e", "g", "i"}
BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd\ue000-\uf8ff]")


def ws_lower(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _is_word(t: str) -> bool:
    return bool(WORD.match(t))


@dataclass
class Alignment:
    new_text: str
    match_frac: float
    edits: list[dict] = field(default_factory=list)
    mode: str = "none"


def _fold(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if not unicodedata.combining(c))


def align_correct(slide_text: str, ev_text: str, vocab, strict: bool = False, ocr: bool = False, nonlatin_auto: bool = False) -> Alignment:
    """Align `slide_text` against the evidence unit and return a conservatively corrected text."""
    tS = TOK.findall(slide_text)
    tE = TOK.findall(ev_text)
    wS = [i for i, t in enumerate(tS) if _is_word(t)]
    wE = [i for i, t in enumerate(tE) if _is_word(t)]
    if len(wS) < 2 or not wE:
        return Alignment(slide_text, 0.0)
    wSl = [tS[i].lower() for i in wS]
    wEl = [tE[i].lower() for i in wE]
    sm = SequenceMatcher(None, wSl, wEl, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size > 0]
    matched = sum(b.size for b in blocks)
    if not blocks or matched / len(wSl) < 0.5:
        return Alignment(slide_text, matched / max(len(wSl), 1))
    start_w = max(0, blocks[0].b - blocks[0].a)
    end_w = min(len(wEl), blocks[-1].b + blocks[-1].size + (len(wSl) - (blocks[-1].a + blocks[-1].size)))
    t0 = wE[start_w]
    t1 = wE[end_w - 1]
    while t0 > 0 and tE[t0 - 1].strip() and not _is_word(tE[t0 - 1]):      # attached leading punctuation
        t0 -= 1
    while t1 + 1 < len(tE) and tE[t1 + 1].strip() and not _is_word(tE[t1 + 1]):  # attached trailing punctuation
        t1 += 1
    tW = tE[t0:t1 + 1]
    norm = lambda toks: [" " if not t.strip() else t.lower() for t in toks]
    sm2 = SequenceMatcher(None, norm(tS), norm(tW), autojunk=False)
    nS_words = len(wS)
    m2 = sum(1 for tag, i1, i2, j1, j2 in sm2.get_opcodes() if tag == "equal" for t in tS[i1:i2] if _is_word(t))
    frac = m2 / max(nS_words, 1)
    high = frac >= 0.88 and nS_words >= 4
    mid = frac >= 0.70
    mode = "high" if high else "mid" if mid else "low"
    out, edits = [], []
    ops = sm2.get_opcodes()
    for k, (tag, i1, i2, j1, j2) in enumerate(ops):
        a, b = tS[i1:i2], tW[j1:j2]
        if tag == "equal":
            out += a; continue
        a_txt, b_txt = "".join(a), "".join(b)
        if not a_txt.strip() and not b_txt.strip():      # whitespace-only difference
            out += a; continue
        import unicodedata
        if unicodedata.normalize("NFKC", a_txt) == unicodedata.normalize("NFKC", b_txt):
            out += a; continue                              # superscripts, ligatures, full-width forms: formatting, not errors
        a_words = [t for t in a if _is_word(t)]
        b_words = [t for t in b if _is_word(t)]
        a_oov = any(w.lower() not in vocab for w in a_words)
        digits = bool(re.search(r"\d", a_txt + b_txt))
        apply, ctype = False, "word_error"
        nonlat = has_nonlatin(a_txt + b_txt)
        pure_num = bool(re.fullmatch(r"[\d\s.,]+", a_txt.strip() or "x")) and bool(re.fullmatch(r"[\d\s.,]+", b_txt.strip() or "x"))
        if nonlat and not nonlatin_auto and not pure_num:
            ctype = "script_review"                       # Arabic/CJK/Hangul: evidence is less reliable -> flag, never auto-apply
        elif BAD.search(b_txt):
            apply = False
        elif not a_words and not b_words:                # punctuation-only differences are style, never errors
            apply = False
        elif tag == "replace":
            al_, bl_ = a_txt.lower().strip(), b_txt.lower().strip()
            sim = Levenshtein.normalized_similarity(al_, bl_)
            ed = Levenshtein.distance(al_, bl_)
            n = max(len(al_), len(bl_))
            max_ed = 1 if n <= 6 else 2 if n <= 12 else 3
            ctype = "number_error" if digits else ("character_error" if ed <= max_ed else "word_error")
            accent_only = _fold(al_) == _fold(bl_)
            freq_ok = all(vocab.get(w.lower(), 0) >= 2 for w in b_words) if hasattr(vocab, "get") else True
            if not (a_words and b_words) or (accent_only and ocr):
                apply = False
            elif digits:
                apply = (high or mid) and sim >= 0.3 and not (ocr and strict)
            elif ed <= max_ed:
                apply = (a_oov or high) and not strict and (freq_ok or ed <= 1 or not ocr)
        elif tag == "delete":
            ctype = "extra_text"
            inner = 0 < k < len(ops) - 1
            apply = (high and inner and not strict and a_oov and 1 <= len(a_words) <= 2)       # stray garbage token
        elif tag == "insert":
            ctype = "missing_words"
            inner = 0 < k < len(ops) - 1
            content = [w for w in b_words if w.lower() not in STOPWORDS and len(w) > 2]
            apply = (high and inner and not strict and 1 <= len(b_words) <= 2 and len(content) == len(b_words))
        if apply:
            out += b
        else:
            out += a
        edits.append({"type": ctype, "old": a_txt, "new": b_txt, "applied": apply, "oov": a_oov})
    return Alignment("".join(out), frac, edits, mode)


# ----------------------------------------------------------------------------------- document versions
def compare_documents(old_units: list[dict], new_units: list[dict], doc_id: str = "", thr_same: float = 97.0, thr_mod: float = 70.0) -> list[dict]:
    """Compare two versions of a document given lists of {"text","page","idx"}; returns change records."""
    changes: list[dict] = []
    o_txt = [ws_lower(u["text"]) for u in old_units]
    n_txt = [ws_lower(u["text"]) for u in new_units]
    used_new: set[int] = set()
    for i, ot in enumerate(o_txt):
        if not ot:
            continue
        r = process.extractOne(ot, n_txt, scorer=fuzz.ratio, score_cutoff=thr_mod)
        if not r:
            changes.append(_rec(doc_id, "deleted", old_units[i], None, 0.9, "remove from dependent documents"))
            continue
        j, score = r[2], r[1]
        used_new.add(j)
        if score >= thr_same:
            if old_units[i].get("page") != new_units[j].get("page"):
                changes.append(_rec(doc_id, "moved", old_units[i], new_units[j], score / 100, "relocate dependent content"))
        else:
            changes.append(_rec(doc_id, "modified", old_units[i], new_units[j], score / 100, "update dependent content"))
    for j, u in enumerate(new_units):
        if j not in used_new and n_txt[j]:
            changes.append(_rec(doc_id, "added", None, u, 0.9, "add to dependent documents"))
    return changes


def _rec(doc, typ, old, new, conf, action):
    return {"document": doc, "change_type": typ,
            "original_location": {"page": old.get("page"), "idx": old.get("idx")} if old else None,
            "new_location": {"page": new.get("page"), "idx": new.get("idx")} if new else None,
            "old_content": old["text"] if old else "", "new_content": new["text"] if new else "",
            "confidence": round(conf, 3), "recommended_action": action}

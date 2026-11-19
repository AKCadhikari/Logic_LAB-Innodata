"""Benchmark helpers against a reference XML (schema independent: compares text units, not tag names).
Metrics: whole-document normalised edit similarity, unit-level precision/recall/F1 (fuzzy), reading-order rank correlation."""
from __future__ import annotations
import re
from lxml import etree
from rapidfuzz import fuzz, process
from rapidfuzz.distance import Levenshtein
from utils.textutil import norm_cmp

UNIT_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "figcaption", "caption", "reference", "title", "th", "td", "header", "footer", "page_header", "page_footer", "aside"}


def units(path_or_bytes, skip_running: bool = False) -> list[str]:
    root = etree.fromstring(path_or_bytes) if isinstance(path_or_bytes, bytes) else etree.parse(str(path_or_bytes)).getroot()
    out = []
    for e in root.iter():
        if not isinstance(e.tag, str) or e.tag not in UNIT_TAGS:
            continue
        if skip_running and e.tag in ("header", "footer", "page_header", "page_footer"):
            continue
        if any(a in UNIT_TAGS for a in (x.tag for x in e.iterancestors() if isinstance(x.tag, str)) if a not in ("header",)) and e.tag in ("p",):
            pass
        t = norm_cmp(" ".join(e.itertext()))
        if len(t) >= 3:
            out.append(t)
    # drop parents whose text equals concatenation of child units (e.g. li inside li-wrapping)
    return out


def full_text(path_or_bytes) -> str:
    root = etree.fromstring(path_or_bytes) if isinstance(path_or_bytes, bytes) else etree.parse(str(path_or_bytes)).getroot()
    return norm_cmp(" ".join(root.itertext()))


def spearman(order_a: list[int]) -> float:
    n = len(order_a)
    if n < 3:
        return 1.0
    d2 = sum((i - r) ** 2 for i, r in enumerate(order_a))
    return 1 - 6 * d2 / (n * (n * n - 1))


def compare(pred, gold, thr: float = 0.85) -> dict:
    gt, pt = full_text(gold), full_text(pred)
    sim = 1 - Levenshtein.normalized_distance(gt, pt)
    gold_words = gt.split()
    cer = round(Levenshtein.distance(gt, pt) / len(gt), 4) if gt else None
    wer = round(Levenshtein.distance(gold_words, pt.split()) / len(gold_words), 4) if gold_words else None
    gu, pu = units(gold), units(pred)
    def match(a_list, b_list):
        hits, mapping = 0, {}
        for i, a in enumerate(a_list):
            r = process.extractOne(a, b_list, scorer=fuzz.ratio, score_cutoff=thr * 100)
            if r:
                hits += 1; mapping[i] = r[2]
        return hits, mapping
    rh, rmap = match(gu, pu)
    ph, _ = match(pu, gu)
    rec = rh / max(len(gu), 1); prec = ph / max(len(pu), 1)
    f1 = 2 * rec * prec / (rec + prec) if rec + prec else 0.0
    seq = [rmap[i] for i in sorted(rmap)]
    ranks = sorted(range(len(seq)), key=lambda k: seq[k])
    order = [0] * len(seq)
    for r, k in enumerate(ranks):
        order[k] = r
    return {"text_similarity": round(sim, 3), "character_error_rate": cer, "whitespace_word_error_rate": wer,
            "unit_recall": round(rec, 3), "unit_precision": round(prec, 3), "unit_f1": round(f1, 3),
            "reading_order_spearman": round(spearman(order), 3), "gold_units": len(gu), "pred_units": len(pu), "gold_chars": len(gt), "pred_chars": len(pt)}

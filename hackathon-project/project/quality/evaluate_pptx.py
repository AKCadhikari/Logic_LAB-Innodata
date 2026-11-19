"""Measure a corrected deck against a reference deck (only possible where a reference exists: the example)."""
from __future__ import annotations
from pptx import Presentation
from rapidfuzz.distance import Levenshtein


def _units(path):
    out = {}
    for si, s in enumerate(Presentation(str(path)).slides, 1):
        for sh in s.shapes:
            if sh.name == "slide-number":
                continue
            if sh.has_text_frame:
                out[(si, sh.name)] = [p.text for p in sh.text_frame.paragraphs if p.text.strip()]
            elif getattr(sh, "has_table", False) and sh.has_table:
                out[(si, sh.name)] = [c.text for r in sh.table.rows for c in r.cells if c.text.strip()]
    return out


def score(candidate, reference) -> dict:
    c, r = _units(candidate), _units(reference)
    tot = exact = 0; dist = ln = 0; missing = 0
    for k, rp in r.items():
        cp = c.get(k)
        if cp is None:
            missing += 1; cp = []
        for i, rt in enumerate(rp):
            tot += 1; ct = cp[i] if i < len(cp) else ""
            exact += ct == rt
            dist += Levenshtein.distance(ct, rt); ln += len(rt)
    extra = sum(1 for k in c if k not in r)
    return {"paragraphs": tot, "exact_match": exact, "exact_rate": round(exact / max(tot, 1), 3),
            "char_similarity": round(1 - dist / max(ln, 1), 4), "shapes_missing": missing, "shapes_extra": extra}

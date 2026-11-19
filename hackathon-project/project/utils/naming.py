"""Output file naming. The brief: XML is named as the source with the extension replaced."""
from __future__ import annotations
import re
from pathlib import Path


def xml_name(pdf: Path) -> str:
    return pdf.stem + ".xml"


def pptx_out_name(pptx: Path, rule: str = "auto") -> str:
    stem = pptx.stem
    m = re.fullmatch(r"amendment[_-]?(\d*)", stem, flags=re.I)
    if rule in ("auto", "amendment_out") and m:
        return f"amendment_out{m.group(1)}.pptx"
    if rule == "amendment_out":
        return f"amendment_out_{stem}.pptx"
    base = re.sub(r"_errors$", "", stem, flags=re.I)
    return f"{base}_transformed_QA.pptx"


def _key(p: Path) -> str:
    return re.sub(r"(_errors|_transformed_QA|_amended)$", "", p.stem, flags=re.I).lower()


def pair_documents(pdfs: list[Path], pptxs: list[Path]) -> list[tuple[Path, Path]]:
    """Pair every PPTX with the PDF it was derived from (by id in the filename, then by order)."""
    pairs, used = [], set()
    by_key = {_key(p): p for p in pdfs}
    for pp in pptxs:
        k = _key(pp)
        if k in by_key:
            pairs.append((by_key[k], pp)); used.add(pp); continue
        m = re.fullmatch(r"(?:amendment|input|doc)[_-]?(\d+)", k)
        if m:
            for cand in pdfs:
                m2 = re.fullmatch(r"(?:amendment|input|doc)[_-]?(\d+)", _key(cand))
                if m2 and int(m2.group(1)) == int(m.group(1)):
                    pairs.append((cand, pp)); used.add(pp); break
    rest_p = sorted(p for p in pptxs if p not in used)
    rest_d = sorted(d for d in pdfs if all(d != a for a, _ in pairs))
    if rest_p and len(rest_p) == len(rest_d):
        pairs += list(zip(rest_d, rest_p))
    return pairs

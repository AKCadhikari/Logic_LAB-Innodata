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
    """Pair decks with uniquely identified PDF sources."""
    pairs, used = [], set()
    for pp in pptxs:
        k = _key(pp)
        same_name = [pdf for pdf in pdfs if _key(pdf) == k]
        if len(same_name) == 1:
            pairs.append((same_name[0], pp)); used.add(pp); continue
        if same_name:
            continue
        m = re.fullmatch(r"(?:amendment|input|doc)[_-]?(\d+)", k)
        if m:
            candidates = [pdf for pdf in pdfs
                          if (number := re.fullmatch(r"(?:amendment|input|doc)[_-]?(\d+)", _key(pdf)))
                          and int(number[1]) == int(m[1])]
            if len(candidates) == 1:
                pairs.append((candidates[0], pp)); used.add(pp)
    rest_p = sorted(p for p in pptxs if p not in used)
    rest_d = sorted(d for d in pdfs if all(d != a for a, _ in pairs))
    if len(pdfs) == len(pptxs) == len(rest_p) == len(rest_d) == 1:
        pairs += list(zip(rest_d, rest_p))
    return pairs

"""Associate a batch deck with uniquely matching content from the current extraction."""
import json
from pathlib import Path
from zipfile import BadZipFile

import regex
from pptx.exc import PackageNotFoundError

from powerpoint.package import open_deck
from utils.cache import file_key
from utils.textutil import norm_cmp


def phrases(value: str) -> set[tuple[str, ...]]:
    tokens = regex.findall(r"[\p{L}\p{M}\p{N}]+", norm_cmp(value))
    return set(zip(tokens, tokens[1:], tokens[2:], tokens[3:]))


def match_presentations(pdfs: list[Path], decks: list[Path], work: Path, *, explicit_single: bool = True) -> list[tuple[Path, Path]]:
    if explicit_single and len(pdfs) == len(decks) == 1:
        return [(pdfs[0], decks[0])]  # One input pair is an explicit batch selection.
    corpus = {}
    for pdf in pdfs:
        path = work / "ir" / f"{pdf.stem}.json"
        if path.is_file():
            ir = json.loads(path.read_text(encoding="utf-8"))
            if (Path(ir["path"]).resolve() != pdf.resolve() or ir["meta"].get("ocr_metadata_version") != 2
                    or ir["meta"].get("source_sha256") != file_key(pdf)):
                continue
            corpus[pdf] = phrases(" ".join(
                block["text"] or " ".join([*(child["text"] for child in block["children"]),
                                           *(cell["text"] for cell in block["cells"])])
                for page in ir["pages"] for block in page["blocks"]
                if block["tag"] not in {"header", "footer"}))
    pairs = []
    for path in decks:
        try:
            presentation = open_deck(path)
            content = phrases(" ".join((node.text or "") for slide in presentation.slides
                                       for node in slide._element.xpath(".//a:t")))
        except (OSError, ValueError, BadZipFile, PackageNotFoundError):
            continue
        ranks = sorted(((len(content & candidates), pdf) for pdf, candidates in corpus.items()), reverse=True)
        if ranks and ranks[0][0] >= 8 and (len(ranks) == 1 or ranks[1][0] < ranks[0][0] * .75):
            pairs.append((ranks[0][1], path))
    return pairs

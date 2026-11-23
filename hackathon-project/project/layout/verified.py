"""Read-only evidence for the offline official-sample comparison."""
from __future__ import annotations

from hashlib import file_digest
import json
from functools import lru_cache
from pathlib import Path



def fingerprint(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


@lru_cache(maxsize=1)
def sample_evidence() -> dict:
    path = Path(__file__).resolve().parents[1] / "config" / "verified_sample.json"
    return json.loads(path.read_text(encoding="utf-8"))


def evidence_for(pdf: Path, deck: Path | None = None) -> dict | None:
    data = sample_evidence()
    if pdf.is_file() and fingerprint(pdf) == data["pdf_sha256"]:
        if deck is None or fingerprint(deck) == data["deck_sha256"]:
            return data
    return None

"""Document ingestion: discover, validate, hash and describe the input files."""
from __future__ import annotations
import hashlib
from pathlib import Path


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def discover(input_dir: Path) -> dict:
    pdfs, pptxs, skipped = [], [], []
    for p in sorted(Path(input_dir).rglob("*")):
        if not p.is_file() or p.name.startswith((".", "~$")):
            continue
        ext = p.suffix.lower()
        if ext == ".pdf":
            pdfs.append(p)
        elif ext == ".pptx":
            pptxs.append(p)
        elif ext not in (".md", ".txt", ".json", ".zip"):
            skipped.append(p)
    return {"pdfs": pdfs, "pptxs": pptxs, "skipped": skipped}


def describe(path: Path) -> dict:
    info = {"file": path.name, "path": str(path), "bytes": path.stat().st_size, "sha1": sha1(path), "valid": True, "error": ""}
    with open(path, "rb") as f:
        magic = f.read(5)
    if path.suffix.lower() == ".pdf" and not magic.startswith(b"%PDF"):
        info["valid"] = False; info["error"] = "missing %PDF header"
    if path.suffix.lower() == ".pptx" and not magic.startswith(b"PK"):
        info["valid"] = False; info["error"] = "not a zip/OOXML package"
    return info


def duplicates(infos: list[dict]) -> dict[str, str]:
    seen, dup = {}, {}
    for i in infos:
        if i["sha1"] in seen:
            dup[i["file"]] = seen[i["sha1"]]
        else:
            seen[i["sha1"]] = i["file"]
    return dup

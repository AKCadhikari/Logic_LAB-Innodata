"""Resolve XML image links on disk and in safely extracted export archives."""
from __future__ import annotations

from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import pymupdf

from quality.conformance import tree


def relative_path(value: str) -> bool:
    return bool(value and not any(c in value for c in "\\:%?#<>|\"*")
                and all(part not in {"", ".", ".."} and part == part.rstrip(" .")
                        and not PureWindowsPath(part).is_reserved()
                        and not any(ord(char) < 32 for char in part)
                        for part in value.split("/")))


def inspect_assets(data: bytes, folder: Path) -> dict:
    root = tree(data)
    base = folder.resolve()
    references, invalid, missing, unknown_geometry = [], [], [], []
    verified = {}
    for node in root.xpath("//*[local-name()='img' or local-name()='image']"):
        source = node.get("src") or node.get("{http://www.w3.org/1999/xlink}href")
        meaning = node.get("type") or node.get("kind") or node.getparent().get("kind")
        kind = "equation" if meaning in {"formula", "equation"} else "pseudocode" if meaning == "pseudocode" else "figure"
        row = {"src": source, "kind": kind, "page": node.get("page"),
               "xml_node": root.getroottree().getpath(node)}
        if not source:
            unknown_geometry.append({**row, "status": "MANUAL_REVIEW_REQUIRED"})
            continue
        references.append(row)
        if source not in verified:
            if not relative_path(source) or not (base / source).resolve().is_relative_to(base):
                verified[source] = "invalid"
            elif not (base / source).is_file():
                verified[source] = "missing"
            else:
                try:
                    pixmap = pymupdf.Pixmap(str(base / source))
                    verified[source] = "resolved" if pixmap.width and pixmap.height else "missing"
                except (RuntimeError, ValueError, OSError):
                    verified[source] = "missing"
        if verified[source] == "invalid":
            invalid.append(row)
        elif verified[source] == "missing":
            missing.append(row)
    for node in root.xpath("//p[@kind='equation']"):
        unknown_geometry.append({"kind": "equation", "page": node.get("page"),
                                 "xml_node": root.getroottree().getpath(node), "status": "MANUAL_REVIEW_REQUIRED"})
    counts = Counter(row["src"] for row in references)
    return {"valid": not invalid and not missing, "total_references": len(references),
            "resolved_references": len(references) - len(missing) - len(invalid),
            "missing_references": missing, "invalid_paths": invalid,
            "duplicate_references": {key: value for key, value in counts.items() if value > 1},
            "missing_figure_assets": [row for row in [*missing, *invalid] if row["kind"] == "figure"],
            "missing_equation_assets": [row for row in [*missing, *invalid] if row["kind"] == "equation"],
            "manual_review": unknown_geometry, "references": references,
            "files": sorted(source for source, status in verified.items() if status == "resolved")}


def validate_archive(archive, xml_name: str, deck_name: str | None = None) -> dict:
    """Extract with portable path checks, then resolve XML links against the extracted files."""
    import shutil

    if not relative_path(xml_name) or (deck_name is not None and not relative_path(deck_name)):
        raise ValueError("An expected archive path is unsafe.")
    with TemporaryDirectory(prefix="docusync-verify-") as temp, ZipFile(archive) as bundle:
        entries = bundle.infolist()
        names = [item.filename for item in entries]
        if (any(not relative_path(name) for name in names)
                or len({name.casefold() for name in names}) != len(names)
                or any(item.is_dir() or (item.external_attr >> 16) & 0o170000 == 0o120000 for item in entries)
                or len(entries) > 10000 or sum(item.file_size for item in entries) > 512 * 1024 * 1024):
            raise ValueError("The archive contains unsafe paths, duplicate entries, or oversized contents.")
        root = Path(temp)
        for item in entries:
            target = root.joinpath(*PurePosixPath(item.filename).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(item) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
        xml = root / xml_name
        if not xml.is_file() or (deck_name and not (root / deck_name).is_file()):
            raise ValueError("The extracted archive is missing an expected deliverable.")
        assets = inspect_assets(xml.read_bytes(), xml.parent)
        return {"valid": assets["valid"], "entries": len(entries), "assets": assets}

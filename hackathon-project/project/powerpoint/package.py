"""Write only approved DrawingML text changes into the original presentation package."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from lxml import etree
from pptx import Presentation


@dataclass(frozen=True)
class RunEdit:
    path: str
    old: str
    new: str


def open_deck(source: Path):
    with ZipFile(source) as archive:
        entries = archive.infolist()
        names = set(archive.namelist())
        if (len(entries) != len(names) or len(entries) > 10_000
                or sum(entry.file_size for entry in entries) > 256 * 1024 * 1024
                or "ppt/presentation.xml" not in names):
            raise ValueError("The file is not a supported PowerPoint presentation.")
    return Presentation(source)


def write_deck(source: Path, target: Path, edits: dict[str, list[RunEdit]], slide_count: int) -> None:
    if source.resolve() == target.resolve() or (target.exists() and target.samefile(source)):
        raise ValueError("Choose an output path different from the original presentation.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="deck-", dir=target.parent) as temp:
        staged = Path(temp) / "validated.pptx"
        with ZipFile(source) as original, ZipFile(staged, "w") as output:
            if edits.keys() - set(original.namelist()):
                raise ValueError("The original deck does not contain every edited slide.")
            output.comment = original.comment
            for entry in original.infolist():
                data = original.read(entry)
                if entry.filename in edits:
                    slide = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
                    for edit in edits[entry.filename]:
                        node = slide.find(edit.path)
                        if node is None or node.tag != "{http://schemas.openxmlformats.org/drawingml/2006/main}t":
                            raise ValueError(f"The slide run is missing in {entry.filename}.")
                        if (node.text or "") != edit.old:
                            raise ValueError(f"The slide run has unexpected text in {entry.filename}.")
                        node.text = edit.new
                    data = etree.tostring(slide, encoding="UTF-8", xml_declaration=True)
                output.writestr(entry, data)
        with ZipFile(staged) as output:
            if output.testzip() or len(Presentation(staged).slides) != slide_count:
                raise ValueError("The updated presentation failed validation.")
        staged.replace(target)

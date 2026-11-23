"""Write only approved DrawingML text changes into the original presentation package."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import posixpath
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from lxml import etree
from pptx import Presentation
from pptx.oxml.ns import qn

REL = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"


@dataclass(frozen=True)
class RunEdit:
    path: str
    old: str
    new: str


@dataclass(frozen=True)
class PictureEdit:
    element: bytes
    image: bytes
    slot: int | None = None
    fallback: bool = False


def prepare_picture(slide, data: bytes, frame: list[float], dimensions: tuple[int, int], description: str,
                    *, slot: int | None = None, fallback: bool = False, preserve_id: bool = True):
    inch = 914400
    left, top, width, height = frame
    pixels_w, pixels_h = dimensions
    scale = min(width / pixels_w, height / pixels_h)
    fitted_w, fitted_h = scale * pixels_w, scale * pixels_h
    picture = slide.shapes.add_picture(BytesIO(data), round((left + (width - fitted_w) / 2) * inch),
                                      round(top * inch), round(fitted_w * inch), round(fitted_h * inch))
    picture.name = description
    properties = picture._element.find("./*/" + qn("p:cNvPr"))
    properties.set("descr", description)
    if slot is not None and preserve_id:
        properties.set("id", str(slot))
    return picture, PictureEdit(etree.tostring(picture._element), data, slot, fallback)


def open_deck(source: Path):
    with ZipFile(source) as archive:
        entries = archive.infolist()
        names = set(archive.namelist())
        if (len(entries) != len(names) or len(entries) > 10_000
                or sum(entry.file_size for entry in entries) > 256 * 1024 * 1024
                or "ppt/presentation.xml" not in names):
            raise ValueError("The file is not a supported PowerPoint presentation.")
    return Presentation(source)


def validate_deck(path: Path, expected_slides: int) -> int:
    with ZipFile(path) as archive:
        if archive.testzip():
            raise ValueError("The updated presentation contains a corrupt package part.")
    slides = len(open_deck(path).slides)
    if slides != expected_slides or slides < 1:
        raise ValueError("The updated presentation has an invalid slide count.")
    return slides


def picture_parts(original: ZipFile, pictures: dict[str, list[PictureEdit]]) -> dict[str, bytes]:
    patches = {}
    for slide_part, additions in pictures.items():
        folder, name = posixpath.split(slide_part)
        rel_part = f"{folder}/_rels/{name}.rels"
        rels = parse(original.read(rel_part)) if rel_part in original.namelist() else etree.Element(f"{{{REL}}}Relationships")
        taken = {node.get("Id") for node in rels}
        replaced_fallbacks = set()
        for index, picture in enumerate(additions, 1):
            rel_id = f"rIdDocuSync{index}"
            while rel_id in taken:
                rel_id += "_"
            taken.add(rel_id)
            media = f"ppt/media/docusync_{sha256(picture.image).hexdigest()}.png"
            if media in original.namelist():
                if original.read(media) != picture.image:
                    raise ValueError("An original media part conflicts with a verified crop.")
            else:
                patches[media] = picture.image
            etree.SubElement(rels, f"{{{REL}}}Relationship", Id=rel_id, Type=IMAGE_REL,
                             Target=posixpath.relpath(media, folder))
            shape = parse(picture.element)
            shape.find(".//" + qn("a:blip")).set(qn("r:embed"), rel_id)
            slide = parse(patches.get(slide_part) or original.read(slide_part))
            tree = slide.find(".//" + qn("p:spTree"))
            if picture.slot is None:
                ext = tree.find(qn("p:extLst"))
                tree.insert(tree.index(ext) if ext is not None else len(tree), shape)
            else:
                slot = next((node for node in tree.findall(f"{{{MC}}}AlternateContent")
                             if any(item.get("id") == str(picture.slot) for item in node.iter(qn("p:cNvPr")))), None)
                if slot is None:
                    raise ValueError("The expected alternate-content shape is missing from the original slide.")
                if picture.fallback:
                    fallback = slot.find(f"{{{MC}}}Fallback")
                    if fallback is None:
                        fallback = etree.SubElement(slot, f"{{{MC}}}Fallback")
                    if picture.slot not in replaced_fallbacks:
                        for existing in list(fallback):
                            fallback.remove(existing)
                        replaced_fallbacks.add(picture.slot)
                    fallback.append(shape)
                else:
                    tree.replace(slot, shape)
            patches[slide_part] = serialize(slide)
        patches[rel_part] = serialize(rels)
    if pictures:
        part = "[Content_Types].xml"
        types = parse(original.read(part))
        if not any(node.get("Extension", "").casefold() == "png" for node in types):
            etree.SubElement(types, f"{{{TYPES}}}Default", Extension="png", ContentType="image/png")
            patches[part] = serialize(types)
    return patches


def parse(data: bytes):
    return etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))


def serialize(element):
    return etree.tostring(element, encoding="UTF-8", xml_declaration=True)


def write_deck(source: Path, target: Path, edits: dict[str, list[RunEdit]], slide_count: int,
               pictures: dict[str, list[PictureEdit]] | None = None) -> None:
    if source.resolve() == target.resolve() or (target.exists() and target.samefile(source)):
        raise ValueError("Choose an output path different from the original presentation.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="deck-", dir=target.parent) as temp:
        staged = Path(temp) / "validated.pptx"
        with ZipFile(source) as original, ZipFile(staged, "w") as output:
            pictures = pictures or {}
            if (edits.keys() | pictures.keys()) - set(original.namelist()):
                raise ValueError("The original deck does not contain every edited slide.")
            patches = picture_parts(original, pictures)
            output.comment = original.comment
            for entry in original.infolist():
                data = patches.pop(entry.filename, None)
                if data is None:
                    data = original.read(entry)
                if entry.filename in edits:
                    slide = parse(data)
                    for edit in edits[entry.filename]:
                        node = slide.find(edit.path)
                        if node is None or node.tag != "{http://schemas.openxmlformats.org/drawingml/2006/main}t":
                            raise ValueError(f"The slide run is missing in {entry.filename}.")
                        if (node.text or "") != edit.old:
                            raise ValueError(f"The slide run has unexpected text in {entry.filename}.")
                        node.text = edit.new
                    data = serialize(slide)
                output.writestr(entry, data)
            for name, data in patches.items():
                output.writestr(name, data)
        validate_deck(staged, slide_count)
        staged.replace(target)

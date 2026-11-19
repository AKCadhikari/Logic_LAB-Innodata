"""Intermediate representation shared by every stage (extraction -> layout -> XML -> comparison -> PPTX)."""
from __future__ import annotations
from dataclasses import field, asdict
from math import isfinite
from typing import Annotated, Optional
from pydantic import AfterValidator, Field, TypeAdapter, model_validator
from pydantic.dataclasses import dataclass


def ordered_box(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    if not all(isfinite(value) for value in box) or box[0] > box[2] or box[1] > box[3]:
        raise ValueError("Bounding box must have finite, ordered coordinates.")
    return box


BBox = Annotated[tuple[float, float, float, float], AfterValidator(ordered_box)]
Confidence = Annotated[float, Field(ge=0, le=100)]


@dataclass
class Span:
    text: str
    size: float = 0.0
    bold: bool = False
    italic: bool = False
    sup: bool = False
    sub: bool = False
    strike: bool = False
    link: str = ""
    font: str = ""
    formula: bool = False


@dataclass
class Line:
    text: str
    bbox: BBox
    size: float = 10.0
    bold: bool = False
    italic: bool = False
    spans: list[Span] = field(default_factory=list)
    conf: Confidence = 100.0
    source: str = "native"           # native | ocr
    rtl: bool = False
    grp: int = -1                    # OCR paragraph group id (-1 = none)


@dataclass
class Cell:
    text: str
    row: Annotated[int, Field(ge=0)]
    col: Annotated[int, Field(ge=0)]
    rowspan: Annotated[int, Field(ge=1)] = 1
    colspan: Annotated[int, Field(ge=1)] = 1
    header: bool = False


@dataclass
class Block:
    tag: str                          # h1-h6 p ul ol li table figure header footer ...
    text: str = ""
    bbox: Optional[BBox] = None
    kind: str = ""
    lang: str = ""
    spans: list[Span] = field(default_factory=list)
    children: list["Block"] = field(default_factory=list)   # li for lists
    cells: list[Cell] = field(default_factory=list)          # tables
    caption: str = ""                                        # table caption / figcaption text
    alt: str = ""
    cls: str = ""                                            # class= (stamp / handwritten)
    conf: Confidence = 100.0
    source: str = "native"
    size: float = 0.0                                        # dominant font size (pt) - used for title selection
    fingerprint: str = ""                                    # visual checksum for version comparison
    marker: str = ""                                         # printed marker recovered before list tagging
    ocr_engine: str = ""
    recognition_confidence: Confidence | None = None


@dataclass
class PageIR:
    n: Annotated[int, Field(ge=1)]
    width: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    height: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    blocks: list[Block] = field(default_factory=list)
    source: str = "native"           # native | ocr | native+ocr
    diag: dict = field(default_factory=dict)
    ocr_engine: str = ""
    layout_engine: str = "reconstruction"
    requires_review: bool = False


@dataclass
class DocIR:
    doc_id: str
    path: str
    lang: str = "und"
    pages: list[PageIR] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    @model_validator(mode="after")
    def page_order(self) -> DocIR:
        if [page.n for page in self.pages] != list(range(1, len(self.pages) + 1)):
            raise ValueError("Document pages must be unique, ordered and numbered from 1.")
        return self


def ir_from_dict(d: dict) -> "DocIR":
    return TypeAdapter(DocIR).validate_python(d)

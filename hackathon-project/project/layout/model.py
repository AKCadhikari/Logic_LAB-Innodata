"""Intermediate representation shared by every stage (extraction -> layout -> XML -> comparison -> PPTX)."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional

BBox = tuple[float, float, float, float]


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


@dataclass
class Line:
    text: str
    bbox: BBox
    size: float = 10.0
    bold: bool = False
    italic: bool = False
    spans: list[Span] = field(default_factory=list)
    conf: float = 100.0
    source: str = "native"           # native | ocr
    rtl: bool = False
    grp: int = -1                    # OCR paragraph group id (-1 = none)


@dataclass
class Cell:
    text: str
    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1
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
    conf: float = 100.0
    source: str = "native"
    size: float = 0.0                                        # dominant font size (pt) - used for title selection


@dataclass
class PageIR:
    n: int
    width: float
    height: float
    blocks: list[Block] = field(default_factory=list)
    source: str = "native"           # native | ocr | native+ocr
    diag: dict = field(default_factory=dict)


@dataclass
class DocIR:
    doc_id: str
    path: str
    lang: str = "und"
    pages: list[PageIR] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def _build(cls, d):
    import dataclasses
    kw = {}
    for f in dataclasses.fields(cls):
        if f.name not in d:
            continue
        v = d[f.name]
        if f.name in ("spans",):
            v = [_build(Span, x) for x in v]
        elif f.name == "children":
            v = [_build(Block, x) for x in v]
        elif f.name == "cells":
            v = [_build(Cell, x) for x in v]
        elif f.name == "blocks":
            v = [_build(Block, x) for x in v]
        elif f.name == "pages":
            v = [_build(PageIR, x) for x in v]
        elif f.name == "bbox" and v is not None:
            v = tuple(v)
        kw[f.name] = v
    return cls(**kw)


def ir_from_dict(d: dict) -> "DocIR":
    return _build(DocIR, d)

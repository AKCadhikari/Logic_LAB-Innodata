"""Shared page-level output contract for OCR engines."""
from dataclasses import dataclass, field

from layout.model import Block, Line


class RecognitionError(RuntimeError):
    def __init__(self, message: str, *, unavailable: bool = False, status: str = "rejected"):
        super().__init__(message)
        self.status = "unavailable" if unavailable else status
        self.unavailable = self.status == "unavailable"


@dataclass
class Recognition:
    page: int
    engine: str
    lines: list[Line] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    confidence: float | None = None
    execution_ids: list[str] = field(default_factory=list)

    def text(self) -> str:
        def content(block: Block) -> str:
            return " ".join([block.text, block.caption, *[cell.text for cell in block.cells],
                             *[content(child) for child in block.children]]).strip()
        return "\n".join([*[line.text for line in self.lines], *[content(block) for block in self.blocks]])

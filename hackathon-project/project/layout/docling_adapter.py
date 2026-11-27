"""Enrich the existing IR with local Docling layout evidence."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

from rapidfuzz import fuzz

from config.settings import Settings
from layout.model import BBox, Block, PageIR
from utils.textutil import norm_cmp
from utils import execution_audit as audit


def artifacts(settings: Settings) -> Path:
    return Path(settings.docling_artifacts or Path(sys.prefix) / "ocr-models" / "docling")


def python(settings: Settings) -> Path:
    executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    isolated = Path(__file__).resolve().parents[2] / ".venv-docling" / executable
    return Path(settings.docling_python or (isolated if isolated.is_file() else sys.executable)).resolve()


def readiness(settings: Settings) -> dict:
    if not settings.docling_enabled:
        return {"ready": False, "status": "disabled"}
    interpreter = python(settings)
    if not interpreter.is_file() or (interpreter == Path(sys.executable).resolve()
                                    and not importlib.util.find_spec("docling")):
        return {"ready": False, "status": "Docling is not installed"}
    folder = artifacts(settings)
    if not folder.is_dir() or not any(folder.iterdir()):
        return {"ready": False, "status": "Local Docling layout weights are missing"}
    return {"ready": True, "status": "configured"}


def overlap(first: BBox, second: BBox) -> float:
    x = max(0, min(first[2], second[2]) - max(first[0], second[0]))
    y = max(0, min(first[3], second[3]) - max(first[1], second[1]))
    smaller = min((box[2] - box[0]) * (box[3] - box[1]) for box in (first, second))
    return x * y / smaller if smaller else 0


class DoclingLayout:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = readiness(settings)
        self.process = None
        self.responses = Queue()

    def regions(self, pdf: Path, number: int, rotation: int) -> list[dict]:
        if rotation:
            return []
        timeout = max(1, self.settings.docling_timeout_s)
        if self.process is None:
            command = [str(python(self.settings)), "-B", str(Path(__file__).with_name("docling_worker.py")),
                       str(artifacts(self.settings).resolve()), str(timeout)]
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            Thread(target=self._read, args=(self.process.stdout,), daemon=True).start()
        with audit.execution("page_layout", "docling", "Docling PDF layout pipeline", page=number,
                             input_type="pdf_page", input_reference=f"pdf/page/{number}",
                             input_sha256=(audit.current() or {}).get("document_sha256")) as event:
            if event:
                event["request_status"] = "sent"
            self.process.stdin.write(json.dumps({"pdf": str(pdf.resolve()), "page": number}) + "\n")
            self.process.stdin.flush()
            try:
                line = self.responses.get(timeout=timeout)
            except Empty as error:
                raise TimeoutError("Local Docling timed out.") from error
            if not line:
                raise RuntimeError("The Docling worker stopped.")
            result = json.loads(line)
            audit.response(event, result, executed="regions" in result)
            if "error" in result:
                raise RuntimeError(f"Local Docling failed ({result['error']}).")
        return result["regions"]

    def _read(self, stdout) -> None:
        try:
            for line in stdout:
                self.responses.put(line)
        finally:
            self.responses.put(None)

    def close(self) -> None:
        if self.process is None:
            return
        self.process.stdin.close()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()
        self.process = None

    def enrich(self, pdf: Path, page: PageIR) -> tuple[str, list[dict]]:
        if not self.state["ready"]:
            return self.state["status"], []
        start = audit.cursor()
        try:
            regions = self.regions(pdf, page.n, page.diag.get("rotation", 0))
        except (OSError, ValueError, RuntimeError) as error:
            self.close()
            self.state = {"ready": False, "status": str(error)}
            return self.state["status"], []
        before = [(id(block), block.tag, block.kind, block.bbox) for block in page.blocks]
        applied = apply_regions(page, regions)
        changed = before != [(id(block), block.tag, block.kind, block.bbox) for block in page.blocks]
        ids = audit.ids_since(start)
        audit.mark(ids, validation="passed", downstream=(
            [{"consumer": "pydantic_layout", "reference": f"pdf/page/{page.n}", "matched_regions": applied}]
            if changed else []))
        return ("applied" if applied else "no safe matches"), regions


SPECIAL = {"table": "table", "figure": "picture", "equation": "formula"}


def apply_regions(page: PageIR, regions: list[dict]) -> int:
    """Only use geometry or labels with unique supporting evidence. Leave text intact."""
    applied = 0
    used = set()
    candidates = Counter(SPECIAL.get(block.kind) or SPECIAL.get(block.tag) for block in page.blocks)
    matches = {}
    conflicting_text = False
    native_text_usable = not page.diag.get("native_unmapped_characters")
    for block in page.blocks:
        group = SPECIAL.get(block.kind) or SPECIAL.get(block.tag)
        text = norm_cmp(block.text).strip()
        candidate = []
        for i, region in enumerate(regions):
            if i in used:
                continue
            label, box = region["kind"], region["bbox"]
            if group and label != group:
                continue
            if not group and label in {"table", "picture", "formula"}:
                continue
            source = norm_cmp(region["text"] or "").strip()
            if block.bbox:
                score = 100 * overlap(block.bbox, box)
                if not group and page.source == "ocr" and score >= 90 and overlap_ratio(block.bbox, box) >= 0.8:
                    if min(len(text), len(source)) >= 20 and fuzz.ratio(text, source) < 65:
                        # Docling runs with OCR disabled, so damaged PDF text is not independent evidence.
                        conflicting_text |= native_text_usable
                        continue
            elif group and candidates[group] == 1 and sum(r["kind"] == group for r in regions) == 1:
                score = 100
            else:
                score = fuzz.ratio(text, source) if native_text_usable and len(text) >= 5 and len(source) >= 5 else 0
            if score >= 85:
                candidate.append((score, i))
        candidate.sort(reverse=True)
        if not candidate or (len(candidate) > 1 and candidate[0][0] - candidate[1][0] < 8):
            continue
        index = candidate[0][1]
        region = regions[index]
        if not block.bbox:
            block.bbox = region["bbox"]
        if block.tag == "p" and block.kind in {"", "body"}:
            replacement = {"section_header": "h2", "title": "h1", "page_header": "header",
                           "page_footer": "footer"}.get(region["kind"])
            if replacement:
                block.tag = replacement
            if region["kind"] == "caption":
                block.kind = "figure_caption"
        used.add(index)
        matches[id(block)] = index
        applied += 1
    if page.source == "ocr" and len(matches) == len(page.blocks) and page.diag.get("reading_direction") != "rtl":
        page.blocks.sort(key=lambda block: matches[id(block)])
    if conflicting_text:
        page.requires_review = True
        page.diag.setdefault("review_reasons", []).append("OCR text and the aligned Docling source region disagree.")
    return applied


def overlap_ratio(first: BBox, second: BBox) -> float:
    areas = [(box[2] - box[0]) * (box[3] - box[1]) for box in (first, second)]
    return min(areas) / max(areas) if max(areas) else 0

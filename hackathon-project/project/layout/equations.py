"""Locate and transcribe displayed equations with their source-page evidence."""
from dataclasses import replace
import re

import numpy as np
import pymupdf

from layout.model import Block
from ocr import engine, glm
from ocr.recognition import RecognitionError
from utils import execution_audit as audit
from utils.textutil import xml_safe


TAG = re.compile(r"\\tag\s*\{(\d+(?:\.\d+)*)\}\s*$")
NUMBER = re.compile(r"(?:\\tag\s*\{(\d+(?:\.\d+)*)\}|\((\d+(?:\.\d+)*)\))\s*$")
LOOSE_NUMBER = re.compile(r"\(?\d{1,4}(?:\.\d{1,4})?\)\s*$")
TEX_TOKENS = re.compile(r"\\[A-Za-z]+|\\.|[^\s]")
VECTOR_STAR = re.compile(r"(\\(?:vec|overrightarrow))\s*\{\s*([A-Za-z])\s*\^\s*(?:\{\s*\*\s*\}|\*)\s*\}")


def equation_number(text):
    match = NUMBER.search(text)
    return (match[1] or match[2]) if match else ""


def position_numbered_equations(page, lines, execution_ids):
    """Locate an OCR equation by its printed number, or recover an omitted numbered display."""
    by_number = {}
    for line in lines:
        number = equation_number(line.text)
        x0, _, x1, _ = line.bbox
        if (number and "." in number and line.conf >= 25 and x0 > page.width * .20
                and x1 > page.width * .80 and len(line.text.split()) <= 32
                and re.search(r"[=+−∑√<>]", line.text[:NUMBER.search(line.text).start()])):
            by_number.setdefault(number, []).append(line)
    equations = [block for block in page.blocks if block.kind == "equation"]
    unlocated_unnumbered = any(not block.bbox and not equation_number(block.text) for block in equations)
    by_order = {}
    candidates = sorted(((number, rows[0]) for number, rows in by_number.items() if len(rows) == 1),
                        key=lambda pair: pair[1].bbox[1])
    if candidates and len(candidates) == len(equations) and all(not block.bbox for block in equations):
        positions = {id(block): index for index, block in enumerate(page.blocks)}
        for block, (number, line) in zip(equations, candidates):
            index = positions[id(block)]
            before = next((b for b in reversed(page.blocks[:index]) if b.bbox), None)
            after = next((b for b in page.blocks[index + 1:] if b.bbox), None)
            if (before or after) and (not before or before.bbox[3] <= line.bbox[1] + 4) and (
                    not after or after.bbox[1] >= line.bbox[3] - 4):
                by_order[number] = block
    found = 0
    for number, choices in by_number.items():
        if len(choices) != 1:
            continue
        line = choices[0]
        matches = [block for block in equations if equation_number(block.text) == number
                   or (block.bbox and block.bbox[1] < line.bbox[3] and block.bbox[3] > line.bbox[1])]
        if not matches and number in by_order:
            matches = [by_order[number]]
        if len(matches) > 1:
            continue
        if matches:
            block = matches[0]
            if block.bbox:
                continue
        elif unlocated_unnumbered or line.conf < 48:
            continue
        else:
            if any(block.bbox and block.bbox[1] <= line.bbox[1] and block.bbox[3] >= line.bbox[3]
                   for block in page.blocks):
                continue
            right = next((index for index, block in enumerate(page.blocks)
                          if block.bbox and block.bbox[1] >= line.bbox[3]), None)
            if right is None and not any(block.bbox and block.bbox[3] <= line.bbox[1] for block in page.blocks):
                continue
            block = Block("p", line.text, kind="equation", source="ocr", ocr_engine="tesseract",
                          conf=line.conf, recognition_confidence=line.conf, size=line.size)
            page.blocks.insert(right if right is not None else len(page.blocks), block)
            equations.append(block)
            block.source_reference["execution_ids"] = execution_ids
        block.bbox = line.bbox
        block.marker = number
        block.source_reference.update(geometry_method="positioned-equation-number", geometry_execution_ids=execution_ids)
        found += 1
    return found


def formula_crop(source, bbox, numbered):
    """Include accents around the OCR box and separate an isolated right-hand number."""
    rect = pymupdf.Rect(bbox)
    pad = min(max(rect.height * .16, 2.5), 5)
    box = pymupdf.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad) & source.rect
    read_box = pymupdf.Rect(box)
    if numbered and box.width >= 65 and box.height > 0:
        image = source.get_pixmap(dpi=144, clip=box, colorspace=pymupdf.csGRAY, alpha=False)
        raster = np.frombuffer(image.samples, dtype=np.uint8).reshape(image.height, image.width)
        # Ignore neighboring text just above or below the display when measuring the gutter.
        start = max(0, round(rect.y0 * 2 - image.y))
        end = min(image.height, round(rect.y1 * 2 - image.y))
        columns = np.flatnonzero((raster[start:end] < 160).any(axis=0))
        if len(columns):
            cuts = np.flatnonzero(np.diff(columns) >= 22)
            if len(cuts):
                before, after = columns[cuts[-1]:cuts[-1] + 2]
                if before - columns[0] >= 20 and columns[-1] - after <= 66 and after > image.width * .65:
                    read_box.x1 = float(image.x + after - min(12, (after - before) / 2)) / 2
    return box, read_box


def tex_tokens(text):
    tokens = TEX_TOKENS.findall(text)
    depth = 0
    for token in tokens:
        depth += (token == "{") - (token == "}")
        if depth < 0:
            return []
    return [token for token in tokens if token not in {r"\left", r"\right"}] if (
        not depth and xml_safe(text) == text and "\ufffd" not in text) else []


def recognise_equation_number(source, crop, settings):
    """Read the isolated printed label in two rasterizations."""
    results = []
    start = audit.cursor()
    with audit.details(purpose="equation_number", ocr_engine="tesseract", page=source.number + 1):
        try:
            for dpi in (375, 525):
                raster = source.get_pixmap(dpi=dpi, clip=crop, colorspace=pymupdf.csGRAY, alpha=False)
                pixels = np.frombuffer(raster.samples, dtype=np.uint8).reshape(raster.height, raster.width)
                lines = engine.ocr_image(pixels, "eng", dpi, settings, psm=7, character_whitelist="0123456789.()",
                                         input_reference=f"pdf/page/{source.number + 1}/equation-number/{dpi}dpi")
                match = re.fullmatch(r"\((\d+(?:\.\d+)*)\)", "".join(line.text for line in lines).replace(" ", ""))
                results.append(match[1] if match else "")
        except (OSError, RuntimeError, ValueError, engine.subprocess.TimeoutExpired):
            pass
    ids = audit.ids_since(start)
    if len(results) == 2 and results[0] and results[0] == results[1]:
        audit.mark(ids, validation="passed")
        return results[0], ids
    audit.mark(ids, validation="review_required")
    return "", ids


def recognise_formulas(source, page, settings):
    blocks = [block for block in page.blocks if block.kind == "equation"]
    if not blocks:
        return
    from ocr.glm import model_info

    allowed = (settings.ocr_enabled and (settings.ocr_backend in {"automatic", "glm-ocr"}
                                         or settings.formula_glm_on_other_engines)
               and page.source == "ocr" and not source.rotation and any(block.bbox for block in blocks))
    ready = allowed and model_info(settings)["ready"]
    config = replace(settings, glm_ocr_tokens=min(768, settings.glm_ocr_tokens),
                     glm_ocr_timeout_s=min(45, settings.glm_ocr_timeout_s), glm_ocr_keep_alive="2m")
    rows = []
    for block in blocks:
        old = block.source_reference.get("formula", {})
        if old.get("status") in {"two_pass_agreement", "native_text", "source_reviewed"}:
            rows.append(old)
            continue
        printed = equation_number(block.text)
        number = block.marker or (printed if "." in printed or block.source == "native" else "")
        if number:
            block.marker = number
        original_bbox = old.get("source_bbox") or block.bbox
        record = {"status": "review_required", "number": number, "bbox": block.bbox, "source_bbox": original_bbox,
                  "reason": "No corroborated equation text is available."}
        if block.source == "native" and page.diag.get("health") == "healthy" and tex_tokens(block.text):
            record.update(status="native_text", encoding="unicode", reason="")
        elif block.bbox and ready:
            box, read_box = formula_crop(source, original_bbox, number or LOOSE_NUMBER.search(block.text))
            if box.is_empty or read_box.is_empty:
                record["reason"] = "The equation region lies outside the source page."
                block.bbox = None
                record["bbox"] = None
                block.source_reference["formula"] = record
                rows.append(record)
                continue
            if not number and read_box.x1 < box.x1 and engine.find_tesseract(settings):
                label = pymupdf.Rect(read_box.x1, box.y0, box.x1, box.y1)
                number, number_ids = recognise_equation_number(source, label, settings)
                record["number_execution_ids"] = number_ids
                if number:
                    block.marker = record["number"] = number
                    record["number_source"] = "two_renders_of_printed_label"
            results = []
            tags = []
            execution_ids = []
            with audit.details(purpose="equation_transcription", ocr_engine="glm-ocr"):
                try:
                    for dpi in (300, 450):
                        raster = source.get_pixmap(dpi=dpi, clip=read_box, colorspace=pymupdf.csGRAY, alpha=False)
                        result = glm.recognise_image(raster.tobytes("png"), page.n, config, "formula",
                                                     input_reference=f"pdf/page/{page.n}/equation/{number or len(rows) + 1}/{dpi}dpi")
                        execution_ids.extend(result.execution_ids)
                        if not result.issues and len(result.blocks) == 1 and result.blocks[0].kind == "equation":
                            text = result.blocks[0].text.strip()
                            tag = TAG.search(text)
                            tags.append(tag[1] if tag else "")
                            results.append(VECTOR_STAR.sub(r"\1{\2}^{*}", TAG.sub("", text).strip()))
                except (RecognitionError, OSError, RuntimeError, ValueError) as error:
                    ready = False
                    record["reason"] = str(error)
            block.bbox = tuple(box)
            record.update(bbox=block.bbox, execution_ids=execution_ids)
            if len(results) == 2 and tex_tokens(results[0]) and tex_tokens(results[0]) == tex_tokens(results[1]):
                block.original_text = block.original_text or block.text
                block.text, block.spans, block.ocr_engine = results[0], [], "glm-ocr"
                block.source_reference["execution_ids"] = execution_ids
                if tags[0] and tags[0] == tags[1] and not number:
                    block.marker = tags[0]
                    record["number"] = tags[0]
                record.update(status="two_pass_agreement", encoding="latex", reason="")
                audit.mark(execution_ids, validation="passed")
            else:
                if len(results) == 2:
                    record["reason"] = "The two rendered crops produced different mathematical tokens."
                audit.mark(execution_ids, validation="review_required")
        block.source_reference["formula"] = record
        rows.append(record)
    page.diag["formula_validation"] = rows
    page.diag["equation_review"] = [f"Equation {row['number'] or index} requires text review. {row['reason']}"
                                    for index, row in enumerate(rows, 1) if row["status"] == "review_required"]

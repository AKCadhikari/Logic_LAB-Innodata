"""Repair existing presentation text using attributable PDF evidence."""
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
from pptx.oxml.ns import qn
from rapidfuzz import fuzz, process

from comparison.differ import TOK, Alignment, align_correct, ws_lower
from config.settings import Settings
from layout.model import DocIR, ir_from_dict
from powerpoint.package import RunEdit, open_deck, parse, write_deck
from powerpoint.evidence import EvidenceIndex
from powerpoint import compatibility, formulas, structured
from utils.logs import AuditLog
from utils import execution_audit as model_audit
from utils.textutil import xml_script_digits
from xml_engine.serializer import to_xml

FURNITURE = {"slide-number", "footer", "rule", "date"}


@dataclass(frozen=True)
class Unit:
    text: str
    norm: str
    page: int
    node: str
    confidence: float
    source: str
    reliable: bool
    engine: str = ""
    block_id: str | None = None
    execution_ids: tuple = ()
    kind: str = ""
    bbox: tuple | None = None
    language: str = ""

    def reference(self) -> dict:
        return {"page": self.page, "xml_node": self.node, "text": self.text,
                "extraction": self.source, "ocr_engine": self.engine or None,
                "block_id": self.block_id, "execution_ids": list(self.execution_ids),
                "kind": self.kind, "bbox": self.bbox,
                "extraction_confidence": None if self.engine in {"glm-ocr", "paddleocr-vl"} else self.confidence}


@dataclass(frozen=True)
class Match:
    unit: Unit
    alignment: Alignment
    score: float

    @property
    def confidence(self) -> float:
        return min(0.99, (self.alignment.match_frac + self.score / 100) / 2)


def load_ir(pdf: Path, work: Path, settings: Settings, audit: AuditLog) -> DocIR:
    from utils.cache import file_key

    path = work / "ir" / f"{pdf.stem}.json"
    if path.exists():
        ir = ir_from_dict(json.loads(path.read_text(encoding="utf-8")))
        if (Path(ir.path).resolve() == pdf.resolve() and ir.meta.get("ocr_metadata_version") == 2
                and ir.meta.get("source_sha256") == file_key(pdf)):
            return ir
    from agents.orchestrator import process_pdf

    with TemporaryDirectory() as temp:
        result = process_pdf(pdf, settings, Path(temp), work, audit)
    if not result["ok"]:
        raise ValueError("A successfully extracted source PDF is required for PowerPoint repair.")
    return ir_from_dict(json.loads(path.read_text(encoding="utf-8")))


def build_evidence(ir: DocIR, settings: Settings) -> list[Unit]:
    locations = {}
    to_xml(ir, settings, locations)
    evidence = []
    for page in ir.pages:
        if page.diag.get("error"):
            continue
        for block in page.blocks:
            if block.tag in ("header", "footer") or block.kind == "equation":
                continue
            entries = [(id(block), block.text, block.conf)]
            if block.tag in ("ul", "ol"):
                entries = [(id(item), item.text, item.conf) for item in block.children]
            elif block.tag in ("table", "figure"):
                entries = [((id(block), "caption"), block.caption, block.conf)]
            for key, content, confidence in entries:
                text = " ".join(content.split())
                if block.kind == "byline":
                    text = xml_script_digits(text)
                if len(text) < 6 or key not in locations:
                    continue
                reliable = (block.source == "native" and block.validation_status != "manual_review_required"
                            and page.diag.get("health") == "healthy"
                            and not page.diag.get("error") and confidence >= 80)
                evidence.append(Unit(text, ws_lower(text), page.n, locations[key], confidence,
                                     block.source, reliable, block.ocr_engine, block.source_reference.get("block_id"),
                                     tuple(block.source_reference.get("execution_ids", [])), block.kind,
                                     block.source_reference.get("caption_bbox", block.bbox) if isinstance(key, tuple) else block.bbox,
                                     block.lang))
    return evidence


def _is_group(sh):
    return sh.shape_type == MSO_SHAPE_TYPE.GROUP


def iter_shapes(shapes):
    for sh in shapes:
        if _is_group(sh):
            yield from iter_shapes(sh.shapes)
        else:
            yield sh


def _ph_type(sh):
    try:
        return sh.placeholder_format.type if sh.is_placeholder else None
    except Exception:
        return None


def is_furniture(sh) -> bool:
    t = _ph_type(sh)
    return sh.name.lower() in FURNITURE or t in (PP_PLACEHOLDER.SLIDE_NUMBER, PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.DATE)


def para_text(p) -> str:
    return "".join(r.text for r in p.runs)


def text_paragraphs(sh):
    """Yield (kind, paragraph) for a shape: text-frame paragraphs and table cell paragraphs."""
    if getattr(sh, "has_table", False) and sh.has_table:
        for r in sh.table.rows:
            for c in r.cells:
                for p in c.text_frame.paragraphs:
                    yield "cell", p
    elif sh.has_text_frame:
        for p in sh.text_frame.paragraphs:
            yield "text", p


def apply_text_edit(para, new: str):
    """Rewrite paragraph text by editing only the runs that contain changed characters (formatting preserved)."""
    runs = list(para.runs)
    old = "".join(r.text for r in runs)
    if old == new:
        return
    if not runs:
        para.add_run().text = new; return
    ops = SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in reversed(ops):
        if tag == "equal":
            continue
        seg = new[j1:j2]
        bounds, pos = [], 0
        for r in runs:
            bounds.append((pos, pos + len(r.text))); pos += len(r.text)
        def run_at(ch, prefer_left=False):
            for ri, (s, e) in enumerate(bounds):
                if s <= ch < e or (prefer_left and s < ch <= e):
                    return ri
            return len(runs) - 1
        first = run_at(i1, prefer_left=(i1 == i2 and i1 > 0)) if i1 < i2 or i1 == i2 else 0
        remaining = i2 - i1
        s0, _ = bounds[first]
        r0 = runs[first]
        local = i1 - s0
        take = min(remaining, len(r0.text) - local) if remaining else 0
        r0.text = r0.text[:local] + seg + r0.text[local + take:]
        remaining -= take
        ri = first + 1
        while remaining > 0 and ri < len(runs):
            t = runs[ri].text
            cut = min(remaining, len(t))
            runs[ri].text = t[cut:]; remaining -= cut; ri += 1


def editable_paragraph(paragraph) -> bool:
    protected = {qn("a:fld"), qn("a:br")}
    return bool(paragraph.runs) and not any(child.tag in protected for child in paragraph._p)


def record_text_edit(slide, paragraph, new: str, edits: dict[str, list[RunEdit]]) -> None:
    if not editable_paragraph(paragraph):
        raise ValueError("The paragraph contains a field or special break.")
    nodes = [run._r.find(qn("a:t")) for run in paragraph.runs]
    originals = [node.text or "" for node in nodes]
    apply_text_edit(paragraph, new)
    if para_text(paragraph) != new:
        raise ValueError("The edited runs do not reproduce the approved paragraph.")
    root = slide._element.getroottree()
    part = str(slide.part.partname).lstrip("/")
    edits[part].extend(RunEdit(root.getelementpath(node), old, node.text or "")
                       for node, old in zip(nodes, originals) if (node.text or "") != old)


def candidate_matches(text: str, evidence: list[Unit], normalized: list[str],
                      vocab: Counter, settings: Settings) -> list[Match]:
    scored = []
    for _, score, index in process.extract(ws_lower(text), normalized, scorer=fuzz.partial_ratio,
                                           limit=None, score_cutoff=settings.amend_review_match):
        unit = evidence[index]
        alignment = labeled_number(text, unit.text)
        if alignment is None and unit.kind == "byline":
            alignment = author_alignment(text, unit.text)
        if alignment is None and unit.kind == "keywords":
            alignment = metadata_alignment(text, unit.text)
        if alignment is None:
            alignment = align_correct(text, unit.text, vocab,
                                      strict=unit.confidence < 80 and unit.engine not in {"glm-ocr", "paddleocr-vl"},
                                      ocr=unit.source == "ocr" and unit.engine not in {"glm-ocr", "paddleocr-vl"},
                                      nonlatin_auto=settings.amend_nonlatin_auto)
        if alignment.match_frac >= 0.5:
            scored.append(Match(unit, alignment, score))
    return sorted(scored, key=lambda match: (match.confidence, match.alignment.match_frac), reverse=True)


def author_alignment(old, source):
    """Align author lists with the same separators and at least one identity anchor per author."""
    separate = re.compile(r"(,\s*|\s+and\s+|;\s*)", re.I)
    before, after = separate.split(old), separate.split(source)
    if len(before) < 3 or len(before) != len(after):
        return None
    changes = []
    for index in range(0, len(before), 2):
        first, second = word_tokens(xml_script_digits(before[index])), word_tokens(xml_script_digits(after[index]))
        if first == second:
            continue
        meaningful = set(first) & set(second)
        if not any(len(word) > 2 and word.isalpha() for word in meaningful):
            return None
        matcher = SequenceMatcher(None, first, second, autojunk=False)
        for tag, i, j, x, y in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag not in {"insert", "replace"} or not 1 <= y - x <= 2 or any(not word.isalpha() for word in second[x:y]):
                return None
            changes.append({"type": "author_name", "old": " ".join(first[i:j]),
                            "new": " ".join(second[x:y]), "oov": True, "applied": True})
    if not changes:
        return None
    candidate = "".join(after[index] if index % 2 == 0 else before[index] for index in range(len(before)))
    return Alignment(candidate, .94, changes, "high")


def metadata_alignment(old, source):
    before, separator, old_items = old.partition(":")
    label, colon, source_items = source.partition(":")
    if not separator or not colon or ws_lower(before) != ws_lower(label):
        return None
    original = {ws_lower(value.strip().rstrip(".")): value.strip() for value in old_items.split(",") if value.strip()}
    expected = {ws_lower(value.strip().rstrip(".")): value.strip() for value in source_items.split(",") if value.strip()}
    if min(len(original), len(expected)) < 2 or len(original.keys() & expected.keys()) < 2:
        return None
    if original.keys() == expected.keys() or len(original.keys() ^ expected.keys()) > 2:
        return None
    changes = [{"type": "metadata_list", "old": original.get(key, ""), "new": expected.get(key, ""),
                "oov": True, "applied": True} for key in sorted(original.keys() ^ expected.keys())]
    return Alignment(before + ":" + source_items, .96, changes, "high")


NUMBER_LABEL = re.compile(r"^([^:\r\n]{5,80}:\s*)([+-]?\d+(?:[.,]\d+)*)(\s*[%\w°.-]*)$")


def labeled_number(old: str, source: str) -> Alignment | None:
    before, after = NUMBER_LABEL.fullmatch(old.strip()), NUMBER_LABEL.fullmatch(source.strip())
    if not before or not after:
        return None
    if ws_lower(before[1]) != ws_lower(after[1]) or ws_lower(before[3]) != ws_lower(after[3]):
        return None
    if before[2] == after[2]:
        return Alignment(old, 1, mode="high")
    start = len(old) - len(old.lstrip()) + before.start(2)
    new = old[:start] + after[2] + old[start + len(before[2]):]
    return Alignment(new, 1, [{"type": "number_error", "old": before[2], "new": after[2],
                               "oov": False, "applied": True}], "high")


def confirm_ocr(ir: DocIR, match: Match, replacements: list[dict], settings: Settings, cache: dict[int, str]) -> bool:
    """Require an alternate OCR pass to read each replacement with adjacent context."""
    import pymupdf
    from ocr.engine import available_langs, ocr_page

    unit = match.unit
    if (unit.confidence < 86 and unit.engine not in {"glm-ocr", "paddleocr-vl"}) or not settings.ocr_enabled:
        return False
    if unit.kind in {"byline", "keywords"} and unit.bbox:
        return verify_positioned_text(ir, unit, match.alignment.new_text, settings, cache)
    if unit.bbox and all(edit["type"] != "extra_text" for edit in replacements):
        if verify_positioned_text(ir, unit, match.alignment.new_text, settings, cache, excerpt=True):
            return True
    if unit.page not in cache:
        cache[unit.page] = ""
        try:
            with pymupdf.open(ir.path) as pdf:
                check_settings = replace(settings, ocr_psm=11)
                with model_audit.details(purpose="verify_pptx_source_text"):
                    lines, _ = ocr_page(pdf[unit.page - 1], ir.lang, min(275, max(240, settings.ocr_dpi + 40)),
                                    check_settings, available_langs(check_settings), orient=False, deskew=False)
                cache[unit.page] = " ".join(word_tokens(" ".join(line.text for line in lines if line.conf >= 75)))
        except (OSError, RuntimeError, ValueError, TimeoutExpired):
            return False
    revised = word_tokens(match.alignment.new_text)
    for edit in replacements:
        replacement = word_tokens(edit["new"])
        if edit["type"] == "extra_text":
            if not contains_words(cache[unit.page], revised) or word_tokens(unit.text) != revised:
                return False
            continue
        if not replacement or not any(
            revised[i:i + len(replacement)] == replacement and
            contains_words(cache[unit.page], revised[max(0, i - 2):i + len(replacement) + 2])
            for i in range(len(revised))
        ):
            return False
    return bool(replacements)


def verify_positioned_text(ir, unit, revised, settings, cache, *, excerpt=False, interval=None):
    """Reread the positioned text at two resolutions with a second OCR engine."""
    import numpy as np
    import pymupdf
    from ocr.engine import available_langs, ocr_image
    from utils.textutil import ISO_TO_TESS

    pack = ISO_TO_TESS.get(unit.language) or ISO_TO_TESS.get(ir.lang)
    key = (unit.page, tuple(unit.bbox), pack)
    if key not in cache:
        cache[key] = []
        try:
            if pack not in available_langs(settings):
                return False
            with pymupdf.open(ir.path) as pdf:
                page = pdf[unit.page - 1]
                box = ((pymupdf.Rect(unit.bbox) + (-3, -3, 3, 3)) * page.rotation_matrix) & page.rect
                if box.is_empty or max(box.width, box.height) > 850:
                    return False
                for dpi in (320, 480):
                    image = page.get_pixmap(dpi=dpi, clip=box, colorspace=pymupdf.csGRAY, alpha=False)
                    raster = np.frombuffer(image.samples, dtype=np.uint8).reshape(image.height, image.width)
                    with model_audit.details(page=unit.page, purpose="verify_pptx_source_crop"):
                        lines = ocr_image(raster, pack, dpi, settings, psm=7 if box.height < 30 else 6)
                    cache[key].append([(line.text, line.conf) for line in lines])
        except (OSError, RuntimeError, ValueError, TimeoutExpired):
            return False
    def meaningful(words):
        return [word for word in words if unit.kind != "byline" or word.isalpha()]

    if interval is not None:
        # Tesseract can render a printed mathematical minus as an en or em dash.
        # Require exact brackets and both bounds in each read, with a stronger second read.
        if len(cache[key]) != 2:
            return False
        wanted = word_tokens(revised)
        minus = str.maketrans("\N{MINUS SIGN}\N{EN DASH}\N{EM DASH}", "---")
        for lines, threshold in zip(cache[key], (35, 65)):
            matched = False
            for index, (line, confidence) in enumerate(lines):
                if confidence < threshold:
                    continue
                for value in compatibility.INTERVAL.finditer(line.translate(minus)):
                    anchor = line[:value.start()]
                    if index and lines[index - 1][1] >= 78:
                        anchor = lines[index - 1][0] + " " + anchor
                    if formulas.normalized(value[0]) == interval and word_tokens(anchor)[-len(wanted):] == wanted:
                        matched = True
            if not matched:
                return False
        return True
    wanted = meaningful(word_tokens(revised))
    if len(cache[key]) != 2:
        return False
    for lines in cache[key]:
        if not lines or min(confidence for _, confidence in lines) < 78:
            return False
        words = word_tokens(" ".join(line for line, _ in lines))
        agrees = contains_words(" ".join(words), wanted) if excerpt else meaningful(words) == wanted
        if not agrees:
            return False
    return True


def word_tokens(text: str) -> list[str]:
    return re.findall(r"[+-]?\d+(?:[.,]\d+)*%?|[^\W\d_]+", text.casefold().replace("\N{MINUS SIGN}", "-"))


def contains_words(text: str, phrase: list[str]) -> bool:
    return bool(phrase and (" " + " ".join(phrase) + " ") in (" " + text + " "))


def local_edit(edit: dict, revised: str, source: str) -> bool:
    if edit["type"] == "author_name":
        return 1 <= len(word_tokens(edit["new"])) <= 2
    if edit["type"] == "metadata_list":
        return word_tokens(revised) == word_tokens(source)
    if edit["type"] in {"number_error", "character_error", "word_error"}:
        return bool(edit["old"].strip() and edit["new"].strip() and len(word_tokens(edit["new"])) <= 2)
    if edit["type"] == "missing_words":
        return not edit["old"].strip() and 1 <= len(word_tokens(edit["new"])) <= 2
    return (edit["type"] == "extra_text" and not edit["new"].strip()
            and 1 <= len(word_tokens(edit["old"])) <= 2 and word_tokens(source) == word_tokens(revised))


def source_pages(slide, total: int) -> set[int]:
    if not slide.has_notes_slide or slide.notes_slide.notes_text_frame is None:
        return set()
    notes = slide.notes_slide.notes_text_frame.text
    match = re.search(r"^Source:\s*pages?\s+(\d+(?:(?:\s*[,–-]\s*|\s+and\s+)\d+)*)", notes, re.I | re.M)
    if not match:
        return set()
    raw = match[1]
    pages = {int(page) for page in re.findall(r"\d+", raw)}
    for start, end in re.findall(r"(\d+)\s*[-–]\s*(\d+)", raw):
        pages.update(range(max(1, int(start)), min(total, int(end)) + 1))
    return {page for page in pages if 1 <= page <= total}


def repair_presentation(ir: DocIR, source: Path, target: Path, settings: Settings) -> dict:
    from powerpoint.repair_qa import write_qa

    started = time.monotonic()
    with model_audit.scope(ir.doc_id, ir.meta.get("source_sha256"),
                           trace_id=ir.meta.get("execution_trace_id")) as trace:
        report = _repair_presentation(ir, source, target, settings)
        report["verification_executions"] = trace["executions"]
    report["repair_seconds"] = round(time.monotonic() - started, 2)
    report["qa_artifacts"] = write_qa(ir, target, report)
    return report


def _repair_presentation(ir, source, target, settings):
    from powerpoint.numbering import numbering
    from powerpoint.validation import verify_presentation

    if source.resolve() == target.resolve() or (target.exists() and target.samefile(source)):
        raise ValueError("Choose an output path different from the original presentation.")
    evidence = build_evidence(ir, settings)
    retrieval = EvidenceIndex(evidence, settings)
    vocab = Counter(token.lower() for unit in evidence for token in TOK.findall(unit.text) if token.isalnum())
    deck = open_deck(source)
    records = []
    edits = defaultdict(list)
    checked_pages = {}
    equation_sources = list(formulas.equation_evidence(ir))
    tables = list(structured.source_tables(ir))
    links = structured.source_links(ir)
    inventory = Counter()
    for number, slide in enumerate(deck.slides, 1):
        hints = source_pages(slide, len(ir.pages)) if settings.amend_use_notes else set()
        if settings.amend_use_notes and slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            hints = retrieval.corroborated_pages(slide.notes_slide.notes_text_frame.text, hints)
            inventory["notes"] += 1
        context = slide.shapes.title.text if slide.shapes.title is not None else ""
        for shape in iter_shapes(slide.shapes):
            inventory["shapes_inspected"] += 1
            inventory[str(shape.shape_type)] += 1
            if shape.has_table:
                inventory["table_cells"] += len(shape.table.rows) * len(shape.table.columns)
                inspect_table(ir, slide, number, shape, tables, records, edits)
                continue
            if not shape.has_text_frame:
                if shape.has_chart:
                    try:
                        findings = structured.chart_discrepancies(shape.chart, tables)
                    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
                        findings = None
                    if findings is None:
                        records.append(change_record(ir, number, shape, 0, "", "", 0, "review",
                                                     "Chart data did not identify a unique source table.",
                                                     change_type="chart_review"))
                    else:
                        for series, category, old, new, proof in findings:
                            locator = {"shape_id": shape.shape_id, "shape": shape.name,
                                       "series": series, "category": category}
                            records.append(object_record(ir, number, locator, old, new, "chart_value", proof, False,
                                                         "The chart differs from its source cell. Review the cached series and workbook together."))
                continue
            if is_furniture(shape) or shape.name.lower().startswith("breadcrumb"):
                continue
            for index, paragraph in enumerate(shape.text_frame.paragraphs, 1):
                inventory["text_paragraphs"] += 1
                if not editable_paragraph(paragraph):
                    continue
                text = para_text(paragraph)
                if len(text.strip()) < 6 or sum(char.isalpha() for char in text) < 4:
                    continue
                shortlisted = retrieval.shortlist(text, context, hints)
                matches = candidate_matches(text, shortlisted, [unit.norm for unit in shortlisted], vocab, settings)
                if not matches:
                    if sum(char.isalpha() for char in text) >= 25:
                        records.append(change_record(ir, number, shape, index, text, "", 0, "review",
                                                     "No matching passage found in the source PDF."))
                    continue

                best = matches[0]
                alignment = best.alignment
                title_variant = re.sub(r"\s*\((?:cont\.?|continued)\)\s*$", "", text, flags=re.I)
                if ws_lower(title_variant) == best.unit.norm:
                    continue
                proposals = [edit for edit in alignment.edits
                             if edit["old"].strip() != edit["new"].strip() and
                             (edit["applied"] or edit["oov"] or re.search(r"\d", edit["old"] + edit["new"]))]
                if not proposals:
                    continue
                conflicting = [match.unit.reference() for match in matches[1:]
                               if best.confidence - match.confidence <= 0.05
                               and match.alignment.new_text != alignment.new_text]
                applied_edits = [edit for edit in proposals if edit["applied"]]
                safe_edits = all(local_edit(edit, alignment.new_text, best.unit.text) for edit in applied_edits)
                supported = (not conflicting and bool(applied_edits) and safe_edits
                             and alignment.mode == "high" and best.score >= settings.amend_min_match)
                verified_ocr = (supported and best.unit.source == "ocr"
                                and confirm_ocr(ir, best, applied_edits, settings, checked_pages))
                apply = supported and (best.unit.reliable or verified_ocr)
                if apply:
                    reason = ("The replacement and adjacent text agree across two local OCR passes." if verified_ocr
                              else "An unambiguous replacement is supported by the native PDF passage.")
                elif conflicting:
                    reason = "Multiple source passages support different text."
                elif not best.unit.reliable:
                    reason = "Source extraction requires verification before this edit can be applied."
                else:
                    reason = "The alignment does not support a high-confidence replacement in the existing text."
                if apply:
                    record_text_edit(slide, paragraph, alignment.new_text, edits)
                record = change_record(ir, number, shape, index, text, alignment.new_text if apply else "",
                                       best.confidence, "applied" if apply else "review", reason, best.unit)
                record["proposals"] = applied_edits if apply else proposals
                if not apply:
                    record["proposed_content"] = alignment.new_text if alignment.new_text != text else None
                if len(record["proposals"]) == 1:
                    record["change_type"] = record["proposals"][0]["type"]
                if conflicting:
                    record["alternatives"] = conflicting
                if verified_ocr:
                    ids = [row["execution_id"] for row in model_audit.since(0)
                           if row.get("page") == best.unit.page and row["validation"] == "passed"]
                    record["source"]["execution_ids"].extend(ids)
                records.append(record)
        part = str(slide.part.partname).lstrip("/")
        with ZipFile(source) as original:
            root = parse(original.read(part))
            inventory["office_math_nodes"] += sum(1 for _ in root.iter(qn("m:oMath")))
            inventory["compatibility_objects"] += len(root.xpath("//*[local-name()='AlternateContent']"))
            for locator, old, new, proof, changes, reason in formulas.inspect_formulas(root, equation_sources, hints):
                if changes:
                    edits[part].extend(changes)
                records.append(object_record(ir, number, locator, old, new, "equation", proof, bool(changes), reason))
            for locator, old, new, unit, excerpt, changes in compatibility.inline_intervals(root, evidence):
                confirmed = bool(changes) and (unit.reliable or (
                    settings.ocr_enabled and unit.bbox and
                    verify_positioned_text(ir, unit, excerpt, settings, checked_pages, interval=new)))
                if confirmed:
                    edits[part].extend(changes)
                proof = {**unit.reference(), "expected_tokens": list(new)}
                records.append(object_record(ir, number, locator, old, new, "equation", proof, confirmed,
                                             "Matching variable and prose identify the source interval." if confirmed else
                                             "The source interval or its Office structure needs independent review."))
            for locator, old, new, page, block, line, source_text, patch in compatibility.algorithm_references(root, ir):
                proof = {"page": page.n, "bbox": block.line_boxes[line], "text": source_text, "extraction": block.source,
                         "block_id": block.source_reference.get("block_id")}
                field = Unit(source_text, ws_lower(source_text), page.n, "", block.conf, block.source,
                             False, block.ocr_engine, bbox=block.line_boxes[line], kind="algorithm_line", language=block.lang)
                reliable = (block.source == "native" and page.diag.get("health") == "healthy" and not page.requires_review)
                apply = reliable or (settings.ocr_enabled and verify_positioned_text(ir, field, new, settings, checked_pages))
                if apply:
                    edits[part].append(patch)
                records.append(object_record(ir, number, locator, old, new, "algorithm_reference", proof, apply,
                                             "Adjacent algorithm lines identify the numbered source reference." if apply else
                                             "The matching algorithm line could not be independently confirmed."))
            for rel_part, patch, proof, pointer, text_edits in structured.hyperlink_edits(original, part, root, links):
                shape = next(pointer.iterancestors(qn("p:sp")), None)
                node = shape.find(".//" + qn("p:cNvPr")) if shape is not None else None
                locator = {"shape_id": int(node.get("id")) if node is not None else None,
                           "shape": node.get("name") if node is not None else "Hyperlink",
                           "relationship_part": rel_part, "visible_url_edited": bool(text_edits)}
                occupied = {edit.path for edit in edits.get(part, ())}
                can_apply = not any(edit.path in occupied for edit in text_edits)
                if can_apply:
                    edits[rel_part].append(patch)
                    if text_edits:
                        edits[part].extend(text_edits)
                records.append(object_record(ir, number, locator, patch.old, patch.new, "hyperlink", proof, can_apply,
                                             "The displayed address and the PDF identify a unique complete URL." if can_apply
                                             else "A separate edit overlaps the linked text and requires review."))
    for item in numbering(deck, edits):
        if item.get("old") != item.get("new"):
            location = {"slide": item["slide"], "shape_id": item["shape_id"], "shape": item["shape"]}
            records.append({**item, "action": "applied", "change_type": "slide_number", "confidence": 1,
                            "document": ir.doc_id, "original_location": location, "new_location": location,
                            "old_content": item["old"], "new_content": item["new"],
                            "reason": "Updated the cached number in the existing slide-number field or shape.",
                            "source": {"part": item["part"], "expected_number": item["expected"]}})
    for index, record in enumerate(records, 1):
        record["error_id"] = f"E{index:04d}"
    from powerpoint.advisory import review
    advisory = review(records, settings)
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="pptx-repair-", dir=target.parent) as temp:
        staged = Path(temp) / target.name
        write_deck(source, staged, edits, len(deck.slides))
        validation = verify_presentation(source, staged, edits, records, settings.amend_render_validation)
        failed = {record["slide"] for record in records if record["validation_status"] == "visible_write_back_failure"}
        render_first = validation["render"]
        if failed:
            rejected = {str(deck.slides[number - 1].part.partname).lstrip("/") for number in failed}
            rejected.update(record["relationship_part"] for record in records if record["slide"] in failed
                            and record["action"] == "applied" and record["change_type"] == "hyperlink")
            edits = {part: ops for part, ops in edits.items() if part not in rejected}
            for record in records:
                if record["slide"] in failed and record["action"] == "applied":
                    record.update(action="review", render_verification="reverted",
                                  proposed_content=record["new"], recommended_action="human review",
                                  reason="The rendered slide failed its visible-content check. The slide edits were reverted.")
            write_deck(source, staged, edits, len(deck.slides))
            validation = verify_presentation(source, staged, edits, records, settings.amend_render_validation)
            validation["first_render"] = render_first
        validation["iterations"] = 2 if failed else 1
        staged.replace(target)
    return {"slides": len(deck.slides), "applied": [r for r in records if r["action"] == "applied"],
            "review": [r for r in records if r["action"] == "review"], "records": records,
            "slide_numbering": numbering(open_deck(target)), "status": "updated" if edits else "unchanged",
            "object_inventory": dict(inventory), "presentation_validation": validation, "advisory": advisory}


def inspect_table(ir, slide, number, shape, sources, records, edits):
    table = shape.table
    candidates = structured.table_matches(table, sources)
    if not candidates:
        records.append(change_record(ir, number, shape, 0, "", "", 0, "review",
                                     "The table headers and row labels do not identify a source grid.",
                                     change_type="table_alignment"))
        return
    page, block, grid = candidates[0]
    for (row, col), source in grid.items():
        target = table.cell(row, col)
        if source is None or target.is_spanned or target.is_merge_origin:
            continue
        old, new = target.text, source.text
        if ws_lower(old) == ws_lower(new):
            continue
        consistent = all(other.get((row, col)) is not None and ws_lower(other[row, col].text) == ws_lower(new)
                         for _, _, other in candidates[1:])
        paragraphs = target.text_frame.paragraphs
        reliable = block.source == "native" and page.diag.get("health") == "healthy" and not page.requires_review
        can_apply = (consistent and reliable and len(paragraphs) == 1 and editable_paragraph(paragraphs[0])
                     and bool(new.strip()) and source.rowspan == source.colspan == 1)
        if can_apply:
            record_text_edit(slide, paragraphs[0], new, edits)
        locator = {"shape_id": shape.shape_id, "shape": shape.name, "row": row + 1, "column": col + 1}
        proof = {"page": page.n, "bbox": source.bbox or block.bbox, "text": new, "extraction": block.source,
                 "table_row": source.row + 1, "table_column": source.col + 1}
        records.append(object_record(ir, number, locator, old, new, "table_cell", proof, can_apply,
                                     "Row and column labels identify this source cell." if can_apply else
                                     "Matched source cell is ambiguous or cannot be edited safely."))


def object_record(ir, slide, locator, old, new, category, proof, applied, reason):
    location = {"slide": slide, **locator}
    return {**location, "document": ir.doc_id, "original_location": location, "new_location": dict(location),
            "change_type": category, "old": old, "new": new, "old_content": old, "new_content": new,
            "confidence": round(proof.get("alignment", .98), 3) if proof else 0,
            "source": proof, "action": "applied" if applied else "review",
            "recommended_action": "none" if applied else "human review", "reason": reason}


def change_record(ir: DocIR, slide: int, shape, paragraph: int, old: str, new: str, confidence: float,
                  action: str, reason: str, unit: Unit | None = None, *, change_type="text_correction") -> dict:
    location = {"slide": slide, "shape_id": shape.shape_id, "shape": shape.name, "paragraph": paragraph}
    return {"document": ir.doc_id, "original_location": location, "new_location": dict(location),
            "change_type": change_type, "old_content": old, "new_content": new,
            "old": old, "new": new, **location, "confidence": round(confidence, 3), "action": action,
            "recommended_action": "none" if action == "applied" else "human review", "reason": reason,
            "source": unit.reference() if unit else None}


def amend_deck(pdf: Path, pptx: Path, target: Path, work: Path, settings: Settings, audit: AuditLog, *,
               ir: DocIR | None = None) -> dict:
    from utils.cache import file_key

    started = time.monotonic()
    ir = ir if ir is not None else load_ir(pdf, work, settings, audit)
    if ir.meta.get("source_sha256") and file_key(pdf) != ir.meta["source_sha256"]:
        raise ValueError("Source PDF evidence does not match the selected PDF.")
    report = repair_presentation(ir, pptx, target, settings)
    changes_dir = work / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    (changes_dir / f"{pptx.stem}.json").write_text(json.dumps(report["records"], ensure_ascii=False, indent=2),
                                                encoding="utf-8")
    result = {"pptx": pptx.name, "pdf": pdf.name, "output": target.name, "slides": report["slides"],
              "applied": len(report["applied"]), "review": len(report["review"]), "status": report["status"],
              "types": dict(Counter(r["change_type"] for r in report["applied"])),
              "valid": True, "seconds": round(time.monotonic() - started, 2)}
    audit.event("powerpoint", "deck_done", **result)
    return {**result, "records": report["records"], "slide_numbering": report["slide_numbering"],
            "verification_executions": report["verification_executions"],
            "object_inventory": report["object_inventory"], "presentation_validation": report["presentation_validation"],
            "qa_artifacts": report["qa_artifacts"], "advisory": report["advisory"]}

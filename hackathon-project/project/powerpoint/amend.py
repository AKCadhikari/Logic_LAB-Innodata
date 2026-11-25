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

from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
from pptx.oxml.ns import qn
from rapidfuzz import fuzz, process

from comparison.differ import TOK, Alignment, align_correct, ws_lower
from config.settings import Settings
from layout.model import DocIR, ir_from_dict
from powerpoint.package import RunEdit, open_deck, write_deck
from utils.logs import AuditLog
from utils import execution_audit as model_audit
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

    def reference(self) -> dict:
        return {"page": self.page, "xml_node": self.node, "text": self.text,
                "extraction": self.source, "ocr_engine": self.engine or None,
                "block_id": self.block_id, "execution_ids": list(self.execution_ids),
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
        if page.requires_review:
            continue
        for block in page.blocks:
            if block.tag in ("header", "footer") or block.kind in {"equation", "pseudocode"}:
                continue
            entries = [(id(block), block.text, block.conf)]
            if block.tag in ("ul", "ol"):
                entries = [(id(item), item.text, item.conf) for item in block.children]
            elif block.tag in ("table", "figure"):
                entries = [((id(block), "caption"), block.caption, block.conf)]
            for key, content, confidence in entries:
                text = " ".join(content.split())
                if len(text) < 6 or key not in locations:
                    continue
                reliable = (block.source == "native" and block.validation_status != "manual_review_required"
                            and page.diag.get("health") == "healthy"
                            and not page.diag.get("error") and confidence >= 80)
                evidence.append(Unit(text, ws_lower(text), page.n, locations[key], confidence,
                                     block.source, reliable, block.ocr_engine, block.source_reference.get("block_id"),
                                     tuple(block.source_reference.get("execution_ids", []))))
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
        if alignment is None:
            alignment = align_correct(text, unit.text, vocab,
                                      strict=unit.confidence < 80 and unit.engine not in {"glm-ocr", "paddleocr-vl"},
                                      ocr=unit.source == "ocr", nonlatin_auto=settings.amend_nonlatin_auto)
        if alignment.match_frac >= 0.5:
            scored.append(Match(unit, alignment, score))
    return sorted(scored, key=lambda match: (match.confidence, match.alignment.match_frac), reverse=True)


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


def word_tokens(text: str) -> list[str]:
    return re.findall(r"[+-]?\d+(?:[.,]\d+)*%?|[^\W\d_]+", text.casefold())


def contains_words(text: str, phrase: list[str]) -> bool:
    return bool(phrase and (" " + " ".join(phrase) + " ") in (" " + text + " "))


def local_edit(edit: dict, revised: str, source: str) -> bool:
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
    with model_audit.scope(ir.doc_id, ir.meta.get("source_sha256"),
                           trace_id=ir.meta.get("execution_trace_id")) as trace:
        report = _repair_presentation(ir, source, target, settings)
        report["verification_executions"] = trace["executions"]
    return report


def _repair_presentation(ir, source, target, settings):
    from powerpoint.numbering import numbering

    evidence = build_evidence(ir, settings)
    normalized = [unit.norm for unit in evidence]
    vocab = Counter(token.lower() for unit in evidence for token in TOK.findall(unit.text) if token.isalnum())
    deck = open_deck(source)
    records = []
    edits = defaultdict(list)
    checked_pages = {}
    for number, slide in enumerate(deck.slides, 1):
        hints = source_pages(slide, len(ir.pages)) if settings.amend_use_notes else set()
        hinted = [unit for unit in evidence if unit.page in hints]
        hinted_text = [unit.norm for unit in hinted]
        for shape in iter_shapes(slide.shapes):
            if is_furniture(shape) or shape.name.lower().startswith("breadcrumb") or not shape.has_text_frame:
                continue
            for index, paragraph in enumerate(shape.text_frame.paragraphs, 1):
                if not editable_paragraph(paragraph):
                    continue
                text = para_text(paragraph)
                if len(text.strip()) < 6 or sum(char.isalpha() for char in text) < 4:
                    continue
                matches = candidate_matches(text, hinted, hinted_text, vocab, settings) if hinted else []
                if not matches or matches[0].alignment.match_frac < 0.75:
                    matches = candidate_matches(text, evidence, normalized, vocab, settings)
                if not matches:
                    if sum(char.isalpha() for char in text) >= 25:
                        records.append(change_record(ir, number, shape, index, text, "", 0, "review",
                                                     "No matching passage found in the source PDF."))
                    continue

                best = matches[0]
                alignment = best.alignment
                if ws_lower(text) == best.unit.norm:
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
                if len(record["proposals"]) == 1:
                    record["change_type"] = record["proposals"][0]["type"]
                if conflicting:
                    record["alternatives"] = conflicting
                if verified_ocr:
                    ids = [row["execution_id"] for row in model_audit.since(0)
                           if row.get("page") == best.unit.page and row["validation"] == "passed"]
                    record["source"]["execution_ids"].extend(ids)
                records.append(record)
    for item in numbering(deck, edits):
        if item.get("old") != item.get("new"):
            location = {"slide": item["slide"], "shape_id": item["shape_id"], "shape": item["shape"]}
            records.append({**item, "action": "applied", "change_type": "slide_number", "confidence": 1,
                            "document": ir.doc_id, "original_location": location, "new_location": location,
                            "old_content": item["old"], "new_content": item["new"],
                            "reason": "Updated the cached number in the existing slide-number field or shape.",
                            "source": {"part": item["part"], "expected_number": item["expected"]}})
    write_deck(source, target, edits, len(deck.slides))
    return {"slides": len(deck.slides), "applied": [r for r in records if r["action"] == "applied"],
            "review": [r for r in records if r["action"] == "review"], "records": records,
            "slide_numbering": numbering(open_deck(target)), "status": "updated" if edits else "unchanged"}


def change_record(ir: DocIR, slide: int, shape, paragraph: int, old: str, new: str, confidence: float,
                  action: str, reason: str, unit: Unit | None = None) -> dict:
    location = {"slide": slide, "shape_id": shape.shape_id, "shape": shape.name, "paragraph": paragraph}
    return {"document": ir.doc_id, "original_location": location, "new_location": dict(location),
            "change_type": "text_correction", "old_content": old, "new_content": new,
            "old": old, "new": new, **location, "confidence": round(confidence, 3), "action": action,
            "recommended_action": "none" if action == "applied" else "human review", "reason": reason,
            "source": unit.reference() if unit else None}


def amend_deck(pdf: Path, pptx: Path, target: Path, work: Path, settings: Settings, audit: AuditLog) -> dict:
    started = time.monotonic()
    ir = load_ir(pdf, work, settings, audit)
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
            "verification_executions": report["verification_executions"]}

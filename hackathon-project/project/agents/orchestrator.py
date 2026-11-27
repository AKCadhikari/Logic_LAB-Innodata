"""Document diagnostics, local OCR, layout reconstruction and validated XML."""
from __future__ import annotations
import json, os, time, traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

from config.settings import Settings
from diagnostics.page_diagnostics import analyze_page
from diagnostics.profile import decorate_document, quality_gate
from quality.layout_fidelity import layout_fidelity
from ingestion.discover import describe
from layout.model import PageIR, DocIR, Line, Block, Span, ir_from_dict
from layout import native, reconstruct, kinds, figures, ocr_geometry, equations, pseudocode, doclayout, region_association, running
from layout.docling_adapter import DoclingLayout
from ocr import engine as ocr
from ocr.paddle import ARABIC_RECOGNIZER
from ocr.router import OCRRouter
from ocr.recognition import ENGINE_MODES
from quality.qa import page_confidence, doc_quality
from utils.cache import Cache, file_key
from utils.logs import AuditLog
from utils.naming import xml_name
from utils.textutil import detect_lang, language_profile, norm_cmp, norm_ws
from reporting.exports import write_report
from reporting import execution as execution_reports
from utils import execution_audit as model_audit
from xml_engine.serializer import to_xml
from xml_engine.validate import validate_xml, verify_unicode
from xml_engine.completeness import compare_content, numeric_values
from xml_engine.assets import inspect_assets

def ocr_cache_identity(settings, installed):
    if not settings.use_cache or not installed or not settings.tessdata_dir:
        return None
    from hashlib import sha256

    binary = Path(ocr.find_tesseract(settings)).resolve()
    models = [Path(settings.tessdata_dir) / f"{pack}.traineddata" for pack in sorted(installed)]
    if not binary.is_file() or not all(path.is_file() for path in models):
        return None
    inputs = [file_key(binary), *(file_key(path) for path in models), str(settings.ocr_psm), ocr.cv2.__version__]
    return sha256("|".join(inputs).encode()).hexdigest()


def _lines_to_json(lines):
    return [{**l.__dict__, "spans": [s.__dict__ for s in l.spans]} for l in lines]


def _lines_from_json(arr):
    return [Line(**{**a, "bbox": tuple(a["bbox"]), "spans": [Span(**s) for s in a.get("spans", [])]}) for a in arr]


def _ocr_cached(page, cache, identity, fkey, lang, settings, installed, deskew, dpi):
    key = ("language-order-v2", identity, fkey, f"p{page.number + 1}", f"{dpi}_{lang}_{int(deskew)}")
    hit = cache.get(*key) if identity else None
    if hit:
        return _lines_from_json(hit["lines"]), hit["info"], True
    lines, info = ocr.ocr_page(page, lang, dpi, settings, installed, orient=deskew, deskew=deskew)
    if identity and lines:
        cache.put({"lines": _lines_to_json(lines), "info": info}, *key)
    return lines, info, False


def _text_of(lines) -> str:
    return " ".join(l.text for l in lines)


def process_pdf(pdf: Path, settings: Settings, out_dir: Path, work_dir: Path, audit: AuditLog,
                deadline: float | None = None, progress: Callable[[int, int], None] | None = None,
                processing: Callable[[dict], None] | None = None, *, document_id=None) -> dict:
    private = work_dir / "private-model-responses" if settings.audit_private_responses else None
    with model_audit.scope(document_id or pdf.stem, file_key(pdf), private):
        try:
            result = _process_pdf(pdf, settings, out_dir, work_dir, audit, deadline, progress, processing)
        except Exception:
            save_execution_reports(work_dir, pdf.stem, {})
            raise
        if "execution_evidence" not in result:
            save_execution_reports(work_dir, pdf.stem, result)
        return result


def _process_pdf(pdf, settings, out_dir, work_dir, audit, deadline, progress, processing):
    with ExitStack() as resources:
        return _extract_pdf(pdf, settings, out_dir, work_dir, audit, deadline, progress, processing, resources)


def _extract_pdf(pdf, settings, out_dir, work_dir, audit, deadline, progress, processing, resources):
    import pymupdf
    from rapidfuzz import fuzz
    t0 = time.time()
    doc_id = pdf.stem
    res = {"doc_id": doc_id, "file": pdf.name, "ok": False, "pages": 0, "error": "", "page_log": []}
    cache = Cache(work_dir / settings.cache_dir, settings.use_cache)
    router = OCRRouter(settings, work_dir / "recognition" / doc_id) if settings.ocr_backend != "tesseract" else None
    layout = DoclingLayout(settings)
    resources.callback(layout.close)
    if router:
        resources.callback(router.close)
    counts = {"required": 0, "glm_ocr": 0, "paddle_ocr": 0, "failed": 0, "review": 0}
    res["processing_counts"] = counts

    def stage(name: str, **values) -> None:
        if processing:
            processing({"stage": name, "counts": dict(counts), **values})

    fkey = model_audit.current()["document_sha256"]
    stage("diagnostics", state="running")
    try:
        doc = resources.enter_context(pymupdf.open(str(pdf)))
    except Exception as e:
        audit.event("ingestion", "unreadable", doc=doc_id, error=str(e))
        res["error"] = f"cannot open: {e}"
        _write_xml(out_dir, pdf, b'<?xml version="1.0" encoding="UTF-8"?>\n<document lang="und"/>\n')
        return res
    if not doc.is_pdf:
        res["error"] = "The uploaded file is not a PDF."
        return res
    if doc.needs_pass:
        res["error"] = "PDF is encrypted and requires a password."
        return res
    n = doc.page_count
    if not n:
        res["error"] = "PDF contains no pages."
        return res
    if progress:
        progress(0, n)
    res["pages"] = n
    installed = ocr.available_langs(settings) if settings.ocr_enabled and settings.ocr_backend == "tesseract" else set()
    cache_identity = ocr_cache_identity(settings, installed)
    _, metadata_language = doc.xref_get_key(doc.pdf_catalog(), "Lang")
    metadata_language = metadata_language.split("-", 1)[0].lower()
    audit.event("analysis", "start", doc=doc_id, pages=n)
    # ---- Document Analysis agent: per-page diagnostics ------------------------------------------
    diags = []
    for i in range(n):
        try:
            diags.append(analyze_page(doc[i], settings, metadata_language))
        except Exception as e:
            diags.append({"page": i + 1, "health": "error", "decision": "native", "reasons": [str(e)], "native_chars": 0, "width": 595, "height": 842})
            audit.event("analysis", "page_error", doc=doc_id, page=i + 1, error=str(e))
    counts["required"] = sum(bool(d.get("ocr_required")) for d in diags)
    usable = " ".join(doc[i].get_text()[:3000] for i in range(n) if diags[i]["health"] in ("healthy", "suspicious"))[:24000]
    lang = detect_lang(usable)
    arabic_document = ((metadata_language == "ar" or any(d.get("lang_hint") == "ar" for d in diags))
                       and not any(d.get("scripts", {}).get("latin", 0) > d.get("scripts", {}).get("arab", 0)
                                   for d in diags))
    audit.event("analysis", "doc_language", doc=doc_id, lang=lang, healthy_pages=sum(d["health"] == "healthy" for d in diags),
                decisions={k: sum(d["decision"] == k for d in diags) for k in ("native", "compare", "ocr")})
    stage("diagnostics", state="failed" if any(d["health"] == "error" for d in diags) else "completed")
    layout_pages = detect_layout(doc, settings, work_dir, stage, audit)
    # ---- OCR & Extraction agent ---------------------------------------------------------------
    stage("ocr", state="running")
    raw = []
    for i in range(n):
        d = diags[i]; tp = time.time()
        page_language = d.get("lang_hint", "und")
        if settings.ocr_language == "ar-en":
            native_language = d.get("native_language", {})
            latin_only = (native_language.get("script") == "latin" and native_language.get("confidence", 0) >= .6
                          and not d.get("scripts", {}).get("arab"))
            page_language = "en" if latin_only else "ar"
        elif settings.ocr_language in {"ar", "en"}:
            page_language = settings.ocr_language
        elif page_language == "und" and arabic_document:
            page_language = "ar"
        d["ocr_language_requested"] = settings.ocr_language or "auto"
        d["ocr_language_hint"] = page_language
        fast = bool(deadline and time.time() > deadline)
        dpi = settings.ocr_dpi_fast if fast else settings.ocr_dpi
        rec = {"n": i + 1, "w": d["width"], "h": d["height"], "lines": [], "tables": [], "figs": [],
               "source": "native", "diag": d, "blocks": None}
        try:
            page = doc[i]
            decision = d["decision"]
            if fast and decision == "compare":
                decision = "native"; d["reasons"].append("fast mode: compare skipped")
                d["ocr"] = {"engine": None, "status": "skipped", "requires_review": True}
            recovery = None
            if decision == "ocr" and settings.ocr_enabled and router is not None:
                recovery = router.recover_unmapped_text(page, dpi, d,
                    lambda engine: stage("ocr", page=i + 1, engine=engine))
            if recovery:
                rec["lines"], d["ocr"] = recovery
                rec["source"] = "ocr"
                if page_language == "ar" and numeric_values(page.get_text()) != numeric_values(_text_of(rec["lines"])):
                    d["reasons"].append("OCR and embedded PDF text disagree on numeric values. Verify them against the source image.")
                    d["ocr"]["requires_review"] = True
                rec["figs"] = native.extract_figures(page, settings, [])
                audit.event("ocr", "selection", doc=doc_id, page=i + 1, **d["ocr"])
            elif decision != "native" and settings.ocr_enabled and settings.ocr_backend in ENGINE_MODES:
                model_names = {"glm-ocr": settings.glm_ocr_model, "paddleocr-vl": settings.paddle_vl_model}
                result, info = router.recognise(page, page_language, dpi, d,
                                               lambda engine: stage("ocr", page=i + 1, engine=engine, model=model_names.get(engine)),
                                               event=lambda attempt: stage("ocr_attempt", page=i + 1, **attempt))
                d["ocr"] = info
                if result and decision == "compare":
                    native_text = page.get_text()
                    ocr_text = result.text()
                    similarity = fuzz.token_set_ratio(norm_cmp(native_text), norm_cmp(ocr_text)) / 100
                    numeric_disagreement = numeric_values(native_text) != numeric_values(ocr_text)
                    d["compare_similarity"] = round(similarity, 3)
                    if d.get("native_decoding_risk"):
                        d["compare_order_similarity"] = round(fuzz.ratio(norm_ws(native_text), norm_ws(ocr_text)) / 100, 3)
                        d["native_order_corroborated"] = norm_ws(native_text) == norm_ws(result.text()) and not info["requires_review"]
                        info["requires_review"] |= not d["native_order_corroborated"]
                        result = None
                    elif similarity >= settings.native_vs_ocr_min_similarity:
                        if numeric_disagreement:
                            d["reasons"].append("OCR and native text disagree on numeric values. Review the source before relying on either.")
                            info["requires_review"] = True
                        else:
                            d["reasons"].append("Kept native text after comparing OCR.")
                        result = None
                    else:
                        d["reasons"].append("OCR and native text disagree. Review before relying on the result.")
                        info["requires_review"] = True
                        if d.get("native_language", {}).get("direction") in {"rtl", "mixed"}:
                            result = None
                if result and d.get("mixed_content"):
                    similarity = fuzz.partial_ratio(norm_cmp(page.get_text()).strip(), norm_cmp(result.text()))
                    if similarity < settings.native_vs_ocr_min_similarity * 100:
                        d["reasons"].append("The OCR result could not corroborate the sparse native text.")
                        info["requires_review"] = True
                if result and page_language == "ar":
                    recognized = result.text()
                    if d.get("native_chars") and numeric_values(page.get_text()) != numeric_values(recognized):
                        d["reasons"].append("OCR and embedded PDF text disagree on numeric values. Verify them against the source image.")
                        info["requires_review"] = True
                    if d.get("scripts", {}).get("latin", 0) >= 8 and not result.language.get("scripts", {}).get("latin"):
                        d["reasons"].append("OCR did not retain Latin text present in the embedded layer.")
                        info["requires_review"] = True
                    if (result.language.get("direction") == "mixed"
                            and recognized.count("(") != recognized.count(")")):
                        d["reasons"].append("Mixed-direction OCR contains unmatched parentheses. Verify punctuation against the source.")
                        info["requires_review"] = True
                if info.get("review_reason"):
                    d["reasons"].append(info["review_reason"])
                audit.event("ocr", "selection", doc=doc_id, page=i + 1, **info)
                if result:
                    rec["lines"], rec["blocks"], rec["source"] = result.lines, result.blocks or None, "ocr"
                    if result.engine == "paddleocr" and result.model == ARABIC_RECOGNIZER:
                        low_confidence = sum(line.conf < 65 for line in result.lines)
                        if low_confidence:
                            d["reasons"].append(f"{low_confidence} Arabic OCR regions have low recognition confidence.")
                            info.update(requires_review=True, low_confidence_regions=low_confidence)
                        regions = layout_pages[i]["detections"]
                        if result.raw.get("coordinate_space") == "rendered-page-points":
                            info["display_coordinates"] = True
                            regions = [{**region, "bbox": region["display_bbox"]} for region in regions]
                        rec["lines"] = reconstruct.join_arabic_fragments(rec["lines"], regions)
                    if d["health"] != "absent":
                        rec["figs"] = native.extract_figures(page, settings, [])
                        if rec["lines"] and not d["rotation"]:
                            rec["lines"] = native.recover_running_text(page, rec["lines"], settings.header_band)
                else:
                    info.pop("selected_execution_ids", None)
                    rec["lines"] = native.extract_lines(page)
                    if info["requires_review"]:
                        d["reasons"].append("OCR requires manual review. The source page and any native text were retained.")
            elif decision in ("ocr", "compare") and settings.ocr_enabled and settings.ocr_backend == "tesseract" and installed:
                stage("ocr", page=i + 1, engine="tesseract")
                deskew = d["health"] == "absent" and d["image_coverage"] > 0.6
                if page_language == "und":
                    page_language = ocr.pick_language(page, settings, installed)
                    audit.event("ocr", "page_language_by_trial", doc=doc_id, page=i + 1, lang=page_language)
                with model_audit.details(selected_ocr_engine="tesseract", ocr_engine="tesseract"):
                    olines, info, cached = _ocr_cached(page, cache, cache_identity, fkey, page_language, settings,
                                                   installed, deskew, dpi)
                oconf = sum(l.conf for l in olines) / len(olines) if olines else 0.0
                otext = _text_of(olines)
                d["ocr"] = {"engine": "tesseract", "status": "accepted" if olines else "review_required",
                            "requires_review": not bool(olines), "confidence": round(oconf, 1) if olines else None,
                            "dpi": dpi, "langs": info.get("langs"), "conf": round(oconf, 1), "cached": cached,
                            "lines": len(olines), **{k: info[k] for k in ("rotate", "skew", "osd_script", "retry") if k in info}}
                if decision == "ocr":
                    use_ocr = bool(olines) or d["native_chars"] == 0
                    if not olines and d["health"] != "absent":
                        use_ocr = False; d["reasons"].append("OCR found no text; kept native layer")
                else:   # compare
                    nlines = native.extract_lines(page)
                    sim = fuzz.token_set_ratio(norm_cmp(_text_of(nlines)), norm_cmp(otext)) / 100.0 if olines else 1.0
                    d["compare_similarity"] = round(sim, 3)
                    use_ocr = (not d.get("native_decoding_risk") and bool(olines)
                               and oconf >= 45 and sim < settings.native_vs_ocr_min_similarity)
                    rec["lines"] = nlines
                    audit.event("diagnostics", "compare", doc=doc_id, page=i + 1, similarity=round(sim, 3), ocr_conf=round(oconf, 1), chosen="ocr" if use_ocr else "native")
                if use_ocr:
                    d["ocr"]["selected_execution_ids"] = [] if cached else info.get("selected_execution_ids", [])
                    rec["lines"], rec["source"] = olines, "ocr"
                    if d["health"] != "absent":     # digital page with broken text: keep real figures
                        try: rec["figs"] = native.extract_figures(page, settings, [])
                        except Exception: pass
                        if not (d["rotation"] or info.get("rotate") or info.get("skew")):
                            rec["lines"] = native.recover_running_text(page, olines, settings.header_band)
                elif decision == "ocr":
                    rec["lines"] = native.extract_lines(page)
            else:
                rec["lines"] = native.extract_lines(page)
                if decision in ("ocr", "compare"):
                    reason = ("OCR is disabled." if not settings.ocr_enabled
                              else "The configured OCR engine is unavailable.")
                    d["reasons"].append(reason)
                    d["ocr"] = {"engine": None, "status": "review_required", "requires_review": True}
            if rec["source"] == "native" and rec["lines"] and d["health"] in ("healthy", "suspicious"):
                rec["tables"] = native.extract_tables(page, settings, layout_pages[i]["detections"])
                rec["figs"] = native.extract_figures(page, settings, [t.bbox for t in rec["tables"]], rec["lines"])
            audit.event("extraction", "page", doc=doc_id, page=i + 1, source=rec["source"], health=d["health"], lines=len(rec["lines"]),
                        tables=len(rec["tables"]), figures=len(rec["figs"]), seconds=round(time.time() - tp, 2))
        except Exception as e:
            audit.event("extraction", "page_error", doc=doc_id, page=i + 1, error=str(e), trace=traceback.format_exc()[-600:])
            d["error"] = str(e)
            d["reasons"].append("A page extraction step failed.")
            if not rec["lines"]:
                try:
                    rec["lines"] = native.extract_lines(doc[i])
                except Exception:
                    pass
        raw.append(rec)
        d["selected_ocr_engine"] = settings.ocr_backend
        ocr_info = d.get("ocr", {})
        for engine, key in (("glm-ocr", "glm_ocr"), ("paddleocr", "paddle_ocr"), ("paddleocr-vl", "paddle_vl"),
                            ("tesseract", "tesseract")):
            if ocr_info.get("engine") == engine and ocr_info.get("status") == "accepted":
                counts[key] = counts.get(key, 0) + 1
        counts["failed"] += bool(d.get("ocr_required") and
                                 (d.get("error") or ocr_info.get("status") == "review_required"))
        counts["review"] += bool(ocr_info.get("requires_review") or d.get("error"))
        unread_scan = (d["decision"] == "ocr" and not rec["lines"] and d.get("image_coverage", 0) > 0.5
                       and (ocr_info.get("status") != "accepted" or "error" in d))
        res["page_log"].append({"page": i + 1, "source": rec["source"], "health": d["health"],
                                "native_chars": d["native_chars"], "ocr_required": d.get("ocr_required", False),
                                "ocr": ocr_info, "seconds": round(time.time() - tp, 2), "unread_scan": unread_scan})
        if progress:
            progress(i + 1, n)
        stage("extracted", page=i + 1, elapsed_seconds=round(time.time() - tp, 3),
              ocr_required=bool(d.get("ocr_required")))
    metadata = {key: value for key, value in (doc.metadata or {}).items() if value}
    if router:
        router.close()
    stage("ocr", state="failed" if counts["failed"] else "completed")
    stage("association", state="running")
    for record, detection in zip(raw, layout_pages):
        info = record["diag"].get("ocr", {})
        if not record["diag"].get("rotation") and not any(info.get(k) for k in ("rotate", "skew")):
            region_association.prepare(record, detection["detections"])
    stage("association", state="completed" if settings.doclayout_enabled else "skipped")
    # ---- Layout Reconstruction agent ------------------------------------------------------------------
    stage("layout", state="running")
    ctx = reconstruct.compute_context(raw, settings)
    ctx.lang = lang
    pages: list[PageIR] = []
    running_lines = []
    geometry_packs = ocr.available_langs(settings) if settings.ocr_enabled and (
        any(item["blocks"] for item in raw) or any(
            region["class_name"] in {"image_caption", "table_caption"}
            for evidence in layout_pages for region in evidence["detections"])) else set()
    try:
        for rp in raw:
            stage("layout", page=rp["n"])
            source_page = doc[rp["n"] - 1]
            source_width, source_height = source_page.cropbox.width, source_page.cropbox.height
            rtl = False
            try:
                ls = rp["lines"]
                flow = language_profile(_text_of(ls) + " " + " ".join(block.text for block in rp["blocks"] or []))
                rtl = (flow["script"] in {"arab", "hebrew"} or flow["base_direction"] == "rtl"
                       or (rp["diag"].get("ocr_language_hint") == "ar" and flow["script"] == "und"
                           and rp["source"] == "ocr"))
                display_coordinates = rp["diag"].get("ocr", {}).get("display_coordinates")
                width, height = (rp["w"], rp["h"]) if display_coordinates else (source_width, source_height)
                pg = reconstruct.build_page(rp["n"], width, height, ls, rp["tables"],
                                            [] if display_coordinates else rp["figs"],
                                            ctx, settings, rp["source"], doc_rtl=rtl)
                if display_coordinates:
                    def to_source(box):
                        return tuple(pymupdf.Rect(box) * source_page.derotation_matrix)

                    for block in execution_reports.blocks(pg.blocks):
                        if block.bbox:
                            block.bbox = to_source(block.bbox)
                        block.line_boxes = [to_source(box) for box in block.line_boxes]
                    pg.blocks.extend(rp["figs"])
                if rp["blocks"] is not None:
                    pg.blocks = rp["blocks"] + pg.blocks
                for b in pg.blocks:
                    if b.tag == "figure" and not b.alt:
                        b.alt = figures.describe(b, rp["w"], rp["h"], rp["n"])
            except Exception as e:
                audit.event("layout", "page_error", doc=doc_id, page=rp["n"], error=str(e), trace=traceback.format_exc()[-600:])
                pg = PageIR(n=rp["n"], width=rp["w"], height=rp["h"], source=rp["source"])
                pg.blocks = rp["blocks"] or ([Block(tag="p", text=" ".join(l.text for l in rp["lines"]),
                                                    kind="body", source=rp["source"])] if rp["lines"] else [])
                rp["diag"].setdefault("review_reasons", []).append("Layout reconstruction failed.")
            pg.width, pg.height = rp["w"], rp["h"]
            pg.diag = {**rp["diag"], **pg.diag, "source_width": source_width, "source_height": source_height}
            pg.diag["reading_direction"] = "rtl" if rtl else "ltr"
            if pg.source == "native" and pg.diag.get("health") == "healthy":
                pg.diag["native_numeric_inventory"] = dict(numeric_values(doc[pg.n - 1].get_text()))
            ocr_info = pg.diag.get("ocr", {})
            pg.ocr_engine = (ocr_info.get("engine") or "") if pg.source == "ocr" else ""
            pg.requires_review = bool(ocr_info.get("requires_review") or pg.diag.get("error")
                                      or pg.diag.get("review_reasons"))
            detection = layout_pages[pg.n - 1]
            layout_status, regions = layout.enrich(pdf, pg)
            pg.diag["layout"] = {
                "engine": doclayout.MODEL if detection["status"] == "completed" else
                          ("Docling" if layout_status == "applied" else "reconstruction"),
                "status": detection["status"], "docling_status": layout_status,
                "regions": len(detection["detections"]), **detection,
            }
            positioned = []
            if geometry := ocr_geometry.position_page(source_page, pg, settings, geometry_packs, positioned_lines=positioned):
                pg.diag["layout"]["coordinate_alignment"] = geometry
                audit.event("layout", "ocr_coordinate_alignment", page=pg.n, **geometry)
            detections = detection["detections"]
            can_associate = pg.source == "native" or (
                not source_page.rotation and not (ocr_info.get("rotate") or ocr_info.get("skew")))
            try:
                native_lines = ls if rp["source"] == "native" else native.extract_lines(source_page)
                margin_lines = running.native_candidates(source_page, native_lines)
            except (RuntimeError, ValueError):
                margin_lines = []
            ocr_lines = [line for line in ls if line.source == "ocr"] + positioned if can_associate else []
            running_lines.append(margin_lines + ocr_lines)
            if can_associate and detections:
                region_association.add_visuals(pg, detections)
                region_association.merge_figure_panels(pg, detections)
                region_association.associate(pg, detections, classify=True)
            if detection["status"] == "failed":
                pg.requires_review = True
                pg.diag.setdefault("review_reasons", []).append(detection["error"])
            reconstruct.place_visuals(pg)
            if can_associate and detections:
                ocr_geometry.recover_captions(source_page, pg, margin_lines or ocr_lines, settings, geometry_packs)
                region_association.retain_figure_text(pg, detections)
                if rp["blocks"] is not None:
                    reconstruct.order_positioned_blocks(pg, rtl)
            pseudocode.preserve_pseudocode(pg)
            equations.recognise_formulas(doc[pg.n - 1], pg, settings)
            equations.reconcile_displays(pg)
            if can_associate and detections:
                matched = region_association.associate(pg, detections)
                if execution_ids := detection.get("execution_ids"):
                    model_audit.mark(execution_ids, validation="passed", downstream=[
                        {"consumer": "layout_regions", "reference": f"pdf/page/{pg.n}", "matched_regions": matched}
                    ])
            pg.layout_engine = pg.diag["layout"]["engine"]
            if regions and settings.audit_private_responses:
                report = work_dir / "recognition" / doc_id / f"page-{pg.n:04d}-docling.json"
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(json.dumps(regions, ensure_ascii=False), encoding="utf-8")
            for block in pg.blocks:
                if block.source == "ocr" and not block.ocr_engine:
                    block.ocr_engine = pg.ocr_engine
                    block.recognition_confidence = block.conf
                if block.source == "ocr" and block.ocr_engine == pg.ocr_engine and ocr_info.get("selected_execution_ids"):
                    for item in execution_reports.blocks([block]):
                        item.source_reference.setdefault("execution_ids", ocr_info["selected_execution_ids"])
            pg.diag["confidence"] = page_confidence(pg)
            page_result = res["page_log"][pg.n - 1]
            page_result.update(layout={key: value for key, value in pg.diag["layout"].items() if key != "detections"},
                               review_required=pg.requires_review,
                               confidence=pg.diag["confidence"])
            pages.append(pg)
        running.apply(pages, running_lines, settings.header_band)
        counts["review"] = sum(page.requires_review for page in pages)
    finally:
        layout.close()
    layout_failed = any("Layout reconstruction failed." in page.diag.get("review_reasons", [])
                        or page.diag["layout"]["docling_status"] not in {"applied", "no safe matches", "disabled"}
                        for page in pages)
    stage("layout", state="failed" if layout_failed else "completed")
    dir_ = DocIR(doc_id=doc_id, path=str(pdf), lang=lang, pages=pages,
                 meta={"settings": settings.schema_profile, "pdf_metadata": metadata, "source_sha256": fkey,
                       "selected_ocr_engine": settings.ocr_backend,
                       "ocr_metadata_version": 2})
    stage("validation", state="running")
    result = finish_extraction(pdf, dir_, settings, out_dir, work_dir, audit, res, t0)
    stage("validation", state="completed" if result["ok"] else "failed")
    return result


def detect_layout(doc, settings, work_dir, stage, audit):
    pages = [{"status": "disabled", "detections": []} for _ in doc]
    if not settings.doclayout_enabled:
        stage("rendering", state="skipped")
        stage("detection", state="skipped")
        return pages
    stage("rendering", state="running")
    work_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="doclayout-", dir=work_dir) as scratch:
        folder = Path(scratch)
        rendered = []
        for i in range(len(pages)):
            try:
                rendered.append(doclayout.render(doc[i], folder, settings.doclayout_image_size))
            except (OSError, RuntimeError, ValueError) as error:
                pages[i].update(status="failed", error=f"Page {i + 1} could not be rendered for layout detection: {error}")
        stage("rendering", state="completed" if len(rendered) == len(pages) else "failed")
        stage("detection", state="running", runtime_status="loading")
        with doclayout.Detector(settings, folder) as detector:
            initialization_error = None
            for rendered_page in rendered:
                number = rendered_page["page"]
                result = pages[number - 1]
                if not initialization_error and detector.process is None:
                    try:
                        detector.start()
                    except doclayout.LayoutError as error:
                        initialization_error = str(error)
                stage("detection", page=number, runtime_status="error" if initialization_error else "processing")
                try:
                    if initialization_error:
                        raise doclayout.LayoutError(initialization_error)
                    start = model_audit.cursor()
                    result.update(status="completed", detections=detector.detect(rendered_page),
                                  execution_ids=model_audit.ids_since(start))
                except doclayout.LayoutError as error:
                    result.update(status="failed", error=str(error))
                    audit.event("layout", "doclayout_error", page=number, error=str(error))
                rendered_page["path"].unlink(missing_ok=True)
    stage("detection", state="failed" if any(p["status"] == "failed" for p in pages) else "completed")
    return pages


def refresh_layout(pdf, saved_ir, settings, out_dir, work_dir, audit, result, progress, processing, *,
                   force_detection=False):
    previous = result.get("execution_evidence", {})
    private = work_dir / "private-model-responses" if settings.audit_private_responses else None
    with model_audit.scope(previous.get("document_id", saved_ir.doc_id),
                           saved_ir.meta.get("source_sha256"), private) as trace:
        trace["executions"].extend({**row, "historical": True} for row in previous.get("executions", []))
        return _refresh_layout(pdf, saved_ir, settings, out_dir, work_dir, audit, result, progress, processing,
                               force_detection)


def _refresh_layout(pdf, saved_ir, settings, out_dir, work_dir, audit, result, progress, processing, force_detection):
    """Reconcile only the saved OCR attempt for the same source file."""
    import pymupdf

    start = time.time()
    if saved_ir.meta.get("source_sha256") != file_key(pdf):
        raise ValueError("The source PDF changed. Run a new extraction.")
    packs = ocr.available_langs(settings)
    if not packs and not settings.doclayout_enabled:
        raise RuntimeError("Tesseract is unavailable for layout refresh.")
    result["layout_refresh"] = True
    with pymupdf.open(pdf) as source:
        if len(source) != len(saved_ir.pages):
            raise ValueError("The PDF and the saved extraction have different page counts.")
        refresh = settings.doclayout_enabled and (force_detection or any(
            page.diag.get("layout", {}).get("status") != "completed" for page in saved_ir.pages))
        detections = detect_layout(source, settings, work_dir, lambda name, **detail: processing(
            {"stage": name, **detail}), audit) if refresh else None
        processing({"stage": "layout", "state": "running"})
        running_lines = []
        for page in saved_ir.pages:
            processing({"stage": "layout", "page": page.n})
            original = source[page.n - 1]
            page.diag.update(source_width=original.cropbox.width, source_height=original.cropbox.height)
            current = page.diag.setdefault("layout", {})
            if detections:
                evidence = detections[page.n - 1]
                current.update(evidence, regions=len(evidence["detections"]))
                if evidence["status"] == "completed":
                    page.layout_engine = current["engine"] = doclayout.MODEL
                elif evidence.get("error"):
                    page.requires_review = True
                    page.diag.setdefault("review_reasons", []).append(evidence["error"])
            positioned = []
            if geometry := ocr_geometry.position_page(original, page, settings, packs, positioned_lines=positioned):
                current["coordinate_alignment"] = geometry
                audit.event("layout", "ocr_coordinate_alignment", page=page.n, **geometry)
            reconstruct.place_visuals(page)
            pseudocode.preserve_pseudocode(page)
            ocr_info = page.diag.get("ocr", {})
            aligned = page.source == "native" or (
                not original.rotation and not (ocr_info.get("rotate") or ocr_info.get("skew")))
            try:
                native_lines = running.native_candidates(original, native.extract_lines(original))
            except (RuntimeError, ValueError):
                native_lines = []
            lines = native_lines + (positioned if aligned else [])
            running_lines.append(lines)
            if current.get("detections") and aligned:
                region_association.merge_figure_panels(page, current["detections"])
                region_association.associate(page, current["detections"], classify=True)
                ocr_geometry.recover_captions(original, page, native_lines or positioned, settings, packs)
                region_association.retain_figure_text(page, current["detections"])
                flow = language_profile(" ".join(block.text for block in page.blocks))
                rtl = flow["script"] in {"arab", "hebrew"} or flow["base_direction"] == "rtl"
                page.diag["reading_direction"] = "rtl" if rtl else "ltr"
                reconstruct.order_positioned_blocks(page, rtl)
            equations.recognise_formulas(original, page, settings)
            equations.reconcile_displays(page)
            if current.get("detections") and aligned:
                region_association.associate(page, current["detections"])
            result["page_log"][page.n - 1]["layout"] = {key: value for key, value in current.items()
                                                         if key != "detections"}
            progress(page.n, len(saved_ir.pages))
        running.apply(saved_ir.pages, running_lines, settings.header_band)
    processing({"stage": "layout", "state": "completed"})
    processing({"stage": "validation", "state": "running"})
    result = finish_extraction(pdf, saved_ir, settings, out_dir, work_dir, audit, result, start)
    processing({"stage": "validation", "state": "completed" if result["ok"] else "failed"})
    return result


def finish_extraction(pdf, dir_, settings, out_dir, work_dir, audit, res, t0):
    import pymupdf

    doc_id, pages = dir_.doc_id, dir_.pages
    kinds.assign_kinds(dir_)
    # Running headers and final block classification can change the geometric anchors.
    for page in pages:
        equations.reconcile_displays(page)
    profile = decorate_document(dir_)
    dir_.meta["execution_trace_id"] = model_audit.current()["trace_id"]
    execution_reports.anchor_ir(dir_, model_audit.current()["trace_id"])
    gate = quality_gate(dir_)
    res["processing_counts"]["review"] = len(gate["review_pages"])
    for page in pages:
        res["page_log"][page.n - 1].update(review_required=page.n in gate["review_pages"], language=page.language)
    res.update(document_profile=profile, multilingual_validation=gate)
    profile_dir = work_dir / "profiles" / doc_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    write_report(profile_dir / "document_profile.json", profile)
    write_report(profile_dir / "layout_fidelity_report.json", layout_fidelity(dir_))
    dir_ = ir_from_dict(dir_.to_dict())
    # ---- XML Generation + QA agents -----------------------------------------------------------------------
    assets = {}
    locations = {}
    data = to_xml(dir_, settings, locations=locations, assets=assets)
    val = validate_xml(data, len(pages), settings)
    if not val["well_formed"] or val["issues"]:
        audit.event("xml", "invalid", doc=doc_id, issues=val["issues"])
        res.update(ok=False, validation=val, error="Generated XML failed validation.")
        gate.update(status="processing_failed", xml={"status": "invalid", "issues": val["issues"]})
        write_report(profile_dir / "multilingual_validation_report.json", gate)
        write_report(profile_dir / "xml_validation_report.json", val)
        save_execution_reports(work_dir, doc_id, res, dir_)
        return res
    gate["xml"] = verify_unicode(data, dir_, locations)
    if gate["xml"]["unconfirmed_nodes"] and gate["status"] == "structural_validation_passed":
        gate["status"] = "validation_partial"
    write_report(profile_dir / "multilingual_validation_report.json", gate)
    if assets:
        with pymupdf.open(pdf) as source_pdf:
            for name, (page, box) in assets.items():
                asset_path = out_dir / name
                asset_path.parent.mkdir(parents=True, exist_ok=True)
                original = source_pdf[page - 1]
                clip = pymupdf.Rect(box) * original.rotation_matrix
                original.get_pixmap(dpi=180, clip=clip, alpha=False).save(asset_path)
    _write_xml(out_dir, pdf, data)
    complete = compare_content(data, dir_, locations, sample=settings.schema_profile == "sample")
    write_report(profile_dir / "xml_validation_report.json", {**val, "unicode": gate["xml"]})
    write_report(profile_dir / "content_completeness_report.json",
                 {key: value for key, value in complete.items() if key != "formatting"})
    write_report(profile_dir / "formatting_report.json", complete["formatting"])
    write_report(profile_dir / "image_asset_report.json", inspect_assets(data, out_dir))
    save_execution_reports(work_dir, doc_id, res, dir_, data, locations)
    ir_dir = work_dir / "ir"; ir_dir.mkdir(parents=True, exist_ok=True)
    (ir_dir / f"{doc_id}.json").write_text(json.dumps(dir_.to_dict(), ensure_ascii=False), encoding="utf-8")
    q = doc_quality(dir_, val)
    res.update(ok=val["well_formed"] and not val["issues"], validation=val, quality=q, lang=dir_.lang, seconds=round(time.time() - t0, 2),
               ocr_pages=sum(1 for p in pages if p.source == "ocr"), source_counts={s: sum(1 for p in pages if p.source == s) for s in ("native", "ocr")})
    audit.event("xml", "written", doc=doc_id, seconds=res["seconds"], issues=val["issues"], ocr_pages=res["ocr_pages"])
    return res


def save_execution_reports(work_dir, doc_id, result, ir=None, data=None, locations=None):
    state = model_audit.current()
    result["execution_evidence"] = {key: value for key, value in state.items() if key != "private"}
    folder = work_dir / "execution-audit" / doc_id
    folder.mkdir(parents=True, exist_ok=True)
    for name, report in execution_reports.reports(ir, result["execution_evidence"], data, locations).items():
        write_report(folder / name, report)


def _write_xml(out_dir: Path, pdf: Path, data: bytes):
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / (xml_name(pdf) + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, out_dir / xml_name(pdf))


def _job(args):
    pdf, settings_d, out_dir, work_dir, audit_path, deadline = args
    s = Settings(**settings_d)
    audit = AuditLog(audit_path)
    try:
        return process_pdf(Path(pdf), s, Path(out_dir), Path(work_dir), audit, deadline)
    except Exception as e:
        audit.event("orchestrator", "document_failed", doc=Path(pdf).stem, error=str(e), trace=traceback.format_exc()[-800:])
        try:
            _write_xml(Path(out_dir), Path(pdf), b'<?xml version="1.0" encoding="UTF-8"?>\n<document lang="und"/>\n')
        except Exception:
            pass
        return {"doc_id": Path(pdf).stem, "file": Path(pdf).name, "ok": False, "error": str(e), "pages": 0}


def run_pdfs(pdfs: list[Path], settings: Settings, out_dir: Path, work_dir: Path, audit: AuditLog, t_start: float) -> list[dict]:
    workers = settings.workers or (1 if settings.ocr_backend != "tesseract" or settings.docling_enabled or settings.doclayout_enabled
                                   else max(1, min((os.cpu_count() or 2) - 1, 6, len(pdfs) or 1)))
    deadline = t_start + 0.75 * settings.time_budget_s            # after this point OCR drops to fast mode
    audit.event("orchestrator", "batch_start", documents=len(pdfs), workers=workers, soft_deadline_s=round(0.75 * settings.time_budget_s))
    jobs = [(str(p), settings.to_dict(), str(out_dir), str(work_dir), str(audit.path) if audit.path else None, deadline) for p in pdfs]
    results = []
    if workers <= 1 or len(pdfs) <= 1:
        for j in jobs:
            results.append(_job(j))
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_job, j): j for j in jobs}
            for f in as_completed(futs):
                try:
                    results.append(f.result())
                except Exception as e:
                    results.append({"doc_id": Path(futs[f][0]).stem, "ok": False, "error": str(e)})
    results.sort(key=lambda r: r["doc_id"])
    return results

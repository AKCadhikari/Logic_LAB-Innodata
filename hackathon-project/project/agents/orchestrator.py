"""Central orchestrator. The "agents" are deterministic stage workers sharing one structured state; every
delegation and decision is written to the audit log.  No LLM is required (and none is used) - see docs for why."""
from __future__ import annotations
import json, os, time, traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from config.settings import Settings
from diagnostics.page_diagnostics import analyze_page
from ingestion.discover import describe
from layout.model import PageIR, DocIR, Line, Block, Span
from layout import native, reconstruct, kinds, figures
from ocr import engine as ocr
from quality.qa import page_confidence, doc_quality
from utils.cache import Cache, file_key
from utils.logs import AuditLog
from utils.naming import xml_name
from utils.textutil import detect_lang, norm_cmp, is_rtl
from xml_engine.serializer import to_xml
from xml_engine.validate import validate_xml

_INSTALLED: set[str] | None = None


def _installed(settings):
    global _INSTALLED
    if _INSTALLED is None:
        _INSTALLED = ocr.available_langs(settings)
    return _INSTALLED


def _lines_to_json(lines):
    return [{**l.__dict__, "spans": [s.__dict__ for s in l.spans]} for l in lines]


def _lines_from_json(arr):
    return [Line(**{**a, "bbox": tuple(a["bbox"]), "spans": [Span(**s) for s in a.get("spans", [])]}) for a in arr]


def _ocr_cached(page, cache, fkey, lang, settings, deskew, dpi):
    key = (fkey, f"p{page.number + 1}", f"{dpi}_{lang}_{int(deskew)}")
    hit = cache.get(*key)
    if hit:
        return _lines_from_json(hit["lines"]), hit["info"], True
    lines, info = ocr.ocr_page(page, lang, dpi, settings, _installed(settings), orient=deskew, deskew=deskew)
    cache.put({"lines": _lines_to_json(lines), "info": info}, *key)
    return lines, info, False


def _text_of(lines) -> str:
    return " ".join(l.text for l in lines)


def process_pdf(pdf: Path, settings: Settings, out_dir: Path, work_dir: Path, audit: AuditLog, deadline: float | None = None) -> dict:
    import pymupdf
    from rapidfuzz import fuzz
    t0 = time.time()
    doc_id = pdf.stem
    res = {"doc_id": doc_id, "file": pdf.name, "ok": False, "pages": 0, "error": "", "page_log": []}
    cache = Cache(work_dir / settings.cache_dir, settings.use_cache)
    fkey = file_key(pdf)
    try:
        doc = pymupdf.open(str(pdf))
    except Exception as e:
        audit.event("ingestion", "unreadable", doc=doc_id, error=str(e))
        res["error"] = f"cannot open: {e}"
        _write_xml(out_dir, pdf, b'<?xml version="1.0" encoding="UTF-8"?>\n<document lang="und"/>\n')
        return res
    n = doc.page_count
    res["pages"] = n
    audit.event("analysis", "start", doc=doc_id, pages=n)
    # ---- Document Analysis agent: per-page diagnostics ------------------------------------------
    diags = []
    for i in range(n):
        try:
            diags.append(analyze_page(doc[i], settings))
        except Exception as e:
            diags.append({"page": i + 1, "health": "error", "decision": "native", "reasons": [str(e)], "native_chars": 0, "width": 595, "height": 842})
            audit.event("analysis", "page_error", doc=doc_id, page=i + 1, error=str(e))
    usable = " ".join(doc[i].get_text()[:3000] for i in range(n) if diags[i]["health"] in ("healthy", "suspicious"))[:24000]
    lang = detect_lang(usable)
    audit.event("analysis", "doc_language", doc=doc_id, lang=lang, healthy_pages=sum(d["health"] == "healthy" for d in diags),
                decisions={k: sum(d["decision"] == k for d in diags) for k in ("native", "compare", "ocr")})
    # ---- OCR & Extraction agent ---------------------------------------------------------------
    raw = []
    for i in range(n):
        d = diags[i]; tp = time.time()
        fast = bool(deadline and time.time() > deadline)
        dpi = settings.ocr_dpi_fast if fast else settings.ocr_dpi
        rec = {"n": i + 1, "w": d["width"], "h": d["height"], "lines": [], "tables": [], "figs": [], "source": "native", "diag": d}
        try:
            page = doc[i]
            decision = d["decision"]
            if fast and decision == "compare":
                decision = "native"; d["reasons"].append("fast mode: compare skipped")
            can_ocr = settings.ocr_enabled
            if decision in ("ocr", "compare") and can_ocr:
                deskew = d["health"] == "absent" and d["image_coverage"] > 0.6
                if lang == "und":                   # undetermined doc: pick the OCR language pack by trial on this page
                    lang = ocr.pick_language(page, settings, _installed(settings))
                    audit.event("ocr", "doc_language_by_trial", doc=doc_id, page=i + 1, lang=lang)
                olines, info, cached = _ocr_cached(page, cache, fkey, lang, settings, deskew, dpi)
                if lang == "und" and olines:
                    lang = detect_lang(_text_of(olines)) or "und"
                oconf = sum(l.conf for l in olines) / len(olines) if olines else 0.0
                otext = _text_of(olines)
                d["ocr"] = {"dpi": dpi, "langs": info.get("langs"), "conf": round(oconf, 1), "cached": cached, "lines": len(olines), **{k: info[k] for k in ("rotate", "skew", "osd_script", "retry") if k in info}}
                if decision == "ocr":
                    use_ocr = bool(olines) or d["native_chars"] == 0
                    if not olines and d["health"] != "absent":
                        use_ocr = False; d["reasons"].append("OCR found no text; kept native layer")
                else:   # compare
                    nlines = native.extract_lines(page)
                    sim = fuzz.token_set_ratio(norm_cmp(_text_of(nlines)), norm_cmp(otext)) / 100.0 if olines else 1.0
                    d["compare_similarity"] = round(sim, 3)
                    use_ocr = bool(olines) and oconf >= 45 and sim < settings.native_vs_ocr_min_similarity
                    rec["lines"] = nlines
                    audit.event("diagnostics", "compare", doc=doc_id, page=i + 1, similarity=round(sim, 3), ocr_conf=round(oconf, 1), chosen="ocr" if use_ocr else "native")
                if use_ocr:
                    rec["lines"], rec["source"] = olines, "ocr"
                    if d["health"] != "absent":     # digital page with broken text: keep real figures
                        try: rec["figs"] = native.extract_figures(page, settings, [])
                        except Exception: pass
                elif decision == "ocr":
                    rec["lines"] = native.extract_lines(page)
            else:
                rec["lines"] = native.extract_lines(page)
            if rec["source"] == "native" and rec["lines"] and d["health"] in ("healthy", "suspicious"):
                rec["tables"] = native.extract_tables(page, settings)
                rec["figs"] = native.extract_figures(page, settings, [t.bbox for t in rec["tables"]], rec["lines"])
            audit.event("extraction", "page", doc=doc_id, page=i + 1, source=rec["source"], health=d["health"], lines=len(rec["lines"]),
                        tables=len(rec["tables"]), figures=len(rec["figs"]), seconds=round(time.time() - tp, 2))
        except Exception as e:
            audit.event("extraction", "page_error", doc=doc_id, page=i + 1, error=str(e), trace=traceback.format_exc()[-600:])
            d["error"] = str(e)
        raw.append(rec)
        res["page_log"].append({"page": i + 1, "source": rec["source"], "health": d["health"], "seconds": round(time.time() - tp, 2)})
    doc.close()
    # ---- Layout Reconstruction agent ------------------------------------------------------------------
    ctx = reconstruct.compute_context(raw, settings)
    ctx.lang = lang
    pages: list[PageIR] = []
    for rp in raw:
        try:
            ls = rp["lines"]
            rtl = bool(ls) and sum(l.rtl for l in ls) / len(ls) > 0.5
            pg = reconstruct.build_page(rp["n"], rp["w"], rp["h"], ls, rp["tables"], rp["figs"], ctx, settings, rp["source"], doc_rtl=rtl)
            for b in pg.blocks:
                if b.tag == "figure" and not b.alt:
                    b.alt = figures.describe(b, rp["w"], rp["h"], rp["n"])
        except Exception as e:
            audit.event("layout", "page_error", doc=doc_id, page=rp["n"], error=str(e), trace=traceback.format_exc()[-600:])
            pg = PageIR(n=rp["n"], width=rp["w"], height=rp["h"], source=rp["source"])
            pg.blocks = [Block(tag="p", text=" ".join(l.text for l in rp["lines"]), kind="body")] if rp["lines"] else []
        pg.diag = rp["diag"]
        pg.diag["confidence"] = page_confidence(pg)
        pages.append(pg)
    dir_ = DocIR(doc_id=doc_id, path=str(pdf), lang=lang, pages=pages, meta={"settings": settings.schema_profile})
    kinds.assign_kinds(dir_)
    # ---- XML Generation + QA agents -----------------------------------------------------------------------
    data = to_xml(dir_, settings)
    val = validate_xml(data, n, settings)
    if not val["well_formed"]:                                   # never emit invalid XML: fall back to plain paragraphs
        audit.event("xml", "fallback_plain", doc=doc_id, issues=val["issues"])
        for pg in dir_.pages:
            pg.blocks = [Block(tag="p", text="\n".join(b.text for b in pg.blocks if b.text), kind="body")]
        data = to_xml(dir_, settings); val = validate_xml(data, n, settings)
    _write_xml(out_dir, pdf, data)
    ir_dir = work_dir / "ir"; ir_dir.mkdir(parents=True, exist_ok=True)
    (ir_dir / f"{doc_id}.json").write_text(json.dumps(dir_.to_dict(), ensure_ascii=False), encoding="utf-8")
    q = doc_quality(dir_, val)
    res.update(ok=val["well_formed"], validation=val, quality=q, lang=lang, seconds=round(time.time() - t0, 2),
               ocr_pages=sum(1 for p in pages if p.source == "ocr"), source_counts={s: sum(1 for p in pages if p.source == s) for s in ("native", "ocr")})
    audit.event("xml", "written", doc=doc_id, seconds=res["seconds"], issues=val["issues"], ocr_pages=res["ocr_pages"])
    return res


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
    workers = settings.workers or max(1, min((os.cpu_count() or 2) - 1, 6, len(pdfs) or 1))
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

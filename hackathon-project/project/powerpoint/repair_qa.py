"""Publish job evidence and keep planned, saved and visibly verified edits distinct."""
from collections import Counter
from hashlib import sha256
from html import escape
from pathlib import Path

import pymupdf

from layout.math_structure import equation_catalog
from reporting.exports import write_report


def write_qa(ir, target: Path, report):
    folder = target.parent / f"{target.stem}_reports"
    folder.mkdir(parents=True, exist_ok=True)
    records = report["records"]
    crops = []
    equations = equation_catalog(ir)
    try:
        with pymupdf.open(ir.path) as pdf:
            for evidence in [*equations["equations"], *(row.get("source") or {} for row in records)]:
                box, page = evidence.get("bbox"), evidence.get("page")
                if not box or not isinstance(page, int) or not 1 <= page <= len(pdf):
                    continue
                try:
                    sheet = pdf[page - 1]
                    crop = (pymupdf.Rect(box) * sheet.rotation_matrix) & sheet.rect
                    if crop.is_empty:
                        continue
                    key = sha256(str((page, tuple(crop))).encode()).hexdigest()[:16]
                    name = f"evidence/page-{page}-{key}.png"
                    path = folder / name
                    if name not in crops:
                        path.parent.mkdir(exist_ok=True)
                        dpi = max(1, min(380, int(3000 * 72 / max(crop.width, crop.height))))
                        sheet.get_pixmap(dpi=dpi, clip=crop, alpha=False).save(path)
                        crops.append(name)
                    evidence["visual_crop"] = name
                except (RuntimeError, ValueError, OSError):
                    continue
    except (RuntimeError, ValueError, OSError):
        pass
    unresolved = [row for row in records if row["action"] == "review" or
                  row["validation_status"] == "visible_write_back_failure" or
                  row.get("render_verification") in {"requires_visual_equation_check", "no_visual_change_in_renderer",
                                                      "unconfirmed", "visual_check_pending"}]
    linked = sum(bool((row.get("source") or {}).get("page")) for row in records)
    applied_types = Counter(row["change_type"] for row in report["applied"])
    render = report["presentation_validation"]["render"]
    summary = {"slides": report["slides"], "object_inventory": report["object_inventory"],
               "assessment_rows": len(records), "review_candidates_by_category": dict(Counter(row["change_type"] for row in records)),
               "applied": len(report["applied"]), "package_verified": len(report["applied"]),
               "visible_verified": report["presentation_validation"]["visible_verified"],
               "source_equations_detected": len(equations["equations"]),
               "source_equations_structured": sum(item["mathml"] is not None for item in equations["equations"]),
               "source_equations_verified": sum(item["status"] != "review_required" and item["mathml"] is not None
                                                for item in equations["equations"]),
               "editable_equations_rebuilt": sum(bool(row.get("native_equation_rebuilt")) for row in report["applied"]),
               "editable_equations_from_pictures": sum(bool(row.get("native_equation_generated")) for row in report["applied"]),
               "equation_image_fallbacks": sum(row["change_type"] == "equation_image" and
                                               not row.get("native_equation_generated") for row in report["applied"]),
               "missing_bullets_restored": applied_types["missing_bullet"],
               "unsupported_text_removed": applied_types["extra_text"],
               "unexpected_saved_package_changes": len(report["presentation_validation"]["structure"]["unexpected_changes"]),
               "unexpected_unchanged_slides_in_renderer": render.get("unchanged_slide_render_differences"),
               "reverted_visible_failures": sum(row.get("render_verification") == "reverted" for row in records),
               "processing_seconds": report["repair_seconds"],
               "unresolved": len(unresolved), "source_linked": linked,
               "source_evidence_coverage": round(linked / len(records), 4) if records else None}
    items = {"source_ir.json": ir.to_dict(), "detected_errors.json": {"items": records},
             "applied_corrections.json": {"items": report["applied"]}, "unresolved_errors.json": {"items": unresolved},
             "equations.json": equations, "slide_source_alignment.json": {"slides": report["slide_source_alignment"]},
             "pptx_validation.json": {"summary": summary, **report["presentation_validation"]},
             "processing_log.json": {"seconds": report["repair_seconds"],
                                     "model_executions": report["verification_executions"],
                                     "advisory": report["advisory"],
                                     "stages": ["source_index", "object_inspection", "correction_plan", "package_write",
                                                "saved_package_validation", "local_render_validation"]}}
    for name, value in items.items():
        write_report(folder / name, value)
    headings = ("Slide", "Object", "Category", "Original", "Proposed or applied", "PDF evidence", "Confidence",
                "Application", "Validation", "Render check")
    rows = []
    for row in records:
        proof = row.get("source") or {}
        source = f"Page {proof['page']} · {proof.get('text', '')}" if proof.get("page") else row.get("reason", "")
        if proof.get("bbox"):
            source += " · PDF box " + ", ".join(f"{value:.1f}" for value in proof["bbox"])
        proof_html = escape(source)
        if proof.get("visual_crop"):
            proof_html += f' <a href="{escape(proof["visual_crop"], quote=True)}">View source crop</a>'
        values = (row.get("slide"), row.get("shape"), row["change_type"], row["old"], row.get("proposed_content") or row["new"],
                  source, row.get("confidence"), row["action"], row["validation_status"], row.get("render_verification"))
        cells = [escape(str(value)) if value is not None else "" for value in values]
        cells[5] = proof_html
        rows.append("<tr>" + "".join(f"<td>{value}</td>" for value in cells) + "</tr>")
    (folder / "quality_report.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>PowerPoint correction report</title>'
        '<style>body{font:15px system-ui;margin:2rem}td,th{padding:.55rem;border:1px solid #ccc;'
        'vertical-align:top;text-align:left;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%}</style>'
        '<h1>PowerPoint correction report</h1>'
        f'<p>{summary["slides"]} slides, {summary["applied"]} saved corrections, '
        f'{summary["visible_verified"]} visibly confirmed, {len(unresolved)} items for review.</p>'
        f'<p>{summary["source_equations_detected"]} source equations detected, '
        f'{summary["source_equations_verified"]} verified transcriptions, {summary["editable_equations_rebuilt"]} '
        f'editable equations rebuilt, {summary["equation_image_fallbacks"]} image replacements.</p>'
        '<table><thead><tr>' + "".join(f"<th>{name}</th>" for name in headings) +
        '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></html>', encoding="utf-8")
    return [f"{folder.name}/{name}" for name in [*items, "quality_report.html", *crops]]

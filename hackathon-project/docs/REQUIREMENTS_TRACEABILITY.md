# Requirements traceability

| # | Requirement (source) | Module | Tests / evidence | Status |
|---|---|---|---|---|
| R1 | One XML per PDF, named as source with `.xml` (brief) | `utils/naming.py`, `agents/orchestrator.py` | `test_naming_rules`, benchmark 13/13 | Done |
| R2 | Detect scanned / corrupted text layers, never trust native text (brief, master prompt) | `diagnostics/page_diagnostics.py` | `test_diagnostics_decisions`, `test_corrupted_text_layer_detected` | Done |
| R3 | OCR for 10 languages, preserve scripts | `ocr/engine.py` | `test_ocr_fallback_on_image_only_page`; corpus run | Done (Arabic weaker) |
| R4 | HTML element vocabulary, one `<page n>` per page, `lang` on root | `xml_engine/serializer.py`, `validate.py` | `test_digital_pdf_to_valid_xml`, `test_validate_flags_bad_numbering` | Done |
| R5 | Reading order, multi-column, RTL | `layout/reconstruct.py` | `test_two_column_reading_order` | Done (heuristic) |
| R6 | Headings, lists, footnotes, captions, header/footer | `layout/reconstruct.py` | `test_digital_pdf_to_valid_xml` | Done |
| R7 | Real tables with row/colspan | `layout/native.py`, serializer | `test_table_structure_spans_and_header` | Digital pages only |
| R8 | Figures with non-duplicating alt text, figcaption | `layout/native.py`, `figures.py` | `test_figure_alt_and_caption_and_lang` | Heuristic alt text |
| R9 | `del`/`ins`/`sup`/`sub`/`a`/`strong`/`em`; stamp/handwritten classes | `layout/native.py`, serializer | `test_inline_formatting` | `del/sup/sub/a/strong/em` done; handwriting/stamps/`ins` not detected |
| R10 | Per-element `lang` when different | `layout/kinds.py` | `test_figure_alt_and_caption_and_lang` | Done (script-level, long Latin blocks) |
| R11 | `kind` attribute (bonus) | `layout/kinds.py` | benchmark titles | Inferred vocabulary |
| R12 | Schema discrepancy documented + configurable | serializer profiles | `test_profile_and_lang_format`; TECHNICAL_APPROACH s4 | Done |
| R13 | Version comparison engine + machine-readable report | `comparison/differ.py` | `test_compare_documents_reports_added_deleted_modified` | Done |
| R14 | PPTX ingestion, targeted correction, formatting preserved | `powerpoint/amend.py` | `test_apply_text_edit_preserves_run_formatting`, `test_end_to_end_deck_repair_and_report` | Done |
| R15 | Change record: id, locations, type, old/new, confidence, action | `powerpoint/amend.py` | same test | Done |
| R16 | Output decks reopen; originals never overwritten | `powerpoint/amend.py` | same test; benchmark 13/13 | Done |
| R17 | Unattended, failure isolation, checkpoints/cache, time budget | `agents/orchestrator.py`, `utils/cache.py` | `test_unreadable_pdf_does_not_stop_batch` | Done (resume flag not exposed) |
| R18 | 2-hour Phase 3 runtime | orchestrator | docs/BENCHMARKS.md | Indicated, not proven on target HW |
| R19 | QA reports, heuristic confidence (not accuracy) | `quality/`, `reporting/` | run_report.json | Done |
| R20 | Dashboard | `ui/` | manual + API smoke test | Done (Flask, not React/FastAPI) |
| R21 | Offline / confidential data | whole system | no network calls at run time | Done |
| R22 | Submission files: README, PROJECT_DETAILS, TECHNICAL_APPROACH, `project-demo.mp4`, phase folders | repo root | `project-demo.mp4` **not created** | Partly - video is yours to record |
| R23 | Windows 11 target | - | not tested here | **Unverified** |

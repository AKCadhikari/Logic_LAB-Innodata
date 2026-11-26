# DocuSync AI — project details

- **Competition:** Innodata AI Catalyst 2026, Internal Track
- **Entry point:** `project/main.py`
- **Runtime:** Python 3.11, CPU-first local processing.

## Problem

Convert PDFs with native, degraded or scanned text into structured XML, then proofread supplied PowerPoint presentations against source PDF evidence. Keep uncertain content visible for review rather than silently inventing corrections.

## Objectives

- Process variable PDF names and page layouts without document-specific output templates.
- Detect page regions with DocLayout-YOLO, recognize text with a separately selected OCR engine when needed, and preserve page geometry, language and reading order in a shared document representation.
- Emit XML that is structurally checked before it is reported as successful.
- Correct supported PPTX text defects using source evidence while preserving slide structure and formatting where possible.
- Keep confidential files and model inference local; retain audit and validation reports.

## Main features

1. **PDF processing:** validate input, inspect each page, keep healthy native text or route image pages to OCR, run DocLayout-YOLO, associate regions with text, reconstruct structure and validate XML.
2. **OCR choices:** Automatic, GLM-OCR, PaddleOCR-VL through the local Ollama adapter, and PaddleOCR Python. The layout detector is separate from OCR. A configured OCR backend is not treated as available unless its runtime responds.
3. **XML:** page hierarchy, semantic block kinds, reading order, source geometry, language/direction metadata, and linked image assets where available. The workspace uses the engineering sample profile; CLI supports `brief`, `sample`, and `example` profiles.
4. **PPTX proofreading:** upload a deck against a processed source PDF, match slide text to PDF evidence, apply supported corrections, report uncertain items, validate the resulting PPTX and save it beside the phase output.
5. **Document review:** PDF preview, extracted text, reading order, detected regions, metadata, validation and export history.
6. **Local workflow:** select Phase 1 or Phase 2 in the workspace. PDF uploads and proofreading decks are copied to that phase's input folder. Successful PDF jobs publish versioned XML; PPTX repair publishes a versioned corrected deck. Existing exports are retained.
7. **Batch workflow:** `project/main.py` discovers PDFs and presentations in the selected phase input folder and writes export bundles to the matching output folder.

## User workflow

1. Start the local workspace with `Start_DocuSync_AI.bat` or `project/ui/server.py`.
2. Select a submission phase, choose a PDF and select the OCR mode. Processing stages and model results come from actual worker events.
3. Review the extraction and submit metadata when appropriate. XML is validated and written to the selected phase output directory automatically.
4. In **PowerPoint & changes**, upload the supplied deck and choose source repair, or compare two processed PDF versions to synchronize an existing deck. The corrected PPTX and change report are saved in a new output folder.
5. Inspect output validation and use the download links or the phase output folders.

## Expected results

- One validated XML per successfully processed PDF.
- A corrected PPTX and evidence/change report for each supported proofreading run.
- Versioned output folders so repeated attempts do not overwrite earlier results.
- Per-page diagnostics, OCR/layout usage, confidence and review warnings.

These are expected product behaviors, not a claim that the full competition data set has been processed. At this audit, the local engineering sample consists of one 12-page PDF and one paired error PPTX; full Phase 1 coverage requires the remaining supplied files.

Measured current-run results and comparisons with the local engineering XML/PPTX are in [local sample verification](docs/LOCAL_SAMPLE_VERIFICATION.md).

## Recorded historical benchmark

`docs/BENCHMARKS.md` and `docs/OFFICIAL_SAMPLE_VALIDATION.md` contain previously recorded project measurements and sample comparisons. They were not rerun as part of this compliance pass and should not be presented as current coverage or as results from the currently available single PDF/deck pair.

## Limitations

- OCR quality and table/equation recovery vary with scan quality and the selected recognizer.
- Layout geometry may need review on skewed or rotated scans.
- PPTX edits are evidence-based and conservative; ambiguous changes are flagged for human review.
- Full accuracy, CPU performance and memory use on the target machine need measurement across the complete competition set.

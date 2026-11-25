# DocuSync AI (working title)

A configurable document-processing pipeline for the Innodata AI Catalyst 2026 Employee Track.

## Launch the DocuSync AI workspace

Double-click [Start_DocuSync_AI.bat](Start_DocuSync_AI.bat) in this folder. It opens the browser workspace from
the repository's `hackathon-project` folder. The shared launcher starts or reuses Ollama, checks and warms
the configured models, and opens DocuSync AI when the workspace is ready. The **AI startup** panel shows
installation, residency, inference checks, and model errors.

The workspace uses `hackathon-project/.venv`, the optional `hackathon-project/.env`, and
`hackathon-project/logs/startup.log`. See the [workspace setup, shutdown, and troubleshooting instructions](../../hackathon-project/README.md).

## Current status

The command-line entry point below runs the Phase 1 starter baseline:
- Reads every `*.pdf` in an input folder.
- Extracts native text blocks page by page using PyMuPDF.
- Writes well-formed XML with one `<page n="...">` element per PDF page and paragraph blocks.
- Reports page, empty-page, block, and character counts.

This baseline does **not** yet implement OCR, layout classification, tables, figures, handwritten annotations, change detection, or PowerPoint amendment generation. Do not present it as a complete solution.

## Setup (Windows 11 PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r project\requirements.txt
```

## Run

```powershell
python project\main.py --input phase-1\input --output phase-1\output
```

## Next engineering milestones

1. Add per-page diagnostics: native text density, image coverage, rotation, and language/script hints.
2. Add OCR fallback only for pages with absent or suspicious native text.
3. Add layout-aware reconstruction for headings, paragraphs, lists, tables, figures, captions, headers, and footers.
4. Add structural and text QA against the supplied example reference.
5. Implement PPTX amendment comparison and propagation as a separate module.
6. Benchmark quality and runtime on the complete Phase 1 corpus.

## Important

The challenge brief requires the HTML element vocabulary and `page` numbering with `@n`. The supplied example XML uses a different, richer legacy schema (including `page @num` and custom tags). Confirm this discrepancy with the organisers before finalizing the serializer. This starter follows the written challenge brief's `page @n` convention.

# DocuSync AI starter

A Phase 1 PDF-to-XML baseline for the Innodata AI Catalyst 2026 Employee Track. Start here if you want to run `project/main.py` in this folder. For OCR, document layout analysis, and PowerPoint repair, use the [full workspace setup](../../README.md#get-started-on-windows).

## Launch the DocuSync AI workspace

Double-click [Start_DocuSync_AI.bat](Start_DocuSync_AI.bat) in this folder. It opens the browser workspace from
the repository's `hackathon-project` folder. The shared launcher starts or reuses Ollama, checks and warms
the configured models, and opens DocuSync AI when the workspace is ready. The **AI startup** panel shows
installation, residency, inference checks, and model errors.

The workspace uses `hackathon-project/.venv`, the optional `hackathon-project/.env`, and
`hackathon-project/logs/startup.log`. See the [workspace setup, shutdown, and troubleshooting instructions](../../hackathon-project/README.md).

## Technology stack

| Technology | Role |
| --- | --- |
| Python 3.11 | Runs the command-line converter. |
| PyMuPDF | Reads PDFs and extracts their embedded text in page and block order. |
| `argparse` and `pathlib` | Handle command-line arguments and file paths. |
| `xml.etree.ElementTree` | Writes UTF-8 XML with numbered pages and paragraph blocks. |

The starter processes PDFs with selectable text. Image-only scanned pages appear as empty pages in its XML output.

## Run the starter on Windows

1. Install Python 3.11 and open PowerShell at the repository root. Confirm that `python --version` reports Python 3.11. If you select Python 3.11 through the Windows Python launcher, use `py -3.11` in place of `python` when creating the environment.

2. Enter the starter folder, create a virtual environment, and install its dependency.

   ```powershell
   Set-Location DocuSyncAI-starter\DocuSyncAI
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r project\requirements.txt
   ```

3. Create the input folder and copy at least one PDF with selectable text into `phase-1\input`.

   ```powershell
   New-Item -ItemType Directory -Force phase-1\input
   ```

4. Run the converter from the starter folder. Both arguments take folder paths. Quote paths that contain spaces.

   ```powershell
   .\.venv\Scripts\python.exe project\main.py --input phase-1\input --output phase-1\output
   ```

5. Open `phase-1\output` to find one XML file per PDF. For example, `sample.pdf` produces `sample.xml`. The terminal prints an `[OK]` line for each converted file with the page, empty-page, text block, and character counts.

If the command reports that the input folder is missing or has no PDFs, check the folder path and place the files directly in that folder. The converter creates the output directory as needed and overwrites XML files with the same name on subsequent runs.

## Next engineering milestones

1. Add per-page diagnostics: native text density, image coverage, rotation, and language/script hints.
2. Add OCR fallback only for pages with absent or suspicious native text.
3. Add layout-aware reconstruction for headings, paragraphs, lists, tables, figures, captions, headers, and footers.
4. Add structural and text QA against the supplied example reference.
5. Implement PPTX amendment comparison and propagation as a separate module.
6. Benchmark quality and runtime on the complete Phase 1 corpus.

## Important

The challenge brief requires the HTML element vocabulary and `page` numbering with `@n`. The supplied example XML uses a different, richer legacy schema (including `page @num` and custom tags). Confirm this discrepancy with the organisers before finalizing the serializer. This starter follows the written challenge brief's `page @n` convention.

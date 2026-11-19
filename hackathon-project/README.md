# DocuSync AI - Innodata AI Catalyst 2026 (Internal Track)

Turns scanned / corrupted / multilingual PDFs into structured XML and proof-reads the matching PowerPoint decks
against the source document. CPU-first, fully offline, no model APIs.

```
PDF -> [diagnose each page] -> native text  or  OCR -> layout/reading order -> XML (<name>.xml)
PPTX + source PDF -> evidence alignment -> targeted, formatting-preserving fixes -> corrected deck + change report
```

## Quick start

```powershell
# Windows 11 / PowerShell, Python 3.11
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r project\requirements.txt
# OCR engine (one-time): install Tesseract 5 (UB Mannheim build) and tick the language packs
#   Arabic, Japanese, Korean, Czech, Slovak, German, Dutch, Italian, French, English, "OSD"
# if it is not on PATH:   $env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"

python project\main.py --input phase-1\input --output phase-1\output
```

Linux/macOS: `sudo apt install tesseract-ocr tesseract-ocr-{ara,jpn,kor,ces,slk,deu,nld,ita,fra}` (or the brew equivalents).

* **Input**: any folder containing `*.pdf` (and optionally `*.pptx`). Nothing is hard-coded to file names.
* **Output** (`--output`): `<pdf name>.xml` for every PDF, and one corrected deck per PPTX
  (`amendment<N>.pptx` -> `amendment_out<N>.pptx`; otherwise `<id>_transformed_QA.pptx`, the pattern used by the example data).
* **Run folder** (`<output>/../docsync_run`): audit log (`audit.jsonl`), manifest, per-document IR, change reports
  (`changes/*.json`), `run_report.json`, `report.html`, OCR cache. It is separate so the output folder stays clean.
* Useful flags: `--workers N`, `--profile brief|example`, `--no-ocr`, `--skip-pptx`, `--time-budget SECONDS`, `--config file.json`
  (see `project/config/default.example.json`).

## Review dashboard (demo UI)

```powershell
pip install flask   # already in requirements.txt
python project\ui\server.py --run-dir phase-1\docsync_run --input phase-1\input --output phase-1\output
# open http://127.0.0.1:8765
```
Zoning / Reading Order / Source PDF / Text views, per-page diagnostics, confidence-filtered metadata, XML view + download,
deck change review (accept / reject), re-process a document, and a run dashboard. Runs offline.

## Tests and benchmark

```powershell
cd project ; python -m pytest tests -q                       # 21 tests
python tests\benchmark.py --corpus "<path>\Hackathon-Internal-Track\Data"
```
Measured results and their caveats: `docs/BENCHMARKS.md`. Requirement-to-module mapping: `docs/REQUIREMENTS_TRACEABILITY.md`.
Design, limitations and tool/AI declarations: `TECHNICAL_APPROACH.md`.

## Repository layout

```
project/            main.py, requirements.txt, config/ ingestion/ diagnostics/ ocr/ layout/ xml_engine/
                    comparison/ powerpoint/ agents/ quality/ reporting/ ui/ utils/ tests/
phase-1/ phase-2/   input/ and output/ (inputs are git-ignored: the data is confidential)
docs/               traceability matrix, benchmarks
```

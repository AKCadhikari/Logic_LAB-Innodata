# DocuSync AI - Innodata AI Catalyst 2026 (Internal Track)

Turns scanned / corrupted / multilingual PDFs into structured XML and proof-reads the matching PowerPoint decks
against the source document. CPU-first and local. Qwen3 4B provides advisory review of document-version changes.

```
PDF -> PyMuPDF diagnostics -> native text or GLM-OCR (PaddleOCR fallback)
    -> Docling or local layout reconstruction -> validated document IR -> XML (<name>.xml)
PDF versions -> deterministic changes -> Qwen3 review -> verified PowerPoint version sync
PPTX + source PDF -> evidence alignment -> targeted, formatting-preserving fixes -> corrected deck + change report
```

## Quick start

```powershell
# Windows 11 / PowerShell, Python 3.11
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r project\requirements.txt
# With Ollama running:
ollama pull glm-ocr:q8_0
ollama pull qwen3:4b

python project\main.py --input phase-1\input --output phase-1\output
```

See [local OCR setup](docs/LOCAL_OCR.md) for PaddleOCR, Docling, Tesseract verification, and runtime controls.

* **Input**: any folder containing `*.pdf` (and optionally `*.pptx`). Nothing is hard-coded to file names.
* **Output** (`--output`): each export has its own `<pdf stem>/<export id>/` folder containing
  `<pdf stem>.xml`, `<pdf stem>_updated.pptx`, and processing, OCR, and validation reports. The XML
  uses the organizer sample profile and passes local structural validation. A failed or missing deck leaves the validated
  XML and reports in place and produces a nonzero batch exit code.
  Complete exports also contain an extracted-and-verified ZIP with linked assets, correction evidence, and AI execution details.
* **Run folder** (`<output>/../docsync_run`): audit log (`audit.jsonl`), manifest, per-document IR, change reports
  (`changes/*.json`), `run_report.json`, `report.html`, OCR cache. It is separate so the output folder stays clean.
* Useful flags: `--workers N`, `--profile brief|sample|example`, `--no-ocr`, `--skip-pptx`, `--time-budget SECONDS`, `--config file.json`
  (see `project/config/default.example.json`).

## Local review workspace

```powershell
.\.venv\Scripts\python.exe -B project\ui\server.py
# open http://127.0.0.1:8765
```
Upload one PDF at a time. The local workspace stores each original and its processing attempts under `storage/`
and shows job progress, native-text diagnostics, OCR attempts, layout status, and pages flagged for review.
Review a page in Source PDF, Text, Zoning or Reading Order, correct the title or language,
then approve and export XML. Locate an extracted title in the PDF when the source region is available. Use Changes
to compare two versions and apply matching text updates to an uploaded PowerPoint deck.
Use **Repair and export XML + PowerPoint** in Changes to proofread a deck against the selected PDF,
or **Apply version changes and export** to synchronize an existing deck. The result shows both file names,
sizes, validation outcomes, and the output folder. Download either file separately or both with the
XML's figure assets in a ZIP. The standalone approved XML download remains available in Document review.
Uploads and export history are scoped to the browser's local workspace cookie. The first browser to open the
workspace owns Batch review and adopts documents that predate browser ownership.
The settings dialog shows local model availability and saves a reviewer name for attribution.

Select **Batch review** to open the original dashboard. It uses the Phase 1 folders by default and accepts
`--run-dir`, `--input` and `--output` for other batch runs. Use a dedicated `--storage` path for each running
workspace instance. The server binds to loopback for local single-user review.

The project can discover a portable Tesseract under `.venv/tesseract/` on Windows. Other installations can use
`TESSERACT_CMD` or the pipeline settings file. Settings in the workspace show the available language packs.
Qwen3 review is enabled by default. Set `DOCUSYNC_OLLAMA_ENABLED=false` to disable it.

## Tests and benchmark

```powershell
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider project/tests -q
.\.venv\Scripts\python.exe -B project\tests\benchmark.py --corpus "<path>\Hackathon-Internal-Track\Data" --out storage\windows-benchmark.json
```
Measured results and their caveats: `docs/BENCHMARKS.md`. Requirement-to-module mapping: `docs/REQUIREMENTS_TRACEABILITY.md`.
Design, limitations and tool/AI declarations: `TECHNICAL_APPROACH.md`.
The [official sample validation](../docs/OFFICIAL_SAMPLE_VALIDATION.md) records the source and reference comparisons,
the remaining differences, and the command for reproducing the reports. The [repository audit](../docs/REPOSITORY_AUDIT.md)
and [implementation roadmap](../docs/IMPLEMENTATION_ROADMAP.md) record the earlier Windows verification.
For a saved bundle, use `project/sample_check.py --export-dir <folder>` with the reference inputs described there.
All documents use the same page-level profiling, OCR, layout, and presentation-matching pipeline. The earlier
source-region map remains available to the offline sample comparison command as historical ground truth.

## Local multilingual verification

The engineering brief requires Arabic, Japanese, Korean, Czech, Slovak, German, Dutch, Italian, French,
and English. Detection records include script, language, direction, confidence, and method. The workspace
shows page and block language and flags uncertainty. Downloads from pages with missing text or unresolved
native text ordering are marked as draft, with the PowerPoint update blocked. Structural validation and
source accuracy are reported separately.

Each extraction saves `document_profile.json` and `multilingual_validation_report.json` in its
`profiles/<document-id>` work folder. Dual exports include both reports alongside the XML and PowerPoint.
Run the local benchmark with the project's virtual environment from this directory.

```powershell
.\.venv\Scripts\python.exe -B project/language_check.py --out storage/new-language-capabilities
.\.venv\Scripts\python.exe -B project/unseen_check.py --out storage/new-unseen-validation --capabilities storage/new-language-capabilities/language_support_report.json
```

The held-out runner uses Playwright, installed Chrome, and (for presentation rendering) LibreOffice.
It writes the language support matrix and unseen-document report as well as the profiles and validation
reports. The authored holdout is separate from the official samples. The matrix reports Unicode text,
font coverage, and text recovered from rendered slides separately. Bidirectional presentation fidelity
still requires visual review, especially for dates, units, and punctuation.

## Repository layout

```
project/            main.py, requirements.txt, config/ ingestion/ diagnostics/ ocr/ layout/ xml_engine/
                    comparison/ powerpoint/ agents/ quality/ reporting/ ui/ utils/ tests/
phase-1/ phase-2/   input/ and output/ (inputs are git-ignored: the data is confidential)
docs/               traceability matrix, benchmarks
```

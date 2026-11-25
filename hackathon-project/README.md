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

On Windows, double-click **[Start_DocuSync_AI.bat](Start_DocuSync_AI.bat)**. The launcher finds or starts Ollama,
warms each configured model in turn, and verifies which models Ollama keeps loaded. It then starts the existing
workspace, checks the backend and frontend, and opens [DocuSync AI](http://127.0.0.1:8765/) in the default browser.
Open **AI startup** in the workspace to see each model's installation, residency and synthetic inference status.

The launcher uses the built-in defaults. To override them, copy `.env.example` to `.env`. Both the launcher and the
application read the same settings. When supplied, a JSON configuration is loaded first, then `.env`, then the
process environment. A running workspace with changed settings needs a launcher restart.

| Startup mode | Behavior |
| --- | --- |
| `all` (default) | Warms each model in turn and checks that both remain resident. |
| `ocr-only` | Warms GLM-OCR and leaves Qwen to load when document review first uses it. |
| `sequential` | Tests both models and releases the first model if it was loaded by this startup. |

The default startup residency is 30 minutes. Ollama can evict a model sooner under memory pressure. If it does,
the workspace reports the model's current state. `OLLAMA_KEEP_ALIVE` in `.env` also controls retention after
document inference. The launcher leaves already-running Ollama services in place.

**Troubleshooting.** If the workspace opens in degraded mode, read the specific error under **AI startup**.
Missing models show their exact tags and a manual installation command. Install only the missing model, then
select **Retry model startup**. If there is a port conflict, check which application owns the port or configure a
different Ollama endpoint. If memory is tight, use `OLLAMA_STARTUP_MODE=ocr-only` or `sequential`.
See `logs/startup.log` for startup events, `logs/backend.log` for Flask output and `logs/ollama-11434.log`
for output from an Ollama server started by the launcher.

For controlled shutdown or restart, run one of these from the project directory. Shutdown leaves a workspace
with active requests or document jobs running. Repeat the command after the work completes.

```cmd
Start_DocuSync_AI.bat -Action Stop
Start_DocuSync_AI.bat -Action Restart
```

If you originally started the backend directly from a terminal, close it from that terminal before using the
launcher. `-NoBrowser` suppresses the browser for smoke tests. Advanced commands can supply `-Port`, `-Config`,
`-Storage` and `-Logs`. Use separate storage and logs when testing a second workspace.

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

### AI execution audit

Open **AI startup** with a processed document selected to see which model requests were recorded and which
responses reached a downstream consumer. Model availability and startup warm-ups are reported separately.
The audit distinguishes native PyMuPDF extraction, Ollama inference, local PaddleOCR/Tesseract recognition,
Docling layout, and deterministic processing of the IR, XML, and presentation.

Download the three JSON reports from the audit panel. They are also included in dual exports. The execution
report records observed requests, responses, timing and validation. The usage report counts real executions,
and the processing trace links available source block IDs to the saved XML and verified PPTX edits. Document
text and prompts are omitted from the audit reports. Set `DOCUSYNC_AUDIT_PRIVATE_RESPONSES=true` to keep raw
responses in the local run folder for debugging. These files are excluded from the audit downloads and ZIP.
Older attempts may show source methods without execution evidence. A fresh extraction records the model calls.

To run live checks with fresh, synthetic inputs against the configured models:

```powershell
.\.venv\Scripts\python.exe -B project/execution_check.py --out storage/new-execution-check
```

The execution check distinguishes live model probes from the full pipeline run. Mock HTTP tests use the
`MOCKED_TEST` label and are excluded from real usage totals.

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

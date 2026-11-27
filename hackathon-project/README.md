# DocuSync AI - Innodata AI Catalyst 2026 (Internal Track)

Turns scanned / corrupted / multilingual PDFs into structured XML and proof-reads the matching PowerPoint decks
against the source document. CPU-first and local. Qwen3 4B provides advisory review of document-version changes.

```
PDF -> PyMuPDF diagnostics and rendering -> DocLayout-YOLO -> native text or selected OCR
    -> layout association -> Docling and local reconstruction -> validated XML and PowerPoint exports
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

python project\main.py --phase 1
# For an advanced team, use the same workflow with --phase 2.
```

See [local OCR setup](docs/LOCAL_OCR.md) for PaddleOCR, Docling, Tesseract verification, and runtime controls.
See the [AI Catalyst compliance checklist](COMPLIANCE_CHECKLIST.md) for official requirements and current evidence.
See [local sample verification](docs/LOCAL_SAMPLE_VERIFICATION.md) for measured DocLayout inference, XML comparison and PPTX reference results.

* **Input**: any folder containing `*.pdf` (and optionally `*.pptx`). Nothing is hard-coded to file names.
* **Output** (`--output`): each export has its own `<pdf stem>/<export id>/` folder containing
  `<pdf stem>.xml`, `<pdf stem>_updated.pptx`, and processing, OCR, and validation reports. The XML
  uses the organizer sample profile and passes local structural validation. A failed or missing deck leaves the validated
  XML and reports in place and produces a nonzero batch exit code.
  Complete exports also contain an extracted-and-verified ZIP with linked assets, correction evidence, and AI execution details.
* **Run folder** (`<output>/../docsync_run`): audit log (`audit.jsonl`), manifest, per-document IR, change reports
  (`changes/*.json`), `run_report.json`, `report.html`, OCR cache. It is separate so the output folder stays clean.
* `--phase 1|2` selects the matching input, output and run folders by default. Explicit `--input`, `--output` and `--work-dir` override them.
* Useful flags: `--workers N`, `--profile brief|sample|example`, `--no-ocr`, `--skip-pptx`, `--time-budget SECONDS`, `--config file.json`
  (see `project/config/default.example.json`).

## Local review workspace

### Correct one presentation from its source PDF

From the `hackathon-project` directory, run the explicit pair command:

```powershell
.\.venv\Scripts\python.exe -B project\repair_pptx.py --pdf "path\source.pdf" --pptx "path\original.pptx" --out "path\corrected.pptx"
```

The command uses the PDF's extracted structure and local OCR evidence. It changes only source-supported
PowerPoint objects and keeps the original package parts. The output has a nearby `_reports` folder with the
source IR, detected and applied edits, unresolved items, PDF crops, and an HTML quality report. LibreOffice,
when available, renders the saved slides for an additional visibility check. Native equations in compatibility
markup can require a visual check in PowerPoint. Chart workbook edits, figure replacement, new slide objects,
and unsupported equation structures require manual review. `--ai-review` adds advisory classifications to
selected unresolved items using local Ollama. Set `amend_embedding_model` in the project configuration to
an installed local embedding model to supplement keyword and fuzzy retrieval.

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

Choose the phase and OCR engine in **Processing settings**, then add a PDF and a PowerPoint using the two upload boxes.
The workspace opens when both uploads succeed. Switch between **PDF** and **PowerPoint** at the top of the workspace.
Select **Start Processing** to extract the PDF. PowerPoint proofreading begins after upload.
The workspace archives the source PDF in the selected `phase-N/input/` folder and stores processing attempts under
`storage/`. After a successful run it automatically validates and publishes a versioned XML bundle under the matching
`phase-N/output/` folder. The document details show the output location, DocLayout page status, detection count and
measured detection time. **Zoning** overlays DocLayout-YOLO detections on the original page. Select a region to inspect its page,
confidence, coordinates and associated text. **Reading order** shows the reconstructed block sequence.
Choose **OCR engine** before upload or reprocessing. **GLM-OCR**, **PaddleOCR-VL (Ollama)** and
**PaddleOCR (Python)** are strict choices. **Automatic** defaults to GLM-OCR, then PaddleOCR-VL, then
PaddleOCR Python. It advances after an unavailable engine, a timeout, rejected output, or a result
requiring corroboration. A disagreement leaves one result flagged for review.
Unreviewed source extraction or XML content differences keep the XML marked as a draft and block PowerPoint export.
In the workspace, review the source and extracted content, then approve the document to allow export; blocking
validation errors still prevent publication.
Pages with a usable native layer continue through native extraction. Uncheck **Remember engine** for an override
that applies only to the next submission. Each attempt retains its own engine and model settings.

Use **PaddleOCR language** for an image-only page with a known language. When there is no language evidence,
PaddleOCR uses the configured default (`DOCUSYNC_PADDLE_OCR_UNKNOWN_LANG`, initially `en`).
`GLM_OCR_MODEL` accepts `glm-ocr:q8_0` or `glm-ocr:latest`. `PADDLE_VL_MODEL` defaults to
`MedAIBase/PaddleOCR-VL:0.9b`. The installed MedAIBase artifact exposes completion only and rejects document images.
DocuSync records this as an unavailable OCR engine. Its **Test Engine** still submits a synthetic image to verify
the server's response. A vision-capable tag can be configured separately after it is installed.
Configure Automatic with `DOCUSYNC_OCR_AUTOMATIC_PRIMARY`, `DOCUSYNC_OCR_AUTOMATIC_SECONDARY`, and
`DOCUSYNC_OCR_AUTOMATIC_TERTIARY`. An empty tertiary value disables the third attempt.
The workspace queues jobs by default and can run more concurrently through `DOCUSYNC_WORKSPACE_WORKERS`.
The CLI accepts `--ocr-backend automatic|glm-ocr|paddleocr-vl|paddleocr|tesseract`.

**Settings → OCR health and comparison** separates installation, availability, observed residency, and previous
successful inference. Use the engine's **Test** button to submit a generated image with a known value.
The workspace shows elapsed time, measured requests, fallback events, and an estimate after two completed
page measurements. The estimate covers extraction. Labelled accuracy appears after running a
benchmark. Run this from the project directory to publish a local comparison in the dashboard.

```powershell
.\.venv\Scripts\python.exe -B project/ocr_selector_check.py --out storage/new-ocr-comparison --dashboard-file logs/ocr-comparison.json
```

Review a page in Source PDF, Text, Zoning or Reading Order, correct the title or language,
then select **Submit review**. Export XML or choose **Export PPTX** for one image-backed slide per source page with
the reconstructed text in the speaker notes. **Re-edit** saves your updates as a draft. Locate an extracted title in
the PDF when the source region is available. Use **PowerPoint & changes**
to compare two versions and apply matching text updates to an uploaded PowerPoint deck.
Use **Repair and export XML + PowerPoint** in Changes to proofread a deck against the selected PDF,
or **Apply version changes and export** to synchronize an existing deck. The result shows both file names,
sizes, validation outcomes, and the output folder. Download either file separately or both with the
XML's figure assets in a ZIP. The standalone approved XML download remains available in Document review.
Uploaded proofreading decks are archived in the selected phase input folder; corrected PPTX exports are versioned in
that phase's output folder. Deck repair is an explicit action so the user can choose which supplied deck to proofread.
Uploads and export history are scoped to the browser's local workspace cookie. The first browser to open the
workspace owns Batch review and adopts documents that predate browser ownership.
The settings dialog shows local model availability and saves a reviewer name for attribution.

Select **Batch review** to open the original dashboard. It uses the Phase 1 folders by default and accepts
`--run-dir`, `--input` and `--output` for other batch runs. Use a dedicated `--storage` path for each running
workspace instance. The server binds to loopback for local single-user review.

The project can discover a portable Tesseract under `.venv/tesseract/` on Windows. Other installations can use
`TESSERACT_CMD` or the pipeline settings file. Settings in the workspace show the available language packs.
Qwen3 review is enabled by default. Set `DOCUSYNC_OLLAMA_ENABLED=false` to disable it.

### DocLayout-YOLO runtime

The default checkpoint is `models/layout/universal/DocLayout-YOLO-universal.pt`. DocuSync starts the local
`.venv-doclayout` interpreter and loads the vendored repository from `third_party/DocLayout-YOLO-release-v2`.
The worker loads the checkpoint once per document and runs inference on CPU. Docling continues to use its
separate runtime. **Ready** means the layout model passed initialization. A failed detection is listed for the
affected page and the extraction retains its native or OCR content for review.

From the repository root, install Git LFS and retrieve the checkpoint and pinned model code. Then create the
local DocLayout environment from the `hackathon-project` directory.

```powershell
git lfs install
git lfs pull
git submodule update --init -- hackathon-project/third_party/DocLayout-YOLO-release-v2
Set-Location hackathon-project
py -3.11 -m venv .venv-doclayout
.\.venv-doclayout\Scripts\python.exe -m pip install .\third_party\DocLayout-YOLO-release-v2
```

Set `DOCUSYNC_DOCLAYOUT_PYTHON`, `DOCUSYNC_DOCLAYOUT_REPO` or `DOCUSYNC_DOCLAYOUT_CHECKPOINT` to override their
paths. Relative paths resolve from the hackathon project root. Set `DOCUSYNC_DOCLAYOUT_ENABLED=false` to use the
existing reconstruction path without YOLO. `DOCUSYNC_DOCLAYOUT_IMAGE_SIZE`, `DOCUSYNC_DOCLAYOUT_CONFIDENCE` and
`DOCUSYNC_DOCLAYOUT_TIMEOUT_S` control inference. Zoning supports PDF page rotation. Links to reconstructed
text require corroborated geometry and may be unavailable for skewed or rotated text.

If Ollama is already running, start the review workspace directly from the hackathon project root:

```powershell
.\.venv\Scripts\python.exe -B project\ui\server.py --port 8765
```

## Tests and benchmark

Run the browser integration check against a running workspace with Chrome and Playwright installed. Supply an
existing single-page digital PDF and a multi-page PDF with an OCR-required page and visible text. If the
published sample paths are unavailable, use equivalent local files.

```powershell
.\.venv\Scripts\python.exe -B project\tests\live_doclayout_check.py `
  --single storage\b2959a82b78e486fbe06c4e91cc6bef5\source.pdf `
  --scan storage\ocr-selector-live\sample-en.pdf
```

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
In an individual workspace document, **XML preview** and **Export XML** use the organizer's sample hierarchy.
Approve the metadata review, then choose **Export XML**. If the XML links to figures or equation crops, the download
is a ZIP with the XML and its `output_images/<document>/` folder. Extract the ZIP and keep the folder beside the XML.
For command-line extraction in the same format, use
`--profile sample --skip-pptx`. Integrations that need the written-brief format can request
`?profile=brief` on the individual document XML endpoint.
For OCR-derived equations, Automatic and GLM-OCR run formula recognition on independently rendered crops.
The XML keeps the source image. When the readings agree, the formula image also carries a
`formula_encoding="latex"` attribute and searchable LaTeX in `alt`. The validation report counts formulas
with text and flags unresolved readings for review.

All XML profiles are checked against local XSDs in \`project/xml_engine/schemas/\`. The sample XSD is derived
from the organizer's reference XML. The supplied archive has no organizer XSD. The export package includes
\`manifest.json\` and reports for schema validation, source-linked completeness, formatting, and image assets.
The completeness report compares XML with the intermediate representation and records source uncertainty by page.
The formatting report distinguishes measured PDF font and geometry data from heuristics.

### Compare an engineering reference

Generate a sample-profile export with `project/main.py --input <folder> --output <output> --profile sample`.
Then run `project/xml_quality_check.py --reference <expected.xml> --actual <generated.xml> --report <report.json>`.
Add `--baseline <prior.xml>` for a before-and-after report. The comparison aligns text within each page and
allows adjacent split or merged blocks. It reports schema and page checks, CER and WER, semantics, reading order,
headings, references, captions, and equation labels. Use `--equation-evidence <review.json>` to supply
source-reviewed printed labels when the reference stores equations solely as images. Reference files are used by
the evaluator. PDF conversion and PowerPoint repair use the source PDF and shared intermediate representation.
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

Competition inputs are confidential. Keep this repository private and share only with authorized reviewers. The
`.gitignore` intentionally excludes local inputs and generated output bundles; when preparing the official submission,
stage only the required, generated examples in the designated private team repository after confirming access and privacy.

# DocuSync AI development roadmap

Canonical application directory is [hackathon-project/project](../hackathon-project/project). Keep the phase folders and starter baseline available as reference. Run the application locally on Windows from its dedicated Python 3.11 environment. See [repository audit](REPOSITORY_AUDIT.md) for the source inventory and requirement conflicts.

| Phase | Priority | Current state | Dependencies and acceptance criteria |
| --- | --- | --- | --- |
| A. Repository audit | P0 | Repository and accessible attachment contents inventoried. | Obtain original organizer files and record any differences from the repository's account. |
| B. Architecture and migration | P0 | Established one application with the existing Flask UI, batch CLI and shared IR. | Preserve CLI output naming and phase folders. Keep storage ignored. Check both application entry points when modifying shared modules. |
| C. Windows environment | P0 | Isolated Python 3.11, package checks and portable Tesseract verified. | Reproduce on Ryzen 3 with the exact CPU, Windows and Radeon configuration recorded. |
| D. Backend and storage | P0 | UUID upload, original storage, SQLite history, single background worker and retries. | Verify batch coexistence, duplicate filenames, failure and restart recovery. Add deployment controls before any shared access. |
| E. Review workspace | P0 | PDF preview, page and zoom controls, text, overlays, title source linking, edits, approval and XML export. Chrome workflow tested. | Review against supplied screenshots when available. Confirm accessibility and behavior on complex, rotated and long PDFs. |
| F. PDF diagnostics and extraction | P0 | Selective native/OCR routing and encrypted/corrupt-file handling in the workspace. | Add labeled mixed-layer and rotated fixtures. Validate geometry and warnings for incomplete pages. |
| G. OCR and layout intelligence | P1 | Tesseract smoke tests cover the ten requested recognition packs. Digital native table and figure extraction is the baseline. | Compare Paddle and Docling/TableFormer adapters on authorized scans. Measure character/word accuracy, reading order and table reconstruction, together with end-to-end time and memory. |
| H. Metadata and XML | P0 | Reviewed title/language drive serialized XML. Structural checks run before workspace export. | Inspect the actual example and written requirements, write a schema or golden assertions, and confirm tag and metadata coverage with the organizer. |
| I. Version comparison | P1 | Deterministic matching with page references and optional local comparison hints. | Measure added/removed/modified, structure/table/figure and repeated-content cases with labeled version pairs. Run actual Qwen3 4B only after local setup and hardware verification. |
| J. PowerPoint | P0 | Existing batch repair plus workspace text synchronization with change records and reopen verification. | Exercise organizer decks and validate affected paragraph and slide sets, unchanged areas, mapping decisions and unsupported changes. |
| K. Performance and submission | P0 | Synthetic Windows measurements and browser/API regression tests. | Benchmark the protected corpus on target hardware, verify the reported two-hour limit against the official brief and prepare the submission assets. |

## Verification record

- First startup with the starter environment reached CLI help and failed importing Flask. The isolated environment restored Flask server startup and left the starter usable as a baseline. Installed the existing Python requirements, portable Tesseract 5.4.0 and ten recognition packs. Playwright was installed locally for browser verification.
- Initial test suite in the isolated environment had **20 passed, 1 skipped** before Tesseract was provisioned. The expanded suite reported **37 passed**, including generated multilingual OCR images, an encrypted PDF, corrupt PDF retry and history, metadata correction and approved XML, reviewer attribution, exact-match PPTX synchronization, ambiguous deck match handling and optional Ollama response and unavailable cases.
- Chrome through Playwright ran against the live Flask application. It exercised two same-named uploads, linked versions, rendered PDF pages, navigation and zoom, zoning and reading order overlays, title-region highlighting, review approval, XML inspection and download, comparison and validated PPTX export.
- The synthetic batch used one worker and completed in **2.19 s** with **199.9 MiB** sampled peak process-tree RSS. The Windows execution host reports an Intel processor. These results apply to the generated inputs and host. The corpus and Ryzen 3 measurements are pending.

| Synthetic batch input | Pages/slides | Extraction or update | Elapsed | Output and validation |
| --- | --- | --- | --- | --- |
| `doc1.pdf` | 1 page | 1 native, 0 OCR | Below 0.01 s at report precision | `doc1.xml`, parsed with one numbered page, zero structural issues and the expected text. |
| `doc2.pdf` | 1 page | 0 native, 1 OCR | 1.60 s | `doc2.xml`, parsed with one numbered page, zero structural issues and the expected text. |
| `amendment1.pptx` | 1 slide | 1 targeted number update | 0.03 s | `amendment_out1.pptx`, passed ZIP verification and reopened with the expected update. |

No processing errors were recorded in that synthetic batch. The multilingual OCR suite checks one expected token or phrase and valid OCR boxes and confidence in each of ten rendered synthetic lines. Accuracy metrics on representative organizer data remain pending.

### Reproduce locally

From `hackathon-project` in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider project/tests -q
.\.venv\Scripts\python.exe -B project/ui/server.py
.\.venv\Scripts\python.exe -B project/main.py --input phase-1/input --output phase-1/output
.\.venv\Scripts\python.exe -B project/tests/benchmark.py --corpus "<authorized corpus>" --out storage/windows-benchmark.json
```

The batch and benchmark commands require input files. The benchmark accepts a new report path, leaving the historical Linux result available.

### Implementation checklist

- [x] Keep the existing application and deterministic extraction modules as the canonical implementation.
- [x] Run the Windows backend, frontend, batch pipeline, selective OCR and review-to-export flow.
- [x] Preserve separate originals and attempts and validate workspace XML and exported PPTX.
- [x] Verify generated scans across all ten Tesseract language packs and the unavailable Ollama fallback.
- [ ] Validate the supplied competition XML contract and benchmark the authorized corpus on the target hardware.
- [ ] Evaluate scanned table/layout adapters, live local Qwen3 behavior, stronger version matching and source geometry for rotated OCR.
- [ ] Validate additional competition metadata, presentation relationships and required submission artifacts.

### Implementation files

| Change | Files under `hackathon-project/project` |
| --- | --- |
| Workspace API, storage, jobs and browser controls | `ui/server.py`, `ui/workspace.py`, `ui/workspace_routes.py`, `ui/static/index.html`, `ui/static/workspace.js` |
| Extraction, review IR, OCR and XML validation | `agents/orchestrator.py`, `layout/model.py`, `layout/native.py`, `layout/kinds.py`, `ocr/engine.py`, `xml_engine/validate.py` |
| Deterministic comparison, local Ollama and PPTX sync | `comparison/differ.py`, `comparison/ollama.py`, `powerpoint/sync.py`, `config/settings.py`, `config/default.example.json` |
| Evaluation, runtime requirements and tests | `quality/evaluate.py`, `requirements.txt`, `tests/benchmark.py`, `tests/test_pipeline.py`, `tests/test_amend.py`, `tests/test_workspace.py`, `tests/test_multilingual_ocr.py`, `tests/test_ollama.py` |

The hackathon project's README, technical approach, benchmark and traceability notes describe the updated system.
Its `.gitignore` covers workspace storage and default phase output folders.

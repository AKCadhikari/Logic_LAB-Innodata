# DocuSync AI

DocuSync AI is a local document inspection and conversion workspace. It extracts text and structure from digital, scanned, and multilingual PDFs, produces validated XML, and helps users review and correct PowerPoint presentations.

## Get started on Windows

Use Python 3.11 and PowerShell. Install Git LFS for the layout checkpoint and Ollama for GLM-OCR and PowerPoint proofreading. Install LibreOffice if you want slide previews and render-based checks.

From the repository root, retrieve the model assets and set up the two Python environments.

```powershell
git lfs install
git lfs pull
git submodule update --init -- hackathon-project/third_party/DocLayout-YOLO-release-v2

Set-Location hackathon-project
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r project\requirements.txt
py -3.11 -m venv .venv-doclayout
.\.venv-doclayout\Scripts\python.exe -m pip install .\third_party\DocLayout-YOLO-release-v2
```

With Ollama running, pull the default OCR and proofreading models, then start the workspace.

```powershell
ollama pull glm-ocr:q8_0
ollama pull qwen3:4b
.\Start_DocuSync_AI.bat
```

The launcher opens [DocuSync AI on localhost](http://127.0.0.1:8765/). See the [project guide](hackathon-project/README.md) for environment overrides and [local OCR setup](hackathon-project/docs/LOCAL_OCR.md) for PaddleOCR, Docling, and Tesseract.

## Use the workspace

1. Add the source PDF and its PowerPoint in either order. Once both files upload, DocuSync starts one job and uses the saved PDF as evidence for repairs.
2. Use the **PDF** tab to inspect the source, zoning, reading order, text, metadata, and XML. The shared timeline tracks extraction, slide repair, validation, and packaging.
3. Switch to **PowerPoint** to inspect the repaired slides and review the correction details. Use the preview selector to compare with the original when both are available.
4. Choose **Download Final Output (ZIP)** from either tab. The archive contains the validated XML, repaired deck, and assets referenced by the XML.

If the job pauses for source review, check the affected pages and submit the review to resume the saved job. For blocking content or validation failures, reprocess the saved inputs after addressing the reported issue.

**Processing History** lets you reopen documents and completed jobs. The browser's local workspace session controls access to its uploads and history.

## Command-line workflows

Run these commands from `hackathon-project`.

**Extract XML from a folder**

```powershell
.\.venv\Scripts\python.exe -B project\main.py --phase 1 --input "path\to\pdfs" --output "path\to\output" --profile sample --skip-pptx
```

**Repair one presentation against its source PDF**

```powershell
.\.venv\Scripts\python.exe -B project\repair_pptx.py --pdf "source.pdf" --pptx "original.pptx" --out "corrected.pptx"
```

Use the repair command when the PDF should supply the facts for a correction. It applies evidence-backed edits to supported PowerPoint objects, reopens the saved deck to validate the edits, and writes a sibling `corrected_reports/` folder with source evidence, corrections, unresolved items, and an HTML QA report. Optional `--ai-review` adds local advisory review. Complex equations, charts, and figures can require manual verification.

## Outputs and checks

| Location | Contents |
| --- | --- |
| `hackathon-project/phase-1/` and `phase-2/` | Archived uploads and versioned XML or PowerPoint exports under the selected phase |
| `hackathon-project/storage/` | Workspace database and local processing attempts |
| `hackathon-project/logs/` | Launcher and backend diagnostics |

XML downloads may be ZIP files when the XML references extracted images. Keep the asset folder beside the XML after extracting the ZIP. Keep client documents, generated outputs, and local configuration out of Git.

Run the automated tests from `hackathon-project`.

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q project\tests
```

If the browser reports missing preview routes after a code update, allow active jobs to finish, then restart the launcher and reload the page.

```powershell
.\Start_DocuSync_AI.bat -Action Restart
```

For a server started directly in a terminal, stop it from that terminal and start it again with the current checkout.

## Project references

- [Detailed setup, troubleshooting, and pipeline configuration](hackathon-project/README.md)
- [Benchmarks and evaluation caveats](hackathon-project/docs/BENCHMARKS.md)
- [Official sample validation](docs/OFFICIAL_SAMPLE_VALIDATION.md)

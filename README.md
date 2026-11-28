# DocuSync AI

DocuSync AI is a local document inspection and conversion workspace. It extracts text and structure from digital, scanned, and multilingual PDFs, produces validated XML, and helps users review and correct PowerPoint presentations.

The browser workspace lives in `hackathon-project`. If you are working in `DocuSyncAI-starter`, its Python entry point runs the original PDF-to-XML baseline. See the [starter setup guide](DocuSyncAI-starter/DocuSyncAI/README.md) for that command.

## Technology stack

| Technology | Role |
| --- | --- |
| Python 3.11 and Flask | Run the document pipeline and serve the local browser workspace. |
| HTML, CSS, and JavaScript | Provide document uploads, previews, and job review in the browser. |
| PyMuPDF | Extracts PDF text and renders pages for layout analysis and OCR. |
| DocLayout-YOLO and OpenCV | Detect document regions such as text, tables, and figures and process page images. |
| Ollama, GLM-OCR, and Qwen3 4B | Run OCR and advisory proofreading locally. |
| lxml and python-pptx | Support structured XML processing and PowerPoint inspection and updates. |
| LibreOffice | Optionally renders slides for previews and visual checks. |
| pytest | Runs the automated Python tests. |

## Get started on Windows

1. Install Python 3.11, Git with Git LFS, and Ollama. Install LibreOffice if you want slide previews and render-based checks. Open PowerShell and confirm that `python --version` reports Python 3.11 and that `git lfs version` and `ollama --version` work. If you select Python 3.11 through the Windows Python launcher, use `py -3.11` in place of `python` for the environment creation commands below.

2. Clone the repository if you need a local copy, then open the `Logic_LAB` folder in PowerShell.

   ```powershell
   git clone https://github.com/AKCadhikari/Logic_LAB.git
   Set-Location Logic_LAB
   ```

3. From the repository root, fetch the model checkpoint and the DocLayout-YOLO submodule.

   ```powershell
   git lfs install
   git lfs pull
   git submodule update --init -- hackathon-project/third_party/DocLayout-YOLO-release-v2
   ```

4. Create the workspace and layout-analysis environments. Run the remaining setup commands from `hackathon-project`.

   ```powershell
   Set-Location hackathon-project
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r project\requirements.txt
   python -m venv .venv-doclayout
   .\.venv-doclayout\Scripts\python.exe -m pip install .\third_party\DocLayout-YOLO-release-v2
   ```

5. Open the Ollama app so its local service is running, then download the models and start DocuSync AI.

   ```powershell
   ollama pull glm-ocr:q8_0
   ollama pull qwen3:4b
   .\Start_DocuSync_AI.bat
   ```

6. The launcher opens [DocuSync AI on localhost](http://127.0.0.1:8765/). Check **AI startup** in the workspace for model status. If startup fails, open `hackathon-project/logs/startup.log`.

After the first setup, run `.\Start_DocuSync_AI.bat` from `hackathon-project` to reopen the workspace. Run `.\Start_DocuSync_AI.bat -Action Stop` to shut it down. See the [project guide](hackathon-project/README.md) for environment overrides and [local OCR setup](hackathon-project/docs/LOCAL_OCR.md) for PaddleOCR, Docling, and Tesseract.

## Use the workspace

1. Add the source PDF and its PowerPoint in either order. Once both files upload, DocuSync starts one job and uses the saved PDF as evidence for repairs.
2. Use the **PDF** tab to inspect the source, zoning, reading order, text, metadata, and XML. The shared timeline tracks extraction, slide repair, validation, and packaging.
3. Switch to **PowerPoint** to inspect the repaired slides and review the correction details. Use the preview selector to compare with the original when both are available.
4. Choose **Download Final Output (ZIP)** from either tab. The archive contains the validated XML, repaired deck, and assets referenced by the XML.

If the job pauses for source review, check the affected pages and submit the review to resume the saved job. For blocking content or validation failures, reprocess the saved inputs after addressing the reported issue.

**Processing History** lets you reopen documents and completed jobs. The browser's local workspace session controls access to its uploads and history.

### Bulk Processing

1. Open **Bulk Processing** from the workspace header. Select Phase 1 or Phase 2 and choose the OCR settings for the batch.
2. Choose a parent directory or drop folders onto the upload area. Put one source PDF and one PowerPoint in each document folder. The filenames can differ from one folder to the next.
3. Review the detected pairs. **File issues** lists missing files, ambiguous pairs, and unexpected or invalid files. Choose **Start bulk processing** to queue the valid pairs.
4. Use the queue to track PDF or slide progress, cancel pending jobs, retry failed or partial jobs, or open **View results** to inspect a pair. If source review is required, submit it in the document workspace.
5. Download an individual XML or PPTX, or choose **Download All Results** for a ZIP with the original folder names and a report of the batch. The ZIP includes successful outputs from partial jobs and any image assets referenced by their XML.

Bulk exports are versioned under `hackathon-project/phase-1/output/bulk/` or `hackathon-project/phase-2/output/bulk/`. **Bulk history** reopens batches for the same browser session. Each batch accepts up to 500 folders and 2,000 files, with each PDF or PPTX under 64 MB. Browser folder pickers may omit empty folders. If DocuSync was already running before the update, use the launcher’s restart command to load the bulk routes.

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

The repair command and paired workspace use the uploaded PDF as the source for PowerPoint edits. The engine aligns slides to source passages, preserves existing slide objects where possible, and reopens the saved deck to check its package and approved changes. When LibreOffice is available, it renders the whole deck, checks changed content, and retries a safe image fallback or reverts failed slide edits.

The sibling `corrected_reports/` folder contains the HTML QA report, the saved and unresolved correction records, the slide-to-source page mapping, and an equation catalogue with LaTeX, MathML, confidence where available, and PDF crops. The paired export also writes `reports/equations.json` alongside the XML, with IDs and relative image paths that link the two. The final ZIP contains the validated XML, repaired PPTX, and referenced images.

Run-level repairs cover supported visible text and tables. A unique gap within a matching PDF bullet list can be restored if it fits the existing shape. Supported OMML edits preserve editable PowerPoint mathematics. Recognized equation pictures can receive an editable Office alternative and a verified PDF image fallback. Complex equations, chart workbooks, SmartArt, ambiguous matches, and render differences can require manual verification.

GLM-OCR is the standard local visual recognizer. To corroborate equations with a second local model, install `project/requirements-formula-ocr.txt` into a separate environment and set `DOCUSYNC_FORMULA_PIX2TEX_CHECKPOINT` to the absolute path of an approved pix2tex checkpoint. Set `DOCUSYNC_FORMULA_PIX2TEX_PYTHON` to that environment's Python executable. The recognizer uses locally provisioned weights. Optional `--ai-review` adds local advisory review of unresolved slide findings.

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

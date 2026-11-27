# Local OCR and layout

The upload and batch entry points share one pipeline. PyMuPDF inspects each page for native character
count, image coverage, and signs of a broken text layer. Reliable native text goes directly to layout.
The workspace offers GLM-OCR, PaddleOCR-VL (Ollama), PaddleOCR (Python), and Automatic. Automatic
defaults to GLM-OCR, PaddleOCR-VL, then PaddleOCR Python. For PDF pages with missing Unicode mappings,
Automatic first tries the local Tesseract pack for the detected page language when available. For Arabic
this is `ara.traineddata`. The layout pipeline orders right-to-left columns from right to left. If recognition attempts fail, the page stays
in the IR and draft XML for review. The selected mode, each executed model, and the accepted output
are recorded separately.

## Windows setup

Run these commands from `hackathon-project` with Python 3.11 and Ollama installed. Open the Ollama
app or run `ollama serve` if the server is stopped.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r project\requirements.txt
ollama list
.\.venv\Scripts\python.exe -B project\ui\server.py
```

Open `http://127.0.0.1:8765`. The local `/api/health` endpoint shows the configured models and
their readiness checks. GLM-OCR receives a page image through `/api/generate` with the `Text Recognition:`
prompt. The adapter preserves the raw reply and parses text, tables, and formulas. The image service
also accepts table and formula crops with their respective recognition prompts. Page numbers come
from the app. Recognition confidence and region coordinates are nullable because the Ollama response
provides text without those fields. [Ollama model](https://ollama.com/library/glm-ocr) ·
[GLM-OCR Ollama guide](https://github.com/zai-org/GLM-OCR/blob/main/examples/ollama-deploy/README.md)

### PaddleOCR-VL through Ollama

The default tag is `MedAIBase/PaddleOCR-VL:0.9b`. The installed release advertises completion only and
rejects image requests with a missing-projector error. The adapter verifies image capability before
submitting a document. The dashboard's synthetic test can verify the actual rejection without document
content. Configure a separately installed vision-capable build through `PADDLE_VL_MODEL`. DocuSync
loads secondary vision models on demand. Its PaddleOCR-VL inference defaults to CPU and a zero
keep-alive so the runner can release memory when the request finishes.
[Installed Ollama model listing](https://ollama.com/MedAIBase/PaddleOCR-VL:0.9b)

### PaddleOCR Python

Install the [Microsoft Visual C++ x64 Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist/)
on Windows. Stop the app before installing Python packages that replace OpenCV.

```powershell
.\.venv\Scripts\python.exe -m pip install -r project\requirements-ocr.txt
$paddleRoot = Resolve-Path .venv
foreach ($model in 'PP-OCRv5_mobile_det', 'latin_PP-OCRv5_mobile_rec') {
    $modelDir = Join-Path $paddleRoot "ocr-models\$model"
    New-Item -ItemType Directory -Force $modelDir | Out-Null
    foreach ($file in 'inference.yml', 'inference.json', 'inference.pdiparams') {
        curl.exe -fL "https://huggingface.co/PaddlePaddle/$model/resolve/main/$file" -o (Join-Path $modelDir $file)
        if ($LASTEXITCODE -ne 0) { throw "Model download failed for $model/$file" }
    }
}
.\.venv\Scripts\python.exe -c "import paddle; print(paddle.__version__)"
```

These weights provide the default Latin recognizer. For other scripts, provision the compatible detector
and recognizer and set `DOCUSYNC_PADDLE_DET_MODEL_DIR` and `DOCUSYNC_PADDLE_REC_MODEL_DIR`.
`DOCUSYNC_PADDLE_OCR_LANG` can override the PDF language hint. The fallback records recognition scores
and polygon bounds mapped to PDF points. [PaddleOCR models](https://www.paddleocr.ai/main/en/version3.x/module_usage/text_recognition.html)

### Docling layout

Use a separate environment for Docling and its OpenCV dependencies. The app discovers `.venv-docling`
automatically. Set `DOCUSYNC_DOCLING_PYTHON` for a different interpreter or `DOCUSYNC_DOCLING_ARTIFACTS`
for a different weights directory.

```powershell
python -m venv .venv-docling
.\.venv-docling\Scripts\python.exe -m pip install -r project\requirements-docling.txt
.\.venv-docling\Scripts\docling-tools.exe models download layout tableformer --output-dir .venv\ocr-models\docling
```

Docling processes one page at a time using local layout and table models. It preserves regions, parent
and caption references, and detected table grids as evidence. DocuSync applies supported labels and
regions and uses Docling reading order when all OCR blocks can be matched. The existing reconstruction
handles missing or ambiguous regions. [Docling offline setup](https://docling-project.github.io/docling/usage/advanced_options/)

## Processing and review

The review screen shows live job progress, native character counts, individual OCR attempts, the selected
engine, layout status, and review flags. Elapsed time comes from the job start timestamp. Extraction ETA
uses completed pages of the same type when enough measurements exist. Audit reports contain hashes and
metadata; approved local debugging can retain raw responses and Qwen prompts with
`DOCUSYNC_AUDIT_PRIVATE_RESPONSES=true`.

The quality gates check empty or truncated text, repetition, corrupt characters, page references,
table structure, and formula delimiters. A recognition score does not verify facts.
The Pydantic model validates page ordering, bounds, confidence, and table indices before XML generation.
XML validation is required for downstream processing. Use `--profile sample` for the official example
hierarchy. The UI and CLI default to the written brief profile. See the
[official sample validation](../../docs/OFFICIAL_SAMPLE_VALIDATION.md) for known differences.

Version comparison and PowerPoint sync keep PDF page and XML references. The workspace sends up to four
prioritized changes per request to Qwen via `/api/chat` at temperature zero. It checks the JSON against
a Pydantic schema and uses the returned classifications to display advisory review explanations.
Numerical values displayed alongside the suggestions come from the deterministic comparison. The
existing source and slide matching rules govern which edits can be applied.
Tesseract can independently corroborate source text during deck repair. Install Tesseract with your source
language packs and set `TESSERACT_CMD` if required. The explicit legacy extraction backend is available
through `--ocr-backend tesseract`.

These PowerShell environment variables override the JSON configuration and the older per-service settings.

```powershell
$env:OLLAMA_BASE_URL = 'http://localhost:11434'
$env:GLM_OCR_MODEL = 'glm-ocr:q8_0'
$env:QWEN_MODEL = 'qwen3:4b'
$env:OLLAMA_TIMEOUT = '300'
$env:OLLAMA_KEEP_ALIVE = '0'
```

These are the defaults. A keep-alive of `0` requests immediate unloading after inference. Omit the shared
override to use different durations through `DOCUSYNC_GLM_OCR_KEEP_ALIVE` and
`DOCUSYNC_OLLAMA_KEEP_ALIVE`. Set `DOCUSYNC_OLLAMA_ENABLED=false` to disable Qwen review.
Runtime controls also include `DOCUSYNC_GLM_OCR_RETRIES`, `DOCUSYNC_GLM_OCR_MAX_EDGE`,
`DOCUSYNC_PADDLE_OCR_TIMEOUT_S`, and `DOCUSYNC_DOCLING_TIMEOUT_S`, along with the OCR thresholds in
`project/config/settings.py`. The CPU profile processes one document at a time by default.
The workspace supports retrying a completed or failed job.

```powershell
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider project/tests -q
.\.venv\Scripts\python.exe -B project\sample_check.py --help
```

To compare the installed GLM-OCR variants, run the [official sample command](../../docs/OFFICIAL_SAMPLE_VALIDATION.md#reproduce)
with `GLM_OCR_MODEL=glm-ocr:q8_0` and then with `GLM_OCR_MODEL=glm-ocr:latest`, using a fresh output
directory each time. Compare per-page attempts and OCR times in `summary.json` alongside XML and deck scores.

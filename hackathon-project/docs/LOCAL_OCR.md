# Local OCR and layout

The upload and batch entry points share one pipeline. PyMuPDF inspects each page for native character
count, image coverage, and signs of a broken text layer. Reliable native text goes directly to layout.
GLM-OCR processes pages that need recognition. PaddleOCR runs after a GLM-OCR error, timeout, or failed
quality check. If both attempts fail, the page stays in the IR and XML and the source stays available for review.

## Windows setup

Run these commands from `hackathon-project` with Python 3.11 and Ollama installed. Open the Ollama
app or run `ollama serve` if the server is stopped.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r project\requirements.txt
ollama list
ollama pull glm-ocr:q8_0
.\.venv\Scripts\python.exe -B project\ui\server.py
```

Open `http://127.0.0.1:8765`. The local `/api/health` endpoint shows the configured models and
their readiness checks. GLM-OCR receives a page image through Ollama's vision API with the `Text Recognition:`
prompt. The adapter preserves the raw reply and parses text, tables, and formulas. Page numbers come
from the app. Recognition confidence and region coordinates are nullable because the Ollama response
provides text without those fields. [Ollama model](https://ollama.com/library/glm-ocr) ·
[GLM-OCR Ollama guide](https://github.com/zai-org/GLM-OCR/blob/main/examples/ollama-deploy/README.md)

### PaddleOCR fallback

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
engine, layout status, and review flags. JSON in the job's `recognition/` folder contains page-keyed OCR
and layout evidence. Keep this folder private alongside the source PDFs.

The quality gates check empty or truncated text, repetition, corrupt characters, page references,
table structure, and formula delimiters. A recognition score does not verify facts.
The Pydantic model validates page ordering, bounds, confidence, and table indices before XML generation.
XML validation is required for downstream processing. Use `--profile sample` for the official example
hierarchy. The UI and CLI default to the written brief profile. See the
[official sample validation](../../docs/OFFICIAL_SAMPLE_VALIDATION.md) for known differences.

Comparison and PowerPoint changes keep PDF page and XML references. Uncertain edits go to review.
Tesseract can independently corroborate source text during deck repair. Install Tesseract with your source
language packs and set `TESSERACT_CMD` if required. The explicit legacy extraction backend is available
through `--ocr-backend tesseract`.

Settings accept a JSON file through `--config` or a `DOCUSYNC_` environment variable. Runtime controls
include `GLM_OCR_TIMEOUT_S`, `GLM_OCR_RETRIES`, `GLM_OCR_MAX_EDGE`, `PADDLE_OCR_TIMEOUT_S`,
`DOCLING_TIMEOUT_S`, and the OCR thresholds in `project/config/settings.py`. The CPU profile processes
one document at a time by default. The workspace supports retrying a completed or failed job.

```powershell
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider project/tests -q
.\.venv\Scripts\python.exe -B project\sample_check.py --help
```

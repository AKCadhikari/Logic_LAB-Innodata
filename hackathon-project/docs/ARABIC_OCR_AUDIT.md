# Arabic OCR and RTL verification

## Baseline and reference

The supplied estimate was approximately 30% accuracy. Its numerator, denominator and scoring rules
were unspecified. This audit uses CER and WER, calculated as Levenshtein edits divided by reference
Unicode code points and whitespace-delimited words, respectively. It collapses whitespace and retains
digits, punctuation and diacritics. Aggregate scores weight each region by its reference length.

The local `PL202611161.pdf` is a two-page printed Arabic gazette with large headings, small body text,
dates, and right-to-left columns. Five regions were transcribed from the rendered pages and encoded
with their rectangles in `project/arabic_ocr_check.py`. These are sampled regions, with no independent
second transcription or full-page reference. A separate authored, image-only mixed-language fixture
contains English text, a date, and both digit systems. The audit kept the PDF, renders, extracted XML,
raw model outputs where captured, per-page and per-region scores, and timings under
`storage/arabic-audit`. The report records the PDF SHA-256.

## Pipeline audit

1. The starter launcher delegates to the full workspace launcher. The workspace runs its PDF pipeline
   in `.venv`. PyMuPDF diagnoses the native text layer, tracks PDF rotation and image coverage, and
   extracts native text when the layer passes its health checks. The gazette had 352 and 207 Unicode
   mapping failures on pages 1 and 2. Visual inspection also found misdecoded Arabic letters and
   displaced dates in the extracted text.
2. DocLayout-YOLO runs in `.venv-doclayout` on rendered pages. Its boxes are mapped from pixels to
   PDF points, taking the page rotation into account. It detected 70 and 67 regions on the gazette.
   The extraction pipeline uses detections for region association, structure classification, visual
   crops and captions. The main GLM and Paddle adapters receive page images. Paddle's text detector
   creates recognition crops internally. The Arabic reconstruction now also uses layout regions to
   avoid joining fragments from different detected regions or table cells.
3. The default full-page GLM adapter rendered the large pages at about 113 effective DPI because of
   its 1,800-pixel cap. The `glm-ocr:q8_0` runs produced excessive repetition and incomplete responses
   on both pages. The pipeline rejected both and retained the damaged native layer. The GLM adapter
   has a generic recognition prompt. Its visible Arabic output confirms some capability, with weak
   results on these pages. [GLM-OCR project](https://github.com/zai-org/GLM-OCR)
4. The application hard-coded the Arabic v3 model path. That path was absent, while Arabic v5 weights
   were already installed. The PaddleOCR 3.7.0 implementation resolves `lang="ar"` to a v5 server
   detector and `arabic_PP-OCRv5_mobile_rec`. The application explicitly pairs that verified Arabic
   recognizer with the installed mobile detector for offline CPU inference. The model loaded and
   returned Arabic text and positions. Its dictionary contains Latin letters, Arabic marks, and both
   digit systems. Accuracy on mixed lines varied considerably. [Official PaddleOCR model list and language mappings](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/OCR.html)
5. The installed `MedAIBase/PaddleOCR-VL:0.9b` Ollama tag advertises completion only. An image probe
   returned HTTP 500 and reported the missing multimodal projector. The upstream 0.9B model lists
   Arabic support. Local Arabic CER and WER for this tag are unavailable. [Upstream PaddleOCR-VL model card](https://huggingface.co/PaddlePaddle/PaddleOCR-VL)
6. Paddle sometimes returned a single heading as several independently positioned words.
   Joining compatible adjacent RTL detections before the existing gutter-based reading-order pass
   reduced heading reordering. Character strings from each detection stay in their original Unicode
   order. The XML serializers use UTF-8 and direction attributes. The HTML preview uses browser
   bidirectional rendering, and native Arabic tables follow their right-to-left grid order even when
   the first physically stored cell is English. [Unicode Bidirectional Algorithm](https://www.unicode.org/reports/tr9/)

## CPU comparison

The scores below are for the same five gazette regions. Crop and full-page timings cover different
workloads. The crop-corpus runs also timed the separately scored authored scan. The measurements
include model startup where it occurred and reflect a shared development machine.

| Configuration | CER | WER | Wall time | Scope |
|---|---:|---:|---:|---|
| Native text retained by the original GLM-only workflow | 32.15% | 58.57% | — | Region samples from the embedded layer |
| Current GLM-OCR Q8, 200 DPI requested | 59.92% | 94.29% | 41.32 s | Five gazette crops and one authored scan |
| Paddle Arabic v5, 300 DPI grayscale, raw region order | 20.25% | 35.71% | 16.00 s | Same crops |
| Paddle Arabic v5, 300 DPI grayscale, reconstructed lines | 3.97% | 15.71% | 16.00 s | Same OCR responses, spatial line joins |
| Paddle Arabic v5, 300 DPI, full pages | — | — | 180.16 s | Both gazette pages hit the 90-second timeout |
| Tesseract Arabic pack, 260 DPI | 12.53% | 18.57% | 10.56 s | Both full pages, scored on the five reference regions |
| Installed PaddleOCR-VL | — | — | — | Local runtime rejected image input |

GLM-only rejected both full pages after 195.67 and 193.31 seconds of recognition. Its full gazette
pipeline took 428.19 seconds. The Arabic-aware pipeline accepted `ara` recovery for both over
13.35 seconds of extraction, and produced XML in 68.33 seconds end to end with layout enabled.
The existing Automatic workflow also had Arabic Tesseract recovery for this class of damaged PDF.
The improvement on the gazette is relative to the GLM-only workflow.

The field check compared four visually transcribed strings from the right column on page 1.
The full-page Tesseract result matched none of the four verbatim. Numeric disagreements with the
embedded layer now trigger source review. No punctuation, dates, case numbers, or amounts are
rewritten by a language model.

## Rendering and preprocessing

| Paddle Arabic crop input | CER with spatial line joins | WER with spatial line joins |
|---|---:|---:|
| 200 DPI grayscale | 5.22% | 20.00% |
| 300 DPI grayscale | 3.97% | 15.71% |
| 400 DPI grayscale | 5.64% | 20.00% |
| 300 DPI plus CLAHE | 3.97% | 15.71% |
| 300 DPI plus median denoising | 3.76% | 14.29% |
| 300 DPI plus Otsu binarization | 3.76% | 18.57% |
| 300 DPI plus the existing deskew and contrast routine | 4.18% | 15.71% |

300 DPI grayscale is the Arabic Paddle render target, subject to the bounded page pixel count.
Contrast processing tied grayscale, while Otsu increased word error after reading-order repair.
Median denoising made a small gain on these five printed regions. Grayscale remains the production
input for Paddle to retain fine marks on unseen scans. The Tesseract scan fallback uses the existing
orientation and deskew routines. A controlled rotation of the gazette produced a correct 180° result
from orientation detection, and deskew reduced an estimated −3.23° angle to 0°.

The authored mixed scan measured 76.92% CER and 81.25% WER with the old GLM configuration, 38.46%
CER and 87.50% WER through the Arabic Paddle pipeline, and 19.78% CER and 31.25% WER with
`ara+eng` at 200 DPI. That measurement supports using the two packs first in **Mixed Arabic and
English** mode. It still produced incorrect brackets, date ordering and numbers. Arabic-only scans
try Paddle Arabic first, then the Arabic Tesseract pack if needed. Both named language choices
retain the established GLM and PaddleOCR-VL fallbacks and use native text where available.

## Verification and scope

- The generated XML uses logical Unicode and right-to-left direction attributes. Tests cover
  mixed-script text and digits, two RTL columns, a rotated PDF coordinate system, table column
  ordering, and a document combining native English and Arabic OCR pages.
- No dependency or model installation changed. PaddleOCR 3.7.0 and PaddlePaddle 3.3.1 were already
  in the existing `.venv`. Docling 2.150.1 is in `.venv-docling`. The DocLayout-YOLO subprocess
  continues to use `.venv-doclayout`.
- Arabic OCR outputs and coordinates are saved locally in the run's `recognition` folder.
  Low-confidence regions and inconsistent mixed-direction text are surfaced for review.
  The fallback keeps the provisional text when models disagree on numeric values.
- Further validation requires a larger independently transcribed sample, particularly Arabic
  scans, legal fields and scanned tables. Paddle produces positioned text lines. Scanned table
  cells may still need manual reconstruction. High recognizer confidence does not establish
  numeric or textual accuracy.

## Changed files and reproduction

- Language options and routing are in `project/config/settings.py`, `project/main.py`,
  `project/ocr/paddle.py`, `project/ocr/router.py`, and `project/agents/orchestrator.py`.
- Layout and direction are in `project/layout/reconstruct.py` and `project/diagnostics/profile.py`.
- The language controls and diagnostics are in `project/ui/workspace.py`,
  `project/ui/workspace_routes.py`, and `project/ui/static/{index.html,home.js,workspace.js}`.
- The benchmark is `project/arabic_ocr_check.py`. Regression coverage is in
  `project/tests/test_arabic_ocr.py`. Setup is documented in `docs/LOCAL_OCR.md`.

Run from `hackathon-project` with the original local sample. Use a new output directory for each run.

```powershell
.\.venv\Scripts\python.exe -B project/arabic_ocr_check.py phase-1/input/PL202611161.pdf storage/arabic-repeat
.\.venv\Scripts\python.exe -B project/arabic_ocr_check.py phase-1/input/PL202611161.pdf storage/arabic-full-repeat --models paddle tesseract --full-pages-only --page-dpi 300
.\.venv\Scripts\python.exe -B -m pytest -q
```

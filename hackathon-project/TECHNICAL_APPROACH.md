# Technical approach

The batch pipeline and the Flask workspace share the same [local OCR pipeline](docs/LOCAL_OCR.md).
GLM-OCR uses the loopback Ollama API when a page requires recognition. Browser version comparison can also
request optional local Ollama hints for uncertain pairs.

The current [official sample validation](../docs/OFFICIAL_SAMPLE_VALIDATION.md) records the evidence-based
PowerPoint changes and the legacy XML sample profile. The historical repair rules described below
predate the package-preserving repair engine.

## 1. Design principles
* **Never trust the native text layer.** Each page is scored; broken layers are replaced by OCR, doubtful ones are
  compared against OCR and the more reliable text wins.
* **CPU-first, offline, no data leaves the machine.** No cloud OCR/LLM calls; the competition files are confidential.
* **Evidence-based decisions.** GLM-OCR proposes page content. Quality checks, source references, and independent
  corroboration govern whether that content is accepted and whether it can support a deck edit.
* **Precision-first repairs.** A wrong "correction" is worse than a flagged one: uncertain changes are logged for review.
* **Never lose a page / never stop the batch.** Page-, document- and deck-level isolation with fallbacks; every decision is
  written to `audit.jsonl`.

## 2. Pipeline and components
| Stage ("agent") | Module | Notes |
|---|---|---|
| Ingestion | `ingestion/discover.py` | recursive discovery, header validation, SHA-1, duplicates, manifest |
| Document analysis | `diagnostics/page_diagnostics.py` | control chars, private-use glyphs, mojibake, word plausibility, image coverage, scripts |
| OCR | `ocr/router.py`, `ocr/glm.py`, `ocr/paddle.py`, `ocr/quality.py` | GLM-OCR via local Ollama with quality checks and page-level PaddleOCR fallback |
| Layout | `layout/docling_adapter.py`, `layout/reconstruct.py`, `layout/kinds.py` | Docling regions and reading order, native geometry, deterministic reconstruction |
| Shared IR | `layout/model.py` | Pydantic document, page, block, cell, span, coordinates, extraction method and confidence |
| XML | `xml_engine/` | `lxml` serializer and validation for the brief and sample profiles |
| Comparison | `comparison/differ.py` | token alignment engine + version comparison (added/deleted/modified/moved) |
| PowerPoint | `powerpoint/amend.py` | evidence matching, run-level edits, footer/breadcrumb/slide-number repair, missing-box restoration |
| Quality | `quality/` | heuristic confidence, XML checks, evaluators against reference XML/PPTX |
| Orchestrator | `agents/orchestrator.py` | shared state, delegation, time budget, parallelism, failure isolation |
| Reporting / UI | `reporting/`, `ui/` | JSON + HTML report; offline Flask review dashboard |

The orchestrator coordinates the processing stages. The benchmark baseline is the plain text-layer extractor
(`tests/baseline_native.py`).

## 3. Key algorithms
* **Page health**: weighted signals (control ratio > 4 % or private-use > 20 % => corrupt; word-likeness; mojibake patterns).
  Suspicious pages are OCR'd and the native text is kept only if token-set similarity with OCR >= 0.55 (Arabic digital pages
  survive this test, whereas OCR is worse there).
* **Language**: script counts plus stop-word and diacritic scoring guide recognizer selection. PDF language hints and the
  configured PaddleOCR recognizer can override that inference.
* **Reading order**: recursive XY-cut over lines and table/figure boxes; vertical gutters >= max(5 pt, 1.2 % width); full-width
  items are treated as separators so they cannot hide a column gutter; RTL reverses column order.
* **Deck repair**: each slide paragraph is matched to the best source passage (fuzzy partial match, page hint from speaker notes
  when present), then token-aligned. Rules, tuned for precision on the one reference deck: punctuation and function-word
  differences are never rewritten (slides are condensed versions of the paper); replacements need a small edit in a near-verbatim
  passage; number changes are applied; OCR evidence needs corroboration; Unicode-compatibility variants (superscripts,
  ligatures) are ignored; Arabic/CJK/Hangul text is review-only unless the change is a number (`amend_nonlatin_auto` opts in).
* **Consistency repairs**: truncated repeated footers, breadcrumbs missing numbered ancestors, hard-coded slide numbers, and
  missing body boxes (cloned from peers of the same layout, filled from the PDF section whose heading matches the slide title;
  divider slides that are parents of other numbered slides are skipped).

## 4. Schema discrepancy (needs an organiser decision)
The written brief specifies an HTML vocabulary with `<page n=...>`; the example XML uses a different, legacy-style vocabulary
(`page_header`, `figure_wrap`, `all-caps`, `<page num=.../>` milestones, `lang="english"`). The serializer follows the written
brief by default; `--profile example` and `lang_format=name` switch the page attribute/dimensions/language format. The `kind`
vocabulary (title, byline, affiliation, abstract, keywords, section_heading, body, figure_caption, reference_entry, ...) is
inferred from the single example. Output names: `<pdf name>.xml`; decks follow the example pattern unless named `amendment<N>`.

## 5. Declarations
* **AI tools used for development**: Claude (Anthropic) was used as a coding/design assistant for this project. *Edit this line
  to list exactly the assistants your team used, as the rules require.*
* **Runtime models** are GLM-OCR, the provisioned PaddleOCR recognizer and detector, and optional Docling layout and table
  weights. Tesseract language data support the legacy OCR backend and independent deck-edit verification.
* **Third-party components and licences**: PyMuPDF (AGPL-3.0 or commercial - acknowledge and confirm acceptability with the
  organisers), Tesseract + tessdata (Apache-2.0), OpenCV (Apache-2.0), python-pptx (MIT), lxml (BSD), RapidFuzz (MIT),
  NumPy (BSD), Flask (BSD), pytest (MIT).
* **Data handling**: processing is local. Competition documents are confidential; `phase-*/input/` is git-ignored and the
  repository must be private.

## 6. Limitations / future work
Scanned table and formula coverage depends on the recognized structure. Figure alt text is heuristic. Deck repair
precision uses the single official reference deck. Windows 11 / Ryzen 3 performance remains unmeasured.

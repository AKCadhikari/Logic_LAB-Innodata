# Technical approach

## 1. Design principles
* **Never trust the native text layer.** Each page is scored; broken layers are replaced by OCR, doubtful ones are
  compared against OCR and the more reliable text wins.
* **CPU-first, offline, no data leaves the machine.** No cloud OCR/LLM calls; the competition files are confidential.
* **Deterministic stages over LLMs.** Every step is classical Python/CV; results are reproducible and auditable.
* **Precision-first repairs.** A wrong "correction" is worse than a flagged one: uncertain changes are logged for review.
* **Never lose a page / never stop the batch.** Page-, document- and deck-level isolation with fallbacks; every decision is
  written to `audit.jsonl`.

## 2. Pipeline and components
| Stage ("agent") | Module | Notes |
|---|---|---|
| Ingestion | `ingestion/discover.py` | recursive discovery, header validation, SHA-1, duplicates, manifest |
| Document analysis | `diagnostics/page_diagnostics.py` | control chars, private-use glyphs, mojibake, word plausibility, image coverage, scripts |
| OCR / CV | `ocr/engine.py` | Tesseract via subprocess (TSV); OSD orientation, deskew (scans only), CLAHE, sparse retry, RTL word order, trial language selection, cache |
| Layout | `layout/native.py`, `layout/reconstruct.py`, `layout/kinds.py` | XY-cut with wide-item handling, per-script font statistics, headings/lists/footnotes/captions/equations, tables via PyMuPDF with span grid, figures, `kind` |
| XML | `xml_engine/` | `lxml` serializer with profiles (`brief`, `example`), validator |
| Comparison | `comparison/differ.py` | token alignment engine + version comparison (added/deleted/modified/moved) |
| PowerPoint | `powerpoint/amend.py` | evidence matching, run-level edits, footer/breadcrumb/slide-number repair, missing-box restoration |
| Quality | `quality/` | heuristic confidence, XML checks, evaluators against reference XML/PPTX |
| Orchestrator | `agents/orchestrator.py` | shared state, delegation, time budget, parallelism, failure isolation |
| Reporting / UI | `reporting/`, `ui/` | JSON + HTML report; offline Flask review dashboard |

**Multi-agent claim.** The "agents" are sequential, deterministic workers coordinated by one orchestrator. No LLM is used and
no benefit of multiple agents is claimed; the benchmark baseline is the plain text-layer extractor (`tests/baseline_native.py`).

## 3. Key algorithms
* **Page health**: weighted signals (control ratio > 4 % or private-use > 20 % => corrupt; word-likeness; mojibake patterns).
  Suspicious pages are OCR'd and the native text is kept only if token-set similarity with OCR >= 0.55 (Arabic digital pages
  survive this test, whereas OCR is worse there).
* **Language**: script counts + stop-word/diacritic scoring for 10 languages; for unknown scanned documents the OCR pack is
  chosen by trial (weighted mean confidence on a low-DPI render).
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
* **Runtime models**: none. OCR uses Tesseract's pre-trained language data; there is no LLM, embedding or vision model at run time.
* **Third-party components and licences**: PyMuPDF (AGPL-3.0 or commercial - acknowledge and confirm acceptability with the
  organisers), Tesseract + tessdata (Apache-2.0), OpenCV (Apache-2.0), python-pptx (MIT), lxml (BSD), RapidFuzz (MIT),
  NumPy (BSD), Flask (BSD), pytest (MIT).
* **Data handling**: processing is local. Competition documents are confidential; `phase-*/input/` is git-ignored and the
  repository must be private.

## 6. Limitations / future work
Scanned pages: no tables/handwriting/stamps/strike-through; figure alt text is heuristic (a local VLM can be plugged into
`layout/figures.describe(..., describer=...)` if approved); restored deck boxes are approximate; precision of the deck repair
is measured on one deck only; quality on other documents is unmeasured (no references); Windows 11 / Ryzen 3 performance
has not been measured (benchmark ran on a 1-core Linux VM; see docs/BENCHMARKS.md).

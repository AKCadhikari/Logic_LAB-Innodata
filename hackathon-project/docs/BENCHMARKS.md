# Benchmarks (measured with `project/tests/benchmark.py`; raw numbers in `benchmark_results.json`)

## Advanced correction verification

The Windows run processed the 12-page engineering PDF with local GLM-OCR and rendered all 40 output slides with LibreOffice.
The correction engine used the original source PDF. The engineering XML and QA deck were supplied after export for comparison.

| Measure | Observed result |
| --- | --- |
| Cold PDF extraction | 1,113.99 s |
| PowerPoint repair and QA | 18.97 s |
| Engineering XML text similarity | 0.981 |
| XML reading-order Spearman correlation on matched units | 0.761 |
| XML assets verified inside the final ZIP | 14 |
| Exact paragraph matches against the engineering deck | 210/334 before, 215/334 after |
| Saved PowerPoint corrections | 18 package-verified, 9 independently confirmed in the rendered slides |
| Unexpected changes outside the verified edit plan | 0 package parts and 0 unchanged slide renders |
| Review or visual confirmation outstanding | 51 findings |

The sample yielded 26 structured equation entries, of which 12 had verified source transcriptions. One Office equation was
rebuilt structurally. The output deck retains its submitted structure, with 10 shapes from the engineering QA deck still
absent. Differences in the engineering references are comparison results and can require independent review against the PDF.

Two additional controlled fixtures seeded five errors across visible text, an omitted bullet, native OMML, and a formula
picture. All five were detected and corrected in the saved files. All three expected equation structures and all 24 checked
symbol and structural tokens matched. A live GLM-OCR run corrected the formula picture, saved an editable Office alternative,
and verified its image fallback through LibreOffice. These measurements describe the labeled fixtures. Broader error recall,
native Office visual fidelity, and independently labeled general-purpose formula accuracy remain to be measured.

## Historical Linux baseline

The [implementation roadmap](../../docs/IMPLEMENTATION_ROADMAP.md)
records the Windows synthetic run and browser verification separately. Windows process memory is now sampled
with psutil, so those memory figures use a different method.

Environment: Linux VM, **1 CPU core**, 3 GB RAM, Python 3.12, Tesseract 5. Not the target hardware (Windows 11, Ryzen 3).

## Throughput (cold cache, 1 worker)
| | |
|---|---|
| Documents | 13 PDFs (125 pages, 40 OCR'd) + 13 PPTX |
| Wall time | **186.8 s** |
| Peak resident memory | 591 MB |
| Well-formed XML | 13 / 13, no validation issues |
| Decks reopened successfully | 13 / 13 |

The Phase 3 set size is unknown, so the 2-hour limit is only *indicated* (about 1.5 s per page including OCR here). Parallel
workers (`--workers`) and the time-budget fast mode (lower OCR DPI after 75 % of the budget) are implemented but their effect on
the target machine was not measured.

## Accuracy: XML vs the only golden reference (example document, text layer is corrupt)
| Metric | Text-layer baseline | DocuSync |
|---|---|---|
| Whole-document normalised edit similarity | 0.135 | **0.951** |
| Text-unit recall / precision / F1 (fuzzy >= 0.85) | 0.084 / 0.043 / 0.057 | 0.606 / 0.450 / 0.516 |
| Reading-order Spearman (matched units) | 0.995* | 0.836 |
| Characters (gold 24,296) | 7,384 | 24,654 |
*computed on very few matched units; not meaningful.
Unit F1 is low partly because the golden segments text differently (legacy tags, run-in headings). These are *proxy* metrics,
not the judges' harness. No table/figure reference exists for any document, so those scores are unmeasured.

## Deck repair vs the only reference deck (example)
| | Before repair | After repair |
|---|---|---|
| Exact-match paragraphs | 210 / 334 | **213 / 334** (no regressions) |
| Character similarity | 0.7454 | **0.7592** |
| Missing body boxes | 10 | 6 (4 restored, approximate) |

Approach and honesty note: the first rule set *reduced* quality (16 regressions vs 6 fixes) because reference slides are condensed
paraphrases of the paper; the final precision-first rules were tuned on this one deck, so generalisation to other decks is
unproven. Decks without a reference were spot-checked by hand only (number corrections looked right; non-Latin text is review-only).

## Tests
`python -m pytest tests -q` -> 21 passed (digital PDF, two columns/RTL, corrupted layer, OCR fallback, hostile characters,
unreadable PDF in a batch, serializer, tables/spans, naming/pairing, alignment rules, run-preserving edits, deck end-to-end,
version comparison).

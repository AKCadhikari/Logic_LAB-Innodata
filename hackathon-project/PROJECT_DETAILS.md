# DocuSync AI - project details

**Competition**: Innodata AI Catalyst 2026, Internal Employee Track - *Evolving Document Synthesis*.
**Language / runtime**: Python 3.11, CPU only, offline. **Entry point**: `project/main.py`.

## What it does
1. **Ingest** a folder of PDFs/PPTX: validate headers, hash, detect duplicates, write a manifest.
2. **Diagnose every PDF page**: native text health (control characters, private-use glyphs, mojibake, word plausibility),
   image coverage, rotation, scripts. Decision per page: `native`, `compare` (OCR and keep the more reliable text) or `ocr`.
3. **OCR** where needed (Tesseract, 10 languages): orientation (OSD), deskew for real scans, language-pack selection by trial
   when the document language is unknown, sparse-text retry, page-level cache.
4. **Reconstruct layout**: XY-cut reading order (multi-column, RTL aware), header/footer by repetition, headings (size, bold,
   numbering), lists, footnotes, captions, real tables with row/col spans, figures, inline `strong/em/sup/sub/del/a`,
   equations, `kind` attribute.
5. **Serialize + validate XML** with `lxml` (vocabulary, page numbering, language, well-formedness; never emits invalid XML).
6. **Proof-read the decks** against the PDF: evidence alignment, run-level edits, footer/breadcrumb/slide-number consistency,
   restoration of missing body boxes, change report with confidence + recommended action.
7. **Report**: audit log, per-document quality (heuristic confidence, not accuracy), HTML/JSON run reports, review dashboard.

## Results (measured; see docs/BENCHMARKS.md)
| Check | Result |
|---|---|
| All 12 sample PDFs + example (125 pages) | 13/13 well-formed XML, 40 pages OCR'd |
| Cold run, 1 CPU core, 591 MB peak | 187 s for PDFs + 13 decks |
| Example PDF (text layer is garbage) vs golden XML | text similarity 0.951 (text-layer baseline: 0.135) |
| Example deck vs reference deck | exact paragraphs 210 -> 213 of 334, no regressions; 4 missing boxes restored |

## Known limitations (honest list)
Scanned pages: no table structure, handwriting, stamps or strike-through detection. Figure `alt` text is heuristic (no
vision model shipped). Deck repair is evidence-based and precision-first: it cannot recover content the PDF does not
contain, restored body boxes are approximate (flagged for review), non-Latin deck text is review-only by default.
Arabic OCR is weaker than Latin. Only one reference XML/deck exists, so quality on other documents is unmeasured.
Validated on Linux only; Windows 11 steps above are untested by the author of this file.

# Local engineering-sample verification

Run date: 26 November 2026. Competition source files and generated outputs remain local in Git-ignored phase folders.

## Batch application run

Command:

```powershell
.\.venv\Scripts\python.exe -B project\main.py --phase 1 `
  --input phase-1\input `
  --output phase-1\output `
  --work-dir phase-1\docsync_run\compliance-20261126
```

The local authorized set contained one PDF, `20239005532129.pdf`, with 12 pages, and one paired deck, `20239005532129_errors.pptx`. The pipeline completed the PDF in **1,110.84 s**; total batch wall time was **1,112.06 s**. All 12 pages required OCR and used local **GLM-OCR**. The run completed with one PDF success and no failed PDF.

The batch report is `phase-1/docsync_run/compliance-20261126/run_report.json`; per-page IR is in `phase-1/docsync_run/compliance-20261126/ir/20239005532129.json`. The versioned bundle is:

`phase-1/output/20239005532129/e86999dede854c778b26ea2a33bfe840/`

XML and PPTX both passed export validation. The XML validation recorded 12 pages, a valid sample-profile XSD result, no missing/invalid linked assets, 14 figure assets, 20 detected formulas, 12 formula transcriptions and 8 formulas flagged for manual review. No table was recovered in this run.

## DocLayout-YOLO and downstream use

- Model: DocLayout-YOLO; checkpoint: `models/layout/universal/DocLayout-YOLO-universal.pt`.
- Runtime: `.venv-doclayout`, CPU, loaded once for the PDF; the model health endpoint returned the expected 22 class names and IDs.
- Inference: all 12 rendered pages completed. There were **188 detections**: text 77, title 22, header 5, footer 22, page number 11, image 2, image caption 3, equation 12, list 5, algorithm 1, code caption 1, reference text 27.
- Integration: **111 extracted blocks** carry a layout_region_id, evidence that detection regions were associated with downstream IR blocks. Docling structure matching was applied on pages 4 and 8; the other pages returned no safe matches.
- Example output: page 1, `text` class (ID 0), confidence 0.937, PDF box `[80.50, 386.49, 419.66, 482.34]`.

Detection-region counts by page: 30, 10, 12, 18, 18, 9, 18, 15, 15, 18, 18, 7. The model detects layout; GLM-OCR performed recognition.

## XML comparison with the engineering output

`quality.evaluate.compare` compared the generated XML with the local engineering reference `20239005532129.xml`:

| Metric | Result |
|---|---:|
| Normalized text similarity | 0.906 |
| Character error rate | 0.0938 |
| Word error rate | 0.163 |
| Unit recall / precision / F1 | 0.761 / 0.863 / 0.809 |
| Reading-order Spearman | 0.834 |

These are local proxy metrics, not the judges' scoring result. The reference uses its own segmentation and vocabulary, so text and order metrics do not assess visual fidelity or every element type.

## PPTX comparison with the local engineering outputs

The batch produced a valid 40-slide `20239005532129_updated.pptx`. Its report says `presentation_status: unchanged`, `applied: 0`, `review: 76`. The output text matches the local engineering `20239005532129_errors_updated.pptx` reference at **255/255 paragraphs** (exact rate 1.0, character similarity 1.0); the input deck scores the same against that reference. This sample therefore verifies pairing, no-op handling, output generation and file validation, but it does not demonstrate that the sample required a text repair. The 76 withheld review items are retained in `phase-1/docsync_run/compliance-20261126/changes/20239005532129_errors.json`: 61 require source-extraction verification, 13 have no matching source passage, and 2 have conflicting source matches.

The test suite separately exercises a supported numeric correction on a synthetic deck and confirms that the change is applied while the PPTX remains valid. Do not treat that synthetic case as evidence of accuracy on the competition decks.

## Live workspace verification

The Chrome/Playwright integration check passed against the updated local server. It uploaded the authorized single-page sample as Phase 2, confirmed the phase-specific input and versioned XML output paths, and exercised the DocLayout status and page overlay, region inspection, reading order, review submission, valid XML/PPTX downloads and metadata re-editing. It then processed a two-page subset with both GLM-OCR and PaddleOCR; both engines returned accepted text associated with DocLayout regions. The check also verified rotated-page coordinate mapping and that a missing checkpoint produces a real failed layout status while XML remains valid.

The browser test generated local bundles under `phase-2/output/compliance-20261126-202390-page1/` and `phase-2/output/compliance-20261126-202390-pages1-2/`. Inputs and outputs remain Git-ignored. The complete Windows test suite finished with **114 passed** in 189.63 seconds.

## Coverage limits

The authorized local folder provided one PDF/deck pair, not the full 12+12 Phase 1 set. Phase 2 selection and output routing were exercised with a small local sample subset, but no Phase 2 competition data or advancement status is available. There is no evidence here for the unseen Phase 3 PDFs, the demo presentation, or the required `project-demo.mp4`. All input and output samples are ignored by Git; confirm the designated repository's privacy and authorized reviewers before any submission upload.

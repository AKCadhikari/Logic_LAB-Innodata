# Official hackathon sample validation

## References and acceptance

The supplied archive for document `20239005532129` contains the source PDF, the original slides, a reference XML, and two output decks. I used `20239005532129_1333ffd5dc464ce9b5965c99c8232625_transformed_QA.pptx` as the transformed deck reference. The `_errors_updated.pptx` variant retains the original slide-level shape text, positions, types, placeholders and run formatting observed in the survey. The separate QA variant changes these properties.

The organizer's written PDF brief requires HTML-style XML with numbered page wrappers `<page n="…">`. The reference XML instead uses a document header and nested body sections, `<page num="…"/>` milestones, language names, and specialized tags for references, figures and typography. DocuSync's default `brief` profile follows the written rules. The explicit `sample` profile emits the conventions observed in the supplied XML for side-by-side comparison.

**Exact matching remains open.** Automatic deck repair is limited to attributable text changes. The reference QA deck converts 39 literal slide numbers into DrawingML fields with the cached value `‹#›`, adds ten text shapes, and changes package parts beyond the slides. The generated deck preserves the source's slide numbers, shapes, slide order, layouts, masters, media, notes and relationships. The comparison reports the differences so the preservation and example-fidelity requirements can be evaluated together.

## Root causes and fixes

| Finding | Fix |
| --- | --- |
| The workspace originally applied exact matching between two processed PDF versions. It had no way to proofread a deck against one PDF. | **Changes → Repair against source PDF** now uses the same evidence-based repair engine as batch processing. Version sync remains a separate action. |
| The old QA heuristics converted slide numbers, moved paragraphs and cloned missing boxes. A failed batch could write the original deck into the output directory. | Write only approved text-run changes into the original package. Flag weak or competing alignments for review. Explicitly report a failed amendment and leave the output unpublished. |
| Deck changes lacked a stable source target. Ordered fallback could pair unrelated batch inputs. | Each applied repair carries the PDF page, extracted text, XML XPath, slide number, shape ID and name, paragraph, old and new text, reason, and confidence. Batch pairing requires a unique filename or numeric association, or a one-PDF/one-deck input. |
| The older `example` XML profile changed page attributes while retaining its original page-wrapper structure. | The new `sample` profile emits the observed hierarchy, landmarks, list markers, references and local figure/formula images. The comparator checks markup and content. |
| The official PDF has a corrupt text layer on all 12 pages. OCR also transcribed text inside an embedded figure and merged running footer items. | Use selective OCR, prefer original margin lines that agree with OCR, exclude duplicate text inside matched figure regions, and preserve detected formulas as source image crops. Automated OCR-based deck changes require corroborating text from a second local pass. |

## Verified sample results

- The sample-profile XML is well-formed and validates locally. It contains 12 page milestones, 12 sections, 25 reference elements, and 14 figure or formula regions. Normalized whole-document text similarity was **0.982** against the reference. Differences remain in content grouping, attributes, typography, reading order, and extracted text.
- The deck reopened with all **40 slides in their original order**. Four paragraphs changed across slides **6, 7 and 31**. The original and generated packages differ only in those three slide XML parts, and only in text nodes within them. The exact-reference paragraph score went from **210/334** to **213/334**. Review suggestions and unsupported text are listed separately in the change file.
- The source PDF visibly gives `Population size: 150` on page 8. Slide 31, shape ID 4, now says `150`. The change references `/document/body/section[5]/ul/li[1]` in the generated sample XML.
- LibreOffice rendered all three decks locally. **19 of 40** generated slides matched the reference QA images pixel for pixel at 88 DPI. The HTML report includes the original, expected and generated image for every slide.
- The normal batch command completed against the official input pair using the written-brief profile. The sample-profile comparison command returned **1** to signal reference differences. It produced both output files, detailed reports and all slide previews.
- **42 automated tests passed.** A live Chrome run uploaded the official PDF, repaired the official original deck using that one PDF, and verified the population edit and 40-slide count in the downloaded presentation.

## Reproduce

From `hackathon-project` in PowerShell, use the extracted archive directory and a fresh local output path:

```powershell
$sample = 'D:\Hackathon-Internal-Track\Hackathon-Internal-Track\Data\Example Data Set\20239005532129'
$referenceDeck = Get-ChildItem -LiteralPath "$sample\output" -Filter '*_transformed_QA.pptx' | Select-Object -First 1
.\.venv\Scripts\python.exe -B project\sample_check.py `
  --pdf "$sample\input\20239005532129.pdf" `
  --source-deck "$sample\input\20239005532129_errors.pptx" `
  --expected-xml "$sample\output\20239005532129.xml" `
  --expected-deck $referenceDeck.FullName `
  --previous-deck "$sample\output\20239005532129_errors_updated.pptx" `
  --out storage\official-comparison-new
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider project/tests -q
```

Outputs are under `generated`, with `summary.json`, `xml-comparison.json`, `powerpoint-comparison.json`,
`run/changes`, and `visual/index.html` alongside. The reference files are read-only inputs. Exit code 0 requires XML,
PowerPoint, and rendered-slide agreement. Code 1 signals differences or missing visual coverage. Code 2 signals a
processing error or invalid arguments.

## Changed modules

The deck edits are in `powerpoint/amend.py`, `powerpoint/package.py`, and `powerpoint/sync.py`. Batch routing and
failure handling are in `main.py`, `utils/naming.py`, and `reporting/report.py`. Sample XML and extraction changes
are in `xml_engine/`, `layout/`, and `agents/orchestrator.py`. Reference comparison and visual reports are in
`sample_check.py`, `quality/conformance.py`, and `quality/slide_preview.py`. The workspace route, review
controls, settings, and automated tests were also updated.

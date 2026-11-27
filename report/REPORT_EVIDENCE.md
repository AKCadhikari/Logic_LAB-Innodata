# DocuSync AI report evidence

The numbered entries below correspond to the references in the report. Source paths refer to repository snapshot `69075eae`. This index identifies private run artifacts by relative location and aggregate results. The underlying competition documents and engineering references remain in authorized local storage.

## Source index

| Ref. | Evidence | What it supports |
| --- | --- | --- |
| [1] | Organizer-supplied `AI_Catalyst_2026_Internal_Submission_Guidelines.pdf`, all seven pages | Phase scope and scoring, repository and access requirements, output rules, demo limits and format, AI-tool disclosure and confidentiality. The local PDF was read directly. The repository's [compliance checklist](../hackathon-project/COMPLIANCE_CHECKLIST.md) is supplementary. |
| [2] | [Batch entry point](../hackathon-project/project/main.py), [workspace routes](../hackathon-project/project/ui/workspace_routes.py), [job storage and workers](../hackathon-project/project/ui/workspace.py), [actual frontend](../hackathon-project/project/ui/static/index.html), [paired upload controller](../hackathon-project/project/ui/static/home.js) | CLI and Flask workflows, input archiving, job persistence, PDF/PPTX separation, preview and history. |
| [3] | [Orchestrator](../hackathon-project/project/agents/orchestrator.py), [page diagnostics](../hackathon-project/project/diagnostics/page_diagnostics.py), [detector adapter](../hackathon-project/project/layout/doclayout.py), [isolated worker](../hackathon-project/project/layout/doclayout_worker.py), [association](../hackathon-project/project/layout/region_association.py), [reconstruction](../hackathon-project/project/layout/reconstruct.py), [document IR](../hackathon-project/project/layout/model.py), [Docling adapter](../hackathon-project/project/layout/docling_adapter.py) | Native-versus-OCR decision, actual model invocation, geometry conversion, association, reading order and data fields. |
| [4] | [Runtime settings](../hackathon-project/project/config/settings.py), [OCR router](../hackathon-project/project/ocr/router.py), [GLM-OCR](../hackathon-project/project/ocr/glm.py), [PaddleOCR Python](../hackathon-project/project/ocr/paddle.py), [PaddleOCR-VL adapter](../hackathon-project/project/ocr/paddle_vl.py), [local Ollama transport](../hackathon-project/project/utils/ollama.py), [PPTX proofreading](../hackathon-project/project/powerpoint/proofread.py), [source-grounded repair](../hackathon-project/project/powerpoint/amend.py) | Model selection, fallback, validated suggestions, category limits, and package-preserving edits. |
| [5] | [Technical approach and disclosures](../hackathon-project/TECHNICAL_APPROACH.md), [direct dependencies](../hackathon-project/project/requirements.txt), [local OCR setup](../hackathon-project/docs/LOCAL_OCR.md), [local engineering-sample verification](../hackathon-project/docs/LOCAL_SAMPLE_VERIFICATION.md) | Licence acknowledgements and known limits, GLM/PaddleOCR live evidence, failed checkpoint handling, and the local engineering PDF's validation and detections. Runtime and coding-assistant disclosures reside in `TECHNICAL_APPROACH.md`. |
| [6] | [Serializer](../hackathon-project/project/xml_engine/serializer.py), [XML validation](../hackathon-project/project/xml_engine/validate.py), [sample XSD](../hackathon-project/project/xml_engine/schemas/sample.xsd), [completeness](../hackathon-project/project/xml_engine/completeness.py), [publication](../hackathon-project/project/reporting/exports.py) | Generated XML, local schema validation, source completeness checks, review gates, assets and versioned outputs. |
| [7] | [Explicit repair CLI](../hackathon-project/project/repair_pptx.py), [saved-deck validation](../hackathon-project/project/powerpoint/validation.py), [QA report writer](../hackathon-project/project/powerpoint/repair_qa.py), [grounded PPTX tests](../hackathon-project/project/tests/test_grounded_pptx.py) and local `storage/pptx-correction-verification/corrected_reports/pptx_validation.json` | The later 40-slide repair inspected 198 shapes, saved and package-verified 17 corrections, and verified nine in rendered slides. Eight compatibility-markup edits still require a PowerPoint visual check. The evaluation covers 13 labeled issues, with 12 corrected. Full-deck accuracy was not measured. |
| [8] | Local `storage/report-screenshot-verification/evidence.json` and `storage/ui-inspection-verification/browser-verification.json` beneath `hackathon-project/` | Chromium/Edge browser verification of the real UI. The two-page synthetic PDF had four actual DocLayout-YOLO detections per page, passed the local sample XSD and matched its downloaded XML. The prior synthetic PPTX check verified that one spelling correction reached the downloaded two-slide deck. |
| [9] | [Workspace tests](../hackathon-project/project/tests/test_workspace.py), [startup tests](../hackathon-project/project/tests/test_startup.py), [fidelity tests](../hackathon-project/project/tests/test_fidelity.py), and full test run below | Automated regression coverage. The startup version guard addresses a real stale-backend preview 404. Earlier Windows LibreOffice cleanup behavior was an observed preview failure and was fixed before successful browser verification. |

## Model configuration

| Component | Configured path or setting | Evidence distinction |
| --- | --- | --- |
| DocLayout-YOLO | `.venv-doclayout`, pinned vendored source, and `models/layout/universal/DocLayout-YOLO-universal.pt` | Inference was observed in the browser run and in the 12-page sample. Exact checkpoint licence remains unverified. |
| GLM-OCR | `glm-ocr:q8_0` via loopback Ollama | Accepted recognition on the local engineering sample. The upstream release terms and exact local quantization provenance are separate. |
| PaddleOCR Python | Optional provisioned local detector/recognizer | Accepted recognition in the earlier two-page live check. The exact weight revision is not established by the report. |
| PaddleOCR-VL | `MedAIBase/PaddleOCR-VL:0.9b` by default | Adapter present. The configured tag rejected document-image requests in recorded checks. |
| Docling | Optional isolated worker and local model artifacts | Applied supplemental layout matches to two sample pages. This demonstrates bounded downstream use. |
| Qwen3 | `qwen3:4b` via loopback Ollama | One synthetic correction is verified in the downloaded deck. Source-backed advisory mode is optional and does not apply edits. |

## Evidence boundaries

- The full 12+12 Phase 1 output set, team roster, designated repository access, Phase 2 progression, blind Phase 3 results and demo-video delivery could not be established locally. The ten-page report is supplementary to the organizer's repository, output and video deliverables.
- The engineering XML reference was used for offline comparison. Structural validation and matching exports show pipeline correctness on tested artifacts. They do not measure all extracted text, reading order, or presentation content across the competition corpus.
- The report's screenshots show the running application against synthetic inputs. Figures 2–5 and 7–10 were captured for this report, and Figure 6 reuses a previous successful synthetic proofreading capture. See the [screenshot guide](SCREENSHOT_GUIDE.md) for the figure map.

## Reproduce the regression run

From `hackathon-project`, use the configured Windows environment.

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider project\tests
```

Observed on 27 November 2026 at snapshot `69075eae` with the local Windows environment. **138 passed in 211.34 seconds.** The collected suite completed with zero failures. Earlier observed runtime and compatibility issues are described in [5] and [9].

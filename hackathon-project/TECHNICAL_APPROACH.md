# Technical approach

## Architecture

`project/main.py` is the batch entry point. The Flask review workspace uses the same extraction and export modules. `layout.model` is the shared structured document representation; extraction, XML, PowerPoint edits, validation and audit reports operate on it.

```text
PDF -> PyMuPDF diagnostics/rendering -> isolated DocLayout-YOLO inference
    -> native text or selected OCR -> detection/text association -> Docling + reconstruction
    -> shared document IR -> XML serialization and validation -> versioned phase output

PDF + existing PPTX -> source/evidence matching -> conservative text and consistency repairs
    -> package-preserving PPTX write -> PPTX/XML validation and change report -> phase output
```

The pipeline is local-first. Ollama is contacted only on the configured local endpoint. DocLayout-YOLO runs in the separate `.venv-doclayout` Python process, exchanging rendered page requests and detections through JSON Lines. Docling has its own optional environment. No competition document is sent to an external inference API by these paths.

## PDF and DocLayout-YOLO execution path

1. `project/main.py` or `project/ui/workspace.py` validates/discovers a PDF and calls `agents.orchestrator.process_pdf`.
2. PyMuPDF diagnostics inspect each page and render it for layout inference. Healthy text layers may be retained; damaged, scanned or uncertain pages enter the selected OCR path.
3. `layout.doclayout.Detector.start()` launches `project/layout/doclayout_worker.py` using `Settings.doclayout_python`, the vendored repository and configured checkpoint. The worker loads the model once per document and reports its class map and device.
4. `agents.orchestrator.detect_layout` calls `Detector.detect` for each rendered page. Returned class IDs/names, confidence values and pixel boxes are checked and mapped into PDF coordinates by `layout.doclayout.convert_regions`.
5. `region_association.add_visuals` and `region_association.associate` consume those detections. Associated geometry and classifications are attached to the page IR and exposed to the Zoning viewer. DocLayout supplies regions; it does not recognize text and it does not independently decide final reading order. Reconstruction retains responsibility for text order and document structure.
6. The UI reports page coverage, detections and the actual detection-stage duration from recorded processing events. Model readiness is reported separately from successful per-page inference.

OCR is distinct from layout detection. The workspace offers Automatic, GLM-OCR and PaddleOCR paths (including an optional PaddleOCR-VL adapter). Each attempt stores its selected backend. OCR output is quality-checked and uncertain results remain reviewable. See [local OCR setup](docs/LOCAL_OCR.md).

## Structure, XML and assets

`layout.model` represents pages, blocks, spans, cells, geometry, language, direction, confidence, extraction method and source references. `layout/reconstruct.py`, `layout/kinds.py`, Docling adapters, geometry association and diagnostics contribute to the final structure. Reading-order reconstruction uses positioned content and XY-cut logic, including RTL column ordering.

`xml_engine/serializer.py` serializes the IR with `lxml`; `xml_engine/validate.py` and completeness checks validate well-formedness, root/page structure, page count and content. Export code validates before publishing. Image assets are copied/materialized into the versioned output bundle and checked for valid references. The workspace uses the engineering sample profile by default; the CLI exposes `brief`, `sample` and `example`. The example's legacy vocabulary differs from the written schema, so confirm the required profile with the organizers before final delivery.

## PowerPoint proofreading

`powerpoint/amend.py` aligns slide text to source passages and makes conservative run-level changes only when evidence supports them. Additional repair logic handles selected numbering, footer, breadcrumb and missing-body-box cases. Ambiguous or low-confidence edits are reported for review. The package-writing layer preserves the deck's existing structure and formatting where possible.

The batch entry point pairs PDFs with presentations discovered in the same selected phase input folder. In the workspace, a user can upload a source deck for **source repair**, synchronize an existing deck between two processed PDF versions, or generate a slide deck from a PDF. Source decks uploaded through this flow are retained in the selected phase input folder. Each resulting export receives a unique directory containing XML/PPTX as applicable, processing and validation reports, and assets. Existing exports are not overwritten.

## Progress, persistence and security

Workspace progress is updated by extraction/export callbacks, not artificial timers. Failed attempts retain an error and can be retried. The source PDF and job history are stored in workspace storage; phase input copies are written to `phase-1/input` or `phase-2/input`. Output bundles go to the corresponding phase output directory. `.gitignore` excludes phase inputs/outputs, model weights, virtual environments, `.env`, storage and logs. Competition samples must remain in the authorized private submission environment and be deleted after the event.

## Runtime models, services and development assistants

| Item | Role | Use / evidence | License status |
|---|---|---|---|
| DocLayout-YOLO universal checkpoint | Page-region detection | Local `models/layout/universal/DocLayout-YOLO-universal.pt`; loaded by isolated CPU worker | The vendored repository notice explicitly says model/dataset licenses are separate. No independent license for this exact checkpoint is recorded locally; confirm before redistribution. |
| DocLayout-YOLO / Ultralytics code, 8.3.63 in `.venv-doclayout` | Detection runtime | Vendored under `third_party/DocLayout-YOLO-release-v2`; isolated from the main and Docling environments | Local `LICENSE` and upstream source identify AGPL-3.0. Review obligations for the intended submission/distribution. |
| GLM-OCR (`glm-ocr:q8_0` default) | OCR | Local Ollama model, selected directly or as the automatic primary when installed | Z.ai's model repository states Apache-2.0. The exact local quantized artifact provenance is not pinned here; retain its Ollama/model manifest and confirm it is the same licensed release. |
| Qwen3 4B | Advisory version-change review | Optional local Ollama model; not required for deterministic matching/repair | Official Qwen3-4B repository states Apache-2.0. |
| PaddleOCR Python | OCR fallback/alternative | Optional local Python runtime and provisioned recognizer | PaddleOCR source project is Apache-2.0; exact downloaded model-weight revisions should be recorded before redistribution. |
| PaddleOCR-VL Ollama adapter | Optional OCR alternative | Adapter is present. The configured `MedAIBase/PaddleOCR-VL:0.9b` runtime has previously rejected document-image input; do not report it as a successful OCR engine unless a compatible vision model passes an actual image request. | Exact Ollama tag's weight license is not verified in this checkout. |
| Docling layout Heron, `docling-project/docling-layout-heron` | Additional page-layout evidence | Installed locally under the Docling artifacts directory; the adapter runs it through the isolated Docling worker | Model card declares Apache-2.0. |
| Docling layout Heron ONNX export | Additional page-layout evidence | The ONNX export is also present in local artifacts; whether it is selected depends on the installed Docling runtime | Model card declares Apache-2.0. |
| Docling TableFormer `accurate` and `fast` weights (`docling-project/docling-models`) | Table structure | Local Docling artifacts; Docling uses these when table structure is enabled | The model repository advertises CDLA-Permissive-2.0 and Apache-2.0 across its assets. Confirm which grant/notices apply to the exact files before redistribution. |
| Docling package | Optional structure aid | Version 2.150.1 is installed in the separate Docling runtime; reconstruction remains available when disabled | Docling source repository is MIT. Model assets carry separate terms as listed above. |
| Tesseract OCR and language data | Optional legacy OCR/source verification | External executable and traineddata, not installed by `project/requirements.txt` | Tesseract source is Apache-2.0; check any additional traineddata source separately. |
| Ollama | Local model runner | Provides the configured local endpoint for GLM-OCR and Qwen3; not a hosted inference service in this workflow | Ollama code is MIT; model weights have separate terms. |
| Claude (Anthropic) | AI coding assistant | Existing project disclosure says it was used during project development | Development assistant; no runtime call in the document-processing pipeline. |
| OpenAI Codex | AI coding assistant | Used for the current implementation and compliance work | Development assistant; no competition document was uploaded to a hosted model by this workflow. |

Third-party software directly used by the project includes PyMuPDF (AGPL-3.0 or commercial), PyTorch/torchvision (BSD-3-Clause), PaddleOCR/PaddlePaddle (Apache-2.0), Docling (MIT), OpenCV (Apache-2.0), `python-pptx` (MIT), `lxml` (BSD-3-Clause), NumPy (BSD-3-Clause), RapidFuzz (MIT), Flask (BSD-3-Clause), Pydantic (MIT), Pillow (MIT-CMU), `regex` (CNRI Python 1.6 license for derived portions plus Apache-2.0 for additions), pytest (MIT, test-only), psutil (BSD-3-Clause), Ollama (MIT), and Tesseract (Apache-2.0, external optional executable). See each package's license file and the local DocLayout `LICENSE`/`NOTICE`. This is a direct-component inventory, not a complete transitive software bill of materials.

The relevant license sources are [DocLayout-YOLO](https://github.com/opendatalab/DocLayout-YOLO/blob/main/LICENSE), [Ultralytics](https://github.com/ultralytics/ultralytics/blob/main/LICENSE), [GLM-OCR](https://github.com/zai-org/GLM-OCR/blob/main/LICENSE), [Qwen3-4B](https://huggingface.co/Qwen/Qwen3-4B/blob/main/LICENSE), [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR/blob/main/LICENSE), [Docling](https://github.com/docling-project/docling), [Docling layout Heron](https://huggingface.co/docling-project/docling-layout-heron), [Docling TableFormer](https://huggingface.co/docling-project/docling-models/tree/main/model_artifacts/tableformer), [PyMuPDF](https://github.com/pymupdf/PyMuPDF/blob/main/README.md), [regex](https://github.com/mrabarnett/mrab-regex/blob/hg/LICENSE.txt), [Pillow](https://github.com/python-pillow/Pillow/blob/main/LICENSE), and [Tesseract](https://github.com/tesseract-ocr/tesseract/blob/main/LICENSE).

## Limits and known risks

- The available local competition example is one 12-page PDF and one paired PPTX. It does not establish performance on all 12 Phase 1 pairs or the new Phase 2 set.
- Scanned tables, equations, skew and multilingual OCR depend on model output and need page-level review. `image`/figure descriptions can be heuristic.
- PPTX repair is intentionally conservative. This reduces unsupported edits but leaves ambiguous defects for a reviewer.
- CPU timing and memory need measurement on the team's target hardware and full data set.
- The exact DocLayout, PaddleOCR and optional Docling model-weight licenses/revisions need to be recorded before any redistribution.
- The team should confirm the XML profile expected by the organizers and check repository privacy/member access, naming, submission URL and demo-video delivery.

The current local execution and engineering-reference comparison are recorded in [local sample verification](docs/LOCAL_SAMPLE_VERIFICATION.md); historical benchmark figures in `docs/BENCHMARKS.md` were not rerun in this pass.

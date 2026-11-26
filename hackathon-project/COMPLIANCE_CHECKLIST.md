# AI Catalyst 2026 compliance checklist

Reviewed against all seven pages of `AI_Catalyst_2026_Internal_Submission_Guidelines.pdf` on 26 November 2026. The PDF is the source for the official requirements below. Local implementation improvements requested separately are listed in their own section.

**Status key:** PASS = evidence checked; PARTIAL = some evidence exists but a required deliverable or coverage is missing; FAIL = known gap; N/A = conditional requirement does not currently apply; UNVERIFIED = depends on team or organizer information unavailable in this checkout.

## Official requirements

| Official requirement | Status | Evidence and remaining work |
|---|---|---|
| Competition covers PDF-to-structured-XML and proofreading presentations to corrected PPTX in each phase. | PARTIAL | The batch entry point supports PDF/PPTX pairing; the review workspace supports PDF extraction and source-deck repair. The local input set is one 12-page PDF and one paired error deck, not the Phase 1 set of 12 of each. |
| Phase 1 processes all 12 supplied PDFs and all 12 supplied PPTX presentations; outputs are evaluated. | FAIL | The required application paths exist. Only `phase-1/input/20239005532129.pdf` and `20239005532129_errors.pptx` are available locally. The remaining 11 pairs were not present, so full-set results cannot be claimed. |
| Phase 2 processes the new 10 PDFs and 10 presentations, improves the solution, and submits to the same repository by the announced deadline. | N/A | Applies only if this team advances. No Phase 2 competition data or advancement status is available. The Phase 2 upload/output path was smoke-tested with authorized local sample pages; this does not establish full Phase 2 coverage. |
| Phase 3 live evaluation includes the demo, judges' Q&A and two unseen defect PDFs; Phase 1 marks carry forward. | UNVERIFIED | This is a scheduled team activity. No blind evaluation has occurred in this workspace. |
| GitHub account, dedicated repository, `team_name-innodata` naming, Innodata organization access, member permission by 17 November, and repository URL in proposal. | UNVERIFIED | Local remote is `https://github.com/AKCadhikari/Logic_LAB.git`; account ownership, privacy, team naming, designated member access and proposal submission cannot be established locally. Confirm with the team and organizers. |
| Keep the designated Innodata team access current throughout the competition. | UNVERIFIED | Requires checking repository permissions with the team/organizers. |
| Required root structure: `project/main.py`, `project/requirements.txt`, `phase-1/{input,output}`, conditional `phase-2/{input,output}`, root `README.md`, `PROJECT_DETAILS.md`, `TECHNICAL_APPROACH.md`, and `project-demo.mp4`. | PARTIAL | All required folders, entry point and three Markdown files exist. `project-demo.mp4` is missing. Input files are local and ignored by Git; outputs are generated locally and ignored. The exact competition files/outputs are not yet in a submission repository. |
| Phase 1 source, input/output samples, working implementation and documentation in the designated repository. | PARTIAL | Code and required docs exist. This checkout has only the one local sample pair and generated output evidence; ignored input/output files have not been committed or uploaded. Do not publish confidential files to a public or otherwise unauthorized destination. |
| Phase 1 XML files and corrected amendment presentations must be in the phase output folder. | PARTIAL | CLI and workspace output paths are phase-specific. The workspace now creates versioned XML on successful PDF processing and versioned corrected PPTX on the explicit deck-repair action. The required 12+12 output set remains unavailable. |
| Phase 2 folder and deliverables apply to selected teams; update the same repository before the final deadline. | N/A | Conditional; see Phase 2 status above. |
| Demo preparation 5 minutes, presentation 15 minutes, Q&A 10 minutes; overrun deductions; at most 10 slides. | UNVERIFIED | Team preparation and slide deck are not part of this local code change. |
| Demo video explains problem, solution, key functionality and an actual execution. | FAIL | `project-demo.mp4` is absent. No video was fabricated. |
| Video is MP4/H.264/AAC, at least 720p (1080p preferred), 5-10 minutes, maximum 100 MB, named project-demo.mp4. | FAIL | No video file is present to inspect. |
| Original work developed during the Building Phase and Grand Finale; no copying from another team/source. | UNVERIFIED | The repository history and source attribution are available for review, but originality and team provenance are matters for the team to attest to. Third-party components are documented below. |
| Third-party libraries, frameworks, models and APIs may be used only under compatible terms and must be acknowledged. | PARTIAL | Direct dependencies and discovered licenses are listed in `TECHNICAL_APPROACH.md`. The locally supplied DocLayout checkpoint and some provisioned OCR weights do not include a clear, independently verified weight license in this checkout. Confirm their redistribution/use terms with the team/organizers. |
| Disclose every AI model, service and tool, including AI coding assistants, in `TECHNICAL_APPROACH.md`. | PASS | The document records runtime models/services and the project history's Claude disclosure, and adds Codex for this implementation work. |
| Every XML/PPTX must be generated by the submitted solution; no manual correction or hard-coded sample answer. | PARTIAL | Current code paths generate XML/PPTX from the source PDF/deck and the tests use synthetic inputs. This audit does not claim the missing competition set was processed. Keep output directories versioned and retain reports/audit evidence. |
| Competition files are confidential, limited to competition use, not uploaded to unauthorized services, and deleted after the event. | PARTIAL | Local samples are in Git-ignored phase input folders and were processed locally. No external model API was used. The event has not concluded; delete samples after the event as required. Confirm repository privacy and authorized access before uploading any samples. |
| Never commit keys, passwords, access tokens or other secrets; use secure configuration. | PASS | `.gitignore` excludes `.env` and local runtime data. The checked-in example configuration contains defaults only. Recheck before every commit/push. |
| Fair play, respectful conduct, compliance with applicable laws and Innodata policies; organizer/judge decisions are final. | UNVERIFIED | Participant conduct and organizer decisions cannot be validated from this codebase. |

## Requested engineering improvements (not additional PDF requirements)

| Request | Status | Evidence |
|---|---|---|
| PDF upload, validation, diagnostics, OCR/layout processing, structured output, validation and saved location in the UI. | PASS (tested workflow) | The Chrome/Playwright run verified upload, actual progress, Phase 2 input archival, versioned XML under `phase-2/output`, validation, review and re-edit using a local sample page. Full Phase 1 data coverage remains incomplete. |
| Real DocLayout-YOLO as layout detection, distinct from OCR; show status, detections and measured time. | PASS | `layout/doclayout.py` starts the isolated `.venv-doclayout` JSON-lines worker; `agents/orchestrator.py` renders each page and calls `Detector.detect`; `region_association` consumes detections. The 12-page sample run recorded 188 detections and 111 associated IR blocks. The browser check also verified status, visible detections and timing. |
| Select OCR through UI and preserve local model isolation. | PASS (tested workflow) | The workspace offers Automatic, GLM-OCR and PaddleOCR. The browser test processed a two-page local sample with both GLM-OCR and PaddleOCR; both were accepted and associated with DocLayout regions. DocLayout remains isolated and `.venv-docling` was not modified. |
| Separate PPTX proofreading, with actual input/output and no overwrite. | PASS (implementation and synthetic correction) | `/documents/<id>/presentations` supports source repair and version-sync modes; outputs use unique ID folders and uploaded decks are archived in the selected phase input. Automated tests verify a synthetic numeric correction and PPTX validity. The real engineering sample required no text changes, so its run was a validated no-op. |
| Actual processing progress, errors, warnings and output paths; no simulated completion. | PASS (implementation) | Progress callbacks reflect pipeline stage events; export reports carry output paths and validation. UI now includes DocLayout detection time. |
| Maintain structured, validated XML and save linked assets. | PASS | `xml_engine/serializer.py`, `xml_engine/validate.py`, and `reporting/exports.py` generate and validate XML and package linked assets. The full 12-page sample and browser workflow produced valid XML; the sample XML validation found no missing/invalid assets. |
| Keep official top-level structure while retaining useful implementation directories. | PASS | Required top-level folders/files exist; internal modules and isolated runtimes remain separate. |
| Automated verification | PASS (114 tests) | The full Windows test suite completed with **114 passed**. The live Chrome/Playwright Phase 2 flow also passed with both OCR engines, XML/PPTX checks, rotated-page coordinates and missing-checkpoint failure handling. |

## Recommended, not mandated by the PDF

- Pin all runtime and transitive dependency versions and generate a software bill of materials.
- Add representative layout-specific accuracy benchmarks beyond the one local engineering example.
- Capture and report CPU, memory and per-page timing on the team's target hardware.
- Confirm third-party model-weight licenses and add attribution / notices before distributing weights.

"""Build privacy-safe evidence for the saved IR and its actual outputs."""
from collections import Counter
from copy import deepcopy

from lxml import etree

from utils.execution_audit import digest
from utils.textutil import norm_ws


def blocks(items):
    for block in items:
        yield block
        yield from blocks(block.children)


def anchor_ir(ir, trace_id):
    for page in ir.pages:
        for index, block in enumerate(blocks(page.blocks), 1):
            block.source_reference.setdefault("block_id", f"{trace_id}/page/{page.n}/block/{index}")
            if block.source == "native":
                block.source_reference.setdefault("extraction_id", f"{trace_id}/page/{page.n}/pymupdf")


def reports(ir, evidence, xml=None, locations=None, *, edits=(), analysis=None, pptx=None):
    evidence = evidence or {}
    trace_id = evidence.get("trace_id")
    document_hash = (ir.meta.get("source_sha256") if ir else None)
    executions = deepcopy(evidence.get("executions", []))
    if analysis and analysis.get("execution"):
        executions.append(deepcopy(analysis["execution"]))
    by_id = {row["execution_id"]: row for row in executions}
    root = etree.fromstring(xml) if xml else None
    lineage, pages, pptx_links = [], [], []
    paddle_attempted = False
    if ir:
        for page in ir.pages:
            paddle_attempted |= any(item["engine"] == "paddleocr" for item in page.diag.get("ocr", {}).get("attempts", []))
            method = "NATIVE_EXTRACTION_USED" if page.source == "native" else "OCR_OUTPUT_USED" if page.source == "ocr" else page.source
            pages.append({"page": page.n, "method": method, "ocr_required": page.diag.get("ocr_required"),
                          "ocr_status": page.diag.get("ocr", {}).get("status"),
                          "source_execution_ids": page.diag.get("ocr", {}).get("selected_execution_ids", []),
                          "reused_cache": bool(page.diag.get("ocr", {}).get("cached")),
                          "execution_ids": [row["execution_id"] for row in executions if row.get("page") == page.n]})
            for block in blocks(page.blocks):
                ref = block.source_reference
                path = locations.get(id(block)) if locations else None
                nodes = root.xpath(path) if root is not None and path else []
                node = nodes[0] if nodes else None
                text = block.text or block.caption
                rendered = norm_ws("".join(node.itertext())) if node is not None else ""
                corresponds = bool(node is not None and (not text or norm_ws(text) in rendered or
                                   any(norm_ws(text) in norm_ws(img.get("alt", "")) for img in node.iter("img"))))
                if corresponds and block.cells:
                    for cell in block.cells:
                        cell_path = locations.get(id(cell))
                        cell_nodes = root.xpath(cell_path) if cell_path else []
                        if not cell_nodes or norm_ws("".join(cell_nodes[0].itertext())) != norm_ws(cell.text):
                            corresponds = False
                            break
                ids = ref.get("execution_ids", []) if block.source == "ocr" else []
                row = {"page": page.n, "block_id": ref.get("block_id"), "extraction_id": ref.get("extraction_id"),
                       "source": block.source, "ocr_engine": block.ocr_engine or None, "execution_ids": ids,
                       "ir_text_sha256": digest((text if not block.cells else "\n".join(
                           cell.text for cell in block.cells)).encode("utf-8")), "xml_node": path,
                       "xml_corresponds_to_ir": corresponds, "bbox": block.bbox}
                lineage.append(row)
                for key in ids:
                    if key in by_id:
                        by_id[key]["downstream"].append({"consumer": "pydantic_ir", "reference": row["block_id"]})
                        if corresponds:
                            by_id[key]["downstream"].append({"consumer": "xml", "reference": path, "block_id": row["block_id"]})
    linked = {row["block_id"]: row for row in lineage if row["block_id"]}
    for edit in confirmed_edits(pptx, edits):
        source = edit.get("source") or {}
        block_id = source.get("block_id")
        if block_id in linked:
            link = {"block_id": block_id, "source_xml_node": linked[block_id]["xml_node"], "slide": edit["slide"],
                    "shape_id": edit["shape_id"], "paragraph": edit.get("paragraph"), "output": pptx.name,
                    "pptx_text_sha256": digest(edit["new"].encode("utf-8"))}
            pptx_links.append(link)
            for key in source.get("execution_ids", []):
                if key in by_id:
                    by_id[key]["downstream"].append({"consumer": "pptx", **link})
    for row in executions:
        statuses = []
        if row["request_status"] == "sent":
            statuses.append("REQUEST_SENT")
        elif row["request_status"] == "attempted":
            statuses.append("REQUEST_ATTEMPTED")
        if row["executed"]:
            statuses.append("EXECUTED")
        if row["response_status"] == "received":
            statuses.append("RESPONSE_RECEIVED")
        if row["validation"] == "passed":
            statuses.append("OUTPUT_VALIDATED")
        if row["downstream"]:
            statuses.append("USED_DOWNSTREAM")
        elif row["executed"]:
            statuses.append("EXECUTED_BUT_NOT_USED")
        if row.get("error") or row["response_status"] in {"failed", "http_error", "invalid"} or row["validation"] == "rejected":
            statuses.append("FAILED")
        if row["mode"] == "MOCKED_TEST":
            statuses.append("MOCKED_TEST")
        row["statuses"] = statuses
    real = [row for row in executions if row["mode"] != "MOCKED_TEST"]
    models = Counter(row.get("returned_model") or row["model"] for row in real if row["executed"])
    identity = {"trace_id": trace_id, "document_sha256": document_hash or evidence.get("document_sha256"),
                "document_id": evidence.get("document_id") or (ir.doc_id if ir else None), "mode": evidence.get("mode")}
    usage = {**identity, "executed_models": dict(models),
             "requests": sum(row["request_status"] in {"attempted", "sent"} for row in real),
             "responses": sum(row["response_status"] == "received" for row in real),
             "failures": sum("FAILED" in row["statuses"] for row in real),
             "fallback_executions": sum(row["executed"] and bool(row.get("fallback_reason")) for row in real),
             "used_responses": sum(bool(row["downstream"]) for row in real),
             "native_pages": sum(row["method"] == "NATIVE_EXTRACTION_USED" for row in pages),
             "ocr_pages": sum(row["method"] == "OCR_OUTPUT_USED" for row in pages),
             "paddleocr": ("EXECUTED" if any(row["provider"] == "paddleocr" and row["executed"] for row in real)
                           else "REQUESTED" if any(row["provider"] == "paddleocr" for row in real)
                           else "UNAVAILABLE_OR_NOT_EXECUTED" if paddle_attempted else "NOT_REQUIRED")}
    return {"ai_execution_report.json": {**identity, "executions": executions},
            "model_usage_report.json": usage,
            "processing_trace.json": {**identity, "pages": pages, "blocks": lineage, "pptx": pptx_links,
                                      "deterministic_stages": [
                                          *(["PyMuPDF native extraction"] if usage["native_pages"] else []),
                                          *(["Pydantic IR"] if ir else []), *(["XML serialization"] if xml else []),
                                          *(["PPTX editing"] if pptx else [])],
                                      "xml_sha256": digest(xml) if xml else None,
                                      "qwen_execution_id": (analysis or {}).get("execution_id")}}


def confirmed_edits(path, edits):
    if not path or not edits:
        return
    from powerpoint.amend import iter_shapes, para_text, text_paragraphs
    from powerpoint.package import open_deck

    deck = open_deck(path)
    for edit in edits:
        if not edit.get("paragraph") or not edit.get("source") or "new" not in edit:
            continue
        shapes = iter_shapes(deck.slides[edit["slide"] - 1].shapes)
        shape = next((item for item in shapes if item.shape_id == edit.get("shape_id")), None)
        if shape is not None:
            paragraphs = list(text_paragraphs(shape))
            index = edit["paragraph"] - 1
            if index < len(paragraphs) and para_text(paragraphs[index][1]) == edit["new"]:
                yield edit

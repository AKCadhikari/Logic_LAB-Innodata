"""Read page requests on stdin and return local Docling regions as JSON lines."""
from __future__ import annotations

import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path


def converter(artifacts: Path, timeout: int):
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(do_ocr=False, do_table_structure=True, enable_remote_services=False,
                                 allow_external_plugins=False, artifacts_path=artifacts,
                                 document_timeout=timeout,
                                 accelerator_options=AcceleratorOptions(num_threads=2, device=AcceleratorDevice.CPU))
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def regions(converter, pdf: Path, number: int) -> list[dict]:
    result = converter.convert(pdf, page_range=(number, number), raises_on_error=False)
    if getattr(result.status, "value", "") not in {"success", "partial_success"}:
        raise RuntimeError("Local Docling conversion did not complete.")
    document = result.document
    found = []
    for item, _ in document.iterate_items():
        for provenance in item.prov:
            page = document.pages.get(provenance.page_no)
            if page is None or provenance.page_no != number:
                continue
            box = provenance.bbox.to_top_left_origin(page_height=page.size.height)
            coords = (box.l, box.t, box.r, box.b)
            if coords[0] < coords[2] and coords[1] < coords[3]:
                region = {"kind": item.label.value, "text": getattr(item, "text", ""), "bbox": coords,
                          "ref": item.self_ref, "parent": item.parent.cref if item.parent else None,
                          "captions": [caption.cref for caption in getattr(item, "captions", [])]}
                if item.label.value == "table":
                    region["grid"] = {"rows": item.data.num_rows, "columns": item.data.num_cols,
                                      "cells": [cell.model_dump(mode="json") for cell in item.data.table_cells]}
                found.append(region)
    return found


def main() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    model = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with redirect_stdout(sys.stderr):
                if model is None:
                    model = converter(Path(sys.argv[1]), int(sys.argv[2]))
                result = {"regions": regions(model, Path(request["pdf"]), request["page"])}
        except Exception as error:
            result = {"error": type(error).__name__}
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()

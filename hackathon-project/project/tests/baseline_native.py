"""DocuSync AI - Phase 1 PDF-to-XML baseline.

This first baseline extracts native PDF text in page/block order. It is deliberately
conservative: it does not claim to perform OCR, table reconstruction, or figure
understanding yet. Those are subsequent pipeline stages.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import fitz  # PyMuPDF


def safe_text(value: str) -> str:
    # XML 1.0 excludes most control characters. Preserve normal Unicode scripts.
    return "".join(ch for ch in value if ch in "\t\n\r" or ord(ch) >= 0x20)


def extract_pdf(pdf_path: Path, output_path: Path) -> dict:
    doc = fitz.open(pdf_path)
    root = ET.Element("document", {"lang": "und"})
    stats = {"pages": len(doc), "empty_pages": 0, "blocks": 0, "characters": 0}

    for page_index, page in enumerate(doc, start=1):
        page_node = ET.SubElement(root, "page", {"n": str(page_index)})
        page_dict = page.get_text("dict", sort=True)
        text_blocks = [b for b in page_dict.get("blocks", []) if b.get("type") == 0]
        if not text_blocks:
            stats["empty_pages"] += 1

        # PyMuPDF's sorted block order is a baseline heuristic, not guaranteed
        # reading order for complex/multi-column layouts.
        for block in text_blocks:
            lines = []
            for line in block.get("lines", []):
                spans = [s.get("text", "") for s in line.get("spans", [])]
                line_text = "".join(spans).strip()
                if line_text:
                    lines.append(line_text)
            text = safe_text("\n".join(lines)).strip()
            if not text:
                continue
            ET.SubElement(page_node, "p").text = text
            stats["blocks"] += 1
            stats["characters"] += len(text)

    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    doc.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert PDFs in a folder to baseline page-structured XML.")
    parser.add_argument("--input", type=Path, required=True, help="Folder containing source PDFs")
    parser.add_argument("--output", type=Path, required=True, help="Folder for generated XML files")
    args = parser.parse_args()
    if not args.input.is_dir():
        raise SystemExit(f"Input folder does not exist: {args.input}")

    pdfs = sorted(args.input.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"No PDF files found in: {args.input}")

    for pdf in pdfs:
        out = args.output / f"{pdf.stem}.xml"
        stats = extract_pdf(pdf, out)
        print(f"[OK] {pdf.name} -> {out.name} | pages={stats['pages']} empty={stats['empty_pages']} blocks={stats['blocks']} chars={stats['characters']}")


if __name__ == "__main__":
    main()

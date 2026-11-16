# Technical Approach

## Hardware-aware strategy
Target Windows 11 with CPU-first processing. Keep models small and batch work page-wise to stay within 20 GB RAM. Treat AMD GPU acceleration as optional until the exact Radeon model and supported runtime are verified.

## Initial stack
- Python 3.11
- PyMuPDF for PDF parsing and native text extraction
- Python standard-library XML serializer for the first baseline

## Planned additions (after compatibility tests)
- OpenCV for image preprocessing
- PaddleOCR or another locally runnable OCR engine for image-only pages
- Layout detection only where it improves measurable structure fidelity
- python-pptx for reading and writing PowerPoint files

## Design principle
Use deterministic extraction first. Trigger OCR or heavier processing only on pages whose diagnostics indicate missing, low-quality, or corrupted text. Log every decision and keep the pipeline unattended and reproducible.

"""Run local OCR capability measurements against a frozen, labelled corpus."""
from __future__ import annotations

import argparse
from difflib import SequenceMatcher
from html import escape
import json
from pathlib import Path
import time

import pymupdf
import regex
from rapidfuzz.distance import Levenshtein

from config.settings import Settings
from ocr import engine, glm, paddle
from tests.language_corpus import SAMPLES
from utils.textutil import ISO_TO_TESS, REQUIRED_LANGUAGES, LANGUAGE_SCRIPTS, language_profile, norm_ws


def measure(expected: str, actual: str, language: str) -> dict:
    source, output = (norm_ws(value) for value in (expected, actual))
    a, b = (regex.findall(r"\X", value) for value in (source, output))
    edits = [{"type": kind, "expected": "".join(a[i:j]), "actual": "".join(b[k:l])}
             for kind, i, j, k, l in SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if kind != "equal"]
    return {"expected": source, "actual": output, "cer": round(Levenshtein.distance(a, b) / max(1, len(a)), 4),
            "wer": None if language in {"ja", "zh"} else round(
                Levenshtein.distance(source.split(), output.split()) / max(1, len(source.split())), 4),
            "exact_logical_order": source == output, "character_changes": edits}


def fixture(expected: str, digital: Path, scan: Path) -> None:
    with pymupdf.open() as document, pymupdf.open() as raster:
        page = document.new_page(width=760, height=132)
        page.insert_htmlbox(page.rect + (24, 22, -24, -22), f'<p dir="auto">{escape(expected)}</p>',
                            css="p {font-size: 23pt; margin: 0;}")
        document.save(digital)
        raster.new_page(width=760, height=132).insert_image(page.rect, pixmap=page.get_pixmap(dpi=190))
        raster.save(scan)


def timed(call, expected: str, language: str) -> dict:
    start = time.monotonic()
    try:
        result = measure(expected, call(), language)
    except (OSError, RuntimeError, ValueError) as error:
        result = {"error": str(error)}
    result["seconds"] = round(time.monotonic() - start, 3)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--languages", nargs="+", choices=list(SAMPLES), default=list(SAMPLES))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    settings = Settings.load(use_cache=False)
    packs = engine.available_langs(settings)
    glm_status = glm.model_info(settings)
    report = {"method": "Authored digital PDF rendered into an image-only PDF; paths and results are local.",
              "required_language_source": {"document": "Innodata_AI_Catalyst.pdf", "page": 8},
              "required_languages": list(REQUIRED_LANGUAGES), "glm": glm_status, "languages": {}}
    output = args.out / "language_support_report.json"
    fallback = paddle.PaddleOCR(settings)
    try:
        for index, language in enumerate(args.languages, 1):
            expected = SAMPLES[language]
            digital = args.out / f"fixture-{index:02}.pdf"
            scan = args.out / f"scan-{index:02}.pdf"
            fixture(expected, digital, scan)
            script = LANGUAGE_SCRIPTS[language]
            row = {"script": script, "direction": language_profile(expected, language)["base_direction"],
                   "required": language in REQUIRED_LANGUAGES, "digital": digital.name, "scan": scan.name,
                   "paddle_availability": paddle.readiness(settings, language)}
            with pymupdf.open(digital) as document, pymupdf.open(scan) as raster:
                row["native"] = timed(lambda: document[0].get_text(), expected, language)
                if glm_status["ready"]:
                    row["glm_ocr"] = timed(lambda: glm.recognise(raster[0], 190, settings).text(), expected, language)
                pack = ISO_TO_TESS[language]
                row["tesseract"] = {"status": "pack_unavailable", "pack": pack}
                if pack in packs:
                    row["tesseract"] = timed(lambda: "\n".join(
                        line.text for line in engine.ocr_page(raster[0], language, 190, settings, packs,
                                                            orient=False, deskew=False)[0]), expected, language)
                    row["tesseract"]["pack"] = pack
                if row["paddle_availability"]["ready"]:
                    row["paddle"] = timed(lambda: fallback.recognise(raster[0], language, 190).text(), expected, language)
                    fallback.close()
            report["languages"][language] = row
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(language, *(f"{key}={row[key].get('cer', row[key].get('status', 'error'))}"
                              for key in ("native", "glm_ocr", "paddle", "tesseract") if key in row), flush=True)
    finally:
        fallback.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

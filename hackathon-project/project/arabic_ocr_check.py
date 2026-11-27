"""Score local OCR on a frozen, visually transcribed Arabic PDF sample."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from time import perf_counter

import cv2
import pymupdf
from rapidfuzz.distance import Levenshtein

from config.settings import Settings
from layout import native, reconstruct
from layout.region_association import coverage
from language_check import fixture
from ocr import engine, glm, paddle, paddle_vl
from ocr.recognition import RecognitionError
from tests.language_corpus import SAMPLES
from utils.textutil import norm_ws


# PDF points measured on the rendered pages of the local two-page gazette.
REGIONS = (
    (1, "title", (145, 155, 620, 185), "الموافقة على الممكنات النظامية للهيئة الوطنية للأمن السيبراني"),
    (1, "right-column", (390, 190, 717, 258),
     "إنَّ مجلس الوزراء\nبعد الاطلاع على المعاملة الواردة من الديوان الملكي برقم ٢٣٧٨٤ وتاريخ ١٤٤٦/٤/٤هـ، المشتملة\n"
     "على برقية معالي رئيس مجلس إدارة الهيئة الوطنية للأمن السيبراني رقم ٣٣ وتاريخ ١٤٤٤/١/١٠هـ،\nفي شأن الممكنات النظامية للهيئة."),
    (1, "left-column", (56, 228, 381, 257),
     "يقرر ما يلي:\nأولاً: الموافقة على الممكنات النظامية للهيئة الوطنية للأمن السيبراني، بالصيغة المرافقة."),
    (2, "title", (91, 401, 685, 436),
     "الموافقة على اتفاقية عامة للتعاون بين حكومة المملكة وحكومة جمهورية هندوراس"),
    (2, "article-heading", (640, 912, 724, 929), "المادة الأولى:"),
)


def score(expected, actual):
    expected, actual = map(norm_ws, (expected, actual))
    units = ((expected, actual), (expected.split(), actual.split()))
    results = {"reference": expected, "actual": actual}
    for label, (reference, output) in zip(("character", "word"), units):
        results[label + "_edits"] = Levenshtein.distance(reference, output)
        results[label + "_count"] = len(reference)
    return results


def totals(rows):
    return {key: round(sum(row[label + "_edits"] for row in rows) /
                       sum(row[label + "_count"] for row in rows), 6)
            for key, label in (("cer", "character"), ("wer", "word"))} if rows else {}


def in_region(lines, box, width, *, join=False):
    selected = [line for line in lines if coverage(line.bbox, box) >= .55]
    if join:
        selected = reconstruct.join_arabic_fragments(selected)
    return "\n".join(line.text for group in reconstruct.xy_cut(selected, 10, width, True) for line in group)


def variant(name, image, settings):
    if name == "contrast":
        return cv2.createCLAHE(2.0, (8, 8)).apply(image)
    if name == "denoise":
        return cv2.medianBlur(image, 3)
    if name == "otsu":
        return cv2.threshold(image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    if name == "deskew":
        return engine.preprocess(image, settings, orient=False, deskew=True)[0]
    return image


def cropped(page, box):
    clip = pymupdf.Rect(box)
    document = pymupdf.open()
    document.new_page(width=clip.width, height=clip.height).show_pdf_page(
        pymupdf.Rect(0, 0, clip.width, clip.height), page.parent, page.number, clip=clip)
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--models", nargs="+", choices=("paddle", "glm", "tesseract"), default=("paddle", "glm"))
    parser.add_argument("--recognizer", type=Path)
    parser.add_argument("--full-pages", action="store_true", help="score crops of the full-page PaddleOCR output")
    parser.add_argument("--full-pages-only", action="store_true")
    parser.add_argument("--page-dpi", nargs="+", type=int, default=(200, 300, 400))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    report = {"file": args.pdf.name, "sha256": hashlib.sha256(args.pdf.read_bytes()).hexdigest(),
              "metrics": "Levenshtein over Unicode code points and whitespace-delimited words; "
                         "collapse whitespace and keep punctuation, digits and diacritics.",
              "paddle_vl": paddle_vl.model_info(Settings()), "runs": {}}
    path = args.out / "comparison.json"

    def save(key, rows, seconds):
        real = [row for row in rows if row["region"] != "authored-mixed-scan"]
        fields = [field for row in real for field in row.get("fields", [])]
        report["runs"][key] = {"seconds": round(seconds, 3), "gazette": totals(real),
                               "pages": {str(page): totals([row for row in real if row["page"] == page])
                                         for page in (1, 2)}, "regions": rows,
                               "exact_fields": {"matches": sum(fields), "count": len(fields)}}
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(key, report["runs"][key]["gazette"], round(seconds, 3), flush=True)

    settings = Settings()
    if args.recognizer:
        settings = replace(settings, paddle_rec_model_dir=str(args.recognizer.resolve()))
    report["paddle"] = paddle.readiness(settings, "ar")
    fixture(SAMPLES["ar"], args.out / "mixed-native.pdf", args.out / "mixed-scan.pdf")
    ocr = paddle.PaddleOCR(settings)
    try:
        with pymupdf.open(args.pdf) as pdf, pymupdf.open(args.out / "mixed-scan.pdf") as scan:
            actual = [score(ref, in_region(native.extract_lines(pdf[num - 1]), box, pdf[num - 1].rect.width))
                      | {"page": num, "region": label} for num, label, box, ref in REGIONS]
            save("native-regions", actual, 0)
            configs = []
            if "paddle" in args.models:
                configs.extend(("paddle", dpi, mode) for dpi, mode in
                               ((200, "raw"), (300, "raw"), (400, "raw"), (300, "contrast"),
                                (300, "denoise"), (300, "otsu"), (300, "deskew")))
            if "glm" in args.models:
                configs.append(("glm", 200, "raw"))
            if args.full_pages_only:
                configs.clear()
            original_render = paddle.render_gray
            for model, dpi, mode in configs:
                rows = []
                started = perf_counter()
                paddle.render_gray = lambda page, dpi: variant(mode, original_render(page, dpi), settings)
                for num, label, box, ref in (*REGIONS, (1, "authored-mixed-scan", None, SAMPLES["ar"])):
                    pdf_page = scan[0] if box is None else pdf[num - 1]
                    with cropped(pdf_page, box or pdf_page.rect) as crop:
                        tick = perf_counter()
                        result = (ocr.recognise(crop[0], "ar", dpi) if model == "paddle"
                                  else glm.recognise(crop[0], dpi, settings))
                        actual = in_region(result.lines, crop[0].rect, crop[0].rect.width) if result.lines else result.text()
                        row = score(ref, actual) | {"page": num, "region": label,
                                                   "seconds": round(perf_counter() - tick, 3),
                                                   "raw": result.raw, "issues": result.issues}
                        if result.lines:
                            row["after_line_join"] = score(ref, in_region(result.lines, crop[0].rect,
                                                                         crop[0].rect.width, join=True))
                        rows.append(row)
                    save(f"{model}-{dpi}-{mode}", rows, perf_counter() - started)
            paddle.render_gray = original_render
            if args.full_pages or args.full_pages_only:
                def compare_pages(key, recognize, *, join):
                    started = perf_counter()
                    results = {}
                    failures = {}
                    for page in pdf:
                        try:
                            results[page.number + 1] = recognize(page)
                        except (OSError, RecognitionError, ValueError) as error:
                            failures[page.number + 1] = str(error)
                    elapsed = perf_counter() - started
                    rows = []
                    for num, label, box, ref in REGIONS:
                        if num not in results:
                            continue
                        lines, _ = results[num]
                        actual = in_region(lines, box, pdf[num - 1].rect.width, join=join)
                        fields = ("٢٣٧٨٤", "١٤٤٦/٤/٤", "٣٣", "١٤٤٤/١/١٠") if label == "right-column" else ()
                        rows.append(score(ref, actual) | {"page": num, "region": label,
                                                         "fields": [field in actual for field in fields]})
                    save(key, rows, elapsed)
                    report["runs"][key]["page_failures"] = failures
                    if failures:
                        report["runs"][key]["gazette"] = None
                    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                    (args.out / f"{key}.json").write_text(json.dumps(
                        {num: raw for num, (_, raw) in results.items()}, ensure_ascii=False, indent=2),
                        encoding="utf-8")

                packs = engine.available_langs(settings)
                if "ara" in packs and "tesseract" in args.models:
                    def tesseract(page):
                        lines, info = engine.ocr_page(page, "ar", 260, settings, {"ara"},
                                                      orient=False, deskew=False)
                        return lines, {**info, "lines": [line.__dict__ for line in lines]}

                    compare_pages("tesseract-pages-260", tesseract, join=False)
                page_configs = [(dpi, "raw") for dpi in args.page_dpi]
                if 300 in args.page_dpi:
                    page_configs.append((300, "otsu"))
                for dpi, mode in page_configs if "paddle" in args.models else ():
                    def paddle_page(page):
                        result = ocr.recognise(page, "ar", dpi)
                        return result.lines, result.raw

                    paddle.render_gray = lambda page, dpi: variant(mode, original_render(page, dpi), settings)
                    compare_pages(f"paddle-pages-{dpi}-{mode}", paddle_page, join=True)
    finally:
        ocr.close()


if __name__ == "__main__":
    main()

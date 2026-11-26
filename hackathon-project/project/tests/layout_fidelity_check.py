"""Run the real layout checkpoint against independently authored PDF fixtures."""
import argparse
from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import sys
from time import monotonic

from lxml import etree
import psutil
import pymupdf
from rapidfuzz.distance import Levenshtein

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.orchestrator import process_pdf
from config.settings import Settings
from quality.xml_alignment import evaluate_xml
from utils.logs import AuditLog
from utils.textutil import norm_ws
from xml_engine.assets import inspect_assets


def text_nodes(root, xpath):
    return [norm_ws("".join(node.itertext())) for node in root.xpath(xpath)]


def write_line(page, x, y, text, size=11, bold=False):
    font = "hebo" if bold else "helv"
    page.insert_text((x, y), text, fontsize=size, fontname=font)
    width = pymupdf.get_text_length(text, fontname=font, fontsize=size)
    return (x, y - size, x + width, y + size * .25)


def write_paragraph(page, x, y, lines):
    boxes = [write_line(page, x, y + 15 * index, text) for index, text in enumerate(lines)]
    return (x, boxes[0][1], max(box[2] for box in boxes), boxes[-1][3])


def technical(path):
    order = []
    labels = []
    running = {"headers": [], "footers": []}
    with pymupdf.open() as pdf:
        for index in range(2):
            page = pdf.new_page(width=600, height=780)
            header = "Municipal Pump Service Guide"
            footer = ["Field copy", str(41 + index), "Maintenance Unit"]
            labels.append((index + 1, "header", write_line(page, 50, 29, header, 9)))
            for x, value in zip((50, 295, 453), footer):
                label = "page_number" if value.isdecimal() else "footer"
                labels.append((index + 1, label, write_line(page, x, 752, value, 9)))
            running["headers"].append(header)
            running["footers"].extend(footer)
            if not index:
                labels.append((1, "title", write_line(page, 50, 80, "Water Pump Maintenance Manual", 23, True)))
            prefix = "Shutdown" if not index else "Restart"
            labels.append((index + 1, "title", write_line(page, 50, 134, f"{index + 1} {prefix}", 17, True)))
            labels.append((index + 1, "title", write_line(page, 50, 170, f"{index + 1}.1 Inspection sequence", 13, True)))
            paragraphs = [
                ("The left inlet valve must remain closed", "until the pressure returns to normal."),
                ("The second left test is made at low flow", "before the pressure gauge is removed."),
                ("The right outlet is inspected after the", "left side is cleared for safe access."),
                ("The last right inspection covers alarms", "and records the status of the device."),
            ]
            for lines, (x, y) in zip(paragraphs, [(50, 224), (50, 310), (320, 224), (320, 310)]):
                actual = (f"{prefix}: {lines[0]}", lines[1])
                order.append(" ".join(actual))
                write_paragraph(page, x, y, actual)
        pdf.save(path)
    return {"order": order, "headings": {f"{n} {name}": "h2" for n, name in ((1, "Shutdown"), (2, "Restart"))}
            | {f"{n}.1 Inspection sequence": "h3" for n in (1, 2)}, **running, "regions": labels}


def finance(path, bordered=True):
    table = [["Account", "January", "February"], ["Equipment", "12400", "14950"],
             ["Supplies", "3100", "2800"], ["Reserves", "58200", "59100"]]
    caption = "Figure 1. Spending comparison by measured quarter"
    intro = ("The finance office compared the first two months of the year.",
             "The figures are in dollars and the accounts are shown below.")
    with pymupdf.open() as pdf, pymupdf.open() as picture:
        page = pdf.new_page(width=600, height=780)
        write_line(page, 45, 64, "Monthly Financial Report", 24, True)
        write_paragraph(page, 45, 118, intro)
        if bordered:
            for x in (55, 215, 365, 515):
                page.draw_line((x, 185), (x, 305))
            for y in (185, 215, 245, 275, 305):
                page.draw_line((55, y), (515, y))
        for row, cells in enumerate(table):
            for col, value in enumerate(cells):
                write_line(page, 65 + 150 * col, 205 + 30 * row, value)
        visual = picture.new_page(width=300, height=145)
        for x, height in ((42, 30), (100, 50), (158, 65), (216, 84)):
            visual.draw_rect((x, 120 - height, x + 31, 120), fill=(.2, .47, .69))
        visual.draw_line((24, 121), (275, 121), width=2)
        image_box = (70, 390, 520, 608)
        page.insert_image(image_box, stream=visual.get_pixmap().tobytes("png"))
        caption_box = write_line(page, 81, 635, caption)
        pdf.save(path)
    return {"order": [" ".join(intro)], "table": table, "captions": [caption],
            "regions": [(1, "table", (55, 185, 515, 305)), (1, "image", image_box),
                        (1, "image_caption", caption_box)]}


def language_memo(path, scanned):
    paragraphs = [
        ("The service desk receives the completed forms for review.",
         "The request is filed with the team before the end of the day."),
        ("Dans ce rapport, les mesures sont prises près de la rivière.",
         "Les résultats ont été examinés par les membres de la commission."),
    ]
    with pymupdf.open() as original:
        page = original.new_page(width=520, height=700)
        write_line(page, 50, 65, "Service Desk Memo", 22, True)
        for y, lines in zip((125, 265), paragraphs):
            write_paragraph(page, 50, y, lines)
        if not scanned:
            original.save(path)
        else:
            with pymupdf.open() as scan:
                target = scan.new_page(width=520, height=700)
                target.insert_image(target.rect, stream=page.get_pixmap(dpi=260).tobytes("png"))
                scan.xref_set_key(scan.pdf_catalog(), "Lang", "(en)")
                scan.save(path)
    return {"order": [" ".join(lines) for lines in paragraphs], "languages": ["en", "fr"],
            "empty": ["figure_wrap", "table", "reference"]}


def research_note(path):
    references = ["Parker, A. 2021. A field guide to flow gauges. Applied Hydrology 7, 12-21.",
                  "Silva, B. 2023. Pressure measurements in open channels. Water Studies 9, 101-117."]
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=780)
        write_line(page, 48, 75, "Experimental Flow Measurements", 23, True)
        write_line(page, 48, 132, "1 Calibration", 16, True)
        write_paragraph(page, 48, 185, ("The flow is measured at three points along the test channel.",
                                       "The calibrated coefficient is multiplied by the local velocity."))
        formula = write_line(page, 180, 264, "P = 3 × v (1.1)", 14)
        write_line(page, 48, 385, "References", 16, True)
        for y, text in zip((435, 480), references):
            write_line(page, 48, y, text, 10)
        pdf.save(path)
    return {"headings": {"1 Calibration": "h2"}, "equations": ["1.1"],
            "references": references, "regions": [(1, "equation", formula)]}


def iou(first, second):
    x0, y0, x1, y1 = first
    a, b, c, d = second
    intersection = max(0, min(x1, c) - max(x0, a)) * max(0, min(y1, d) - max(y0, b))
    union = (x1 - x0) * (y1 - y0) + (c - a) * (d - b) - intersection
    return intersection / union if union else 0


def measure_regions(expected, pages):
    aliases = {"image_block": "image"}
    scope = {(page, label) for page, label, _ in expected}
    actual = [(page["n"], aliases.get(region["class_name"], region["class_name"]), region["bbox"])
              for page in pages for region in page["diag"]["layout"]["detections"]
              if (page["n"], aliases.get(region["class_name"], region["class_name"])) in scope]
    matched = Counter()
    overlaps = sorted(((iou(box, found), i, j) for i, (page, label, box) in enumerate(expected)
                       for j, (number, name, found) in enumerate(actual) if (page, label) == (number, name)),
                      reverse=True)
    used_expected, used_actual = set(), set()
    for overlap, i, j in overlaps:
        if overlap >= .5 and i not in used_expected and j not in used_actual:
            used_expected.add(i)
            used_actual.add(j)
            matched[expected[i][1]] += 1
    want = Counter(label for _, label, _ in expected)
    found = Counter(label for _, label, _ in actual)
    return {label: {"true_positive": matched[label], "annotated": want[label], "predicted": found[label],
                    "precision": matched[label] / found[label] if found[label] else None,
                    "recall": matched[label] / want[label]} for label in want}


def evaluate_fixture(data, evidence, truth):
    xml = etree.fromstring(data)
    text = " ".join(text_nodes(xml, ".//p[@kind='body']"))
    order = truth.get("order", [])
    positions = [text.find(value) for value in order]
    matches = [position for position in positions if position >= 0]
    pairs = [(a, b) for i, a in enumerate(matches) for b in matches[i + 1:]]
    reference = " ".join(order)
    keys = {norm_ws("".join(node.itertext())): node.tag for node in xml.xpath(".//h2|.//h3|.//h4")}
    table = [text_nodes(row, "./th|./td") for row in xml.findall(".//tr")]
    metrics = {
        "exact_prose_anchors": [len(matches), len(order)],
        "aligned_anchor_order_pairs": [sum(a < b for a, b in pairs), len(pairs)],
        "body_character_error_rate": Levenshtein.distance(reference, text) / len(reference) if reference else None,
        "heading_levels": [sum(keys.get(key) == level for key, level in truth.get("headings", {}).items()),
                           len(truth.get("headings", {}))],
        "languages": evidence["language"]["languages"],
    }
    for key, tag in (("headers", "page_header"), ("footers", "page_footer"), ("captions", "figcaption")):
        if key in truth:
            expected = Counter(truth[key])
            seen = Counter(text_nodes(xml, f".//{tag}"))
            metrics[key] = {"matched": sum((expected & seen).values()), "expected": sum(expected.values()),
                            "actual": sum(seen.values())}
    if "table" in truth:
        metrics["table"] = {"exact_grid": table == truth["table"], "rows": len(table),
                            "expected_rows": len(truth["table"])}
    if "empty" in truth:
        metrics["unexpected_regions"] = {tag: len(xml.findall(f".//{tag}")) for tag in truth["empty"]}
    if "equations" in truth:
        actual = xml.xpath(".//img[@type='formula' and @src]/@equation_number")
        metrics["equation_labels_with_crops"] = {"matched": sum((Counter(actual) & Counter(truth["equations"])).values()),
                                               "expected": len(truth["equations"]), "actual": actual}
    if "references" in truth:
        actual = Counter(text_nodes(xml, ".//reference"))
        metrics["references"] = {"matched": sum((actual & Counter(truth["references"])).values()),
                                 "expected": len(truth["references"]), "actual": sum(actual.values())}
    if "regions" in truth:
        metrics["region_iou_at_0.5"] = measure_regions(truth["regions"], evidence["pages"])
    return metrics


def run_fixture(path, output, ocr_engine, truth):
    settings = replace(Settings.load(default_schema_profile="sample"), ocr_backend=ocr_engine, docling_enabled=False)
    start = monotonic()
    cpu = psutil.Process().cpu_times()
    result = process_pdf(path, settings, output / "xml", output / "run", AuditLog(output / "audit.jsonl"))
    row = {"ok": result["ok"], "pdf_pages": result["pages"], "wall_seconds": round(monotonic() - start, 2),
           "process_cpu_seconds": round(sum(psutil.Process().cpu_times()[:2]) - sum(cpu[:2]), 2),
           "process_rss_after_bytes": psutil.Process().memory_info().rss,
           "validation": result.get("validation"), "error": result.get("error")}
    if result["ok"]:
        xml = (output / "xml" / f"{path.stem}.xml").read_bytes()
        evidence = json.loads((output / "run" / "ir" / f"{path.stem}.json").read_bytes())
        row["layout_pages"] = Counter(page["diag"]["layout"]["status"] for page in evidence["pages"])
        row["assets"] = inspect_assets(xml, output / "xml")
        if truth:
            row["metrics"] = evaluate_fixture(xml, evidence, truth)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--holdout", type=Path, action="append", default=[])
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--generated", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    result = {"scope": ("Authored fixtures have independent annotations for the reported categories only. "
                        "External holdouts report runtime and validation, not precision without ground truth. "
                        "CPU and RSS cover the orchestrator process, excluding the model workers."),
              "fixtures": {}, "holdouts": {}}
    for name, author in (("technical_manual", technical), ("financial_report", finance),
                         ("borderless_financial_report", lambda path: finance(path, bordered=False)),
                         ("research_note", research_note),
                         ("business_memo", lambda path: language_memo(path, False)),
                         ("scanned_memo", lambda path: language_memo(path, True))):
        path = output / f"{name}.pdf"
        truth = author(path)
        result["fixtures"][name] = run_fixture(path, output / name, "tesseract", truth)
        print(name, result["fixtures"][name]["ok"], flush=True)
    for source in args.holdout:
        path = output / f"{source.stem}-excerpt.pdf"
        with pymupdf.open(source) as pdf, pymupdf.open() as excerpt:
            excerpt.insert_pdf(pdf, from_page=0, to_page=min(1, len(pdf) - 1))
            excerpt.save(path)
        result["holdouts"][source.name] = run_fixture(path, output / path.stem, "tesseract", {})
        print(source.name, result["holdouts"][source.name]["ok"], flush=True)
    if args.reference and args.generated:
        result["engineering_comparison"] = evaluate_xml(args.reference.read_bytes(), args.generated.read_bytes())
        if args.baseline:
            result["baseline_comparison"] = evaluate_xml(args.reference.read_bytes(), args.baseline.read_bytes())
    (output / "verification.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if all(row["ok"] for row in [*result["fixtures"].values(), *result["holdouts"].values()]) else 1


if __name__ == "__main__":
    raise SystemExit(main())

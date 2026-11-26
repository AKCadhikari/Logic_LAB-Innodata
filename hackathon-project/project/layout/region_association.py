"""Ground existing extracted content in detected layout without replacing its text."""
from dataclasses import replace

from layout.model import Block


def coverage(box, region):
    x0, y0, x1, y1 = box
    a, b, c, d = region
    area = (x1 - x0) * (y1 - y0)
    return max(0, min(x1, c) - max(x0, a)) * max(0, min(y1, d) - max(y0, b)) / area if area else 0


GROUPS = {
    "table": {"table"}, "figure": {"image", "image_block"},
    "equation": {"equation", "equation_block"}, "ul": {"list"}, "ol": {"list"},
}
STRUCTURES = {"table", "image", "image_block", "equation", "equation_block"}


def match(box, regions, group=""):
    if not box:
        return None
    labels = GROUPS.get(group)
    candidates = []
    for region in regions:
        label = region["class_name"]
        if (labels is not None and label not in labels) or (labels is None and label in STRUCTURES):
            continue
        contained = coverage(box, region["bbox"])
        if contained >= .72:
            score = .75 * contained + .25 * coverage(region["bbox"], box)
            candidates.append((score, region))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    if not candidates or (len(candidates) > 1 and candidates[0][0] - candidates[1][0] < .04
                          and candidates[0][1]["class_name"] != candidates[1][1]["class_name"]):
        return None
    return candidates[0][1]


def prepare(record, regions):
    """Record where positioned OCR lines and structured OCR blocks land before reconstruction."""
    for block in record["blocks"] or ():
        if region := match(block.bbox, regions, block.kind if block.kind == "equation" else block.tag):
            block.source_reference["layout_region_id"] = region["id"]
    if record["source"] == "ocr":
        for line in record["lines"]:
            if line.source == "ocr" and (region := match(line.bbox, regions) or match(line.bbox, regions, "table")):
                region.setdefault("ocr_lines", []).append(line.text)


def associate(page, regions, *, classify=False):
    matched = 0
    for block in page.blocks:
        block.source_reference.pop("layout_region_id", None)
        block.source_reference.pop("layout_detection", None)
        block.source_reference.pop("caption_region_id", None)
        block.source_reference.pop("layout_class", None)
        block.source_reference.pop("layout_confidence", None)
        if region := match(block.bbox, regions, block.kind if block.kind == "equation" else block.tag):
            matched += 1
            block.source_reference["layout_region_id"] = region["id"]
            block.source_reference["layout_detection"] = region["source"]
            block.source_reference["layout_class"] = region["class_name"]
            block.source_reference["layout_confidence"] = region["confidence"]
            if classify and block.tag == "p" and block.kind in {"", "body"} and region["confidence"] >= .55:
                label = region["class_name"]
                if coverage(region["bbox"], block.bbox) >= .45:
                    block.tag = {"title": "h1" if page.n == 1 else "h2", "aside_text": "aside",
                                 "page_footnote": "aside"}.get(label, block.tag)
                    height = page.diag.get("source_height", page.height)
                    if len(block.text) <= 160 and (
                            label == "header" and block.bbox[3] <= .12 * height
                            or label == "footer" and block.bbox[1] >= .88 * height):
                        block.tag = label
                    if label == "image_caption":
                        block.kind = "figure_caption"
                    elif label == "page_footnote":
                        block.kind = "footnote"
                    elif label == "aside_text":
                        block.kind = "sidebar"
        # Captions that the existing reconstruction has joined to a visual keep their own region link.
        if region := match(block.source_reference.get("caption_bbox"), regions):
            block.source_reference["caption_region_id"] = region["id"]
    return matched


def add_visuals(page, regions):
    """Keep supported visual detections as source crops when existing extraction left them out."""
    labels = {"image": "figure", "image_block": "figure", "equation": "equation", "equation_block": "equation"}
    for region in sorted(regions, key=lambda item: item["class_name"].endswith("_block")):
        label, box = region["class_name"], region["bbox"]
        compound = label.endswith("_block")
        if label not in labels or region["confidence"] < (.72 if compound else .60):
            continue
        if compound and any(other["class_name"] == label.removesuffix("_block") and coverage(other["bbox"], box) >= .8
                            for other in regions):
            continue
        if any(block.bbox and max(coverage(box, block.bbox), coverage(block.bbox, box)) >= .45
               for block in page.blocks):
            continue
        visual = Block(tag="figure", kind=labels[label], bbox=box, source="layout",
                       source_reference={"geometry_method": region["source"]})
        before = next((i for i, block in enumerate(page.blocks) if block.bbox and block.bbox[1] > box[3]
                       and min(block.bbox[2], box[2]) > max(block.bbox[0], box[0])), len(page.blocks))
        page.blocks.insert(before, visual)


def merge_figure_panels(page, regions):
    """Create one crop when a compound visual covers panels without independent captions."""
    for region in regions:
        if region["class_name"] != "image_block" or region["confidence"] < .72:
            continue
        panels = [block for block in page.blocks if block.tag == "figure" and block.kind != "equation"
                  and block.bbox and coverage(block.bbox, region["bbox"]) >= .85]
        if len(panels) < 2 or any(block.caption for block in panels):
            continue
        boxes = [block.bbox for block in panels]
        union = (min(box[0] for box in boxes), min(box[1] for box in boxes),
                 max(box[2] for box in boxes), max(box[3] for box in boxes))
        if any(block.tag == "p" and block.kind in {"", "body"} and block.bbox
               and len(block.text) > 120 and coverage(block.bbox, union) >= .8 for block in page.blocks):
            continue
        grouped = replace(panels[0], bbox=union, source="layout", alt="",
                          source_reference={"geometry_method": region["source"], "figure_panels": boxes})
        identifiers = {id(block) for block in panels[1:]}
        page.blocks = [grouped if block is panels[0] else block for block in page.blocks if id(block) not in identifiers]


def retain_figure_text(page, regions):
    """Keep well-contained chart labels with their figure rather than in body flow."""
    images = [region for region in regions if region["class_name"] in GROUPS["figure"]
              and region["confidence"] >= .7]
    figures = [(block, region) for block in page.blocks if block.tag == "figure" and block.kind != "equation"
               and (region := match(block.bbox, images, "figure"))]
    kept = []
    for block in page.blocks:
        owners = [figure for figure, region in figures if block.tag == "p" and block.kind in {"", "body"}
                  and block.bbox and len(block.text) <= 120
                  and coverage(block.bbox, region["bbox"]) >= .95 and coverage(block.bbox, figure.bbox) >= .95]
        if len(owners) == 1:
            owners[0].source_reference.setdefault("embedded_text", []).append({"text": block.text, "bbox": block.bbox})
        else:
            kept.append(block)
    page.blocks = kept

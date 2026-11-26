"""Ground existing extracted content in detected layout without replacing its text."""
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
        if region := match(block.bbox, regions, block.kind if block.kind == "equation" else block.tag):
            matched += 1
            block.source_reference["layout_region_id"] = region["id"]
            block.source_reference["layout_detection"] = region["source"]
            if classify and block.tag == "p" and block.kind in {"", "body"} and region["confidence"] >= .55:
                label = region["class_name"]
                if coverage(region["bbox"], block.bbox) >= .45:
                    block.tag = {"title": "h1" if page.n == 1 else "h2", "header": "header",
                                 "footer": "footer", "aside_text": "aside",
                                 "page_footnote": "aside"}.get(label, block.tag)
                    if label == "image_caption":
                        block.kind = "figure_caption"
                    elif label == "page_footnote":
                        block.kind = "footnote"
        # Captions that the existing reconstruction has joined to a visual keep their own region link.
        if region := match(block.source_reference.get("caption_bbox"), regions):
            block.source_reference["caption_region_id"] = region["id"]
    return matched


def add_visuals(page, regions):
    """Keep supported visual detections as source crops when existing extraction left them out."""
    for region in regions:
        label, box = region["class_name"], region["bbox"]
        if label not in {"image", "equation"} or region["confidence"] < .6:
            continue
        if any(block.bbox and max(coverage(box, block.bbox), coverage(block.bbox, box)) >= .45
               for block in page.blocks):
            continue
        kind = "equation" if label == "equation" else "figure"
        visual = Block(tag="figure", kind=kind, bbox=box, source="native" if page.source == "native" else "layout",
                       source_reference={"geometry_method": region["source"]})
        before = next((i for i, block in enumerate(page.blocks) if block.bbox and block.bbox[1] > box[3]
                       and min(block.bbox[2], box[2]) > max(block.bbox[0], box[0])), len(page.blocks))
        page.blocks.insert(before, visual)

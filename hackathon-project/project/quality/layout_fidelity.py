"""Report detector/IR coverage and suspicious duplicates without asserting model truth."""
from collections import Counter, defaultdict

from layout.region_association import coverage
from utils.textutil import norm_cmp


def layout_fidelity(document):
    rows = []
    counts = Counter()
    for page in document.pages:
        detection = page.diag.get("layout", {})
        used = set()
        repeats = defaultdict(list)
        duplicates = []
        for block in page.blocks:
            evidence = block.source_reference
            used.update(value for key in ("layout_region_id", "caption_region_id") if (value := evidence.get(key)))
            used.update(evidence.get("layout_fragments", ()))
            key = norm_cmp(block.text)
            if key and block.bbox:
                if any(min(coverage(box, block.bbox), coverage(block.bbox, box)) >= .85 for box in repeats[key]):
                    duplicates.append(block.reading_order)
                repeats[key].append(block.bbox)
        regions = detection.get("detections", [])
        unmatched = [{"id": region["id"], "class": region["class_name"], "confidence": region["confidence"]}
                     for region in regions if region["confidence"] >= .60 and region["id"] not in used]
        counts.update(detections=len(regions), linked=sum(region["id"] in used for region in regions),
                      unlinked_high_confidence=len(unmatched), possible_duplicates=len(duplicates))
        rows.append({"page": page.n, "engine": detection.get("engine"), "status": detection.get("status"),
                     "region_counts": dict(Counter(region["class_name"] for region in regions)),
                     "unlinked_high_confidence": unmatched, "possible_duplicates": duplicates})
    return {"scope": "Detector-to-IR coverage. Unlinked regions require review and may be detector false positives.",
            "counts": dict(counts), "pages": rows}

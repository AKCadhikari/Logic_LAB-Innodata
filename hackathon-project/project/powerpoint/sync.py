"""Apply unambiguous document-version changes to an existing presentation."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from powerpoint.amend import editable_paragraph, is_furniture, iter_shapes, para_text, record_text_edit, text_paragraphs
from powerpoint.package import open_deck, write_deck


def synchronize(source: Path, output: Path, changes: list[dict]) -> dict:
    deck = open_deck(source)
    replacements = defaultdict(set)
    evidence = {}
    for change in changes:
        if change.get("can_sync"):
            replacements[change["old_content"]].add(change["new_content"])
            evidence[(change["old_content"], change["new_content"])] = change

    occurrences = defaultdict(list)
    for slide_number, slide in enumerate(deck.slides, 1):
        for shape in iter_shapes(slide.shapes):
            if is_furniture(shape):
                continue
            for index, (_, paragraph) in enumerate(text_paragraphs(shape), 1):
                original = para_text(paragraph)
                candidates = replacements.get(original)
                if candidates and len(candidates) == 1:
                    occurrences[original].append((slide_number, slide, shape, index, paragraph))
    applied = []
    matched = set()
    edits = defaultdict(list)
    for original, locations in occurrences.items():
        if len(locations) != 1:
            continue
        slide_number, slide, shape, index, paragraph = locations[0]
        if not editable_paragraph(paragraph):
            continue
        updated = next(iter(replacements[original]))
        record_text_edit(slide, paragraph, updated, edits)
        matched.add(original)
        change = evidence[(original, updated)]
        applied.append({"slide": slide_number, "shape_id": shape.shape_id, "shape": shape.name,
                        "paragraph": index, "old": original, "new": updated,
                        "source": change.get("source"), "confidence": change.get("confidence"),
                        "reason": "Exact paragraph match for a verified document-version change."})
    write_deck(source, output, edits, len(deck.slides))
    review = []
    for change in changes:
        original = change["old_content"]
        if not change.get("can_sync"):
            review.append({**change, "reason": change.get("reason", "Manual review required for this change.")})
        elif original not in matched:
            locations = occurrences.get(original, [])
            if len(locations) == 1:
                reason = "The matching paragraph contains a field or special break."
            else:
                reason = "More than one deck paragraph matches." if locations else "No exact deck paragraph match."
            review.append({**change, "reason": reason})
    return {"slides": len(deck.slides), "applied": applied, "review": review,
            "status": "updated" if edits else "unchanged"}

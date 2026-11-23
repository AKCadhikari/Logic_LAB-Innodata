"""Evaluate slide-local numbers and repair placeholder caches in place."""
from __future__ import annotations

from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.oxml.ns import qn

PLACEHOLDERS = {"‹#›", "<#>", "〈#〉"}


def number_nodes(slide):
    for shape in slide.shapes:
        for field in shape._element.iter(qn("a:fld")):
            if field.get("type", "").casefold() == "slidenum":
                if (node := field.find(qn("a:t"))) is not None:
                    yield shape, node, "field"
        identified = shape.name.casefold() == "slide-number" or (
            shape.is_placeholder and shape.placeholder_format.type == PP_PLACEHOLDER.SLIDE_NUMBER)
        if identified:
            for run in shape._element.iter(qn("a:r")):
                if (node := run.find(qn("a:t"))) is not None:
                    yield shape, node, "slide-number shape"


def numbering(deck, edits=None) -> list[dict]:
    from powerpoint.package import RunEdit

    first = int(deck._element.get("firstSlideNum", "1"))
    results = []
    for index, slide in enumerate(deck.slides, 1):
        expected = str(first + index - 1)
        part = str(slide.part.partname).lstrip("/")
        for shape, node, kind in number_nodes(slide):
            original = node.text or ""
            updated = expected if edits is not None and original.strip() in PLACEHOLDERS else original
            if updated != original:
                path = slide._element.getroottree().getelementpath(node)
                edits[part].append(RunEdit(path, original, updated))
            results.append({"slide": index, "shape_id": shape.shape_id, "shape": shape.name,
                            "kind": kind, "part": part, "expected": expected, "old": original, "new": updated,
                            "status": "CORRECT" if updated == expected else "MANUAL_REVIEW_REQUIRED"})
        if not results or results[-1]["slide"] != index:
            results.append({"slide": index, "expected": expected, "part": part, "status": "UNCERTAIN",
                            "reason": "The slide has no explicit slide-number field or shape."})
    return results

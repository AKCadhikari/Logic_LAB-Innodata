"""Restore one unambiguous item in a verbatim PowerPoint bullet sequence."""
from copy import deepcopy

from pptx.oxml.ns import qn

from comparison.differ import ws_lower
from powerpoint.formulas import canonical
from powerpoint.package import RunEdit


def source_lists(ir):
    for page in ir.pages:
        if page.requires_review or page.diag.get("health") != "healthy":
            continue
        for block in page.blocks:
            if block.tag != "ul" or block.source != "native" or any(
                    item.source != "native" or item.validation_status == "manual_review_required"
                    for item in block.children):
                continue
            yield page.n, block.children


def missing_bullet(slide, shape, candidates, prior_edits):
    paragraphs = shape.text_frame.paragraphs
    if len(paragraphs) < 3 or shape.width is None or shape.height is None:
        return None
    body = shape.text_frame._txBody
    path = slide._element.getroottree().getelementpath(body)
    if any(edit.path.startswith(path + "/") for edit in prior_edits):
        return None
    bullets = [paragraph._p.find("./" + qn("a:pPr") + "/" + qn("a:buChar")) for paragraph in paragraphs]
    if any(bullet is None for bullet in bullets) or len({p.level for p in paragraphs}) != 1:
        return None
    if len({bullet.get("char") for bullet in bullets}) != 1 or any(
            len(paragraph.runs) != 1 or paragraph.runs[0].hyperlink.address or any(
                child.tag not in {qn("a:pPr"), qn("a:r"), qn("a:endParaRPr")} for child in paragraph._p)
            for paragraph in paragraphs):
        return None
    shown = [ws_lower(paragraph.text) for paragraph in paragraphs]
    if len(set(shown)) != len(shown) or any(not item for item in shown):
        return None
    matches = []
    for page, items in candidates:
        if len(items) != len(shown) + 1:
            continue
        expected = [ws_lower(item.text) for item in items]
        for offset in range(1, len(expected) - 1):
            if expected[:offset] + expected[offset+1:] == shown and expected[offset] not in shown:
                matches.append((page, offset, items[offset]))
    if len(matches) != 1:
        return None
    page, index, source = matches[0]
    if not source.bbox or "\n" in source.text:
        return None
    template = paragraphs[index - 1]
    font = template.runs[0].font.size
    if not font or any(paragraph.runs[0].font.size != font for paragraph in paragraphs):
        return None
    bullets_height = int(font * (len(paragraphs) + 1) * 1.75)
    available_height = shape.height - shape.text_frame.margin_top - shape.text_frame.margin_bottom
    available_width = shape.width - shape.text_frame.margin_left - shape.text_frame.margin_right - 2 * font
    if available_height < bullets_height or any(
            len(value) * font * .85 > available_width for value in [source.text, *shown]):
        return None
    patched = deepcopy(body)
    clone = deepcopy(template._p)
    clone.find("./" + qn("a:r") + "/" + qn("a:t")).text = source.text
    target = patched.findall(qn("a:p"))[index]
    target.addprevious(clone)
    proof = {"page": page, "bbox": source.bbox, "text": source.text, "extraction": source.source,
             "block_id": source.source_reference.get("block_id")}
    return index + 1, proof, RunEdit(path, canonical(body), canonical(patched), qn("p:txBody"))

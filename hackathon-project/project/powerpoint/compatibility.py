"""Read authoritative DrawingML choices hidden behind AlternateContent wrappers."""
from difflib import SequenceMatcher
import re

from pptx.oxml.ns import qn

from powerpoint.package import MC, RunEdit
from powerpoint.formulas import edits_for, normalized, office_tokens

EQUATION_REFERENCE = re.compile(r"\b(?:eq\.?|equation)\s*\((\d+(?:\.\d+)*)\)", re.I)
INTERVAL = re.compile(r"\[\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*\]")


def line_key(value):
    value = EQUATION_REFERENCE.sub("equation reference", value)
    value = re.sub(r"\\(?:geq?|leq?|leftarrow|rightarrow|text)\b", " ", value)
    return tuple(re.findall(r"[^\W_]+", value.casefold()))


def inline_intervals(root, evidence):
    """Match a closed numeric interval using the surrounding variable and prose."""
    def words(value):
        return re.findall(r"[^\W\d_]+", value.casefold())

    sources = {}
    for unit in evidence:
        text = unit.text.replace("\N{MINUS SIGN}", "-")
        for match in INTERVAL.finditer(text):
            anchor = words(text[:match.start()])[-6:]
            if len(anchor) == 6:
                sources.setdefault(tuple(anchor), []).append((unit, normalized(match[0])))
    if not sources:
        return
    for paragraph in root.iter(qn("a:p")):
        if any(parent.tag == f"{{{MC}}}Fallback" for parent in paragraph.iterancestors()):
            continue
        context = ""
        for item in paragraph:
            equation = next(item.iter(qn("m:oMath")), None)
            if equation is None:
                context += "".join(child.text or "" for child in item.iter(qn("a:t")))
                continue
            tape = office_tokens(equation)
            interval = "".join(tape.tokens) if tape else ""
            anchor = words(context)[-6:]
            if not INTERVAL.fullmatch(interval):
                context += interval
                continue
            candidates = sources.get(tuple(anchor), ())
            if len({value for _, value in candidates}) != 1:
                continue
            unit, value = next((item for item in candidates if item[0].reliable), candidates[0])
            if value == interval:
                continue
            owner = next(paragraph.iterancestors(qn("p:sp")), None)
            marker = owner.find(".//" + qn("p:cNvPr")) if owner is not None else None
            locator = {"shape_id": int(marker.get("id")) if marker is not None else None,
                       "shape": marker.get("name") if marker is not None else "Office equation",
                       "equation_path": root.getroottree().getelementpath(equation), "object_type": "office_math"}
            patches = edits_for(tape, list(value), root.getroottree())
            yield locator, interval, value, unit, " ".join(anchor[-4:]), patches


def algorithm_references(root, ir):
    candidates = []
    for page in ir.pages:
        for block in page.blocks:
            lines = block.text.splitlines() if block.text else []
            if (len(lines) >= 5 and len(block.line_boxes) == len(lines)
                    and (block.kind == "pseudocode" or sum(bool(EQUATION_REFERENCE.search(line)) for line in lines) >= 2)):
                candidates.append((page, block, lines, [line_key(line) for line in lines]))
    if not candidates:
        return
    for choice in root.iter(f"{{{MC}}}Choice"):
        for shape in choice.findall(qn("p:sp")):
            marker = shape.find(".//" + qn("p:cNvPr"))
            if marker is None:
                continue
            paras = [p for p in shape.iter(qn("a:p")) if any(node.text for node in p.iter(qn("a:t"), qn("m:t")))]
            if len(paras) < 5:
                continue
            keys = [line_key("".join((node.text or "") if node.tag == qn("a:t") else normalized(node.text or "")
                                     for node in p.iter(qn("a:t"), qn("m:t")))) for p in paras]
            ranked = []
            for page, block, lines, source_keys in candidates:
                mapping = {left + i: right + i for left, right, count in
                           SequenceMatcher(None, keys, source_keys, autojunk=False).get_matching_blocks()
                           for i in range(count)}
                ranked.append((len(mapping), page, block, lines, mapping))
            ranked.sort(key=lambda item: item[0], reverse=True)
            score, page, block, lines, mapping = ranked[0]
            if score < max(5, len(paras) * .65) or (len(ranked) > 1 and score - ranked[1][0] < 3):
                continue
            for index, source_index in mapping.items():
                if mapping.get(index - 1) != source_index - 1 or mapping.get(index + 1) != source_index + 1:
                    continue
                paragraph = paras[index]
                if next(paragraph.iter(qn("m:oMath")), None) is not None:
                    continue
                nodes = list(paragraph.iter(qn("a:t")))
                old = "".join(node.text or "" for node in nodes)
                actual, wanted = EQUATION_REFERENCE.search(old), EQUATION_REFERENCE.search(lines[source_index])
                if not actual or not wanted or actual[1] == wanted[1]:
                    continue
                start, stop = actual.span(1)
                offset = 0
                for node in nodes:
                    value = node.text or ""
                    if offset <= start < stop <= offset + len(value):
                        new = value[:start-offset] + wanted[1] + value[stop-offset:]
                        edit = RunEdit(root.getroottree().getelementpath(node), value, new)
                        yield ({"shape_id": int(marker.get("id")), "shape": marker.get("name"), "paragraph": index + 1},
                               old, old[:start] + wanted[1] + old[stop:], page, block, source_index, lines[source_index], edit)
                        break
                    offset += len(value)

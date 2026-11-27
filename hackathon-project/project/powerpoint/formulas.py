"""Compare editable Office math with source-verified PDF expression trees."""
from dataclasses import dataclass
from copy import deepcopy
import re
import unicodedata
from difflib import SequenceMatcher

from pptx.oxml.ns import qn
from lxml import etree
from layout.equations import equation_number
from powerpoint.package import MC, RunEdit


SYMBOLS = {"cdot": "·", "times": "×", "pm": "±", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥",
           "neq": "≠", "infty": "∞", "sum": "∑", "int": "∫", "vert": "|", "{": "{", "}": "}"}
SYMBOLS.update(zip(("alpha", "beta", "gamma", "delta", "epsilon", "lambda", "mu", "pi", "rho",
                    "sigma", "tau", "theta", "phi", "omega"), "αβγδελμπρσ τθφω".replace(" ", "")))
MARKERS = {"vector": ("@vector", "@endvector"), "power": ("@power", "@endpower"),
           "sub": ("@sub", "@endsub"), "fraction": ("@fraction", "@endfraction"), "root": ("@root", "@endroot")}
FUNCTIONS = {"sin", "cos", "tan", "exp", "log", "ln"}


def normalized(value):
    return "".join(unicodedata.normalize("NFKC", value).translate(str.maketrans("−∗⋅", "-*·")).split())


def wrap(kind, contents):
    start, end = MARKERS[kind]
    return [start, *contents, end]


def latex_tokens(text):
    """Parse a conservative TeX subset. Unknown commands never authorize edits."""
    parts = iter(re.findall(r"\\[A-Za-z]+|\\.|[^\s]", text))

    def item(token):
        if token == "{":
            return sequence(group=True)
        if token in {"^", "_"}:
            return wrap("power" if token == "^" else "sub", item(next(parts)))
        if token in {r"\vec", r"\overrightarrow"}:
            contents = item(next(parts))
            if len(contents) >= 4 and len(contents[0]) == 1 and contents[1] == "@sub" and contents[-1] == "@endsub":
                return [*wrap("vector", contents[:1]), *contents[1:]]
            return wrap("vector", contents)
        if token == r"\frac":
            return wrap("fraction", [*item(next(parts)), "@denominator", *item(next(parts))])
        if token == r"\sqrt":
            return wrap("root", item(next(parts)))
        if token.startswith("\\"):
            name = token[1:]
            if name in {"left", "right", ",", "!", ";", "quad", "qquad"}:
                return []
            if name in {"mathrm", "text", "operatorname"}:
                return item(next(parts))
            if name in FUNCTIONS:
                return list(name)
            if name in SYMBOLS:
                return [SYMBOLS[name]]
            raise ValueError("Unsupported TeX command")
        return list(normalized(token))

    def sequence(group=False):
        result = []
        for token in parts:
            if token == "}":
                if group:
                    return result
                raise ValueError("Unbalanced TeX group")
            result.extend(item(token))
        if group:
            raise ValueError("Unbalanced TeX group")
        return result

    try:
        return sequence()
    except (ValueError, StopIteration, RecursionError):
        return None


@dataclass
class Tape:
    tokens: list
    leaves: list


def office_tokens(node):
    tape = Tape([], [])

    def structural(token):
        tape.tokens.append(token)
        tape.leaves.append(None)

    def emit(element):
        if element is None or not isinstance(element.tag, str):
            return
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "t":
            for offset, char in enumerate(element.text or ""):
                for token in normalized(char):
                    tape.tokens.append(token)
                    tape.leaves.append((element, offset))
        elif tag in {"sSup", "sSub", "sSubSup"}:
            emit(element.find(qn("m:e")))
            for field, kind in (("sub", "sub"), ("sup", "power")):
                child = element.find(qn("m:" + field))
                if child is not None:
                    start, end = MARKERS[kind]
                    structural(start)
                    emit(child)
                    structural(end)
        elif tag == "acc":
            char = element.find("./" + qn("m:accPr") + "/" + qn("m:chr"))
            if char is None or char.get(qn("m:val")) not in {"→", "⃗", "⟶"}:
                raise ValueError("Unsupported accent")
            start, end = MARKERS["vector"]
            structural(start)
            emit(element.find(qn("m:e")))
            structural(end)
        elif tag == "d":
            for field, default in (("begChr", "("), ("e", ""), ("endChr", ")")):
                if field == "e":
                    children = element.findall(qn("m:e"))
                    if len(children) != 1:
                        raise ValueError("Unsupported group")
                    emit(children[0])
                else:
                    child = element.find("./" + qn("m:dPr") + "/" + qn("m:" + field))
                    value = child.get(qn("m:val"), default) if child is not None else default
                    for token in normalized(value):
                        structural(token)
        elif tag in {"f", "rad"}:
            if tag == "rad" and (degree := element.find(qn("m:deg"))) is not None and "".join(degree.itertext()).strip():
                raise ValueError("Unsupported root index")
            start, end = MARKERS["fraction" if tag == "f" else "root"]
            structural(start)
            emit(element.find(qn("m:num" if tag == "f" else "m:e")))
            if tag == "f":
                structural("@denominator")
                emit(element.find(qn("m:den")))
            structural(end)
        elif tag in {"oMath", "oMathPara", "e", "num", "den", "sup", "sub", "r", "func", "fName"}:
            for child in element:
                emit(child)
        elif not (tag.endswith("Pr") or tag in {"ctrlPr", "brk"}):
            raise ValueError("Unsupported Office math structure")

    try:
        emit(node)
    except (ValueError, RecursionError):
        return None
    return tape


def styled_character(old, new):
    if new == "-" and old in "+−-":
        return "−"
    name = unicodedata.name(old, "")
    base = normalized(old)
    if name.startswith("MATHEMATICAL ") and len(base) == 1 and base.isascii() and new.isascii():
        old_name = unicodedata.name(base, "")
        new_name = unicodedata.name(new, "")
        if old_name and new_name and old_name.split(" ")[0] == new_name.split(" ")[0]:
            try:
                return unicodedata.lookup(name.removesuffix(old_name.rsplit(" ", 1)[-1]) + new_name.rsplit(" ", 1)[-1])
            except KeyError:
                pass
    return new


def edits_for(tape, expected, root):
    replacements = {}
    for tag, i, j, a, b in SequenceMatcher(None, tape.tokens, expected, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert" and 1 <= b - a <= 2 and i < len(tape.leaves) and tape.leaves[i] is not None:
            if any(token.startswith("@") or len(token) != 1 for token in expected[a:b]):
                return []
            node, offset = tape.leaves[i]
            if len(normalized(node.text[offset])) != 1:
                return []
            replacements.setdefault(node, []).append((offset, 0, "".join(expected[a:b])))
            continue
        if (tag != "replace" or j - i != b - a or j - i > 3 or any(item is None for item in tape.leaves[i:j])
                or any(token.startswith("@") or len(token) != 1 for token in expected[a:b])):
            return []
        for (node, offset), value in zip(tape.leaves[i:j], expected[a:b]):
            if len(normalized(node.text[offset])) != 1:
                return []
            replacements.setdefault(node, []).append((offset, 1, styled_character(node.text[offset], value)))
    result = []
    for node, changes in replacements.items():
        text = node.text
        for offset, length, value in sorted(changes, reverse=True):
            text = text[:offset] + value + text[offset + length:]
        result.append(RunEdit(root.getelementpath(node), node.text, text, qn("m:t")))
    return result


def append_verified_terms(node, tape, expected, root):
    """Add a verified trailing scalar/vector term without restructuring existing Office math."""
    if expected[:len(tape.tokens)] != tape.tokens:
        return []
    tail = expected[len(tape.tokens):]
    if not 2 <= len(tail) <= 12 or tail[0] not in {"·", "×", "+", "-"}:
        return []
    clone = deepcopy(node)
    sample = next((item for item in node.iter(qn("m:r")) if item.find(qn("m:t")) is not None), None)
    sample_vector = next(node.iter(qn("m:acc")), None)

    def add_run(parent, value):
        run = etree.SubElement(parent, qn("m:r"))
        if sample is not None:
            for child in sample:
                if child.tag in {qn("m:rPr"), qn("a:rPr")}:
                    run.append(deepcopy(child))
        etree.SubElement(run, qn("m:t")).text = "−" if value == "-" else value

    index = 0
    while index < len(tail):
        token = tail[index]
        if (token == "@vector" and sample_vector is not None and index + 2 < len(tail)
                and len(tail[index + 1]) == 1 and tail[index + 2] == "@endvector"):
            vector = etree.SubElement(clone, qn("m:acc"))
            props = sample_vector.find(qn("m:accPr"))
            if props is not None:
                vector.append(deepcopy(props))
            add_run(etree.SubElement(vector, qn("m:e")), tail[index + 1])
            index += 3
        elif len(token) == 1:
            add_run(clone, token)
            index += 1
        else:
            return []
    checked = office_tokens(clone)
    if checked and checked.tokens == expected:
        def canonical(value):
            return etree.tostring(value, method="c14n", exclusive=True).decode("utf-8")
        return [RunEdit(root.getelementpath(node), canonical(node), canonical(clone), qn("m:oMath"))]
    return []


def equation_evidence(ir):
    for page in ir.pages:
        for block in page.blocks:
            proof = block.source_reference.get("formula", {})
            if (block.kind != "equation" or not block.bbox or proof.get("status") not in
                    {"two_pass_agreement", "source_reviewed", "native_text"}):
                continue
            content = block.text
            label = block.marker or equation_number(content)
            if label and content.rstrip().endswith(f"({label})"):
                content = content.rstrip()[:-len(label) - 2]
            expected = latex_tokens(content) if proof.get("encoding") == "latex" else list(normalized(content))
            yield (label, expected, {"page": page.n, "bbox": block.bbox, "text": content,
                                    "block_id": block.source_reference.get("block_id"),
                                    "extraction": block.source, "formula_status": proof["status"]})


def inspect_formulas(root, evidence, hinted):
    for index, node in enumerate(root.iter(qn("m:oMath")), 1):
        if any(parent.tag == f"{{{MC}}}Fallback" for parent in node.iterancestors()):
            continue
        text = "".join(child.text or "" for child in node.iter(qn("m:t")))
        if "=" not in normalized(text) or len(normalized(text)) < 5:
            continue
        paragraph = next(node.iterancestors(qn("a:p")), None)
        label = ""
        if paragraph is not None:
            plain = "".join(child.text or "" for child in paragraph.iter(qn("a:t")))
            label = equation_number(plain.strip())
        tape = office_tokens(node)
        shape = next(node.iterancestors(qn("p:sp")), None)
        identity = shape.find(".//" + qn("p:cNvPr")) if shape is not None else None
        target = {"shape_id": int(identity.get("id")) if identity is not None else None,
                  "shape": identity.get("name") if identity is not None else "Office equation",
                  "equation_index": index, "object_type": "office_math"}
        if tape is None:
            matching = [proof for marker, _, proof in evidence if label and marker == label]
            proof = matching[0] if len(matching) == 1 else None
            yield target, text, proof["text"] if proof else "", proof, [], "This Office math structure requires a visual or manual check."
            continue
        formula = tape.tokens
        candidates = [(SequenceMatcher(None, formula, expected, autojunk=False).ratio(), marker, expected, proof)
                      for marker, expected, proof in evidence if expected and (not label or marker == label)]
        candidates.sort(key=lambda item: (-item[0], item[3]["page"] not in hinted))
        if not candidates or candidates[0][0] < .85:
            if label:
                yield target, text, "", None, [], "The printed equation label did not identify a compatible verified source formula."
            continue
        score, _, expected, proof = candidates[0]
        if expected == formula:
            continue
        competing = any(score - candidate[0] < .075 and candidate[2] != expected for candidate in candidates[1:])
        changes = [] if competing else (edits_for(tape, expected, root.getroottree())
                                        or append_verified_terms(node, tape, expected, root.getroottree()))
        reason = ("Another source equation is too close to distinguish." if competing else
                  "Verified PDF and Office equation structures agree at the edited tokens." if changes else
                  "The source differs structurally. A verified replacement would require rebuilding this equation.")
        if changes:
            target["equation_path"] = root.getroottree().getelementpath(node)
        yield target, text, proof["text"], {**proof, "alignment": score, "expected_tokens": expected}, changes, reason

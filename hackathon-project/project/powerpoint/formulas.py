"""Compare editable Office math with source-verified PDF expression trees."""
from dataclasses import dataclass
from copy import deepcopy
import re
import unicodedata
from difflib import SequenceMatcher

from pptx.oxml.ns import qn
from lxml import etree
from layout.equations import equation_number
from layout.formula_validation import polynomial_equivalence
from layout.math_structure import mathml, source_equations
from powerpoint.package import MC, RunEdit


SYMBOLS = {"cdot": "·", "times": "×", "pm": "±", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥",
           "neq": "≠", "infty": "∞", "sum": "∑", "int": "∫", "vert": "|", "div": "÷",
           "approx": "≈", "partial": "∂", "to": "→", "prod": "∏", "mp": "∓", "{": "{", "}": "}"}
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
    for row, root in source_equations(ir):
        if (row["kind"] != "display" or not row["bbox"] or root is None or row["status"] not in
                {"two_pass_agreement", "source_reviewed", "native_text"}):
            continue
        expected = latex_tokens(row["latex"])
        yield row["number"] or "", expected, {"page": row["page"], "bbox": row["bbox"],
            "text": row["latex"] if row["encoding"] == "latex" else row["unicode"], "block_id": row["id"],
            "extraction": row["extraction"], "formula_status": row["status"],
            "extraction_confidence": row["recognition_confidence"], "execution_ids": row["execution_ids"],
            "equation_latex": row["latex"], "equation_mathml": row["mathml"], "equation_number": row["number"]}


def equation_prefix(tokens):
    return next((tokens[:index] for index, token in enumerate(tokens) if token in "=≠≤≥≈<>"), [])


def native_equation(latex, tokens, template=None):
    """Build OMML from an expression tree so scripts retain their full base."""
    if not tokens or len(tokens) > 240:
        return None
    structure = mathml(latex)
    if structure is None:
        return None
    sample = next(template.iter(qn("m:r")), None) if template is not None else None
    root = etree.Element(qn("m:oMath"))

    def run(parent, token):
        node = etree.SubElement(parent, qn("m:r"))
        if sample is not None:
            for child in sample:
                if child.tag in {qn("m:rPr"), qn("a:rPr")}:
                    node.append(deepcopy(child))
        etree.SubElement(node, qn("m:t")).text = "−" if token == "-" else token

    def write(source, parent):
        kind = etree.QName(source).localname
        child_layout = {"msup": ("sSup", ("e", "sup")), "msub": ("sSub", ("e", "sub")),
                        "msubsup": ("sSubSup", ("e", "sub", "sup")), "mfrac": ("f", ("num", "den"))}
        if kind in {"math", "mrow"}:
            for child in source:
                write(child, parent)
        elif kind in child_layout:
            tag, fields = child_layout[kind]
            if len(source) != len(fields):
                raise ValueError("Malformed script or fraction")
            node = etree.SubElement(parent, qn("m:" + tag))
            for child, field in zip(source, fields):
                write(child, etree.SubElement(node, qn("m:" + field)))
        elif kind == "msqrt":
            node = etree.SubElement(parent, qn("m:rad"))
            properties = etree.SubElement(node, qn("m:radPr"))
            etree.SubElement(properties, qn("m:degHide")).set(qn("m:val"), "1")
            etree.SubElement(node, qn("m:deg"))
            content = etree.SubElement(node, qn("m:e"))
            for child in source:
                write(child, content)
        elif kind == "mover" and len(source) == 2 and (source[1].text or "") in {"→", "⃗", "⟶"}:
            node = etree.SubElement(parent, qn("m:acc"))
            properties = etree.SubElement(node, qn("m:accPr"))
            etree.SubElement(properties, qn("m:chr")).set(qn("m:val"), "⃗")
            write(source[0], etree.SubElement(node, qn("m:e")))
        elif kind in {"mi", "mn", "mo", "mtext"} and source.text:
            run(parent, source.text)
        else:
            raise ValueError("Unsupported Office equation node")

    try:
        write(structure, root)
        check = office_tokens(root)
        return root if check is not None and check.tokens == tokens else None
    except (ValueError, RecursionError):
        return None


def canonical(node):
    return etree.tostring(node, method="c14n", exclusive=True).decode("utf-8")


def inspect_formulas(root, evidence, hinted):
    for index, node in enumerate(root.iter(qn("m:oMath")), 1):
        if any(parent.tag == f"{{{MC}}}Fallback" for parent in node.iterancestors()):
            continue
        text = "".join(child.text or "" for child in node.iter(qn("m:t")))
        paragraph = next(node.iterancestors(qn("a:p")), None)
        label = ""
        if paragraph is not None:
            plain = "".join(child.text or "" for child in paragraph.iter(qn("a:t")))
            label = equation_number(plain.strip())
        if not label and (not equation_prefix(list(normalized(text))) or len(normalized(text)) < 5):
            continue
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
        if not candidates:
            if label:
                yield target, text, "", None, [], "The printed equation label did not identify a compatible verified source formula."
            continue
        score, _, expected, proof = candidates[0]
        if expected == formula:
            continue
        labelled = (label and len([item for item in evidence if item[0] == label]) == 1 and
                    equation_prefix(formula) and equation_prefix(formula) == equation_prefix(expected))
        if score < .85 and not labelled:
            if label:
                yield target, text, "", proof, [], "The equation does not share enough verified source structure."
            continue
        competing = any(score - candidate[0] < .075 and candidate[2] != expected for candidate in candidates[1:])
        changes = [] if competing else (edits_for(tape, expected, root.getroottree())
                                        or append_verified_terms(node, tape, expected, root.getroottree()))
        if labelled and not competing and not changes:
            replacement = native_equation(proof["equation_latex"], expected, node)
            if replacement is not None:
                path = root.getroottree().getelementpath(node)
                changes = [RunEdit(path, canonical(node), canonical(replacement), qn("m:oMath"))]
                target["native_equation_rebuilt"] = True
        reason = ("Another source equation is too close to distinguish." if competing else
                  "Verified PDF and Office equation structures agree at the edited tokens." if changes else
                  "The source differs structurally. A verified replacement would require rebuilding this equation.")
        if changes:
            target["equation_path"] = root.getroottree().getelementpath(node)
        symbolic = None
        if (all(len(token) == 1 for token in formula) and all(len(token) == 1 for token in expected)):
            symbolic = polynomial_equivalence("".join(formula), "".join(expected))
        yield target, text, proof["text"], {**proof, "alignment": score, "expected_tokens": expected,
                                          "symbolic_equivalence": symbolic}, changes, reason

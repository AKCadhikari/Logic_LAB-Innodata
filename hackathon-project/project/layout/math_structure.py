"""Structured source mathematics. Transcription status remains separate from syntax."""
from itertools import groupby
import re
import unicodedata

from latex2mathml.converter import convert
from lxml import etree

from layout.equations import NUMBER, TAG, equation_number

MATHML = "http://www.w3.org/1998/Math/MathML"
SCRIPT = dict(zip("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎",
                  "0123456789+-=()0123456789+-=()"))
SPECIAL = dict(zip("−×÷·±∓≤≥≠≈∞∑∏∫∂∇→∈∀∃",
                   ("-", "times", "div", "cdot", "pm", "mp", "leq", "geq", "neq", "approx", "infty",
                    "sum", "prod", "int", "partial", "nabla", "to", "in", "forall", "exists")))


def native_latex(text, spans=()):
    def encode(value):
        result = []
        for char in value:
            name = unicodedata.name(char, "")
            if char in SPECIAL:
                token = SPECIAL[char]
                result.append("-" if token == "-" else "\\" + token + " ")
            elif name.startswith(("GREEK SMALL LETTER ", "GREEK CAPITAL LETTER ")):
                symbol = name.split()[-1]
                command = symbol.lower() if char.islower() else symbol.title()
                if char == "ς":
                    result.append("\\varsigma ")
                elif command in {"Alpha", "Beta", "Epsilon", "Zeta", "Eta", "Iota", "Kappa", "Mu", "Nu",
                                  "Omicron", "Rho", "Tau", "Chi", "omicron"}:
                    result.append(char)
                else:
                    result.append("\\" + command + " ")
            elif char in "{}%#$&_":
                result.append("\\" + char)
            else:
                result.append(char)
        return "".join(result)

    def encode_scripts(value):
        result = []
        for match in re.finditer(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾]+|[₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎]+|[^⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎]+", value):
            group = match[0]
            if group[0] in SCRIPT and result and "".join(result).strip():
                operator = "_" if group[0] in "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎" else "^"
                result.append(operator + "{" + "".join(SCRIPT[char] for char in group) + "}")
            else:
                result.append(encode(group))
        return "".join(result)

    if spans and "".join(span.text for span in spans).strip().startswith(text):
        output = []
        remaining = len(text)
        for span in spans:
            value = span.text[:remaining]
            remaining -= len(value)
            if value.strip() and (span.sup or span.sub) and output:
                output.append(("_" if span.sub else "^") + "{" + encode(value.strip()) + "}")
            else:
                output.append(encode_scripts(value))
            if remaining <= 0:
                break
        return "".join(output)
    return encode_scripts(text)


def mathml(latex):
    if not latex or len(latex) > 3500 or latex.count("{") > 320:
        return None
    try:
        root = etree.fromstring(convert(latex).encode(), parser=etree.XMLParser(resolve_entities=False, no_network=True))
    except (ValueError, IndexError, KeyError, RecursionError, etree.XMLSyntaxError):
        return None
    except Exception as exc:
        # Conversion errors specific to latex2mathml are invalid source transcriptions.
        if exc.__class__.__module__.startswith("latex2mathml"):
            return None
        raise
    if root.tag != f"{{{MATHML}}}math" or any(
        node.tag == f"{{{MATHML}}}merror" or (node.text or "").startswith("\\") for node in root.iter()
    ):
        return None
    return root


def signature(root):
    """Keep symbol order, fences, and script/array arity for exact fidelity checks."""
    if root is None:
        return None
    tag = etree.QName(root).localname
    children = tuple(signature(child) for child in root)
    if tag in {"math", "mrow"}:
        # Formatting braces and redundant row wrappers do not change the expression.
        children = tuple(item for child in children for item in (child[1] if child[0] == "mrow" else (child,)))
        return ("mrow", children)
    return (tag, unicodedata.normalize("NFC", (root.text or "").strip()), children)


def unicode_expression(node):
    """Provide a linear view with explicit grouping when a formula has no native text."""
    if node is None:
        return None
    kind = etree.QName(node).localname
    values = [unicode_expression(child) for child in node]
    if kind == "mfrac" and len(values) == 2:
        return f"({values[0]})∕({values[1]})"
    if kind in {"msup", "msub"} and len(values) == 2:
        operator = "^" if kind == "msup" else "_"
        return f"{values[0]}{operator}({values[1]})"
    if kind == "msubsup" and len(values) == 3:
        return f"{values[0]}_({values[1]})^({values[2]})"
    if kind == "msqrt":
        return "√(" + "".join(values) + ")"
    if kind == "mroot" and len(values) == 2:
        return f"√[{values[1]}]({values[0]})"
    if kind == "mtr":
        return " | ".join(values)
    if kind == "mtable":
        return "[" + " / ".join(values) + "]"
    return "".join(values) or (node.text or "")


def source_equations(ir):
    """Use stable IDs from the PDF IR and keep unverified OCR as evidence only."""
    for page in ir.pages:
        for index, block in enumerate(page.blocks, 1):
            if block.kind != "equation":
                continue
            proof = block.source_reference.get("formula", {})
            label = block.marker or equation_number(block.text)
            text = block.text.strip()
            if label:
                text = NUMBER.sub("", text).rstrip()
            status = proof.get("status", "review_required")
            encoding = proof.get("encoding", "latex" if text.startswith("\\") else "unicode")
            latex = TAG.sub("", text).strip() if encoding == "latex" else native_latex(text, block.spans)
            root = mathml(latex)
            if status == "native_text" and (root is None or page.requires_review):
                status = "review_required"
            identifier = block.source_reference.get("block_id") or f"p{page.n}-b{index}"
            row = {"id": identifier, "kind": "display", "page": page.n, "bbox": block.bbox,
                   "number": label or None, "status": status,
                   "encoding": encoding,
                   "unicode": text if encoding == "unicode" else unicode_expression(root),
                   "latex": latex if root is not None else None,
                   "mathml": etree.tostring(root, encoding="unicode") if root is not None else None,
                   "extraction": block.source, "ocr_engine": block.ocr_engine or None,
                   "recognition_confidence": block.recognition_confidence,
                   "verification": proof.get("verification"),
                   "execution_ids": proof.get("execution_ids", block.source_reference.get("execution_ids", []))}
            yield row, root

        for index, block in enumerate(page.blocks, 1):
            if block.kind == "equation":
                continue
            indexed = enumerate(block.spans, 1)
            for marked, group in groupby(indexed, lambda item: item[1].formula):
                if not marked:
                    continue
                items = list(group)
                cursor = items[0][0]
                spans = [span for _, span in items]
                label = block.source_reference.get("block_id") or f"p{page.n}-b{index}"
                text = "".join(span.text for span in spans)
                latex = text if block.source != "native" else native_latex(text, spans)
                root = mathml(latex)
                yield {"id": f"{label}-inline-{cursor}", "kind": "inline", "page": page.n, "bbox": block.bbox,
                       "bbox_scope": "containing_block", "number": None, "status": "review_required",
                       "unicode": text if block.source == "native" else unicode_expression(root),
                       "latex": latex if root is not None else None,
                       "mathml": etree.tostring(root, encoding="unicode") if root is not None else None,
                       "extraction": block.source, "recognition_confidence": block.recognition_confidence}, root


def equation_catalog(ir, xml=None, locations=None):
    rows = [row for row, _ in source_equations(ir)]
    if xml is None or not locations:
        return {"version": 1, "equations": rows}
    root = etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    identifiers = {}
    for page in ir.pages:
        for index, block in enumerate(page.blocks, 1):
            if block.kind == "equation":
                identifier = block.source_reference.get("block_id") or f"p{page.n}-b{index}"
                identifiers[identifier] = locations.get(id(block))
    for item in rows:
        path = identifiers.get(item["id"])
        if path:
            nodes = root.xpath(path)
            if len(nodes) == 1:
                images = nodes[0].xpath("self::img[@src]|./img[@src]")
                item.update(xml_node=path, xml_id=nodes[0].get("id"),
                            image=images[0].get("src") if len(images) == 1 else None)
    return {"version": 1, "equations": rows}

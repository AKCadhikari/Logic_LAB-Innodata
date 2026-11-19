"""Compare generated XML and PowerPoint structure to local reference files."""
from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from zipfile import ZipFile

from lxml import etree
from pptx import Presentation


def tree(data: bytes) -> etree._Element:
    root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    if root.getroottree().docinfo.doctype:
        raise ValueError("Document type declarations are unsupported.")
    return root


def describe(node: etree._Element) -> dict:
    return {"tag": str(node.tag) if isinstance(node.tag, str) else "comment",
            "attributes": dict(node.attrib), "text": "".join(node.itertext()) if isinstance(node.tag, str) else node.text}


def content(node: etree._Element, value: str | None) -> str:
    if value and not value.strip() and "\n" in value and node.get("{http://www.w3.org/XML/1998/namespace}space") != "preserve":
        return ""
    return value or ""


def identity(node: etree._Element):
    shape = node.find("./*/{http://schemas.openxmlformats.org/presentationml/2006/main}cNvPr")
    return node.tag, shape.get("id") if shape is not None else node.get("id") or node.get("Id")


def compare_nodes(expected: etree._Element, actual: etree._Element) -> dict:
    differences = []
    expected_tree, actual_tree = expected.getroottree(), actual.getroottree()

    def add(kind, wanted, got, left, right):
        differences.append({"type": kind, "expected_path": expected_tree.getpath(left) if left is not None else None,
                            "actual_path": actual_tree.getpath(right) if right is not None else None,
                            "expected": wanted, "actual": got})

    def compare(left, right):
        if left.tag != right.tag:
            add("element_name", describe(left), describe(right), left, right)
        for kind, wanted, got in (
            ("attributes", dict(left.attrib), dict(right.attrib)),
            ("namespaces", {k or "": v for k, v in left.nsmap.items()}, {k or "": v for k, v in right.nsmap.items()}),
            ("text", content(left, left.text), content(right, right.text)),
        ):
            if wanted != got:
                add(kind, wanted, got, left, right)
        matcher = SequenceMatcher(None, [identity(node) for node in left], [identity(node) for node in right], autojunk=False)
        for opcode, i, j, k, l in matcher.get_opcodes():
            if opcode == "replace" and j - i == l - k:
                opcode = "equal"
            if opcode == "equal":
                for a, b in zip(left[i:j], right[k:l]):
                    compare(a, b)
                    wanted, got = content(left, a.tail), content(right, b.tail)
                    if wanted != got:
                        add("tail", wanted, got, a, b)
            else:
                for node in left[i:j]:
                    add("missing_element", describe(node), None, node, None)
                for node in right[k:l]:
                    add("additional_element", None, describe(node), None, node)

    compare(expected, actual)
    counts = Counter(item["type"] for item in differences)
    return {"match": not differences, "diff_counts": dict(counts), "differences": differences}


def compare_xml(expected: Path, actual: Path) -> dict:
    return compare_nodes(tree(expected.read_bytes()), tree(actual.read_bytes()))


def slide_parts(path: Path) -> list[tuple[int, str, str]]:
    return [(slide.slide_id, str(slide.part.partname).lstrip("/"),
             slide.shapes.title.text if slide.shapes.title is not None else "")
            for slide in Presentation(path).slides]


def compare_pptx(expected: Path, actual: Path) -> dict:
    before, after = slide_parts(expected), slide_parts(actual)
    report = {"expected_slides": len(before), "actual_slides": len(after),
              "slide_order_match": [s[0] for s in before] == [s[0] for s in after], "slides": [],
              "missing_slides": [dict(id=s[0], part=s[1], title=s[2]) for s in before[len(after):]],
              "additional_slides": [dict(id=s[0], part=s[1], title=s[2]) for s in after[len(before):]]}
    with ZipFile(expected) as left, ZipFile(actual) as right:
        for number, (a, b) in enumerate(zip(before, after), 1):
            left_data, right_data = left.read(a[1]), right.read(b[1])
            result = ({"match": True, "diff_counts": {}, "differences": []} if left_data == right_data
                      else compare_nodes(tree(left_data), tree(right_data)))
            report["slides"].append({"number": number, "expected_title": a[2], "actual_title": b[2],
                                     "expected_slide_id": a[0], "actual_slide_id": b[0], **result})
        lnames = set(left.namelist()) - {slide[1] for slide in before}
        rnames = set(right.namelist()) - {slide[1] for slide in after}
        other = []
        for name in sorted(lnames & rnames):
            a, b = left.read(name), right.read(name)
            if a == b:
                continue
            if name.endswith((".xml", ".rels")):
                result = compare_nodes(tree(a), tree(b))
                if not result["match"]:
                    other.append({"part": name, **result})
            else:
                other.append({"part": name, "type": "changed_binary_part",
                              "expected_bytes": len(a), "actual_bytes": len(b)})
        report["other_parts"] = {"missing": sorted(lnames - rnames), "additional": sorted(rnames - lnames),
                                 "changed": other}
    report["changed_slides"] = [slide["number"] for slide in report["slides"] if not slide["match"]]
    report["match"] = report["slide_order_match"] and not report["changed_slides"] and not any(report["other_parts"].values())
    return report

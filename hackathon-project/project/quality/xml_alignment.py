"""Page-aware evaluation of engineering XML against an optional reference."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
import re
import unicodedata

from rapidfuzz.distance import Levenshtein
from lxml import etree

from config.settings import Settings
from quality.conformance import compare_nodes, tree
from xml_engine.validate import validate_xml


HEADINGS = {f"h{number}" for number in range(1, 7)}
TEXT_TAGS = HEADINGS | {"p", "li", "reference", "th", "td", "figcaption", "caption", "header", "footer",
                        "page_header", "page_footer", "aside", "pre", "blockquote"}


@dataclass
class Unit:
    path: str
    text: str
    role: str
    tag: str
    level: int | None
    parent: str
    figure_box: tuple | None
    section_levels: tuple


def clean(value):
    return " ".join(unicodedata.normalize("NFC", value).split())


def positive_number(value, fallback):
    return int(value) if value and re.fullmatch(r"[0-9]{1,9}", value) and int(value) > 0 else fallback


def inventory(root):
    result, numbers, counts = defaultdict(list), defaultdict(list), Counter()
    codes = {"header": "publication_metadata", "footer": "publication_metadata", "page_header": "publication_metadata",
             "page_footer": "publication_metadata", "reference": "reference_entry", "figcaption": "figure_caption"}
    xml = root.getroottree()
    page = 0
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        counts[node.tag] += 1
        if node.tag == "page":
            page = positive_number(node.get("n") or node.get("num"), page)
        position = positive_number(node.get("page"), page or 1)
        role = node.get("kind") or codes.get(node.tag, node.tag)
        if node.get("type") == "formula" or (role == "equation" and node.tag == "p"):
            label = node.get("equation_number")
            if not label and node.tag == "p":
                match = re.search(r"(?:\\tag\{([0-9.]+)\}|\(([0-9.]+)\))\s*$", "".join(node.itertext()))
                label = (match[1] or match[2]) if match else None
            numbers[position].append({"path": xml.getpath(node), "label": label, "encoding": node.get("formula_encoding"),
                                      "formula": node.get("alt") if node.tag == "img" else "".join(node.itertext())})
            continue
        if node.tag not in TEXT_TAGS or (node.tag == "header" and node.getparent() is root):
            continue
        value = clean("".join(node.itertext()))
        if not value:
            continue
        parent = node.getparent()
        image = parent.find("img") if node.tag == "figcaption" else None
        box = tuple(image.get(edge) for edge in ("x0", "y0", "x1", "y1")) if image is not None else ()
        try:
            figure_box = tuple(map(float, box)) if box and all(box) else None
        except ValueError:
            figure_box = None
        result[position].append(Unit(xml.getpath(node), value, role, node.tag,
                                     int(node.tag[1]) if node.tag in HEADINGS else None,
                                     parent.tag, figure_box,
                                     tuple(parent.get("level") for parent in reversed(list(node.iterancestors("section"))))))
    return result, numbers, counts


def align(expected, generated):
    """Match each unit at most once, with adjacent groups for changed segmentation."""
    candidates = []
    for i in range(len(expected)):
        for j in range(len(generated)):
            width = 32 if generated[j].role == "pseudocode" else 4
            groups = [(1, n) for n in range(1, 5)] + [(m, 1) for m in range(2, width + 1)]
            algorithm = (generated[j].role == "algorithm_caption" and j + 1 < len(generated)
                         and generated[j + 1].role == "pseudocode")
            if algorithm:
                groups += [(m, 2) for m in range(2, 33)]
            for m, n in groups:
                if i + m > len(expected) or j + n > len(generated):
                    continue
                a = " ".join(unit.text for unit in expected[i:i + m])
                b = " ".join(unit.text for unit in generated[j:j + n])
                if min(len(a), len(b)) < .72 * max(len(a), len(b)):
                    continue
                score = Levenshtein.normalized_similarity(a, b)
                threshold = .90 if min(len(a), len(b)) < 15 or m + n > 2 else .82
                if score >= threshold:
                    penalty = .003 if width == 32 or algorithm and n == 2 else .015
                    candidates.append((score - penalty * (m + n - 2), i, j, m, n, score))
    used_e, used_a, matches = set(), set(), []
    for _, i, j, m, n, score in sorted(candidates, key=lambda item: (-item[0], item[1], item[2])):
        left, right = set(range(i, i + m)), set(range(j, j + n))
        if left & used_e or right & used_a:
            continue
        used_e.update(left)
        used_a.update(right)
        matches.append((i, j, m, n, score))
    return sorted(matches), used_e, used_a


def rate(value, total):
    return round(value / total, 4) if total else None


def prf(true, predicted, expected):
    return {"precision": rate(true, predicted), "recall": rate(true, expected),
            "f1": rate(2 * true, predicted + expected)}


def same_region(a, b):
    if not (a and b):
        return None
    width = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - width * height
    return bool(union and width * height / union >= .5)


def xml_profile(root):
    if root.find("body") is not None:
        return "sample"
    return "example" if root.find("page[@num]") is not None else "brief"


def evaluate_xml(expected_data, actual_data, *, labels=None):
    expected = tree(expected_data)
    try:
        actual = tree(actual_data)
    except (etree.XMLSyntaxError, ValueError):
        return {"validation": validate_xml(actual_data, len(list(expected.iter("page"))),
                                          Settings(schema_profile="sample")),
                "metric_scope": "Content and structural metrics need parseable XML without a document type declaration."}
    reference, expected_equations, expected_tags = inventory(expected)
    generated, generated_equations, actual_tags = inventory(actual)
    detail, tally, label_results = [], Counter(), []
    if labels is not None:
        expected_equations = defaultdict(list)
        for label in labels:
            expected_equations[label["page"]].append({"label": label["equation"], "formula": label.get("latex")})
    numbers = lambda root: [node.get("num") or node.get("n") for node in root.iter("page")]
    reference_pages, actual_pages = numbers(expected), numbers(actual)
    profile = xml_profile(actual)
    reference_profile = xml_profile(expected)
    validation = validate_xml(actual_data, len(reference_pages), Settings(schema_profile=profile))
    compatibility = validation if profile == reference_profile else validate_xml(
        actual_data, len(reference_pages), Settings(schema_profile=reference_profile))
    for page in sorted(set(reference) | set(generated) | set(expected_equations) | set(generated_equations)):
        a, b = reference[page], generated[page]
        left, right = (" ".join(unit.text for unit in items) for items in (a, b))
        tally.update(char_errors=Levenshtein.distance(left, right), chars=len(left),
                     word_errors=Levenshtein.distance(left.split(), right.split()), words=len(left.split()),
                     reference=len(a), generated=len(b),
                     references_expected=sum(unit.role == "reference_entry" for unit in a),
                     references_generated=sum(unit.role == "reference_entry" for unit in b),
                     headings_expected=sum(unit.level is not None for unit in a),
                     captions_expected=sum(unit.tag == "figcaption" for unit in a))
        matches, aligned_a, aligned_b = align(a, b)
        tally.update(aligned_expected=len(aligned_a), aligned_actual=len(aligned_b))
        for first, second in combinations(matches, 2):
            tally["order_pairs"] += 1
            tally["order_preserved"] += first[1] < second[1]
        rows = []
        for i, j, m, n, score in matches:
            left_units, right_units = a[i:i + m], b[j:j + n]
            roles_a, roles_b = {x.role for x in left_units}, {x.role for x in right_units}
            if len(roles_a) == 1 and roles_a == roles_b:
                tally["semantic_correct"] += min(m, n)
                tally["references_correct"] += min(m, n) if "reference_entry" in roles_a else 0
            if m == n == 1:
                u, v = left_units[0], right_units[0]
                tally["headings_correct"] += u.level is not None and u.level == v.level
                tally["hierarchy_correct"] += u.level is not None and u.level == v.level and u.section_levels == v.section_levels
                if u.tag == v.tag == "figcaption":
                    region = same_region(u.figure_box, v.figure_box)
                    if region is not None:
                        tally["caption_geometry_compared"] += 1
                        tally["caption_geometry_correct"] += region
            rows.append({"expected": [x.path for x in left_units], "generated": [x.path for x in right_units],
                         "expected_roles": sorted(roles_a), "generated_roles": sorted(roles_b),
                         "text_similarity": round(score, 4)})
        expected_labels = Counter(row["label"] for row in expected_equations[page] if row["label"])
        actual_labels = Counter(row["label"] for row in generated_equations[page] if row["label"])
        tally.update(number_expected=sum(expected_labels.values()),
                     number_preserved=sum((expected_labels & actual_labels).values()),
                     displays_expected=len(expected_equations[page]), displays_generated=len(generated_equations[page]))
        for index, expected_equation in enumerate(expected_equations[page]):
            if not expected_equation["label"] or index >= len(generated_equations[page]):
                continue
            found = generated_equations[page][index]
            tally["number_at_expected_position"] += expected_equation["label"] == found["label"]
        for wanted in expected_equations[page]:
            if wanted["label"] and wanted.get("formula"):
                tally["reviewed_latex_expected"] += 1
                matches = [item for item in generated_equations[page]
                           if item["label"] == wanted["label"] and item["encoding"] == "latex" and item.get("formula")]
                if len(matches) == 1:
                    parse = lambda formula: re.findall(r"\\[A-Za-z]+|\\.|[^\s]", formula)
                    tally["reviewed_latex_compared"] += 1
                    tally["reviewed_latex_exact_tokens"] += parse(wanted["formula"]) == parse(matches[0]["formula"])
        if expected_labels or actual_labels:
            label_results.append({"page": page, "expected": dict(expected_labels), "generated": dict(actual_labels)})
        detail.append({"page": page, "matched": rows,
                       "unmatched_reference": [a[i].__dict__ for i in range(len(a)) if i not in aligned_a],
                       "unmatched_generated": [b[i].__dict__ for i in range(len(b)) if i not in aligned_b]})
    return {
        "validation": validation, "selected_profile": profile, "reference_profile": reference_profile,
        "reference_schema_validation": compatibility["xsd"],
        "page_count_accuracy": rate(sum(a == b for a, b in zip(reference_pages, actual_pages)),
                                    max(len(reference_pages), len(actual_pages))),
        "reference_pages": reference_pages, "generated_pages": actual_pages,
        "character_error_rate": rate(tally["char_errors"], tally["chars"]),
        "word_error_rate": rate(tally["word_errors"], tally["words"]),
        "text_similarity": rate(tally["chars"] - tally["char_errors"], tally["chars"]),
        "content_recall": rate(tally["aligned_expected"], tally["reference"]),
        "content_precision": rate(tally["aligned_actual"], tally["generated"]),
        "semantic": prf(tally["semantic_correct"], tally["generated"], tally["reference"]),
        "references": prf(tally["references_correct"], tally["references_generated"], tally["references_expected"]),
        "matched_pair_reading_order": rate(tally["order_preserved"], tally["order_pairs"]),
        "heading_level_accuracy": rate(tally["headings_correct"], tally["headings_expected"]),
        "heading_hierarchy_accuracy": rate(tally["hierarchy_correct"], tally["headings_expected"]),
        "matched_caption_region_accuracy": rate(tally["caption_geometry_correct"], tally["caption_geometry_compared"]),
        "equation_label_recall": rate(tally["number_preserved"], tally["number_expected"]),
        "equation_labels_in_order_recall": rate(tally["number_at_expected_position"], tally["number_expected"]),
        "equation_label_source": "visually reviewed PDF labels" if labels is not None else "explicit reference XML labels",
        "equation_labels": label_results, "counts": dict(tally),
        "expected_tags": dict(expected_tags), "actual_tags": dict(actual_tags),
        "structural_diff_counts": compare_nodes(expected, actual)["diff_counts"], "pages": detail,
        "metric_scope": ("Text uses NFC and whitespace normalization with case and punctuation retained. "
                         "Formula text is evaluated separately. Reading order counts pairs among aligned units. "
                         "Caption agreement uses matched captions and source-box intersection over union."),
    }

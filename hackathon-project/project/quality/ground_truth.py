"""Compare local PDF evidence, engineering artifacts and exported files."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import posixpath
import unicodedata
from zipfile import ZipFile

from lxml import etree
import pymupdf
from pptx import Presentation
from pptx.oxml.ns import qn

from config.settings import Settings
from diagnostics.page_diagnostics import analyze_page
from layout.verified import evidence_for
from powerpoint.numbering import numbering
from powerpoint.package import MC, REL
from quality.conformance import compare_pptx, compare_xml, tree
from quality.evaluate import compare as score_xml
from xml_engine.assets import inspect_assets, validate_archive
from xml_engine.validate import validate_xml


def text(value: str) -> str:
    return " ".join(value.split())


def xml_units(root):
    return [(root.getroottree().getpath(node), node.get("page"), text("".join(node.itertext())))
            for node in root.xpath("//p|//li|//th|//td|//reference|//h1|//h2|//h3|//h4|//h5|//h6")
            if text("".join(node.itertext()))]


def reference_differences(expected, actual):
    left, right = Counter(row[2] for row in expected), Counter(row[2] for row in actual)
    def select(rows, outstanding, status):
        result = []
        for xpath, page, content in rows:
            if outstanding[content]:
                outstanding[content] -= 1
                result.append({"status": status, "basis": "engineering XML", "page": page,
                               "xml_node": xpath, "content": content})
        return result
    return {"exact_units": sum((left & right).values()),
            "missing": select(expected, left - right, "MISSING"),
            "extra": select(actual, right - left, "EXTRA")}


def annotate_reference_differences(result: dict) -> dict:
    for difference in result.get("differences", []):
        difference["status"] = {"missing_element": "MISSING", "additional_element": "EXTRA"}.get(
            difference["type"], "UNCERTAIN")
        difference["basis"] = "Engineering artifact"
    return result


def preservation(original: Path, actual: Path, approved: list[dict] = ()) -> dict:
    before, after = Presentation(original), Presentation(actual)
    authorized = {(item.get("slide"), item.get("shape_id")) for item in approved}
    decks = []
    for index, (a, b) in enumerate(zip(before.slides, after.slides), 1):
        parts = {shape.shape_id: shape for shape in b.shapes}
        old_ids = [shape.shape_id for shape in a.shapes]
        new_ids = [shape.shape_id for shape in b.shapes]
        changes = []
        for shape in a.shapes:
            replacement = parts.get(shape.shape_id)
            if replacement is None:
                changes.append({"shape_id": shape.shape_id, "status": "MISSING"})
                continue
            old, new = tree(etree.tostring(shape._element)), tree(etree.tostring(replacement._element))
            before_text, after_text = [node.text for node in old.iter(qn("a:t"))], [node.text for node in new.iter(qn("a:t"))]
            for element in (old, new):
                for node in element.iter(qn("a:t")):
                    node.text = ""
            structure = etree.tostring(old, method="c14n", exclusive=True) == etree.tostring(new, method="c14n", exclusive=True)
            status = "CORRECT" if structure and (before_text == after_text or (index, shape.shape_id) in authorized) else "INCORRECT"
            changes.append({"shape_id": shape.shape_id, "status": status, "layout_format_and_content_structure_preserved": structure,
                            "text_changed": before_text != after_text})
        for shape in b.shapes:
            if shape.shape_id not in old_ids:
                changes.append({"shape_id": shape.shape_id, "status": "CORRECT" if (index, shape.shape_id) in authorized else "EXTRA"})
        decks.append({"slide": index, "slide_id_preserved": a.slide_id == b.slide_id,
                      "layout_preserved": a.slide_layout.part.partname == b.slide_layout.part.partname,
                      "original_shape_order_preserved": old_ids == [shape_id for shape_id in new_ids if shape_id in old_ids],
                      "original_shapes": len(old_ids), "output_shapes": len(new_ids), "shapes": changes})
    with ZipFile(original) as old, ZipFile(actual) as new:
        scope = ("ppt/slideLayouts/", "ppt/slideMasters/", "ppt/theme/", "ppt/notesSlides/", "ppt/media/")
        package = [name for name in old.namelist() if name.startswith(scope)
                   and (name not in new.namelist() or old.read(name) != new.read(name))]
        for index, (a, b) in enumerate(zip(before.slides, after.slides), 1):
            prior = tree(old.read(str(a.part.partname).lstrip("/")))
            restored = tree(new.read(str(b.part.partname).lstrip("/")))
            choices = {shape_id(node): node for node in restored.iter(f"{{{MC}}}Choice")}
            entries = []
            for original_choice in prior.iter(f"{{{MC}}}Choice"):
                identifier = shape_id(original_choice)
                if identifier is None:
                    continue
                current = choices.get(identifier)
                if current is not None:
                    same = canonical(original_choice) == canonical(current)
                    structure = masked(original_choice) == masked(current)
                    status = "CORRECT" if same or (structure and (index, identifier) in authorized) else "INCORRECT"
                    if structure:
                        for entry in decks[index - 1]["shapes"]:
                            if entry["shape_id"] == identifier and entry["status"] == "EXTRA":
                                entry["status"] = "CORRECT"
                else:
                    same = False
                    structure = False
                    status = "CORRECT" if (index, identifier) in authorized else "MISSING"
                entries.append({"shape_id": identifier, "editable_choice_preserved": structure,
                                "choice_changed": not same, "status": status})
            decks[index - 1]["alternate_content"] = entries
            decks[index - 1]["effective_original_shapes"] = decks[index - 1]["original_shapes"] + len(entries)
            decks[index - 1]["effective_output_shapes"] = decks[index - 1]["output_shapes"] + len(choices)
    return {"original_slide_count": len(before.slides), "output_slide_count": len(after.slides),
            "original_pictures": sum(shape.shape_type == 13 for slide in before.slides for shape in slide.shapes),
            "output_pictures": sum(shape.shape_type == 13 for slide in after.slides for shape in slide.shapes),
            "changed_original_master_layout_notes_theme_or_media_parts": package, "slides": decks}


def shape_id(node) -> int | None:
    prop = next(node.iter(qn("p:cNvPr")), None)
    return int(prop.get("id")) if prop is not None else None


def canonical(node) -> bytes:
    return etree.tostring(node, method="c14n", exclusive=True)


def masked(node) -> bytes:
    from copy import deepcopy

    copy = deepcopy(node)
    for text_node in copy.iter(qn("a:t")):
        text_node.text = ""
    return canonical(copy)


def picture_blob(deck, package: ZipFile, number: int, identifier: int | None) -> bytes | None:
    if identifier is None or number > len(deck.slides):
        return None
    part = str(deck.slides[number - 1].part.partname).lstrip("/")
    folder, leaf = posixpath.split(part)
    slide = tree(package.read(part))
    for pic in slide.iter(qn("p:pic")):
        if shape_id(pic) != identifier:
            continue
        blip = next(pic.iter(qn("a:blip")), None)
        if blip is None:
            continue
        rel_id = blip.get(qn("r:embed"))
        rels = tree(package.read(f"{folder}/_rels/{leaf}.rels"))
        for relationship in rels.iter(f"{{{REL}}}Relationship"):
            if relationship.get("Id") == rel_id and relationship.get("TargetMode") != "External":
                target = posixpath.normpath(posixpath.join(folder, relationship.get("Target")))
                return package.read(target)
    return None


def mathematical_text(node) -> str:
    if node.tag in {qn("a:t"), qn("m:t")}:
        return node.text or ""
    if node.tag == qn("m:d"):
        begins = node.find("./" + qn("m:dPr") + "/" + qn("m:begChr"))
        ends = node.find("./" + qn("m:dPr") + "/" + qn("m:endChr"))
        left = begins.get(qn("m:val"), "(") if begins is not None else "("
        right = ends.get(qn("m:val"), ")") if ends is not None else ")"
        return left + "".join(mathematical_text(child) for child in node if child.tag == qn("m:e")) + right
    return "".join(mathematical_text(child) for child in node)


def editable_lines(package: ZipFile, deck, number: int, identifier: int) -> list[dict]:
    if len(deck.slides) < number:
        return []
    root = tree(package.read(str(deck.slides[number - 1].part.partname).lstrip("/")))
    shape = next((node for node in root.iter(qn("p:sp")) if shape_id(node) == identifier
                  and not any(parent.tag == f"{{{MC}}}Fallback" for parent in node.iterancestors())), None)
    if shape is None:
        return []
    lines = []
    for paragraph in shape.iter(qn("a:p")):
        properties = paragraph.find(qn("a:pPr"))
        lines.append({"slide": number, "shape_id": identifier, "content": mathematical_text(paragraph),
                      "left_margin_emu": properties.get("marL") if properties is not None else None})
    return lines


def code_tokens(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).replace("∗", "*").split())


def pdf_regions(pdf, root, folder: Path) -> list[dict]:
    results = []
    for node in root.xpath("//img[@src]"):
        page = node.get("page")
        coordinates = [node.get(key) for key in ("x0", "y0", "x1", "y1")]
        path = node.get("src")
        item = {"kind": "equation" if node.get("type") == "formula" else "figure", "page": page,
                "bbox": coordinates, "src": path, "xml_node": root.getroottree().getpath(node),
                "status": "MANUAL_REVIEW_REQUIRED"}
        if page and all(value is not None for value in coordinates):
            try:
                box = pymupdf.Rect(*(float(value) for value in coordinates))
                # Sample geometry is rounded to hundredths in XML. A one-pixel edge can move.
                sample = pdf[int(page) - 1].get_pixmap(dpi=180, clip=box, alpha=False)
                saved = pymupdf.Pixmap(str(folder / path))
                if (sample.width, sample.height, sample.samples) == (saved.width, saved.height, saved.samples):
                    item["status"] = "CORRECT"
            except (OSError, RuntimeError, ValueError, IndexError):
                pass
        results.append(item)
    return results


def verified_sample(pdf, source_path: Path, original: Path, reference_path: Path, actual_path: Path,
                    xml, approved: list[dict]) -> dict:
    data = evidence_for(source_path, original)
    if not data:
        return {"status": "MANUAL_REVIEW_REQUIRED", "reason": "No visually verified region map is available for this PDF and original deck."}
    reference_deck, actual_deck = Presentation(reference_path), Presentation(actual_path)
    algorithm = data["algorithm"]
    expected_lines = algorithm["lines"]
    code = xml.find(".//p[@kind='pseudocode']")
    lines = (code.text or "").splitlines() if code is not None else []
    if code is not None and (caption := code.getprevious()) is not None and caption.get("kind") == "algorithm_caption":
        lines.insert(0, "".join(caption.itertext()))
    expected_tokens = [code_tokens(value) for value in expected_lines]
    output_tokens = [code_tokens(value) for value in lines]
    expected_count, output_count = Counter(expected_tokens), Counter(output_tokens)
    pseudocode = {"page": algorithm["page"], "bbox": algorithm["box"], "lines": [
        {"line": index, "source": value, "output": lines[index - 1] if index <= len(lines) else None,
         "status": ("MISSING" if index > len(lines) else
                    "CORRECT" if expected_tokens[index - 1] == output_tokens[index - 1] else "INCORRECT")}
        for index, value in enumerate(expected_lines, 1)],
        "extra": [{"content": lines[output_tokens.index(value)], "count": count, "status": "EXTRA"}
                  for value, count in (output_count - expected_count).items()],
        "ordered_match": expected_tokens == output_tokens, "comparison_ignores_indentation": True}
    crops = []
    with ZipFile(actual_path) as actual_package:
        editable = [*editable_lines(actual_package, actual_deck, 26, 4), *editable_lines(actual_package, actual_deck, 27, 4)]
        pseudocode["powerpoint_lines"] = [
            {"line": index, "source": line, "powerpoint": editable[index - 1] if index <= len(editable) else None,
             "status": ("MISSING" if index > len(editable) else
                        "CORRECT" if code_tokens(line) == code_tokens(editable[index - 1]["content"]) else "INCORRECT")}
            for index, line in enumerate(expected_lines, 1)
        ]
        pseudocode["extra_powerpoint_lines"] = [
            {**row, "status": "EXTRA"} for row in editable[len(expected_lines):]
        ]
        for number, expected_shape, page, box, _, description in data["slide_crops"]:
            reference = find_shape(reference_deck, number, expected_shape)
            accepted = next((row for row in approved if row.get("slide") == number and row.get("source_pdf_reference")
                             and row["source_pdf_reference"].get("bbox") == box), None)
            candidate_id = accepted.get("shape_id") if accepted else None
            candidate = picture_blob(actual_deck, actual_package, number, candidate_id)
            source = pdf[page - 1].get_pixmap(dpi=220, clip=pymupdf.Rect(box), alpha=False).tobytes("png")
            crops.append({"slide": number, "shape_id": candidate_id, "reference_shape_id": expected_shape,
                          "reference_content": reference.text if reference and reference.has_text_frame else None,
                          "page": page, "bbox": box, "content": description,
                          "editable_choice_retained": number in data["editable_slides"],
                          "status": "CORRECT" if candidate == source else "MISSING" if candidate is None else "INCORRECT"})
    facts = []
    all_xml = text(" ".join(xml.itertext()))
    for number, shape_id, index, page, box, _, value in data["slide_text"]:
        paragraph = paragraph_text(find_shape(actual_deck, number, shape_id), index)
        reference_value = paragraph_text(find_shape(reference_deck, number, shape_id), index)
        facts.append({"page": page, "bbox": box, "slide": number, "shape_id": shape_id, "source_content": value,
                      "status": "CORRECT" if value in paragraph else "INCORRECT",
                      "engineering_pptx_status": "CORRECT" if value in reference_value else "INCORRECT",
                      "xml_status": "CORRECT" if value in all_xml else "MANUAL_REVIEW_REQUIRED"})
    return {"pseudocode": pseudocode, "slide_content": crops, "author_abstract_keywords_and_parameters": facts,
            "equation_conflicts": [{**item, "engineering_status": "INCORRECT", "resolution": "Verified PDF crop"}
                                   for item in data["equation_conflicts"]]}


def find_shape(deck, number: int, identifier: int):
    if number <= len(deck.slides):
        return next((shape for shape in deck.slides[number - 1].shapes if shape.shape_id == identifier), None)
    return None


def paragraph_text(shape, index: int) -> str:
    if shape is not None and shape.has_text_frame and len(shape.text_frame.paragraphs) >= index:
        return shape.text_frame.paragraphs[index - 1].text
    return ""


def compare_ground_truth(pdf_path: Path, original: Path, expected_xml: Path, expected_deck: Path,
                         actual_xml: Path, actual_deck: Path, *, corrections: list[dict] = (),
                         archive: Path | None = None, xsd: Path | None = None, profile: str = "sample") -> dict:
    expected, actual = tree(expected_xml.read_bytes()), tree(actual_xml.read_bytes())
    settings = Settings(schema_profile=profile)
    with pymupdf.open(pdf_path) as pdf:
        diagnostics = [analyze_page(page, settings) for page in pdf]
        logged = bool(diagnostics and all(row["decision"] == "native" for row in diagnostics))
        reliable_text = [(page.number + 1, text(page.get_text())) for page, row in zip(pdf, diagnostics)
                         if row["decision"] == "native"]
        actual_units = xml_units(actual)
        source_units = [{"xml_node": path, "page": page, "content": value,
                         "status": "CORRECT" if any(value in body for number, body in reliable_text
                                                    if not page or page == str(number)) else "MANUAL_REVIEW_REQUIRED"}
                        for path, page, value in actual_units]
        assets = inspect_assets(actual_xml.read_bytes(), actual_xml.parent)
        crops = pdf_regions(pdf, actual, actual_xml.parent) if assets["valid"] else []
        facts = verified_sample(pdf, pdf_path, original, expected_deck, actual_deck, actual, corrections)
        xml_validation = validate_xml(actual_xml.read_bytes(), len(pdf), settings)
        accuracy = {"exact_native_matches": sum(row["status"] == "CORRECT" for row in source_units),
                    "all_pages_have_reliable_native_text": logged, "units": source_units,
                    "engineering_text_metrics": score_xml(actual_xml, expected_xml)}
    if xsd:
        schema = etree.XMLSchema(etree.parse(xsd, parser=etree.XMLParser(resolve_entities=False, no_network=True)))
        xml_validation["xsd"] = {"valid": schema.validate(actual), "issues": [str(item) for item in schema.error_log]}
    else:
        xml_validation["schema_basis"] = "Local structural rules derived from the engineering sample."
    approved = [row for row in corrections if row.get("status") == "CORRECT"]
    presentation = compare_pptx(expected_deck, actual_deck)
    for slide in presentation["slides"]:
        annotate_reference_differences(slide)
    for part in presentation["other_parts"]["changed"]:
        annotate_reference_differences(part)
    return {
        "text_accuracy": accuracy, "text_completeness": reference_differences(xml_units(expected), actual_units),
        "tables": {"actual": [etree.tostring(node, encoding="unicode") for node in actual.findall(".//table")],
                   "engineering_count": len(expected.findall(".//table")), "status": "MANUAL_REVIEW_REQUIRED"
                   if actual.findall(".//table") or expected.findall(".//table") else "CORRECT"},
        "pdf_pages": diagnostics, "xml_validation": xml_validation,
        "xml_structure": annotate_reference_differences(compare_xml(expected_xml, actual_xml)),
        "assets": assets, "engineering_asset_integrity": inspect_assets(expected_xml.read_bytes(), expected_xml.parent),
        "figures": [row for row in crops if row["kind"] == "figure"], "equations": [row for row in crops if row["kind"] == "equation"],
        "verified_source_comparison": facts, "engineering_presentation": presentation,
        "original_deck_preservation": preservation(original, actual_deck, approved),
        "slide_numbering": numbering(Presentation(actual_deck)),
        "zip": validate_archive(archive, actual_xml.name, actual_deck.name) if archive else None,
    }

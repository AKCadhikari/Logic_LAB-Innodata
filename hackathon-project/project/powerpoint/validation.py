"""Verify persisted edits and render the saved slides through a local renderer."""
from collections import defaultdict
from pathlib import Path
from subprocess import DEVNULL, TimeoutExpired, run
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from lxml import etree
import pymupdf

from powerpoint.package import open_deck, parse, set_patch_value
from utils.textutil import norm_cmp


def preservation(source, target, edits):
    """Restore approved leaves in memory and compare the remainder of the package."""
    with ZipFile(source) as before, ZipFile(target) as after:
        equal_parts = set(before.namelist()) == set(after.namelist())
        unexpected = []
        for part in sorted(set(before.namelist()) & set(after.namelist())):
            first, second = before.read(part), after.read(part)
            if first == second:
                continue
            if part not in edits:
                unexpected.append(part)
                continue
            root = parse(second)
            for patch in reversed(edits[part]):
                node = root.find(patch.path)
                set_patch_value(node, patch, patch.old)
            if etree.tostring(root, method="c14n") != etree.tostring(parse(first), method="c14n"):
                unexpected.append(part)
    return {"same_package_parts": equal_parts, "unexpected_changes": unexpected,
            "preserved": equal_parts and not unexpected}


def render_pdf(executable, deck, folder, profile):
    folder.mkdir()
    launch = run([executable, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--convert-to", "pdf",
                  "--outdir", str(folder), str(deck.resolve())],
                 stdin=DEVNULL, capture_output=True, timeout=180)
    path = folder / f"{deck.stem}.pdf"
    if launch.returncode or not path.is_file():
        raise ValueError("LibreOffice could not render the presentation.")
    return path


def rendered_check(source, target, records):
    from quality.slide_preview import renderer

    executable = renderer()
    if not executable:
        return {"status": "unavailable", "reason": "LibreOffice was not found."}
    try:
        with TemporaryDirectory(prefix="docusync-slides-") as temp:
            root = Path(temp).resolve()
            profile = root / "profile"
            after_path = render_pdf(executable, target, root / "after", profile)
            changed = defaultdict(list)
            for row in records:
                if row["action"] == "applied":
                    changed[row["slide"]].append(row)
            source_path = render_pdf(executable, source, root / "before", profile) if changed else after_path
            deck = open_deck(target)
            with pymupdf.open(after_path) as after, pymupdf.open(source_path) as before:
                mismatches = []
                if len(after) != len(deck.slides) or len(after) != len(before):
                    return {"status": "failed", "reason": "Rendered slide counts differ."}
                for number, edits in changed.items():
                    old_page, new_page = before[number - 1], after[number - 1]
                    raw_compare = None
                    for row in edits:
                        category = row["change_type"]
                        if category == "hyperlink" and not row.get("visible_url_edited"):
                            row["render_verification"] = "relationship_only"
                            continue
                        identity = row.get("shape_id")
                        shape = next((shape for shape in deck.slides[number - 1].shapes if shape.shape_id == identity), None)
                        area = None
                        if shape is not None and all(getattr(shape, name) is not None for name in ("left", "top", "width", "height")):
                            sx, sy = new_page.rect.width / deck.slide_width, new_page.rect.height / deck.slide_height
                            area = pymupdf.Rect(sx * shape.left - 2, sy * shape.top - 2,
                                                sx * (shape.left + shape.width) + 2,
                                                sy * (shape.top + shape.height) + 2)
                        old = norm_cmp(old_page.get_textbox(area) if area is not None else old_page.get_text())
                        new = norm_cmp(new_page.get_textbox(area) if area is not None else new_page.get_text())
                        wanted, previous = norm_cmp(row["new"]), norm_cmp(row["old"])
                        if category == "equation":
                            if raw_compare is None:
                                raw_compare = old_page.get_pixmap(dpi=72).samples == new_page.get_pixmap(dpi=72).samples
                            row["render_verification"] = ("no_visual_change_in_renderer" if raw_compare
                                                          else "requires_visual_equation_check")
                        elif wanted and wanted in new and (not previous or previous not in new or previous in wanted):
                            row["render_verification"] = "confirmed"
                        elif (area is not None and previous and previous in old and wanted and wanted not in new
                              and wanted in norm_cmp(new_page.get_text())):
                            row["render_verification"] = "text_outside_original_shape"
                            mismatches.append(row["error_id"])
                        elif previous and previous in old and previous in new and wanted not in new:
                            row["render_verification"] = "old_content_still_visible"
                            mismatches.append(row["error_id"])
                        else:
                            row["render_verification"] = "unconfirmed"
                return {"status": "completed", "renderer": "LibreOffice", "slides_rendered": len(after),
                        "visible_mismatches": mismatches}
    except (OSError, ValueError, RuntimeError, TimeoutExpired) as error:
        return {"status": "failed", "reason": str(error)}


def verify_presentation(source, target, edits, records, render):
    from powerpoint.formulas import office_tokens

    structure = preservation(source, target, edits)
    if not structure["preserved"]:
        raise ValueError("Saved presentation changed parts outside the approved edit plan.")
    deck = open_deck(target)
    with ZipFile(target) as package:
        for row in records:
            row["validation_status"] = "package_verified" if row["action"] == "applied" else "unresolved"
            if row["action"] == "applied":
                row.pop("render_verification", None)
            if row["action"] == "applied" and row["change_type"] == "equation":
                slide = deck.slides[row["slide"] - 1]
                tree = parse(package.read(str(slide.part.partname).lstrip("/")))
                node = tree.find(row["equation_path"])
                tape = office_tokens(node) if node is not None else None
                if not tape or tape.tokens != row["source"]["expected_tokens"]:
                    raise ValueError("The saved Office equation differs from the verified PDF structure.")
    visual = rendered_check(source, target, records) if render else {"status": "disabled"}
    for row in records:
        if row["action"] == "applied" and visual["status"] != "completed" and not row.get("render_verification"):
            row["render_verification"] = ("relationship_only" if row["change_type"] == "hyperlink"
                                           and not row.get("visible_url_edited") else "visual_check_pending")
        if row.get("render_verification") == "confirmed":
            row["validation_status"] = "visible_verified"
        elif row.get("render_verification") in {"old_content_still_visible", "text_outside_original_shape"}:
            row["validation_status"] = "visible_write_back_failure"
    return {"structure": structure, "render": visual, "run_edits_verified": sum(map(len, edits.values())),
            "visible_verified": sum(row["validation_status"] == "visible_verified" for row in records),
            "visible_write_back_failures": sum(row["validation_status"] == "visible_write_back_failure" for row in records)}

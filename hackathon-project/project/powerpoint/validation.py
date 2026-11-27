"""Verify persisted edits and render the saved slides through a local renderer."""
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from subprocess import DEVNULL, TimeoutExpired, run
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from lxml import etree
import pymupdf
import numpy as np
from PIL import Image, ImageOps

from powerpoint.package import MC, find_patch, open_deck, parse, set_patch_value
from utils.textutil import norm_cmp


def preservation(source, target, edits, media=None):
    """Restore approved leaves in memory and compare the remainder of the package."""
    with ZipFile(source) as before, ZipFile(target) as after:
        media = media or {}
        equal_parts = set(before.namelist()) == set(after.namelist())
        unexpected = []
        for part in sorted(set(before.namelist()) & set(after.namelist())):
            first, second = before.read(part), after.read(part)
            if first == second:
                continue
            if part in media and second == media[part]:
                continue
            if part not in edits:
                unexpected.append(part)
                continue
            root = parse(second)
            for patch in reversed(edits[part]):
                node = find_patch(root, patch)
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


def _rendered_image_matches(page, area, expected):
    if area is None or not page.rect.contains(area):
        return False
    pixmap = page.get_pixmap(dpi=168, clip=area, colorspace=pymupdf.csGRAY, alpha=False)
    with Image.open(BytesIO(expected)) as image:
        scale = (max(1, pixmap.width), max(1, pixmap.height))
        shown = Image.frombytes("L", scale, pixmap.samples)
        first, second = [np.asarray(ImageOps.grayscale(item).resize((256, 128), Image.Resampling.LANCZOS))
                         for item in (image, shown)]
    a, b = first < 160, second < 160
    union = int(np.count_nonzero(a | b))
    return union > 0 and np.count_nonzero(a & b) / union >= .55


def rendered_check(source, target, records, media=None):
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
                unchanged_differences = []
                for number in range(1, len(after) + 1):
                    old_page, new_page = before[number - 1], after[number - 1]
                    edits = changed.get(number, [])
                    if not edits:
                        if old_page.get_pixmap(dpi=72).samples != new_page.get_pixmap(dpi=72).samples:
                            unchanged_differences.append(number)
                        continue
                    for row in edits:
                        category = row["change_type"]
                        if category == "hyperlink" and not row.get("visible_url_edited"):
                            row["render_verification"] = "relationship_only"
                            continue
                        identity = row.get("shape_id")
                        shape = next((shape for shape in deck.slides[number - 1].shapes if shape.shape_id == identity), None)
                        area = None
                        exact = None
                        frame = row.get("picture_frame")
                        if frame is None and shape is not None:
                            frame = tuple(getattr(shape, name) for name in ("left", "top", "width", "height"))
                        if frame is not None and all(value is not None for value in frame):
                            left, top, width, height = frame
                            sx, sy = new_page.rect.width / deck.slide_width, new_page.rect.height / deck.slide_height
                            exact = pymupdf.Rect(sx * left, sy * top, sx * (left + width), sy * (top + height))
                            area = exact + (-2, -2, 2, 2)
                        old = norm_cmp(old_page.get_textbox(area) if area is not None else old_page.get_text())
                        new = norm_cmp(new_page.get_textbox(area) if area is not None else new_page.get_text())
                        wanted, previous = norm_cmp(row["new"]), norm_cmp(row["old"])
                        if category == "equation_image":
                            from powerpoint.formulas import normalized

                            expected = "".join(row.get("source", {}).get("expected_tokens", []))
                            if (row.get("native_equation_generated") and exact is not None and
                                    new_page.rect.contains(exact) and expected and
                                    expected in normalized(new_page.get_textbox(exact + (-1, -1, 1, 1)))):
                                row["render_verification"] = "confirmed"
                                row["editable_picture_validation"] = "native_render_verified"
                            elif _rendered_image_matches(new_page, exact, media[row["media_part"]]):
                                row["render_verification"] = "confirmed"
                                if row.get("native_equation_generated"):
                                    row["editable_picture_validation"] = "fallback_render_verified"
                            else:
                                row["render_verification"] = "equation_image_mismatch"
                                mismatches.append(row["error_id"])
                        elif category == "equation":
                            clip = area & new_page.rect if area is not None else new_page.rect
                            if clip.is_empty:
                                row["render_verification"] = "equation_outside_slide"
                                mismatches.append(row["error_id"])
                            elif old_page.get_pixmap(dpi=100, clip=clip).samples == new_page.get_pixmap(dpi=100, clip=clip).samples:
                                row["render_verification"] = "no_visual_change_in_renderer"
                            else:
                                row["render_verification"] = "requires_visual_equation_check"
                        elif (category == "missing_bullet" and any(
                                norm_cmp(bullet) in old and norm_cmp(bullet) not in new
                                for bullet in row["original_bullets"])):
                            row["render_verification"] = "original_bullet_clipped"
                            mismatches.append(row["error_id"])
                        elif wanted and wanted in new and (not previous or previous not in new or previous in wanted):
                            row["render_verification"] = "confirmed"
                        elif (area is not None and previous and previous in old and wanted and wanted not in new
                              and wanted in norm_cmp(new_page.get_text())):
                            row["render_verification"] = "text_outside_original_shape"
                            mismatches.append(row["error_id"])
                        elif previous and previous in old and previous in new and wanted not in new:
                            row["render_verification"] = "old_content_still_visible"
                            mismatches.append(row["error_id"])
                        elif category == "missing_bullet":
                            row["render_verification"] = "missing_bullet_not_visible"
                            mismatches.append(row["error_id"])
                        else:
                            row["render_verification"] = "unconfirmed"
                return {"status": "completed", "renderer": "LibreOffice", "slides_rendered": len(after),
                        "visible_mismatches": mismatches, "unchanged_slide_render_differences": unchanged_differences}
    except (OSError, ValueError, RuntimeError, TimeoutExpired) as error:
        return {"status": "failed", "reason": str(error)}


def verify_presentation(source, target, edits, records, render, *, media=None):
    from powerpoint.formulas import office_tokens
    from pptx.oxml.ns import qn

    structure = preservation(source, target, edits, media)
    if not structure["preserved"]:
        raise ValueError("Saved presentation changed parts outside the approved edit plan.")
    deck = open_deck(target)
    with ZipFile(target) as package:
        for row in records:
            row["validation_status"] = "package_verified" if row["action"] == "applied" else "unresolved"
            if row["action"] == "applied":
                row.pop("render_verification", None)
            if row["action"] == "applied" and row.get("native_equation_generated"):
                part = row["editable_picture_part"]
                patch = next(item for item in edits[part] if item.position == row["editable_picture_position"])
                wrapper = find_patch(parse(package.read(part)), patch)
                choice = wrapper.find(f"{{{MC}}}Choice")
                math = next(choice.iter(qn("m:oMath")), None) if choice is not None else None
                tape = office_tokens(math) if math is not None else None
                if not tape or tape.tokens != row["source"]["expected_tokens"]:
                    raise ValueError("The saved editable image alternative differs from the verified source equation.")
            if row["action"] == "applied" and row["change_type"] == "equation":
                slide = deck.slides[row["slide"] - 1]
                tree = parse(package.read(str(slide.part.partname).lstrip("/")))
                node = tree.find(row["equation_path"])
                tape = office_tokens(node) if node is not None else None
                if not tape or tape.tokens != row["source"]["expected_tokens"]:
                    raise ValueError("The saved Office equation differs from the verified PDF structure.")
    visual = rendered_check(source, target, records, media or {}) if render else {"status": "disabled"}
    for row in records:
        if row["action"] == "applied" and visual["status"] != "completed" and not row.get("render_verification"):
            row["render_verification"] = ("relationship_only" if row["change_type"] == "hyperlink"
                                           and not row.get("visible_url_edited") else "visual_check_pending")
        if row.get("render_verification") == "confirmed":
            row["validation_status"] = "visible_verified"
        elif row["error_id"] in visual.get("visible_mismatches", []):
            row["validation_status"] = "visible_write_back_failure"
    return {"structure": structure, "render": visual, "run_edits_verified": sum(map(len, edits.values())),
            "visible_verified": sum(row["validation_status"] == "visible_verified" for row in records),
            "visible_write_back_failures": sum(row["validation_status"] == "visible_write_back_failure" for row in records)}

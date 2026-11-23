"""Use DrawingML direction and installed font coverage without changing Unicode text."""
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unicodedata

from lxml import etree
import pymupdf
from pptx.oxml.ns import qn

from utils.textutil import language_profile, norm_ws, script_of

FONTS = {
    "latin": (("Arial", "arial.ttf"),),
    "arab": (("Noto Sans Arabic", "NotoSansArabic-Regular.ttf"), ("Arial", "arial.ttf")),
    "hebrew": (("Noto Sans Hebrew", "NotoSansHebrew-Regular.ttf"), ("Arial", "arial.ttf")),
    "devanagari": (("Nirmala UI", "Nirmala.ttc"), ("Mangal", "mangal.ttf")),
    "sinhala": (("Iskoola Pota", "iskpota.ttf"), ("Nirmala UI", "Nirmala.ttc")),
    "tamil": (("Nirmala UI", "Nirmala.ttc"), ("Latha", "latha.ttf")),
    "han": (("Microsoft YaHei", "msyh.ttc"), ("SimSun", "simsun.ttc")),
    "kana": (("Yu Gothic", "YuGothR.ttc"), ("Meiryo", "meiryo.ttc")),
    "hangul": (("Malgun Gothic", "malgun.ttf"),),
}


@lru_cache(maxsize=32)
def installed_font(filename: str):
    root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    path = root / filename
    if path.is_file():
        try:
            return pymupdf.Font(fontfile=str(path))
        except (ValueError, RuntimeError, OSError):
            pass
    return None


def covering_font(script: str, text: str, preferred: str | None = None) -> str | None:
    chars, preceding_script = set(), None
    for char in text:
        if unicodedata.category(char).startswith("M"):
            if preceding_script == script:
                chars.add(char)
        else:
            preceding_script = script_of(char)
            if preceding_script == script:
                chars.add(char)
    candidates = FONTS.get(script, ())
    for family, filename in candidates:
        if preferred and family.casefold() != preferred.casefold():
            continue
        font = installed_font(filename)
        if font and all(font.has_glyph(ord(char), fallback=False) for char in chars):
            return family
    return None


def plain_text(paragraph) -> str:
    return "".join(node.text or "" for node in paragraph.iter(qn("a:t")))


def shape_paragraph(paragraph) -> None:
    content = plain_text(paragraph)
    profile = language_profile(content)
    if not (set(profile["scripts"]) - {"latin", "other"}):
        return
    if profile["base_direction"] == "rtl":
        props = paragraph.find(qn("a:pPr"))
        if props is None:
            props = etree.Element(qn("a:pPr"))
            paragraph.insert(0, props)
        props.set("rtl", "1")
        if "algn" not in props.attrib:
            props.set("algn", "r")
    for run in paragraph.findall(qn("a:r")):
        value = plain_text(run)
        run_profile = language_profile(value)
        script = run_profile["script"]
        if script in {"und", "latin", "other"}:
            continue
        tag = "ea" if script in {"han", "kana", "hangul"} else "cs"
        props = run.find(qn("a:rPr"))
        if props is not None and props.find(qn("a:" + tag)) is not None:
            continue
        family = covering_font(script, value)
        if family:
            if props is None:
                props = etree.Element(qn("a:rPr"))
                run.insert(0, props)
            element = etree.Element(qn("a:" + tag), typeface=family)
            later = {"ea": {"cs", "sym", "hlinkClick", "hlinkMouseOver", "rtl", "extLst"},
                     "cs": {"sym", "hlinkClick", "hlinkMouseOver", "rtl", "extLst"}}
            index = next((i for i, child in enumerate(props)
                          if etree.QName(child).localname in later[tag]), len(props))
            props.insert(index, element)


def presentation_language_report(path: Path) -> dict:
    from pptx import Presentation

    deck = Presentation(path)
    languages = set()
    warnings = []
    rendering_probes = {}
    for number, slide in enumerate(deck.slides, 1):
        for paragraph in slide._element.iter(qn("a:p")):
            value = plain_text(paragraph)
            profile = language_profile(value)
            if profile["language"] != "und":
                languages.add(profile["language"])
            if set(profile["scripts"]) - {"latin"}:
                rendering_probes.setdefault(number, []).append(value)
            if profile["base_direction"] == "rtl":
                props = paragraph.find(qn("a:pPr"))
                if props is None or props.get("rtl") not in {"1", "true"}:
                    warnings.append({"slide": number, "issue": "RTL direction depends on inherited settings.",
                                     "text": value[:160]})
            for script in profile["scripts"]:
                if script == "latin":
                    continue
                tag = "ea" if script in {"han", "kana", "hangul"} else "cs"
                families = [node.get("typeface") for node in paragraph.iter(qn("a:" + tag))]
                if (families and not any(covering_font(script, value, family) for family in families)
                        or not families and not covering_font(script, value)):
                    warnings.append({"slide": number, "issue": f"Installed font coverage is unverified for {script}.",
                                     "text": value[:160]})
    rendered = check_rendered_text(path, rendering_probes)
    needs_review = bool(warnings) or rendered["status"] in {"renderer_unavailable", "render_failed", "render_review_required"}
    return {"languages": sorted(languages), "warnings": warnings, "visual_validation": rendered,
            "status": "manual_review_required" if needs_review else "structure_checked"}


def check_rendered_text(path: Path, samples: dict[int, list[str]]) -> dict:
    """Check that multilingual slide text survives a real local render."""
    from quality.slide_preview import renderer

    if not samples:
        return {"status": "no_multilingual_text"}
    executable = renderer()
    if not executable:
        return {"status": "renderer_unavailable", "slides": list(samples)}
    try:
        with TemporaryDirectory(prefix="docu-render-", dir=path.parent) as temp:
            folder = Path(temp)
            profile = (folder / "profile").resolve().as_uri()
            proc = subprocess.run([executable, f"-env:UserInstallation={profile}", "--headless", "--convert-to", "pdf",
                                   "--outdir", str(folder.resolve()), str(path.resolve())],
                                  capture_output=True, timeout=60)
            if proc.returncode:
                return {"status": "render_failed", "slides": list(samples)}
            with pymupdf.open(folder / path.with_suffix(".pdf").name) as pdf:
                issues = []
                for slide, expected in samples.items():
                    text = norm_ws(pdf[slide - 1].get_text()) if slide <= len(pdf) else ""
                    missing = sum(norm_ws(value) not in text for value in expected)
                    if missing:
                        issues.append({"slide": slide, "unconfirmed_paragraphs": missing,
                                       "issue": "Rendered PDF text differs. Review glyphs, order, wrapping and clipping."})
                return {"status": "render_review_required" if issues else "rendered_text_checked",
                        "renderer": "LibreOffice", "slides": list(samples), "issues": issues,
                        "geometry": "requires_visual_review"}
    except (OSError, subprocess.TimeoutExpired, ValueError, RuntimeError):
        return {"status": "render_failed", "slides": list(samples)}

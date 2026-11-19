"""Tesseract based OCR with CPU-friendly preprocessing. Offline; no model downloads at run time."""
from __future__ import annotations
import csv, io, math, os, re, shutil, subprocess, sys
from pathlib import Path
import numpy as np
import cv2
from layout.model import Line
from utils.textutil import ISO_TO_TESS, join_words, is_rtl

_SCRIPT_TO_LANG = {"Latin": "eng", "Arabic": "ara", "Japanese": "jpn", "Hangul": "kor", "Han": "jpn", "Cyrillic": "eng"}
LATIN_ALL = "eng+deu+fra+ita+nld+ces+slk"


class OCRUnavailable(RuntimeError):
    pass


def find_tesseract(settings) -> str:
    cand = settings.tesseract_cmd or os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
    if not cand and sys.platform.startswith("win"):
        for p in (Path(sys.prefix) / "tesseract" / "tesseract.exe",
                  r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
            if Path(p).exists():
                cand = str(p); break
    if not cand:
        raise OCRUnavailable("Tesseract not found. Install it (see README) or set tesseract_cmd / TESSERACT_CMD.")
    return cand


def available_langs(settings) -> set[str]:
    try:
        result = _run([find_tesseract(settings), "--list-langs"], None, 5, settings)
        if result.returncode:
            return set()
        out = result.stdout.decode("utf-8", "replace")
        return {line.strip() for line in out.splitlines()[1:] if line.strip()}
    except Exception:
        return set()


def tess_langs(lang_code: str, installed: set[str] | None = None) -> str:
    """Language pack string for a document language (always adds English for mixed content)."""
    if lang_code in ("und", "", None) or lang_code not in ISO_TO_TESS:
        pack = LATIN_ALL
    else:
        pack = ISO_TO_TESS[lang_code] + ("" if lang_code == "en" else "+eng")
    if installed:
        pack = "+".join(l for l in pack.split("+") if l in installed) or "eng"
    return pack


def render_gray(page, dpi: int) -> np.ndarray:
    import pymupdf
    if page.rect.width * page.rect.height * (dpi / 72) ** 2 > 25_000_000:
        raise ValueError("Page exceeds the OCR render limit.")
    pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), colorspace=pymupdf.csGRAY, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()


def _run(cmd, data: bytes | None, timeout: int, settings) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if settings.tessdata_dir:
        env["TESSDATA_PREFIX"] = settings.tessdata_dir
    env.setdefault("OMP_THREAD_LIMIT", "1")
    return subprocess.run(cmd, input=data, capture_output=True, timeout=timeout, env=env)


def detect_orientation(img: np.ndarray, settings) -> tuple[int, str, float]:
    """Tesseract OSD -> (rotate_degrees, script, confidence). Falls back to (0, '', 0)."""
    try:
        ok, buf = cv2.imencode(".png", img)
        r = _run([find_tesseract(settings), "stdin", "stdout", "--psm", "0"], buf.tobytes(), 60, settings)
        txt = r.stdout.decode("utf-8", "ignore")
        rot = int(re.search(r"Rotate:\s*(\d+)", txt).group(1))
        conf = float(re.search(r"Orientation confidence:\s*([\d.]+)", txt).group(1))
        script = (re.search(r"Script:\s*(\w+)", txt) or [None, ""])[1]
        return rot, script, conf
    except Exception:
        return 0, "", 0.0


def estimate_skew(gray: np.ndarray) -> float:
    """Skew in degrees (-15..15) from the dominant text-line angle."""
    h, w = gray.shape
    scale = 800.0 / max(h, w)
    small = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else gray
    bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    if bw.mean() < 2:
        return 0.0
    bw = cv2.dilate(bw, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 1)))
    lines = cv2.HoughLinesP(bw, 1, np.pi / 720, threshold=80, minLineLength=small.shape[1] // 4, maxLineGap=10)
    if lines is None:
        return 0.0
    angs = [math.degrees(math.atan2(y2 - y1, x2 - x1)) for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4)]
    angs = [a for a in angs if abs(a) <= 15]
    return float(np.median(angs)) if len(angs) >= 3 else 0.0


def preprocess(gray: np.ndarray, settings, orient: bool = True, deskew: bool = True) -> tuple[np.ndarray, dict]:
    info = {"rotate": 0, "skew": 0.0}
    img = gray
    if orient:
        rot, script, conf = detect_orientation(img, settings)
        info["osd_script"] = script
        if rot in (90, 180, 270) and conf >= 1.5:
            img = np.ascontiguousarray(np.rot90(img, k={90: 3, 180: 2, 270: 1}[rot]))   # OSD "Rotate" = clockwise fix
            info["rotate"] = rot
    sk = estimate_skew(img) if deskew else 0.0
    if 0.3 <= abs(sk) <= 15:
        h, w = img.shape
        M = cv2.getRotationMatrix2D((w / 2, h / 2), sk, 1.0)
        img = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        info["skew"] = round(sk, 2)
    img = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(img)
    return img, info


def ocr_image(img: np.ndarray, langs: str, dpi: int, settings, psm: int | None = None) -> list[Line]:
    ok, buf = cv2.imencode(".png", img)
    cmd = [find_tesseract(settings), "stdin", "stdout", "-l", langs, "--psm", str(psm or settings.ocr_psm), "-c", "preserve_interword_spaces=0", "tsv"]
    r = _run(cmd, buf.tobytes(), settings.ocr_timeout_s, settings)
    if r.returncode != 0 and not r.stdout:
        raise RuntimeError(r.stderr.decode("utf-8", "ignore")[:300])
    rows = csv.reader(io.StringIO(r.stdout.decode("utf-8", "ignore")), delimiter="\t", quoting=csv.QUOTE_NONE)
    next(rows, None)
    groups: dict[tuple, list] = {}
    for row in rows:
        if len(row) < 12 or row[0] != "5":
            continue
        txt = row[11].strip()
        try:
            conf = float(row[10])
        except ValueError:
            continue
        if not txt or conf < 0:
            continue
        key = (int(row[2]), int(row[3]), int(row[4]))
        groups.setdefault(key, []).append((int(row[6]), int(row[7]), int(row[8]), int(row[9]), conf, txt))
    k = 72.0 / dpi
    lines: list[Line] = []
    par_ids: dict[tuple, int] = {}
    for key in sorted(groups):
        ws = sorted(groups[key], key=lambda w: w[0])
        if is_rtl(" ".join(w[5] for w in ws)):
            ws = ws[::-1]                      # RTL line: logical order is right-to-left
        text = join_words([w[5] for w in ws])
        x0 = min(w[0] for w in ws); y0 = min(w[1] for w in ws)
        x1 = max(w[0] + w[2] for w in ws); y1 = max(w[1] + w[3] for w in ws)
        hs = sorted(w[3] for w in ws)
        size = hs[len(hs) // 2] * k * 0.85
        gid = par_ids.setdefault(key[:2], len(par_ids))
        lines.append(Line(text=text, bbox=(x0 * k, y0 * k, x1 * k, y1 * k), size=size, conf=float(np.mean([w[4] for w in ws])),
                          source="ocr", rtl=is_rtl(text), grp=gid))
    return lines


_SCRIPT_PACK = {"Arabic": "ara+eng", "Japanese": "jpn+eng", "Hangul": "kor+eng", "Han": "jpn+eng"}


def ocr_page(page, lang_code: str, dpi: int, settings, installed: set[str] | None = None, orient: bool = True, deskew: bool = True) -> tuple[list[Line], dict]:
    """OCR one page. If the document language is unknown the OSD script picks the language pack."""
    gray = render_gray(page, dpi)
    img, info = preprocess(gray, settings, orient=orient, deskew=deskew)
    info["dpi"] = dpi
    langs = tess_langs(lang_code, installed)
    if info.get("osd_script") in _SCRIPT_PACK:
        langs = _SCRIPT_PACK[info["osd_script"]]
        if installed:
            langs = "+".join(l for l in langs.split("+") if l in installed) or "eng"
    info["langs"] = langs
    if info.get("rotate"):
        info["note"] = "page rotated by OCR preprocessing; bbox are in rotated space"
    lines = ocr_image(img, langs, dpi, settings)
    if not lines:                                   # photo / sparse layouts: retry with sparse-text mode on the raw render
        lines = ocr_image(gray, langs, dpi, settings, psm=11)
        info["retry"] = "psm11-raw"
    return lines, info


def pick_language(page, settings, installed: set[str] | None, dpi: int = 130) -> str:
    """Trial OCR on a low-resolution render with a few candidate packs; the best weighted confidence wins."""
    from utils.textutil import detect_lang
    gray = render_gray(page, dpi)
    img, info = preprocess(gray, settings, orient=True, deskew=False)
    routed = _SCRIPT_PACK.get(info.get("osd_script", ""), None)
    cands = []
    other = ["ara+eng", LATIN_ALL] if routed else ["ara+eng", LATIN_ALL, "jpn+eng", "kor+eng"]
    for pk in ([routed] if routed else []) + other:
        pk = "+".join(l for l in pk.split("+") if not installed or l in installed)
        if pk and pk not in cands:
            cands.append(pk)
    best, best_score, best_text = None, -1.0, ""
    for pk in cands:
        try:
            lines = ocr_image(img, pk, dpi, settings)
        except Exception:
            continue
        words = sum(len(l.text.split()) for l in lines)
        conf = sum(l.conf for l in lines) / len(lines) if lines else 0.0
        score = conf * min(1.0, words / 25.0)
        if score > best_score:
            best, best_score, best_text = pk, score, " ".join(l.text for l in lines)
    if not best:
        return "und"
    lg = detect_lang(best_text)
    if lg != "und":
        return lg
    return {"ara+eng": "ar", "jpn+eng": "ja", "kor+eng": "ko"}.get(best, "en")

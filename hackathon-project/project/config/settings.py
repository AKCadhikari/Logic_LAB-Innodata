"""Central, overridable settings. Override with --config file.json or DOCUSYNC_<NAME> env vars."""
from __future__ import annotations
import json, os
from dataclasses import dataclass, asdict, fields
from pathlib import Path


@dataclass
class Settings:
    # --- schema -------------------------------------------------------------
    schema_profile: str = "brief"          # "brief" (page@n, HTML vocab) | "example" (page@num, legacy-ish)
    lang_format: str = "iso"               # "iso" (en, de ...) | "name" (english, german ...)
    emit_kind: bool = True                 # kind= attribute (bonus marks in the brief)
    emit_bbox: bool = False                # debug: data-bbox on blocks
    # --- diagnostics / OCR -----------------------------------------------------
    ocr_enabled: bool = True
    ocr_dpi: int = 200                     # render DPI for OCR (CPU friendly)
    ocr_dpi_fast: int = 150                # used once the time budget is under pressure
    ocr_psm: int = 3
    compare_suspicious: bool = True        # OCR suspicious pages and pick the more reliable text
    native_ctrl_ratio_corrupt: float = 0.04
    native_pua_ratio_suspicious: float = 0.01
    native_min_chars: int = 25             # fewer chars than this on an image page => treated as scanned
    native_vs_ocr_min_similarity: float = 0.55
    tesseract_cmd: str = ""                # optional explicit path (Windows: C:\Program Files\Tesseract-OCR\tesseract.exe)
    tessdata_dir: str = ""
    ocr_timeout_s: int = 120
    # --- layout -----------------------------------------------------------------
    header_band: float = 0.075             # top/bottom fraction of page considered header/footer band
    heading_ratio: float = 1.12
    figure_min_pt: float = 48.0
    # --- runtime ------------------------------------------------------------------
    workers: int = 0                       # 0 = auto
    time_budget_s: int = 6600              # 110 min safety margin below the 2h hard limit
    use_cache: bool = True
    cache_dir: str = ".docusync_cache"
    resume: bool = False
    # --- amendment -----------------------------------------------------------------
    amend_min_match: float = 82.0          # evidence alignment score needed to auto-correct a paragraph
    amend_review_match: float = 60.0
    amend_reorder: bool = True
    amend_nonlatin_auto: bool = False      # auto-apply non-number corrections in Arabic/CJK/Hangul text (default: review only)
    amend_use_notes: bool = False          # speaker notes are NOT trusted as evidence by default
    # --- naming ----------------------------------------------------------------------
    pptx_suffix_rule: str = "auto"         # auto | amendment_out | transformed_QA

    @classmethod
    def load(cls, path: str | None = None, **overrides) -> "Settings":
        s = cls()
        if path:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        for f in fields(s):
            env = os.environ.get("DOCUSYNC_" + f.name.upper())
            if env is not None:
                cur = getattr(s, f.name)
                setattr(s, f.name, env.lower() in ("1", "true", "yes") if isinstance(cur, bool) else type(cur)(env))
        for k, v in overrides.items():
            if v is not None and hasattr(s, k):
                setattr(s, k, v)
        return s

    def to_dict(self) -> dict:
        return asdict(self)

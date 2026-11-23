"""Central, overridable settings. Override with --config file.json or DOCUSYNC_<NAME> env vars."""
from __future__ import annotations
import json, os
from dataclasses import dataclass, asdict, fields
from pathlib import Path


@dataclass
class Settings:
    # --- schema -------------------------------------------------------------
    schema_profile: str = "brief"          # brief | example (page attribute only) | sample (legacy hierarchy)
    lang_format: str = "iso"               # "iso" (en, de ...) | "name" (english, german ...)
    emit_kind: bool = True                 # kind= attribute (bonus marks in the brief)
    emit_bbox: bool = False                # debug: data-bbox on blocks
    # --- diagnostics / OCR -----------------------------------------------------
    ocr_enabled: bool = True
    ocr_backend: str = "glm-ocr"           # glm-ocr with Paddle fallback, or explicit legacy tesseract
    glm_ocr_url: str = "http://localhost:11434"
    glm_ocr_model: str = "glm-ocr:q8_0"
    glm_ocr_timeout_s: int = 300
    glm_ocr_keep_alive: str = "0"
    glm_ocr_retries: int = 0
    glm_ocr_max_edge: int = 1800
    glm_ocr_tokens: int = 4096
    paddle_ocr_timeout_s: int = 90
    paddle_det_model_dir: str = ""
    paddle_rec_model_dir: str = ""
    paddle_ocr_lang: str = ""              # override the language inferred from native text
    ocr_min_chars: int = 2
    ocr_max_chars: int = 42000
    ocr_min_native_ratio: float = 0.30
    ocr_max_repeat_ratio: float = 0.30
    ocr_max_corruption_ratio: float = 0.03
    ocr_min_paddle_confidence: float = 0.45
    ocr_min_fallback_similarity: float = 0.80
    ocr_dpi: int = 200                     # render DPI for OCR (CPU friendly)
    ocr_dpi_fast: int = 150                # used once the time budget is under pressure
    ocr_psm: int = 3
    compare_suspicious: bool = True        # OCR suspicious pages and pick the more reliable text
    native_ctrl_ratio_corrupt: float = 0.04
    native_pua_ratio_suspicious: float = 0.01
    native_min_chars: int = 25             # fewer chars than this on an image page => treated as scanned
    native_mixed_max_chars: int = 180
    native_mixed_min_image_coverage: float = 0.50
    native_vs_ocr_min_similarity: float = 0.55
    tesseract_cmd: str = ""                # optional explicit path (Windows: C:\Program Files\Tesseract-OCR\tesseract.exe)
    tessdata_dir: str = ""
    ocr_timeout_s: int = 120
    # --- layout -----------------------------------------------------------------
    header_band: float = 0.075             # top/bottom fraction of page considered header/footer band
    heading_ratio: float = 1.12
    figure_min_pt: float = 48.0
    docling_enabled: bool = True
    docling_artifacts: str = ""
    docling_python: str = ""
    docling_timeout_s: int = 40
    # --- runtime ------------------------------------------------------------------
    workers: int = 0                       # 0 = auto
    time_budget_s: int = 6600              # 110 min safety margin below the 2h hard limit
    use_cache: bool = True
    cache_dir: str = ".docusync_cache"
    resume: bool = False
    # --- amendment -----------------------------------------------------------------
    amend_min_match: float = 82.0          # evidence alignment score needed to auto-correct a paragraph
    amend_review_match: float = 60.0
    amend_reorder: bool = False           # retained for existing settings files
    amend_nonlatin_auto: bool = False      # auto-apply non-number corrections in Arabic/CJK/Hangul text (default: review only)
    amend_use_notes: bool = True          # use source page notes as search hints
    # --- optional version comparison assistance ---------------------------------
    ollama_enabled: bool = True
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:4b"
    ollama_timeout_s: int = 300
    ollama_keep_alive: str = "0"
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
        for name, targets, convert in (
            ("OLLAMA_BASE_URL", ("glm_ocr_url", "ollama_url"), str),
            ("OLLAMA_TIMEOUT", ("glm_ocr_timeout_s", "ollama_timeout_s"), int),
            ("OLLAMA_KEEP_ALIVE", ("glm_ocr_keep_alive", "ollama_keep_alive"), str),
            ("GLM_OCR_MODEL", ("glm_ocr_model",), str),
            ("QWEN_MODEL", ("ollama_model",), str),
        ):
            if name in os.environ:
                for target in targets:
                    setattr(s, target, convert(os.environ[name]))
        for k, v in overrides.items():
            if v is not None and hasattr(s, k):
                setattr(s, k, v)
        return s

    def to_dict(self) -> dict:
        return asdict(self)

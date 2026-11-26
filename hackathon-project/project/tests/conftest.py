import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest


@pytest.fixture
def settings():
    from config.settings import Settings
    s = Settings(ocr_automatic_secondary="paddleocr", ocr_automatic_tertiary="")
    s.use_cache = False; s.workers = 1
    return s


@pytest.fixture
def audit(tmp_path):
    from utils.logs import AuditLog
    return AuditLog(tmp_path / "audit.jsonl")



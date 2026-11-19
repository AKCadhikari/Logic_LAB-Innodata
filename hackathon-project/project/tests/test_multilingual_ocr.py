from html import escape

import pymupdf
import pytest

from ocr.engine import available_langs, ocr_page
from utils.textutil import ISO_TO_TESS


@pytest.mark.parametrize(("language", "sample", "expected"), [
    ("en", "Optical recognition reads this example document.", "recognition"),
    ("fr", "Le résultat vérifié confirme la qualité du document.", "qualité"),
    ("de", "Die Überprüfung bestätigt die Qualität des Dokuments.", "Qualität"),
    ("it", "La verifica conferma la qualità del documento.", "qualità"),
    ("nl", "De verificatie bevestigt de kwaliteit van het document.", "kwaliteit"),
    ("cs", "Kontrola potvrdila kvalitu zpracování dokumentu.", "dokumentu"),
    ("sk", "Overenie potvrdilo kvalitu spracovania dokumentu.", "dokumentu"),
    ("ar", "هذا المستند يحتوي على معلومات مهمة", "المستند"),
    ("ja", "これは日本語の文書です。", "日本語"),
    ("ko", "이 문서는 중요한 정보를 포함합니다.", "문서"),
])
def test_language_pack_reads_rasterized_text(settings, language, sample, expected):
    available = available_langs(settings)
    pack = ISO_TO_TESS[language]
    if pack not in available:
        pytest.skip(f"Tesseract language pack {pack} is unavailable.")
    with pymupdf.open() as source, pymupdf.open() as scan:
        page = source.new_page(width=540, height=180)
        page.insert_htmlbox(page.rect + (20, 20, -20, -20), f'<p dir="auto">{escape(sample)}</p>',
                            css="p {font-size: 24pt}")
        raster = scan.new_page(width=540, height=180)
        raster.insert_image(raster.rect, pixmap=page.get_pixmap(dpi=200))
        assert not raster.get_text().strip()
        lines, info = ocr_page(raster, language, 200, settings, available, orient=False, deskew=False)
    assert pack in info["langs"].split("+")
    assert expected in " ".join(line.text for line in lines)
    assert all(line.bbox and 0 <= line.conf <= 100 for line in lines)

"""Exercise one engine on a locally generated, known image."""
from dataclasses import replace
from uuid import uuid4

import pymupdf

from ocr.router import OCRRouter
from reporting.execution import reports
from utils import execution_audit as audit


def test_engine(settings, engine, *, source_pdf=None, expected_code=None):
    token = audit.test_mode.set("ENGINE_PROBE")
    code = expected_code or str(1000000 + uuid4().int % 9000000)
    try:
        with pymupdf.open() as text, pymupdf.open(source_pdf) if source_pdf else pymupdf.open() as scan:
            if not source_pdf:
                reference = text.new_page(width=460, height=155)
                reference.insert_text((32, 50), f"Pressure reference {code}", fontsize=23)
                reference.insert_text((32, 90), "The laboratory reading remained stable.", fontsize=20)
                page = scan.new_page(width=460, height=155)
                page.insert_image(page.rect, pixmap=reference.get_pixmap(dpi=180))
            page = scan[0]
            config = replace(settings, ocr_backend=engine, audit_private_responses=False, paddle_ocr_lang="en")
            with audit.scope("labelled-engine-test" if source_pdf else "synthetic-engine-test",
                             audit.digest(scan.tobytes())) as trace:
                router = OCRRouter(config, None)
                try:
                    result, detail = router.recognise(page, "en", config.ocr_dpi, {"health": "absent"},
                                                      lambda _: None, probe=True)
                finally:
                    router.close()
                verified = bool(result and code in result.text()
                                and any(item["executed"] for item in trace["executions"]))
                if result and not verified:
                    audit.mark(result.execution_ids, validation="rejected")
                evidence = reports(None, trace)["ai_execution_report.json"]
            error = None if verified else next((issue for attempt in reversed(detail["attempts"])
                                                for issue in attempt["issues"]),
                                               "The engine did not reproduce the known image text.")
            return {"engine": engine, "verified": verified, "error": error, "completed_at": audit.timestamp(),
                    "attempts": detail["attempts"], "executions": evidence["executions"]}
    finally:
        audit.test_mode.reset(token)

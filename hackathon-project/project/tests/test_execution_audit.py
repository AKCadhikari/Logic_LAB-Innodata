import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import sleep

import pytest

from config.settings import Settings
from layout.model import Block, DocIR, PageIR
from reporting.execution import anchor_ir, reports
from utils import execution_audit as audit, ollama
from xml_engine.serializer import to_xml


@contextmanager
def fake_service(code, body, delay=0):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            sleep(delay)
            self.send_response(code)
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            worker.join()


def test_mocked_http_preserves_zero_metrics_and_tracks_used_and_discarded_outputs():
    payload = b'{"model":"fixture:test","done":true,"response":"private statement","eval_count":0}'
    token = audit.test_mode.set("MOCKED_TEST")
    try:
        with fake_service(200, payload) as url, audit.scope("doc", "pdf-hash") as trace:
            for _ in range(2):
                with audit.execution("page_ocr", "ollama", "fixture:test", page=1) as record:
                    ollama.request_json(url, "/api/generate", {"prompt": "sensitive message"}, 3)
                    record["validation"] = "passed"
            used, unused = trace["executions"]
            block = Block("p", "private statement", source="ocr",
                          source_reference={"execution_ids": [used["execution_id"]]})
            doc = DocIR("doc", "fixture.pdf", pages=[PageIR(1, 500, 700, [block], source="ocr")])
            anchor_ir(doc, trace["trace_id"])
            nodes = {}
            xml = to_xml(doc, Settings(), locations=nodes)
            built = reports(doc, trace, xml, nodes)
            actual, discarded = built["ai_execution_report.json"]["executions"]
            assert actual["performance"] == {"eval_count": 0}
            assert actual["response_sha256"] == audit.digest(payload)
            assert {"MOCKED_TEST", "USED_DOWNSTREAM"} <= set(actual["statuses"])
            assert {"pydantic_ir", "xml"} == {item["consumer"] for item in actual["downstream"]}
            assert discarded["execution_id"] == unused["execution_id"] and "EXECUTED_BUT_NOT_USED" in discarded["statuses"]
            assert built["model_usage_report.json"]["requests"] == 0
            assert "private statement" not in json.dumps(built)
            assert "sensitive message" not in json.dumps(built)
    finally:
        audit.test_mode.reset(token)


@pytest.mark.parametrize("code,body,delay,expected", [
    (404, b'{"error":"unknown model"}', 0, "http_error"),
    (200, b'not json', 0, "invalid"),
    (200, b'{}', .2, "failed"),
])
def test_inference_transport_failures_never_claim_execution(code, body, delay, expected):
    token = audit.test_mode.set("MOCKED_TEST")
    try:
        with fake_service(code, body, delay) as url, audit.scope("doc", "pdf-hash") as trace:
            with pytest.raises(ollama.OllamaError), audit.execution("ocr", "ollama", "absent:model"):
                ollama.request_json(url, "/api/generate", {"model": "absent:model"}, .05 if delay else 3)
            row = trace["executions"][0]
            assert row["request_status"] == ("attempted" if delay else "sent")
            assert row["response_status"] == expected
            assert not row["executed"] and row["error"]["status"]
    finally:
        audit.test_mode.reset(token)

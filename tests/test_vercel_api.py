import io
import json
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIRECTORY))

from api.index import app


def call_wsgi(path, operation="", method="GET", payload=None, cookie=None):
    body = json.dumps(payload or {}).encode("utf-8")
    environ = {
        "PATH_INFO": path,
        "QUERY_STRING": f"operation={operation}" if operation else "",
        "REQUEST_METHOD": method,
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": "application/json",
        "HTTP_HOST": "blackbird.example",
        "HTTP_ORIGIN": "https://blackbird.example",
        "wsgi.input": io.BytesIO(body),
    }
    if cookie:
        environ["HTTP_COOKIE"] = cookie

    response = {}

    def start_response(status, headers):
        response["status"] = status
        response["headers"] = dict(headers)

    response_body = b"".join(app(environ, start_response))
    return response, response_body


class VercelApiTests(unittest.TestCase):
    def test_ai_key_status_is_json_and_does_not_require_api_url(self):
        response, body = call_wsgi("/api", operation="ai-key")

        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(json.loads(body), {"enabled": False})

    def test_invalid_search_returns_json_validation_error(self):
        response, body = call_wsgi(
            "/api",
            operation="search",
            method="POST",
            payload={"mode": "email", "identifiers": ["invalid"]},
        )

        self.assertEqual(response["status"], "400 Bad Request")
        self.assertEqual(json.loads(body)["error"], "Enter a valid email address.")

    def test_search_streams_without_an_ai_key(self):
        def fake_search(context, payload, api_key):
            self.assertIsNone(api_key)
            context.emit_event({"type": "start", "mode": payload["mode"], "searches": 1})
            context.emit_event({"type": "complete", "partial": False})

        with patch("api.index.run_search", side_effect=fake_search):
            response, body = call_wsgi(
                "/api",
                operation="search",
                method="POST",
                payload={"mode": "username", "identifiers": ["blackbird-test-identifier"]},
            )

        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(
            response["headers"]["Content-Type"],
            "application/x-ndjson; charset=utf-8",
        )
        events = [json.loads(line) for line in body.decode("utf-8").splitlines()]
        self.assertEqual([event["type"] for event in events], ["start", "complete"])

    def test_unexpected_error_log_omits_search_values(self):
        secret = "private-search-value"
        with self.assertLogs(level=logging.ERROR) as captured:
            try:
                raise ValueError(secret)
            except ValueError as error:
                from api.index import _log_failure

                _log_failure("search", error)

        self.assertNotIn(secret, "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()

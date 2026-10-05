import asyncio
import io
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

PROJECT_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIRECTORY / "src"))

from modules.utils.public_resolver import PublicNetworkResolver
from modules.utils.search_runner import collect_results
from modules.whatsmyname.list_operations import checkUpdates
from src.vercel_app import (
    PROJECT_ROOT,
    RequestValidationError,
    _analyze_found_accounts,
    expand_usernames,
    issue_ai_key,
    normalize_identifier,
    parse_json_request,
    require_same_origin,
    run_search,
    validate_options,
)


class VercelRequestValidationTests(unittest.TestCase):
    def test_accepts_supported_search_options(self):
        options = validate_options(
            {
                "mode": "username",
                "identifiers": ["alice", "bob"],
                "permute": True,
                "excludeNsfw": True,
                "concurrency": 30,
                "timeout": 15,
            }
        )

        self.assertEqual(options["identifiers"], ["alice", "bob"])
        self.assertTrue(options["permute"])
        self.assertEqual(options["concurrency"], 30)

    def test_deduplicates_values_and_rejects_invalid_email(self):
        options = validate_options(
            {"mode": "email", "identifiers": ["user@example.com", "user@example.com"]}
        )
        self.assertEqual(options["identifiers"], ["user@example.com"])
        with self.assertRaises(RequestValidationError):
            normalize_identifier("not-an-email", "email")

    def test_rejects_invalid_option_types_and_limits(self):
        with self.assertRaises(RequestValidationError):
            validate_options({"mode": "username", "identifiers": ["alice"], "timeout": True})
        with self.assertRaises(RequestValidationError):
            validate_options(
                {"mode": "username", "identifiers": [f"user{index}" for index in range(26)]}
            )
        with self.assertRaises(RequestValidationError):
            validate_options(
                {"mode": "email", "identifiers": ["user@example.com"], "permute": True}
            )

    def test_blocks_private_proxy_addresses(self):
        with self.assertRaises(RequestValidationError):
            validate_options(
                {
                    "mode": "username",
                    "identifiers": ["alice"],
                    "proxy": "http://127.0.0.1:3128",
                }
            )

    def test_same_origin_is_required(self):
        handler = SimpleNamespace(
            headers={"Origin": "https://blackbird.example", "Host": "blackbird.example"}
        )
        require_same_origin(handler)
        handler.headers["Origin"] = "https://attacker.example"
        with self.assertRaises(RequestValidationError):
            require_same_origin(handler)

    def test_rejects_oversized_request_body(self):
        handler = SimpleNamespace(
            headers={"Content-Length": str(4 * 1024 * 1024 + 1)},
            rfile=io.BytesIO(),
        )
        with self.assertRaises(RequestValidationError):
            parse_json_request(handler)

    def test_permutations_are_deduplicated(self):
        options = validate_options(
            {"mode": "username", "identifiers": ["Ada", "Lovelace"], "permute": True}
        )
        expanded = expand_usernames(options)
        self.assertGreater(len(expanded), 1)
        self.assertEqual(len(expanded), len(set(expanded)))


class VercelRuntimeSafetyTests(unittest.TestCase):
    def test_bundled_username_data_is_used_when_remote_refresh_fails(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "wmn-data.json"
            console = Console(file=io.StringIO(), quiet=True)
            config = SimpleNamespace(
                USERNAME_LIST_PATH=str(target),
                USERNAME_FALLBACK_PATH=str(PROJECT_ROOT / "data" / "wmn-data.json"),
                USERNAME_LIST_URL="https://example.invalid/wmn-data.json",
                timeout=30,
                dataset_timeout=8,
                console=console,
                verbose=False,
                suppress_sensitive_logs=True,
            )
            with patch("modules.whatsmyname.list_operations.do_sync_request", return_value=None):
                checkUpdates(config)
            self.assertTrue(target.is_file())
            self.assertGreater(len(json.loads(target.read_text(encoding="utf-8"))["sites"]), 600)

    def test_private_addresses_are_rejected_by_public_resolver(self):
        async def check_address():
            resolver = PublicNetworkResolver()
            with self.assertRaises(OSError):
                await resolver.resolve("127.0.0.1", 80)
            await resolver.close()

        asyncio.run(check_address())

    def test_search_tasks_are_cancelled_at_deadline(self):
        async def check_deadline():
            async def wait_forever():
                await asyncio.Event().wait()

            loop = asyncio.get_running_loop()
            config = SimpleNamespace(
                deadline_at=loop.time() - 1,
                progress_callback=lambda event: None,
                console=Console(file=io.StringIO(), quiet=True),
            )
            return await collect_results([wait_forever()], config, 1, "search")

        result = asyncio.run(check_deadline())
        self.assertTrue(result["partial"])
        self.assertEqual(result["completed"], 0)

    def test_sensitive_error_details_are_not_logged(self):
        secret = "private-search-value"
        output = io.StringIO()
        logger = logging.getLogger("root")
        handler = logging.StreamHandler(output)
        logger.addHandler(handler)
        try:
            from modules.utils.log import logError

            logError(
                ValueError(secret),
                f"failed for {secret}",
                SimpleNamespace(suppress_sensitive_logs=True),
            )
        finally:
            logger.removeHandler(handler)
        self.assertNotIn(secret, output.getvalue())

    def test_ai_setup_requires_explicit_consent(self):
        handler = SimpleNamespace(headers={}, rfile=io.BytesIO(b'{"consent":false}'))
        handler.headers = {"Content-Length": "17", "Origin": "https://example.com", "Host": "example.com"}
        with self.assertRaises(RequestValidationError):
            issue_ai_key(handler)

    def test_ai_analysis_uses_the_original_service_user_agent(self):
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "success": True,
                "data": {"result": {"summary": "Synthetic test summary"}},
            },
        )
        config = SimpleNamespace(suppress_sensitive_logs=True, ai_analysis=None)
        found_accounts = [{"name": "Site One"}, {"name": "Site Two"}, {"name": "Site Three"}]

        with patch("src.vercel_app.get_ai_base_url", return_value="https://ai.blackbird.run"):
            with patch("src.vercel_app.requests.post", return_value=response) as request:
                result = _analyze_found_accounts(
                    found_accounts,
                    "synthetic-test-key",
                    config,
                    remaining_seconds=10,
                )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["result"]["summary"], "Synthetic test summary")
        self.assertEqual(request.call_args.kwargs["headers"]["User-Agent"], "blackbird-cli")

    def test_ai_analysis_logs_safe_upstream_status(self):
        response = SimpleNamespace(status_code=429)
        config = SimpleNamespace(suppress_sensitive_logs=True)
        found_accounts = [{"name": "Site One"}, {"name": "Site Two"}, {"name": "Site Three"}]

        with patch("src.vercel_app.get_ai_base_url", return_value="https://ai.blackbird.run"):
            with patch("src.vercel_app.requests.post", return_value=response):
                with self.assertLogs(level=logging.ERROR) as captured:
                    result = _analyze_found_accounts(
                        found_accounts,
                        "synthetic-test-key",
                        config,
                        remaining_seconds=10,
                    )

        self.assertEqual(result["status"], "error")
        self.assertIn("HTTP 429", result["message"])
        self.assertIn("upstream_http_status=429", "\n".join(captured.output))

    def test_search_streams_exports_without_persisting_them(self):
        class FakeHandler:
            def __init__(self):
                self.events = []
                self.stream_started = False

            def begin_event_stream(self):
                self.stream_started = True

            def emit_event(self, event):
                self.events.append(event)

        def fake_search(identifier, config):
            config.searchPartial = False
            dump_path = Path(config.saveDirectory) / f"dump_{identifier}" / "Example.html"
            dump_path.parent.mkdir(parents=True, exist_ok=True)
            dump_path.write_text("public profile page", encoding="utf-8")
            return [
                {
                    "name": "Example",
                    "url": f"https://example.com/{identifier}",
                    "category": "social",
                    "status": "FOUND",
                    "metadata": None,
                }
            ]

        handler = FakeHandler()
        with patch("src.vercel_app.verifyUsername", side_effect=fake_search):
            run_search(
                handler,
                {
                    "mode": "username",
                    "identifiers": ["alice"],
                    "noUpdate": True,
                    "csv": True,
                    "dump": True,
                },
                None,
            )

        self.assertTrue(handler.stream_started)
        self.assertEqual(handler.events[-1]["type"], "complete")
        filenames = {
            event["filename"]
            for event in handler.events
            if event["type"] == "artifact-start"
        }
        self.assertTrue(any(name.endswith(".csv") for name in filenames))
        self.assertIn("blackbird-dumps.zip", filenames)

    def test_ai_analysis_cannot_start_without_a_browser_key(self):
        class FakeHandler:
            def begin_event_stream(self):
                self.stream_started = True

            def emit_event(self, event):
                return None

        handler = FakeHandler()
        with self.assertRaises(RequestValidationError):
            run_search(
                handler,
                {"mode": "username", "identifiers": ["alice"], "aiConsent": True},
                None,
            )
        self.assertFalse(getattr(handler, "stream_started", False))


if __name__ == "__main__":
    unittest.main()

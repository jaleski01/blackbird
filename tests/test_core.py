import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

PROJECT_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIRECTORY / "src"))

from src.modules.core.email import verifyEmail
from src.modules.core.username import verifyUsername


def make_config():
    return SimpleNamespace(
        console=Console(file=io.StringIO(), quiet=True),
        filter="name=TestSite",
        no_nsfw=False,
        proxy=None,
        verbose=False,
        timeout=5,
        max_concurrent_requests=2,
        dump=False,
        ai=False,
        aiModel=None,
        userAgent="Blackbird offline test",
        currentUser=None,
        currentEmail=None,
    )


def make_site():
    return {
        "name": "TestSite",
        "uri_check": "https://example.invalid/{account}",
        "e_code": 200,
        "e_string": "profile",
        "m_string": "missing",
        "m_code": 404,
        "cat": "social",
        "pre_check": None,
        "input_operation": None,
        "method": "GET",
        "data": None,
        "headers": None,
        "metadata": None,
    }


async def fake_site_response(method, url, session, config, data=None, customHeaders=None):
    return {
        "url": url,
        "status_code": 200,
        "content": "profile found",
        "headers": {},
        "json": None,
    }


class CoreSearchTests(unittest.TestCase):
    def test_username_search_uses_site_response_without_network(self):
        config = make_config()
        with patch(
            "src.modules.core.username.do_async_request",
            side_effect=fake_site_response,
        ):
            found_accounts = verifyUsername(
                "blackbird-test-user",
                config,
                sitesToSearch=[make_site()],
                metadata_params={"sites": {}},
            )

        self.assertEqual([account["name"] for account in found_accounts], ["TestSite"])

    def test_email_search_uses_site_response_without_network(self):
        config = make_config()
        site = make_site()
        with (
            patch("src.modules.core.email.readList", return_value={"sites": [site]}),
            patch(
                "src.modules.core.email.do_async_request",
                side_effect=fake_site_response,
            ),
        ):
            found_accounts = verifyEmail("blackbird-test@example.invalid", config)

        self.assertEqual([account["name"] for account in found_accounts], ["TestSite"])


if __name__ == "__main__":
    unittest.main()

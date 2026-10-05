import base64
import io
import ipaddress
import json
import logging
import os
import shutil
import socket
import tempfile
import time
import zipfile
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import urlsplit

import requests
from rich.console import Console

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in os.sys.path:
    os.sys.path.insert(0, str(PROJECT_ROOT / "src"))

import config as base_config
from modules.core.email import verifyEmail
from modules.core.username import verifyUsername
from modules.export.csv import saveToCsv
from modules.export.file_operations import createSaveDirectory, generateName
from modules.export.json import saveToJson
from modules.export.pdf import saveToPdf
from modules.utils.filter import applyFilters
from modules.utils.permute import Permute
from modules.utils.userAgent import getRandomUserAgent
from modules.whatsmyname.list_operations import checkUpdates, readList

MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_IDENTIFIERS = 25
MAX_PERMUTATIONS = 100
MAX_IDENTIFIER_LENGTH = 254
MAX_TIMEOUT_SECONDS = 60
MAX_CONCURRENCY = 30
SEARCH_BUDGET_SECONDS = 270
DATASET_TIMEOUT_SECONDS = 8
ARTIFACT_CHUNK_BYTES = 48 * 1024
AI_REQUEST_TIMEOUT_SECONDS = 20
AI_COOKIE_NAME = "blackbird_ai_key"
AI_COOKIE_MAX_AGE = 30 * 24 * 60 * 60


class RequestValidationError(ValueError):
    """Raised when an API request does not match the supported contract."""


def parse_json_request(handler):
    raw_length = handler.headers.get("Content-Length", "")
    if not raw_length.isdecimal():
        raise RequestValidationError("A valid Content-Length header is required.")
    length = int(raw_length)
    if length > MAX_REQUEST_BYTES:
        raise RequestValidationError("The request body exceeds 4 MB.")
    try:
        value = json.loads(handler.rfile.read(length))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RequestValidationError("The request body must be valid JSON.") from error
    if not isinstance(value, dict):
        raise RequestValidationError("The request body must be a JSON object.")
    return value


def require_same_origin(handler):
    origin = handler.headers.get("Origin")
    if not origin:
        raise RequestValidationError("A same-origin request is required.")
    try:
        origin_parts = urlsplit(origin)
    except ValueError as error:
        raise RequestValidationError("The request origin is invalid.") from error
    expected_host = (
        handler.headers.get("X-Forwarded-Host")
        or handler.headers.get("Host")
        or ""
    ).split(",", 1)[0].strip().lower()
    origin_host = (origin_parts.netloc or "").lower()
    is_local = origin_parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        origin_parts.scheme not in ({"https", "http"} if is_local else {"https"})
        or origin_host != expected_host
    ):
        raise RequestValidationError("The request origin does not match this site.")


def _is_boolean(value):
    return isinstance(value, bool)


def normalize_identifier(value, mode):
    if not isinstance(value, str):
        raise RequestValidationError("Each search value must be text.")
    identifier = value.strip()
    if (
        not identifier
        or len(identifier) > MAX_IDENTIFIER_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in identifier)
    ):
        raise RequestValidationError("A search value is empty, too long, or invalid.")
    if mode == "email" and (identifier.count("@") != 1 or any(c.isspace() for c in identifier)):
        raise RequestValidationError("Enter a valid email address.")
    return identifier


def validate_proxy(proxy):
    if proxy in (None, ""):
        return None
    if not isinstance(proxy, str) or len(proxy) > 2048:
        raise RequestValidationError("The proxy address is invalid.")
    try:
        parts = urlsplit(proxy)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError
        try:
            addresses = [ipaddress.ip_address(parts.hostname)]
        except ValueError:
            addresses = [
                ipaddress.ip_address(record[4][0])
                for record in socket.getaddrinfo(
                    parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM
                )
            ]
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError
        parts.port
    except (OSError, ValueError) as error:
        raise RequestValidationError(
            "Use an HTTP(S) proxy with a public hostname and no embedded credentials."
        ) from error
    return proxy


def validate_options(body):
    mode = body.get("mode")
    if mode not in {"username", "email"}:
        raise RequestValidationError("Choose username or email search.")
    values = body.get("identifiers")
    if not isinstance(values, list) or not values or len(values) > MAX_IDENTIFIERS:
        raise RequestValidationError(
            f"Provide between 1 and {MAX_IDENTIFIERS} search values."
        )
    identifiers = list(dict.fromkeys(normalize_identifier(value, mode) for value in values))
    if len(identifiers) > MAX_IDENTIFIERS:
        raise RequestValidationError(f"A maximum of {MAX_IDENTIFIERS} values is allowed.")

    boolean_options = {
        "permute",
        "permuteAll",
        "excludeNsfw",
        "dump",
        "csv",
        "pdf",
        "json",
        "verbose",
        "noUpdate",
        "aiConsent",
    }
    for name in boolean_options:
        if name in body and not _is_boolean(body[name]):
            raise RequestValidationError(f"The {name} option must be boolean.")

    permute = body.get("permute", False)
    permute_all = body.get("permuteAll", False)
    if mode == "email" and (permute or permute_all):
        raise RequestValidationError("Permutations are only available for usernames.")
    if permute and permute_all:
        raise RequestValidationError("Choose one permutation mode.")
    if (permute or permute_all) and len(identifiers) < 2:
        raise RequestValidationError("Permutations need at least two username values.")

    timeout = body.get("timeout", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise RequestValidationError(f"Timeout must be between 1 and {MAX_TIMEOUT_SECONDS} seconds.")
    concurrency = body.get("concurrency", MAX_CONCURRENCY)
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= MAX_CONCURRENCY:
        raise RequestValidationError(f"Concurrency must be between 1 and {MAX_CONCURRENCY}.")

    filter_expression = body.get("filter", "")
    if not isinstance(filter_expression, str) or len(filter_expression) > 200:
        raise RequestValidationError("The site filter is invalid.")
    return {
        "mode": mode,
        "identifiers": identifiers,
        "permute": permute,
        "permuteAll": permute_all,
        "excludeNsfw": body.get("excludeNsfw", False),
        "dump": body.get("dump", False),
        "csv": body.get("csv", False),
        "pdf": body.get("pdf", False),
        "json": body.get("json", False),
        "verbose": body.get("verbose", False),
        "noUpdate": body.get("noUpdate", False),
        "aiConsent": body.get("aiConsent", False),
        "timeout": timeout,
        "concurrency": concurrency,
        "filter": filter_expression.strip(),
        "proxy": validate_proxy(body.get("proxy", "")),
    }


def read_ai_cookie(handler):
    cookies = SimpleCookie()
    try:
        cookies.load(handler.headers.get("Cookie", ""))
    except Exception:
        return None
    cookie = cookies.get(AI_COOKIE_NAME)
    if cookie is None:
        return None
    value = cookie.value
    if not value or len(value) > 4096 or any(ord(character) < 33 for character in value):
        return None
    return value


def get_ai_base_url():
    raw_url = os.environ.get("API_URL", "").strip()
    if not raw_url:
        logging.error("Blackbird AI is unavailable because API_URL is not configured.")
        raise RequestValidationError("AI is unavailable because API_URL is not configured.")
    try:
        parts = urlsplit(raw_url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or parts.hostname.lower() != "ai.blackbird.run"
        ):
            raise ValueError
        parts.port
    except ValueError as error:
        logging.error("Blackbird AI is unavailable because API_URL is invalid.")
        raise RequestValidationError("The configured AI service URL is invalid.") from error
    return raw_url.rstrip("/")


def prepare_search_config(options, work_directory, emit):
    values = {
        name: value
        for name, value in vars(base_config).items()
        if not name.startswith("__") and not isinstance(value, ModuleType)
    }
    config = SimpleNamespace(**values)
    config.USERNAME_LIST_PATH = str(work_directory / "wmn-data.json")
    config.USERNAME_FALLBACK_PATH = str(PROJECT_ROOT / "data" / "wmn-data.json")
    config.EMAIL_LIST_PATH = str(PROJECT_ROOT / "data" / "email-data.json")
    config.USERNAME_METADATA_LIST_PATH = str(PROJECT_ROOT / "data" / "wmn-metadata.json")
    config.console = Console(file=io.StringIO(), quiet=True)
    config.currentUser = None
    config.currentEmail = None
    config.username = None
    config.email = None
    config.username_file = None
    config.email_file = None
    config.filter = options["filter"] or None
    config.no_nsfw = options["excludeNsfw"]
    config.permute = options["permute"]
    config.permuteall = options["permuteAll"]
    config.proxy = options["proxy"]
    config.timeout = options["timeout"]
    config.dataset_timeout = DATASET_TIMEOUT_SECONDS
    config.max_concurrent_requests = options["concurrency"]
    config.no_update = options["noUpdate"]
    config.ai = False
    config.aiModel = None
    config.ai_analysis = None
    config.api_url = os.environ.get("API_URL", "").strip()
    config.instagram_session_id = os.environ.get("INSTAGRAM_SESSION_ID", "").strip() or None
    config.csv = options["csv"]
    config.pdf = options["pdf"]
    config.json = options["json"]
    config.dump = options["dump"]
    config.verbose = options["verbose"]
    config.results_root = str(work_directory / "results")
    config.dateRaw = datetime.now().strftime("%m_%d_%Y")
    config.datePretty = datetime.now().strftime("%B %d, %Y")
    config.userAgent = getRandomUserAgent(config)
    config.suppress_sensitive_logs = True
    config.public_network_only = True
    config.encode_identifier_urls = True
    config.disable_remote_images = True
    config.deadline_at = time.monotonic() + SEARCH_BUDGET_SECONDS
    config.progress_callback = emit
    return config


def _load_search_list(config, mode):
    if mode == "username":
        target = Path(config.USERNAME_LIST_PATH)
        fallback = Path(config.USERNAME_FALLBACK_PATH)
        if config.no_update:
            shutil.copyfile(fallback, target)
        else:
            checkUpdates(config)
        return readList("username", config), readList("metadata", config)
    return readList("email", config), None


def _validate_filter(config, sites):
    try:
        filtered_sites = applyFilters(sites, config)
    except (SystemExit, IndexError, ValueError, TypeError) as error:
        raise RequestValidationError("The site filter is invalid or matches no sites.") from error
    if not filtered_sites:
        raise RequestValidationError("The site filter matches no sites.")
    return filtered_sites


def expand_usernames(options):
    identifiers = options["identifiers"]
    if not options["permute"] and not options["permuteAll"]:
        return identifiers
    if len(identifiers) > 6:
        raise RequestValidationError("Permutations support at most six username values.")
    way = "all" if options["permuteAll"] else "strict"
    expanded = list(dict.fromkeys(Permute(identifiers).gather(way)))
    if len(expanded) > MAX_PERMUTATIONS:
        raise RequestValidationError(
            f"This permutation would create more than {MAX_PERMUTATIONS} searches."
        )
    return expanded


def _analyze_found_accounts(found_accounts, api_key, config, remaining_seconds):
    if len(found_accounts) < 3:
        return {"status": "skipped", "reason": "At least three found accounts are required."}
    try:
        api_base = get_ai_base_url()
        response = requests.post(
            f"{api_base}/analyze",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "blackbird-vercel",
                "x-api-key": api_key,
            },
            json={"prompt": ", ".join(account["name"] for account in found_accounts)},
            timeout=min(AI_REQUEST_TIMEOUT_SECONDS, max(1, int(remaining_seconds))),
            verify=True,
        )
        if response.status_code != 200:
            from modules.utils.log import logError

            logError(
                RuntimeError(f"AI service returned HTTP {response.status_code}"),
                "AI analysis failed",
                config,
            )
            return {"status": "error", "message": "The AI service could not complete the analysis."}
        body = response.json()
        result = body.get("data", {}).get("result") if body.get("success") else None
        if not isinstance(result, dict):
            from modules.utils.log import logError

            logError(ValueError("AI service returned an invalid result"), "AI analysis failed", config)
            return {"status": "error", "message": "The AI service returned an invalid response."}
        config.ai_analysis = result
        return {"status": "complete", "result": result}
    except (requests.RequestException, ValueError, TypeError, KeyError) as error:
        from modules.utils.log import logError

        logError(error, "AI analysis failed", config)
        return {"status": "error", "message": "The AI service is temporarily unavailable."}


def _stream_artifact(handler, path):
    artifact_path = Path(path)
    filename = artifact_path.name
    handler.emit_event(
        {"type": "artifact-start", "filename": filename, "size": artifact_path.stat().st_size}
    )
    with artifact_path.open("rb") as artifact_file:
        while chunk := artifact_file.read(ARTIFACT_CHUNK_BYTES):
            handler.emit_event(
                {
                    "type": "artifact-chunk",
                    "filename": filename,
                    "data": base64.b64encode(chunk).decode("ascii"),
                }
            )
    handler.emit_event({"type": "artifact-end", "filename": filename})


def _write_dump_archive(search_directory):
    dump_directories = list(Path(search_directory).rglob("dump_*"))
    if not dump_directories:
        return None
    archive_path = Path(search_directory) / "blackbird-dumps.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for dump_directory in dump_directories:
            for file_path in dump_directory.rglob("*"):
                if file_path.is_file():
                    archive.write(file_path, file_path.relative_to(search_directory))
    return archive_path


def run_search(handler, body, api_key):
    options = validate_options(body)
    if options["aiConsent"] and not api_key:
        raise RequestValidationError("Enable AI for this browser before requesting analysis.")
    if options["aiConsent"]:
        get_ai_base_url()
    identifiers = expand_usernames(options)

    handler.begin_event_stream()
    handler.emit_event({"type": "start", "mode": options["mode"], "searches": len(identifiers)})
    started_at = time.monotonic()

    try:
        with tempfile.TemporaryDirectory(prefix="blackbird-", dir="/tmp") as temp_name:
            work_directory = Path(temp_name)
            config = prepare_search_config(options, work_directory, handler.emit_event)
            data, metadata = _load_search_list(config, options["mode"])
            sites = data.get("sites") if isinstance(data, dict) else None
            if not isinstance(sites, list) or not sites:
                raise RuntimeError("The search site list is unavailable.")
            if metadata is not None and (not isinstance(metadata, dict) or not isinstance(metadata.get("sites"), dict)):
                metadata = {"sites": {}}
            _validate_filter(config, sites)

            if options["mode"] == "username":
                config.metadata_params = metadata or {"sites": {}}
            partial = False
            completed_searches = 0
            search_count = len(identifiers)
            for index, identifier in enumerate(identifiers, start=1):
                remaining = config.deadline_at - time.monotonic()
                if remaining <= 0:
                    partial = True
                    break
                handler.emit_event(
                    {"type": "search", "index": index, "total": search_count}
                )
                if options["mode"] == "username":
                    config.currentUser = identifier
                else:
                    config.currentEmail = identifier
                config.dateRaw = datetime.now().strftime("%m_%d_%Y_%H%M%S") + f"_{index}"
                config.ai_analysis = None
                if options["dump"] or options["csv"] or options["pdf"] or options["json"]:
                    createSaveDirectory(config)
                if options["mode"] == "username":
                    found_accounts = verifyUsername(identifier, config)
                else:
                    found_accounts = verifyEmail(identifier, config)
                current_partial = bool(getattr(config, "searchPartial", False))
                partial = partial or current_partial
                completed_searches += 1

                ai_event = None
                if options["aiConsent"] and found_accounts:
                    ai_event = _analyze_found_accounts(
                        found_accounts,
                        api_key,
                        config,
                        config.deadline_at - time.monotonic(),
                    )
                    handler.emit_event({"type": "ai", **ai_event})

                generated_files = []
                if found_accounts:
                    if options["csv"] and saveToCsv(found_accounts, config):
                        generated_files.append(Path(config.saveDirectory) / generateName(config, "csv"))
                    if options["pdf"] and saveToPdf(
                        found_accounts, options["mode"], config
                    ):
                        generated_files.append(Path(config.saveDirectory) / generateName(config, "pdf"))
                    if options["json"] and saveToJson(found_accounts, config):
                        generated_files.append(Path(config.saveDirectory) / generateName(config, "json"))

                if options["mode"] == "username":
                    config.currentUser = None
                else:
                    config.currentEmail = None
                for file_path in generated_files:
                    if file_path.is_file():
                        _stream_artifact(handler, file_path)
                if current_partial:
                    break

            if options["dump"]:
                archive_path = _write_dump_archive(config.results_root)
                if archive_path:
                    _stream_artifact(handler, archive_path)

            handler.emit_event(
                {
                    "type": "complete",
                    "partial": partial or completed_searches < search_count,
                    "completedSearches": completed_searches,
                    "totalSearches": search_count,
                    "durationSeconds": round(time.monotonic() - started_at, 1),
                }
            )
    except RequestValidationError as error:
        handler.emit_event({"type": "error", "message": str(error)})
    except (BrokenPipeError, ConnectionResetError):
        return
    except Exception as error:
        from modules.utils.log import logError

        logError(error, "Vercel search failed", config if "config" in locals() else None)
        handler.emit_event(
            {"type": "error", "message": "The search could not be completed. Try again."}
        )


def issue_ai_key(handler):
    body = parse_json_request(handler)
    require_same_origin(handler)
    if body.get("consent") is not True:
        raise RequestValidationError("Explicit consent is required to enable AI.")
    api_base = get_ai_base_url()
    try:
        response = requests.get(
            f"{api_base}/generate-key",
            headers={"User-Agent": "blackbird-vercel"},
            timeout=10,
            verify=True,
        )
        if response.status_code != 200:
            raise RuntimeError("The AI key service is unavailable")
        result = response.json()
        data = result.get("data") if isinstance(result, dict) else None
        api_key = data.get("api_key") if isinstance(data, dict) else None
        if not api_key and isinstance(data, dict) and result.get("status") == 200:
            api_key = data.get("api_key")
        if (
            not isinstance(api_key, str)
            or not api_key
            or len(api_key) > 4096
            or any(
                not (character.isascii() and (character.isalnum() or character in "._~-"))
                for character in api_key
            )
        ):
            raise RuntimeError("The AI key service returned an invalid response")
    except (requests.RequestException, ValueError, RuntimeError) as error:
        from modules.utils.log import logError

        logError(error, "AI key setup failed", SimpleNamespace(suppress_sensitive_logs=True))
        raise RequestValidationError("Could not enable AI right now. Try again later.") from error

    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header(
        "Set-Cookie",
        f"{AI_COOKIE_NAME}={api_key}; Path=/; Max-Age={AI_COOKIE_MAX_AGE}; Secure; HttpOnly; SameSite=Strict",
    )
    handler.end_headers()
    handler.wfile.write(b'{"enabled":true}')

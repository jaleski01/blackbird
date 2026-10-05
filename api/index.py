import io
import json
import logging
import queue
import threading
import traceback
from email.message import Message
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from src.vercel_app import (
    AIServiceUnavailableError,
    MAX_REQUEST_BYTES,
    RequestValidationError,
    expand_usernames,
    get_ai_base_url,
    issue_ai_key,
    parse_json_request,
    read_ai_cookie,
    require_same_origin,
    run_search,
    validate_options,
)

_STREAM_END = object()


class _StreamWriter:
    def __init__(self, events=None, disconnected=None):
        self.events = events
        self.disconnected = disconnected
        self.body = io.BytesIO()

    def write(self, data):
        if self.events is None:
            return self.body.write(data)
        while not self.disconnected.is_set():
            try:
                self.events.put(data, timeout=0.25)
                return len(data)
            except queue.Full:
                continue
        raise BrokenPipeError("The client closed the response stream.")

    def getvalue(self):
        return self.body.getvalue()


class _RequestContext:
    def __init__(self, environ):
        self.environ = environ
        self.path = environ.get("PATH_INFO", "/")
        query = environ.get("QUERY_STRING", "")
        if query:
            self.path += f"?{query}"
        self.headers = Message()
        for name, value in environ.items():
            if name.startswith("HTTP_"):
                header_name = name[5:].replace("_", "-").title()
                self.headers[header_name] = value
        if "CONTENT_TYPE" in environ:
            self.headers["Content-Type"] = environ["CONTENT_TYPE"]
        if "CONTENT_LENGTH" in environ:
            self.headers["Content-Length"] = environ["CONTENT_LENGTH"]

        content_length = environ.get("CONTENT_LENGTH", "")
        body_size = int(content_length) if content_length.isdecimal() else 0
        body_size = min(body_size, MAX_REQUEST_BYTES + 1)
        self.rfile = io.BytesIO(environ["wsgi.input"].read(body_size))
        self.wfile = _StreamWriter()
        self.status = 200
        self.response_headers = []

    def send_response(self, status, message=None):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, value))

    def end_headers(self):
        return None

    def begin_event_stream(self):
        return None

    def emit_event(self, event):
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write((encoded + "\n").encode("utf-8"))


def _operation(environ):
    query = parse_qs(environ.get("QUERY_STRING", ""))
    operation = query.get("operation", [""])[0]
    if operation in {"search", "ai-key"}:
        return operation
    path = environ.get("PATH_INFO", "").rstrip("/")
    if path.endswith("/search"):
        return "search"
    if path.endswith("/ai-key"):
        return "ai-key"
    return ""


def _log_failure(operation, error):
    frames = traceback.extract_tb(error.__traceback__)
    locations = " > ".join(
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in frames[-8:]
    ) or "unknown"
    logging.error(
        "Blackbird Vercel %s failed (%s; frames=%s)",
        operation,
        type(error).__name__,
        locations,
    )


def _respond(context, start_response, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    context.send_response(status)
    context.send_header("Content-Type", "application/json; charset=utf-8")
    context.send_header("Content-Length", str(len(body)))
    context.send_header("Cache-Control", "no-store")
    context.send_header("X-Content-Type-Options", "nosniff")
    headers = context.response_headers
    start_response(f"{status} {HTTPStatus(status).phrase}", headers)
    return [body]


def _stream_search(context, start_response, body, api_key):
    headers = [
        ("Content-Type", "application/x-ndjson; charset=utf-8"),
        ("Cache-Control", "no-store, no-transform"),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Accel-Buffering", "no"),
    ]
    start_response("200 OK", headers)
    events = queue.Queue(maxsize=8)
    disconnected = threading.Event()
    context.wfile = _StreamWriter(events, disconnected)

    def execute_search():
        try:
            run_search(context, body, api_key)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            _log_failure("search", error)
            context.emit_event(
                {"type": "error", "message": "The search could not be completed. Check the Vercel function logs."}
            )
        finally:
            while not disconnected.is_set():
                try:
                    events.put(_STREAM_END, timeout=0.25)
                    return
                except queue.Full:
                    continue

    threading.Thread(target=execute_search, name="blackbird-search", daemon=True).start()

    def response_stream():
        try:
            while True:
                event = events.get()
                if event is _STREAM_END:
                    break
                yield event
        finally:
            disconnected.set()

    return response_stream()


def app(environ, start_response):
    context = _RequestContext(environ)
    method = environ.get("REQUEST_METHOD", "GET").upper()
    operation = _operation(environ)

    if method == "GET" and operation == "ai-key":
        return _respond(
            context,
            start_response,
            200,
            {"enabled": read_ai_cookie(context) is not None},
        )
    if not operation:
        return _respond(context, start_response, 404, {"error": "API route not found."})
    if method != "POST":
        context.send_header("Allow", "GET, POST")
        return _respond(context, start_response, 405, {"error": "Use GET or POST for this API route."})

    try:
        if operation == "ai-key":
            issue_ai_key(context)
            body = context.wfile.getvalue()
            if not any(name.lower() == "content-length" for name, _ in context.response_headers):
                context.send_header("Content-Length", str(len(body)))
            start_response(
                f"{context.status} {HTTPStatus(context.status).phrase}",
                context.response_headers,
            )
            return [body]

        require_same_origin(context)
        body = parse_json_request(context)
        options = validate_options(body)
        api_key = read_ai_cookie(context)
        if options["aiConsent"] and not api_key:
            raise RequestValidationError("Enable AI for this browser before requesting analysis.")
        if options["aiConsent"]:
            get_ai_base_url()
        expand_usernames(options)
        return _stream_search(context, start_response, body, api_key)
    except RequestValidationError as error:
        return _respond(context, start_response, 400, {"error": str(error)})
    except AIServiceUnavailableError as error:
        return _respond(context, start_response, 502, {"error": str(error)})
    except (BrokenPipeError, ConnectionResetError):
        return []
    except Exception as error:
        _log_failure(operation, error)
        return _respond(
            context,
            start_response,
            500,
            {"error": "The request could not be processed. Check the Vercel function logs."},
        )

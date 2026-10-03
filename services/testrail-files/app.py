"""Streaming TestRail attachment bridge for DigitalOcean App Platform."""

import hmac
import os
import re
from urllib.parse import urlsplit

import httpx
import requests
from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)

CONNECT_TIMEOUT_SECONDS = 5.0
TRANSFER_TIMEOUT_SECONDS = 600.0
ENDPOINT_PATTERN = re.compile(
    r"^(?P<action>[a-z][a-z0-9_]*)(?:/[A-Za-z0-9][A-Za-z0-9._~-]*)*$"
)
ATTACHMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,255}")
FORWARDED_RESPONSE_HEADERS = {
    "content-disposition",
    "content-type",
    "retry-after",
}


class RequestError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _authenticate():
    expected = os.environ["TESTRAIL_BRIDGE_SECRET"]
    if len(expected) < 16:
        raise RequestError(500, "TestRail bridge configuration error")
    supplied = request.headers.get("Authorization", "")
    if not supplied.startswith("Bearer ") or not hmac.compare_digest(
        supplied[7:].encode("utf-8"), expected.encode("utf-8")
    ):
        raise RequestError(401, "Unauthorized")


def _base_url():
    value = os.environ["TESTRAIL_BASE_URL"].rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("TESTRAIL_BASE_URL must be an HTTPS origin")
    return value


def _url(endpoint):
    return f"{_base_url()}/index.php?/api/v2/{endpoint}"


def _auth():
    return (os.environ["TESTRAIL_USER_EMAIL"], os.environ["TESTRAIL_API_KEY"])


def _forwarded_headers(upstream):
    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() in FORWARDED_RESPONSE_HEADERS
    }
    headers.setdefault("Content-Type", "application/octet-stream")
    headers["Cache-Control"] = "private, no-store"
    headers["X-Content-Type-Options"] = "nosniff"
    return headers


def _upload_endpoint(endpoint):
    match = ENDPOINT_PATTERN.fullmatch(endpoint)
    if match is None:
        raise RequestError(400, "endpoint must be a relative TestRail API method")
    action = match.group("action")
    if action.startswith("delete_"):
        raise RequestError(403, "TestRail delete endpoints are blocked")
    if not action.startswith("add_attachment_to_"):
        raise RequestError(400, "This route accepts TestRail attachment uploads only")
    return endpoint


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/attachments/<attachment_id>")
def download_attachment(attachment_id):
    upstream = None
    try:
        _authenticate()
        if ATTACHMENT_ID_PATTERN.fullmatch(attachment_id) is None:
            raise RequestError(400, "Invalid attachment ID")
        upstream = requests.get(
            _url(f"get_attachment/{attachment_id}"),
            auth=_auth(),
            headers={
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "User-Agent": "Edge-TestRail-Bridge/1.0",
            },
            stream=True,
            timeout=(CONNECT_TIMEOUT_SECONDS, TRANSFER_TIMEOUT_SECONDS),
            allow_redirects=False,
        )
        response = Response(
            stream_with_context(upstream.iter_content(chunk_size=64 * 1024)),
            status=upstream.status_code,
            headers=_forwarded_headers(upstream),
        )
        response.call_on_close(upstream.close)
        return response
    except RequestError as error:
        if upstream is not None:
            upstream.close()
        return jsonify({"ok": False, "error": error.message}), error.status_code
    except requests.Timeout:
        if upstream is not None:
            upstream.close()
        return jsonify({"ok": False, "error": "TestRail request timed out"}), 504
    except requests.RequestException:
        if upstream is not None:
            upstream.close()
        return jsonify({"ok": False, "error": "TestRail request failed"}), 502
    except (KeyError, ValueError):
        if upstream is not None:
            upstream.close()
        return (
            jsonify({"ok": False, "error": "TestRail bridge configuration error"}),
            500,
        )


@app.post("/attachments/<path:endpoint>")
def upload_attachment(endpoint):
    try:
        _authenticate()
        endpoint = _upload_endpoint(endpoint)
        attachment = request.files.get("attachment")
        if attachment is None or not attachment.filename:
            raise RequestError(400, "multipart field 'attachment' is required")

        upstream = httpx.post(
            _url(endpoint),
            auth=_auth(),
            headers={
                "Accept": "application/json",
                "User-Agent": "Edge-TestRail-Bridge/1.0",
            },
            files={
                "attachment": (
                    attachment.filename,
                    attachment.stream,
                    attachment.mimetype or "application/octet-stream",
                )
            },
            timeout=httpx.Timeout(
                TRANSFER_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS
            ),
            follow_redirects=False,
        )
        return Response(
            upstream.content,
            status=upstream.status_code,
            headers=_forwarded_headers(upstream),
        )
    except RequestError as error:
        return jsonify({"ok": False, "error": error.message}), error.status_code
    except (requests.Timeout, httpx.TimeoutException):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "TestRail request timed out",
                    "outcome": "unknown",
                    "retry_safe": False,
                }
            ),
            504,
        )
    except (requests.RequestException, httpx.RequestError):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "TestRail request failed",
                    "outcome": "unknown",
                    "retry_safe": False,
                }
            ),
            502,
        )
    except (KeyError, ValueError):
        return (
            jsonify({"ok": False, "error": "TestRail bridge configuration error"}),
            500,
        )


@app.errorhandler(405)
def method_not_allowed(_error):
    return jsonify({"ok": False, "error": "Method not allowed"}), 405

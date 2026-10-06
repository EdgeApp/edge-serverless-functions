"""Authenticated TestRail API bridge that blocks destructive deletion."""

import base64
import binascii
import hmac
import json
import os
import re
from urllib.parse import urlencode, urlsplit

import requests

CONNECT_TIMEOUT_SECONDS = 2.0
READ_TIMEOUT_SECONDS = 40.0
MAX_ATTACHMENT_BYTES = 700_000
MAX_MULTIPART_BODY_BYTES = 720_000
MAX_FUNCTION_RESULT_BYTES = 980_000
ALLOWED_PAYLOAD_FIELDS = {"body", "endpoint", "operation_id", "params"}
ENDPOINT_PATTERN = re.compile(
    r"^(?P<action>[a-z][a-z0-9_]*)(?:/[A-Za-z0-9][A-Za-z0-9._~-]*)*$"
)
OPERATION_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{8,128}")


class RequestError(Exception):
    def __init__(self, status_code, message, **details):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.details = details


def _response(status_code, body, headers=None):
    response_headers = {"content-type": "application/json"}
    if headers:
        response_headers.update(headers)
    result = {
        "statusCode": status_code,
        "headers": response_headers,
        "body": json.dumps(body),
    }
    if len(json.dumps(result).encode("utf-8")) <= MAX_FUNCTION_RESULT_BYTES:
        return result
    return {
        "statusCode": 413,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(
            {
                "ok": False,
                "error": "TestRail response exceeds the Function response limit",
                "hint": "Request a smaller page with limit and offset",
            }
        ),
    }


def _binary_response(response, content):
    headers = {
        "content-type": response.headers.get(
            "Content-Type", "application/octet-stream"
        ),
        "cache-control": "no-store",
    }
    disposition = response.headers.get("Content-Disposition")
    if disposition:
        headers["content-disposition"] = disposition
    return {
        "statusCode": response.status_code,
        "headers": headers,
        "body": base64.b64encode(content).decode("ascii"),
    }


def _event_headers(event):
    return {
        str(key).lower(): str(value)
        for key, value in event.get("http", {}).get("headers", {}).items()
    }


def _authenticate(event):
    expected = os.environ["TESTRAIL_BRIDGE_SECRET"]
    if len(expected) < 16:
        raise RequestError(500, "TestRail bridge configuration error")
    supplied = _event_headers(event).get("authorization", "")
    if not supplied.startswith("Bearer ") or not hmac.compare_digest(
        supplied[7:].encode("utf-8"), expected.encode("utf-8")
    ):
        raise RequestError(401, "Unauthorized")


def _request_bytes(event):
    http = event.get("http", {})
    if http.get("method", "POST").upper() != "POST":
        raise RequestError(405, "Only POST is supported")

    raw = http.get("body", "")
    if http.get("isBase64Encoded"):
        try:
            return base64.b64decode(raw, validate=True)
        except (binascii.Error, TypeError):
            raise RequestError(400, "Request body must be valid base64")
    if not isinstance(raw, str):
        raise RequestError(400, "Request body must be text or base64")
    return raw.encode("utf-8")


def _payload(event):
    try:
        raw = _request_bytes(event).decode("utf-8")
    except UnicodeDecodeError:
        raise RequestError(400, "Request body must be valid JSON")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        raise RequestError(400, "Request body must be valid JSON")
    if not isinstance(value, dict):
        raise RequestError(400, "Request body must be a JSON object")

    unsupported = sorted(set(value) - ALLOWED_PAYLOAD_FIELDS)
    if unsupported:
        raise RequestError(400, "Unsupported request fields: " + ", ".join(unsupported))
    return value


def _endpoint(payload):
    endpoint = payload.get("endpoint")
    if not isinstance(endpoint, str):
        raise RequestError(400, "endpoint is required")
    match = ENDPOINT_PATTERN.fullmatch(endpoint)
    if match is None:
        raise RequestError(400, "endpoint must be a relative TestRail API method")

    action = match.group("action")
    if action.startswith("delete_"):
        raise RequestError(403, "TestRail delete endpoints are blocked")
    return endpoint, action


def _operation_id(payload):
    operation_id = payload.get("operation_id")
    if operation_id is None:
        return None
    if not isinstance(operation_id, str) or not OPERATION_ID_PATTERN.fullmatch(
        operation_id
    ):
        raise RequestError(400, "operation_id must be 8-128 safe characters")
    return operation_id


def _contains_destructive_is_deleted(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "is_deleted" and item not in (0, False, "0", None):
                return True
            if _contains_destructive_is_deleted(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_destructive_is_deleted(item) for item in value)
    return False


def _body(payload):
    value = payload.get("body")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RequestError(400, "body must be a JSON object")
    if _contains_destructive_is_deleted(value):
        raise RequestError(403, "Setting is_deleted to a destructive value is blocked")
    return value


def _params(payload):
    value = payload.get("params", {})
    if not isinstance(value, dict):
        raise RequestError(400, "params must be a JSON object")
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise RequestError(400, "params keys must be non-empty strings")
        items = item if isinstance(item, list) else [item]
        if not all(
            element is None or isinstance(element, (str, int, float, bool))
            for element in items
        ):
            raise RequestError(400, "params values must be scalars or scalar lists")
    return value


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


def _url(endpoint, params):
    value = f"{_base_url()}/index.php?/api/v2/{endpoint}"
    if params:
        value += "&" + urlencode(params, doseq=True)
    return value


def _upstream_body(response):
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _call_testrail(method, endpoint, params, body):
    return requests.request(
        method,
        _url(endpoint, params),
        auth=(os.environ["TESTRAIL_USER_EMAIL"], os.environ["TESTRAIL_API_KEY"]),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Edge-TestRail-Bridge/1.0",
        },
        json=body,
        timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        allow_redirects=False,
        stream=endpoint.split("/", 1)[0] == "get_attachment",
    )


def _multipart_endpoint(event):
    endpoint = _event_headers(event).get("x-testrail-endpoint")
    return _endpoint({"endpoint": endpoint})


def _call_testrail_multipart(event, endpoint):
    content_type = _event_headers(event).get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data;"):
        raise RequestError(400, "Multipart requests require a boundary")
    body = _request_bytes(event)
    if len(body) > MAX_MULTIPART_BODY_BYTES:
        raise RequestError(
            413,
            "Attachment exceeds the Function upload limit",
            max_attachment_bytes=MAX_ATTACHMENT_BYTES,
        )
    return requests.request(
        "POST",
        _url(endpoint, {}),
        auth=(os.environ["TESTRAIL_USER_EMAIL"], os.environ["TESTRAIL_API_KEY"]),
        headers={
            "Accept": "application/json",
            "Content-Type": content_type,
            "User-Agent": "Edge-TestRail-Bridge/1.0",
        },
        data=body,
        timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        allow_redirects=False,
    )


def _limited_attachment_content(response):
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > MAX_ATTACHMENT_BYTES:
                raise RequestError(
                    413,
                    "Attachment exceeds the Function download limit",
                    max_attachment_bytes=MAX_ATTACHMENT_BYTES,
                )
        except ValueError:
            pass

    content = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        content.extend(chunk)
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise RequestError(
                413,
                "Attachment exceeds the Function download limit",
                max_attachment_bytes=MAX_ATTACHMENT_BYTES,
            )
    return bytes(content)


def main(event, context):
    del context
    method = None
    operation_id = None
    try:
        _authenticate(event)
        content_type = _event_headers(event).get("content-type", "")
        if content_type.lower().startswith("multipart/form-data"):
            endpoint, action = _multipart_endpoint(event)
            method = "POST"
            response = _call_testrail_multipart(event, endpoint)
        else:
            payload = _payload(event)
            endpoint, action = _endpoint(payload)
            method = "GET" if action.startswith("get_") else "POST"
            operation_id = _operation_id(payload)
            body = _body(payload)
            if method == "GET" and body is not None:
                raise RequestError(400, "GET endpoints do not accept body")
            params = _params(payload)
            response = _call_testrail(method, endpoint, params, body)

        if action == "get_attachment" and 200 <= response.status_code < 300:
            return _binary_response(response, _limited_attachment_content(response))

        upstream_body = _upstream_body(response)
        receipt = {
            "ok": 200 <= response.status_code < 300,
            "endpoint": endpoint,
            "method": method,
            "operation_id": operation_id,
            "upstream_status": response.status_code,
            "upstream_body": upstream_body,
        }

        if receipt["ok"]:
            receipt["outcome"] = "confirmed"
            receipt["retry_safe"] = False
            return _response(response.status_code, receipt)

        receipt["outcome"] = "rejected"
        receipt["retry_safe"] = response.status_code < 500 or method == "GET"
        if method == "POST" and response.status_code >= 500:
            receipt["outcome"] = "unknown"
        headers = {}
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            headers["retry-after"] = retry_after
        return _response(response.status_code, receipt, headers)
    except RequestError as error:
        body = {"ok": False, "error": error.message}
        body.update(error.details)
        return _response(error.status_code, body)
    except requests.Timeout:
        body = {
            "ok": False,
            "error": "TestRail request timed out",
            "outcome": "unknown" if method == "POST" else "unconfirmed",
            "retry_safe": method == "GET",
        }
        if operation_id is not None:
            body["operation_id"] = operation_id
        return _response(504, body)
    except requests.RequestException:
        body = {
            "ok": False,
            "error": "TestRail request failed",
            "outcome": "unknown" if method == "POST" else "unconfirmed",
            "retry_safe": method == "GET",
        }
        if operation_id is not None:
            body["operation_id"] = operation_id
        return _response(502, body)
    except (KeyError, ValueError):
        return _response(500, {"ok": False, "error": "TestRail bridge configuration error"})

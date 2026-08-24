"""Authenticated bridge for creating new Intercom article drafts."""

import base64
import datetime
import hashlib
import hmac
import json
import os
import re

import requests

API_URL = "https://api.intercom.io"
API_VERSION = "2.16"
TIMEOUT_SECONDS = 10


class RequestError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _headers():
    return {
        "Authorization": f"Bearer {os.environ['INTERCOM_ARTICLE_ACCESS_TOKEN']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Intercom-Version": API_VERSION,
    }


def _request(method, path, payload=None):
    response = requests.request(
        method,
        f"{API_URL}{path}",
        headers=_headers(),
        json=payload,
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def _event_headers(event):
    return {
        str(key).lower(): str(value)
        for key, value in event.get("http", {}).get("headers", {}).items()
    }


def _authenticate(event):
    expected = os.environ["INTERCOM_DRAFT_BRIDGE_SECRET"]
    if len(expected) < 16:
        raise RequestError(500, "Draft bridge configuration error")
    supplied = _event_headers(event).get("authorization", "")
    if not supplied.startswith("Bearer ") or not hmac.compare_digest(
        supplied[7:], expected
    ):
        raise RequestError(401, "Unauthorized")


def _payload(event):
    http = event.get("http", {})
    if http.get("method", "POST").upper() != "POST":
        raise RequestError(405, "Only POST is supported")
    raw = http.get("body", "")
    if http.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        raise RequestError(400, "Request body must be valid JSON")
    if not isinstance(value, dict):
        raise RequestError(400, "Request body must be a JSON object")
    return value


def _article_fields(payload):
    operation_id = payload.get("operation_id")
    if not isinstance(operation_id, str) or not re.fullmatch(
        r"[A-Za-z0-9._:-]{8,128}", operation_id
    ):
        raise RequestError(400, "operation_id must be 8-128 safe characters")

    title = payload.get("title")
    body_markdown = payload.get("body_markdown")
    if not isinstance(title, str) or not title.strip():
        raise RequestError(400, "title is required")
    if not isinstance(body_markdown, str) or not body_markdown.strip():
        raise RequestError(400, "body_markdown is required")

    fields = {
        "title": title.strip(),
        "body_markdown": body_markdown,
        "author_id": int(os.environ["INTERCOM_ARTICLE_AUTHOR_ID"]),
    }
    description = payload.get("description")
    if description is not None:
        if not isinstance(description, str):
            raise RequestError(400, "description must be a string")
        fields["description"] = description

    if payload.get("parent_id") is not None or payload.get("parent_type") is not None:
        raise RequestError(400, "parent placement is not supported by create-only v0")
    return fields


def _content_hash(payload):
    content = {
        "body_markdown": payload["body_markdown"],
        "description": payload.get("description"),
        "title": payload["title"].strip(),
    }
    encoded = json.dumps(
        content, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rich_openings(markdown):
    return {
        line.strip()
        for line in markdown.splitlines()
        if line.strip().startswith(":::") and line.strip() != ":::"
    }


def _verify_created(article, submitted_markdown):
    if not str(article.get("id", "")).isdigit():
        raise RequestError(502, "Intercom did not return a valid article ID")
    if article.get("state") != "draft":
        raise RequestError(502, "Intercom did not confirm draft state")

    expected_rich = _rich_openings(submitted_markdown)
    if expected_rich:
        returned_markdown = article.get("body_markdown")
        if not isinstance(returned_markdown, str) or not expected_rich.issubset(
            _rich_openings(returned_markdown)
        ):
            raise RequestError(502, "Intercom did not preserve requested rich formatting")


def _create(payload):
    fields = _article_fields(payload)
    fields["state"] = "draft"
    article = _request("POST", "/articles", fields)
    _verify_created(article, fields["body_markdown"])
    return "created", article


def main(event, context):
    try:
        _authenticate(event)
        payload = _payload(event)
        operation = payload.get("operation")
        if operation == "create":
            result, article = _create(payload)
        else:
            raise RequestError(400, "operation must be create")

        completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        return _response(
            200,
            {
                "ok": True,
                "operation_id": payload["operation_id"],
                "operation": "create",
                "result": result,
                "article_id": str(article["id"]),
                "title": payload["title"].strip(),
                "before_state": None,
                "after_state": "draft",
                "state": "draft",
                "completed_at": completed_at,
                "submitted_content_hash": _content_hash(payload),
            },
        )
    except RequestError as error:
        return _response(error.status_code, {"ok": False, "error": error.message})
    except requests.RequestException as error:
        status = error.response.status_code if error.response is not None else 502
        return _response(status, {"ok": False, "error": "Intercom request failed"})
    except (KeyError, TypeError, ValueError, RuntimeError):
        return _response(500, {"ok": False, "error": "Draft bridge configuration error"})

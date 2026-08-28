"""Authenticated bridge for creating and updating Intercom article drafts."""

import base64
import binascii
import datetime
import hashlib
import hmac
import json
import os
import re
import time

import requests

API_URL = "https://api.intercom.io"
API_VERSION = "2.16"
REQUEST_BUDGET_SECONDS = 30.0
CONNECT_TIMEOUT_SECONDS = 2.0
READ_TIMEOUT_SECONDS = 5.0
MIN_REQUEST_WINDOW_SECONDS = 0.75
SCHEDULE_FIELDS = ("scheduled_publish_at", "scheduled_unpublish_at")
DETERMINISTIC_MUTATION_STATUSES = {400, 401, 403, 404, 405, 409, 422}
ALLOWED_PAYLOAD_FIELDS = {
    "article_id",
    "body_markdown",
    "description",
    "operation",
    "operation_id",
    "replace_staged_draft_fingerprint",
    "title",
}


class RequestError(Exception):
    def __init__(self, status_code, message, mutation_attempted=False, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.mutation_attempted = mutation_attempted
        self.details = details or {}


class ReconciliationRequired(Exception):
    def __init__(self, operation_id, article_id=None):
        super().__init__("Intercom mutation requires manual reconciliation")
        self.operation_id = operation_id
        self.article_id = article_id


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _headers():
    return {
        "Authorization": f"Bearer {os.environ['INTERCOM_ACCESS_TOKEN']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Intercom-Version": API_VERSION,
    }


def _request(method, path, deadline, payload=None):
    remaining = deadline - time.monotonic()
    if remaining < MIN_REQUEST_WINDOW_SECONDS:
        raise requests.Timeout("Intercom request deadline exceeded")

    connect_timeout = min(CONNECT_TIMEOUT_SECONDS, remaining / 3)
    read_timeout = min(READ_TIMEOUT_SECONDS, remaining - connect_timeout)
    response = requests.request(
        method,
        f"{API_URL}{path}",
        headers=_headers(),
        json=payload,
        timeout=(connect_timeout, read_timeout),
    )
    if time.monotonic() > deadline:
        raise requests.Timeout("Intercom request deadline exceeded")
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise RequestError(502, "Intercom returned an invalid response")
    return value


def _mutation_request(method, path, payload, deadline, operation_id, article_id=None):
    try:
        return _request(method, path, deadline, payload)
    except RequestError:
        # A malformed success response can arrive after Intercom committed the write.
        raise ReconciliationRequired(operation_id, article_id)
    except requests.RequestException as error:
        status = error.response.status_code if error.response is not None else None
        if status in DETERMINISTIC_MUTATION_STATUSES:
            raise RequestError(
                status,
                "Intercom rejected the draft mutation",
                mutation_attempted=True,
            )
        # Timeouts, connection loss, 5xx, 408, and unclassified responses do not
        # prove that the write failed. Re-raise only explicitly deterministic
        # rejection statuses; every other mutation outcome requires reconciliation.
        raise ReconciliationRequired(operation_id, article_id)


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
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except (binascii.Error, TypeError, UnicodeDecodeError):
            raise RequestError(400, "Request body must be valid base64-encoded JSON")
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


def _operation_id(payload):
    operation_id = payload.get("operation_id")
    if not isinstance(operation_id, str) or not re.fullmatch(
        r"[A-Za-z0-9._:-]{8,128}", operation_id
    ):
        raise RequestError(400, "operation_id must be 8-128 safe characters")
    return operation_id


def _article_id(payload, required):
    value = payload.get("article_id")
    if value is None and not required:
        return None
    article_id = _positive_ascii_id(value)
    if article_id is None:
        raise RequestError(400, "article_id must be a numeric Intercom article ID")
    return article_id


def _positive_ascii_id(value):
    if isinstance(value, bool):
        return None
    text = str(value or "")
    if not re.fullmatch(r"[1-9][0-9]*", text):
        return None
    return text


def _author_id():
    value = os.environ["INTERCOM_ARTICLE_AUTHOR_ID"]
    author_id = _positive_ascii_id(value)
    if author_id is None:
        raise ValueError("INTERCOM_ARTICLE_AUTHOR_ID must be a positive integer")
    return int(author_id)


def _article_fields(payload):
    _operation_id(payload)
    title = payload.get("title")
    body_markdown = payload.get("body_markdown")
    if not isinstance(title, str) or not title.strip():
        raise RequestError(400, "title is required")
    if not isinstance(body_markdown, str) or not body_markdown.strip():
        raise RequestError(400, "body_markdown is required")

    fields = {
        "title": title.strip(),
        "body_markdown": body_markdown,
        "author_id": _author_id(),
    }
    description = payload.get("description")
    if not isinstance(description, str) or not description.strip():
        raise RequestError(400, "description is required")
    fields["description"] = description.strip()
    return fields


def _content_hash(payload):
    content = {
        "body_markdown": payload["body_markdown"],
        "description": payload["description"].strip(),
        "title": payload["title"].strip(),
    }
    try:
        encoded = json.dumps(
            content, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except UnicodeEncodeError:
        raise RequestError(400, "Article content must be valid UTF-8 text")
    return hashlib.sha256(encoded).hexdigest()


def _replacement_fingerprint(payload):
    value = payload.get("replace_staged_draft_fingerprint")
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RequestError(
            400,
            "replace_staged_draft_fingerprint must be a lowercase SHA-256",
        )
    return value


def _rich_openings(markdown):
    return {
        line.strip()
        for line in markdown.splitlines()
        if line.strip().startswith(":::") and line.strip() != ":::"
    }


def _verify_identity(article, expected_id=None):
    article_id = _positive_ascii_id(article.get("id"))
    if article_id is None:
        raise RequestError(502, "Intercom did not return a valid article ID")
    if expected_id is not None and article_id != expected_id:
        raise RequestError(502, "Intercom returned a different article ID")
    return article_id


def _verify_content(article, fields):
    if _positive_ascii_id(article.get("author_id")) != str(fields["author_id"]):
        raise RequestError(502, "Intercom did not preserve the submitted author")
    if article.get("title") != fields["title"]:
        raise RequestError(502, "Intercom did not preserve the submitted title")
    if article.get("description") != fields["description"]:
        raise RequestError(502, "Intercom did not preserve the submitted description")
    returned_markdown = article.get("body_markdown")
    if not isinstance(returned_markdown, str) or not returned_markdown.strip():
        raise RequestError(502, "Intercom returned an empty Markdown draft")

    expected_rich = _rich_openings(fields["body_markdown"])
    if expected_rich and not expected_rich.issubset(
        _rich_openings(returned_markdown)
    ):
        raise RequestError(502, "Intercom did not preserve requested rich formatting")


def _verify_unscheduled(article, status_code=502):
    missing = [field for field in SCHEDULE_FIELDS if field not in article]
    if missing:
        raise RequestError(502, "Intercom did not report article schedule state")
    scheduled = [field for field in SCHEDULE_FIELDS if article.get(field) is not None]
    if scheduled:
        if status_code == 409:
            raise RequestError(
                409,
                "Scheduled articles require manual reconciliation before update",
            )
        raise RequestError(502, "Intercom did not confirm an unscheduled draft")


def _required_timestamp(article, field):
    value = article.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RequestError(502, f"Intercom did not report a valid {field}")
    return value


def _published_draft_status(article):
    if "has_unpublished_changes" not in article or "draft_updated_at" not in article:
        raise RequestError(502, "Intercom did not report the published article's draft state")

    pending = article["has_unpublished_changes"]
    draft_updated_at = article["draft_updated_at"]
    if pending is False and draft_updated_at is None:
        return False
    if pending is True:
        if (
            isinstance(draft_updated_at, bool)
            or not isinstance(draft_updated_at, int)
            or draft_updated_at <= 0
        ):
            raise RequestError(502, "Intercom reported an inconsistent staged-draft state")
        return True
    raise RequestError(502, "Intercom reported an inconsistent staged-draft state")


def _verify_unpublished_draft_status(article):
    if "has_unpublished_changes" not in article or "draft_updated_at" not in article:
        raise RequestError(502, "Intercom did not report the unpublished article's draft state")
    if article["has_unpublished_changes"] is not False or article["draft_updated_at"] is not None:
        raise RequestError(502, "Intercom reported an inconsistent unpublished-draft state")


def _verify_draft(article, fields, expected_id=None):
    article_id = _verify_identity(article, expected_id)
    if article.get("state") != "draft":
        raise RequestError(502, "Intercom did not confirm unpublished draft state")
    _verify_unpublished_draft_status(article)
    _verify_unscheduled(article)
    _verify_content(article, fields)
    return article_id


def _verify_staged(article, fields, expected_id):
    article_id = _verify_identity(article, expected_id)
    if (
        article.get("state") != "published"
        or _published_draft_status(article) is not True
    ):
        raise RequestError(502, "Intercom did not confirm a staged published-article draft")
    _verify_unscheduled(article)
    _verify_content(article, fields)
    return article_id


def _staged_draft_fingerprint(article, expected_id):
    """Fingerprint the exact remote staged draft returned by Intercom."""
    article_id = _verify_identity(article, expected_id)
    if (
        article.get("state") != "published"
        or _published_draft_status(article) is not True
    ):
        raise RequestError(502, "Intercom did not confirm an existing staged revision")
    _verify_unscheduled(article)

    author_id = _positive_ascii_id(article.get("author_id"))
    title = article.get("title")
    description = article.get("description")
    body_markdown = article.get("body_markdown")
    draft_updated_at = _required_timestamp(article, "draft_updated_at")
    if (
        author_id is None
        or not isinstance(title, str)
        or (description is not None and not isinstance(description, str))
        or not isinstance(body_markdown, str)
    ):
        raise RequestError(502, "Intercom staged-draft readback was incomplete")

    encoded = json.dumps(
        {
            "article_id": article_id,
            "author_id": author_id,
            "body_markdown": body_markdown,
            "description": description,
            "draft_updated_at": draft_updated_at,
            "title": title,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), draft_updated_at


def _staged_draft_conflict(fingerprint, draft_updated_at):
    return RequestError(
        409,
        "Published article already has staged changes; explicit replacement confirmation is required",
        details={
            "conflict": "existing_staged_draft",
            "existing_staged_draft_fingerprint": fingerprint,
            "existing_draft_updated_at": draft_updated_at,
        },
    )


def _live_snapshot(article, include_version=False):
    if article.get("state") != "published":
        raise RequestError(502, "Intercom did not confirm live published state")
    _verify_unscheduled(article)
    author_id = _positive_ascii_id(article.get("author_id"))
    if author_id is None:
        raise RequestError(502, "Intercom live-article author readback was incomplete")
    title = article.get("title")
    body = article.get("body")
    body_markdown = article.get("body_markdown")
    if not isinstance(title, str) or not any(
        isinstance(value, str) for value in (body, body_markdown)
    ):
        raise RequestError(502, "Intercom live-article readback was incomplete")
    snapshot = {
        "title": title,
        "description": article.get("description"),
        "body": body,
        "body_markdown": body_markdown,
        "state": "published",
        "author_id": author_id,
        "scheduled_publish_at": article.get("scheduled_publish_at"),
        "scheduled_unpublish_at": article.get("scheduled_unpublish_at"),
    }
    if include_version:
        snapshot["updated_at"] = _required_timestamp(article, "updated_at")
    return snapshot


def _create(payload, deadline):
    if "article_id" in payload:
        raise RequestError(400, "article_id is not allowed for create")
    if "replace_staged_draft_fingerprint" in payload:
        raise RequestError(
            400, "replace_staged_draft_fingerprint is not allowed for create"
        )
    fields = _article_fields(payload)
    fields["state"] = "draft"
    operation_id = _operation_id(payload)
    article = _mutation_request("POST", "/articles", fields, deadline, operation_id)
    article_id = _positive_ascii_id(article.get("id"))
    try:
        _verify_draft(article, fields)
    except RequestError:
        raise ReconciliationRequired(operation_id, article_id)
    return {
        "result": "created",
        "article": article,
        "before_state": None,
        "after_state": "draft",
        "state": "draft",
        "draft_mode": "new",
    }


def _update(payload, deadline):
    article_id = _article_id(payload, required=True)
    operation_id = _operation_id(payload)
    fields = _article_fields(payload)
    replacement_fingerprint = _replacement_fingerprint(payload)
    existing = _request("GET", f"/articles/{article_id}", deadline)
    _verify_identity(existing, article_id)
    _verify_unscheduled(existing, status_code=409)
    before_state = existing.get("state")

    if before_state == "draft":
        if replacement_fingerprint is not None:
            raise RequestError(
                409,
                "The approved staged revision no longer exists",
                details={"conflict": "staged_draft_missing"},
            )
        _verify_unpublished_draft_status(existing)
        # Omitting state preserves an unpublished draft. This workflow accepts the
        # narrow race where a human could publish between this GET and PUT; the
        # mandatory readback detects the resulting state drift.
        _mutation_request(
            "PUT",
            f"/articles/{article_id}",
            fields,
            deadline,
            operation_id,
            article_id,
        )
        try:
            article = _request("GET", f"/articles/{article_id}", deadline)
            _verify_draft(article, fields, article_id)
        except (RequestError, requests.RequestException):
            raise ReconciliationRequired(operation_id, article_id)
        return {
            "result": "updated_draft",
            "article": article,
            "before_state": "draft",
            "after_state": "draft",
            "state": "draft",
            "draft_mode": "unpublished",
        }

    if before_state == "published":
        pending = _published_draft_status(existing)
        live_before = _live_snapshot(existing)
        prewrite_before = _live_snapshot(existing, include_version=True)
        approved_fingerprint = None

        if pending:
            staged_before = _request(
                "GET", f"/articles/{article_id}/draft", deadline
            )
            approved_fingerprint, draft_updated_at = _staged_draft_fingerprint(
                staged_before, article_id
            )
            if replacement_fingerprint != approved_fingerprint:
                raise _staged_draft_conflict(
                    approved_fingerprint, draft_updated_at
                )
        elif replacement_fingerprint is not None:
            raise RequestError(
                409,
                "The approved staged revision no longer exists",
                details={"conflict": "staged_draft_missing"},
            )

        # Narrow the accepted human-edit race with an immediate, version-bound
        # read just before the staged-draft write. Intercom offers no conditional
        # write token, so a final sub-request race remains an explicit workflow
        # tradeoff rather than something this bridge can eliminate atomically.
        prewrite = _request("GET", f"/articles/{article_id}", deadline)
        _verify_identity(prewrite, article_id)
        _verify_unscheduled(prewrite, status_code=409)
        if prewrite.get("state") != "published":
            raise RequestError(409, "Article state changed during update preflight")
        prewrite_pending = _published_draft_status(prewrite)
        if pending:
            if not prewrite_pending:
                raise RequestError(
                    409,
                    "The approved staged revision no longer exists",
                    details={"conflict": "staged_draft_missing"},
                )
            staged_prewrite = _request(
                "GET", f"/articles/{article_id}/draft", deadline
            )
            current_fingerprint, draft_updated_at = _staged_draft_fingerprint(
                staged_prewrite, article_id
            )
            if current_fingerprint != approved_fingerprint:
                raise _staged_draft_conflict(
                    current_fingerprint, draft_updated_at
                )
        elif prewrite_pending:
            staged_prewrite = _request(
                "GET", f"/articles/{article_id}/draft", deadline
            )
            current_fingerprint, draft_updated_at = _staged_draft_fingerprint(
                staged_prewrite, article_id
            )
            raise _staged_draft_conflict(current_fingerprint, draft_updated_at)
        if _live_snapshot(prewrite, include_version=True) != prewrite_before:
            raise RequestError(409, "Published article changed during update preflight")

        _mutation_request(
            "PUT",
            f"/articles/{article_id}/draft",
            fields,
            deadline,
            operation_id,
            article_id,
        )
        try:
            article = _request("GET", f"/articles/{article_id}/draft", deadline)
            _verify_staged(article, fields, article_id)
            staged_draft_updated_at = article["draft_updated_at"]
            staged_draft_fingerprint, _ = _staged_draft_fingerprint(
                article, article_id
            )
            live_after = _request("GET", f"/articles/{article_id}", deadline)
            _verify_identity(live_after, article_id)
            if _published_draft_status(live_after) is not True:
                raise RequestError(
                    502, "Intercom live article did not confirm the staged revision"
                )
            if live_after["draft_updated_at"] != staged_draft_updated_at:
                raise RequestError(
                    502, "Intercom staged revision changed during final readback"
                )
            if _live_snapshot(live_after) != live_before:
                raise RequestError(
                    502, "Intercom live article changed while staging its draft"
                )
        except (RequestError, requests.RequestException):
            raise ReconciliationRequired(operation_id, article_id)
        return {
            "result": "staged_draft",
            "article": article,
            "before_state": "published",
            "after_state": "published_with_draft",
            "state": "published",
            "draft_mode": "published_revision",
            "has_unpublished_changes": True,
            "staged_draft_fingerprint": staged_draft_fingerprint,
            "draft_updated_at": staged_draft_updated_at,
        }

    raise RequestError(409, "Article must be an unpublished draft or a published article")


def main(event, context):
    deadline = time.monotonic() + REQUEST_BUDGET_SECONDS
    payload = {}
    try:
        _authenticate(event)
        payload = _payload(event)
        operation = payload.get("operation")
        if operation not in ("create", "update"):
            raise RequestError(400, "operation must be create or update")

        # Complete every validation step that can fail deterministically before
        # the first upstream write. In particular, computing the UTF-8 content
        # hash here guarantees a committed mutation can always return the
        # operation-bound non-retry receipt or reconciliation response.
        if operation == "create":
            if "article_id" in payload:
                raise RequestError(400, "article_id is not allowed for create")
        else:
            _article_id(payload, required=True)
        _article_fields(payload)
        submitted_content_hash = _content_hash(payload)

        if operation == "create":
            outcome = _create(payload, deadline)
        else:
            outcome = _update(payload, deadline)

        article = outcome.pop("article")
        completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        receipt = {
            "ok": True,
            "operation_id": payload["operation_id"],
            "operation": operation,
            "article_id": str(article["id"]),
            "title": payload["title"].strip(),
            "description": payload["description"].strip(),
            "completed_at": completed_at,
            "submitted_content_hash": submitted_content_hash,
            **outcome,
        }
        return _response(200, receipt)
    except ReconciliationRequired as error:
        body = {
            "ok": False,
            "error": "Intercom mutation requires manual reconciliation before retry",
            "operation_id": error.operation_id,
            "reconciliation_required": True,
            "retry_safe": False,
        }
        if error.article_id is not None:
            body["article_id"] = error.article_id
        return _response(502, body)
    except RequestError as error:
        body = {"ok": False, "error": error.message}
        body.update(error.details)
        operation_id = payload.get("operation_id")
        if isinstance(operation_id, str) and re.fullmatch(
            r"[A-Za-z0-9._:-]{8,128}", operation_id
        ):
            body.update(
                {
                    "outcome": "rejected",
                    "operation_id": operation_id,
                    "mutation_attempted": error.mutation_attempted,
                    "reconciliation_required": False,
                    "retry_safe": True,
                }
            )
            article_id = _positive_ascii_id(payload.get("article_id"))
            if article_id is not None:
                body["article_id"] = article_id
        return _response(error.status_code, body)
    except requests.RequestException as error:
        status = error.response.status_code if error.response is not None else 502
        return _response(status, {"ok": False, "error": "Intercom request failed"})
    except (KeyError, TypeError, ValueError, RuntimeError):
        return _response(500, {"ok": False, "error": "Draft bridge configuration error"})

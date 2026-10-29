"""Intercom webhook router.

Single entry point for all Intercom webhook topics. Verifies the
HMAC-SHA1 signature once, then dispatches to the appropriate handler
based on the ``topic`` field in the payload.
"""

import base64
import hashlib
import hmac
import json
import logging
import os

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

LEAD_TOPICS = {
    "contact.lead.created",
    "contact.lead.added_email",
    "contact.email.updated",
}

CALL_TOPICS = {
    "call.started",
}

SWITCH_TOPICS = {
    "call.ended",
}


def _get_raw_body(event):
    """Extract the raw HTTP body from a DO Functions raw web event."""
    if not isinstance(event, dict):
        return ""
    http = event.get("http", {})
    if not isinstance(http, dict):
        return ""
    body = http.get("body", "")
    if not isinstance(body, str):
        return ""
    if http.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
    return body


def _get_header(headers, name):
    """Read an HTTP header case-insensitively."""
    if not isinstance(headers, dict):
        return ""
    wanted = name.casefold()
    for key, value in headers.items():
        if isinstance(key, str) and key.casefold() == wanted:
            return value if isinstance(value, str) else ""
    return ""


def _verify_signature(raw_body, signature_header, secret):
    """Verify Intercom's HMAC-SHA1 webhook signature (X-Hub-Signature: sha1=...)."""
    if not raw_body or not signature_header or not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()
    provided = signature_header.removeprefix("sha1=")
    return hmac.compare_digest(expected, provided)


def main(event, context):
    http = event.get("http", {}) if isinstance(event, dict) else {}
    http = http if isinstance(http, dict) else {}
    method_value = http.get("method", "")
    method = method_value.upper() if isinstance(method_value, str) else ""
    if method == "HEAD":
        return {"statusCode": 200, "body": ""}
    if method != "POST":
        return {"statusCode": 405, "body": "Method not allowed"}

    raw_body = _get_raw_body(event)

    headers = http.get("headers", {})
    secret = os.environ.get("WEBHOOK_SECRET", "")

    signature = _get_header(headers, "x-hub-signature")
    if not _verify_signature(raw_body, signature, secret):
        logger.warning("Invalid or missing HMAC signature")
        return {"statusCode": 401, "body": "Invalid signature"}

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Malformed JSON body")
        return {"statusCode": 400, "body": "Malformed body"}
    if not isinstance(payload, dict):
        logger.warning("Webhook JSON body is not an object")
        return {"statusCode": 400, "body": "Malformed body"}

    topic = payload.get("topic", "")
    logger.info("Received webhook topic: %s", topic)

    if topic in LEAD_TOPICS:
        from lead_to_user.handler import handle as handle_lead_to_user
        return handle_lead_to_user(payload)

    if topic in CALL_TOPICS:
        from call_timezone.handler import handle as handle_call_timezone
        return handle_call_timezone(payload)

    if topic in SWITCH_TOPICS:
        from switch_promotion.handler import handle as handle_switch_promotion
        return handle_switch_promotion(payload)

    logger.info("Unhandled topic: %s", topic)
    return {"statusCode": 200, "body": "Ignored"}

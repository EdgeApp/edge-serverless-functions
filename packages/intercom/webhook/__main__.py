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


def _get_raw_body(event):
    """Extract the raw HTTP body from a DO Functions raw web event."""
    http = event.get("http", {})
    body = http.get("body", "")
    if http.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return body


def _verify_signature(raw_body, signature_header, secret):
    """Verify Intercom's HMAC-SHA1 webhook signature (X-Hub-Signature: sha1=...)."""
    if not signature_header:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()
    provided = signature_header.removeprefix("sha1=")
    return hmac.compare_digest(expected, provided)


def main(event, context):
    method = event.get("http", {}).get("method", "").upper()
    if method == "HEAD":
        return {"statusCode": 200, "body": ""}

    raw_body = _get_raw_body(event)

    headers = event.get("http", {}).get("headers", {})
    secret = os.environ.get("WEBHOOK_SECRET", "")

    if not _verify_signature(raw_body, headers.get("x-hub-signature"), secret):
        logger.warning("Invalid or missing HMAC signature")
        return {"statusCode": 401, "body": "Invalid signature"}

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Malformed JSON body")
        return {"statusCode": 400, "body": "Malformed body"}

    topic = payload.get("topic", "")
    logger.info("Received webhook topic: %s", topic)

    if topic in LEAD_TOPICS:
        from lead_to_user.handler import handle as handle_lead_to_user
        return handle_lead_to_user(payload)

    if topic in CALL_TOPICS:
        from call_timezone.handler import handle as handle_call_timezone
        return handle_call_timezone(payload)

    logger.info("Unhandled topic: %s", topic)
    return {"statusCode": 200, "body": "Ignored"}

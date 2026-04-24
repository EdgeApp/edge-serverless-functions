"""Webhook handler for Intercom call.started events.

Infers the caller's timezone from their phone number, then:
1. Creates an internal note on the conversation/ticket with timezone details
2. Sets the `inferred_timezone` custom attribute on the contact
"""

import base64
import hashlib
import hmac
import json
import logging
import os

from timezone import infer_timezone
from intercom_client import create_conversation_note, update_contact_attributes

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def _get_raw_body(event):
    http = event.get("http", {})
    body = http.get("body", "")
    if http.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return body


def _verify_signature(raw_body, signature_header, secret):
    if not signature_header:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()
    provided = signature_header.removeprefix("sha1=")
    return hmac.compare_digest(expected, provided)


def _build_note_body(info: dict, phone: str) -> str:
    """Format the timezone inference as a rich internal note."""
    lines = [
        f"<b>🕐 {info['timezone']}</b> ({info['utc_offset'] or '?'})",
    ]

    if info["local_time"]:
        lines.append(f"Local time: {info['local_time']}")

    parts = []
    if info["location"]:
        parts.append(info["location"])
    if info["country"]:
        parts.append(info["country"])
    if info["area_code"]:
        parts.append(f"area code {info['area_code']}")
    if parts:
        lines.append(" · ".join(parts))

    confidence_label = "High" if info["confidence"] == "high" else "Approximate"
    lines.append(f"Confidence: {confidence_label}")

    return "<br>".join(lines)


def main(event, context):
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
    if topic != "call.started":
        logger.info("Ignoring topic: %s", topic)
        return {"statusCode": 200, "body": "Ignored"}

    item = payload.get("data", {}).get("item", {})

    if item.get("direction") != "inbound":
        logger.info("Skipping non-inbound call (direction=%s)", item.get("direction"))
        return {"statusCode": 200, "body": "Skipped"}

    phone = item.get("phone")
    contact_id = item.get("contact_id")
    conversation_id = item.get("conversation_id")

    if not phone or not contact_id:
        logger.warning("Missing phone or contact_id in payload")
        return {"statusCode": 200, "body": "Skipped — missing data"}

    info = infer_timezone(phone)
    if not info:
        logger.warning("Could not infer timezone for %s", phone)
        return {"statusCode": 200, "body": "No timezone inferred"}

    logger.info(
        "Inferred %s for %s (confidence=%s)", info["timezone"], phone, info["confidence"]
    )

    if conversation_id:
        try:
            note_body = _build_note_body(info, phone)
            create_conversation_note(conversation_id, note_body)
        except Exception:
            logger.exception("Failed to create note on conversation %s", conversation_id)
    else:
        logger.warning("No conversation_id in payload, skipping note")

    try:
        update_contact_attributes(contact_id, {"inferred_timezone": info["timezone"]})
    except Exception:
        logger.exception("Failed to update attributes for contact %s", contact_id)

    return {"statusCode": 200, "body": "OK"}

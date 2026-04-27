"""Inbound call timezone inference handler.

Infers the caller's timezone from their phone number, then:
1. Creates an internal note on the conversation with timezone details
2. Sets the ``inferred_timezone`` custom attribute on the contact

Called by the webhook router for the call.started topic.
"""

import logging

from call_timezone.timezone import infer_timezone
from intercom_client import create_conversation_note, update_contact_attributes

logger = logging.getLogger(__name__)


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


def handle(payload):
    """Process a call.started webhook payload. Expects a pre-verified, parsed dict."""
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

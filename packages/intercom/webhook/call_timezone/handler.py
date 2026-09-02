"""Inbound call timezone inference handler.

Infers the caller's timezone from their phone number, then:
1. Creates an internal note on the conversation with timezone details
2. Sets the ``inferred_timezone`` custom attribute on the contact

Called by the webhook router for the call.started topic.
"""

import logging
import re

from call_timezone.timezone import infer_timezone
from intercom_client import (
    conversation_contains_note_marker,
    create_conversation_note,
    get_conversation,
    update_contact_attributes,
)

logger = logging.getLogger(__name__)

CALL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


def _call_id(item: dict) -> str | None:
    value = item.get("id")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    call_id = str(value).strip()
    return call_id if CALL_ID_PATTERN.fullmatch(call_id) else None


def _receipt_marker(call_id: str) -> str:
    return f"[edge-call-location call_id={call_id}]"


def _receipt_exists(conversation_id: str, marker: str) -> bool:
    return conversation_contains_note_marker(
        get_conversation(conversation_id), marker
    )


def _build_note_body(info: dict, phone: str, call_id: str) -> str:
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
    lines.append(_receipt_marker(call_id))

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
    call_id = _call_id(item)

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

    note_needs_retry = False
    if conversation_id and call_id:
        marker = _receipt_marker(call_id)
        should_create_note = True

        # Intercom numbers the first delivery as 1. Missing or unexpected values
        # take the safe receipt-check path instead of risking a duplicate note.
        if payload.get("delivery_attempts") != 1:
            try:
                should_create_note = not _receipt_exists(conversation_id, marker)
            except Exception:
                logger.exception(
                    "Failed to check call-note receipt on conversation %s",
                    conversation_id,
                )
                should_create_note = False
                note_needs_retry = True

        if should_create_note:
            try:
                create_conversation_note(
                    conversation_id, _build_note_body(info, phone, call_id)
                )
            except Exception:
                logger.exception(
                    "Failed to create note on conversation %s; checking receipt",
                    conversation_id,
                )
                try:
                    note_needs_retry = not _receipt_exists(conversation_id, marker)
                except Exception:
                    logger.exception(
                        "Failed to reconcile call-note receipt on conversation %s",
                        conversation_id,
                    )
                    note_needs_retry = True
    elif not conversation_id:
        logger.warning("No conversation_id in payload, skipping note")
    else:
        logger.warning("Missing or invalid call ID, skipping non-idempotent note")

    try:
        update_contact_attributes(contact_id, {"inferred_timezone": info["timezone"]})
    except Exception:
        logger.exception("Failed to update attributes for contact %s", contact_id)

    if note_needs_retry:
        return {"statusCode": 500, "body": "Call note requires retry"}
    return {"statusCode": 200, "body": "OK"}

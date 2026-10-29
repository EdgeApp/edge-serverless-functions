"""Promote Intercom Phone callers before their Switch Messenger session starts.

This action receives only ``call.ended`` webhooks.  For an inbound phone call,
it fetches the authoritative Call and Conversation objects, proves that the
exact call was forwarded externally during its active interval, and promotes
the call's attached lead to a user in place.  Edge uses external call
forwarding only for Phone Switch, so that conversation part is the workspace's
Switch-selection marker.

The action deliberately lives outside the general Intercom webhook router.  A
failure after a possibly committed write returns 500; Intercom's retry then
re-reads the contact and safely observes the already-user state.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
from typing import Any

import requests

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

API_BASE = "https://api.intercom.io"
API_VERSION = "2.17"
EXPECTED_TOPIC = "call.ended"
FORWARD_PART_TYPE = "forward_call_to_external_number"
CALL_TIME_SKEW_SECONDS = 5
REQUEST_TIMEOUT = (3.05, 8)


class IntercomError(RuntimeError):
    """An Intercom read or write could not be verified."""


def _response(status_code: int, outcome: str) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(
            {
                "ok": 200 <= status_code < 300,
                "outcome": outcome,
            },
            separators=(",", ":"),
        ),
    }


def _raw_body(event: dict[str, Any]) -> str:
    http = event.get("http")
    if not isinstance(http, dict):
        return ""
    body = http.get("body", "")
    if not isinstance(body, str):
        return ""
    if http.get("isBase64Encoded"):
        try:
            return base64.b64decode(body, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
    return body


def _header(event: dict[str, Any], name: str) -> str:
    http = event.get("http")
    headers = http.get("headers") if isinstance(http, dict) else None
    if not isinstance(headers, dict):
        return ""
    wanted = name.casefold()
    for key, value in headers.items():
        if isinstance(key, str) and key.casefold() == wanted and isinstance(value, str):
            return value
    return ""


def _valid_signature(raw_body: str, signature: str, secret: str) -> bool:
    if not raw_body or not signature or not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), raw_body.encode("utf-8"), hashlib.sha1
    ).hexdigest()
    provided = signature.removeprefix("sha1=")
    return hmac.compare_digest(expected, provided)


def _api_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ['INTERCOM_ACCESS_TOKEN']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Intercom-Version": API_VERSION,
    }


def _request_json(
    method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    try:
        response = requests.request(
            method,
            f"{API_BASE}{path}",
            headers=_api_headers(),
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as error:
        raise IntercomError(f"Intercom {method} transport failure") from error

    if not response.ok:
        raise IntercomError(f"Intercom {method} returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as error:
        raise IntercomError(f"Intercom {method} returned malformed JSON") from error
    if not isinstance(payload, dict):
        raise IntercomError(f"Intercom {method} returned a non-object response")
    return payload


def _get_call(call_id: str) -> dict[str, Any]:
    return _request_json("GET", f"/calls/{call_id}")


def _get_conversation(conversation_id: str) -> dict[str, Any]:
    return _request_json("GET", f"/conversations/{conversation_id}")


def _get_contact(contact_id: str) -> dict[str, Any]:
    return _request_json("GET", f"/contacts/{contact_id}")


def _update_contact(contact_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    return _request_json("PUT", f"/contacts/{contact_id}", fields)


def _search_contacts_by_phone(phone: str) -> list[dict[str, Any]]:
    payload = _request_json(
        "POST",
        "/contacts/search",
        {
            "query": {"field": "phone", "operator": "=", "value": phone},
            "pagination": {"per_page": 150},
        },
    )
    contacts = payload.get("data")
    if not isinstance(contacts, list):
        raise IntercomError("phone contact search returned malformed data")
    pages = payload.get("pages")
    if isinstance(pages, dict) and pages.get("total_pages", 1) not in (0, 1):
        raise IntercomError("phone contact search was unexpectedly paginated")
    return [contact for contact in contacts if isinstance(contact, dict)]


def _clean_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    cleaned = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", cleaned):
        return None
    return cleaned


def _timestamp(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _conversation_contact_ids(conversation: dict[str, Any]) -> set[str]:
    container = conversation.get("contacts")
    contacts = container.get("contacts") if isinstance(container, dict) else None
    if not isinstance(contacts, list):
        return set()
    result = set()
    for contact in contacts:
        if isinstance(contact, dict):
            contact_id = _clean_id(contact.get("id"))
            if contact_id:
                result.add(contact_id)
    return result


def _has_forward_marker(conversation: dict[str, Any], call: dict[str, Any]) -> bool:
    initiated_at = _timestamp(call.get("initiated_at"))
    if initiated_at is None:
        initiated_at = _timestamp(call.get("created_at"))
    ended_at = _timestamp(call.get("ended_at"))
    if initiated_at is None or ended_at is None or ended_at < initiated_at:
        return False

    container = conversation.get("conversation_parts")
    parts = container.get("conversation_parts") if isinstance(container, dict) else None
    if not isinstance(parts, list):
        return False

    earliest = initiated_at - CALL_TIME_SKEW_SECONDS
    latest = ended_at + CALL_TIME_SKEW_SECONDS
    return any(
        isinstance(part, dict)
        and part.get("part_type") == FORWARD_PART_TYPE
        and (created_at := _timestamp(part.get("created_at"))) is not None
        and earliest <= created_at <= latest
        for part in parts
    )


def _normalized_phone(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    digits = "".join(character for character in value if character.isdigit())
    return digits or None


def _contact_matches_call(contact: dict[str, Any], call: dict[str, Any]) -> bool:
    contact_phone = _normalized_phone(contact.get("phone"))
    call_phone = _normalized_phone(call.get("phone"))
    return bool(contact_phone and call_phone and contact_phone == call_phone)


def _other_same_phone_user_exists(contact: dict[str, Any], contact_id: str) -> bool:
    phone = contact.get("phone")
    normalized = _normalized_phone(phone)
    if not isinstance(phone, str) or not normalized:
        raise IntercomError("contact has no searchable phone")

    for candidate in _search_contacts_by_phone(phone):
        candidate_id = _clean_id(candidate.get("id"))
        if not candidate_id or candidate_id == contact_id:
            continue
        if (
            candidate.get("role") == "user"
            and _normalized_phone(candidate.get("phone")) == normalized
        ):
            return True
    return False


def _desired_external_id(contact: dict[str, Any], contact_id: str) -> str:
    existing = contact.get("external_id")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    return f"edge-switch-{contact_id}"


def _promote_and_verify(contact: dict[str, Any], contact_id: str) -> None:
    external_id = _desired_external_id(contact, contact_id)

    # The write is idempotent: retries target the same contact and deterministic
    # external_id.  The response is not trusted as final proof; readback is.
    _update_contact(
        contact_id,
        {"role": "user", "external_id": external_id},
    )
    final = _get_contact(contact_id)

    # Some Intercom workspaces have applied the role change while omitting the
    # external_id from the combined write.  Repair that bounded case once.
    if final.get("role") == "user" and not final.get("external_id"):
        _update_contact(contact_id, {"external_id": external_id})
        final = _get_contact(contact_id)

    if final.get("id") != contact_id or final.get("role") != "user":
        raise IntercomError("contact promotion readback did not verify")
    if (
        not isinstance(final.get("external_id"), str)
        or not final["external_id"].strip()
    ):
        raise IntercomError("promoted contact has no external_id")


def _handle_call_ended(item: dict[str, Any]) -> str:
    event_call_id = _clean_id(item.get("id"))
    if not event_call_id:
        return "skipped_missing_call_id"

    call = _get_call(event_call_id)
    if _clean_id(call.get("id")) != event_call_id:
        raise IntercomError("call readback identity mismatch")
    if call.get("state") != "ended":
        raise IntercomError("call readback is not ended")
    if call.get("direction") != "inbound" or call.get("call_type") != "phone":
        return "skipped_non_inbound_phone_call"

    conversation_id = _clean_id(call.get("conversation_id"))
    contact_id = _clean_id(call.get("contact_id"))
    if not conversation_id or not contact_id:
        return "skipped_missing_call_links"

    conversation = _get_conversation(conversation_id)
    if _clean_id(conversation.get("id")) != conversation_id:
        raise IntercomError("conversation readback identity mismatch")
    source = conversation.get("source")
    if not isinstance(source, dict) or source.get("type") != "phone_call":
        return "skipped_non_phone_conversation"
    if contact_id not in _conversation_contact_ids(conversation):
        raise IntercomError("call contact is absent from its conversation")
    if not _has_forward_marker(conversation, call):
        return "skipped_no_switch_forward"

    contact = _get_contact(contact_id)
    if _clean_id(contact.get("id")) != contact_id:
        raise IntercomError("contact readback identity mismatch")
    if not _contact_matches_call(contact, call):
        raise IntercomError("call and contact phone mismatch")

    role = contact.get("role")
    if role == "user":
        return "already_user"
    if role != "lead":
        return "skipped_unsupported_contact_role"
    if _other_same_phone_user_exists(contact, contact_id):
        return "same_phone_user_exists"

    _promote_and_verify(contact, contact_id)
    return "promoted"


def main(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """DigitalOcean Functions entry point."""
    del context
    http = event.get("http")
    method = http.get("method", "").upper() if isinstance(http, dict) else ""
    if method == "HEAD":
        return {"statusCode": 200, "body": ""}
    if method != "POST":
        return _response(405, "method_not_allowed")

    raw_body = _raw_body(event)
    secret = os.environ.get("INTERCOM_SWITCH_WEBHOOK_SECRET", "")
    signature = _header(event, "x-hub-signature")
    if not _valid_signature(raw_body, signature, secret):
        logger.warning("Rejected Switch promotion webhook: invalid signature")
        return _response(401, "invalid_signature")

    try:
        payload = json.loads(raw_body)
    except (TypeError, json.JSONDecodeError):
        return _response(400, "malformed_body")
    if not isinstance(payload, dict):
        return _response(400, "malformed_body")

    topic = payload.get("topic")
    if topic != EXPECTED_TOPIC:
        logger.info("Ignored webhook topic %r", topic)
        return _response(200, "ignored_topic")

    data = payload.get("data")
    item = data.get("item") if isinstance(data, dict) else None
    if not isinstance(item, dict):
        return _response(200, "skipped_missing_call")

    try:
        outcome = _handle_call_ended(item)
    except (IntercomError, KeyError):
        logger.exception("Switch caller promotion requires retry")
        return _response(500, "retry_required")

    logger.info("Switch caller promotion outcome=%s", outcome)
    return _response(200, outcome)

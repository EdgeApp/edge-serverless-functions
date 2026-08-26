"""Intercom API client — shared by all webhook handlers.

Provides helpers for contacts (search, create, merge) and conversations
(notes, attribute updates).
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.intercom.io"
API_VERSION = "2.11"

_cached_admin_id = None


def _headers():
    token = os.environ["INTERCOM_ACCESS_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Intercom-Version": API_VERSION,
    }


# ── Contact helpers (lead-to-user) ──────────────────────────────────


def search_users_by_external_id(external_id):
    """Search Intercom contacts by external_id and return only those with role 'user'."""
    resp = requests.post(
        f"{BASE_URL}/contacts/search",
        headers=_headers(),
        json={
            "query": {"field": "external_id", "operator": "=", "value": external_id},
            "pagination": {"per_page": 10},
        },
        timeout=10,
    )
    resp.raise_for_status()
    contacts = resp.json().get("data", [])
    return [c for c in contacts if c.get("role") == "user"]


def search_users_by_email(email):
    """Search Intercom contacts by email and return only those with role 'user'."""
    resp = requests.post(
        f"{BASE_URL}/contacts/search",
        headers=_headers(),
        json={
            "query": {"field": "email", "operator": "=", "value": email},
            "pagination": {"per_page": 50},
        },
        timeout=10,
    )
    resp.raise_for_status()
    contacts = resp.json().get("data", [])
    users = [c for c in contacts if c.get("role") == "user"]
    if len(users) > 1:
        logger.warning(
            "Multiple users found for email %s — merging into first", email
        )
    return users


def create_user(email, name=None, external_id=None):
    """Create a new Intercom contact with role 'user'."""
    body = {"role": "user"}
    if external_id:
        body["external_id"] = external_id
    body["email"] = email
    if name:
        body["name"] = name
    resp = requests.post(
        f"{BASE_URL}/contacts",
        headers=_headers(),
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    user = resp.json()
    logger.info("Created user %s for email %s", user["id"], email)
    return user


def merge_lead_into_user(lead_id, user_id):
    """Merge a lead into a user. The lead is deleted; the user is returned."""
    resp = requests.post(
        f"{BASE_URL}/contacts/merge",
        headers=_headers(),
        json={"from": lead_id, "into": user_id},
        timeout=10,
    )
    if resp.status_code == 404:
        logger.warning(
            "Lead %s not found during merge (likely already merged)", lead_id
        )
        return None
    resp.raise_for_status()
    logger.info("Merged lead %s into user %s", lead_id, user_id)
    return resp.json()


def set_user_external_id(contact_id, external_id):
    """PUT /contacts/{contact_id} — set the external_id (the client identifier
    shown as "User id" in the Intercom UI) on a contact.

    Used after a lead→user merge to reassign the lead's original id onto the
    surviving user, since the merge does not reliably carry it over. Safe to
    call once the lead is gone (the id is free) and idempotent if it already
    matches.
    """
    resp = requests.put(
        f"{BASE_URL}/contacts/{contact_id}",
        headers=_headers(),
        json={"external_id": external_id},
        timeout=10,
    )
    if not resp.ok:
        logger.error("Intercom API error %s: %s", resp.status_code, resp.text)
    resp.raise_for_status()
    logger.info("Set external_id=%s on contact %s", external_id, contact_id)
    return resp.json()


# ── Conversation / contact helpers (call-timezone) ──────────────────


def _get_admin_id() -> str:
    """Fetch the admin ID for the token owner (cached after first call)."""
    global _cached_admin_id
    if _cached_admin_id:
        return _cached_admin_id
    resp = requests.get(f"{BASE_URL}/me", headers=_headers(), timeout=10)
    resp.raise_for_status()
    _cached_admin_id = resp.json()["id"]
    logger.info("Resolved token owner admin_id=%s", _cached_admin_id)
    return _cached_admin_id


def get_conversation(conversation_id: str) -> dict:
    """GET a conversation for call-note receipt checks."""
    resp = requests.get(
        f"{BASE_URL}/conversations/{conversation_id}",
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    conversation = resp.json()
    if not isinstance(conversation, dict):
        raise ValueError("Intercom returned a malformed conversation")
    return conversation


def conversation_contains_note_marker(conversation: dict, marker: str) -> bool:
    """Return whether a conversation contains the exact note marker."""
    container = conversation.get("conversation_parts")
    if not isinstance(container, dict):
        raise ValueError("Intercom conversation is missing conversation_parts")
    parts = container.get("conversation_parts")
    if not isinstance(parts, list):
        raise ValueError("Intercom conversation parts are malformed")
    return any(
        isinstance(part, dict)
        and isinstance(part.get("body"), str)
        and marker in part["body"]
        for part in parts
    )


def create_conversation_note(conversation_id: str, body: str) -> dict:
    """POST /conversations/{id}/parts — add an internal note to a conversation."""
    admin_id = _get_admin_id()
    resp = requests.post(
        f"{BASE_URL}/conversations/{conversation_id}/parts",
        headers=_headers(),
        json={
            "message_type": "note",
            "type": "admin",
            "admin_id": admin_id,
            "body": body,
        },
        timeout=10,
    )
    resp.raise_for_status()
    logger.info("Created note on conversation %s", conversation_id)
    return resp.json()


def update_contact_attributes(contact_id: str, attrs: dict) -> dict:
    """PUT /contacts/{contact_id} — set custom_attributes on a contact."""
    resp = requests.put(
        f"{BASE_URL}/contacts/{contact_id}",
        headers=_headers(),
        json={"custom_attributes": attrs},
        timeout=10,
    )
    if not resp.ok:
        logger.error("Intercom API error %s: %s", resp.status_code, resp.text)
    resp.raise_for_status()
    logger.info("Updated custom_attributes on contact %s", contact_id)
    return resp.json()

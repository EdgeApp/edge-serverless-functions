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


def create_user(email, name=None):
    """Create a new Intercom contact with role 'user'."""
    body = {"role": "user", "email": email}
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
    logger.info("Updated custom_attributes on contact %s: %s", contact_id, attrs)
    return resp.json()

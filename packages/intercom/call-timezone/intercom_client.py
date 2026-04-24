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

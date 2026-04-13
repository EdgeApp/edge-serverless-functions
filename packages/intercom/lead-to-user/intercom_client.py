import os
import logging
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.intercom.io"
API_VERSION = "2.11"


def _headers():
    token = os.environ["INTERCOM_ACCESS_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Intercom-Version": API_VERSION,
    }


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

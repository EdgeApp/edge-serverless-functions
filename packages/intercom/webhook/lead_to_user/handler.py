"""Lead-to-user auto-converter.

Converts Intercom leads into users whenever a lead has an email address.
Called by the webhook router for contact.lead.created, contact.lead.added_email,
and contact.email.updated topics.
"""

import logging

from intercom_client import search_users_by_external_id, create_user, merge_lead_into_user

logger = logging.getLogger(__name__)


def handle(payload):
    """Process a lead-related webhook payload. Expects a pre-verified, parsed dict."""
    item = payload.get("data", {}).get("item", {})
    role = item.get("role")
    email = item.get("email")
    lead_id = item.get("id")
    lead_user_id = item.get("user_id") or item.get("external_id")
    name = item.get("name")

    if role != "lead" or not email or not lead_id or not lead_user_id:
        logger.info(
            "Skipping: role=%s, email=%s, lead_id=%s, lead_user_id=%s",
            role,
            email,
            lead_id,
            lead_user_id,
        )
        return {"statusCode": 200, "body": "Skipped"}

    external_id = lead_user_id

    try:
        users = search_users_by_external_id(external_id)

        if users:
            user_id = users[0]["id"]
            logger.info("Found existing user %s for external_id %s", user_id, external_id)
        else:
            new_user = create_user(email, name, external_id)
            user_id = new_user["id"]

        result = merge_lead_into_user(lead_id, user_id)
        if result:
            logger.info("Conversion complete: lead %s → user %s", lead_id, user_id)
        else:
            logger.info("Lead %s was already merged", lead_id)

    except Exception:
        logger.exception("Error converting lead %s (%s)", lead_id, email)

    return {"statusCode": 200, "body": "OK"}

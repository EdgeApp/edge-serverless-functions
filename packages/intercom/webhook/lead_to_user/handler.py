"""Lead-to-user auto-converter.

Converts Intercom leads into users whenever a lead has an email address.
Called by the webhook router for contact.lead.created, contact.lead.added_email,
and contact.email.updated topics.
"""

import logging

from intercom_client import search_users_by_email, create_user, merge_lead_into_user

logger = logging.getLogger(__name__)


def handle(payload):
    """Process a lead-related webhook payload. Expects a pre-verified, parsed dict."""
    item = payload.get("data", {}).get("item", {})
    role = item.get("role")
    email = item.get("email")
    lead_id = item.get("id")
    name = item.get("name")

    if role != "lead" or not email:
        logger.info("Skipping: role=%s, email=%s", role, email)
        return {"statusCode": 200, "body": "Skipped"}

    try:
        users = search_users_by_email(email)

        if users:
            user_id = users[0]["id"]
            logger.info("Found existing user %s for %s", user_id, email)
        else:
            new_user = create_user(email, name)
            user_id = new_user["id"]

        result = merge_lead_into_user(lead_id, user_id)
        if result:
            logger.info("Conversion complete: lead %s → user %s", lead_id, user_id)
        else:
            logger.info("Lead %s was already merged", lead_id)

    except Exception:
        logger.exception("Error converting lead %s (%s)", lead_id, email)

    return {"statusCode": 200, "body": "OK"}

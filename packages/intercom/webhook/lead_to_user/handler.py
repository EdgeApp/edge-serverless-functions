"""Lead-to-user auto-converter.

Converts Intercom leads into users whenever a lead has an email address.
Called by the webhook router for contact.lead.created, contact.lead.added_email,
and contact.email.updated topics.

Identity preservation: a lead may already carry a client identifier (shown as
"User id" in the Intercom UI; delivered as ``user_id`` in webhook payloads and
called ``external_id`` in the REST API). That id MUST survive the conversion,
otherwise the customer's Messenger session no longer maps to the contact that
owns their conversation. We therefore never create a user with an explicit
external_id (which would collide with the id the lead already holds and 409);
instead we create the user with email only and let the merge carry the lead's
existing id over to the surviving user.
"""

import logging

from intercom_client import (
    search_users_by_external_id,
    search_users_by_email,
    create_user,
    merge_lead_into_user,
)

logger = logging.getLogger(__name__)


def handle(payload):
    """Process a lead-related webhook payload. Expects a pre-verified, parsed dict."""
    item = payload.get("data", {}).get("item", {})
    role = item.get("role")
    email = item.get("email")
    lead_id = item.get("id")
    name = item.get("name")
    # The lead's existing client identifier. Intercom calls this user_id in webhook
    # payloads and external_id in the REST API; both name the same value.
    lead_user_id = item.get("user_id") or item.get("external_id")

    if role != "lead" or not email or not lead_id:
        logger.info("Skipping: role=%s, email=%s, lead_id=%s", role, email, lead_id)
        return {"statusCode": 200, "body": "Skipped"}

    try:
        # Find a user that already represents this person, so repeated webhooks
        # don't create duplicates. Match on the lead's existing id first (the
        # identity we must preserve), then fall back to email.
        users = []
        if lead_user_id:
            users = search_users_by_external_id(lead_user_id)
        if not users:
            users = search_users_by_email(email)

        if users:
            user_id = users[0]["id"]
            logger.info(
                "Found existing user %s for lead %s (identity %s)",
                user_id,
                lead_id,
                lead_user_id,
            )
        else:
            # Create the user with email only — no external_id. The merge below
            # promotes the lead and carries its existing user_id over, so the
            # contact keeps the same identity it had as a lead.
            new_user = create_user(email, name)
            user_id = new_user["id"]

        result = merge_lead_into_user(lead_id, user_id)
        if result:
            logger.info(
                "Conversion complete: lead %s → user %s (identity %s)",
                lead_id,
                user_id,
                lead_user_id,
            )
        else:
            logger.info("Lead %s was already merged", lead_id)

    except Exception:
        logger.exception("Error converting lead %s (%s)", lead_id, email)

    return {"statusCode": 200, "body": "OK"}

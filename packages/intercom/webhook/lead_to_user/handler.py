"""Lead-to-user auto-converter.

Converts Intercom leads into users whenever a lead has an email address.
Called by the webhook router for contact.lead.created, contact.lead.added_email,
and contact.email.updated topics.

Identity preservation: a lead may already carry a client identifier (shown as
"User id" in the Intercom UI; delivered as ``user_id`` in webhook payloads and
called ``external_id`` in the REST API). That id MUST survive the conversion,
otherwise the customer's Messenger session no longer maps to the contact that
owns their conversation. We never create a user with an explicit external_id
(which would collide with the id the lead already holds and 409). Instead we
create the user with email only, merge the lead in, and then reassign the
lead's id onto the surviving user — the merge does not reliably carry it over.
"""

import logging

from intercom_client import (
    search_users_by_external_id,
    search_users_by_email,
    create_user,
    merge_lead_into_user,
    set_user_external_id,
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
        found_by_external_id = False
        if lead_user_id:
            users = search_users_by_external_id(lead_user_id)
            found_by_external_id = bool(users)
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
            # Create the user with email only — no external_id. Setting it here
            # would collide with the id the lead still holds.
            new_user = create_user(email, name)
            user_id = new_user["id"]

        result = merge_lead_into_user(lead_id, user_id)
        if result:
            logger.info("Conversion complete: lead %s → user %s", lead_id, user_id)
        else:
            logger.info("Lead %s was already merged", lead_id)

        # Reassign the lead's original id onto the surviving user. The merge does
        # not reliably transfer it, so we set it explicitly. This is safe (the
        # lead is gone, so the id is free) and idempotent (no-op if it already
        # matches). Skip when the user was found by that id — it already has it.
        if lead_user_id and not found_by_external_id:
            set_user_external_id(user_id, lead_user_id)
            logger.info("Reassigned identity %s onto user %s", lead_user_id, user_id)

    except Exception:
        logger.exception("Error converting lead %s (%s)", lead_id, email)

    return {"statusCode": 200, "body": "OK"}

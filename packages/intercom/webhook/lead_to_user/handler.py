"""Lead-to-user auto-converter.

Called by the webhook router for contact.lead.created, contact.lead.added_email,
and contact.email.updated topics.

IMPORTANT — why conversion is disabled by default:
Converting a lead to a user server-side requires Intercom's merge API, which
*permanently deletes the lead profile*. A customer's live Messenger session is
bound to the lead's Intercom contact ``id`` (not their email or external_id),
so the merge orphans that session and the next message fails to send
("Couldn't send"). Intercom confirms this is expected behaviour of the merge
API, and there is no REST way to promote a lead to a user in place. The correct
fix is client-side: on login, call Intercom('shutdown') then boot with
user_id + email (+ user_hash) so the Messenger re-establishes the session.

This handler therefore only performs the merge when LEAD_TO_USER_CONVERSION_ENABLED
is explicitly truthy. With it unset (the default) the lead is left intact and
keeps chatting.
"""

import logging
import os

from intercom_client import (
    search_users_by_external_id,
    search_users_by_email,
    create_user,
    merge_lead_into_user,
    set_user_external_id,
)

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}


def _conversion_enabled():
    return os.environ.get("LEAD_TO_USER_CONVERSION_ENABLED", "").strip().lower() in _TRUTHY


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

    if not _conversion_enabled():
        # Do NOT merge: it would delete the lead and break their live Messenger
        # session. Leave the lead intact; conversion must happen client-side.
        logger.info(
            "Conversion disabled; leaving lead %s intact to preserve Messenger session",
            lead_id,
        )
        return {"statusCode": 200, "body": "Skipped (conversion disabled)"}

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
        # not reliably transfer it. Skip when the user was found by that id.
        if lead_user_id and not found_by_external_id:
            set_user_external_id(user_id, lead_user_id)
            logger.info("Reassigned identity %s onto user %s", lead_user_id, user_id)

    except Exception:
        logger.exception("Error converting lead %s (%s)", lead_id, email)

    return {"statusCode": 200, "body": "OK"}

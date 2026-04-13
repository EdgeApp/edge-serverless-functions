import base64
import hashlib
import hmac
import json
import logging
import os

from intercom_client import search_users_by_email, create_user, merge_lead_into_user

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def _get_raw_body(event):
    """Extract the raw HTTP body from a DO Functions raw web event."""
    http = event.get("http", {})
    body = http.get("body", "")
    if http.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return body


def _verify_signature(raw_body, signature_header, secret):
    """Verify Intercom's HMAC-SHA1 webhook signature (X-Hub-Signature: sha1=...)."""
    if not signature_header:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()
    provided = signature_header.removeprefix("sha1=")
    return hmac.compare_digest(expected, provided)


def main(event, context):
    raw_body = _get_raw_body(event)

    headers = event.get("http", {}).get("headers", {})
    secret = os.environ.get("WEBHOOK_SECRET", "")

    if not _verify_signature(raw_body, headers.get("x-hub-signature"), secret):
        logger.warning("Invalid or missing HMAC signature")
        return {"statusCode": 401, "body": "Invalid signature"}

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Malformed JSON body")
        return {"statusCode": 401, "body": "Malformed body"}

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

import base64
import hashlib
import hmac
import importlib.util
import json
import sys
import os
from unittest.mock import patch, MagicMock

import pytest

FUNC_DIR = os.path.join(os.path.dirname(__file__), "..", "webhook")
sys.path.insert(0, FUNC_DIR)

import intercom_client

spec = importlib.util.spec_from_file_location("webhook_handler", os.path.join(FUNC_DIR, "__main__.py"))
handler = importlib.util.module_from_spec(spec)
sys.modules["webhook_handler"] = handler
spec.loader.exec_module(handler)

WEBHOOK_SECRET = "test-secret-key"
INTERCOM_TOKEN = "fake-token"


def _make_payload(role="lead", email="jane@example.com", name="Jane Doe",
                  lead_id="lead_abc123", topic="contact.lead.created"):
    return {
        "type": "notification_event",
        "topic": topic,
        "data": {
            "type": "notification_event_data",
            "item": {
                "type": "contact",
                "id": lead_id,
                "role": role,
                "email": email,
                "name": name,
            },
        },
    }


def _sign(body, secret=WEBHOOK_SECRET):
    digest = hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()
    return f"sha1={digest}"


def _make_event(payload_dict, secret=WEBHOOK_SECRET, signature_override=None, raw_body_override=None):
    """Build a DO Functions raw web event from a payload dict."""
    raw_body = raw_body_override or json.dumps(payload_dict)
    sig = signature_override if signature_override is not None else _sign(raw_body, secret)
    return {
        "http": {
            "method": "POST",
            "headers": {
                "content-type": "application/json",
                "x-hub-signature": sig,
            },
            "body": raw_body,
            "isBase64Encoded": False,
            "path": "",
        }
    }


def _make_event_base64(payload_dict, secret=WEBHOOK_SECRET):
    """Build an event where the body is base64-encoded (as DO may send)."""
    raw_body = json.dumps(payload_dict)
    sig = _sign(raw_body, secret)
    return {
        "http": {
            "method": "POST",
            "headers": {
                "content-type": "application/json",
                "x-hub-signature": sig,
            },
            "body": base64.b64encode(raw_body.encode("utf-8")).decode("utf-8"),
            "isBase64Encoded": True,
            "path": "",
        }
    }


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("INTERCOM_ACCESS_TOKEN", INTERCOM_TOKEN)


def _mock_search(users):
    """Return a mock response for POST /contacts/search."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"type": "list", "data": users}
    resp.raise_for_status = MagicMock()
    return resp


def _mock_create(user_id, email):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"type": "contact", "id": user_id, "role": "user", "email": email}
    resp.raise_for_status = MagicMock()
    return resp


def _mock_merge(user_id, email):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"type": "contact", "id": user_id, "role": "user", "email": email}
    resp.raise_for_status = MagicMock()
    return resp


def _mock_merge_404():
    resp = MagicMock()
    resp.status_code = 404
    resp.json.return_value = {"type": "error.list", "errors": [{"code": "not_found"}]}
    resp.raise_for_status = MagicMock()
    return resp


# ── Signature verification ──────────────────────────────────────────


class TestSignatureVerification:
    def test_valid_signature_accepted(self):
        event = _make_event(_make_payload())
        with patch("intercom_client.requests.post") as mock_post:
            mock_post.side_effect = [
                _mock_search([]),
                _mock_create("user_1", "jane@example.com"),
                _mock_merge("user_1", "jane@example.com"),
            ]
            result = handler.main(event, None)
        assert result["statusCode"] == 200

    def test_invalid_signature_rejected(self):
        event = _make_event(_make_payload(), signature_override="sha1=bogus")
        result = handler.main(event, None)
        assert result["statusCode"] == 401

    def test_missing_signature_rejected(self):
        event = _make_event(_make_payload())
        del event["http"]["headers"]["x-hub-signature"]
        result = handler.main(event, None)
        assert result["statusCode"] == 401

    def test_base64_encoded_body(self):
        event = _make_event_base64(_make_payload())
        with patch("intercom_client.requests.post") as mock_post:
            mock_post.side_effect = [
                _mock_search([]),
                _mock_create("user_1", "jane@example.com"),
                _mock_merge("user_1", "jane@example.com"),
            ]
            result = handler.main(event, None)
        assert result["statusCode"] == 200


# ── Topic routing ───────────────────────────────────────────────────


class TestTopicRouting:
    def test_unhandled_topic_returns_ignored(self):
        payload = _make_payload(topic="conversation.created")
        event = _make_event(payload)
        result = handler.main(event, None)
        assert result["statusCode"] == 200
        assert result["body"] == "Ignored"

    def test_lead_created_topic_dispatches(self):
        event = _make_event(_make_payload(topic="contact.lead.created"))
        with patch("intercom_client.requests.post") as mock_post:
            mock_post.side_effect = [
                _mock_search([]),
                _mock_create("user_1", "jane@example.com"),
                _mock_merge("user_1", "jane@example.com"),
            ]
            result = handler.main(event, None)
        assert result["statusCode"] == 200
        assert result["body"] == "OK"

    def test_lead_added_email_topic_dispatches(self):
        event = _make_event(_make_payload(topic="contact.lead.added_email"))
        with patch("intercom_client.requests.post") as mock_post:
            mock_post.side_effect = [
                _mock_search([]),
                _mock_create("user_1", "jane@example.com"),
                _mock_merge("user_1", "jane@example.com"),
            ]
            result = handler.main(event, None)
        assert result["statusCode"] == 200
        assert result["body"] == "OK"

    def test_email_updated_topic_dispatches(self):
        event = _make_event(_make_payload(topic="contact.email.updated"))
        with patch("intercom_client.requests.post") as mock_post:
            mock_post.side_effect = [
                _mock_search([]),
                _mock_create("user_1", "jane@example.com"),
                _mock_merge("user_1", "jane@example.com"),
            ]
            result = handler.main(event, None)
        assert result["statusCode"] == 200
        assert result["body"] == "OK"


# ── Lead routing ────────────────────────────────────────────────────


class TestLeadRouting:
    def test_lead_no_existing_user_creates_and_merges(self):
        event = _make_event(_make_payload())
        with patch("intercom_client.requests.post") as mock_post:
            mock_post.side_effect = [
                _mock_search([]),
                _mock_create("user_new", "jane@example.com"),
                _mock_merge("user_new", "jane@example.com"),
            ]
            result = handler.main(event, None)

        assert result["statusCode"] == 200
        assert mock_post.call_count == 3
        create_call = mock_post.call_args_list[1]
        assert create_call[1]["json"]["role"] == "user"
        assert create_call[1]["json"]["email"] == "jane@example.com"

    def test_lead_with_existing_user_merges_directly(self):
        existing_user = {"type": "contact", "id": "user_existing", "role": "user", "email": "jane@example.com"}
        event = _make_event(_make_payload())
        with patch("intercom_client.requests.post") as mock_post:
            mock_post.side_effect = [
                _mock_search([existing_user]),
                _mock_merge("user_existing", "jane@example.com"),
            ]
            result = handler.main(event, None)

        assert result["statusCode"] == 200
        assert mock_post.call_count == 2
        merge_call = mock_post.call_args_list[1]
        assert merge_call[1]["json"]["from"] == "lead_abc123"
        assert merge_call[1]["json"]["into"] == "user_existing"

    def test_lead_no_email_skipped(self):
        payload = _make_payload(email=None)
        payload["data"]["item"].pop("email")
        raw_body = json.dumps(payload)
        event = _make_event(payload, raw_body_override=raw_body)
        result = handler.main(event, None)
        assert result["statusCode"] == 200
        assert result["body"] == "Skipped"

    def test_lead_empty_email_skipped(self):
        payload = _make_payload(email="")
        raw_body = json.dumps(payload)
        event = _make_event(payload, raw_body_override=raw_body)
        result = handler.main(event, None)
        assert result["statusCode"] == 200
        assert result["body"] == "Skipped"

    def test_non_lead_contact_skipped(self):
        payload = _make_payload(role="user")
        raw_body = json.dumps(payload)
        event = _make_event(payload, raw_body_override=raw_body)
        result = handler.main(event, None)
        assert result["statusCode"] == 200
        assert result["body"] == "Skipped"


# ── Edge cases ──────────────────────────────────────────────────────


class TestEdgeCases:
    def test_replayed_webhook_lead_already_merged(self):
        event = _make_event(_make_payload())
        with patch("intercom_client.requests.post") as mock_post:
            mock_post.side_effect = [
                _mock_search([{"id": "user_1", "role": "user", "email": "jane@example.com"}]),
                _mock_merge_404(),
            ]
            result = handler.main(event, None)
        assert result["statusCode"] == 200

    def test_malformed_body_returns_400(self):
        raw_body = "not-json{"
        sig = _sign(raw_body)
        event = {
            "http": {
                "method": "POST",
                "headers": {
                    "content-type": "application/json",
                    "x-hub-signature": sig,
                },
                "body": raw_body,
                "isBase64Encoded": False,
                "path": "",
            }
        }
        result = handler.main(event, None)
        assert result["statusCode"] == 400

    def test_empty_body_returns_401(self):
        event = {
            "http": {
                "method": "POST",
                "headers": {
                    "content-type": "application/json",
                },
                "body": "",
                "isBase64Encoded": False,
                "path": "",
            }
        }
        result = handler.main(event, None)
        assert result["statusCode"] == 401


# ── Intercom client unit tests ──────────────────────────────────────


class TestIntercomClient:
    def test_search_filters_by_user_role(self):
        mixed = [
            {"id": "c1", "role": "lead", "email": "a@b.com"},
            {"id": "c2", "role": "user", "email": "a@b.com"},
            {"id": "c3", "role": "lead", "email": "a@b.com"},
        ]
        with patch("intercom_client.requests.post") as mock_post:
            mock_post.return_value = _mock_search(mixed)
            mock_post.return_value.json.return_value = {"data": mixed}
            users = intercom_client.search_users_by_email("a@b.com")
        assert len(users) == 1
        assert users[0]["id"] == "c2"

    def test_create_user_sends_correct_payload(self):
        with patch("intercom_client.requests.post") as mock_post:
            mock_post.return_value = _mock_create("u1", "test@test.com")
            user = intercom_client.create_user("test@test.com", name="Test")
        call_json = mock_post.call_args[1]["json"]
        assert call_json == {"role": "user", "email": "test@test.com", "name": "Test"}

    def test_create_user_without_name(self):
        with patch("intercom_client.requests.post") as mock_post:
            mock_post.return_value = _mock_create("u1", "test@test.com")
            intercom_client.create_user("test@test.com")
        call_json = mock_post.call_args[1]["json"]
        assert "name" not in call_json

    def test_merge_sends_correct_ids(self):
        with patch("intercom_client.requests.post") as mock_post:
            mock_post.return_value = _mock_merge("user_1", "a@b.com")
            intercom_client.merge_lead_into_user("lead_1", "user_1")
        call_json = mock_post.call_args[1]["json"]
        assert call_json == {"from": "lead_1", "into": "user_1"}

    def test_merge_404_returns_none(self):
        with patch("intercom_client.requests.post") as mock_post:
            mock_post.return_value = _mock_merge_404()
            result = intercom_client.merge_lead_into_user("lead_gone", "user_1")
        assert result is None

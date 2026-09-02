import base64
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FUNC_DIR = PROJECT_ROOT / "packages" / "intercom" / "webhook"
sys.path.insert(0, str(FUNC_DIR))

import intercom_client as tz_intercom_client
from call_timezone import timezone as tz_module

spec = importlib.util.spec_from_file_location(
    "webhook_router", FUNC_DIR / "__main__.py"
)
handler = importlib.util.module_from_spec(spec)
sys.modules["webhook_router"] = handler
spec.loader.exec_module(handler)

WEBHOOK_SECRET = "test-secret-key"
INTERCOM_TOKEN = "fake-token"


def _make_call_payload(phone="+12125551234", contact_id="contact_abc", direction="inbound",
                       conversation_id="conv_123", topic="call.started",
                       call_id="call_123", delivery_attempts=1):
    return {
        "type": "notification_event",
        "topic": topic,
        "delivery_attempts": delivery_attempts,
        "data": {
            "type": "notification_event_data",
            "item": {
                "type": "call",
                "id": call_id,
                "phone": phone,
                "contact_id": contact_id,
                "conversation_id": conversation_id,
                "direction": direction,
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


def _mock_ok_response(data=None):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = data or {}
    resp.raise_for_status = MagicMock()
    return resp


def _conversation_with_bodies(*bodies):
    return {
        "conversation_parts": {
            "conversation_parts": [
                {"part_type": "note", "body": body} for body in bodies
            ]
        }
    }


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("INTERCOM_ACCESS_TOKEN", INTERCOM_TOKEN)
    tz_intercom_client._cached_admin_id = None


# ── Timezone inference (pure logic, no mocks needed) ─────────────────


class TestTimezoneInference:
    def test_us_number_with_area_code(self):
        result = tz_module.infer_timezone("+12125551234")
        assert result is not None
        assert result["country"] == "US"
        assert result["area_code"] == "212"
        assert "America/" in result["timezone"]
        assert result["confidence"] in ("high", "approximate")

    def test_uk_number_single_timezone(self):
        result = tz_module.infer_timezone("+442071234567")
        assert result is not None
        assert result["country"] == "GB"
        assert result["timezone"] == "Europe/London"
        assert result["confidence"] == "high"
        assert result["area_code"] is None

    def test_canadian_number(self):
        result = tz_module.infer_timezone("+14165551234")
        assert result is not None
        assert result["country"] == "CA"
        assert result["area_code"] == "416"
        assert "America/" in result["timezone"]

    def test_german_number(self):
        result = tz_module.infer_timezone("+4930123456")
        assert result is not None
        assert result["country"] == "DE"
        assert result["timezone"] == "Europe/Berlin"
        assert result["confidence"] == "high"

    def test_australian_number(self):
        result = tz_module.infer_timezone("+61291234567")
        assert result is not None
        assert result["country"] == "AU"

    def test_result_has_all_fields(self):
        result = tz_module.infer_timezone("+12125551234")
        assert "timezone" in result
        assert "country" in result
        assert "confidence" in result
        assert "utc_offset" in result
        assert "local_time" in result
        assert "area_code" in result
        assert "location" in result

    def test_invalid_number_returns_none(self):
        result = tz_module.infer_timezone("+0000000")
        assert result is None


# ── Signature verification ───────────────────────────────────────────


class TestSignatureVerification:
    def test_valid_signature_accepted(self):
        event = _make_event(_make_call_payload())
        with patch("intercom_client.requests.get", return_value=_mock_ok_response({"id": "admin_1"})), \
             patch("intercom_client.requests.post", return_value=_mock_ok_response()), \
             patch("intercom_client.requests.put", return_value=_mock_ok_response()):
            result = handler.main(event, None)
        assert result["statusCode"] == 200

    def test_invalid_signature_rejected(self):
        event = _make_event(_make_call_payload(), signature_override="sha1=bogus")
        result = handler.main(event, None)
        assert result["statusCode"] == 401

    def test_missing_signature_rejected(self):
        event = _make_event(_make_call_payload())
        del event["http"]["headers"]["x-hub-signature"]
        result = handler.main(event, None)
        assert result["statusCode"] == 401

    def test_base64_encoded_body(self):
        event = _make_event_base64(_make_call_payload())
        with patch("intercom_client.requests.get", return_value=_mock_ok_response({"id": "admin_1"})), \
             patch("intercom_client.requests.post", return_value=_mock_ok_response()), \
             patch("intercom_client.requests.put", return_value=_mock_ok_response()):
            result = handler.main(event, None)
        assert result["statusCode"] == 200


# ── Webhook filtering ────────────────────────────────────────────────


class TestWebhookFiltering:
    def test_wrong_topic_ignored(self):
        payload = _make_call_payload(topic="conversation.created")
        event = _make_event(payload)
        result = handler.main(event, None)
        assert result["statusCode"] == 200
        assert result["body"] == "Ignored"

    def test_outbound_call_skipped(self):
        payload = _make_call_payload(direction="outbound")
        event = _make_event(payload)
        result = handler.main(event, None)
        assert result["statusCode"] == 200
        assert "Skipped" in result["body"]

    def test_missing_phone_skipped(self):
        payload = _make_call_payload()
        del payload["data"]["item"]["phone"]
        raw_body = json.dumps(payload)
        event = _make_event(payload, raw_body_override=raw_body)
        result = handler.main(event, None)
        assert result["statusCode"] == 200
        assert "Skipped" in result["body"]

    def test_missing_contact_id_skipped(self):
        payload = _make_call_payload()
        del payload["data"]["item"]["contact_id"]
        raw_body = json.dumps(payload)
        event = _make_event(payload, raw_body_override=raw_body)
        result = handler.main(event, None)
        assert result["statusCode"] == 200
        assert "Skipped" in result["body"]

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


# ── Full handler flow ────────────────────────────────────────────────


class TestHandlerFlow:
    def test_inbound_us_call_creates_note_and_updates_attribute(self):
        event = _make_event(_make_call_payload(phone="+12125551234", contact_id="ctc_1"))
        with patch("intercom_client.requests.get", return_value=_mock_ok_response({"id": "admin_1"})), \
             patch("intercom_client.requests.post", return_value=_mock_ok_response()) as mock_post, \
             patch("intercom_client.requests.put", return_value=_mock_ok_response()) as mock_put:
            result = handler.main(event, None)

        assert result["statusCode"] == 200
        assert result["body"] == "OK"

        assert mock_post.call_count == 1
        note_call = mock_post.call_args
        assert "conv_123" in note_call[0][0]
        assert note_call[1]["json"]["message_type"] == "note"
        note_body = note_call[1]["json"]["body"]
        assert "America/" in note_body
        assert "212" in note_body
        assert "[edge-call-location call_id=call_123]" in note_body

        assert mock_put.call_count == 1
        attr_call = mock_put.call_args
        assert "ctc_1" in attr_call[0][0]
        attrs = attr_call[1]["json"]["custom_attributes"]
        assert "inferred_timezone" in attrs
        assert "America/" in attrs["inferred_timezone"]

    def test_inbound_uk_call(self):
        event = _make_event(_make_call_payload(phone="+442071234567", contact_id="ctc_2"))
        with patch("intercom_client.requests.get", return_value=_mock_ok_response({"id": "admin_1"})), \
             patch("intercom_client.requests.post", return_value=_mock_ok_response()), \
             patch("intercom_client.requests.put", return_value=_mock_ok_response()) as mock_put:
            result = handler.main(event, None)

        assert result["statusCode"] == 200
        attr_call = mock_put.call_args
        assert attr_call[1]["json"]["custom_attributes"]["inferred_timezone"] == "Europe/London"

    def test_no_conversation_id_skips_note(self):
        payload = _make_call_payload()
        del payload["data"]["item"]["conversation_id"]
        raw_body = json.dumps(payload)
        event = _make_event(payload, raw_body_override=raw_body)
        with patch("intercom_client.requests.post") as mock_post, \
             patch("intercom_client.requests.put", return_value=_mock_ok_response()):
            result = handler.main(event, None)

        assert result["statusCode"] == 200
        mock_post.assert_not_called()

    def test_note_failure_without_receipt_requests_retry_and_updates_attribute(self):
        event = _make_event(_make_call_payload())
        with patch(
            "call_timezone.handler.create_conversation_note",
            side_effect=Exception("response lost"),
        ), patch(
            "call_timezone.handler.get_conversation",
            return_value=_conversation_with_bodies(),
        ), patch(
            "call_timezone.handler.update_contact_attributes"
        ) as mock_update:
            result = handler.main(event, None)

        assert result == {"statusCode": 500, "body": "Call note requires retry"}
        assert mock_update.call_count == 1

    def test_one_minute_retry_skips_note_when_receipt_exists(self):
        marker = "[edge-call-location call_id=call_123]"
        event = _make_event(_make_call_payload(delivery_attempts=2))
        with patch(
            "call_timezone.handler.get_conversation",
            return_value=_conversation_with_bodies(marker),
        ), patch(
            "call_timezone.handler.create_conversation_note"
        ) as mock_note, patch(
            "call_timezone.handler.update_contact_attributes"
        ) as mock_update:
            result = handler.main(event, None)

        assert result == {"statusCode": 200, "body": "OK"}
        mock_note.assert_not_called()
        assert mock_update.call_count == 1

    def test_retry_creates_note_when_exact_receipt_is_absent(self):
        event = _make_event(_make_call_payload(delivery_attempts=2))
        with patch(
            "call_timezone.handler.get_conversation",
            return_value=_conversation_with_bodies(
                "[edge-call-location call_id=call_old]"
            ),
        ), patch(
            "call_timezone.handler.create_conversation_note"
        ) as mock_note, patch(
            "call_timezone.handler.update_contact_attributes"
        ):
            result = handler.main(event, None)

        assert result == {"statusCode": 200, "body": "OK"}
        assert mock_note.call_count == 1
        assert "call_123" in mock_note.call_args.args[1]

    def test_retry_receipt_check_failure_does_not_risk_duplicate_note(self):
        event = _make_event(_make_call_payload(delivery_attempts=2))
        with patch(
            "call_timezone.handler.get_conversation",
            side_effect=Exception("readback unavailable"),
        ), patch(
            "call_timezone.handler.create_conversation_note"
        ) as mock_note, patch(
            "call_timezone.handler.update_contact_attributes"
        ) as mock_update:
            result = handler.main(event, None)

        assert result == {"statusCode": 500, "body": "Call note requires retry"}
        mock_note.assert_not_called()
        assert mock_update.call_count == 1

    def test_ambiguous_note_write_is_covered_by_durable_receipt(self):
        marker = "[edge-call-location call_id=call_123]"
        event = _make_event(_make_call_payload())
        with patch(
            "call_timezone.handler.create_conversation_note",
            side_effect=Exception("response lost"),
        ), patch(
            "call_timezone.handler.get_conversation",
            return_value=_conversation_with_bodies(marker),
        ), patch("call_timezone.handler.update_contact_attributes"):
            result = handler.main(event, None)

        assert result == {"statusCode": 200, "body": "OK"}

    def test_missing_call_id_skips_note_but_updates_attribute(self):
        event = _make_event(_make_call_payload(call_id=None))
        with patch(
            "call_timezone.handler.create_conversation_note"
        ) as mock_note, patch(
            "call_timezone.handler.update_contact_attributes"
        ) as mock_update:
            result = handler.main(event, None)

        assert result == {"statusCode": 200, "body": "OK"}
        mock_note.assert_not_called()
        assert mock_update.call_count == 1

    def test_attribute_failure_still_returns_200(self):
        event = _make_event(_make_call_payload())
        with patch("intercom_client.requests.get", return_value=_mock_ok_response({"id": "admin_1"})), \
             patch("intercom_client.requests.post", return_value=_mock_ok_response()), \
             patch("intercom_client.requests.put", side_effect=Exception("API error")):
            result = handler.main(event, None)

        assert result["statusCode"] == 200

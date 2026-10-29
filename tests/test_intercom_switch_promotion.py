import base64
import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FUNCTION_DIR = PROJECT_ROOT / "packages" / "intercom-switch" / "promote-caller"

spec = importlib.util.spec_from_file_location(
    "intercom_switch_promote_caller", FUNCTION_DIR / "__main__.py"
)
switch = importlib.util.module_from_spec(spec)
sys.modules["intercom_switch_promote_caller"] = switch
spec.loader.exec_module(switch)

WEBHOOK_SECRET = "switch-test-secret"
ACCESS_TOKEN = "switch-test-token"


def _payload(topic="call.ended", call_id="call_123"):
    return {
        "type": "notification_event",
        "topic": topic,
        "delivery_attempts": 1,
        "data": {
            "type": "notification_event_data",
            "item": {
                "type": "call",
                "id": call_id,
                "conversation_id": "conv_123",
                "contact_id": "contact_123",
                "direction": "inbound",
            },
        },
    }


def _signature(body, secret=WEBHOOK_SECRET):
    digest = hmac.new(
        secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha1
    ).hexdigest()
    return f"sha1={digest}"


def _event(payload=None, *, method="POST", base64_encoded=False, signature=None):
    body = json.dumps(payload if payload is not None else _payload())
    encoded_body = (
        base64.b64encode(body.encode("utf-8")).decode("ascii")
        if base64_encoded
        else body
    )
    return {
        "http": {
            "method": method,
            "headers": {
                "Content-Type": "application/json",
                "X-Hub-Signature": signature or _signature(body),
            },
            "body": encoded_body,
            "isBase64Encoded": base64_encoded,
        }
    }


def _call(**overrides):
    value = {
        "type": "call",
        "id": "call_123",
        "conversation_id": "conv_123",
        "contact_id": "contact_123",
        "state": "ended",
        "direction": "inbound",
        "call_type": "phone",
        "phone": "+15551234567",
        "initiated_at": 1_000,
        "created_at": 1_000,
        "ended_at": 1_100,
    }
    value.update(overrides)
    return value


def _conversation(*parts, source_type="phone_call", contacts=("contact_123",)):
    return {
        "id": "conv_123",
        "source": {"type": source_type},
        "contacts": {
            "contacts": [{"type": "contact", "id": item} for item in contacts]
        },
        "conversation_parts": {"conversation_parts": list(parts)},
    }


def _forward_part(created_at=1_088):
    return {
        "type": "conversation_part",
        "id": "part_forward",
        "part_type": "forward_call_to_external_number",
        "created_at": created_at,
        "body": None,
        "external_id": None,
    }


def _contact(role="lead", external_id="visitor-123", phone="+15551234567"):
    return {
        "type": "contact",
        "id": "contact_123",
        "role": role,
        "external_id": external_id,
        "phone": phone,
    }


def _outcome(result):
    return json.loads(result["body"])["outcome"]


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("INTERCOM_SWITCH_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("INTERCOM_ACCESS_TOKEN", ACCESS_TOKEN)


class TestWebhookBoundary:
    def test_valid_signed_call_ended_dispatches(self):
        with patch.object(
            switch, "_handle_call_ended", return_value="promoted"
        ) as handle:
            result = switch.main(_event(), None)

        assert result["statusCode"] == 200
        assert _outcome(result) == "promoted"
        handle.assert_called_once()
        assert handle.call_args.args[0]["id"] == "call_123"

    def test_base64_body_and_case_insensitive_header_are_accepted(self):
        with patch.object(switch, "_handle_call_ended", return_value="already_user"):
            result = switch.main(_event(base64_encoded=True), None)

        assert result["statusCode"] == 200
        assert _outcome(result) == "already_user"

    def test_invalid_signature_is_rejected_before_dispatch(self):
        with patch.object(switch, "_handle_call_ended") as handle:
            result = switch.main(_event(signature="sha1=wrong"), None)

        assert result["statusCode"] == 401
        assert _outcome(result) == "invalid_signature"
        handle.assert_not_called()

    def test_malformed_call_id_is_rejected_before_api_read(self):
        with patch.object(switch, "_get_call") as get_call:
            result = switch.main(_event(_payload(call_id="../contacts/victim")), None)

        assert result["statusCode"] == 200
        assert _outcome(result) == "skipped_missing_call_id"
        get_call.assert_not_called()

    def test_missing_secret_fails_closed(self, monkeypatch):
        monkeypatch.delenv("INTERCOM_SWITCH_WEBHOOK_SECRET")
        result = switch.main(_event(), None)
        assert result["statusCode"] == 401

    def test_head_is_non_mutating_health_response(self):
        result = switch.main({"http": {"method": "HEAD"}}, None)
        assert result == {"statusCode": 200, "body": ""}

    def test_non_post_method_is_rejected(self):
        result = switch.main({"http": {"method": "GET"}}, None)
        assert result["statusCode"] == 405
        assert _outcome(result) == "method_not_allowed"

    def test_malformed_json_is_rejected_after_signature_check(self):
        raw = "{not-json"
        event = {
            "http": {
                "method": "POST",
                "headers": {"x-hub-signature": _signature(raw)},
                "body": raw,
                "isBase64Encoded": False,
            }
        }
        result = switch.main(event, None)
        assert result["statusCode"] == 400
        assert _outcome(result) == "malformed_body"

    def test_other_topics_are_ignored(self):
        result = switch.main(_event(_payload(topic="call.started")), None)
        assert result["statusCode"] == 200
        assert _outcome(result) == "ignored_topic"

    def test_api_failure_requests_webhook_retry(self):
        with patch.object(
            switch, "_handle_call_ended", side_effect=switch.IntercomError("boom")
        ):
            result = switch.main(_event(), None)

        assert result["statusCode"] == 500
        assert _outcome(result) == "retry_required"


class TestSwitchSelectionProof:
    def test_exact_forward_marker_during_call_is_accepted(self):
        assert switch._has_forward_marker(
            _conversation(_forward_part()),
            _call(),
        )

    @pytest.mark.parametrize("created_at", [900, 1_200])
    def test_stale_or_future_forward_marker_is_rejected(self, created_at):
        assert not switch._has_forward_marker(
            _conversation(_forward_part(created_at)),
            _call(),
        )

    def test_missing_call_timestamps_fail_closed(self):
        assert not switch._has_forward_marker(
            _conversation(_forward_part()),
            _call(initiated_at=None, created_at=None, ended_at=None),
        )

    def test_only_exact_part_type_is_accepted(self):
        assert not switch._has_forward_marker(
            _conversation(
                {
                    "part_type": "comment",
                    "created_at": 1_088,
                    "body": "forward_call_to_external_number",
                }
            ),
            _call(),
        )


class TestCallEndedPolicy:
    def test_forwarded_lead_is_promoted_and_verified(self):
        lead = _contact()
        final_user = _contact(role="user")
        with (
            patch.object(switch, "_get_call", return_value=_call()),
            patch.object(
                switch,
                "_get_conversation",
                return_value=_conversation(_forward_part()),
            ),
            patch.object(switch, "_get_contact", side_effect=[lead, final_user]),
            patch.object(switch, "_search_contacts_by_phone", return_value=[lead]),
            patch.object(switch, "_update_contact", return_value=final_user) as update,
        ):
            outcome = switch._handle_call_ended({"id": "call_123"})

        assert outcome == "promoted"
        update.assert_called_once_with(
            "contact_123",
            {"role": "user", "external_id": "visitor-123"},
        )

    def test_existing_same_phone_user_prevents_second_user(self):
        lead = _contact()
        existing_user = {
            "id": "existing_user",
            "role": "user",
            "phone": "+1 (555) 123-4567",
        }
        with (
            patch.object(switch, "_get_call", return_value=_call()),
            patch.object(
                switch,
                "_get_conversation",
                return_value=_conversation(_forward_part()),
            ),
            patch.object(switch, "_get_contact", return_value=lead),
            patch.object(
                switch,
                "_search_contacts_by_phone",
                return_value=[lead, existing_user],
            ),
            patch.object(switch, "_update_contact") as update,
        ):
            outcome = switch._handle_call_ended({"id": "call_123"})

        assert outcome == "same_phone_user_exists"
        update.assert_not_called()

    def test_forwarded_repeat_caller_who_is_already_user_is_noop(self):
        with (
            patch.object(switch, "_get_call", return_value=_call()),
            patch.object(
                switch,
                "_get_conversation",
                return_value=_conversation(_forward_part()),
            ),
            patch.object(switch, "_get_contact", return_value=_contact(role="user")),
            patch.object(switch, "_update_contact") as update,
        ):
            outcome = switch._handle_call_ended({"id": "call_123"})

        assert outcome == "already_user"
        update.assert_not_called()

    def test_ordinary_unforwarded_call_is_noop(self):
        with (
            patch.object(switch, "_get_call", return_value=_call()),
            patch.object(switch, "_get_conversation", return_value=_conversation()),
            patch.object(switch, "_get_contact") as get_contact,
        ):
            outcome = switch._handle_call_ended({"id": "call_123"})

        assert outcome == "skipped_no_switch_forward"
        get_contact.assert_not_called()

    def test_outbound_call_stops_before_conversation_read(self):
        with (
            patch.object(switch, "_get_call", return_value=_call(direction="outbound")),
            patch.object(switch, "_get_conversation") as get_conversation,
        ):
            outcome = switch._handle_call_ended({"id": "call_123"})

        assert outcome == "skipped_non_inbound_phone_call"
        get_conversation.assert_not_called()

    def test_call_must_be_ended(self):
        with (
            patch.object(switch, "_get_call", return_value=_call(state="in_progress")),
            patch.object(switch, "_get_conversation") as get_conversation,
            pytest.raises(switch.IntercomError, match="not ended"),
        ):
            switch._handle_call_ended({"id": "call_123"})

        get_conversation.assert_not_called()

    def test_non_phone_call_conversation_is_noop(self):
        with (
            patch.object(switch, "_get_call", return_value=_call()),
            patch.object(
                switch,
                "_get_conversation",
                return_value=_conversation(_forward_part(), source_type="conversation"),
            ),
            patch.object(switch, "_get_contact") as get_contact,
        ):
            outcome = switch._handle_call_ended({"id": "call_123"})

        assert outcome == "skipped_non_phone_conversation"
        get_contact.assert_not_called()

    def test_conversation_readback_must_match_requested_id(self):
        conversation = _conversation(_forward_part())
        conversation["id"] = "different_conversation"
        with (
            patch.object(switch, "_get_call", return_value=_call()),
            patch.object(switch, "_get_conversation", return_value=conversation),
            pytest.raises(switch.IntercomError, match="conversation readback"),
        ):
            switch._handle_call_ended({"id": "call_123"})

    def test_call_contact_must_still_belong_to_conversation(self):
        with (
            patch.object(switch, "_get_call", return_value=_call()),
            patch.object(
                switch,
                "_get_conversation",
                return_value=_conversation(
                    _forward_part(), contacts=("other_contact",)
                ),
            ),
            pytest.raises(switch.IntercomError, match="absent"),
        ):
            switch._handle_call_ended({"id": "call_123"})

    def test_call_and_contact_phone_must_not_conflict(self):
        with (
            patch.object(switch, "_get_call", return_value=_call()),
            patch.object(
                switch,
                "_get_conversation",
                return_value=_conversation(_forward_part()),
            ),
            patch.object(
                switch, "_get_contact", return_value=_contact(phone="+15557654321")
            ),
            pytest.raises(switch.IntercomError, match="phone mismatch"),
        ):
            switch._handle_call_ended({"id": "call_123"})

    @pytest.mark.parametrize(
        ("call_phone", "contact_phone"),
        [(None, "+15551234567"), ("+15551234567", None), (None, None)],
    )
    def test_call_and_contact_both_require_a_phone(self, call_phone, contact_phone):
        with (
            patch.object(switch, "_get_call", return_value=_call(phone=call_phone)),
            patch.object(
                switch,
                "_get_conversation",
                return_value=_conversation(_forward_part()),
            ),
            patch.object(
                switch,
                "_get_contact",
                return_value=_contact(phone=contact_phone),
            ),
            pytest.raises(switch.IntercomError, match="phone mismatch"),
        ):
            switch._handle_call_ended({"id": "call_123"})

    def test_forward_marker_from_previous_call_does_not_promote(self):
        old_marker = _forward_part(created_at=500)
        with (
            patch.object(switch, "_get_call", return_value=_call()),
            patch.object(
                switch,
                "_get_conversation",
                return_value=_conversation(old_marker),
            ),
            patch.object(switch, "_get_contact") as get_contact,
        ):
            outcome = switch._handle_call_ended({"id": "call_123"})

        assert outcome == "skipped_no_switch_forward"
        get_contact.assert_not_called()


class TestPromotionReadback:
    def test_missing_external_id_uses_deterministic_contact_scoped_value(self):
        lead = _contact(external_id=None)
        final = _contact(role="user", external_id="edge-switch-contact_123")
        with (
            patch.object(switch, "_update_contact", return_value=final) as update,
            patch.object(switch, "_get_contact", return_value=final),
        ):
            switch._promote_and_verify(lead, "contact_123")

        update.assert_called_once_with(
            "contact_123",
            {"role": "user", "external_id": "edge-switch-contact_123"},
        )

    def test_missing_external_id_after_role_change_gets_one_bounded_repair(self):
        lead = _contact(external_id=None)
        user_without_id = _contact(role="user", external_id=None)
        final = _contact(role="user", external_id="edge-switch-contact_123")
        with (
            patch.object(
                switch, "_update_contact", side_effect=[user_without_id, final]
            ) as update,
            patch.object(switch, "_get_contact", side_effect=[user_without_id, final]),
        ):
            switch._promote_and_verify(lead, "contact_123")

        assert update.call_count == 2
        assert update.call_args_list[1].args == (
            "contact_123",
            {"external_id": "edge-switch-contact_123"},
        )

    def test_role_readback_mismatch_fails_for_webhook_retry(self):
        lead = _contact()
        with (
            patch.object(switch, "_update_contact", return_value=lead),
            patch.object(switch, "_get_contact", return_value=lead),
            pytest.raises(switch.IntercomError, match="did not verify"),
        ):
            switch._promote_and_verify(lead, "contact_123")


class TestIntercomClient:
    def test_uses_current_api_version_and_bounded_timeout(self):
        response = MagicMock()
        response.ok = True
        response.json.return_value = {"id": "contact_123"}
        with patch.object(requests, "request", return_value=response) as request:
            result = switch._request_json("GET", "/contacts/contact_123")

        assert result == {"id": "contact_123"}
        assert request.call_args.kwargs["headers"]["Intercom-Version"] == "2.17"
        assert request.call_args.kwargs["timeout"] == (3.05, 8)
        assert request.call_args.args == (
            "GET",
            "https://api.intercom.io/contacts/contact_123",
        )

    def test_transport_error_becomes_retryable_intercom_error(self):
        with (
            patch.object(requests, "request", side_effect=requests.Timeout("timeout")),
            pytest.raises(switch.IntercomError, match="transport"),
        ):
            switch._request_json("GET", "/contacts/contact_123")

    def test_phone_search_is_exact_and_bounded(self):
        response = MagicMock()
        response.ok = True
        response.json.return_value = {
            "data": [_contact()],
            "pages": {"total_pages": 1},
        }
        with patch.object(requests, "request", return_value=response) as request:
            result = switch._search_contacts_by_phone("+15551234567")

        assert result == [_contact()]
        assert request.call_args.args == (
            "POST",
            "https://api.intercom.io/contacts/search",
        )
        assert request.call_args.kwargs["json"] == {
            "query": {
                "field": "phone",
                "operator": "=",
                "value": "+15551234567",
            },
            "pagination": {"per_page": 150},
        }

    def test_malformed_phone_search_is_retryable(self):
        with (
            patch.object(switch, "_request_json", return_value={"data": None}),
            pytest.raises(switch.IntercomError, match="malformed"),
        ):
            switch._search_contacts_by_phone("+15551234567")

    def test_paginated_phone_search_fails_closed(self):
        with (
            patch.object(
                switch,
                "_request_json",
                return_value={"data": [], "pages": {"total_pages": 2}},
            ),
            pytest.raises(switch.IntercomError, match="paginated"),
        ):
            switch._search_contacts_by_phone("+15551234567")

    def test_http_error_does_not_log_upstream_body(self):
        response = MagicMock()
        response.ok = False
        response.status_code = 429
        response.text = "private customer data"
        with (
            patch.object(requests, "request", return_value=response),
            pytest.raises(switch.IntercomError) as error,
        ):
            switch._request_json("GET", "/contacts/contact_123")

        assert "private customer data" not in str(error.value)


def test_digitalocean_action_is_isolated_from_general_intercom_webhook():
    project = (PROJECT_ROOT / "project.yml").read_text(encoding="utf-8")
    assert "- name: intercom-switch" in project
    assert "- name: promote-caller" in project
    assert "INTERCOM_ACCESS_TOKEN: ${INTERCOM_ACCESS_TOKEN}" in project
    assert "INTERCOM_SWITCH_ACCESS_TOKEN" not in project
    assert "INTERCOM_SWITCH_ACCESS_TOKEN" not in (
        PROJECT_ROOT / ".env.example"
    ).read_text(encoding="utf-8")
    assert (
        "INTERCOM_SWITCH_WEBHOOK_SECRET: ${INTERCOM_SWITCH_WEBHOOK_SECRET}" in project
    )
    assert "packages/intercom-switch/promote-caller" not in (
        PROJECT_ROOT / "packages" / "intercom" / "webhook" / "__main__.py"
    ).read_text(encoding="utf-8")

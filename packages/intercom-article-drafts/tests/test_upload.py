import importlib.util
import json
import os
import re
from unittest.mock import MagicMock, patch

import pytest

FUNCTION = os.path.join(os.path.dirname(__file__), "..", "upload", "__main__.py")
spec = importlib.util.spec_from_file_location("article_draft_upload", FUNCTION)
upload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upload)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("INTERCOM_ACCESS_TOKEN", "shared-intercom-token")
    monkeypatch.setenv("INTERCOM_DRAFT_BRIDGE_SECRET", "bridge-secret-value")
    monkeypatch.setenv("INTERCOM_ARTICLE_AUTHOR_ID", "12345")


def event(payload, token="bridge-secret-value", method="POST"):
    return {
        "http": {
            "method": method,
            "headers": {"Authorization": f"Bearer {token}"},
            "body": json.dumps(payload),
        }
    }


def response(data, status=200):
    result = MagicMock()
    result.status_code = status
    result.json.return_value = data
    result.raise_for_status.return_value = None
    return result


def body(result):
    return json.loads(result["body"])


def test_create_forces_draft_and_returns_draft_identity():
    created = {
        "id": "9001",
        # This public API field is not Knowledge Hub's private activeContentId.
        "content_id": "not-an-editor-id",
        "workspace_id": "edgeapp",
        "state": "draft",
        "body_markdown": "# Reset 2FA\n\nDo the thing.",
    }
    with patch.object(upload.requests, "request", return_value=response(created)) as request:
        result = upload.main(
            event(
                {
                    "operation": "create",
                    "operation_id": "kb-test-0001",
                    "title": "Reset 2FA",
                    "description": "Recovery steps",
                    "body_markdown": "# Reset 2FA\n\nDo the thing.",
                }
            ),
            None,
        )

    assert result["statusCode"] == 200
    receipt = body(result)
    assert receipt["ok"] is True
    assert receipt["operation_id"] == "kb-test-0001"
    assert receipt["operation"] == "create"
    assert receipt["result"] == "created"
    assert receipt["article_id"] == "9001"
    assert receipt["title"] == "Reset 2FA"
    assert receipt["before_state"] is None
    assert receipt["after_state"] == "draft"
    assert receipt["state"] == "draft"
    assert re.fullmatch(r"[0-9a-f]{64}", receipt["submitted_content_hash"])
    assert receipt["completed_at"].endswith("Z")
    payload = request.call_args.kwargs["json"]
    headers = request.call_args.kwargs["headers"]
    assert payload["state"] == "draft"
    assert payload["author_id"] == 12345
    assert headers["Authorization"] == "Bearer shared-intercom-token"
    assert request.call_args.args[:2] == ("POST", "https://api.intercom.io/articles")


def test_update_and_arbitrary_operations_are_unreachable():
    for operation in ("update", "publish", "delete", "anything"):
        with patch.object(upload.requests, "request") as request:
            result = upload.main(event({"operation": operation}), None)
        assert result["statusCode"] == 400
        request.assert_not_called()


def test_parent_placement_is_rejected_before_intercom():
    with patch.object(upload.requests, "request") as request:
        result = upload.main(
            event(
                {
                    "operation": "create",
                    "operation_id": "kb-test-parent",
                    "title": "Title",
                    "body_markdown": "Body",
                    "parent_id": 77,
                    "parent_type": "collection",
                }
            ),
            None,
        )
    assert result["statusCode"] == 400
    request.assert_not_called()


@pytest.mark.parametrize(
    "created",
    [
        {"id": "9001", "state": "published"},
        {"id": "not-numeric", "state": "draft"},
    ],
)
def test_create_fails_when_intercom_does_not_confirm_safe_identity(created):
    with patch.object(upload.requests, "request", return_value=response(created)):
        result = upload.main(
            event(
                {
                    "operation": "create",
                    "operation_id": "kb-test-safety",
                    "title": "Title",
                    "body_markdown": "Body",
                }
            ),
            None,
        )
    assert result["statusCode"] == 502


def test_create_fails_when_rich_directive_is_dropped():
    created = {
        "id": "9001",
        "state": "draft",
        "body_markdown": "Ordinary body",
    }
    with patch.object(upload.requests, "request", return_value=response(created)):
        result = upload.main(
            event(
                {
                    "operation": "create",
                    "operation_id": "kb-test-rich",
                    "title": "Title",
                    "body_markdown": ":::callout backgroundColor=\"#feedaf80\"\nBody\n:::",
                }
            ),
            None,
        )
    assert result["statusCode"] == 502


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation": "create", "title": "", "body_markdown": "body"},
        {
            "operation": "create",
            "operation_id": "short",
            "title": "title",
            "body_markdown": "body",
        },
    ],
)
def test_bad_requests_are_rejected(payload):
    result = upload.main(event(payload), None)
    assert result["statusCode"] == 400


def test_wrong_secret_is_rejected_without_calling_intercom():
    with patch.object(upload.requests, "request") as request:
        result = upload.main(event({"operation": "create"}, token="wrong"), None)
    assert result["statusCode"] == 401
    request.assert_not_called()


def test_short_server_secret_fails_closed(monkeypatch):
    monkeypatch.setenv("INTERCOM_DRAFT_BRIDGE_SECRET", "short")
    with patch.object(upload.requests, "request") as request:
        result = upload.main(event({"operation": "create"}), None)
    assert result["statusCode"] == 500
    assert body(result)["error"] == "Draft bridge configuration error"
    request.assert_not_called()


def test_only_post_is_supported():
    result = upload.main(event({}, method="GET"), None)
    assert result["statusCode"] == 405

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FUNCTION = PROJECT_ROOT / "packages/testrail/bridge/__main__.py"
spec = importlib.util.spec_from_file_location("testrail_bridge", FUNCTION)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TESTRAIL_BASE_URL", "https://edge.testrail.io")
    monkeypatch.setenv("TESTRAIL_USER_EMAIL", "qa@edge.app")
    monkeypatch.setenv("TESTRAIL_API_KEY", "testrail-api-key")
    monkeypatch.setenv("TESTRAIL_BRIDGE_SECRET", "bridge-secret-value")


def event(payload, token="bridge-secret-value", method="POST"):
    return {
        "http": {
            "method": method,
            "headers": {"Authorization": f"Bearer {token}"},
            "body": json.dumps(payload),
        }
    }


def response(data=None, status=200, headers=None):
    result = MagicMock()
    result.status_code = status
    result.ok = 200 <= status < 400
    result.headers = headers or {}
    if data is None:
        result.content = b""
        result.text = ""
    else:
        result.content = json.dumps(data).encode()
        result.text = json.dumps(data)
        result.json.return_value = data
    return result


def body(result):
    return json.loads(result["body"])


def test_get_request_returns_confirmed_testrail_response():
    upstream = {"id": 42, "title": "Create wallet"}
    with patch.object(
        bridge.requests, "request", return_value=response(upstream)
    ) as request:
        result = bridge.main(
            event({"endpoint": "get_case/42", "params": {"history": 1}}), None
        )

    assert result["statusCode"] == 200
    receipt = body(result)
    assert receipt == {
        "ok": True,
        "endpoint": "get_case/42",
        "method": "GET",
        "operation_id": None,
        "upstream_status": 200,
        "upstream_body": upstream,
        "outcome": "confirmed",
        "retry_safe": False,
    }
    assert request.call_args.args == (
        "GET",
        "https://edge.testrail.io/index.php?/api/v2/get_case/42&history=1",
    )
    assert request.call_args.kwargs["auth"] == ("qa@edge.app", "testrail-api-key")
    assert request.call_args.kwargs["json"] is None
    assert request.call_args.kwargs["allow_redirects"] is False


def test_post_request_returns_confirmed_testrail_response():
    upstream = {"id": 43, "title": "Create wallet"}
    payload = {
        "endpoint": "add_case/7",
        "operation_id": "testrail-create-0001",
        "body": {"title": "Create wallet", "priority_id": 2},
    }
    with patch.object(
        bridge.requests, "request", return_value=response(upstream)
    ) as request:
        result = bridge.main(event(payload), None)

    assert result["statusCode"] == 200
    receipt = body(result)
    assert receipt["ok"] is True
    assert receipt["method"] == "POST"
    assert receipt["operation_id"] == "testrail-create-0001"
    assert receipt["upstream_body"] == upstream
    assert request.call_args.args == (
        "POST",
        "https://edge.testrail.io/index.php?/api/v2/add_case/7",
    )
    assert request.call_args.kwargs["json"] == payload["body"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "delete_case/42",
        "delete_cases/3",
        "delete_section/8",
        "delete_suite/9",
        "delete_shared_step/10",
        "delete_project/11",
        "delete_run/12",
        "delete_plan/13",
    ],
)
def test_all_delete_endpoints_are_blocked_before_testrail(endpoint):
    with patch.object(bridge.requests, "request") as request:
        result = bridge.main(
            event({"endpoint": endpoint, "operation_id": "testrail-delete-0001"}),
            None,
        )

    assert result["statusCode"] == 403
    assert body(result)["error"] == "TestRail delete endpoints are blocked"
    request.assert_not_called()


@pytest.mark.parametrize(
    "payload_body",
    [
        {"is_deleted": 1},
        {"case_ids": [1, 2], "changes": {"is_deleted": True}},
        {"items": [{"title": "Safe"}, {"is_deleted": 1}]},
    ],
)
def test_is_deleted_is_blocked_anywhere_in_request_body(payload_body):
    with patch.object(bridge.requests, "request") as request:
        result = bridge.main(
            event(
                {
                    "endpoint": "update_cases/3",
                    "operation_id": "testrail-update-0001",
                    "body": payload_body,
                }
            ),
            None,
        )

    assert result["statusCode"] == 403
    assert body(result)["error"] == "The is_deleted field is blocked"
    request.assert_not_called()


def test_is_deleted_is_blocked_in_query_params():
    with patch.object(bridge.requests, "request") as request:
        result = bridge.main(
            event({"endpoint": "get_cases/1", "params": {"is_deleted": 1}}),
            None,
        )

    assert result["statusCode"] == 403
    assert body(result)["error"] == "The is_deleted field is blocked"
    request.assert_not_called()


def test_missing_or_wrong_bridge_secret_is_rejected():
    with patch.object(bridge.requests, "request") as request:
        result = bridge.main(event({"endpoint": "get_projects"}, token="wrong"), None)

    assert result["statusCode"] == 401
    request.assert_not_called()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example/api/v2/get_cases/1",
        "/get_cases/1",
        "get_cases/1?project=2",
        "get_cases/../delete_case/1",
    ],
)
def test_only_relative_testrail_method_paths_are_allowed(endpoint):
    with patch.object(bridge.requests, "request") as request:
        result = bridge.main(event({"endpoint": endpoint}), None)

    assert result["statusCode"] == 400
    request.assert_not_called()


def test_mutation_requires_operation_id():
    result = bridge.main(event({"endpoint": "add_case/7", "body": {"title": "A"}}), None)

    assert result["statusCode"] == 400
    assert "operation_id" in body(result)["error"]


def test_upstream_429_and_retry_after_are_returned():
    upstream = {"error": "Too many requests"}
    with patch.object(
        bridge.requests,
        "request",
        return_value=response(upstream, status=429, headers={"Retry-After": "60"}),
    ):
        result = bridge.main(event({"endpoint": "get_cases/1"}), None)

    assert result["statusCode"] == 429
    assert result["headers"]["retry-after"] == "60"
    receipt = body(result)
    assert receipt["outcome"] == "rejected"
    assert receipt["retry_safe"] is True
    assert receipt["upstream_status"] == 429


def test_redirect_is_returned_as_rejection_and_not_followed():
    with patch.object(
        bridge.requests,
        "request",
        return_value=response({"redirect": True}, status=302),
    ) as request:
        result = bridge.main(event({"endpoint": "get_projects"}), None)

    assert result["statusCode"] == 302
    receipt = body(result)
    assert receipt["ok"] is False
    assert receipt["outcome"] == "rejected"
    assert request.call_args.kwargs["allow_redirects"] is False


def test_post_timeout_is_reported_as_unknown_and_not_retry_safe():
    with patch.object(bridge.requests, "request", side_effect=requests.Timeout):
        result = bridge.main(
            event(
                {
                    "endpoint": "add_case/7",
                    "operation_id": "testrail-create-0001",
                    "body": {"title": "A"},
                }
            ),
            None,
        )

    assert result["statusCode"] == 504
    receipt = body(result)
    assert receipt["outcome"] == "unknown"
    assert receipt["retry_safe"] is False
    assert receipt["operation_id"] == "testrail-create-0001"


def test_get_timeout_is_retry_safe():
    with patch.object(bridge.requests, "request", side_effect=requests.Timeout):
        result = bridge.main(event({"endpoint": "get_case/42"}), None)

    assert result["statusCode"] == 504
    receipt = body(result)
    assert receipt["outcome"] == "unconfirmed"
    assert receipt["retry_safe"] is True


def test_invalid_config_is_not_exposed():
    with patch.dict("os.environ", {"TESTRAIL_BASE_URL": "http://example.com"}):
        result = bridge.main(event({"endpoint": "get_projects"}), None)

    assert result["statusCode"] == 500
    assert body(result) == {"ok": False, "error": "TestRail bridge configuration error"}


def test_manifest_and_deploy_surface_are_scoped():
    manifest = (PROJECT_ROOT / "project.yml").read_text()
    assert "TESTRAIL_API_KEY: ${TESTRAIL_API_KEY}" in manifest
    assert "TESTRAIL_BRIDGE_SECRET: ${TESTRAIL_BRIDGE_SECRET}" in manifest
    assert "- name: bridge" in manifest

    action_root = PROJECT_ROOT / "packages/testrail"
    assert sorted(path.name for path in action_root.iterdir() if path.is_dir()) == [
        "bridge"
    ]

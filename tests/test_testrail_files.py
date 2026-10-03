import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import httpx
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICE = PROJECT_ROOT / "services/testrail-files/app.py"
spec = importlib.util.spec_from_file_location("testrail_files", SERVICE)
files = importlib.util.module_from_spec(spec)
spec.loader.exec_module(files)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TESTRAIL_BASE_URL", "https://edge.testrail.io")
    monkeypatch.setenv("TESTRAIL_USER_EMAIL", "qa@edge.app")
    monkeypatch.setenv("TESTRAIL_API_KEY", "testrail-api-key")
    monkeypatch.setenv("TESTRAIL_BRIDGE_SECRET", "bridge-secret-value")


@pytest.fixture
def client():
    files.app.config.update(TESTING=True)
    return files.app.test_client()


def upstream(content=b"file-bytes", status=200, headers=None):
    response = MagicMock()
    response.status_code = status
    response.content = content
    response.headers = headers or {}
    response.iter_content.return_value = iter([content[:4], content[4:]])
    return response


def auth():
    return {"Authorization": "Bearer bridge-secret-value"}


def test_health_is_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_download_streams_bytes_and_safe_headers(client):
    remote = upstream(
        b"screenshot-bytes",
        headers={
            "Content-Type": "image/png",
            "Content-Length": "16",
            "Content-Disposition": 'attachment; filename="screen.png"',
            "Set-Cookie": "must-not-pass",
        },
    )
    with patch.object(files.requests, "get", return_value=remote) as get:
        response = client.get(
            "/attachments/2ec27be4-812f-4806-9a5d-d39130d1691a", headers=auth()
        )

    assert response.status_code == 200
    assert response.data == b"screenshot-bytes"
    assert response.headers["Content-Type"] == "image/png"
    assert (
        response.headers["Content-Disposition"]
        == 'attachment; filename="screen.png"'
    )
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Length" not in response.headers
    assert "Set-Cookie" not in response.headers
    assert get.call_args.args == (
        "https://edge.testrail.io/index.php?/api/v2/get_attachment/"
        "2ec27be4-812f-4806-9a5d-d39130d1691a",
    )
    assert get.call_args.kwargs["stream"] is True
    assert get.call_args.kwargs["allow_redirects"] is False
    assert get.call_args.kwargs["headers"]["Accept-Encoding"] == "identity"


def test_download_without_upstream_type_defaults_to_binary(client):
    remote = upstream(b"unknown-file")
    with patch.object(files.requests, "get", return_value=remote):
        response = client.get("/attachments/42", headers=auth())

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/octet-stream"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize(
    "endpoint",
    [
        "add_attachment_to_case/42",
        "add_attachment_to_plan/7",
        "add_attachment_to_plan_entry/7/entry-1",
        "add_attachment_to_result/9",
        "add_attachment_to_run/11",
    ],
)
def test_upload_relays_all_documented_attachment_targets(client, endpoint):
    remote = upstream(
        json.dumps({"attachment_id": "uuid-1"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    uploaded = {}

    def capture_upload(*_args, **kwargs):
        name, stream, content_type = kwargs["files"]["attachment"]
        uploaded.update(name=name, body=stream.read(), content_type=content_type)
        return remote

    with patch.object(files.httpx, "post", side_effect=capture_upload) as post:
        response = client.post(
            f"/attachments/{endpoint}",
            headers=auth(),
            data={"attachment": (io.BytesIO(b"png-data"), "screen.png")},
            content_type="multipart/form-data",
        )

    assert response.status_code == 200
    assert response.get_json() == {"attachment_id": "uuid-1"}
    assert post.call_args.args == (
        f"https://edge.testrail.io/index.php?/api/v2/{endpoint}",
    )
    assert post.call_args.kwargs["follow_redirects"] is False
    assert uploaded == {
        "name": "screen.png",
        "body": b"png-data",
        "content_type": "image/png",
    }
    timeout = post.call_args.kwargs["timeout"]
    assert timeout.connect == files.CONNECT_TIMEOUT_SECONDS
    assert timeout.write == files.TRANSFER_TIMEOUT_SECONDS


def test_delete_attachment_is_blocked(client):
    with patch.object(files.httpx, "post") as post:
        response = client.post(
            "/attachments/delete_attachment/42",
            headers=auth(),
            data={"attachment": (io.BytesIO(b"x"), "x.png")},
            content_type="multipart/form-data",
        )

    assert response.status_code == 403
    assert response.get_json()["error"] == "TestRail delete endpoints are blocked"
    post.assert_not_called()


def test_non_attachment_endpoint_is_not_relayed_on_file_route(client):
    with patch.object(files.httpx, "post") as post:
        response = client.post(
            "/attachments/update_case/42",
            headers=auth(),
            data={"attachment": (io.BytesIO(b"x"), "x.png")},
            content_type="multipart/form-data",
        )

    assert response.status_code == 400
    post.assert_not_called()


def test_upload_requires_attachment(client):
    response = client.post(
        "/attachments/add_attachment_to_case/42", headers=auth()
    )
    assert response.status_code == 400
    assert "attachment" in response.get_json()["error"]


def test_upload_accepts_files_over_digitalocean_function_limit(client):
    payload = b"x" * (2 * 1024 * 1024)
    uploaded_size = {}

    def consume_upload(*_args, **kwargs):
        uploaded_size["bytes"] = len(kwargs["files"]["attachment"][1].read())
        return upstream(
            json.dumps({"attachment_id": "uuid-large"}).encode(),
            headers={"Content-Type": "application/json"},
        )

    with patch.object(files.httpx, "post", side_effect=consume_upload):
        response = client.post(
            "/attachments/add_attachment_to_case/42",
            headers=auth(),
            data={"attachment": (io.BytesIO(payload), "large.png")},
            content_type="multipart/form-data",
        )

    assert response.status_code == 200
    assert uploaded_size["bytes"] == 2 * 1024 * 1024


def test_authentication_is_required(client):
    with patch.object(files.requests, "get") as get:
        response = client.get("/attachments/42")
    assert response.status_code == 401
    get.assert_not_called()


def test_non_ascii_bearer_is_rejected_without_server_error(client):
    with patch.object(files.requests, "get") as get:
        response = client.get(
            "/attachments/42", headers={"Authorization": "Bearer café"}
        )
    assert response.status_code == 401
    get.assert_not_called()


def test_upload_timeout_is_unknown_and_not_retry_safe(client):
    with patch.object(files.httpx, "post", side_effect=httpx.WriteTimeout("slow")):
        response = client.post(
            "/attachments/add_attachment_to_case/42",
            headers=auth(),
            data={"attachment": (io.BytesIO(b"x"), "x.png")},
            content_type="multipart/form-data",
        )

    assert response.status_code == 504
    assert response.get_json()["outcome"] == "unknown"
    assert response.get_json()["retry_safe"] is False


def test_docker_and_app_spec_are_scoped_to_streaming_service():
    dockerfile = (PROJECT_ROOT / "services/testrail-files/Dockerfile").read_text()
    spec_text = (
        PROJECT_ROOT / "services/testrail-files/app-spec.example.yaml"
    ).read_text()
    assert 'CMD ["gunicorn"' in dockerfile
    assert "source_dir: services/testrail-files" in spec_text
    assert "TESTRAIL_API_KEY" in spec_text

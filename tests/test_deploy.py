import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = PROJECT_ROOT / "scripts" / "deploy.sh"


def _write_env(path: Path, **overrides: str) -> None:
    values = {
        "INTERCOM_ACCESS_TOKEN": "test-intercom-token",
        "WEBHOOK_SECRET": "test-webhook-secret",
        "INTERCOM_DRAFT_BRIDGE_SECRET": "test-draft-bridge-secret",
        "INTERCOM_ARTICLE_AUTHOR_ID": "123456",
        "TESTRAIL_BASE_URL": "https://edgeapp.testrail.io",
        "TESTRAIL_USER_EMAIL": "operator@example.com",
        "TESTRAIL_API_KEY": "test-api-key",
        "TESTRAIL_BRIDGE_SECRET": "test-testrail-bridge-secret",
    }
    values.update(overrides)
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    path.chmod(0o600)


def test_deploy_script_is_executable():
    assert os.access(DEPLOY_SCRIPT, os.X_OK)


def test_check_mode_validates_all_functions_without_plaintext_artifact(tmp_path):
    env_file = tmp_path / ".env"
    _write_env(env_file)

    result = subprocess.run(
        [str(DEPLOY_SCRIPT), "--check", "--env-file", str(env_file)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Repository deployment preflight passed.\n"
    assert "test-api-key" not in result.stdout + result.stderr
    assert "test-intercom-token" not in result.stdout + result.stderr
    assert not (PROJECT_ROOT / "services/testrail-files/app-spec.local.yaml").exists()


def test_check_mode_rejects_missing_non_testrail_function_value(tmp_path):
    env_file = tmp_path / ".env"
    _write_env(env_file, INTERCOM_ACCESS_TOKEN="")

    result = subprocess.run(
        [str(DEPLOY_SCRIPT), "--check", "--env-file", str(env_file)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "INTERCOM_ACCESS_TOKEN" in result.stderr
    assert "test-testrail-bridge-secret" not in result.stdout + result.stderr


class _VerificationHandler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        if self.path == "/webhook":
            self._send(200)
        else:
            self._send(404)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, b'{"ok": true}')
        else:
            self._send(404)

    def do_POST(self):
        if self.path == "/drafts":
            self._send(401, b'{"ok": false}')
        elif self.path == "/testrail":
            self._send(200, b'{"ok": true, "upstream_status": 200}')
        else:
            self._send(404)

    def log_message(self, _format, *_args):
        pass


@pytest.mark.parametrize("existing_app", [False, True])
def test_deploys_entire_repository_and_prints_every_url(tmp_path, existing_app):
    env_file = tmp_path / ".env"
    _write_env(env_file)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "doctl.log"
    state_path = tmp_path / "app-created"

    fake_git = fake_bin / "git"
    fake_git.write_text(
        """#!/bin/sh
set -eu
case "$*" in
  "branch --show-current") printf '%s\n' main ;;
  "status --porcelain") exit 0 ;;
  "fetch origin") exit 0 ;;
  "rev-parse HEAD"|"rev-parse origin/main") printf '%s\n' abc123 ;;
  *) printf 'Unexpected git call: %s\n' "$*" >&2; exit 1 ;;
esac
"""
    )
    fake_git.chmod(0o755)

    fake_doctl = fake_bin / "doctl"
    fake_doctl.write_text(
        """#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$FAKE_DOCTL_LOG"
case "$*" in
  "serverless status")
    printf '%s\n' 'namespace fn-c9829d1a-06e2-4af5-9196-b23c58499edc label=edge-tools https://faas-nyc1-2ef2e6cc.doserverless.co'
    ;;
  "serverless deploy "*) exit 0 ;;
  "serverless functions get intercom/webhook --url") printf '%s\n' "$FAKE_BASE_URL/webhook" ;;
  "serverless functions get intercom-article-drafts/upload --url") printf '%s\n' "$FAKE_BASE_URL/drafts" ;;
  "serverless functions get testrail/bridge --url") printf '%s\n' "$FAKE_BASE_URL/testrail" ;;
  "apps list --format ID,Spec.Name --no-header")
    if [ "$FAKE_APP_EXISTS" = 1 ] || [ -f "$FAKE_APP_STATE" ]; then
      printf '%s\n' "app-123 edge-testrail-files"
    fi
    ;;
  "apps create "*) touch "$FAKE_APP_STATE" ;;
  "apps update app-123 "*) exit 0 ;;
  "apps get app-123 --format DefaultIngress --no-header") printf '%s\n' "$FAKE_BASE_URL" ;;
  *) printf 'Unexpected doctl call: %s\n' "$*" >&2; exit 1 ;;
esac
"""
    )
    fake_doctl.chmod(0o755)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _VerificationHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    run_env = os.environ.copy()
    run_env.update(
        {
            "PATH": str(fake_bin) + os.pathsep + run_env["PATH"],
            "FAKE_APP_EXISTS": "1" if existing_app else "0",
            "FAKE_APP_STATE": str(state_path),
            "FAKE_DOCTL_LOG": str(log_path),
            "FAKE_BASE_URL": base_url,
        }
    )

    try:
        result = subprocess.run(
            [str(DEPLOY_SCRIPT), "--env-file", str(env_file)],
            cwd=PROJECT_ROOT,
            env=run_env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stderr
    assert "Repository deployment verified." in result.stdout
    assert "DEPLOYED_SHA=abc123" in result.stdout
    assert f"Intercom webhook URL: {base_url}/webhook" in result.stdout
    assert f"Intercom article-draft bridge URL: {base_url}/drafts" in result.stdout
    assert f"TestRail JSON bridge URL: {base_url}/testrail" in result.stdout
    assert f"TestRail attachment bridge URL: {base_url}" in result.stdout
    assert "test-api-key" not in result.stdout + result.stderr
    assert "test-intercom-token" not in result.stdout + result.stderr

    calls = log_path.read_text()
    assert calls.index(
        "apps list --format ID,Spec.Name --no-header"
    ) < calls.index("serverless deploy")
    assert (
        "serverless deploy . --remote-build --env "
        + str(env_file)
        + " --include intercom/webhook,intercom-article-drafts/upload,testrail/bridge"
        in calls
    )
    if existing_app:
        assert "apps update app-123" in calls
        assert "apps create" not in calls
    else:
        assert "apps create" in calls
        assert "apps update" not in calls


def test_app_platform_authorization_failure_precedes_any_deployment(tmp_path):
    env_file = tmp_path / ".env"
    _write_env(env_file)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "doctl.log"

    fake_git = fake_bin / "git"
    fake_git.write_text(
        """#!/bin/sh
set -eu
case "$*" in
  "branch --show-current") printf '%s\n' main ;;
  "status --porcelain") exit 0 ;;
  "fetch origin") exit 0 ;;
  "rev-parse HEAD"|"rev-parse origin/main") printf '%s\n' abc123 ;;
  *) printf 'Unexpected git call: %s\n' "$*" >&2; exit 1 ;;
esac
"""
    )
    fake_git.chmod(0o755)

    fake_doctl = fake_bin / "doctl"
    fake_doctl.write_text(
        """#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$FAKE_DOCTL_LOG"
case "$*" in
  "serverless status")
    printf '%s\n' 'namespace fn-c9829d1a-06e2-4af5-9196-b23c58499edc label=edge-tools https://faas-nyc1-2ef2e6cc.doserverless.co'
    ;;
  "apps list --format ID,Spec.Name --no-header") exit 1 ;;
  *) printf 'Unexpected doctl call: %s\n' "$*" >&2; exit 1 ;;
esac
"""
    )
    fake_doctl.chmod(0o755)

    run_env = os.environ.copy()
    run_env.update(
        {
            "PATH": str(fake_bin) + os.pathsep + run_env["PATH"],
            "FAKE_DOCTL_LOG": str(log_path),
        }
    )

    result = subprocess.run(
        [str(DEPLOY_SCRIPT), "--env-file", str(env_file)],
        cwd=PROJECT_ROOT,
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "cannot list App Platform apps" in result.stderr
    calls = log_path.read_text()
    assert "apps list --format ID,Spec.Name --no-header" in calls
    assert "serverless deploy" not in calls

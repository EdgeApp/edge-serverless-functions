import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = PROJECT_ROOT / "scripts" / "deploy-testrail.sh"


def _write_env(path: Path, **overrides: str) -> None:
    values = {
        "TESTRAIL_BASE_URL": "https://edgeapp.testrail.io",
        "TESTRAIL_USER_EMAIL": "operator@example.com",
        "TESTRAIL_API_KEY": "test-api-key",
        "TESTRAIL_BRIDGE_SECRET": "test-bridge-secret",
    }
    values.update(overrides)
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    path.chmod(0o600)


def test_deploy_script_is_executable():
    assert os.access(DEPLOY_SCRIPT, os.X_OK)


def test_check_mode_validates_without_leaving_plaintext_spec(tmp_path):
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
    assert result.stdout == "TestRail deployment preflight passed.\n"
    assert "test-api-key" not in result.stdout + result.stderr
    assert not (PROJECT_ROOT / "services/testrail-files/app-spec.local.yaml").exists()


def test_check_mode_rejects_missing_required_value(tmp_path):
    env_file = tmp_path / ".env"
    _write_env(env_file, TESTRAIL_API_KEY="")

    result = subprocess.run(
        [str(DEPLOY_SCRIPT), "--check", "--env-file", str(env_file)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "TESTRAIL_API_KEY" in result.stderr
    assert "test-bridge-secret" not in result.stdout + result.stderr


class _ReadReceiptHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = b'{"ok": true, "upstream_status": 200}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


@pytest.mark.parametrize("existing_app", [False, True])
def test_deploy_path_creates_or_updates_and_prints_urls(tmp_path, existing_app):
    env_file = tmp_path / ".env"
    _write_env(env_file)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "doctl.log"
    state_path = tmp_path / "app-created"

    fake_doctl = fake_bin / "doctl"
    fake_doctl.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$FAKE_DOCTL_LOG"
case "$*" in
  "serverless status") exit 0 ;;
  "serverless deploy "*) exit 0 ;;
  "serverless functions get testrail/bridge --url") printf '%s\\n' "$FAKE_FUNCTION_URL" ;;
  "apps list --format ID,Spec.Name --no-header")
    if [ "$FAKE_APP_EXISTS" = 1 ] || [ -f "$FAKE_APP_STATE" ]; then
      printf '%s\\n' "app-123 edge-testrail-files"
    fi
    ;;
  "apps create "*) touch "$FAKE_APP_STATE" ;;
  "apps update app-123 "*) exit 0 ;;
  "apps get app-123 --format DefaultIngress --no-header") printf '%s\\n' "files.example.test" ;;
  *) printf 'Unexpected doctl call: %s\\n' "$*" >&2; exit 1 ;;
esac
"""
    )
    fake_doctl.chmod(0o755)

    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 0\n")
    fake_curl.chmod(0o755)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _ReadReceiptHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    function_url = f"http://127.0.0.1:{server.server_port}/bridge"

    run_env = os.environ.copy()
    run_env.update(
        {
            "PATH": str(fake_bin) + os.pathsep + run_env["PATH"],
            "FAKE_APP_EXISTS": "1" if existing_app else "0",
            "FAKE_APP_STATE": str(state_path),
            "FAKE_DOCTL_LOG": str(log_path),
            "FAKE_FUNCTION_URL": function_url,
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
    assert f"JSON bridge URL: {function_url}" in result.stdout
    assert "Attachment bridge URL: https://files.example.test" in result.stdout
    assert "test-api-key" not in result.stdout + result.stderr

    calls = log_path.read_text()
    assert "serverless deploy . --remote-build" in calls
    if existing_app:
        assert "apps update app-123" in calls
        assert "apps create" not in calls
    else:
        assert "apps create" in calls
        assert "apps update" not in calls

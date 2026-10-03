#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$repo_root/.env"
check_only=false

namespace_id="fn-c9829d1a-06e2-4af5-9196-b23c58499edc"
namespace_label="edge-tools"
namespace_host="https://faas-nyc1-2ef2e6cc.doserverless.co"
function_paths="intercom/webhook,intercom-article-drafts/upload,testrail/bridge"

usage() {
  printf 'Usage: %s [--check] [--env-file PATH]\n' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      check_only=true
      shift
      ;;
    --env-file)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      env_file="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

command -v python3 >/dev/null 2>&1 || {
  printf 'Missing required command: python3\n' >&2
  exit 1
}

[[ -f "$env_file" ]] || {
  printf 'Missing environment file: %s\n' "$env_file" >&2
  exit 1
}
[[ ! -L "$env_file" ]] || {
  printf 'Environment file must not be a symlink: %s\n' "$env_file" >&2
  exit 1
}

project_file="$repo_root/project.yml"
template="$repo_root/services/testrail-files/app-spec.example.yaml"
[[ -f "$project_file" ]] || {
  printf 'Missing Functions project file: %s\n' "$project_file" >&2
  exit 1
}
[[ -f "$template" ]] || {
  printf 'Missing App Platform template: %s\n' "$template" >&2
  exit 1
}

umask 077
temp_spec="$(mktemp -t edge-serverless-app-spec.XXXXXX)"
cleanup() {
  rm -f -- "$temp_spec"
}
trap cleanup EXIT HUP INT TERM

python3 - "$env_file" "$project_file" "$template" "$temp_spec" <<'PY'
from pathlib import Path
import json
import re
import sys

env_path = Path(sys.argv[1])
project_path = Path(sys.argv[2])
template_path = Path(sys.argv[3])
output_path = Path(sys.argv[4])

if env_path.stat().st_mode & 0o077:
    raise SystemExit(f"Environment file must be mode 600 or 400: {env_path}")

values = {}
for raw_line in env_path.read_text().splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in raw_line:
        continue
    key, value = raw_line.split("=", 1)
    values[key.strip()] = value

required = sorted(
    set(re.findall(r"\$\{([A-Z0-9_]+)\}", project_path.read_text()))
)
if not required:
    raise SystemExit("project.yml contains no templated environment variables")

missing = [key for key in required if not values.get(key)]
if missing:
    raise SystemExit("Missing non-empty .env entries: " + ", ".join(missing))

base_url = values["TESTRAIL_BASE_URL"].rstrip("/")
if not base_url.startswith("https://"):
    raise SystemExit("TESTRAIL_BASE_URL must use https://")
values["TESTRAIL_BASE_URL"] = base_url

testrail_required = {
    "TESTRAIL_BASE_URL",
    "TESTRAIL_USER_EMAIL",
    "TESTRAIL_API_KEY",
    "TESTRAIL_BRIDGE_SECRET",
}
current_key = None
rendered = []
replaced = set()

for line in template_path.read_text().splitlines():
    stripped = line.strip()
    if stripped.startswith("- key: "):
        current_key = stripped[7:]
    if stripped == "value: REPLACE_ME":
        if current_key not in testrail_required:
            raise SystemExit(f"Unexpected placeholder for {current_key!r}")
        indent = line[: len(line) - len(line.lstrip())]
        line = indent + "value: " + json.dumps(values[current_key])
        replaced.add(current_key)
    rendered.append(line)

if replaced != testrail_required:
    missing_placeholders = sorted(testrail_required - replaced)
    raise SystemExit(
        "Template is missing placeholders for: " + ", ".join(missing_placeholders)
    )

output_path.write_text("\n".join(rendered) + "\n")
PY

if [[ "$check_only" == true ]]; then
  printf 'Repository deployment preflight passed.\n'
  exit 0
fi

for command_name in git doctl; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'Missing required command: %s\n' "$command_name" >&2
    exit 1
  }
done

cd "$repo_root"

[[ "$(git branch --show-current)" == "main" ]] || {
  printf 'Refusing deployment: checkout is not on main.\n' >&2
  exit 1
}
[[ -z "$(git status --porcelain)" ]] || {
  printf 'Refusing deployment: working tree is not clean.\n' >&2
  exit 1
}
git fetch origin
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || {
  printf 'Refusing deployment: local main does not match origin/main.\n' >&2
  exit 1
}
deployed_sha="$(git rev-parse HEAD)"

namespace_status="$(doctl serverless status)"
printf '%s\n' "$namespace_status"
printf '%s\n' "$namespace_status" | grep -Fq "$namespace_id" || {
  printf 'Refusing deployment: wrong Functions namespace ID.\n' >&2
  exit 1
}
printf '%s\n' "$namespace_status" | grep -Fq "label=$namespace_label" || {
  printf 'Refusing deployment: wrong Functions namespace label.\n' >&2
  exit 1
}
printf '%s\n' "$namespace_status" | grep -Fq "$namespace_host" || {
  printf 'Refusing deployment: wrong Functions API host.\n' >&2
  exit 1
}

if ! apps_inventory="$(
  doctl apps list --format ID,Spec.Name --no-header
)"; then
  printf 'Refusing deployment: the current doctl credential cannot list App Platform apps.\n' >&2
  printf 'Use a DigitalOcean API token with app:read, app:create, and app:update access.\n' >&2
  exit 1
fi

app_matches="$(
  printf '%s\n' "$apps_inventory" |
    awk '$2 == "edge-testrail-files" {print $1}'
)"
app_count="$(printf '%s\n' "$app_matches" | sed '/^$/d' | wc -l | tr -d ' ')"

if [[ "$app_count" -gt 1 ]]; then
  printf 'Multiple App Platform apps named edge-testrail-files exist; refusing to guess.\n' >&2
  exit 1
fi

printf 'Deploying repository revision %s to %s.\n' "$deployed_sha" "$namespace_label"
doctl serverless deploy . \
  --remote-build \
  --env "$env_file" \
  --include "$function_paths"

resolve_function_url() {
  doctl serverless functions get "$1" --url |
    awk '/^https:\/\// || /^http:\/\/127\.0\.0\.1:/ {url=$0} END {print url}' |
    tr -d '\r'
}

webhook_url="$(resolve_function_url intercom/webhook)"
drafts_url="$(resolve_function_url intercom-article-drafts/upload)"
testrail_url="$(resolve_function_url testrail/bridge)"

for resolved_url in "$webhook_url" "$drafts_url" "$testrail_url"; do
  [[ "$resolved_url" == https://* || "$resolved_url" == http://127.0.0.1:* ]] || {
    printf 'Could not resolve a deployed Function URL. Got: %s\n' "$resolved_url" >&2
    exit 1
  }
done

if [[ "$app_count" -eq 1 ]]; then
  app_id="$app_matches"
  doctl apps update "$app_id" \
    --spec "$temp_spec" \
    --update-sources \
    --wait
else
  doctl apps create --spec "$temp_spec" --wait
  app_id="$(
    doctl apps list --format ID,Spec.Name --no-header |
      awk '$2 == "edge-testrail-files" {print $1; exit}'
  )"
fi

[[ -n "${app_id:-}" ]] || {
  printf 'Could not resolve the TestRail attachment app ID.\n' >&2
  exit 1
}

files_url="$(
  doctl apps get "$app_id" --format DefaultIngress --no-header |
    tr -d '\r'
)"
[[ -n "$files_url" ]] || {
  printf 'Could not resolve the TestRail attachment URL.\n' >&2
  exit 1
}
if [[ "$files_url" != http://* && "$files_url" != https://* ]]; then
  files_url="https://$files_url"
fi

python3 - \
  "$env_file" \
  "$webhook_url" \
  "$drafts_url" \
  "$testrail_url" \
  "$files_url" <<'PY'
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
import sys
import time

env_path = Path(sys.argv[1])
webhook_url, drafts_url, testrail_url, files_url = sys.argv[2:]

values = {}
for raw_line in env_path.read_text().splitlines():
    if not raw_line.strip() or raw_line.lstrip().startswith("#") or "=" not in raw_line:
        continue
    key, value = raw_line.split("=", 1)
    values[key.strip()] = value


def fetch(request, expected_status, attempts=1):
    last_error = None
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=30) as response:
                status = response.status
                body = response.read()
        except HTTPError as error:
            status = error.code
            body = error.read()
        except URLError as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(5)
                continue
            raise SystemExit(f"Verification request failed: {error}")
        if status == expected_status:
            return body
        last_error = RuntimeError(f"expected HTTP {expected_status}, got {status}")
        if attempt + 1 < attempts:
            time.sleep(5)
    raise SystemExit(f"Verification request failed: {last_error}")


fetch(Request(webhook_url, method="HEAD"), 200)
fetch(
    Request(
        drafts_url,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    ),
    401,
)

receipt_body = fetch(
    Request(
        testrail_url,
        data=json.dumps({"endpoint": "get_projects"}).encode(),
        headers={
            "Authorization": "Bearer " + values["TESTRAIL_BRIDGE_SECRET"],
            "Content-Type": "application/json",
        },
        method="POST",
    ),
    200,
)
try:
    receipt = json.loads(receipt_body)
except (TypeError, ValueError):
    raise SystemExit("TestRail JSON bridge returned malformed verification JSON")
if not receipt.get("ok") or receipt.get("upstream_status") != 200:
    raise SystemExit("TestRail JSON bridge read verification failed")

health_body = fetch(Request(files_url + "/health"), 200, attempts=12)
try:
    health = json.loads(health_body)
except (TypeError, ValueError):
    raise SystemExit("TestRail attachment bridge returned malformed health JSON")
if health.get("ok") is not True:
    raise SystemExit("TestRail attachment bridge health verification failed")
PY

printf '\nRepository deployment verified.\n'
printf 'DEPLOYED_SHA=%s\n' "$deployed_sha"
printf 'Intercom webhook URL: %s\n' "$webhook_url"
printf 'Intercom article-draft bridge URL: %s\n' "$drafts_url"
printf 'TestRail JSON bridge URL: %s\n' "$testrail_url"
printf 'TestRail attachment bridge URL: %s\n' "$files_url"

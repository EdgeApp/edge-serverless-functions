#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$repo_root/.env"
check_only=false

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

for command_name in python3; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'Missing required command: %s\n' "$command_name" >&2
    exit 1
  }
done

[[ -f "$env_file" ]] || {
  printf 'Missing environment file: %s\n' "$env_file" >&2
  exit 1
}
[[ ! -L "$env_file" ]] || {
  printf 'Environment file must not be a symlink: %s\n' "$env_file" >&2
  exit 1
}

template="$repo_root/services/testrail-files/app-spec.example.yaml"
[[ -f "$template" ]] || {
  printf 'Missing App Platform template: %s\n' "$template" >&2
  exit 1
}

umask 077
temp_spec="$(mktemp -t edge-testrail-app-spec.XXXXXX)"
cleanup() {
  rm -f -- "$temp_spec"
}
trap cleanup EXIT HUP INT TERM

python3 - "$env_file" "$template" "$temp_spec" <<'PY'
from pathlib import Path
import json
import sys

env_path = Path(sys.argv[1])
template_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])

if env_path.stat().st_mode & 0o077:
    raise SystemExit(f"Environment file must be mode 600: {env_path}")

required = (
    "TESTRAIL_BASE_URL",
    "TESTRAIL_USER_EMAIL",
    "TESTRAIL_API_KEY",
    "TESTRAIL_BRIDGE_SECRET",
)

values = {}
for raw_line in env_path.read_text().splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in raw_line:
        continue
    key, value = raw_line.split("=", 1)
    values[key.strip()] = value

missing = [key for key in required if not values.get(key)]
if missing:
    raise SystemExit("Missing non-empty .env entries: " + ", ".join(missing))

base_url = values["TESTRAIL_BASE_URL"].rstrip("/")
if not base_url.startswith("https://"):
    raise SystemExit("TESTRAIL_BASE_URL must use https://")
values["TESTRAIL_BASE_URL"] = base_url

current_key = None
rendered = []
replaced = set()

for line in template_path.read_text().splitlines():
    stripped = line.strip()
    if stripped.startswith("- key: "):
        current_key = stripped[7:]
    if stripped == "value: REPLACE_ME":
        if current_key not in required:
            raise SystemExit(f"Unexpected placeholder for {current_key!r}")
        indent = line[: len(line) - len(line.lstrip())]
        line = indent + "value: " + json.dumps(values[current_key])
        replaced.add(current_key)
    rendered.append(line)

if replaced != set(required):
    missing_placeholders = sorted(set(required) - replaced)
    raise SystemExit(
        "Template is missing placeholders for: " + ", ".join(missing_placeholders)
    )

output_path.write_text("\n".join(rendered) + "\n")
PY

if [[ "$check_only" == true ]]; then
  printf 'TestRail deployment preflight passed.\n'
  exit 0
fi

for command_name in doctl curl; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'Missing required command: %s\n' "$command_name" >&2
    exit 1
  }
done

cd "$repo_root"

doctl serverless status >/dev/null
doctl serverless deploy . \
  --remote-build \
  --env "$env_file" \
  --include testrail/bridge

function_url="$(
  doctl serverless functions get testrail/bridge --url |
    awk '/^https:\/\// || /^http:\/\/127\.0\.0\.1:/ {url=$0} END {print url}' |
    tr -d '\r'
)"
[[ "$function_url" == https://* || "$function_url" == http://127.0.0.1:* ]] || {
  printf 'Could not resolve the JSON bridge URL. Got: %s\n' "$function_url" >&2
  exit 1
}

app_matches="$(
  doctl apps list --format ID,Spec.Name --no-header |
    awk '$2 == "edge-testrail-files" {print $1}'
)"
app_count="$(printf '%s\n' "$app_matches" | sed '/^$/d' | wc -l | tr -d ' ')"

if [[ "$app_count" -gt 1 ]]; then
  printf 'Multiple App Platform apps named edge-testrail-files exist; refusing to guess.\n' >&2
  exit 1
elif [[ "$app_count" -eq 1 ]]; then
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
  printf 'Could not resolve the attachment bridge app ID.\n' >&2
  exit 1
}

files_url="$(
  doctl apps get "$app_id" --format DefaultIngress --no-header |
    tr -d '\r'
)"
[[ -n "$files_url" ]] || {
  printf 'Could not resolve the attachment bridge URL.\n' >&2
  exit 1
}
if [[ "$files_url" != http://* && "$files_url" != https://* ]]; then
  files_url="https://$files_url"
fi

curl \
  --fail \
  --silent \
  --show-error \
  --retry 12 \
  --retry-all-errors \
  --retry-delay 5 \
  --max-time 20 \
  "$files_url/health" >/dev/null

python3 - "$env_file" "$function_url" <<'PY'
from pathlib import Path
from urllib.request import Request, urlopen
import json
import sys

env_path = Path(sys.argv[1])
function_url = sys.argv[2]

values = {}
for raw_line in env_path.read_text().splitlines():
    if not raw_line.strip() or raw_line.lstrip().startswith("#") or "=" not in raw_line:
        continue
    key, value = raw_line.split("=", 1)
    values[key.strip()] = value

request = Request(
    function_url,
    data=json.dumps({"endpoint": "get_projects"}).encode(),
    headers={
        "Authorization": "Bearer " + values["TESTRAIL_BRIDGE_SECRET"],
        "Content-Type": "application/json",
    },
    method="POST",
)

with urlopen(request, timeout=30) as response:
    receipt = json.load(response)

if not receipt.get("ok") or receipt.get("upstream_status") != 200:
    raise SystemExit("JSON bridge read verification failed")
PY

printf '\nTestRail deployment verified.\n'
printf 'JSON bridge URL: %s\n' "$function_url"
printf 'Attachment bridge URL: %s\n' "$files_url"

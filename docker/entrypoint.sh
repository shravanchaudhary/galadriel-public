#!/bin/sh
set -eu

efs_root="${GALADRIEL_EFS_ROOT:-/mnt/efs}"

if [ "${APPCONFIG_REQUIRED:-false}" = "true" ]; then
    : "${APPCONFIG_APPLICATION:?APPCONFIG_APPLICATION is required}"
    : "${APPCONFIG_ENVIRONMENT:?APPCONFIG_ENVIRONMENT is required}"
    : "${APPCONFIG_CONFIGURATION:?APPCONFIG_CONFIGURATION is required}"
    appconfig_file="/tmp/clyra-appconfig.env"
    appconfig_url="${APPCONFIG_AGENT_URL:-http://127.0.0.1:2772}/applications/${APPCONFIG_APPLICATION}/environments/${APPCONFIG_ENVIRONMENT}/configurations/${APPCONFIG_CONFIGURATION}"
    attempt=0
    until python3 - "$appconfig_url" "$appconfig_file" <<'PY'
import json
import shlex
import sys
from urllib.request import urlopen

url, target = sys.argv[1:]
with urlopen(url, timeout=2) as response:
    content = response.read().decode("utf-8")
try:
    values = json.loads(content)
except json.JSONDecodeError:
    lines = [
        line for line in content.splitlines()
        if line and not line.lstrip().startswith("#")
    ]
else:
    if not isinstance(values, dict) or not all(isinstance(k, str) for k in values):
        raise ValueError("AppConfig content must be a JSON object or dotenv file")
    lines = [f"{key}={shlex.quote(str(value))}" for key, value in values.items()]
with open(target, "w", encoding="utf-8") as output:
    output.write("\n".join(lines) + "\n")
PY
    do
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 30 ]; then
            echo "AppConfig Agent did not provide runtime configuration" >&2
            exit 1
        fi
        sleep 1
    done
    set -a
    # shellcheck disable=SC1090
    . "$appconfig_file"
    set +a
fi

for dir in data memory config state jobs workflows completion-markers; do
    mkdir -p "$efs_root/$dir"
done

# Seed files added by a new image without overwriting state already persisted
# on EFS. `cp -an` is deliberately idempotent across task replacements.
for dir in config memory state jobs workflows; do
    if [ -d "/opt/galadriel-defaults/$dir" ]; then
        cp -an "/opt/galadriel-defaults/$dir/." "$efs_root/$dir/"
    fi
done

# MemPalace creates its own directory tree on demand. Initializing it here
# makes first boot deterministic but remains safe when the palace exists.
if command -v mempalace >/dev/null 2>&1 && [ ! -d "${MEMPALACE_PATH:-/data/.mempalace/palace}" ]; then
    mempalace init --yes --no-llm /data \
        || echo "MemPalace initialization deferred to application startup" >&2
fi

exec "$@"

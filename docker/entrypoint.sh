#!/bin/sh
set -eu

storage_root="${GALADRIEL_STORAGE_ROOT:-${GALADRIEL_EFS_ROOT:-/mnt/efs}}"
defaults_root="${GALADRIEL_DEFAULTS_ROOT:-/opt/galadriel-defaults}"
app_root="${GALADRIEL_APP_ROOT:-/app}"

if [ "${APPCONFIG_REQUIRED:-false}" = "true" ]; then
    : "${APPCONFIG_APPLICATION:?APPCONFIG_APPLICATION is required}"
    : "${APPCONFIG_ENVIRONMENT:?APPCONFIG_ENVIRONMENT is required}"
    : "${APPCONFIG_CONFIGURATION:?APPCONFIG_CONFIGURATION is required}"
    appconfig_file="${TMPDIR:-/tmp}/clyra-appconfig.env"
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

if [ "${REPLIKA_CONTROL_PLANE_ONLY:-false}" = "true" ]; then
    exec "$@"
fi

if [ -n "${REPLIKA_TENANT_ID:-}" ] && [ "${REPLIKA_TENANT_ID}" != "default" ]; then
    tenant_db_id="$(printf '%s' "$REPLIKA_TENANT_ID" | tr -cd 'A-Za-z0-9_-')"
    if [ -z "$tenant_db_id" ]; then
        echo "REPLIKA_TENANT_ID cannot produce a safe tenant database name" >&2
        exit 1
    fi
    MONGO_DB="${REPLIKA_MONGO_DB_PREFIX:-replika_}${tenant_db_id}"
    export MONGO_DB
fi

for dir in data memory config knowledge state jobs workflows personal-tools completion-markers; do
    mkdir -p "$storage_root/$dir"
done

if [ "${PHONE_BRIDGE_ENABLED:-0}" = "1" ]; then
    adb_dir="${ANDROID_USER_HOME:-$storage_root/data/.android}"
    adb_key="${ADB_VENDOR_KEYS:-$adb_dir/adbkey}"
    mkdir -p "$adb_dir" "${TMPDIR:-$storage_root/data/tmp}"
    chmod 700 "$adb_dir"
    if [ ! -f "$adb_key" ]; then
        adb keygen "$adb_key"
    fi
    chmod 600 "$adb_key"
    if [ -f "$adb_key.pub" ]; then
        chmod 600 "$adb_key.pub"
    fi
fi

python3 "$app_root/scripts/migrate_replika_state.py" --root "$storage_root"

# Seed files added by a new image without overwriting state already persisted
# on persistent storage. `cp -an` is deliberately idempotent across replacements.
for dir in config knowledge memory state jobs workflows personal-tools; do
    if [ -d "$defaults_root/$dir" ]; then
        python3 - "$defaults_root/$dir" "$storage_root/$dir" <<'PY'
import shutil
import sys
from pathlib import Path

source_root, target_root = map(Path, sys.argv[1:])
for source in source_root.rglob("*"):
    target = target_root / source.relative_to(source_root)
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
    elif not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
PY
    fi
done

# MemPalace creates its own directory tree on demand. Initializing it here
# makes first boot deterministic but remains safe when the palace exists.
if command -v mempalace >/dev/null 2>&1 && [ ! -d "${MEMPALACE_PATH:-/data/.mempalace/palace}" ]; then
    mempalace init --yes --no-llm /data \
        || echo "MemPalace initialization deferred to application startup" >&2
fi

exec "$@"

#!/usr/bin/env bash
# ============================================================
# reset_palace.sh — Wipe all memory and start from a blank slate
#
# Run as: bash cmd/reset_palace.sh         (prompts for confirmation)
#         bash cmd/reset_palace.sh --yes   (skip the prompt)
#
# The palace lives in MongoDB / DocumentDB, not on disk. This drops those
# collections outright:
#   palace_drawers           all memory: conversations, knowledge, procedures, episodes, preferences
#   palace_knowledge_graph   every KG triple
#   palace_chunk_counters    per-conversation chunk numbering
#   palace_archive_cursors   per-channel archive position
#   palace_outbox            pending mine jobs
#
# It also deletes daily logs (memory/*.md, gitignored) and resets
# config/MEMORY.md to its git-committed state.
#
# DESTRUCTIVE AND IRREVERSIBLE. There is no backup step: the drawers are in a
# remote database, so `mv` cannot save them the way it saved the old on-disk
# palace. Take a mongodump first if you might want any of it back.
#
# Staged archives under PALACE_ARCHIVE_ROOT are left alone — they are the only
# on-disk source that can be re-mined afterwards.
#
# Stop the bot BEFORE running this so nothing writes mid-wipe.
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_DIR"

if [[ -x "${REPO_DIR}/venv/bin/python" ]]; then
    PYTHON="${REPO_DIR}/venv/bin/python"
elif [[ -x "${REPO_DIR}/.venv/bin/python" ]]; then
    PYTHON="${REPO_DIR}/.venv/bin/python"
else
    PYTHON="python3"
fi

TARGET="$("$PYTHON" - <<'PY'
import os
from dotenv import load_dotenv
load_dotenv(".env")
print(os.environ.get("MONGO_DB") or "(MONGO_DB unset)")
PY
)"

echo "🧹 Galadriel memory reset — FULL BLANK SLATE"
echo ""
echo "  Repo:      ${REPO_DIR}"
echo "  Database:  ${TARGET}"
echo "  Python:    ${PYTHON}"
echo ""
echo "  This will:"
echo "    1. Drop the palace collections in ${TARGET}"
echo "    2. Delete daily logs (memory/*.md) — gitignored, unrecoverable"
echo "    3. Reset config/MEMORY.md to its git-committed state"
echo ""
echo "  ⚠️  No backup is taken. The drawers live in a remote database — run a"
echo "      mongodump first if you might want any of this back. Stop the bot"
echo "      before continuing."
echo ""

if [[ "${1:-}" != "--yes" ]]; then
    read -r -p "Type 'wipe' to proceed: " CONFIRM
    if [[ "$CONFIRM" != "wipe" ]]; then
        echo "Aborted."
        exit 1
    fi
fi

# 1. Drop the palace collections.
"$PYTHON" - <<'PY'
import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(".env")
uri, name = os.environ.get("MONGO_URI"), os.environ.get("MONGO_DB")
if not uri or not name:
    raise SystemExit("MONGO_URI / MONGO_DB are not set — nothing to reset.")
database = MongoClient(uri)[name]
for collection in (
    "palace_drawers",
    "palace_knowledge_graph",
    "palace_chunk_counters",
    "palace_archive_cursors",
    "palace_outbox",
):
    count = database[collection].count_documents({})
    database[collection].drop()
    print(f"✅ Dropped {collection} ({count} document(s))")
PY

# 2. Delete daily logs (keep the .gitkeep so the dir survives).
if compgen -G "memory/*.md" > /dev/null; then
    rm -f memory/*.md
    echo "✅ Deleted daily logs (memory/*.md)"
else
    echo "ℹ️  No daily logs to delete."
fi
rm -f memory/*.json 2>/dev/null || true

# 3. Reset curated long-term memory to the committed version.
if git -C "$REPO_DIR" rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    if git -C "$REPO_DIR" ls-files --error-unmatch config/MEMORY.md > /dev/null 2>&1; then
        git -C "$REPO_DIR" checkout -- config/MEMORY.md
        echo "✅ Reset config/MEMORY.md to git-committed state"
    fi
fi

echo ""
echo "✅ Done. The palace rebuilds itself as the agent runs — indexes are"
echo "   created on first write. To re-import conversations that are still"
echo "   staged on disk:"
echo "     python scripts/migrate_palace_bge.py            # dry run"
echo "     python scripts/migrate_palace_bge.py --apply"
echo ""
echo "   Restart the bot:"
echo "     python main.py        # or: sudo systemctl restart galadriel"

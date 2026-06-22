#!/usr/bin/env bash
# ============================================================
# reset_palace.sh — Wipe all memory and rebuild from scratch
#
# Run as: bash cmd/reset_palace.sh         (prompts for confirmation)
#         bash cmd/reset_palace.sh --yes   (skip the prompt)
#
# FULL BLANK SLATE. This is destructive and irreversible for:
#   - Daily logs (memory/*.md)          — gitignored, NOT in git
#   - Palace-only data: diary entries, agent-filed drawers, KG facts
#                                          (no on-disk source to re-mine)
# config/MEMORY.md is reset to its git-committed state.
# Everything else (the vector index) is rebuilt by re-mining the repo.
#
# Stop the bot BEFORE running this so nothing writes mid-wipe.
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_DIR"

# Prefer the repo's venv binary; fall back to whatever is on PATH.
if [[ -x "${REPO_DIR}/venv/bin/mempalace" ]]; then
    MEMPALACE="${REPO_DIR}/venv/bin/mempalace"
else
    MEMPALACE="mempalace"
fi

# Palace lives at $MEMPALACE_PATH's parent, or ~/.mempalace by default.
PALACE_HOME="${HOME}/.mempalace"

echo "🧹 Galadriel memory reset — FULL BLANK SLATE"
echo ""
echo "  Repo:        ${REPO_DIR}"
echo "  Palace home: ${PALACE_HOME}"
echo "  mempalace:   ${MEMPALACE}"
echo ""
echo "  This will:"
echo "    1. Back up ${PALACE_HOME} → ${PALACE_HOME}.bak-<timestamp>"
echo "    2. Delete daily logs (memory/*.md) — gitignored, unrecoverable"
echo "    3. Reset config/MEMORY.md to its git-committed state"
echo "    4. Re-init + re-mine the repo into a fresh palace"
echo ""
echo "  ⚠️  Diary entries, agent-filed drawers, and KG facts have no on-disk"
echo "      source and will NOT come back (the backup in step 1 is your only"
echo "      recovery path). Stop the bot before continuing."
echo ""

if [[ "${1:-}" != "--yes" ]]; then
    read -r -p "Type 'wipe' to proceed: " CONFIRM
    if [[ "$CONFIRM" != "wipe" ]]; then
        echo "Aborted."
        exit 1
    fi
fi

# 1. Back up + wipe the palace in one move.
if [[ -d "$PALACE_HOME" ]]; then
    BACKUP="${PALACE_HOME}.bak-$(date +%Y%m%d-%H%M%S)"
    mv "$PALACE_HOME" "$BACKUP"
    echo "✅ Backed up old palace to ${BACKUP}"
else
    echo "ℹ️  No existing palace at ${PALACE_HOME} — nothing to back up."
fi

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

# Stale per-project entity registry (regenerated on mine).
rm -f "${REPO_DIR}/entities.json"

# 4. Re-init + re-mine.
echo ""
echo "🏰 Rebuilding the palace..."
"$MEMPALACE" init .
echo ""
"$MEMPALACE" status

echo ""
echo "✅ Done. Restart the bot to pick up the fresh palace:"
echo "    python main.py        # or: sudo systemctl restart galadriel"

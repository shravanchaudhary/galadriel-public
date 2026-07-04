"""Permission tiers and safety guardrails for the agent."""

import os
import re

# Green: auto-execute without asking
# Yellow: notify user, proceed unless vetoed within timeout
# Red: require explicit approval before executing

GREEN_PATTERNS = [
    r"^ls\b",
    r"^cat\b(?!.*[^\d&]>)",  # read-only; a `cat ... > file` write is not green (`2>`/`&>` redirs still ok)
    r"^head\b",
    r"^tail\b",
    r"^grep\b",
    r"^find\b",
    r"^pwd$",
    r"^echo\b",
    r"^date$",
    r"^whoami$",
    r"^git\s+(status|log|diff|branch|show|remote)",
    r"^git\s+fetch\b",
    r"^aws\s+(s3\s+ls|dynamodb\s+describe|ec2\s+describe|sts\s+get|cloudformation\s+describe|cloudformation\s+list|ce\s+get)",
    r"^python3?\s+.*\.(py)\s*$",
    r"^pip\s+(list|show|freeze)",
    r"^sam\s+(validate|build)",
    r"^df\b",
    r"^free\b",
    r"^uptime$",
    r"^wc\b",
    r"^sort\b",
    r"^du\b",
]

YELLOW_PATTERNS = [
    r"^git\s+(add|commit|push|pull|merge|checkout|switch)",
    r"^sam\s+deploy",
    r"^aws\s+s3\s+(cp|mv|sync)",
    r"^aws\s+dynamodb\s+(put-item|update-item|batch-write)",
    r"^aws\s+lambda\s+update",
    r"^pip\s+install",
    r"^sudo\s+systemctl\b",
]

RED_PATTERNS = [
    r"^rm\s",
    r"^sudo\s+rm\b",
    r"^aws\s+iam\b",
    r"^aws\s+cognito",
    r"^aws\s+secretsmanager\s+(put|update|delete)",
    r"^aws\s+cloudformation\s+(create|update|delete)",
    r"^aws\s+ec2\s+(terminate|stop|run|modify)",
    r"^aws\s+s3\s+rb\b",
    r"^aws\s+dynamodb\s+(create|delete)-table",
    r"^git\s+(push\s+--force|reset\s+--hard)",
    r"^shutdown\b",
    r"^reboot\b",
    r"curl.*\|\s*(bash|sh)",
]


# Freestyle DB access is no longer allowed: the agent must touch MongoDB only
# through the db_* primitive tools (harness/db_ops.py), never raw pymongo/mongosh
# in run_shell. These patterns detect a shell command trying to reach the DB
# directly so run_shell can refuse it.
DB_FREESTYLE_PATTERNS = [
    r"\bmongosh\b",
    r"\bmongo\b\s+(mongodb|--)",
    r"\bimport\s+pymongo\b",
    r"\bfrom\s+pymongo\b",
    r"\bfrom\s+lib\.db\b",
    r"\bimport\s+lib\.db\b",
    r"\blib\.db\b",
    r"\bget_db\s*\(",
    r"\b(Async)?MongoClient\b",
]


def is_db_freestyle(command: str) -> bool:
    """True if a shell command tries to touch MongoDB directly (forbidden — use
    the db_* primitive tools instead)."""
    cmd = command or ""
    return any(re.search(p, cmd) for p in DB_FREESTYLE_PATTERNS)


# A bare `rm [-f] <path>` with no other flags, globs, or shell tricks appended
# (no `;`, `|`, `&`, redirects, `$()`/backticks, wildcards). Anything more exotic
# (recursive, multiple targets, compound commands) never qualifies for the
# self-created-file allowance below and stays red.
_SIMPLE_RM_RE = re.compile(r"^rm\s+(?:-f\s+)?([^\s;&|<>$`*?\[\]{}~]+)\s*$")


def _simple_rm_target(command: str) -> str | None:
    """Return the target path if `command` is a bare `rm [-f] <path>`, else None."""
    match = _SIMPLE_RM_RE.match(command.strip())
    return match.group(1) if match else None


def is_self_created_file_deletion(command: str, created_files: set, working_dir: str) -> bool:
    """True if `command` only deletes a file the agent itself created earlier in
    this run (tracked by the caller via write_file) — safe to auto-approve.

    Deliberately narrow: only a single, literal `rm [-f] <path>` qualifies, and
    the resolved path must be one the agent wrote into existence (not merely
    edited) during this process's lifetime. Anything pre-existing, gitignored,
    or reached via a compound/glob command stays red.
    """
    if not created_files:
        return False
    target = _simple_rm_target(command)
    if target is None:
        return False
    resolved = os.path.normpath(os.path.join(working_dir or ".", target))
    return resolved in created_files


def classify_command(command: str, created_files: set = None, working_dir: str = ".") -> str:
    """Classify a shell command into a permission tier.

    `created_files` (optional) is the set of absolute paths the agent has
    written into existence during this run — deleting exactly one of them via
    a plain `rm` is auto-approved instead of red.
    """
    cmd = command.strip()
    if is_self_created_file_deletion(cmd, created_files, working_dir):
        return "green"
    for pattern in RED_PATTERNS:
        if re.search(pattern, cmd):
            return "red"
    for pattern in YELLOW_PATTERNS:
        if re.search(pattern, cmd):
            return "yellow"
    for pattern in GREEN_PATTERNS:
        if re.search(pattern, cmd):
            return "green"
    # Unknown commands default to yellow
    return "yellow"


def format_safety_notice(command: str, tier: str) -> str:
    """Format a human-readable safety notice for a command."""
    icons = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
    labels = {
        "green": "Auto-approved",
        "yellow": "Notify & proceed",
        "red": "Requires approval",
    }
    return f"{icons[tier]} **{labels[tier]}**: `{command}`"

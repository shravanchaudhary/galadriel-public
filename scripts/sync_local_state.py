#!/usr/bin/env python3
"""Refresh local cached state from developer-owned repository defaults."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.local_state import (  # noqa: E402
    local_state_root,
    refresh_changed_defaults,
)


def main() -> int:
    target = local_state_root(ROOT)
    target.mkdir(parents=True, exist_ok=True)
    copied = refresh_changed_defaults(ROOT, target)
    print(f"Updated {len(copied)} changed developer defaults in {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

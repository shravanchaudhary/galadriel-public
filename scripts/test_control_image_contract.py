#!/usr/bin/env python3
"""Dependency-light checks for control-plane provisioning packaging."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
dockerfile = (ROOT / "Dockerfile.control").read_text(encoding="utf-8")
assert "harness/tenant_database.py" in dockerfile, (
    "control image must package the database identity broker"
)

from harness.tenant_database import database_name, iam_runtime_uri  # noqa: E402


assert database_name("tenant_123") == "replika_tenant_123"
uri = iam_runtime_uri(
    "mongodb://admin:secret@docdb.example:27017/admin?tls=true&retryWrites=false"
)
assert "admin:secret" not in uri
assert "authMechanism=MONGODB-AWS" in uri

print("control image provisioning contract checks passed")

"""Dependency-light checks for V0 persistence and credential boundaries."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.path_policy import assert_agent_readable, assert_agent_writable  # noqa: E402
from harness import provider_credentials  # noqa: E402
from harness.tenant_database import (  # noqa: E402
    delete_tenant_identity,
    ensure_tenant_identity,
)
from scripts.migrate_replika_state import migrate  # noqa: E402


class _Result:
    deleted_count = 1


class _Collection:
    def __init__(self):
        self.docs = {}

    def replace_one(self, query, document, upsert=False):
        assert upsert
        self.docs[query["_id"]] = document

    def find_one(self, query):
        row = self.docs.get(query["_id"])
        if row and row["tenant_id"] == query["tenant_id"]:
            return row
        return None

    def find(self, query, projection):
        assert projection == {"ciphertext": 0}
        return [
            row for row in self.docs.values()
            if row["tenant_id"] == query["tenant_id"]
        ]

    def delete_one(self, query):
        self.docs.pop(query["_id"], None)
        return _Result()


class _DB:
    def __init__(self):
        self.collection = _Collection()

    def __getitem__(self, _name):
        return self.collection


class _KMS:
    def __init__(self):
        self.contexts = []

    def encrypt(self, KeyId, Plaintext, EncryptionContext):
        assert KeyId == "test-key"
        self.contexts.append(EncryptionContext)
        return {"CiphertextBlob": b"encrypted:" + Plaintext}

    def decrypt(self, CiphertextBlob, EncryptionContext):
        self.contexts.append(EncryptionContext)
        assert CiphertextBlob.startswith(b"encrypted:")
        return {"Plaintext": CiphertextBlob.removeprefix(b"encrypted:")}


class _ExternalDatabase:
    def __init__(self):
        self.commands = []

    def command(self, command):
        self.commands.append(command)


class _MongoClient:
    def __init__(self):
        self.external = _ExternalDatabase()
        self.dropped = []

    def __getitem__(self, name):
        assert name == "$external"
        return self.external

    def drop_database(self, name):
        self.dropped.append(name)


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp).resolve()
    os.environ["GALADRIEL_STORAGE_ROOT"] = str(root)
    os.environ["GALADRIEL_ENFORCE_WRITE_BOUNDARIES"] = "true"
    os.environ["REPLIKA_MANAGED_RUNTIME"] = "true"
    os.environ["GALADRIEL_CORE_ROOT"] = str(ROOT)
    for name in ("memory", "personal-tools"):
        (root / name).mkdir()
    (root / "config").mkdir()
    (root / "jobs").mkdir()
    (root / "knowledge/reference").mkdir(parents=True)
    (root / "config/JOBS.md").write_text(
        "# Jobs\n\nKeep this goal.\n"
        "| End of Day State Commit | 23:55 | `jobs/daily_state_commit.md` |\n",
        encoding="utf-8",
    )
    (root / "jobs/daily_state_commit.md").write_text(
        "# Daily State Commit\n\nRun git add state/ config/ memory/ jobs/.\n",
        encoding="utf-8",
    )
    (root / "knowledge/reference/architecture.md").write_text(
        "# Architecture\n\nKeep this intro.\n\n"
        "## Git discipline\n\nCommit every change.\n\n"
        "## Storage\n\nKeep this section.\n",
        encoding="utf-8",
    )

    _assert(
        assert_agent_writable(str(root / "memory" / "today.md")).is_relative_to(root),
        "persistent Markdown should be writable",
    )
    _assert(
        assert_agent_writable(str(root / "personal-tools" / "tool.py")).is_relative_to(root),
        "personal tools should be writable",
    )
    try:
        assert_agent_writable(str(ROOT / "harness" / "agent.py"))
    except PermissionError:
        pass
    else:
        raise AssertionError("managed harness must not be writable")

    outside = root.parent / "outside"
    (root / "state").mkdir()
    (root / "state" / "escape").symlink_to(outside)
    try:
        assert_agent_writable(str(root / "state" / "escape" / "file"))
    except PermissionError:
        pass
    else:
        raise AssertionError("symlink escape must be blocked")
    try:
        assert_agent_readable("/proc/1/environ")
    except PermissionError:
        pass
    else:
        raise AssertionError("managed agents must not read process secrets")

    first = migrate(root)
    second = migrate(root)
    _assert(first["applied"] == [1, 2], "first migration should apply all state versions")
    _assert(second["applied"] == [], "migration must be idempotent")
    _assert((root / "personal-tools").is_dir(), "personal tool directory must persist")
    _assert(
        not (root / "jobs/daily_state_commit.md").exists(),
        "obsolete self-commit job must be removed",
    )
    _assert(
        "Keep this goal." in (root / "config/JOBS.md").read_text(encoding="utf-8"),
        "migration must preserve unrelated tenant instructions",
    )
    architecture = (root / "knowledge/reference/architecture.md").read_text(
        encoding="utf-8"
    )
    _assert("Git discipline" not in architecture, "Git policy section must be removed")
    _assert("Keep this section." in architecture, "later sections must be preserved")

os.environ["REPLIKA_TENANT_ID"] = "tenant-a"
os.environ["REPLIKA_KMS_KEY_ID"] = "test-key"
db = _DB()
kms = _KMS()
saved = provider_credentials.put("anthropic", "sk-secret-1234", kms_client=kms, db=db)
_assert(saved["masked"] == "****1234", "only a fingerprint may be returned")
stored = next(iter(db.collection.docs.values()))
_assert("sk-secret" not in str(stored), "plaintext key must not be stored")
_assert(
    provider_credentials.get("anthropic", kms_client=kms, db=db) == "sk-secret-1234",
    "assigned tenant should decrypt its key",
)
os.environ["REPLIKA_TENANT_ID"] = "tenant-b"
_assert(
    provider_credentials.get("anthropic", kms_client=kms, db=db) is None,
    "another tenant must not resolve the key",
)
_assert(all(c["tenant_id"] == "tenant-a" for c in kms.contexts), "KMS context isolation")

os.environ["MONGO_URI"] = (
    "mongodb://admin:super-secret@docdb.example:27017/admin"
    "?tls=true&retryWrites=false"
)
mongo = _MongoClient()
identity = ensure_tenant_identity(
    "tenant-a",
    "arn:aws:iam::123456789012:role/replika-tenant-a",
    client=mongo,
)
_assert("admin" not in identity["mongo_uri"], "runtime URI must strip admin username")
_assert("super-secret" not in identity["mongo_uri"], "runtime URI must strip admin password")
_assert("authMechanism=MONGODB-AWS" in identity["mongo_uri"], "runtime must use IAM auth")
_assert(identity["mongo_db"] == "replika_tenant-a", "tenant database name")
_assert(
    mongo.external.commands[0]["roles"]
    == [{"role": "readWrite", "db": "replika_tenant-a"}],
    "DocumentDB identity must be scoped to one tenant database",
)

deleted = delete_tenant_identity(
    "tenant-a",
    task_role_arn="arn:aws:iam::123456789012:role/replika-tenant-a",
    client=mongo,
)
_assert(deleted["status"] == "deleted", "tenant identity teardown succeeds")
_assert(
    {"dropUser": "arn:aws:iam::123456789012:role/replika-tenant-a"}
    in mongo.external.commands,
    "teardown drops the IAM database user",
)
_assert("replika_tenant-a" in mongo.dropped, "teardown drops the tenant database")

print("Replika V0 boundary checks passed.")

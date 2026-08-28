"""Verify first-boot knowledge seeding is idempotent."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class PersistedKnowledgeTests(unittest.TestCase):
    def test_entrypoint_seeds_without_overwriting_existing_knowledge(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            defaults = root / "defaults"
            persisted = root / "persisted"
            (defaults / "knowledge").mkdir(parents=True)
            (persisted / "knowledge").mkdir(parents=True)
            (defaults / "knowledge" / "new.md").write_text("seed", encoding="utf-8")
            (defaults / "knowledge" / "existing.md").write_text("image", encoding="utf-8")
            (persisted / "knowledge" / "existing.md").write_text("runtime", encoding="utf-8")

            environment = {
                **os.environ,
                "APPCONFIG_REQUIRED": "false",
                "GALADRIEL_DEFAULTS_ROOT": str(defaults),
                "GALADRIEL_STORAGE_ROOT": str(persisted),
                "GALADRIEL_APP_ROOT": str(repo),
            }
            result = subprocess.run(
                [str(repo / "docker" / "entrypoint.sh"), "true"],
                cwd=repo,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            self.assertEqual(
                (persisted / "knowledge" / "existing.md").read_text(encoding="utf-8"),
                "runtime",
            )
            self.assertEqual(
                (persisted / "knowledge" / "new.md").read_text(encoding="utf-8"),
                "seed",
            )


if __name__ == "__main__":
    unittest.main()

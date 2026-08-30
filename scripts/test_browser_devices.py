#!/usr/bin/env python3
"""browser_devices: no phantom unconfigured main on managed BCE tenants."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import browser_devices  # noqa: E402
from harness.tools import _resolve_bce_pairing_code  # noqa: E402


class BrowserDevicesDefaultsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("BCE_PAIRING_CODE", None)
        os.environ.pop("BROWSER_CDP_PORT", None)

    def tearDown(self) -> None:
        self._env.stop()

    def test_bce_list_empty_without_pairing_or_mongo(self) -> None:
        os.environ["BROWSER_BACKEND"] = "bce"
        with mock.patch.object(browser_devices.browser_profiles, "list_profiles", return_value=[]):
            devices = browser_devices.list_devices(include_status=False)
        self.assertEqual(devices, [])
        with mock.patch.object(
            browser_devices.browser_profiles, "list_profiles", return_value=[]
        ):
            self.assertIsNone(browser_devices.resolve("main"))

    def test_bce_env_pairing_still_surfaces_as_main(self) -> None:
        os.environ["BROWSER_BACKEND"] = "bce"
        os.environ["BCE_PAIRING_CODE"] = "ABCD-1234"
        with mock.patch.object(browser_devices.browser_profiles, "list_profiles", return_value=[]):
            devices = browser_devices.list_devices(include_status=False)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["profile_id"], "main")
        self.assertEqual(devices[0]["source"], "environment")
        self.assertTrue(devices[0]["configured"])

    def test_browser_use_keeps_local_main(self) -> None:
        os.environ["BROWSER_BACKEND"] = "browser-use"
        with mock.patch.object(browser_devices.browser_profiles, "list_profiles", return_value=[]):
            devices = browser_devices.list_devices(include_status=False)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["profile_id"], "main")
        self.assertEqual(devices[0]["cdp_port"], 9222)

    def test_mongo_main_still_listed(self) -> None:
        os.environ["BROWSER_BACKEND"] = "bce"
        saved = {
            "profile_id": "main",
            "backend": "bce",
            "pairing_code": "WXYZ-9876",
            "purpose": "default",
        }
        with mock.patch.object(
            browser_devices.browser_profiles, "list_profiles", return_value=[saved]
        ):
            devices = browser_devices.list_devices(include_status=False)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["source"], "database")
        self.assertTrue(devices[0]["configured"])

    def test_status_listing_does_not_reread_each_profile(self) -> None:
        """list_profiles already returned the rows — one Mongo read, not one per device."""
        os.environ["BROWSER_BACKEND"] = "bce"
        saved = [
            {
                "profile_id": f"device-{i}",
                "backend": "bce",
                "pairing_code": f"CODE-{i:04d}",
                "purpose": "test",
            }
            for i in range(5)
        ]
        with mock.patch.object(
            browser_devices.browser_profiles, "list_profiles", return_value=saved
        ) as list_mock, mock.patch.object(
            browser_devices, "_bce_status", return_value={"state": "offline", "online": False}
        ):
            devices = browser_devices.list_devices(include_status=True)
        self.assertEqual(len(devices), 5)
        self.assertEqual(devices[0]["state"], "offline")
        list_mock.assert_called_once()

    def test_bce_browser_ask_to_pair_when_main_missing(self) -> None:
        os.environ["BROWSER_BACKEND"] = "bce"
        with mock.patch.object(
            browser_devices.browser_profiles, "list_profiles", return_value=[]
        ):
            code, err = _resolve_bce_pairing_code("main")
        self.assertEqual(code, "")
        self.assertIn("pairing required", err)
        self.assertIn("browser_devices", err)


class DefaultBrowserRoleTest(unittest.TestCase):
    """`main` is a role, not an id: the flagged browser, or the only one paired."""

    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {"BROWSER_BACKEND": "bce"}, clear=False)
        self._env.start()
        os.environ.pop("BCE_PAIRING_CODE", None)

    def tearDown(self) -> None:
        self._env.stop()

    @staticmethod
    def _row(profile_id: str, **extra) -> dict:
        return {
            "profile_id": profile_id,
            "backend": "bce",
            "pairing_code": "ABCD-2345",
            "purpose": "",
            **extra,
        }

    def _paired(self, *rows):
        return mock.patch.object(
            browser_devices.browser_profiles, "list_profiles", return_value=list(rows)
        )

    def test_the_only_browser_is_main_without_any_flag(self) -> None:
        with self._paired(self._row("6vff-qgfn")):
            resolved = browser_devices.resolve("main")
        self.assertEqual(resolved["profile_id"], "6vff-qgfn")

    def test_flagged_browser_wins_when_several_are_paired(self) -> None:
        with self._paired(
            self._row("aaaa-2345"), self._row("bbbb-3456", is_default=True)
        ):
            resolved = browser_devices.resolve("main")
        self.assertEqual(resolved["profile_id"], "bbbb-3456")

    def test_several_unflagged_browsers_leave_main_unset(self) -> None:
        with self._paired(
            self._row("aaaa-2345"), self._row("bbbb-3456")
        ):
            self.assertIsNone(browser_devices.resolve("main"))
            reason = browser_devices.status("main")["error"]
        self.assertIn("aaaa-2345", reason)
        self.assertIn("bbbb-3456", reason)

    def test_an_uppercase_id_still_resolves(self) -> None:
        """Pairing codes are shown uppercase everywhere; ids are stored lowercase."""
        with self._paired(self._row("6vff-qgfn")):
            resolved = browser_devices.resolve("6VFF-QGFN")
        self.assertEqual(resolved["profile_id"], "6vff-qgfn")

    def test_env_main_reports_the_same_role_to_status_and_list(self) -> None:
        """The env-backed main is the effective default; both paths must say so."""
        os.environ["BCE_PAIRING_CODE"] = "ABCD-2345"
        with self._paired(self._row("aaaa-2345"), self._row("bbbb-3456")), mock.patch.object(
            browser_devices, "_bce_status", return_value={"state": "online", "online": True}
        ):
            listed = {
                device["profile_id"]: device["is_default"]
                for device in browser_devices.list_devices()
            }
            reported = browser_devices.status("main")
        self.assertTrue(listed["main"])
        self.assertTrue(reported["is_default"])

    def test_listing_marks_exactly_one_main(self) -> None:
        with self._paired(self._row("aaaa-2345"), self._row("bbbb-3456", is_default=True)):
            devices = browser_devices.list_devices(include_status=False)
        self.assertEqual(
            [device["profile_id"] for device in devices if device["is_default"]],
            ["bbbb-3456"],
        )

    def _connect(self, *existing, saved: str):
        with self._paired(*existing), mock.patch.object(
            browser_devices.browser_profiles, "upsert", return_value=self._row(saved)
        ), mock.patch.object(
            browser_devices.browser_profiles, "set_default"
        ) as set_default, mock.patch.object(
            browser_devices, "status", return_value={}
        ):
            browser_devices.connect(saved, backend="bce", pairing_code="ABCD-2345")
        return set_default

    def test_first_ever_pairing_becomes_main(self) -> None:
        self._connect(saved="aaaa-2345").assert_called_once_with("aaaa-2345")

    def test_pairing_a_second_browser_keeps_the_first_as_main(self) -> None:
        set_default = self._connect(self._row("aaaa-2345"), saved="bbbb-3456")
        set_default.assert_called_once_with("aaaa-2345")

    def test_pairing_leaves_an_existing_choice_alone(self) -> None:
        set_default = self._connect(
            self._row("aaaa-2345", is_default=True), saved="bbbb-3456"
        )
        set_default.assert_not_called()

    def test_resaving_a_browser_does_not_demote_it(self) -> None:
        """upsert rewrites the whole document — the flag has to survive."""
        collection = mock.MagicMock()
        collection.find_one.return_value = {"created_at": "then", "is_default": True}
        saved = browser_devices.browser_profiles.upsert(
            "aaaa-2345",
            "bce",
            pairing_code="ABCD-2345",
            db={browser_devices.browser_profiles.COLLECTION: collection},
        )
        self.assertTrue(saved["is_default"])


    def test_agent_can_pick_the_main_browser(self) -> None:
        with mock.patch.object(
            browser_devices.browser_profiles, "set_default", return_value=True
        ) as set_default:
            result = browser_devices.execute("set_default", profile_id="bbbb-3456")
        set_default.assert_called_once_with("bbbb-3456")
        self.assertTrue(result["updated"])

    def test_connect_names_a_profile_after_its_code_not_the_role(self) -> None:
        with mock.patch.object(
            browser_devices.browser_profiles, "list_profiles", return_value=[]
        ), mock.patch.object(
            browser_devices.browser_profiles, "upsert", return_value=self._row("abcd-2345")
        ) as upsert, mock.patch.object(
            browser_devices.browser_profiles, "set_default"
        ), mock.patch.object(
            browser_devices, "status", return_value={}
        ):
            browser_devices.execute("connect", pairing_code="ABCD-2345")
        self.assertEqual(upsert.call_args.args[0], "abcd-2345")

    def test_browser_tool_does_not_beg_for_a_code_it_already_has(self) -> None:
        """Several paired, none chosen: pick one, don't ask for a new pairing."""
        from harness.tools import _resolve_bce_pairing_code

        with self._paired(self._row("aaaa-2345"), self._row("bbbb-3456")):
            code, err = _resolve_bce_pairing_code(None)
        self.assertEqual(code, "")
        self.assertIn("already paired", err)
        self.assertIn("set_default", err)
        self.assertNotIn("Ask the user for their Chrome extension pairing code", err)

    def test_set_default_action_is_offered_to_the_agent(self) -> None:
        from harness.tools import visible_tool_definitions

        tool = next(
            definition
            for definition in visible_tool_definitions()
            if definition["name"] == "browser_devices"
        )
        self.assertIn(
            "set_default", tool["input_schema"]["properties"]["action"]["enum"]
        )


class TowerMainBrowserEndpointTest(unittest.TestCase):
    def _post(self, updated: bool):
        from flask import Flask

        from tower import devices_board

        app = Flask(__name__)
        devices_board.register_devices_board(app)
        with mock.patch.object(
            devices_board.browser_devices,
            "set_default",
            return_value={"profile_id": "bbbb-3456", "updated": updated},
        ):
            return app.test_client().post("/api/devices/browsers/bbbb-3456/default")

    def test_set_as_main_succeeds(self) -> None:
        self.assertEqual(self._post(True).status_code, 200)

    def test_set_as_main_on_unknown_profile_is_404(self) -> None:
        self.assertEqual(self._post(False).status_code, 404)


if __name__ == "__main__":
    unittest.main()

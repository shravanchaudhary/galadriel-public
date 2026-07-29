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
        with mock.patch.object(browser_devices.browser_profiles, "get", return_value=None):
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

    def test_bce_browser_ask_to_pair_when_main_missing(self) -> None:
        os.environ["BROWSER_BACKEND"] = "bce"
        with mock.patch.object(browser_devices.browser_profiles, "get", return_value=None):
            code, err = _resolve_bce_pairing_code("main")
        self.assertEqual(code, "")
        self.assertIn("pairing required", err)
        self.assertIn("browser_devices", err)


if __name__ == "__main__":
    unittest.main()

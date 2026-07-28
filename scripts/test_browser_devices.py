#!/usr/bin/env python3
"""Focused checks for browser profile persistence, tools, and Devices routes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import browser_devices, browser_profiles, tools  # noqa: E402
from phone_bridge.auth import AuthStore  # noqa: E402
from phone_bridge.device_registry import DeviceRegistry, live_phone_snapshot  # noqa: E402
from phone_bridge.models import ControlSession  # noqa: E402
from tower.devices_board import register_devices_board  # noqa: E402
from tower.phone_bridge import register_phone_bridge  # noqa: E402


class _Result:
    def __init__(self, deleted_count=0):
        self.deleted_count = deleted_count


class _Collection:
    def __init__(self):
        self.documents = {}

    @staticmethod
    def _matches(document, query):
        return all(document.get(key) == value for key, value in query.items())

    def find_one(self, query, projection=None):
        for document in self.documents.values():
            if self._matches(document, query):
                if projection:
                    return {
                        key: value
                        for key, value in document.items()
                        if key == "_id" or projection.get(key)
                    }
                return dict(document)
        return None

    def find(self, query):
        return [
            dict(document)
            for document in self.documents.values()
            if self._matches(document, query)
        ]

    def replace_one(self, query, document, upsert=False):
        self.documents[document["_id"]] = dict(document)
        return _Result()

    def delete_one(self, query):
        match = self.find_one(query)
        if not match:
            return _Result()
        del self.documents[match["_id"]]
        return _Result(deleted_count=1)


class _Database:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, _Collection())


class _WebSocket:
    async def close(self, **_kwargs):
        return None


def check_repository(db: _Database, directory: Path) -> None:
    os.environ["REPLIKA_TENANT_ID"] = "tenant-a"
    legacy = directory / "browser_profiles.md"
    legacy.write_text(
        "| profile_id | pairing_code | purpose |\n"
        "|---|---|---|\n"
        "| main | ABCD-2345 | Default browser |\n",
        encoding="utf-8",
    )
    assert browser_profiles.migrate_legacy_if_empty(db=db, path=legacy) == 1
    assert browser_profiles.migrate_legacy_if_empty(db=db, path=legacy) == 0
    main = browser_profiles.get("main", db=db, migrate=False)
    assert main and main["backend"] == "bce"

    local = browser_profiles.upsert(
        "work",
        "browser-use",
        cdp_port=9333,
        purpose="Work account",
        db=db,
    )
    assert local["cdp_port"] == 9333
    assert [row["profile_id"] for row in browser_profiles.list_profiles(
        db=db, migrate=False
    )] == ["main", "work"]

    os.environ["REPLIKA_TENANT_ID"] = "tenant-b"
    assert browser_profiles.list_profiles(db=db, migrate=False) == []
    os.environ["REPLIKA_TENANT_ID"] = "tenant-a"

    try:
        browser_profiles.upsert("Bad Profile", "bce", pairing_code="ABCD-2345", db=db)
        raise AssertionError("Invalid profile ID was accepted")
    except ValueError:
        pass

    assert browser_profiles.delete("main", db=db)
    assert browser_profiles.delete("work", db=db)
    assert browser_profiles.list_profiles(db=db) == [], (
        "Deleted profiles must not be reimported after the one-time migration"
    )


async def check_tools_and_snapshot(db: _Database, directory: Path) -> None:
    browser_profiles._sync_db = db
    original_bce_status = browser_devices._bce_status
    original_local_status = browser_devices._local_status
    browser_devices._bce_status = lambda _profile: {"state": "online", "online": True}
    browser_devices._local_status = lambda _profile: {"state": "offline", "online": False}
    try:
        result = json.loads(await tools.execute_tool(
            "browser_devices",
            {"action": "list"},
        ))
        assert result["devices"]
        assert all("pairing_code" not in row for row in result["devices"])
        assert any(
            definition["name"] == "browser_devices"
            for definition in tools.visible_tool_definitions()
        )
    finally:
        browser_devices._bce_status = original_bce_status
        browser_devices._local_status = original_local_status

    auth = AuthStore(directory / "phone_auth.json")
    registry = DeviceRegistry(auth)
    session = ControlSession(
        websocket=_WebSocket(),
        tenant_id="tenant-a",
        device_id="phone_test",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    await registry.register_control(session)
    snapshot = live_phone_snapshot("tenant-a")
    assert snapshot["connected"] and snapshot["device_id"] == "phone_test"
    assert not live_phone_snapshot("tenant-b")["connected"]
    await registry.unregister_control(session.websocket)
    assert not live_phone_snapshot("tenant-a")["connected"]


def check_routes(db: _Database, directory: Path) -> None:
    browser_profiles._sync_db = db
    os.environ["PHONE_BRIDGE_AUTH_STORE"] = str(directory / "tower_phone_auth.json")
    original_bce_status = browser_devices._bce_status
    original_local_status = browser_devices._local_status
    browser_devices._bce_status = lambda _profile: {"state": "online", "online": True}
    browser_devices._local_status = lambda _profile: {"state": "offline", "online": False}
    try:
        app = Flask(
            __name__,
            template_folder=str(ROOT / "tower" / "templates"),
            static_folder=str(ROOT / "tower" / "static"),
        )
        app.context_processor(lambda: {
            "control_plane_only": False,
            "chat_filters": [],
            "page_context": {},
        })
        register_phone_bridge(app)
        register_devices_board(app)
        client = app.test_client()

        index = client.get("/devices")
        assert index.status_code == 302 and index.headers["Location"].endswith(
            "/devices/browser"
        )
        browser_page = client.get("/devices/browser")
        assert browser_page.status_code == 200 and b"Browsers" in browser_page.data
        assert b"Enroll phone" not in browser_page.data
        phone_page = client.get("/devices/phone")
        assert phone_page.status_code == 200 and b"Enroll phone" in phone_page.data
        assert b"Add browser" not in phone_page.data
        redirect = client.get("/phone-bridge")
        assert redirect.status_code == 302 and redirect.headers["Location"].endswith(
            "/devices/phone"
        )
        relay = client.post("/api/devices/browsers", json={
            "pairing_code": "WXYZ-6789",
            "purpose": "Relay",
        })
        assert relay.status_code == 201
        assert relay.get_json()["profile_id"] == "wxyz-6789"
        browser_page = client.get("/devices/browser")
        assert b"Pairing code:" in browser_page.data
        assert b"WXYZ-6789" in browser_page.data
        assert b"Profile ID" not in browser_page.data
        assert b"Local Chrome" not in browser_page.data
        assert b"CDP port" not in browser_page.data
        status = client.get("/api/devices/browsers/status")
        assert status.status_code == 200
        relay_status = next(
            row
            for row in status.get_json()["browsers"]
            if row["profile_id"] == "wxyz-6789"
        )
        assert relay_status["pairing_code"] == "WXYZ-6789"
        phone_status = client.get("/api/devices/phone/status")
        assert phone_status.status_code == 200
        assert "phone" in phone_status.get_json()
        removed = client.delete("/api/devices/browsers/wxyz-6789")
        assert removed.status_code == 200 and removed.get_json()["removed"]
    finally:
        browser_devices._bce_status = original_bce_status
        browser_devices._local_status = original_local_status


def main() -> None:
    db = _Database()
    with tempfile.TemporaryDirectory() as raw_directory:
        directory = Path(raw_directory)
        check_repository(db, directory)
        asyncio.run(check_tools_and_snapshot(db, directory))
        check_routes(db, directory)
    browser_profiles._sync_db = None
    print("browser devices tests passed")


if __name__ == "__main__":
    main()

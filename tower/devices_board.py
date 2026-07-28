"""Independent browser and phone device management pages for Tower."""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, redirect, render_template, request

from harness import browser_devices
from harness.bce_client import BCEError, normalize_pairing_code
from phone_bridge.auth import current_tenant_id, get_auth_store
from phone_bridge.device_registry import live_phone_snapshot

log = logging.getLogger("galadriel.tower.devices")


def _browser_payload() -> list[dict]:
    return [
        browser
        for browser in browser_devices.list_devices(include_pairing_code=True)
        if browser["backend"] == "bce"
    ]


def _phone_payload() -> dict:
    tenant_id = current_tenant_id()
    live_phone = live_phone_snapshot(tenant_id)
    enrolled = get_auth_store().list_devices(tenant_id)
    for device in enrolled:
        device["connected"] = bool(
            not device.get("revoked_at")
            and live_phone.get("connected")
            and live_phone.get("device_id") == device["device_id"]
        )
        device["stream_active"] = bool(
            device["connected"] and live_phone.get("stream_active")
        )
    return {**live_phone, "devices": enrolled}


def register_devices_board(app) -> None:
    blueprint = Blueprint("devices_board", __name__)

    @blueprint.get("/devices")
    def devices_index():
        return redirect("/devices/browser")

    @blueprint.get("/devices/browser")
    def browser_page():
        try:
            browsers = _browser_payload()
            error = None
        except Exception as exc:
            log.warning("Could not load browser devices: %s", exc)
            browsers = []
            error = str(exc)
        return render_template(
            "devices/browser.html",
            browsers=browsers,
            devices_error=error,
        )

    @blueprint.get("/devices/phone")
    def phone_page():
        return render_template("devices/phone.html", phone=_phone_payload())

    @blueprint.get("/api/devices/browsers/status")
    def browser_status():
        try:
            return jsonify({"browsers": _browser_payload()})
        except Exception as exc:
            log.warning("Could not load browser status: %s", exc)
            return jsonify({"error": str(exc)}), 503

    @blueprint.get("/api/devices/phone/status")
    def phone_status():
        return jsonify({"phone": _phone_payload()})

    @blueprint.post("/api/devices/browsers")
    def save_browser():
        data = request.get_json(silent=True) or {}
        try:
            pairing_code = normalize_pairing_code(data.get("pairing_code") or "")
            result = browser_devices.connect(
                pairing_code.lower(),
                backend="bce",
                pairing_code=pairing_code,
                purpose=data.get("purpose"),
            )
            return jsonify(result), 201
        except (BCEError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503

    @blueprint.delete("/api/devices/browsers/<profile_id>")
    def remove_browser(profile_id: str):
        try:
            result = browser_devices.remove(profile_id)
            return jsonify(result), 200 if result["removed"] else 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503

    app.register_blueprint(blueprint)

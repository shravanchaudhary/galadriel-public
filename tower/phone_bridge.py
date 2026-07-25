"""Authenticated Tower management endpoints for phone enrollment."""

from flask import Blueprint, jsonify, render_template

from phone_bridge.auth import current_tenant_id, get_auth_store


def register_phone_bridge(app) -> None:
    blueprint = Blueprint("phone_bridge_admin", __name__)

    @blueprint.get("/phone-bridge")
    def phone_bridge_page():
        devices = get_auth_store().list_devices(current_tenant_id())
        return render_template("phone_bridge.html", devices=devices)

    @blueprint.get("/api/phone-bridge/devices")
    def list_devices():
        return jsonify({
            "devices": get_auth_store().list_devices(current_tenant_id())
        })

    @blueprint.post("/api/phone-bridge/enrollment-code")
    def create_enrollment_code():
        code, expires_at = get_auth_store().create_enrollment_code(
            current_tenant_id()
        )
        response = jsonify({
            "code": code,
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        })
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.post("/api/phone-bridge/devices/<device_id>/revoke")
    def revoke_device(device_id: str):
        if not get_auth_store().revoke_device(current_tenant_id(), device_id):
            return jsonify({"error": "Device not found"}), 404
        return jsonify({"status": "revoked"})

    app.register_blueprint(blueprint)

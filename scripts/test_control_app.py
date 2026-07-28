"""Smoke-test the lightweight Replika control-plane app."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["REPLIKA_ALLOW_LOCAL_PROVISIONING"] = "true"
os.environ["REPLIKA_CONTROL_PLANE_ONLY"] = "true"
os.environ["TOWER_AUTH_REQUIRED"] = "false"
os.environ.pop("MONGO_URI", None)
os.environ.pop("REDIS_URL", None)

from tower.control_app import create_control_app  # noqa: E402

assert "harness.agent" not in sys.modules

client = create_control_app().test_client()
assert client.get("/healthz").status_code == 200
assert client.get("/readyz").status_code == 200

root = client.get("/", follow_redirects=False)
assert root.status_code == 302
assert root.headers["Location"] == "/replika"
replika = client.get("/replika")
assert replika.status_code == 200
assert b'href="/replika"' in replika.data
assert b'My Replikas' in replika.data
assert b'settings-page' in replika.data
assert b'skip-link' in replika.data
assert b'site-menu-btn' in replika.data
assert b'id="site-sidebar"' in replika.data
assert b'/static/ui.js' in replika.data
assert b'replika-form' in replika.data
assert b'replika-list' in replika.data
assert b'settings-choice-grid' in replika.data
assert client.get("/api/chat").status_code == 404

ui_js = client.get("/static/ui.js")
assert ui_js.status_code == 200
assert b'towerToast' in ui_js.data
assert b'towerConfirm' in ui_js.data
assert b'renderRelativeTimes' in ui_js.data

style = client.get("/static/style.css")
assert style.status_code == 200
assert b'.table-wrap' in style.data
assert b'.site-topbar' in style.data
assert b'.ui-dialog' in style.data
assert b'@media (max-width: 640px)' in style.data

print("Control-plane app check passed.")

"""Browser Command Executor CLI — browser-use-compatible command surface.

Usage (same argument style as browser-use):
  bce-cli open https://example.com
  bce-cli state
  bce-cli click 2
  bce-cli --json state

Prerequisites: Chrome extension connected, FastAPI server running, pairing code set.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import sys
from typing import Any

from .bce_client import BCEClient, BCECommandError, BCEError, BCEOfflineError, CommandResult

_BROWSER_USE_FLAGS = {"--session", "--cdp-url", "--profile", "--connect", "--headed"}


def _format_element(el: dict[str, Any]) -> str:
    idx = el.get("index", "?")
    tag = el.get("tag") or el.get("role") or "element"
    label = el.get("text") or el.get("label") or el.get("name") or el.get("placeholder") or ""
    label = str(label).replace("\n", " ").strip()
    if len(label) > 80:
        label = label[:77] + "..."
    if label:
        return f'[{idx}] {tag} "{label}"'
    return f"[{idx}] {tag}"


def format_state(result: CommandResult) -> str:
    payload = result.state or result.result or {}
    if isinstance(payload, dict) and "state" in payload and isinstance(payload["state"], dict):
        payload = payload["state"]
    lines: list[str] = []
    url = payload.get("url") if isinstance(payload, dict) else None
    title = payload.get("title") if isinstance(payload, dict) else None
    if url:
        lines.append(f"URL: {url}")
    if title:
        lines.append(f"Title: {title}")
    if lines:
        lines.append("")
    elements = []
    if isinstance(payload, dict):
        elements = payload.get("elements") or []
    for el in elements:
        if isinstance(el, dict):
            lines.append(_format_element(el))
    return "\n".join(lines).strip() or "(no interactive elements)"


def format_result(result: CommandResult, *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(
            {
                "id": result.id,
                "ok": result.ok,
                "duration_ms": result.duration_ms,
                "result": result.result,
                "state": result.state,
                "error": result.error,
            },
            indent=2,
        )
    if result.result is not None:
        if isinstance(result.result, dict) and "value" in result.result:
            val = result.result["value"]
            if isinstance(val, (dict, list)):
                return json.dumps(val, indent=2)
            return str(val)
        if isinstance(result.result, (dict, list)):
            return json.dumps(result.result, indent=2)
        return str(result.result)
    return "OK"


def _screenshot_data(result: CommandResult) -> str | None:
    """Base64 image data from a screenshot result. The BCE server returns it
    under `screenshot`; older/alternate shapes used `data`/`image`/`base64`."""
    payload = result.result or {}
    if not isinstance(payload, dict):
        return None
    return (
        payload.get("screenshot")
        or payload.get("data")
        or payload.get("image")
        or payload.get("base64")
    )


def _save_screenshot(result: CommandResult, path: str) -> str:
    data = _screenshot_data(result)
    if not data:
        return "[error] screenshot command returned no image data."
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    raw = base64.b64decode(data)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)
    return f"Saved screenshot to {path}"


def _parse_int(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise BCEError(f"invalid {name}: {value!r}") from exc


def _strip_global_flags(argv: list[str]) -> tuple[list[str], bool, str | None]:
    """Remove browser-use / bce global flags. Returns (rest, json_mode, pairing_code)."""
    rest: list[str] = []
    json_mode = False
    pairing_code: str | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--json":
            json_mode = True
            i += 1
            continue
        if tok == "--pairing-code":
            if i + 1 >= len(argv):
                raise BCEError("--pairing-code requires a value")
            pairing_code = argv[i + 1]
            i += 2
            continue
        if tok in _BROWSER_USE_FLAGS:
            if tok in {"--session", "--cdp-url", "--profile", "--connect"} and i + 1 < len(argv):
                i += 2
                continue
            i += 1
            continue
        if tok.startswith("--pairing-code="):
            pairing_code = tok.split("=", 1)[1]
            i += 1
            continue
        rest.append(tok)
        i += 1
    return rest, json_mode, pairing_code


def _connect_client(pairing_code: str | None) -> BCEClient:
    client = BCEClient.from_env()
    code = pairing_code or client.pairing_code
    if not code:
        raise BCEError(
            "pairing_code required — set BCE_PAIRING_CODE or pass --pairing-code XXXX-XXXX"
        )
    client.connect(code)
    return client


def run_argv(
    argv: list[str],
    *,
    pairing_code: str | None = None,
    ensure_online: bool = True,
) -> tuple[str, bool]:
    """Run one bce-cli command. Returns (output, ok)."""
    try:
        rest, as_json, flag_code = _strip_global_flags(argv)
        effective_code = pairing_code or flag_code
        if not rest:
            return "[error] No bce-cli command given.", False
        if rest[0] in ("--help", "-h", "help"):
            return _HELP_TEXT, True
        if rest[0] == "--version":
            return "bce-cli 1.0", True
        if rest[0] == "close":
            return (
                "(BCE: no close needed — extension stays connected; use tab commands to manage tabs.)",
                True,
            )

        client = _connect_client(effective_code)
        if ensure_online:
            client.ensure_online()

        cmd = rest[0]
        args = rest[1:]

        if cmd == "open":
            if not args:
                return "[error] open requires a URL.", False
            result = client.open(args[0])
            return format_result(result, as_json=as_json), True

        if cmd == "state":
            result = client.state()
            if as_json:
                return format_result(result, as_json=True), True
            return format_state(result), True

        if cmd == "back":
            result = client.back()
            return format_result(result, as_json=as_json), True

        if cmd == "scroll":
            direction = args[0] if args else "down"
            amount = _parse_int(args[1], "amount") if len(args) > 1 else 800
            if direction not in ("up", "down"):
                return f"[error] scroll direction must be up or down, got {direction!r}.", False
            result = client.scroll(direction, amount)
            return format_result(result, as_json=as_json), True

        if cmd == "click":
            if not args:
                return "[error] click requires an element index (or x y for coordinate click).", False
            if len(args) >= 2:
                result = client.click_xy(float(args[0]), float(args[1]))
            else:
                result = client.click(_parse_int(args[0], "index"))
            return format_result(result, as_json=as_json), True

        if cmd == "type":
            if not args:
                return "[error] type requires text.", False
            result = client.type_text(" ".join(args))
            return format_result(result, as_json=as_json), True

        if cmd == "input":
            if len(args) < 2:
                return "[error] input requires an index and text.", False
            result = client.input(_parse_int(args[0], "index"), " ".join(args[1:]))
            return format_result(result, as_json=as_json), True

        if cmd == "keys":
            if not args:
                return "[error] keys requires a key combo string.", False
            result = client.keys(" ".join(args))
            return format_result(result, as_json=as_json), True

        if cmd == "select":
            if len(args) < 2:
                return "[error] select requires an index and option text.", False
            result = client.select(_parse_int(args[0], "index"), " ".join(args[1:]))
            return format_result(result, as_json=as_json), True

        if cmd == "hover":
            if not args:
                return "[error] hover requires an element index.", False
            result = client.hover(_parse_int(args[0], "index"))
            return format_result(result, as_json=as_json), True

        if cmd == "dblclick":
            if not args:
                return "[error] dblclick requires an element index.", False
            result = client.dblclick(_parse_int(args[0], "index"))
            return format_result(result, as_json=as_json), True

        if cmd == "rightclick":
            if not args:
                return "[error] rightclick requires an element index.", False
            result = client.rightclick(_parse_int(args[0], "index"))
            return format_result(result, as_json=as_json), True

        if cmd == "screenshot":
            full = "--full" in args
            path = next((a for a in args if a != "--full" and not a.startswith("-")), None)
            result = client.screenshot(full=full)
            if path:
                return _save_screenshot(result, path), True
            if as_json:
                return format_result(result, as_json=True), True
            data = _screenshot_data(result)
            if data:
                return f"(screenshot captured, {len(str(data))} chars base64 — use a path to save)", True
            return format_result(result, as_json=as_json), True

        if cmd == "get":
            if not args:
                return "[error] get requires a field (title, html, text, value, ...).", False
            field = args[0]
            index = None
            if len(args) > 1 and args[1].isdigit():
                index = _parse_int(args[1], "index")
            if field == "title":
                title = client.get_title()
                return title if not as_json else json.dumps({"value": title}), True
            result = client.get(field, index=index)  # type: ignore[arg-type]
            return format_result(result, as_json=as_json), True

        if cmd == "eval":
            if not args:
                return "[error] eval requires JavaScript code.", False
            code = " ".join(args)
            if (code.startswith('"') and code.endswith('"')) or (
                code.startswith("'") and code.endswith("'")
            ):
                code = code[1:-1]
            result = client.eval(code)
            return format_result(result, as_json=as_json), True

        if cmd == "wait":
            if len(args) < 2:
                return "[error] wait requires a type and value, e.g. `wait text Welcome`.", False
            wait_type = args[0]
            value = " ".join(args[1:])
            if wait_type == "text":
                result = client.wait_text(value.strip('"').strip("'"))
            elif wait_type == "selector":
                result = client.wait_selector(value.strip('"').strip("'"))
            else:
                return f"[error] unknown wait type {wait_type!r}.", False
            return format_result(result, as_json=as_json), True

        if cmd == "tab":
            if not args:
                return "[error] tab requires an action (list, new, switch, close).", False
            action = args[0]
            if action == "list":
                tabs = client.tab_list()
                if as_json:
                    return json.dumps(tabs, indent=2), True
                lines = []
                for t in tabs:
                    marker = " *" if t.get("active") else ""
                    lines.append(
                        f"[{t.get('index')}] {t.get('title', '')[:50]} — {t.get('url', '')}{marker}"
                    )
                return "\n".join(lines) or "(no tabs)", True
            if action == "new":
                url = args[1] if len(args) > 1 else None
                result = client.tab_new(url)
                return format_result(result, as_json=as_json), True
            if action == "switch":
                if len(args) < 2:
                    return "[error] tab switch requires an index.", False
                result = client.tab_switch(_parse_int(args[1], "index"))
                return format_result(result, as_json=as_json), True
            if action == "close":
                if len(args) < 2:
                    return "[error] tab close requires an index.", False
                result = client.tab_close(_parse_int(args[1], "index"))
                return format_result(result, as_json=as_json), True
            return f"[error] unknown tab action {action!r}.", False

        return f"[error] unknown command {cmd!r}. Run bce-cli --help.", False

    except BCEOfflineError as exc:
        return f"[error] {exc}", False
    except BCECommandError as exc:
        return f"[error] {exc}", False
    except BCEError as exc:
        return f"[error] {exc}", False
    except Exception as exc:
        return f"[error] {exc}", False


_HELP_TEXT = """\
bce-cli — Browser Command Executor (browser-use-compatible commands)

Prerequisites:
  1. MongoDB + FastAPI server running (BCE_BASE_URL, default http://localhost:8000)
  2. Chrome extension loaded, Agent ON, status Connected
  3. Pairing code from extension popup (XXXX-XXXX, e.g. KJ2D-H96M)

Commands (same style as browser-use):
  open <url>              Navigate active tab
  state                   List interactive elements with indices
  click <index>           Click element by index
  click <x> <y>           Click at coordinates
  input <index> <text>    Click field then type
  type <text>             Type into focused element
  keys <combo>            Send key combo (e.g. Enter, Control+a)
  select <index> <text>   Pick dropdown option
  hover / dblclick / rightclick <index>
  scroll [up|down] [px]   Scroll page
  back                    Browser back
  close                   No-op (extension stays connected)
  screenshot [--full] [path]
  get title|html|text|value [index]
  eval <js>               Run JavaScript
  wait text <text>        Wait for text
  wait selector <css>     Wait for selector
  tab list|new [url]|switch <i>|close <i>

Flags:
  --json                      Machine-readable output
  --pairing-code <XXXX-XXXX>  Override BCE_PAIRING_CODE for this call

Environment:
  BCE_BASE_URL, BCE_API_KEY, BCE_PAIRING_CODE, BCE_TIMEOUT_MS
"""


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(_HELP_TEXT)
        return 0
    output, ok = run_argv(args)
    if output:
        print(output)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

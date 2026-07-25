"""Constrained agent tools for the authenticated phone bridge."""

from __future__ import annotations

import os
import re

from phone_bridge.adb_manager import get_adb_manager

PHONE_TOOL_NAMES = frozenset({
    "phone_shell",
    "phone_ui_dump",
    "phone_tap",
    "phone_swipe",
    "phone_type",
    "phone_back",
    "phone_home",
})

_COORDINATE = {"type": "integer", "minimum": 0, "maximum": 100000}

PHONE_TOOL_DEFINITIONS = [
    {
        "name": "phone_shell",
        "description": (
            "Run an explicitly approved arbitrary Android shell command on the "
            "enrolled phone. Disabled unless PHONE_SHELL_ALLOW_ARBITRARY=1."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "maxLength": 4096},
            },
            "required": ["command"],
        },
    },
    {
        "name": "phone_ui_dump",
        "description": "Return the current Android UI hierarchy as XML.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "phone_tap",
        "description": "Tap screen coordinates on the enrolled phone.",
        "input_schema": {
            "type": "object",
            "properties": {"x": _COORDINATE, "y": _COORDINATE},
            "required": ["x", "y"],
        },
    },
    {
        "name": "phone_swipe",
        "description": "Swipe between two screen coordinates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x1": _COORDINATE,
                "y1": _COORDINATE,
                "x2": _COORDINATE,
                "y2": _COORDINATE,
                "duration": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 60000,
                },
            },
            "required": ["x1", "y1", "x2", "y2", "duration"],
        },
    },
    {
        "name": "phone_type",
        "description": "Type plain text into the focused Android field.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "maxLength": 500}},
            "required": ["text"],
        },
    },
    {
        "name": "phone_back",
        "description": "Press the Android Back key.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "phone_home",
        "description": "Press the Android Home key.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def phone_tools_enabled() -> bool:
    return os.environ.get("PHONE_TOOLS_ENABLED", "0").lower() in {
        "1",
        "true",
        "yes",
    }


async def execute_phone_tool(name: str, inputs: dict) -> str:
    manager = get_adb_manager()
    if name == "phone_shell":
        if os.environ.get("PHONE_SHELL_ALLOW_ARBITRARY", "0").lower() not in {
            "1",
            "true",
            "yes",
        }:
            return (
                "[blocked] Arbitrary phone shell access requires explicit operator "
                "approval via PHONE_SHELL_ALLOW_ARBITRARY=1."
            )
        command = inputs["command"]
        if not isinstance(command, str) or not command or len(command) > 4096:
            raise ValueError("command must contain 1-4096 characters")
        return await manager.run("shell", command)
    if name == "phone_ui_dump":
        return await manager.run("shell", "uiautomator", "dump", "/dev/tty")
    if name == "phone_tap":
        x = _coordinate(inputs["x"], "x")
        y = _coordinate(inputs["y"], "y")
        return await manager.run("shell", "input", "tap", str(x), str(y))
    if name == "phone_swipe":
        coordinates = [
            _coordinate(inputs[field], field)
            for field in ("x1", "y1", "x2", "y2")
        ]
        duration = _integer(inputs["duration"], "duration", 1, 60_000)
        return await manager.run(
            "shell",
            "input",
            "swipe",
            *(str(value) for value in coordinates),
            str(duration),
        )
    if name == "phone_type":
        text = inputs["text"]
        if (
            not isinstance(text, str)
            or not text
            or len(text) > 500
            or re.fullmatch(r"[A-Za-z0-9 .,!?_@+\-:/]*", text) is None
        ):
            raise ValueError(
                "text must be 1-500 plain characters without shell metacharacters"
            )
        return await manager.run(
            "shell",
            "input",
            "text",
            text.replace(" ", "%s"),
        )
    if name == "phone_back":
        return await manager.run("shell", "input", "keyevent", "BACK")
    if name == "phone_home":
        return await manager.run("shell", "input", "keyevent", "HOME")
    raise ValueError(f"Unknown phone tool: {name}")


def _coordinate(value, name: str) -> int:
    return _integer(value, name, 0, 100_000)


def _integer(value, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value

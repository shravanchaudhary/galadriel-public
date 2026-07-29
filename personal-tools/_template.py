"""Template for a tenant-owned personal tool.

Copy to a new file named without a leading underscore (e.g. `my_helper.py`),
then edit. Files starting with `_` are not loaded.
"""

TOOL_DEFINITIONS = [
    {
        "name": "example_echo",
        "description": "Echo a short string back (replace with a real personal tool).",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to echo."},
            },
            "required": ["text"],
        },
    },
]


async def execute_tool(name: str, inputs: dict) -> str:
    if name == "example_echo":
        return str(inputs.get("text", ""))
    return f"[error] unknown personal tool: {name}"

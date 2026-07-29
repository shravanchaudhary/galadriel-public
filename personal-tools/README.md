# personal-tools/

Tenant-owned tools the Replika may create and maintain.

Product/developer tools live in the immutable application image (`harness/`).
Anything here lives on persistent tenant storage, so image updates do not overwrite
or clash with agent-authored tools.

## Rules

- **Do create / edit tools here** when you need a reusable coded capability.
- **Do not edit** `harness/tools.py` or other product harness code — those updates
  come from the provider and would be lost or blocked.
- To the model, personal tools and developer tools appear in **one flat tool list**.
- If a personal tool reuses a developer tool name, the **developer tool wins**.

## Module contract

Add a `*.py` file (not starting with `_`). Example:

```python
TOOL_DEFINITIONS = [
    {
        "name": "example_echo",
        "description": "Echo a short string back (personal-tools example).",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
        },
    },
]


async def execute_tool(name: str, inputs: dict) -> str:
    if name == "example_echo":
        return inputs.get("text", "")
    return f"[error] unknown personal tool: {name}"
```

New or changed modules are picked up on the next tool listing (mtime cache).
Keep helpers small; prefer `db_*` / file / palace tools for durable state.

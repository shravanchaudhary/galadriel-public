"""Tower overlay chat — minimal pointer-based page context.

The floating Galadriel widget sends only identifiers (file paths, entity keys,
drawer_ids, …) so the agent loads full data on demand via its tools instead of
receiving dumped page content in the prompt.
"""

from __future__ import annotations

import json
from typing import Any


_OVERLAY_MARKER = "[User instruction]\n"
_OVERLAY_PREFIX = (
    "[Tower overlay — user is on a Tower page. Context is POINTERS ONLY "
    "(paths, entity keys, drawer_ids, …). Load full data with read_file, "
    "db_get, db_query, palace_search, and other palace tools — do not "
    "assume file or record bodies are included here.]"
)


def _parse_context(context: dict | str | None) -> dict:
    if not context:
        return {}
    if isinstance(context, str):
        try:
            return json.loads(context) or {}
        except json.JSONDecodeError:
            return {}
    return context


def format_overlay_system_block(context: dict | str | None) -> str | None:
    """Ephemeral system text for one Tower overlay turn (not stored in history)."""
    ctx = _parse_context(context)
    if not ctx.get("view"):
        return None
    body = json.dumps(ctx, indent=2, ensure_ascii=False)
    return f"{_OVERLAY_PREFIX}\n{body}"


def display_user_text(stored: str) -> str:
    """Strip Tower overlay wrapper from stored user messages for UI display."""
    if _OVERLAY_MARKER in stored:
        return stored.split(_OVERLAY_MARKER, 1)[1].strip()
    if stored.startswith(_OVERLAY_PREFIX):
        return "(Tower overlay message)"
    if stored.startswith("[Tower]: "):
        return stored[len("[Tower]: "):]
    return stored


def _is_tool_results(content: list) -> bool:
    if not content:
        return False
    for block in content:
        if isinstance(block, dict):
            if block.get("type") != "tool_result":
                return False
        elif hasattr(block, "type"):
            if getattr(block, "type", None) != "tool_result":
                return False
        else:
            return False
    return True


def _block_type(block) -> str | None:
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _block_text(block) -> str:
    if isinstance(block, dict):
        return block.get("text") or ""
    return getattr(block, "text", "") or ""


def _tool_use_fields(block) -> tuple[str, str, Any]:
    if isinstance(block, dict):
        return (
            block.get("id") or "",
            block.get("name") or "",
            block.get("input", {}),
        )
    return block.id, block.name, block.input


def _tool_result_fields(block) -> tuple[str, str]:
    if isinstance(block, dict):
        rid, content = block.get("tool_use_id") or "", block.get("content") or ""
    else:
        rid, content = block.tool_use_id, block.content
    if isinstance(content, list):
        # Block-list result (text + image, e.g. screenshots) — text only for
        # the UI, with a marker instead of the base64 payload.
        texts = [
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        n_images = sum(
            1 for b in content if isinstance(b, dict) and b.get("type") == "image"
        )
        content = "\n".join(t for t in texts if t)
        if n_images:
            content += f"\n[{n_images} image(s) attached]"
    return rid, content


def _format_tool_input(tool_input) -> str:
    if isinstance(tool_input, str):
        return tool_input
    try:
        return json.dumps(tool_input, ensure_ascii=False)
    except Exception:
        return str(tool_input)


def _serialize_assistant_turn(messages: list, start: int) -> tuple[list[dict], int]:
    """Consume assistant + tool-result messages for one user turn."""
    blocks: list[dict] = []
    pending_tools: list[dict] = []
    i = start

    while i < len(messages):
        msg = messages[i]
        role = msg.get("role")
        content = msg.get("content")

        if role == "user" and (
            isinstance(content, str)
            or (isinstance(content, list) and not _is_tool_results(content))
        ):
            break

        if role == "assistant":
            thought = msg.get("_thought")
            if thought:
                blocks.append({"type": "thought", "text": thought})

            if isinstance(content, list):
                for block in content:
                    btype = _block_type(block)
                    if btype == "text":
                        text = _block_text(block)
                        if text:
                            blocks.append({"type": "text", "text": text})
                    elif btype == "tool_use":
                        uid, name, inp = _tool_use_fields(block)
                        pending_tools.append({"id": uid, "name": name, "input": inp})
            elif isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            i += 1

        elif role == "user" and isinstance(content, list) and _is_tool_results(content):
            results: dict[str, str] = {}
            for block in content:
                rid, out = _tool_result_fields(block)
                results[rid] = out
            for tool in pending_tools:
                blocks.append({
                    "type": "tool_call",
                    "name": tool["name"],
                    "input": _format_tool_input(tool["input"]),
                    "output": results.get(tool["id"], ""),
                })
            pending_tools = []
            i += 1
        else:
            i += 1

    return blocks, i


def serialize_chat_history(messages: list) -> list[dict]:
    """Build Tower-facing history with structured assistant turns."""
    history: list[dict] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role")
        content = msg.get("content")
        if role == "user" and isinstance(content, str):
            history.append({"role": "user", "text": display_user_text(content)})
            i += 1
            blocks, i = _serialize_assistant_turn(messages, i)
            if blocks:
                history.append({"role": "assistant", "blocks": blocks})
        elif role == "user" and isinstance(content, list) and not _is_tool_results(content):
            # Multimodal user message (text + image blocks from Discord/Slack/Tower).
            texts = [_block_text(b) for b in content if _block_type(b) == "text"]
            n_images = sum(1 for b in content if _block_type(b) == "image")
            text = display_user_text("\n".join(t for t in texts if t))
            if n_images:
                marker = f"[{n_images} image(s) attached]"
                text = f"{text}\n{marker}" if text else marker
            history.append({"role": "user", "text": text})
            i += 1
            blocks, i = _serialize_assistant_turn(messages, i)
            if blocks:
                history.append({"role": "assistant", "blocks": blocks})
        else:
            i += 1
    return history


def _ptr(view: str, label: str, *, reload: bool = True, **fields: Any) -> dict:
    out: dict[str, Any] = {"surface": "tower", "view": view, "label": label}
    if reload:
        out["reload_on_done"] = True
    for key, val in fields.items():
        if val is not None and val != "" and val != []:
            out[key] = val
    return out


def record_ptr(key, status=None) -> dict:
    p: dict[str, Any] = {"key": key}
    if status is not None:
        p["status"] = status
    return p


# ── Workflows (MongoDB entities) ─────────────────────────────────────────────


def workflow_index(entities: list[dict]) -> dict:
    return _ptr(
        "workflow_index",
        "workflows",
        reload=False,
        entities=[{"entity": e["entity"], "workflow": e.get("workflow")} for e in entities],
    )


def workflow_table(
    entity: str,
    key_field: str,
    docs,
    *,
    workflow: str | None = None,
    status_filter: str | None = None,
    approval_states: list | None = None,
) -> dict:
    records = [record_ptr(d.get(key_field), d.get("status")) for d in docs]
    return _ptr(
        "workflow_table",
        f"{entity} · table",
        entity=entity,
        workflow=workflow,
        key_field=key_field,
        status_filter=status_filter,
        approval_states=approval_states or None,
        records=records,
        read={"db_query": {"entity": entity, "filter": {"status": status_filter} if status_filter else {}}},
    )


def workflow_kanban(entity: str, key_field: str, columns, *, workflow: str | None = None) -> dict:
    return _ptr(
        "workflow_kanban",
        f"{entity} · kanban",
        entity=entity,
        workflow=workflow,
        key_field=key_field,
        columns=[
            {
                "status": col["state"],
                "keys": [d.get(key_field) for d in col.get("docs", [])],
            }
            for col in columns
        ],
    )


def workflow_detail(
    entity: str,
    key_field: str,
    key,
    status,
    allowed_transitions,
    *,
    workflow: str | None = None,
) -> dict:
    return _ptr(
        "workflow_detail",
        f"{entity} · detail",
        entity=entity,
        workflow=workflow,
        key_field=key_field,
        record=record_ptr(key, status),
        allowed_transitions=list(allowed_transitions or []),
        read={"db_get": {"entity": entity, "key": key}},
    )


def workflow_approvals(pending: list[dict]) -> dict:
    return _ptr(
        "workflow_approvals",
        "approvals",
        pending=pending,
    )


def workflow_approval_card(entity: str, key_field: str, key, status, allowed) -> dict:
    return {
        "entity": entity,
        "key_field": key_field,
        "record": record_ptr(key, status),
        "allowed_transitions": list(allowed or []),
    }


# ── Brain (config / jobs / state / sme files) ────────────────────────────────


def config_index() -> dict:
    return _ptr("config_index", "brain", reload=False)


def config_browse(category: str, subpath: str, files: list[str]) -> dict:
    label = f"brain/{category}" + (f"/{subpath}" if subpath else "")
    return _ptr(
        "config_browse",
        label,
        reload=False,
        category=category,
        path=subpath or None,
        files=files,
    )


def config_file(relpath: str) -> dict:
    return _ptr(
        "config_file",
        relpath,
        file=relpath,
        read={"read_file": relpath},
    )


def config_preview() -> dict:
    return _ptr("config_preview", "assembled prompt", reload=False)


# ── Palace ───────────────────────────────────────────────────────────────────


def palace_index(wings: list[str]) -> dict:
    return _ptr("palace_index", "palace", reload=False, wings=wings)


def palace_browse(wing: str, rooms: list[str]) -> dict:
    return _ptr("palace_browse", f"palace/{wing}", reload=False, wing=wing, rooms=rooms)


def palace_room(
    wing: str | None,
    room: str | None,
    hall: str | None,
    drawer_ids: list[str],
    *,
    page: int = 1,
) -> dict:
    loc = "/".join(p for p in (wing, room, hall) if p) or "palace"
    return _ptr(
        "palace_room",
        loc,
        wing=wing,
        room=room,
        hall=hall,
        page=page,
        drawer_ids=drawer_ids,
    )


def palace_drawer(drawer_id: str, wing: str, room: str, hall: str | None = None) -> dict:
    return _ptr(
        "palace_drawer",
        drawer_id,
        drawer_id=drawer_id,
        wing=wing,
        room=room,
        hall=hall,
        read={"palace_search": drawer_id},
    )


def palace_search(query: str, drawer_ids: list[str], *, wing=None, room=None, hall=None) -> dict:
    return _ptr(
        "palace_search",
        "palace search",
        reload=False,
        query=query,
        wing=wing,
        room=room,
        hall=hall,
        drawer_ids=drawer_ids,
    )


def palace_kg(entity: str = "", fact_count: int = 0) -> dict:
    ctx = _ptr("palace_kg", "knowledge graph", reload=False, fact_count=fact_count)
    if entity:
        ctx["entity"] = entity
        ctx["read"] = {"palace_kg_timeline": entity}
    return ctx


def palace_diary(drawer_ids: list[str], *, page: int = 1) -> dict:
    return _ptr(
        "palace_diary",
        "diary",
        reload=False,
        page=page,
        drawer_ids=drawer_ids,
    )


# ── Actions & loops ──────────────────────────────────────────────────────────


def actions(date: str, plan_file: str, progress_file: str, worker_status: str) -> dict:
    return _ptr(
        "actions",
        f"actions · {date}",
        date=date,
        plan_file=plan_file,
        progress_file=progress_file,
        worker_status=worker_status,
        read={"read_file": [plan_file, progress_file]},
    )


def loops_index() -> dict:
    return _ptr("loops_index", "loops", reload=False)


def costs_index(range_key: str) -> dict:
    return _ptr("costs_index", f"costs · {range_key}", reload=False, range=range_key)


def worker_ticks_index(date: str, tick_ids: list[str]) -> dict:
    return _ptr(
        "worker_ticks_index",
        f"worker runs · {date}",
        reload=False,
        date=date,
        tick_ids=tick_ids,
    )


def worker_tick_detail(tick_id: str, date: str) -> dict:
    return _ptr(
        "worker_tick_detail",
        f"worker run · {tick_id}",
        tick_id=tick_id,
        date=date,
    )


def runs_index(date: str, run_ids: list[str]) -> dict:
    return _ptr("runs_index", f"runs · {date}", reload=False, date=date, run_ids=run_ids)


def run_detail(run_id: str) -> dict:
    return _ptr("run_detail", f"run · {run_id}", run_id=run_id)

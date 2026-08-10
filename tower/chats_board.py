"""Tower UI — ChatGPT-like chats browser (chat + worker + loop ticks)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for

from harness import conversation_run_store, worker_tick_store
from . import ui_context as ui_ctx

_LIST_POOL = ThreadPoolExecutor(max_workers=2)

CET = ZoneInfo("Europe/Stockholm")

# Filter value → label. "reflection" is shown as Ambient in the product UI.
FILTERS = [
    ("all", "All"),
    ("chat", "Chat"),
    ("worker", "Worker"),
    ("heartbeat", "Heartbeat"),
    ("wake", "Wake"),
    ("morning", "Morning"),
    ("ambient", "Ambient"),
    ("goodnight", "Goodnight"),
    ("completions", "Completions"),
]
_FILTER_IDS = frozenset(fid for fid, _ in FILTERS)
_TICK_FILTERS = _FILTER_IDS - {"all", "chat"}
_CHANNEL_LABELS = {fid: label for fid, label in FILTERS}
# Product filter id → durable channel_id (Mongo / agent storage).
_FILTER_TO_CHANNEL = {
    "chat": "main",
    "ambient": "reflection",
}
_CHANNEL_TO_FILTER = {v: k for k, v in _FILTER_TO_CHANNEL.items()}


def _storage_channel(kind: str) -> str:
    return _FILTER_TO_CHANNEL.get(kind, kind)


def _filter_for_channel(channel_id: str) -> str:
    return _CHANNEL_TO_FILTER.get(channel_id or "", channel_id or "worker")

BUCKET_ORDER = [
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("week", "Past week"),
    ("older", "Older"),
]
PAGE_SIZE = 25


def _today() -> str:
    return datetime.now(CET).strftime("%Y-%m-%d")


def _as_messages(events: list[dict]) -> list[dict]:
    """Rebuild the message list used for Tower transcript serialization.

    User turns are stored as ``direct_user`` (not ``protocol_message``); assistant
    / tool traffic is ``protocol_message`` and may carry ``thought``. Both are
    required — protocol-only drops users, so serialize_chat_history skips every
    assistant turn (and its thoughts). Recall-fire messages (kind=recall_fire)
    are also preserved so suggestions appear in the transcript.
    """
    messages = []
    for event in events:
        if event.get("kind") not in ("protocol_message", "direct_user", "recall_fire"):
            continue
        if event.get("role") is None or event.get("content") is None:
            continue
        message = {"role": event.get("role"), "content": event.get("content")}
        if event.get("thought"):
            message["_thought"] = event["thought"]
        if event.get("kind") == "recall_fire":
            message["kind"] = "recall_fire"
            if event.get("matched_recall_ids"):
                message["matched_recall_ids"] = event["matched_recall_ids"]
        messages.append(message)
    return messages


def _events_to_messages(events: list[dict]) -> list[dict]:
    messages = []
    for event in events:
        message = {"role": event.get("role", "unknown"), "content": event.get("content")}
        if event.get("thought"):
            message["_thought"] = event["thought"]
        if event.get("kind") == "recall_fire":
            message["kind"] = "recall_fire"
            if event.get("matched_recall_ids"):
                message["matched_recall_ids"] = event["matched_recall_ids"]
        messages.append(message)
    return messages


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def _direct_history(events: list[dict]) -> list[dict]:
    history: list[dict] = []
    for event in events:
        role = event.get("role")
        text = _content_text(event.get("content"))
        if role == "user":
            disp = ui_ctx.display_user_text(text)
            if disp:
                history.append({"role": "user", "text": disp})
        elif role == "assistant":
            blocks: list[dict] = []
            thought = (event.get("thought") or "").strip()
            if thought:
                blocks.append({"type": "thought", "text": thought})
            if text:
                is_recall_fire = event.get("kind") == "recall_fire"
                blocks.append({
                    "type": "thought" if is_recall_fire else "text",
                    "text": text,
                })
            if blocks:
                if history and history[-1]["role"] == "assistant":
                    history[-1]["blocks"].extend(blocks)
                else:
                    history.append({"role": "assistant", "blocks": blocks})
    return history


def _jsonable(value):
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "_id":
                continue
            out[key] = _jsonable(item)
        return out
    type_name = type(value).__name__
    if type_name == "ObjectId":
        return str(value)
    return value


def _preview(text: str, limit: int = 72) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _aware(value):
    if not value:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=ZoneInfo("UTC"))
    return value


def _fmt_time(value) -> str:
    value = _aware(value)
    if not value:
        return "—"
    return value.astimezone(CET).strftime("%H:%M")


def _cet_date(value) -> str:
    value = _aware(value)
    if not value:
        return _today()
    return value.astimezone(CET).strftime("%Y-%m-%d")


def _bucket_for(value) -> str:
    """Classify a timestamp into today / yesterday / past week / older (CET)."""
    value = _aware(value)
    if not value:
        return "older"
    local = value.astimezone(CET)
    day = local.date()
    today = datetime.now(CET).date()
    yesterday = today - timedelta(days=1)
    week_floor = today - timedelta(days=7)
    if day == today:
        return "today"
    if day == yesterday:
        return "yesterday"
    if week_floor <= day < yesterday:
        return "week"
    return "older"


def _list_time(value, bucket: str) -> str:
    value = _aware(value)
    if not value:
        return "—"
    local = value.astimezone(CET)
    if bucket in {"today", "yesterday"}:
        return local.strftime("%H:%M")
    if bucket == "week":
        return local.strftime("%a %H:%M")
    return local.strftime("%Y-%m-%d")


def _sort_key(item: dict):
    return _aware(item.get("started_at")) or datetime.min.replace(tzinfo=ZoneInfo("UTC"))


def _run_title(row: dict) -> str:
    title = (row.get("title") or "").strip()
    if title:
        return title
    run_id = row.get("run_id") or ""
    if run_id:
        try:
            filled = conversation_run_store.backfill_run_title(run_id)
            if filled:
                return filled
        except Exception:
            pass
    sources = row.get("sources") or []
    if sources:
        return ", ".join(sources)
    return row.get("end_reason") or "Conversation"


def _main_items(rows: list[dict]) -> list[dict]:
    items = []
    for row in rows:
        started = row.get("started_at")
        bucket = _bucket_for(started)
        items.append({
            "id": row.get("run_id", ""),
            "store": "run",
            "channel": "chat",
            "channel_label": "Chat",
            "bucket": bucket,
            "time": _list_time(started, bucket),
            "started_at": started,
            "state": row.get("state") or "unknown",
            "title": _run_title(row),
            "meta": f"{int(row.get('llm_call_count') or 0)} calls · ${float(row.get('cost_total') or 0):.4f}",
        })
    return items


def _tick_items(ticks: list[dict]) -> list[dict]:
    items = []
    for tick in ticks:
        storage_channel = tick.get("channel_id") or "worker"
        channel = _filter_for_channel(storage_channel)
        note = tick.get("notification") or f"{_CHANNEL_LABELS.get(channel, channel)} tick"
        started = tick.get("started_at")
        bucket = _bucket_for(started)
        items.append({
            "id": tick.get("tick_id", ""),
            "store": "tick",
            "channel": channel,
            "channel_label": _CHANNEL_LABELS.get(channel, storage_channel.title()),
            "bucket": bucket,
            "time": _list_time(started, bucket),
            "started_at": started,
            "state": tick.get("worker_status") or tick.get("state") or "unknown",
            "title": note,
            "meta": f"{int(tick.get('llm_call_count') or 0)} calls · ${float(tick.get('cost_total') or 0):.4f}",
        })
    return items


def _group_sections(items: list[dict]) -> list[dict]:
    by_bucket: dict[str, list] = {key: [] for key, _ in BUCKET_ORDER}
    for item in items:
        by_bucket.setdefault(item.get("bucket") or "older", []).append(item)
    sections = []
    for key, label in BUCKET_ORDER:
        bucket_items = by_bucket.get(key) or []
        if not bucket_items:
            continue
        sections.append({"id": key, "label": label, "rows": bucket_items})
    return sections


def history_for_run(run_id: str) -> tuple[list[dict], list[dict]]:
    """Return (display history, protocol history) for a conversation run.

    Prefer protocol messages for the transcript so thoughts and tool cards
    survive the post-stream rehydrate (same shape as the live SSE turn).
    Fall back to direct_user / direct_reply events when protocol is empty.
    """
    events = conversation_run_store.events_for_run(run_id)
    direct = conversation_run_store.events_for_run(run_id, visibility="user")
    protocol = ui_ctx.serialize_chat_history(_as_messages(events))
    history = protocol or _direct_history(direct)
    return history, protocol


def active_main_history() -> tuple[list[dict], str | None]:
    """Overlay history for the active main-channel run, if any."""
    run = conversation_run_store.active_run("main")
    if not run:
        return [], None
    run_id = run["run_id"]
    history, _protocol = history_for_run(run_id)
    return history, run_id


def _load_main_detail(run_id: str) -> dict | None:
    run = conversation_run_store.get_run(run_id)
    if run is None:
        return None
    history, protocol = history_for_run(run_id)
    return {
        "store": "run",
        "channel": "chat",
        "id": run_id,
        "title": _run_title(run),
        "state": run.get("state") or "unknown",
        "started_label": _fmt_time(run.get("started_at")),
        "date": _cet_date(run.get("started_at")),
        "stats": {
            "events": run.get("event_count") or 0,
            "calls": run.get("llm_call_count") or 0,
            "tokens": run.get("token_total") or 0,
            "cost": run.get("cost_total") or 0,
        },
        "history": history,
        "protocol": protocol if history is not protocol else [],
        "system_versions": _jsonable(run.get("system_prompt_versions") or []),
        "checkpoints": _jsonable(conversation_run_store.checkpoints_for_run(run_id)),
        "calls": _jsonable(conversation_run_store.calls_for_run(run_id)),
        "record": _jsonable(run),
        "continuable": True,
        "user_label": "You",
        "assistant_label": "Agent",
        "page_context": ui_ctx.chat_detail(run_id),
    }


def _load_tick_detail(tick_id: str) -> dict | None:
    tick = worker_tick_store.get_tick(tick_id)
    if tick is None:
        return None
    storage_channel = tick.get("channel_id") or "worker"
    channel = _filter_for_channel(storage_channel)
    events = worker_tick_store.events_for_tick(tick_id)
    history = ui_ctx.serialize_chat_history(_events_to_messages(events))
    return {
        "store": "tick",
        "channel": channel,
        "id": tick_id,
        "title": _preview(
            tick.get("notification") or tick.get("user_prompt") or f"{channel} tick",
            96,
        ),
        "state": tick.get("worker_status") or tick.get("state") or "unknown",
        "started_label": _fmt_time(tick.get("started_at")),
        "date": tick.get("day_cet") or _cet_date(tick.get("started_at")),
        "stats": {
            "events": len(events),
            "calls": tick.get("llm_call_count") or 0,
            "tokens": tick.get("token_total") or 0,
            "cost": tick.get("cost_total") or 0,
            "duration_ms": tick.get("duration_ms") or 0,
        },
        "history": history,
        "protocol": [],
        "system_versions": _jsonable(tick.get("system_prompt_versions") or []),
        "checkpoints": [],
        "calls": _jsonable(worker_tick_store.calls_for_tick(tick_id)),
        "record": _jsonable(tick),
        "user_label": _CHANNEL_LABELS.get(channel, storage_channel.title()),
        "assistant_label": "Agent",
        "page_context": ui_ctx.chat_tick_detail(tick_id, tick.get("day_cet", "")),
    }


def _load_detail(item_or_id: dict | str, *, prefer: str | None = None) -> dict | None:
    if isinstance(item_or_id, dict):
        if item_or_id.get("store") == "run":
            return _load_main_detail(item_or_id["id"])
        return _load_tick_detail(item_or_id["id"])
    selected_id = item_or_id
    if prefer == "run":
        detail = _load_main_detail(selected_id)
        return detail or _load_tick_detail(selected_id)
    if prefer == "tick":
        detail = _load_tick_detail(selected_id)
        return detail or _load_main_detail(selected_id)
    return _load_tick_detail(selected_id) or _load_main_detail(selected_id)


def _detail_payload(detail: dict, *, stream_attachable: bool = False) -> dict:
    """JSON body for GET /chats/detail — transcript + meta, no page_context."""
    record = detail.get("record") or {}
    storage_channel = (
        record.get("channel_id")
        if detail.get("store") == "tick"
        else "main"
    ) or "worker"
    return {
        "store": detail.get("store"),
        "channel": detail.get("channel"),
        "id": detail.get("id"),
        "title": detail.get("title"),
        "state": detail.get("state"),
        "started_label": detail.get("started_label"),
        "date": detail.get("date"),
        "stats": detail.get("stats") or {},
        "history": detail.get("history") or [],
        "protocol": detail.get("protocol") or [],
        "system_versions": detail.get("system_versions") or [],
        "checkpoints": detail.get("checkpoints") or [],
        "calls": detail.get("calls") or [],
        "user_prompt": record.get("user_prompt") if detail.get("store") == "tick" else None,
        "user_label": detail.get("user_label"),
        "assistant_label": detail.get("assistant_label"),
        "continuable": bool(detail.get("continuable")),
        "stream_channel": storage_channel if detail.get("store") == "tick" else "main",
        "stream_attachable": bool(stream_attachable),
    }


def _parse_page(raw) -> int:
    try:
        return max(1, int(raw or 1))
    except (TypeError, ValueError):
        return 1


def _normalize_kind(raw: str | None) -> str:
    kind = (raw or "chat").strip().lower()
    if kind == "main":
        return "chat"
    if kind == "reflection":
        return "ambient"
    return kind


def _public_item(item: dict) -> dict:
    return {
        "id": item.get("id", ""),
        "store": item.get("store"),
        "channel": item.get("channel"),
        "channel_label": item.get("channel_label"),
        "bucket": item.get("bucket"),
        "bucket_label": dict(BUCKET_ORDER).get(item.get("bucket") or "older", "Older"),
        "time": item.get("time"),
        "state": item.get("state"),
        "title": item.get("title"),
        "meta": item.get("meta"),
    }


def _collect_items(kind: str, *, page: int = 1) -> tuple[list[dict], bool, bool, object | None]:
    """Return (page_items, has_more, db_configured, active_run).

    Uses limit+1 instead of count_documents — exact totals are an Atlas RTT tax.
    """
    main_ok = conversation_run_store.is_configured()
    tick_ok = worker_tick_store.is_configured()
    db_configured = main_ok or tick_ok
    page = max(1, int(page or 1))
    offset = (page - 1) * PAGE_SIZE
    fetch = PAGE_SIZE + 1
    want_active = main_ok and kind in {"chat", "all"}
    active_f = (
        _LIST_POOL.submit(lambda: conversation_run_store.active_run(lean=True))
        if want_active else None
    )

    if kind == "chat":
        rows = conversation_run_store.recent_runs(fetch, skip=offset) if main_ok else []
        items = _main_items(rows)
        items.sort(key=_sort_key, reverse=True)
        has_more = len(items) > PAGE_SIZE
        active = active_f.result() if active_f is not None else None
        return items[:PAGE_SIZE], has_more, db_configured, active

    if kind in _TICK_FILTERS:
        channel = _storage_channel(kind)
        ticks = (
            worker_tick_store.recent_ticks(fetch, channel_id=channel, skip=offset)
            if tick_ok else []
        )
        items = _tick_items(ticks)
        items.sort(key=_sort_key, reverse=True)
        has_more = len(items) > PAGE_SIZE
        return items[:PAGE_SIZE], has_more, db_configured, None

    # Merged "all": top-N of a merge is contained in top-N of each source.
    need = offset + fetch
    runs_f = (
        _LIST_POOL.submit(conversation_run_store.recent_runs, need)
        if main_ok else None
    )
    ticks_f = (
        _LIST_POOL.submit(worker_tick_store.recent_ticks, need)
        if tick_ok else None
    )
    items: list[dict] = []
    if runs_f is not None:
        items.extend(_main_items(runs_f.result()))
    if ticks_f is not None:
        items.extend(_tick_items(ticks_f.result()))
    active = active_f.result() if active_f is not None else None
    items.sort(key=_sort_key, reverse=True)
    page_items = items[offset: offset + fetch]
    has_more = len(page_items) > PAGE_SIZE
    return page_items[:PAGE_SIZE], has_more, db_configured, active


def register_chats_board(app, agent=None):
    bp = Blueprint("chats_board", __name__)

    @app.context_processor
    def _inject_chat_nav_defaults():
        """Chat filters everywhere; history rail on non-chats pages too."""
        out = {"chat_filters": FILTERS}
        ep = request.endpoint or ""
        path = request.path or ""
        if (
            ep.startswith("chats_board.")
            or path.startswith("/api/")
            or path.startswith("/static/")
        ):
            return out
        try:
            items, has_more, db_configured, _active = _collect_items("chat", page=1)
            out.update({
                "sections": _group_sections(items),
                "has_more": has_more,
                "db_configured": db_configured,
            })
        except Exception:
            out.update({
                "sections": [],
                "has_more": False,
                "db_configured": False,
            })
        return out

    @app.template_filter("run_time")
    def _run_time(value):
        if not value:
            return "—"
        value = _aware(value)
        return value.astimezone(CET).strftime("%H:%M:%S")

    @app.template_filter("run_duration")
    def _run_duration(value):
        seconds = int((value or 0) / 1000)
        return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"

    @app.template_filter("duration")
    def _duration(value):
        ms = int(value or 0)
        if ms < 1000:
            return f"{ms} ms"
        seconds = ms // 1000
        return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"

    @app.template_filter("utc_cet_time")
    def _utc_cet_time(value):
        return _run_time(value)

    @app.template_filter("usd")
    def _usd(value):
        try:
            return f"${float(value or 0):,.4f}"
        except (TypeError, ValueError):
            return "$0.0000"

    @bp.route("/chats/items")
    def chats_items():
        kind = _normalize_kind(request.args.get("kind"))
        if kind not in _FILTER_IDS:
            abort(400)
        page = _parse_page(request.args.get("page"))
        items, has_more, db_configured, _active = _collect_items(kind, page=page)
        if not db_configured:
            return jsonify({
                "items": [], "page": page, "page_size": PAGE_SIZE, "has_more": False,
            })
        return jsonify({
            "items": [_public_item(item) for item in items],
            "page": page,
            "page_size": PAGE_SIZE,
            "has_more": has_more,
        })

    @bp.route("/chats/detail")
    def chats_detail():
        """Load one chat/tick transcript on demand (not with the list shell)."""
        kind = _normalize_kind(request.args.get("kind"))
        selected_id = (request.args.get("id") or "").strip()
        if not selected_id:
            abort(400)
        if kind not in _FILTER_IDS:
            abort(400)
        prefer = "run" if kind == "chat" else ("tick" if kind in _TICK_FILTERS else None)
        detail = _load_detail(selected_id, prefer=prefer)
        if detail is None:
            abort(404)
        stream_attachable = False
        if (
            agent is not None
            and detail.get("store") == "tick"
            and (detail.get("state") or "") == "running"
        ):
            storage_channel = (detail.get("record") or {}).get("channel_id") or "worker"
            try:
                stream_attachable = bool(
                    agent.conversation_queue.has_stream(storage_channel)
                )
            except Exception:
                stream_attachable = False
        return jsonify(_detail_payload(detail, stream_attachable=stream_attachable))

    @bp.route("/chats")
    def chats_index():
        kind = _normalize_kind(request.args.get("kind"))
        if kind not in _FILTER_IDS:
            abort(400)
        selected_id = (request.args.get("id") or "").strip()

        # List shell only — names/meta. Transcript loads via GET /chats/detail.
        items, has_more, db_configured, active = _collect_items(kind, page=1)
        if active and not (active.get("title") or "").strip():
            filled = conversation_run_store.backfill_run_title(active.get("run_id") or "")
            if filled:
                active = dict(active)
                active["title"] = filled
        sections = _group_sections(items)
        page_ids = [item["id"] for item in items]
        page_context = ui_ctx.chats_index(_today(), page_ids)

        # Chat filter with no selection → new-chat landing (plan/progress + prompt).
        # Other filters still open the newest item.
        if not selected_id and items and kind != "chat":
            return redirect(url_for(
                "chats_board.chats_index",
                kind=kind,
                id=items[0]["id"],
            ))

        new_chat = kind == "chat" and not selected_id
        from .todo_board import dashboard_todo_vars
        todo = dashboard_todo_vars()

        return render_template(
            "chats/chat.html",
            kind=kind,
            filters=FILTERS,
            sections=sections,
            items=items,
            selected_id=selected_id,
            active=active,
            db_configured=db_configured,
            page=1,
            page_size=PAGE_SIZE,
            has_more=has_more,
            page_context=page_context,
            new_chat=new_chat,
            saved=request.args.get("saved"),
            **todo,
        )

    @bp.route("/chats/user/<run_id>")
    def chat_user_detail(run_id: str):
        detail = _load_main_detail(run_id)
        if detail is None:
            abort(404)
        return redirect(url_for(
            "chats_board.chats_index",
            kind="chat",
            id=run_id,
        ))

    @bp.route("/runs")
    def runs_legacy_index():
        args = request.args.to_dict()
        if args.get("kind") == "main":
            args["kind"] = "chat"
        elif args.get("kind") == "reflection":
            args["kind"] = "ambient"
        return redirect(url_for("chats_board.chats_index", **args))

    @bp.route("/runs/user/<run_id>")
    def runs_legacy_user(run_id: str):
        return chat_user_detail(run_id)

    @bp.route("/worker-runs")
    def worker_runs_redirect():
        args = {k: v for k, v in request.args.to_dict().items() if k != "date"}
        args.setdefault("kind", "worker")
        return redirect(url_for("chats_board.chats_index", **args))

    @bp.route("/worker-runs/<tick_id>")
    def worker_run_redirect(tick_id: str):
        detail = _load_tick_detail(tick_id)
        if detail is None:
            abort(404)
        channel = detail.get("channel") or "worker"
        kind = channel if channel in _TICK_FILTERS else "worker"
        return redirect(url_for(
            "chats_board.chats_index",
            kind=kind,
            id=tick_id,
        ))

    app.register_blueprint(bp)

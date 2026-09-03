"""Tower UI — "Palace": memory palace browser.

Browse and administer the memory palace along its own real dimensions (wing,
room, hall, drawer) plus the knowledge graph, the learned-memory pipeline, and
the daily logs. Reads go straight at the palace/consolidation stores (Tower
runs in the same process as the agent).

Two write paths, deliberately different:

- **Creating memory** goes through the same typed commit pipeline the agent's
  `learn` tool and the consolidators use (`consolidation.commit_candidate`,
  source="tower") — so Tower-taught content gets an identity, dedupe,
  provenance, an auto-authored recall trigger and graph edges, exactly like
  anything the agent learns. The KG editor routes through the same pipeline
  (kg_invalidate + kg_triplets), preserving temporal history.
- **Administering storage** (editing a drawer's text, re-filing, deleting)
  stays raw: it is repair of what exists, not new memory. Editing a drawer's
  text triggers a scoped single-record re-embed (see
  `harness/palace.py:update_drawer`) — never a full re-index.
"""

import logging
from pathlib import Path

from flask import Blueprint, abort, redirect, render_template, request, url_for

from harness import palace
from . import ui_context as ui_ctx

log = logging.getLogger("galadriel.tower.palace")

PAGE_SIZE = 50


def register_palace_browser(app, run_async=None):
    """Register the Palace (memory) UI routes on the Flask app.

    `run_async` executes a coroutine from Flask's sync handlers on the agent's
    running loop (falling back to a fresh loop) — required for the learned-
    memory pages and typed create, which use the async operational-DB driver.
    """
    bp = Blueprint("palace_browser", __name__)

    def _await(coro):
        if run_async is None:
            import asyncio
            return asyncio.run(coro)
        return run_async(coro)

    @bp.route("/palace")
    def palace_index():
        data = palace.taxonomy_data()
        notice = request.args.get("notice")
        return render_template(
            "palace/index.html", data=data, notice=notice,
            page_context=ui_ctx.palace_index(list(data.get("wings", {}).keys())),
        )

    @bp.route("/palace/reconcile", methods=["POST"])
    def palace_reconcile():
        result = palace.reconcile_sync()
        notice = "; ".join(result["steps"])
        if not result["ok"]:
            notice = "Reconcile incomplete: " + notice
        # url_for already percent-encodes query params — pre-quoting double-
        # encoded the banner into %20 soup.
        return redirect(url_for("palace_browser.palace_index", notice=notice))

    @bp.route("/palace/browse")
    def palace_browse():
        """Rooms within a wing, as folder tiles — the wing → room step of
        wing → room → drawer navigation (drawer-level browsing happens on
        /palace/room, same as the hall shortcut)."""
        wing = request.args.get("wing", "")
        rooms = palace.taxonomy_data()["wings"].get(wing)
        if rooms is None:
            abort(404)
        return render_template(
            "palace/browse.html", wing=wing, rooms=rooms,
            page_context=ui_ctx.palace_browse(wing, list(rooms.keys())),
        )

    @bp.route("/palace/room")
    def palace_room():
        wing = request.args.get("wing") or None
        room = request.args.get("room") or None
        hall = request.args.get("hall") or None
        page = max(1, int(request.args.get("page", 1)))
        offset = (page - 1) * PAGE_SIZE
        result = palace.list_drawers(wing=wing, room=room, hall=hall, limit=PAGE_SIZE, offset=offset)
        return render_template(
            "palace/room.html",
            wing=wing, room=room, hall=hall, page=page, page_size=PAGE_SIZE,
            total=result["total"], drawers=result["drawers"],
            notice=request.args.get("notice"),
            page_context=ui_ctx.palace_room(
                wing, room, hall,
                [d["id"] for d in result["drawers"]],
                page=page,
            ),
        )

    @bp.route("/palace/teach", methods=["POST"])
    def palace_teach():
        """Create memory through the shared typed pipeline — never a raw
        drawer write. The commit mints the memory_id, files the drawer (room
        derived from type), dedupes, and schedules trigger + edge authoring."""
        from harness import consolidation

        type_ = (request.form.get("type") or "semantic").strip()
        content = (request.form.get("content") or "").strip()
        topic = (request.form.get("topic") or "").strip() or None
        if not content:
            return redirect(url_for(
                "palace_browser.palace_learned", notice="Content is required.",
            ))
        result = _await(consolidation.commit_candidate(
            type=type_, content=content, topic=topic, source="tower",
        ))
        if result["status"] == "committed":
            return redirect(url_for(
                "palace_browser.palace_memory_detail", memory_id=result["memory_id"],
            ))
        return redirect(url_for(
            "palace_browser.palace_learned",
            notice=f"[{result['status']}] {result['detail']}",
        ))

    @bp.route("/palace/learned")
    def palace_learned():
        page = max(1, int(request.args.get("page", 1)))
        offset = (page - 1) * PAGE_SIZE
        from harness import consolidation
        result = _await(consolidation.list_committed_memories(
            limit=PAGE_SIZE, offset=offset,
        ))
        return render_template(
            "palace/learned.html", page=page, page_size=PAGE_SIZE,
            total=result["total"], memories=result["memories"],
            notice=request.args.get("notice"),
            page_context=ui_ctx.palace_learned(
                [m["memory_id"] for m in result["memories"]], page=page,
            ),
        )

    @bp.route("/palace/memory/<memory_id>")
    def palace_memory_detail(memory_id):
        from harness import consolidation
        overview = _await(consolidation.memory_admin_overview(memory_id))
        if overview is None:
            abort(404)
        trigger = (overview["memory"].get("trigger") or {})
        return render_template(
            "palace/memory.html", overview=overview, memory=overview["memory"],
            trigger=trigger,
            page_context=ui_ctx.palace_memory(
                memory_id, recall_id=trigger.get("recall_id"),
            ),
        )

    @bp.route("/palace/drawer/<path:drawer_id>")
    def palace_drawer(drawer_id):
        drawer = palace.get_drawer(drawer_id)
        if drawer is None:
            abort(404)
        return render_template(
            "palace/drawer.html",
            drawer=drawer,
            saved=request.args.get("saved"),
            back_wing=drawer["wing"],
            back_room=drawer["room"],
            page_context=ui_ctx.palace_drawer(
                drawer_id, drawer["wing"], drawer["room"], drawer.get("hall"),
            ),
        )

    @bp.route("/palace/drawer/<path:drawer_id>", methods=["POST"])
    def palace_drawer_save(drawer_id):
        if request.form.get("_action") == "delete":
            drawer = palace.get_drawer(drawer_id)
            if drawer is None:
                abort(404)
            palace.delete_drawer(drawer_id)
            palace.reconcile_sync()
            return redirect(url_for(
                "palace_browser.palace_room",
                wing=drawer["wing"], room=drawer["room"],
            ))
        text = request.form.get("text")
        wing = request.form.get("wing") or None
        room = request.form.get("room") or None
        hall = request.form.get("hall") or None
        result = palace.update_drawer(drawer_id, text=text, wing=wing, room=room, hall=hall)
        log.info(f"Tower palace edit: {result}")
        palace.reconcile_sync()
        return redirect(url_for("palace_browser.palace_drawer", drawer_id=drawer_id, saved=1))

    @bp.route("/palace/search")
    def palace_search_page():
        query = request.args.get("q", "").strip()
        wing = request.args.get("wing") or None
        room = request.args.get("room") or None
        hall = request.args.get("hall") or None
        results = palace.search_data(query, wing=wing, room=room, hall=hall) if query else []
        return render_template(
            "palace/search.html", query=query, wing=wing, room=room, hall=hall, results=results,
            page_context=ui_ctx.palace_search(
                query,
                [r.get("id") or r.get("drawer_id") for r in results if r.get("id") or r.get("drawer_id")],
                wing=wing, room=room, hall=hall,
            ),
        )

    @bp.route("/palace/kg")
    def palace_kg():
        entity = request.args.get("entity", "").strip()
        timeline = palace.kg_timeline(entity) if entity else None
        facts = palace.kg_list(limit=200)
        return render_template(
            "palace/kg.html", entity=entity, timeline=timeline, facts=facts,
            notice=request.args.get("notice"),
            page_context=ui_ctx.palace_kg(entity, len(facts)),
        )

    @bp.route("/palace/kg/edit", methods=["POST"])
    def palace_kg_edit():
        """Edit a KG fact = invalidate the old triple + file the corrected one,
        through the shared commit pipeline (one provenance record, history
        intact) — the same shape as `learn(kg_invalidate=…, kg_triplets=…)`."""
        from harness import consolidation

        old = [request.form.get("old_subject", ""),
               request.form.get("old_predicate", ""),
               request.form.get("old_object", "")]
        new = [(request.form.get("subject") or "").strip(),
               (request.form.get("predicate") or "").strip(),
               (request.form.get("object") or "").strip()]
        # An untouched form (new == old) is a no-op, NOT a retirement. Blanking
        # ALL THREE fields is the deliberate retire-without-replacement
        # gesture; a partially blanked form is almost certainly a mid-edit
        # slip, so it bounces back instead of silently retiring the fact and
        # discarding the typed half of the replacement.
        if any(new) and not all(new):
            return redirect(url_for(
                "palace_browser.palace_kg",
                notice="Nothing changed: fill all three fields to replace the "
                       "fact, or blank all three to retire it.",
            ))
        if all(old) and new != old:
            result = _await(consolidation.commit_candidate(
                type="semantic",
                kg_triplets=[new] if all(new) else None,
                kg_invalidate=[old],
                source="tower",
            ))
            return redirect(url_for(
                "palace_browser.palace_kg", notice=result["detail"],
            ))
        return redirect(url_for("palace_browser.palace_kg"))

    @bp.route("/palace/daily")
    def palace_daily():
        """Read-only view of the daily-log files — the ~48h working-memory
        index. Only yesterday+today ever reach the agent's prompt; older files
        sit here for the operator's eyes only."""
        memory_dir = Path("memory")
        files = sorted(memory_dir.glob("????-??-??.md"), reverse=True)[:14]
        logs = []
        for f in files:
            try:
                logs.append({"date": f.stem, "text": f.read_text(encoding="utf-8")})
            except Exception as e:
                logs.append({"date": f.stem, "text": f"(unreadable: {e})"})
        # "(injected)" must mean what memory.py:build_dynamic_text does: the
        # files named exactly today/yesterday in the agent timezone — not
        # whichever two files happen to be newest.
        from datetime import timedelta
        from harness import tower_settings
        try:
            now = tower_settings.agent_now()
        except Exception:
            from datetime import datetime
            now = datetime.now()
        injected_dates = {
            (now - timedelta(days=delta)).strftime("%Y-%m-%d") for delta in (0, 1)
        }
        return render_template(
            "palace/daily.html", logs=logs, injected_dates=injected_dates,
            page_context=ui_ctx.palace_daily([l["date"] for l in logs]),
        )

    app.register_blueprint(bp)

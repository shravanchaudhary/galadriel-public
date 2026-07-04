"""Tower UI — "Palace": memory palace browser.

Browse and edit the memory palace along its own real dimensions (wing, room,
hall, drawer) plus the knowledge graph and diary. Everything here reads and
writes the palace directly and synchronously — Tower runs in the same
process as the agent, so no subprocess/IPC is needed (same pattern as the
existing `palace_search` / `palace_taxonomy` agent tools).

Editing a drawer's text triggers a scoped, single-record re-embed (see
`harness/palace.py:update_drawer` for the mechanics) — never a full
`mempalace mine` re-index. Editing a KG fact goes through invalidate+add
(`kg_invalidate` + `kg_add`) rather than raw mutation, matching the KG's
temporal-fact design (valid_from/valid_to) instead of fighting it. Diary
entries are stored as drawers (room="diary"), so they're browsed and edited
through the exact same drawer views as everything else.
"""

import logging

from flask import Blueprint, abort, redirect, render_template, request, url_for

from harness import palace
from . import ui_context as ui_ctx

log = logging.getLogger("galadriel.tower.palace")

PAGE_SIZE = 50


def register_palace_browser(app):
    """Register the Palace (memory) UI routes on the Flask app."""
    bp = Blueprint("palace_browser", __name__)

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
        from urllib.parse import quote
        return redirect(url_for("palace_browser.palace_index", notice=quote(notice)))

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

    @bp.route("/palace/drawer/create", methods=["POST"])
    def palace_drawer_create():
        text = request.form.get("text", "")
        wing = request.form.get("wing") or palace.DEFAULT_WING
        room = request.form.get("room") or "general"
        hall = request.form.get("hall") or "general"
        created = palace.create_drawer(text, wing=wing, room=room, hall=hall)
        if created.get("error"):
            from urllib.parse import quote
            return redirect(url_for(
                "palace_browser.palace_room",
                wing=wing, room=room, hall=hall,
                notice=quote(created["error"]),
            ))
        palace.reconcile_sync()
        return redirect(url_for("palace_browser.palace_drawer", drawer_id=created["id"], saved=1))

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
            page_context=ui_ctx.palace_kg(entity, len(facts)),
        )

    @bp.route("/palace/kg/edit", methods=["POST"])
    def palace_kg_edit():
        """Edit a KG fact = invalidate the old triple + file the corrected
        one. Keeps the KG's temporal history intact instead of mutating a
        fact in place."""
        old_s = request.form.get("old_subject", "")
        old_p = request.form.get("old_predicate", "")
        old_o = request.form.get("old_object", "")
        new_s = request.form.get("subject", old_s)
        new_p = request.form.get("predicate", old_p)
        new_o = request.form.get("object", old_o)
        if old_s and old_p and old_o:
            palace.kg_invalidate(old_s, old_p, old_o)
        if new_s and new_p and new_o:
            palace.kg_add(new_s, new_p, new_o)
        return redirect(url_for("palace_browser.palace_kg"))

    @bp.route("/palace/diary")
    def palace_diary():
        page = max(1, int(request.args.get("page", 1)))
        offset = (page - 1) * PAGE_SIZE
        result = palace.list_drawers(
            wing=palace.DEFAULT_WING, room="diary", limit=PAGE_SIZE, offset=offset,
        )
        return render_template(
            "palace/diary.html", page=page, page_size=PAGE_SIZE,
            total=result["total"], drawers=result["drawers"],
            page_context=ui_ctx.palace_diary(
                [d["id"] for d in result["drawers"]], page=page,
            ),
        )

    app.register_blueprint(bp)

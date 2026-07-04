"""Tower UI — generic workflow screens.

Read-only views of MongoDB state (table, kanban, detail+timeline) plus
an approval inbox, all auto-rendered from the workflow specs
(`harness/workflows.py`). The screens never hardcode an entity — add a
`workflows/*.json` spec and they light up automatically.

Rendering uses a synchronous `pymongo.MongoClient` (read-only), kept isolated
from the agent's async connector. The one mutation — advancing a doc's state
from the approval inbox — goes through the same `db_move_state` primitive the
agent uses (run on the agent's event loop), so the state machine is enforced
identically from the UI.
"""

import asyncio
import os
import re
from datetime import datetime, timezone
from flask import Blueprint, abort, redirect, render_template, request, url_for
from markupsafe import Markup, escape
from pymongo import MongoClient

from harness import workflows as wf
from harness import db_ops
from . import ui_context as ui_ctx

_sync_db = None

_URL_RE = re.compile(r'https?://[^\s<>"\']+')
_URL_TRAILING_PUNCT = ".,;:!?)]}'\""


def _linkify(text: str) -> Markup:
    """Escape text and turn any http(s) URL inside it into a clickable link
    that opens in a new tab (e.g. LinkedIn profile URLs in workflow data)."""
    pieces = []
    last = 0
    for m in _URL_RE.finditer(text):
        start, end = m.start(), m.end()
        url = m.group(0)
        trail = ""
        while url and url[-1] in _URL_TRAILING_PUNCT:
            trail = url[-1] + trail
            url = url[:-1]
            end -= 1
        pieces.append(escape(text[last:start]))
        esc_url = escape(url)
        pieces.append(f'<a href="{esc_url}" target="_blank" rel="noopener noreferrer">{esc_url}</a>')
        last = end
    pieces.append(escape(text[last:]))
    return Markup("".join(pieces))


def _db():
    """Return a cached synchronous DB handle for read rendering, or None if the
    DB isn't configured."""
    global _sync_db
    if _sync_db is not None:
        return _sync_db
    uri = os.environ.get("MONGO_URI")
    name = os.environ.get("MONGO_DB")
    if not uri or not name:
        return None
    _sync_db = MongoClient(uri)[name]
    return _sync_db


def _visible(spec) -> bool:
    return spec is not None and not spec.hidden


def _approval_docs(registry, db):
    """Yield (spec, doc) for every doc sitting in an approval state."""
    if db is None:
        return
    for spec in registry.values():
        if spec.hidden or not spec.approval_states:
            continue
        for doc in db[spec.collection].find({"status": {"$in": spec.approval_states}}):
            yield spec, doc



def register_workflows(app, scheduler=None):
    """Register the workflow UI routes on the Flask app."""
    bp = Blueprint("workflows", __name__)

    @app.template_filter("humantime")
    def _humantime(value):
        """Render a datetime as a <time> element carrying the full ISO string.
        Client-side JS turns it into "abs date (relative)" and shows the ISO in
        a hover tooltip. Naive datetimes are assumed UTC (some records are
        written tz-naive) so the browser doesn't misread them as local time.
        String values containing a URL get auto-linked; everything else passes
        through untouched."""
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            iso = escape(value.isoformat())
            return Markup(f'<time class="rel" datetime="{iso}">{iso}</time>')
        if isinstance(value, str) and "://" in value:
            return _linkify(value)
        return value

    @app.template_filter("linkify")
    def _linkify_filter(value):
        """Standalone version of the URL-linking half of `humantime`, for
        text fields (notes, timeline events) that aren't dates."""
        if isinstance(value, str) and "://" in value:
            return _linkify(value)
        return value

    def _approvals_count():
        db = _db()
        return sum(1 for _ in _approval_docs(wf.load_registry(), db))

    @app.context_processor
    def _inject_nav():
        # Make the pending-approval count available to every workflow template.
        try:
            return {"approvals_count": _approvals_count()}
        except Exception:
            return {"approvals_count": 0}

    @bp.route("/workflows")
    def wf_index():
        registry = wf.load_registry()
        db = _db()
        items = []
        for spec in registry.values():
            if spec.hidden:
                continue
            count = db[spec.collection].count_documents({}) if db is not None else 0
            items.append({"spec": spec, "count": count})
        items.sort(key=lambda i: (i["spec"].workflow, i["spec"].name))
        page_context = ui_ctx.workflow_index([
            {"entity": i["spec"].name, "workflow": i["spec"].workflow}
            for i in items
        ])
        return render_template(
            "workflows/index.html", items=items, db_ok=db is not None,
            page_context=page_context,
        )

    @bp.route("/w/<entity>")
    def wf_table(entity):
        spec = wf.load_registry().get(entity)
        if not _visible(spec):
            abort(404)
        db = _db()
        status = request.args.get("status") or None
        query = {"status": status} if status else {}
        docs = list(db[spec.collection].find(query).limit(500)) if db is not None else []
        page_context = ui_ctx.workflow_table(
            spec.name, spec.key, docs,
            workflow=spec.workflow, status_filter=status,
            approval_states=spec.approval_states,
        )
        return render_template(
            "workflows/table.html", spec=spec, docs=docs, status=status,
            page_context=page_context,
        )

    @bp.route("/w/<entity>/kanban")
    def wf_kanban(entity):
        spec = wf.load_registry().get(entity)
        if not _visible(spec):
            abort(404)
        db = _db()
        columns = []
        for state in spec.states:
            docs = (
                list(db[spec.collection].find({"status": state}).limit(200))
                if db is not None
                else []
            )
            columns.append({"state": state, "docs": docs})
        page_context = ui_ctx.workflow_kanban(
            spec.name, spec.key, columns, workflow=spec.workflow,
        )
        return render_template(
            "workflows/kanban.html", spec=spec, columns=columns,
            page_context=page_context,
        )

    @bp.route("/w/<entity>/item")
    def wf_detail(entity):
        spec = wf.load_registry().get(entity)
        if not _visible(spec):
            abort(404)
        key = request.args.get("key")
        db = _db()
        doc = db[spec.collection].find_one({spec.key: key}) if db is not None else None
        if not doc:
            abort(404)
        history = list(reversed(doc.get("history", [])))
        allowed = spec.allowed_from(doc.get("status"))
        page_context = ui_ctx.workflow_detail(
            spec.name, spec.key, doc.get(spec.key), doc.get("status"), allowed,
            workflow=spec.workflow,
        )
        return render_template(
            "workflows/detail.html",
            spec=spec,
            doc=doc,
            history=history,
            allowed=allowed,
            page_context=page_context,
        )

    @bp.route("/approvals")
    def wf_approvals():
        registry = wf.load_registry()
        db = _db()
        cards = []
        for spec, doc in _approval_docs(registry, db):
            cards.append(
                {
                    "spec": spec,
                    "doc": doc,
                    "allowed": spec.allowed_from(doc.get("status")),
                }
            )
        page_context = ui_ctx.workflow_approvals([
            ui_ctx.workflow_approval_card(
                c["spec"].name, c["spec"].key,
                c["doc"].get(c["spec"].key), c["doc"].get("status"), c["allowed"],
            )
            for c in cards
        ])
        return render_template(
            "workflows/approvals.html", cards=cards, db_ok=db is not None,
            page_context=page_context,
        )

    @bp.route("/approvals/act", methods=["POST"])
    def wf_approvals_act():
        entity = request.form.get("entity", "")
        key = request.form.get("key", "")
        to = request.form.get("to", "")
        note = request.form.get("note") or "via Tower approval inbox"
        loop = scheduler._loop if scheduler else None
        if not (loop and loop.is_running()):
            abort(503)
        future = asyncio.run_coroutine_threadsafe(
            db_ops.move_state(entity, key, to, note), loop
        )
        try:
            future.result(timeout=30)
        except Exception:
            pass
        return redirect(url_for("workflows.wf_approvals"))

    app.register_blueprint(bp)

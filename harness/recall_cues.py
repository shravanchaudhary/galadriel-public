"""Generate a recall's trigger cues with an LLM, then prove they work.

A memory nobody can recall on cue is inert: it is only ever found when the
agent happens to search for it. Committing durable content therefore has to
produce a *trigger* as well as a store entry, and the trigger is the hard part
— Stage-1 matches on cosine against a cue array, so coverage has to be dense
and phrased the way people actually talk. A single agent turn, busy with the
user's task, will not hand-author eighty realistic phrasings. So this module
runs its own model pass for it.

The pass is only half the job. Generated cues are a hypothesis, and the
hypothesis is testable for free: run probe phrasings through the real matcher
and see what fires. Two rules make that test honest:

  * Probes are held out. A phrase stored as a cue would score 1.0 against
    itself (Stage-1 accepts on max cosine over the array), so testing with the
    stored cues measures memorisation, not generalisation. The model is asked
    for separate probe sets, deliberately phrased unlike the cues.
  * Both directions run the whole pipeline, Stage-1 *and* the Stage-2 judge.
    A judge false-rejection keeps a recall from firing just as effectively as
    a Stage-1 miss, so grading on Stage-1 alone would report a health the
    system does not have. This costs nothing extra in the common case: a probe
    that produces no Stage-1 candidate never reaches the judge in production
    either, so the judge only ever sees probes that got that far.

Failures feed back into a bounded repair round rather than being papered over.
The repair levers are ordered by blast radius — cues first, then the judge's
own fields, then dropping the over-broad cue, and only then this recall's
`positive_threshold`. Thresholds are per-recall (`recall_positive_threshold`),
so raising one cannot change another recall's accept/reject decision; it only
frees one of the three Stage-2 candidate slots. Lowering could take a slot
away from a sibling, so it is never lowered below the shared default.
"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("galadriel.recall_cues")

# Cue array sizes requested from the model. The stored positives are what
# Stage-1 embeds; `_MAX_EXAMPLES_PER_RECALL` (100) caps them downstream.
_WANT_POSITIVES = 60
_WANT_LEXICAL = 10
_WANT_NEGATIVES = 15
# Held-out probes, never stored. Small on purpose: each surviving probe costs
# one judge call, and 12 is enough to separate a working trigger from a broken
# one without turning every committed memory into a benchmark run.
_WANT_PROBES = 12

# Quality bars. A recall that fires for fewer than three-quarters of the
# phrasings it should catch is not doing its job; one that fires on a quarter
# of its look-alikes is adding noise to a matcher that already over-fires.
_MIN_RECALL_RATE = 0.75
_MAX_FP_RATE = 0.25
_MAX_REPAIR_ROUNDS = 2

# Threshold ceiling for the last-resort lever. Above this a recall effectively
# only answers to its lexical cues.
_MAX_THRESHOLD = 0.85
_THRESHOLD_STEP = 0.05

_MAX_CONDITION_CHARS = 240  # recall_judge truncates past this

_SYSTEM = """\
You write the trigger for a semantic-recall system. Given a memory that was \
just stored, produce the cues that decide WHEN it should be resurfaced later.

How the matcher works, so you can target it:
- Stage 1 embeds the conversation text and compares it to `positive_examples` \
by cosine, accepting on the best single match. Dense, varied, realistic \
phrasings win here. Paraphrases of the instruction do not.
- `lexical_cues` are exact substring matches and fire unconditionally. Use \
only high-precision anchors that would be bizarre in unrelated conversation.
- Stage 2 is an LLM judge that reads ONLY `activation_condition` and \
`exclusions`. Write the condition as a situation about the user or the text \
("The user asks ..."), never as an order to yourself.
- `negative_examples` are shown to the Stage-2 judge as known misfires. They \
do not affect Stage 1.

Return ONLY a JSON object with these keys:
  "instruction": short action pointer to where the memory lives — a tool, \
file, or palace room. One line. Never restate the memory itself.
  "activation_condition": one line, under 240 characters.
  "exclusions": one line naming the look-alikes that must NOT fire, under 240 \
characters.
  "positive_examples": array of ~%(pos)d realistic phrasings that should fire.
  "lexical_cues": array of ~%(lex)d exact high-precision anchors.
  "negative_examples": array of ~%(neg)d plausible misfires.
  "holdout_positives": array of %(probe)d MORE phrasings that should fire, \
worded as differently from positive_examples as you can manage while keeping \
the same intent. These are a held-out test set — reusing positive_examples \
wording makes the test meaningless.
  "holdout_negatives": array of %(probe)d phrasings that must NOT fire, \
distinct from negative_examples. Make these genuinely confusable: same topic \
or vocabulary, different intent."""

_REPAIR_NOTE = """\
A previous attempt was tested against the live matcher and failed.

%(failures)s

Revise the cues. Add coverage for the phrasings that failed to fire by writing \
DIFFERENT wordings around them — copying a failed probe into \
positive_examples makes the test pass without improving anything real, since \
Stage 1 accepts on the best single cosine match. For phrasings that fired but \
should not have, sharpen `exclusions` and `activation_condition`, and remove \
whichever positive_example was broad enough to pull them in. Return the same \
JSON shape, complete."""


def _prompt(pos: int, lex: int, neg: int, probe: int) -> str:
    return _SYSTEM % {"pos": pos, "lex": lex, "neg": neg, "probe": probe}


def _parse_json_object(raw: str) -> dict | None:
    """Pull a JSON object out of a model response, fenced or bare."""
    text = (raw or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _string_list(raw, limit: int) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen, out = set(), []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split()).strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _one_line(raw, limit: int = _MAX_CONDITION_CHARS) -> str:
    text = " ".join(str(raw or "").split()).strip()
    return text[:limit]


def normalize_generated(payload: dict | None) -> dict | None:
    """Coerce a model payload into a usable cue set, or None if unusable.

    Stage-1 cannot run without positives and `learn_recall` rejects a create
    without lexical cues, so those two plus the judge's condition are the hard
    requirements; everything else degrades to empty.
    """
    if not isinstance(payload, dict):
        return None
    cues = {
        "instruction": _one_line(payload.get("instruction"), 300),
        "activation_condition": _one_line(payload.get("activation_condition")),
        "exclusions": _one_line(payload.get("exclusions")),
        "positive_examples": _string_list(payload.get("positive_examples"), 100),
        "lexical_cues": _string_list(payload.get("lexical_cues"), 30),
        "negative_examples": _string_list(payload.get("negative_examples"), 40),
        "holdout_positives": _string_list(payload.get("holdout_positives"), 30),
        "holdout_negatives": _string_list(payload.get("holdout_negatives"), 30),
    }
    if not (cues["instruction"] and cues["activation_condition"]):
        return None
    if not (cues["positive_examples"] and cues["lexical_cues"]):
        return None
    return cues


def drop_probe_overlap(cues: dict) -> dict:
    """Remove probes that leaked into the stored cue arrays.

    A probe that is also a cue scores 1.0 against itself, so leaving the
    overlap in place would inflate the recall rate for free. Models produce
    this overlap routinely no matter how the prompt is worded, so it is
    filtered rather than trusted.
    """
    stored_pos = {c.casefold() for c in cues.get("positive_examples") or []}
    stored_neg = {c.casefold() for c in cues.get("negative_examples") or []}
    lexical = [c.casefold() for c in cues.get("lexical_cues") or []]

    def clean(probes: list[str], stored: set[str]) -> list[str]:
        out = []
        for probe in probes:
            key = probe.casefold()
            if key in stored:
                continue
            # A lexical cue inside the probe is a guaranteed hard trigger, so
            # such a probe tests the anchor, not the semantic coverage.
            if any(cue in key for cue in lexical):
                continue
            out.append(probe)
        return out

    cues = dict(cues)
    cues["holdout_positives"] = clean(cues.get("holdout_positives") or [], stored_pos)
    cues["holdout_negatives"] = clean(cues.get("holdout_negatives") or [], stored_neg)
    return cues


async def generate_cues(
    memory: str,
    *,
    memory_type: str = "semantic",
    topic: str | None = None,
    existing_recalls: list[dict] | None = None,
    failures: str = "",
) -> dict | None:
    """One model pass turning a stored memory into a full cue set.

    Follows the active chat model (see model_registry) — learning quality is
    not a place to save tokens. Returns None on any failure; the caller keeps
    the memory and leaves it triggerless rather than blocking the commit.
    """
    if not (memory or "").strip():
        return None
    try:
        from . import model_registry

        provider = model_registry.get_provider("recall_cues")
        model = model_registry.model_for("recall_cues")
    except Exception as e:
        log.warning("Recall cue provider unavailable: %s", e)
        return None

    parts = [f"Memory type: {memory_type}"]
    if topic:
        parts.append(f"Topic: {topic}")
    parts.append(f"Memory just stored:\n{memory.strip()[:4000]}")
    neighbours = _neighbour_digest(existing_recalls)
    if neighbours:
        parts.append(
            "Triggers that already exist. Keep your cues clear of these so the "
            "matcher does not have to choose between you and them:\n" + neighbours
        )
    if failures:
        parts.append(_REPAIR_NOTE % {"failures": failures})

    try:
        response = await provider.create_message(
            model=model,
            max_tokens=6000,
            system=_prompt(_WANT_POSITIVES, _WANT_LEXICAL, _WANT_NEGATIVES, _WANT_PROBES),
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
            thinking=False,
        )
    except Exception as e:
        log.warning("Recall cue generation failed: %s", e)
        return None

    text = " ".join(
        getattr(block, "text", "") or ""
        for block in (getattr(response, "content", None) or [])
        if getattr(block, "type", None) == "text" or getattr(block, "text", None)
    )
    cues = normalize_generated(_parse_json_object(text))
    if cues is None:
        log.warning("Recall cue generation returned an unusable payload.")
        return None
    return drop_probe_overlap(cues)


def _neighbour_digest(recalls: list[dict] | None, limit: int = 25) -> str:
    if not recalls:
        return ""
    lines = []
    for recall in recalls[:limit]:
        rid = recall.get("recall_id")
        condition = _one_line(
            recall.get("activation_condition") or recall.get("instruction"), 140
        )
        if rid and condition:
            lines.append(f"- [{rid}] {condition}")
    return "\n".join(lines)


# ─── Holdout testing ───────────────────────────────────────────────────────
# The candidate is tested before it is written. `scan_text_for_recalls` takes
# an explicit recall list, so an unsaved candidate can be appended to the live
# catalog in memory: the trigger is proven against real competition (including
# the three-slot Stage-2 cap) without a half-built recall ever going live.

_CANDIDATE_ID = "__cue_test_candidate__"


async def run_probes(
    candidate: dict,
    probes: list[str],
    *,
    catalog: list[dict] | None = None,
) -> list[tuple[str, bool, list[str]]]:
    """(probe, candidate_fired, other_recalls_that_fired) for each probe.

    Runs the full production path — Stage-1 match then the Stage-2 judge — so
    a judge false-rejection registers as a miss, the same way it would in a
    real turn.
    """
    from .recall import (
        fetch_all_recalls,
        filter_matches_with_judge,
        invalidate_semantic_router,
        scan_text_for_recalls,
    )

    if not probes:
        return []
    if catalog is None:
        catalog = await fetch_all_recalls()
    others = [r for r in (catalog or []) if r.get("recall_id") != candidate.get("recall_id")]
    pool = others + [candidate]

    results: list[tuple[str, bool, list[str]]] = []
    try:
        for probe in probes:
            matches = scan_text_for_recalls(
                probe, pool, segments=[{"text": probe, "source": "user"}],
            )
            fired: list[str] = []
            if matches:
                verified, _ = await filter_matches_with_judge(matches)
                fired = [m.get("recall_id") for m in verified]
            cid = candidate.get("recall_id")
            results.append((probe, cid in fired, [r for r in fired if r != cid]))
    finally:
        # The router cached a build that includes the unsaved candidate.
        invalidate_semantic_router()
    return results


async def test_cues(cues: dict, *, catalog: list[dict] | None = None) -> dict | None:
    """Score a cue set against its own held-out probes.

    Returns None when the matcher cannot answer — no encoder, or the recall
    system disarmed without a judge key. That is not a failing recall, it is an
    absent measurement, and reporting it as 0% would trigger repair rounds that
    cannot possibly help.
    """
    from .recall import recall_system_armed

    if not recall_system_armed():
        log.info("Recall system disarmed — skipping cue holdout test.")
        return None

    positives = cues.get("holdout_positives") or []
    negatives = cues.get("holdout_negatives") or []
    if not positives and not negatives:
        return None

    candidate = {
        "recall_id": _CANDIDATE_ID,
        "instruction": cues.get("instruction", ""),
        "activation_condition": cues.get("activation_condition", ""),
        "exclusions": cues.get("exclusions", ""),
        "positive_examples": cues.get("positive_examples") or [],
        "negative_examples": cues.get("negative_examples") or [],
        "lexical_cues": cues.get("lexical_cues") or [],
        "positive_threshold": cues.get("positive_threshold"),
        "source": "user",
    }
    try:
        pos_results = await run_probes(candidate, positives[:_WANT_PROBES], catalog=catalog)
        neg_results = await run_probes(candidate, negatives[:_WANT_PROBES], catalog=catalog)
    except Exception as e:
        log.warning("Cue holdout test failed: %s", e)
        return None

    missed = [(p, others) for p, fired, others in pos_results if not fired]
    false_fires = [p for p, fired, _ in neg_results if fired]
    recall_rate = (
        (len(pos_results) - len(missed)) / len(pos_results) if pos_results else None
    )
    fp_rate = len(false_fires) / len(neg_results) if neg_results else None
    return {
        "recall_rate": recall_rate,
        "fp_rate": fp_rate,
        "probes_positive": len(pos_results),
        "probes_negative": len(neg_results),
        "missed": missed,
        "false_fires": false_fires,
    }


def meets_bar(scores: dict | None) -> bool:
    """True when the measurement is absent or good enough to ship."""
    if not scores:
        return True
    recall_rate = scores.get("recall_rate")
    fp_rate = scores.get("fp_rate")
    if recall_rate is not None and recall_rate < _MIN_RECALL_RATE:
        return False
    if fp_rate is not None and fp_rate > _MAX_FP_RATE:
        return False
    return True


def describe_failures(scores: dict) -> str:
    """The specific evidence handed back to the model for a repair round."""
    lines = []
    missed = scores.get("missed") or []
    if missed:
        lines.append(
            f"These {len(missed)} phrasing(s) should have fired and did not:"
        )
        for probe, others in missed[:10]:
            stolen = f"  (fired {', '.join(others)} instead)" if others else ""
            lines.append(f"  - {probe!r}{stolen}")
    false_fires = scores.get("false_fires") or []
    if false_fires:
        lines.append(f"These {len(false_fires)} phrasing(s) fired and should not have:")
        for probe in false_fires[:10]:
            lines.append(f"  - {probe!r}")
    return "\n".join(lines)


def raise_threshold(cues: dict) -> dict | None:
    """Last-resort false-positive lever: nudge this recall's own floor up.

    Only ever upward. A per-recall floor is isolated for accept/reject, but
    Stage-2 keeps just three candidate slots — raising a floor frees a slot and
    can only help the neighbours, whereas lowering one could push a sibling out
    and would owe every sibling a re-test.
    """
    from .recall import DEFAULT_POSITIVE_THRESHOLD

    current = cues.get("positive_threshold")
    try:
        current = float(current)
    except (TypeError, ValueError):
        current = DEFAULT_POSITIVE_THRESHOLD
    nudged = round(current + _THRESHOLD_STEP, 4)
    if nudged > _MAX_THRESHOLD:
        return None
    out = dict(cues)
    out["positive_threshold"] = nudged
    return out


# ─── Orchestration ─────────────────────────────────────────────────────────


async def build_tested_recall(
    memory: str,
    *,
    memory_type: str = "semantic",
    topic: str | None = None,
) -> tuple[dict | None, dict | None]:
    """(cues, scores) — generate, test, repair up to the bound, then hand back.

    Never raises and never writes. A caller that gets `(None, None)` should
    keep the memory and leave it triggerless; the periodic consolidator picks
    those up from the candidate record.
    """
    catalog = None
    try:
        from .recall import fetch_all_recalls

        catalog = await fetch_all_recalls()
    except Exception as e:
        log.warning("Could not load recall catalog for cue generation: %s", e)

    cues = await generate_cues(
        memory, memory_type=memory_type, topic=topic, existing_recalls=catalog,
    )
    if cues is None:
        return None, None

    scores = await test_cues(cues, catalog=catalog)
    best = (cues, scores)

    for attempt in range(_MAX_REPAIR_ROUNDS):
        if meets_bar(scores):
            break
        log.info(
            "[CueTest] repair round %d/%d — recall=%.2f fp=%.2f",
            attempt + 1, _MAX_REPAIR_ROUNDS,
            scores.get("recall_rate") if scores.get("recall_rate") is not None else -1,
            scores.get("fp_rate") if scores.get("fp_rate") is not None else -1,
        )
        revised = await generate_cues(
            memory,
            memory_type=memory_type,
            topic=topic,
            existing_recalls=catalog,
            failures=describe_failures(scores),
        )
        if revised is None:
            break
        # Carry any threshold already earned, then reach for that lever only
        # when the remaining problem is false positives — a missed positive is
        # never fixed by raising the floor.
        revised["positive_threshold"] = cues.get("positive_threshold")
        if not scores.get("missed") and scores.get("false_fires"):
            nudged = raise_threshold(revised)
            if nudged is not None:
                revised = nudged
        revised_scores = await test_cues(revised, catalog=catalog)
        cues, scores = revised, revised_scores
        if _is_better(revised_scores, best[1]):
            best = (revised, revised_scores)

    final_cues, final_scores = best if not meets_bar(scores) else (cues, scores)
    return final_cues, final_scores


def _is_better(candidate: dict | None, incumbent: dict | None) -> bool:
    """Rank two measurements so a failed repair round cannot lose ground."""
    if candidate is None:
        return False
    if incumbent is None:
        return True
    return _score_key(candidate) > _score_key(incumbent)


def _score_key(scores: dict) -> tuple[float, float]:
    recall_rate = scores.get("recall_rate")
    fp_rate = scores.get("fp_rate")
    return (
        recall_rate if recall_rate is not None else 0.0,
        -(fp_rate if fp_rate is not None else 1.0),
    )


async def create_recall_for_memory(
    memory: str,
    *,
    memory_type: str = "semantic",
    topic: str | None = None,
) -> dict:
    """Full path: generate cues, prove them, write the recall.

    Returns a status dict for the caller's provenance record. Writing goes
    through `learn_recall`, the single recall writer, so cue caps, usage
    stamping and router invalidation all behave exactly as they do for a
    hand-authored recall. Test scores are attached afterwards as telemetry —
    they describe the trigger, they are not part of its definition.
    """
    cues, scores = await build_tested_recall(
        memory, memory_type=memory_type, topic=topic,
    )
    if cues is None:
        return {"status": "skipped", "detail": "cue generation unavailable"}

    from .tools import _learn_recall

    result = await _learn_recall(
        instruction=cues["instruction"],
        activation_condition=cues["activation_condition"],
        exclusions=cues.get("exclusions") or None,
        positive_examples=cues["positive_examples"],
        negative_examples=cues.get("negative_examples") or None,
        lexical_cues=cues["lexical_cues"],
        positive_threshold=cues.get("positive_threshold"),
    )
    if result.startswith("[error]"):
        return {"status": "error", "detail": result}

    recall_id = _extract_recall_id(result)
    if recall_id and scores:
        await _attach_test_scores(recall_id, scores)
    return {
        "status": "created",
        "recall_id": recall_id,
        "detail": result,
        "scores": _public_scores(scores),
    }


def _extract_recall_id(message: str) -> str | None:
    match = re.search(r"ID '([^']+)'", message or "")
    return match.group(1) if match else None


def _public_scores(scores: dict | None) -> dict | None:
    if not scores:
        return None
    return {
        "recall_rate": scores.get("recall_rate"),
        "fp_rate": scores.get("fp_rate"),
        "probes_positive": scores.get("probes_positive"),
        "probes_negative": scores.get("probes_negative"),
    }


async def _attach_test_scores(recall_id: str, scores: dict) -> None:
    """Store the measurement on the recall so a later pass can regression-test.

    Cue eviction changes the array under a recall that once tested clean, so
    the periodic consolidator needs to know both the old score and when it was
    taken to decide what is worth re-running.
    """
    from datetime import datetime, timezone

    try:
        from bson import ObjectId

        from .db_ops import get_db

        db = get_db()
        if db is None:
            return
        payload = dict(_public_scores(scores) or {})
        payload["tested_at"] = datetime.now(timezone.utc)
        await db["recalls"].update_one(
            {"_id": ObjectId(recall_id)}, {"$set": {"cue_test": payload}},
        )
    except Exception as e:
        log.warning("Could not attach cue test scores to %s: %s", recall_id, e)

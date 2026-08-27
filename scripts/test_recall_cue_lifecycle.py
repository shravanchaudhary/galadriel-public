#!/usr/bin/env python3
"""Cue lifecycle: LRU eviction, saturation skip, fail-closed arming.

Covers the guards that keep cue arrays from growing into looser matching:
  - evict_lru_cues drops cues that never won a Stage-2 match, not the oldest-added
  - cue_is_saturated rejects near-duplicate appends that max() would ignore
  - recall_system_armed disarms the whole pipeline (fail-closed) when the
    Stage-2 judge has no provider credential
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["RECALL_JUDGE_VERIFY"] = "1"

from harness.recall import (  # noqa: E402
    cue_is_saturated,
    cue_key,
    evict_lru_cues,
    recall_system_armed,
    scan_text_for_recalls,
    winning_cue,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_cue_key_stable() -> None:
    _assert(cue_key("Remember I like tea") == cue_key("  remember i like TEA  "),
            "cue_key should ignore case and surrounding whitespace")
    _assert(cue_key("a") != cue_key("b"), "distinct cues need distinct keys")
    key = cue_key("anything")
    _assert("." not in key and "$" not in key, "cue_key must be Mongo-safe")
    print("ok cue_key_stable")


def test_evict_prefers_never_used() -> None:
    now = datetime.now(timezone.utc)
    examples = ["authored-used", "authored-never", "appended-never"]
    usage = {cue_key("authored-used"): now - timedelta(days=30)}
    kept = evict_lru_cues(examples, usage, 2)
    _assert(kept == ["authored-used", "appended-never"], f"unexpected kept={kept}")
    print("ok evict_prefers_never_used")


def test_evict_is_lru_not_fifo() -> None:
    """Mirrors tune_recall: stamp the inserted cue, then evict."""
    now = datetime.now(timezone.utc)
    examples = ["oldest-but-hot", "newer-but-cold"]
    usage = {
        cue_key("oldest-but-hot"): now,
        cue_key("newer-but-cold"): now - timedelta(days=90),
        cue_key("fresh"): now,
    }
    kept = evict_lru_cues(examples + ["fresh"], usage, 2)
    _assert("oldest-but-hot" in kept, "recently used cue must survive FIFO position")
    _assert("newer-but-cold" not in kept, "stale cue should be evicted first")
    _assert("fresh" in kept, "freshly stamped cue must not be the victim")
    print("ok evict_is_lru_not_fifo")


def test_unstamped_insert_is_its_own_victim() -> None:
    """Documents why tune_recall must stamp before evicting."""
    now = datetime.now(timezone.utc)
    usage = {cue_key("hot"): now, cue_key("warm"): now}
    kept = evict_lru_cues(["hot", "warm", "unstamped"], usage, 2)
    _assert("unstamped" not in kept, "an unstamped insert ranks oldest by design")
    print("ok unstamped_insert_is_its_own_victim")


def test_evict_noop_under_cap() -> None:
    examples = ["a", "b"]
    _assert(evict_lru_cues(examples, {}, 5) == examples, "under cap must be untouched")
    _assert(evict_lru_cues([], {}, 0) == [], "empty list must be safe")
    print("ok evict_noop_under_cap")


def test_saturation_skips_near_duplicates() -> None:
    existing = ["please remember that I like tea", "steps to deploy to staging"]

    saturated, score = cue_is_saturated("please remember that I like tea", existing)
    _assert(saturated, f"verbatim repeat should saturate (cosine={score})")

    saturated, score = cue_is_saturated("what is the capital of France?", existing)
    _assert(not saturated, f"unrelated cue must not saturate (cosine={score})")

    saturated, _ = cue_is_saturated("anything", [])
    _assert(not saturated, "empty pool cannot saturate")
    print("ok saturation_skips_near_duplicates")


def _arming_env(**overrides):
    """Env snapshot/restore for the credential vars arming actually reads."""
    keys = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_ACCESS_KEY_ID", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "REPLIKA_TENANT_ID", "RECALL_JUDGE_MODEL")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    for k, v in overrides.items():
        if v is not None:
            os.environ[k] = v
    sys.modules["harness.recall"]._DISARM_LOGGED = False
    return saved


def _restore_env(saved) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    sys.modules["harness.recall"]._DISARM_LOGGED = False


def test_armed_follows_the_selected_judges_provider() -> None:
    """The whole point of provider-generic arming: a Bedrock judge with a
    Bedrock credential must arm with NO Gemini key present. The old check read
    GEMINI_API_KEY regardless of judge model and disarmed everything."""
    saved = _arming_env(
        RECALL_JUDGE_MODEL="gpt-oss-20b",
        AWS_BEARER_TOKEN_BEDROCK="bedrock-key",
    )
    try:
        _assert(recall_system_armed(),
                "a Bedrock judge with a Bedrock key must arm without a Gemini key")
    finally:
        _restore_env(saved)


def test_disarmed_when_the_selected_judges_provider_has_no_key() -> None:
    """Mirror image: a Gemini judge is not armed by someone else's credential."""
    saved = _arming_env(
        RECALL_JUDGE_MODEL="gemini-2.5-flash",
        AWS_BEARER_TOKEN_BEDROCK="bedrock-key",
    )
    try:
        _assert(not recall_system_armed(),
                "a Bedrock key must not arm a Gemini judge")
    finally:
        _restore_env(saved)
    print("ok arming_follows_the_judge_provider")


def test_disarmed_without_judge_key() -> None:
    """No credential for the judge model's provider → whole system off,
    Stage-1 included."""
    saved = _arming_env(RECALL_JUDGE_MODEL="gpt-oss-20b")
    try:
        _assert(not recall_system_armed(), "system must disarm without a judge key")
        matches = scan_text_for_recalls(
            "please remember that I like tea",
            [{
                "recall_id": "t_disarm",
                "instruction": "User is teaching a durable preference.",
                "positive_examples": ["please remember that I like tea"],
                "negative_examples": [],
            }],
        )
        _assert(matches == [], "Stage-1 must not propose while disarmed")
    finally:
        _restore_env(saved)
    print("ok disarmed_without_judge_key")


def test_judge_sees_similar_negatives() -> None:
    """filter_matches_with_judge must pass the chunk-similar tune_recall
    negatives to the judge as judge_negatives, capped and best-first."""
    import asyncio

    import harness.recall_judge as rj
    from harness.recall import filter_matches_with_judge

    seen: dict = {}
    real_judge = rj.judge_applicability

    async def spy_judge(provider, *, chunk, candidates, model=None, usage_callback=None, **kw):
        seen["candidates"] = candidates
        return {"applicable": []}

    rj.judge_applicability = spy_judge
    try:
        verified, rejected = asyncio.run(filter_matches_with_judge(
            [{
                "recall_id": "t_neg",
                "instruction": "check the worker control file",
                "activation_condition": "The user asks about background worker state.",
                "matched_chunk": "can you pause the worker for now?",
                "positive_score": 0.9,
                "negative_examples": [
                    "pause the worker please",
                    "what is the capital of France?",
                    "hold the background worker",
                    "I like tea",
                    "stop the worker job",
                ],
            }],
            provider=object(),
        ))
    finally:
        rj.judge_applicability = real_judge

    negs = seen["candidates"][0].get("judge_negatives")
    _assert(negs is not None and len(negs) == 3, f"expected 3 selected negatives, got {negs}")
    _assert("what is the capital of France?" not in negs,
            f"dissimilar negative must not be selected: {negs}")
    _assert("I like tea" not in negs, f"dissimilar negative must not be selected: {negs}")
    _assert(not verified and rejected, "spy judge returned none-applicable")
    print("ok judge_sees_similar_negatives")


def test_winning_cue_names_the_lexical_cue_that_hit() -> None:
    """A lexical match already knows what won; do not re-derive it."""
    cue = winning_cue({
        "recall_id": "t1", "match_source": "lexical",
        "lexical_cue": "save this as a rule",
        "matched_chunk": "please save this as a rule for me",
        "positive_examples": ["something else entirely"],
    })
    _assert(cue == "save this as a rule", f"expected the lexical cue, got {cue!r}")


def test_winning_cue_resolves_the_argmax_positive_for_a_semantic_hit() -> None:
    """Stage-1 accepts on max cosine over positive_examples, so the argmax IS
    the cue that fired. Nothing else records it, and without this the fire path
    stamps no usage at all — which is how LRU order silently became write order.
    """
    cue = winning_cue({
        "recall_id": "t2", "match_source": "semantic",
        "matched_chunk": "can you deploy the service to staging now",
        "positive_examples": [
            "what is the weather like tomorrow",
            "push the build out to the staging environment",
            "remind me to call my dentist",
        ],
    })
    _assert(
        cue == "push the build out to the staging environment",
        f"argmax positive should win, got {cue!r}",
    )


def test_winning_cue_returns_none_when_there_is_nothing_to_resolve() -> None:
    """No chunk or no stored positives means no honest answer; a wrong stamp
    would credit a cue that did not fire."""
    _assert(winning_cue({"recall_id": "t3", "matched_chunk": ""}) is None,
            "empty chunk must not resolve a cue")
    _assert(winning_cue({"recall_id": "t3", "matched_chunk": "hello",
                         "positive_examples": []}) is None,
            "no positives must not resolve a cue")
    _assert(winning_cue({}) is None, "an empty match must not resolve a cue")
    print("ok winning_cue_resolves_the_real_winner")


def test_over_cap_replace_drops_least_recently_used_not_the_head() -> None:
    """A full-array replace over the cap used to keep the LAST N submitted,
    which is arbitrary — it can delete exactly the cues that keep winning."""
    from harness.tools import _MAX_EXAMPLES_PER_RECALL, _normalize_cue_list

    now = datetime.now(timezone.utc)
    hot = "the cue that keeps winning"
    submitted = [hot] + [f"filler cue number {i}" for i in range(_MAX_EXAMPLES_PER_RECALL)]
    usage = {cue_key(hot): now}

    kept = _normalize_cue_list(submitted, usage=usage)
    _assert(len(kept) == _MAX_EXAMPLES_PER_RECALL, f"cap not applied: {len(kept)}")
    _assert(hot in kept, "the recently-used cue must survive an over-cap replace")

    blind = _normalize_cue_list(submitted)
    _assert(hot not in blind, "fixture must be one a position-based trim gets wrong")


def test_dropped_cue_note_names_only_cues_that_had_earned_use() -> None:
    """Arrays are full replacements, so a forgotten cue is deleted silently.
    Report the ones with usage history; pruning a never-used cue is the point
    of the tool, not a mistake worth warning about."""
    from harness.tools import _dropped_cue_note

    now = datetime.now(timezone.utc)
    doc = {"positive_examples": ["earned its place", "never once matched"]}
    updates = {"positive_examples": ["something new"]}
    usage = {cue_key("earned its place"): now}

    note = _dropped_cue_note(doc, updates, usage)
    _assert("earned its place" in note, f"used cue not reported: {note!r}")
    _assert("never once matched" not in note, f"unused cue should be quiet: {note!r}")
    _assert(_dropped_cue_note(doc, {}, usage) == "",
            "no patched array means nothing to warn about")
    print("ok cue_replace_guardrails")


def test_arming_uses_the_tower_judge_once_it_has_been_resolved() -> None:
    """The branch the agent actually takes: no env override, judge configured
    in Tower. Arming may not do I/O, so it reads what resolve_judge_model last
    saw — and if that is never warmed it silently falls back to the DEFAULT
    model's provider and disarms a correctly-configured tenant. Worse, a
    disarmed gate stops the judge running, and the judge is the only thing that
    would have warmed the cache, so the disarm locks itself in.
    """
    import harness.recall_judge as rj

    saved = _arming_env(GEMINI_API_KEY="g")   # judge provider credential only
    saved_cache = (rj._MODEL_CACHE, rj._LAST_RESOLVED_MODEL)
    try:
        rj._MODEL_CACHE = None
        rj._LAST_RESOLVED_MODEL = None
        _assert(not recall_system_armed(),
                "cold cache with no Bedrock credential should not arm")

        # What the startup warm does: resolve once, off the loop.
        rj._MODEL_CACHE = (time.monotonic(), "gemini-2.5-flash")
        rj._LAST_RESOLVED_MODEL = "gemini-2.5-flash"
        sys.modules["harness.recall"]._DISARM_LOGGED = False
        _assert(recall_system_armed(),
                "a warmed Tower judge must arm on its own provider's key")

        # TTL expiry must not silently swing arming back to the default model.
        rj._MODEL_CACHE = (time.monotonic() - rj._MODEL_TTL_SECONDS - 1, "gemini-2.5-flash")
        _assert(recall_system_armed(),
                "an expired TTL means not-rechecked, not no-longer-configured")
    finally:
        rj._MODEL_CACHE, rj._LAST_RESOLVED_MODEL = saved_cache
        _restore_env(saved)


def test_an_unlisted_judge_model_does_not_arm_the_system_open() -> None:
    """Unlisted names resolve to the Ollama provider, which needs no
    credential — so an unvalidated value would fail OPEN in a system whose
    whole contract is fail-closed."""
    saved = _arming_env(RECALL_JUDGE_MODEL="totally-made-up-model")
    try:
        _assert(not recall_system_armed(),
                "a bogus judge model must not arm the system")
    finally:
        _restore_env(saved)
    print("ok arming_cache_and_validation")


def test_cue_array_sent_as_a_string_is_named_as_a_type_problem() -> None:
    """"Cannot be empty" invited the model to resend the same wrong shape.

    Cue arrays are full replacements, so the stakes are higher than `learn`'s:
    a caller told its array was "empty" naturally responds by sending a bigger
    one, in the same JSON-string form that failed.
    """
    from harness.tools import _cue_field_or_error

    values, err = _cue_field_or_error('["a", "b"]', "positive_examples")
    assert err is None, err
    assert values == ["a", "b"], values

    _, err = _cue_field_or_error("not json at all", "positive_examples")
    assert err and "not valid JSON" in err, err
    assert "cannot be empty" not in err, err

    _, err = _cue_field_or_error([1, 2], "lexical_cues")
    assert err and "only strings" in err, err

    _, err = _cue_field_or_error(["", "   "], "positive_examples")
    assert err and "none survived normalization" in err, err


def test_explorium_array_argument_is_not_iterated_per_character() -> None:
    """A stringified array used to become a per-character validation error."""
    import asyncio

    from harness.explorium_tools import execute_explorium_tool

    out = asyncio.run(execute_explorium_tool(
        "explorium_business_events",
        {"business_id": "x", "event_types": "not json"},
    ))
    assert "event_types" in out, out
    assert "not valid JSON" in out, out
    # The old path reported every character as an unknown event type.
    assert "'n', 'o', 't'" not in out, out

    # An empty optional filter was skipped by a falsiness check before this
    # change and must still be skipped — coercion must not reject valid calls.
    out = asyncio.run(execute_explorium_tool(
        "explorium_business_events",
        {"business_id": "x", "event_types": "", "days_back": 1},
    ))
    assert "empty string" not in out, out


def main() -> int:
    test_cue_key_stable()
    test_evict_prefers_never_used()
    test_evict_is_lru_not_fifo()
    test_unstamped_insert_is_its_own_victim()
    test_evict_noop_under_cap()
    test_saturation_skips_near_duplicates()
    test_armed_follows_the_selected_judges_provider()
    test_disarmed_when_the_selected_judges_provider_has_no_key()
    test_disarmed_without_judge_key()
    test_arming_uses_the_tower_judge_once_it_has_been_resolved()
    test_an_unlisted_judge_model_does_not_arm_the_system_open()
    test_judge_sees_similar_negatives()
    test_winning_cue_names_the_lexical_cue_that_hit()
    test_winning_cue_resolves_the_argmax_positive_for_a_semantic_hit()
    test_winning_cue_returns_none_when_there_is_nothing_to_resolve()
    test_over_cap_replace_drops_least_recently_used_not_the_head()
    test_dropped_cue_note_names_only_cues_that_had_earned_use()
    test_cue_array_sent_as_a_string_is_named_as_a_type_problem()
    test_explorium_array_argument_is_not_iterated_per_character()
    print("ok recall_cue_lifecycle")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"ASSERT: {e}", file=sys.stderr)
        raise SystemExit(1)

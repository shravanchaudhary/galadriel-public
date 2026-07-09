#!/usr/bin/env python3
"""Local tests for in-process Headroom compression (`harness/headroom_compress`).

Headroom compresses message lists *before* they are sent to an LLM: JSON tool
outputs, logs, etc. All of this runs locally — no API key or network needed.

Note: this is separate from harness/compaction.py, which does LLM-based
snapshot summarization. Headroom is deterministic, local compression.

Usage:
    venv/bin/python scripts/test_headroom_compaction.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from headroom import CompressConfig, compress  # noqa: E402

from harness import headroom_compress  # noqa: E402

MODEL = "claude-sonnet-4-5-20250929"


def _fake_json_tool_output(n: int = 200) -> str:
    rows = [
        {
            "id": i,
            "name": f"svc-{i}",
            "status": "healthy" if i % 7 else "degraded",
            "region": "us-east-1",
            "cpu": round(0.1 * i % 1, 2),
            "mem_mb": 512 + i,
            "tags": ["prod", "core"],
            "last_deploy": "2026-07-01T12:00:00Z",
        }
        for i in range(n)
    ]
    return json.dumps(rows)


def _fake_log_output(n: int = 300) -> str:
    lines = []
    for i in range(n):
        level = "ERROR" if i == 250 else "INFO"
        lines.append(
            f"2026-07-08T12:{i % 60:02d}:00Z {level} worker-3 processed job id={i} "
            f"queue=default duration_ms={40 + i % 20} status=ok"
        )
    return "\n".join(lines)


def test_json_tool_output() -> bool:
    """SmartCrusher should heavily compress a big JSON tool result."""
    print("=== test: JSON tool output compression ===")
    messages = [
        {"role": "system", "content": "You are an SRE assistant."},
        {"role": "user", "content": "Which services are degraded?"},
        {
            "role": "assistant",
            "content": "Checking.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "list_services", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": _fake_json_tool_output()},
        {"role": "user", "content": "Summarize please."},
    ]
    res = compress(messages, model=MODEL)
    print(
        f"tokens: {res.tokens_before} -> {res.tokens_after} "
        f"(saved {res.tokens_saved}, ratio {res.compression_ratio:.0%})"
    )
    print(f"transforms: {res.transforms_applied}")
    if res.tokens_saved <= 0:
        print("FAIL: expected token savings on large JSON tool output")
        return False
    if len(res.messages) != len(messages):
        print("FAIL: message count changed (history should never be dropped)")
        return False
    print("PASS")
    return True


def test_log_output() -> bool:
    """Repetitive log lines should compress while keeping the anomaly visible."""
    print("\n=== test: log output compression ===")
    messages = [
        {"role": "user", "content": "Find any errors in these worker logs."},
        {
            "role": "assistant",
            "content": "Reading logs.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_logs", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": _fake_log_output()},
    ]
    res = compress(messages, model=MODEL)
    print(
        f"tokens: {res.tokens_before} -> {res.tokens_after} "
        f"(saved {res.tokens_saved}, ratio {res.compression_ratio:.0%})"
    )
    compressed_tool = next(m for m in res.messages if m.get("role") == "tool")
    body = str(compressed_tool.get("content", ""))
    if res.tokens_saved <= 0:
        print("FAIL: expected token savings on repetitive logs")
        return False
    if "ERROR" not in body:
        print("FAIL: the single ERROR line was compressed away")
        return False
    print("PASS: savings achieved and ERROR line survived")
    return True


def test_protection_defaults() -> bool:
    """By default user messages are protected — never rewritten."""
    print("\n=== test: user messages protected by default ===")
    user_text = "Remember these exact ids: ORDER-8842, ORDER-9107, ORDER-1230."
    messages = [
        {"role": "user", "content": user_text},
        {"role": "tool", "tool_call_id": "c1", "content": _fake_json_tool_output(100)},
    ]
    res = compress(messages, model=MODEL)
    kept = [m for m in res.messages if m.get("role") == "user"]
    if not kept or kept[0]["content"] != user_text:
        print("FAIL: user message was modified")
        return False
    print("PASS: user message byte-identical after compression")
    return True


def test_config_knobs() -> bool:
    """CompressConfig: opt in to compressing user messages too."""
    print("\n=== test: CompressConfig (compress_user_messages, protect_recent) ===")
    big_export = json.dumps(
        [{"i": i, "val": i * 3, "note": "steady"} for i in range(300)]
    )
    messages = [
        {"role": "user", "content": big_export},
        {"role": "user", "content": "Summarize the export."},
    ]
    cfg = CompressConfig(
        compress_user_messages=True,
        protect_recent=1,  # keep the actual question intact
    )
    res = compress(messages, model=MODEL, config=cfg)
    print(
        f"tokens: {res.tokens_before} -> {res.tokens_after} "
        f"(saved {res.tokens_saved}, ratio {res.compression_ratio:.0%})"
    )
    print(f"transforms: {res.transforms_applied}")
    if res.messages[-1]["content"] != "Summarize the export.":
        print("FAIL: protect_recent=1 should keep the last message intact")
        return False
    if res.tokens_saved <= 0:
        print("FAIL: expected savings with compress_user_messages=True")
        return False
    print("PASS")
    return True


def test_freeze_lifecycle() -> bool:
    """Turn-scoped freeze via accumulated api_messages (matches agent loop).

    Call 1 compresses history. Call 2 reuses the compressed prefix and only
    compresses newly appended tool_results — prefix must stay byte-identical
    so provider cache hits.
    """
    print("\n=== test: freeze lifecycle (api_messages accumulate) ===")

    async def _run() -> bool:
        history = [
            {"role": "user", "content": "List degraded services."},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "list_services",
                        "input": {},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": _fake_json_tool_output(80),
                    }
                ],
            },
            {"role": "user", "content": "Now dig into the degraded ones."},
        ]
        api_messages, m0 = await headroom_compress.compress_for_api(
            history, model=MODEL, frozen_message_count=0,
        )
        print(
            f"call1: {m0.tokens_before} -> {m0.tokens_after} "
            f"(saved {m0.tokens_saved})"
        )
        if m0.tokens_saved <= 0:
            print("FAIL: turn-start should compress old tool JSON")
            return False

        frozen = len(api_messages)
        new_tail = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "list_services",
                        "input": {"status": "degraded"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": _fake_json_tool_output(60),
                    }
                ],
            },
        ]
        to_compress = list(api_messages) + new_tail
        out1, m1 = await headroom_compress.compress_for_api(
            to_compress, model=MODEL, frozen_message_count=frozen,
        )
        print(
            f"call2: {m1.tokens_before} -> {m1.tokens_after} "
            f"(saved {m1.tokens_saved}) frozen={frozen}"
        )
        if out1[:frozen] != api_messages:
            print("FAIL: compressed prefix must stay byte-identical")
            return False
        if m1.tokens_saved <= 0:
            print("FAIL: new tool_result after freeze should still compress")
            return False
        print("PASS: compressed prefix reused; only new tool_result shrunk")
        return True

    return asyncio.run(_run())


def _fake_image_block(label: str) -> dict:
    import base64

    # Large enough that pruning clearly shrinks JSON size; need not be a real PNG.
    payload = base64.b64encode((label * 12000).encode()).decode()
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": payload,
        },
    }


def _count_images(messages: list) -> int:
    n = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image":
                n += 1
            elif block.get("type") == "tool_result" and isinstance(
                block.get("content"), list
            ):
                for ib in block["content"]:
                    if isinstance(ib, dict) and ib.get("type") == "image":
                        n += 1
    return n


def _approx_json_size(messages: list) -> int:
    return len(json.dumps(messages))


def test_prune_old_screenshots() -> bool:
    """Keep last 3 images; older become placeholders; input list unchanged."""
    print("\n=== test: prune_old_screenshots keep_last=3 ===")
    history = []
    for i in range(5):
        path = f"state/screenshots/shot-{i}.png"
        history.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"t{i}",
                        "content": [
                            {
                                "type": "text",
                                "text": f"Saved screenshot to {path}",
                            },
                            _fake_image_block(f"img{i}"),
                        ],
                    }
                ],
            }
        )
    original_size = _approx_json_size(history)
    original_images = _count_images(history)
    pruned, stats = headroom_compress.prune_old_screenshots(history, keep_last=3)
    after_images = _count_images(pruned)
    after_size = _approx_json_size(pruned)
    print(
        f"images: {original_images} -> {after_images} "
        f"(kept={stats.images_kept} pruned={stats.images_pruned}) "
        f"json_bytes: {original_size} -> {after_size}"
    )
    if _count_images(history) != 5:
        print("FAIL: prune mutated the original history")
        return False
    if after_images != 3 or stats.images_kept != 3 or stats.images_pruned != 2:
        print("FAIL: expected keep 3 / prune 2")
        return False
    if after_size >= original_size:
        print("FAIL: pruned API copy should be smaller")
        return False
    # Oldest two should be placeholders mentioning the path.
    for i in range(2):
        inner = pruned[i]["content"][0]["content"]
        texts = [
            b.get("text", "")
            for b in inner
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        joined = "\n".join(texts)
        if "screenshot omitted" not in joined:
            print(f"FAIL: image {i} not replaced with placeholder: {joined!r}")
            return False
        if f"shot-{i}.png" not in joined:
            print(f"FAIL: placeholder missing path hint for shot-{i}")
            return False
    print("PASS")
    return True


def test_compress_for_api_prunes_screenshots() -> bool:
    """compress_for_api runs prune before Headroom; metrics expose image counts."""
    print("\n=== test: compress_for_api prunes screenshots ===")

    async def _run() -> bool:
        history = []
        for i in range(5):
            history.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"t{i}",
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"Saved screenshot to state/screenshots/s{i}.png",
                                },
                                _fake_image_block(f"big{i}"),
                            ],
                        }
                    ],
                }
            )
        before_images = _count_images(history)
        out, metrics = await headroom_compress.compress_for_api(
            history, model=MODEL, frozen_message_count=0,
        )
        after_images = _count_images(out)
        print(
            f"images: {before_images} -> {after_images} "
            f"metrics kept={metrics.images_kept} pruned={metrics.images_pruned}"
        )
        if _count_images(history) != 5:
            print("FAIL: stored history mutated")
            return False
        if after_images != 3:
            print(f"FAIL: expected 3 images in API copy, got {after_images}")
            return False
        if metrics.images_pruned != 2 or metrics.images_kept != 3:
            print("FAIL: metrics mismatch")
            return False
        print("PASS")
        return True

    return asyncio.run(_run())


def test_downscale_screenshot_bytes() -> bool:
    """Vision attachment is resized; longest edge <= 1280."""
    print("\n=== test: _downscale_screenshot_bytes ===")
    from io import BytesIO

    from PIL import Image

    from harness.tools import _MAX_SCREENSHOT_EDGE, _downscale_screenshot_bytes

    img = Image.new("RGB", (2560, 1440), color=(40, 80, 120))
    buf = BytesIO()
    img.save(buf, format="PNG")
    raw = buf.getvalue()
    out, media_type = _downscale_screenshot_bytes(raw)
    scaled = Image.open(BytesIO(out))
    scaled.load()
    print(f"size: {img.size} -> {scaled.size} media={media_type} bytes={len(raw)}->{len(out)}")
    if max(scaled.size) > _MAX_SCREENSHOT_EDGE:
        print("FAIL: longest edge still above cap")
        return False
    if len(out) >= len(raw):
        print("FAIL: expected fewer bytes after downscale")
        return False
    # Already-small images pass through unchanged.
    small = Image.new("RGB", (800, 600), color=(1, 2, 3))
    sbuf = BytesIO()
    small.save(sbuf, format="PNG")
    sraw = sbuf.getvalue()
    sout, _ = _downscale_screenshot_bytes(sraw)
    if sout != sraw:
        print("FAIL: small image should be returned unchanged")
        return False
    print("PASS")
    return True


def main() -> int:
    results = [
        test_json_tool_output(),
        test_log_output(),
        test_protection_defaults(),
        test_config_knobs(),
        test_freeze_lifecycle(),
        test_prune_old_screenshots(),
        test_compress_for_api_prunes_screenshots(),
        test_downscale_screenshot_bytes(),
    ]
    passed = sum(results)
    print(f"\n{passed}/{len(results)} tests passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

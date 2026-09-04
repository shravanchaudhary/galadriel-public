"""Token-spaced survey of a text: head, N evenly spaced probes, tail.

A head+tail view says nothing about the middle of a large file, which is where
most of it is. A survey shows the two ends plus N windows spaced evenly by
TOKEN offset across the range, each labelled with its token position (and the
line it starts on, when the text has lines at all — minified HTML or a single
paragraph does not). The agent zooms by surveying the range between two
probes, reads a window with read_file(start, end), or slices by line with
grep/sed. Every level of zoom costs the same fixed number of tokens, so a
million-token file is oriented in three calls.

Offsets are tokens via compaction.CHARS_PER_TOKEN, seeked as bytes — the same
approximation study_file and read_file already use. Bytes ≈ chars for the
text this reads; a window cut inside a multibyte character decodes as U+FFFD
at its edge, which is cosmetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .compaction import CHARS_PER_TOKEN

SURVEY_EDGE_TOKENS = 500    # head and tail, each
SURVEY_PROBES = 10          # default probe count
SURVEY_PROBE_TOKENS = 150   # each probe window
SURVEY_MAX_PROBES = 30      # 2×500 + 30×150 = 5,500 tokens, under the 7.5k inline bound

_CHUNK = 1 << 20


def survey_file(path, start: int = 0, end: int | None = None, probes: int | None = None) -> str:
    """Survey `path` between token offsets `start` and `end` (default EOF).
    Blocking file IO — call from a thread when on the event loop."""
    p = Path(path)
    size = p.stat().st_size
    with p.open("rb") as f:
        def read(offset: int, length: int) -> bytes:
            f.seek(offset)
            return f.read(length)
        return _survey(read, size, str(p), start, end, probes)


def survey_text(text: str, label: str, start: int = 0, end: int | None = None,
                probes: int | None = None) -> str:
    """Survey an in-memory string (the spill-failed path: nothing on disk)."""
    data = text.encode("utf-8", errors="replace")
    return _survey(lambda o, n: data[o:o + n], len(data), label, start, end, probes)


def _survey(read: Callable[[int, int], bytes], size: int, label: str,
            start: int, end: int | None, probes: int | None) -> str:
    probes = max(1, min(int(probes or SURVEY_PROBES), SURVEY_MAX_PROBES))
    start_b = max(0, min(int(start or 0) * CHARS_PER_TOKEN, size))
    end_b = size if end is None else max(0, min(int(end) * CHARS_PER_TOKEN, size))
    if end_b <= start_b:
        return (
            f"[survey] {label}: empty range — start={start_b // CHARS_PER_TOKEN:,} "
            f"end={end_b // CHARS_PER_TOKEN:,} on a ≈{size // CHARS_PER_TOKEN:,}-token text."
        )
    total_tokens = size // CHARS_PER_TOKEN
    ranged = start_b > 0 or end_b < size
    scope = (
        f"range {start_b // CHARS_PER_TOKEN:,}–{end_b // CHARS_PER_TOKEN:,} of "
        if ranged else ""
    )

    edge_b = SURVEY_EDGE_TOKENS * CHARS_PER_TOKEN
    probe_b = SURVEY_PROBE_TOKENS * CHARS_PER_TOKEN
    span = end_b - start_b
    if span <= 2 * edge_b + probes * probe_b:
        # The range fits the survey's own budget: show it whole.
        body = _decode(read(start_b, span))
        lines = _line_numbers(read, size, [start_b])
        return (
            f"[survey] {label}: {scope}≈{total_tokens:,} tokens — the whole "
            f"{'range' if ranged else 'text'} fits, shown in full "
            f"(from line {lines[start_b]:,}).]\n{body}"
        )

    # Windows: head, N probes centred at even fractions of the interior, tail.
    windows: list[tuple[str, int, int]] = [("head", start_b, start_b + edge_b)]
    inner_start, inner_end = start_b + edge_b, end_b - edge_b
    inner = inner_end - inner_start
    for k in range(1, probes + 1):
        centre = inner_start + inner * k // (probes + 1)
        lo = max(inner_start, centre - probe_b // 2)
        windows.append((f"probe {k}/{probes}", lo, min(lo + probe_b, inner_end)))
    windows.append(("tail", end_b - edge_b, end_b))
    lines = _line_numbers(read, size, [w[1] for w in windows] + [size])

    out = [
        f"[survey] {label}: {scope}≈{total_tokens:,} tokens, {lines[size]:,} lines. "
        f"Head, {probes} probes spaced evenly by token offset, tail. Zoom into the "
        "gap between two probes with survey_file(path, start=<tok>, end=<tok>); "
        "read a window with read_file(path, start=<tok>, end=<tok>); slice by "
        "line with run_shell grep -n / sed -n 'N,Mp'.]"
    ]
    for name, lo, hi in windows:
        out.append(
            f"\n── {name} · tokens {lo // CHARS_PER_TOKEN:,}–{hi // CHARS_PER_TOKEN:,}"
            f" · line {lines[lo]:,} ──\n{_decode(read(lo, hi - lo))}"
        )
    return "\n".join(out)


def _decode(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


def _line_numbers(read: Callable[[int, int], bytes], size: int,
                  offsets: list[int]) -> dict[int, int]:
    """1-based line number at each byte offset (and the line count at `size`),
    from one streaming pass — never the whole file in memory."""
    wanted = sorted(set(offsets))
    result: dict[int, int] = {}
    pos = lines = 0
    i = 0
    ends_with_newline = False
    while i < len(wanted):
        chunk = read(pos, _CHUNK)
        if not chunk:
            break
        while i < len(wanted) and wanted[i] < pos + len(chunk):
            result[wanted[i]] = lines + chunk[:wanted[i] - pos].count(b"\n") + 1
            i += 1
        lines += chunk.count(b"\n")
        pos += len(chunk)
        ends_with_newline = chunk.endswith(b"\n")
    for off in wanted[i:]:  # at or beyond EOF: the line count, wc -l style
        result[off] = lines if ends_with_newline else lines + 1
    return result

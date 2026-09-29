"""parse_answer() and check_citations(): model-answer cleaning and the citation policy (SPEC §4.9, §6.1)."""

from __future__ import annotations

import json as _json
import re
from collections.abc import Collection, Sequence
from typing import Any

FENCE_RE = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\n(.*?)\n?```", re.S)
MAX_STARTS = 32  # §4.9: successive `{` positions tried before the answer counts as holding no object


def parse_answer(answer: str | dict[str, Any], *, json: bool) -> str | dict[str, Any] | None:
    """§4.9 cleaning: strip, drop a leading <think>…</think>; with json=True try the content of every fenced block
    in order, then successive `{` positions (at most 32), and return the first balanced one that parses to an
    object; None when none does (an invalid output, one retry). A dict answer passes through with json=True.
    Keys are never aliased: the object is returned as the model wrote it."""
    if isinstance(answer, dict):
        return answer if json else None
    text = answer.strip()
    if text.startswith("<think>") and (end := text.find("</think>")) != -1:
        text = text[end + len("</think>"):].strip()
    if not json:
        return text
    for m in FENCE_RE.finditer(text):
        if (obj := first_object(m.group(1).strip())) is not None:
            return obj
    return first_object(text)


def first_object(text: str, max_starts: int = MAX_STARTS) -> dict[str, Any] | None:
    """The first balanced `{...}` (braces inside JSON strings skipped) that parses to an object, trying at most
    `max_starts` `{` positions, so prose such as `{id}` before the JSON does not hide it. A balanced span that does
    not parse is skipped whole, never searched inside: a malformed wrapper is not unwrapped to an inner object (keys
    are never aliased); only an unclosed `{` moves the scan on by one character."""
    start = text.find("{")
    for _ in range(max_starts):
        if start == -1:
            return None
        end = balanced_end(text, start)
        if end is not None:
            try:
                obj = _json.loads(text[start : end + 1])
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                return obj
        start = text.find("{", start + 1 if end is None else end + 1)
    return None


def balanced_end(text: str, start: int) -> int | None:
    """Index of the `}` that closes the `{` at `start`, skipping braces inside JSON strings; None when unclosed."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            esc = not esc and ch == "\\"
            in_str = esc or ch != '"'
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}" and (depth := depth - 1) == 0:
            return i
    return None


def check_citations(given: Sequence[str], allowed: Collection[str]) -> tuple[list[str], list[str]]:
    """§6.1 citation policy: `given` stripped, empties and duplicates dropped, then split into (kept, dropped) by
    membership in `allowed`, order kept. The caller accepts when len(dropped) <= len(kept + dropped) / 2 and its
    floor holds on `kept`; otherwise it rejects with `{k} of {n} ids are not in the stretch: …`."""
    seen: dict[str, None] = {}
    for raw in given:
        if s := str(raw).strip():
            seen.setdefault(s, None)
    kept = [i for i in seen if i in allowed]
    dropped = [i for i in seen if i not in allowed]
    return kept, dropped


def mostly_foreign(kept: Sequence[str], dropped: Sequence[str]) -> bool:
    """The reject side of the citation policy: more than half of the cited ids are not in the input."""
    return len(dropped) * 2 > len(kept) + len(dropped)

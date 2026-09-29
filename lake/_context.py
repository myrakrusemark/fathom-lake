"""context() / context_blocks(): anchors, containers, strips, line renderers, budget (SPEC §5.6)."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING

from . import _db, _plan, _recall, _store
from ._recall import Filters, oneline, window
from ._types import CollapsedRun, ContextResult, Delta, Hit, Timeline, TimelineRow, TimeSpec

if TYPE_CHECKING:
    from .lake import Lake

MAX_STRIPS = 12  # §5.6 step 3: the highest-scoring strips kept for rendering, per 8000 characters of budget
WIDE_TOP = 2  # §5.6.3: the best hits, widened before packing; other anchors widen only with budget left over
ANCHOR_CAP = 600  # §5.6.2: an anchor line shows up to this much, windowed on the query (others: 130); leftover budget widens it
MAX_CONTAINERS = 60  # §5.6 step 2: containers reached upward from the hits, at most
CITE_DEPTH = 3  # §5.6 step 2: cited_by(hit.id, depth=3)
QUERY_CAP = 120  # §5.6.3 block 3
STANCES_SHOWN = 12  # §5.7: live stances rendered in the positions block
VERBS: MappingProxyType[str, str] = MappingProxyType({"affirm": "affirms", "refute": "refutes", "reply": "replies to"})


@dataclass(frozen=True)
class Part:
    """One selected strip's rendered pieces; the budget loop trims the two lists in place."""

    strip: Timeline
    header: str
    anchors: list[str]
    ambient: list[str]  # the divider first, when present


def context_blocks(
    lake: Lake,
    query: str | None,
    *,
    budget: int = 8000,
    limit: int = 30,
    source: str | Sequence[str] | None = None,
    exclude_sources: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    any_tags: Sequence[str] | None = None,
    exclude_tags: Sequence[str] | None = None,
    kind: str | Sequence[str] | None = None,
    since: TimeSpec | None = None,
    until: TimeSpec | None = None,
    crystal: bool = True,
    containers: bool = True,
    noise: bool = True,
    recency: bool = True,
    min_relevance: float = 0.0,
    labels: Mapping[str, str] | None = None,
) -> ContextResult:
    """§5.6 steps 1–4 under one clock reading; sets lake.last_warnings. context() is `.rendered`."""
    if limit < 1:
        raise ValueError(f"limit must be at least 1, not {limit}")
    bad = set([kind] if isinstance(kind, str) else kind or ()) - _recall.KINDS
    if bad:
        raise ValueError(f"unknown kind: {', '.join(sorted(bad))}")
    now = lake.now()
    lab = {**lake.labels, **(labels or {})}
    warnings: list[str] = []
    crys = _store.newest(lake, "crystal") if crystal else None
    q = " ".join(query.split()) if query else ""
    rows = Filters(source=source, exclude_sources=exclude_sources, tags=tags, any_tags=any_tags, exclude_tags=exclude_tags)
    hits: list[Hit] = []
    if q:  # step 1: the anchor recall, mood and crystal rows excluded unless kind is given
        anchors = replace(rows, kind=kind, since=since, until=until, exclude_kinds=() if kind else ("mood", "crystal"))
        hits = _recall.search(lake, query, anchors, limit=limit, now=now, noise=noise, recency=recency,
                              min_relevance=min_relevance, warnings=warnings)
    lake.last_warnings = warnings
    if not hits:
        return ContextResult(crys, hits, [], [], 0, crystal_block(crys, budget, lab) if crys else "", warnings)
    fixed = [crystal_block(crys, budget * 2 // 5, lab)] if crys else []
    fixed += [lab["remember"].format(n=len(hits)), "", lab["query"].format(q=q[:QUERY_CAP] + "…" if len(q) > QUERY_CAP else q)]
    if any(w.startswith("embed failed") for w in warnings):  # §5.6.3: say the vector pass fell back (§5.2)
        fixed.append(lab["fts_only"])
    now_str = lake.now_str()
    active = _db.attach_refutations(lake.conn, active_containers(lake, hits), now_str) if containers else []
    active = _db.attach_dep_refuted(lake.conn, active, now_str) if active else []
    shown: list[Delta] = []
    block4 = ["\n" + lab["containers"]]
    for d in active:  # block 4: lines while blocks 1–4 stay within 0.6 × budget
        line = render_line(TimelineRow(d, True), lab)
        if len("\n".join([*fixed, *block4, line])) > budget * 3 // 5:
            break
        block4.append(line)
        shown.append(d)
    if shown:
        if len(shown) < len(active):
            block4.append(lab["more_containers"].format(n=len(active) - len(shown), s=plural(len(active) - len(shown))))
        fixed += block4
    built = _plan.timeline(lake, hits, _plan.TimelineParams(20, 6, 15, 300), rows, now=now)  # step 3
    best = {h.delta.id: h.score for h in hits}
    ranked = sorted(built, key=lambda s: (-max((best.get(i, 0.0) for i in s.anchor_ids), default=0.0), s.t_start))
    keep = max(MAX_STRIPS, budget * MAX_STRIPS // 8000)  # a larger budget can be filled
    shown_ids = {r.delta.id for s in ranked[:keep] for r in s.rows if isinstance(r, TimelineRow) and r.delta.source == "lake:stance"}
    sure = {x.id: x.label for x in _recall.stances(lake, now, shown_ids)} if shown_ids else {}
    toks = _recall.fts_tokens(q)
    cap = max(ANCHOR_CAP, budget * ANCHOR_CAP // 8000)  # §5.6.3: the WIDE_TOP best hits widen before packing
    wide = set([h.delta.id for h in hits if h.delta.kind != "container"][:WIDE_TOP if cap > ANCHOR_CAP else 0])
    sel = [Part(s, *strip_parts(s, lab, sure, toks, ANCHOR_CAP, wide, cap)) for s in ranked[:keep]]
    omitted = len(built) - len(sel)
    while True:  # §5.6.3 budget: ambient lines first (lowest strip first), then whole strips, then anchor lines
        order = sorted(sel, key=lambda p: p.strip.t_start)
        text = assemble(fixed, order, omitted, lab)
        if len(text) <= budget or not sel:
            break
        last, amb = sel[-1], next((p for p in reversed(sel) if p.ambient), None)
        if amb is not None:
            amb.ambient.pop()
            if len(amb.ambient) == 1:
                amb.ambient.pop()  # the divider goes with the last ambient line
        elif len(sel) > 1 or not last.anchors:
            sel.pop()
            omitted += 1
        else:
            last.anchors.pop()
    for p in sel if cap > ANCHOR_CAP and len(text) < budget else ():
        old, p.anchors[:] = p.anchors[:], strip_parts(p.strip, lab, sure, toks, cap)[1][:len(p.anchors)]
        grown = assemble(fixed, order, omitted, lab)
        p.anchors[:], text = (old, text) if len(grown) > budget else (p.anchors, grown)
    return ContextResult(crys, hits, shown, [p.strip for p in order], omitted, text[:budget], warnings)


def crystal_block(crys: Delta, cut: int, lab: Mapping[str, str]) -> str:
    """§5.6.3 block 1: header, blank line, content, trailing newline; over `cut`, cut at the last newline + '…'."""
    block = f"{lab['crystal_header'].format(ts=crys.timestamp[:16])}\n\n{crys.content}\n"
    return block if len(block) <= cut else block[: max(block.rfind("\n", 0, cut - 1), 0)] + "\n…"


def system_prompt(lake: Lake, *, moods: int = 3, budget: int = 8000, labels: Mapping[str, str] | None = None) -> str:
    """§5.7: the crystal block plus the newest `moods` moods' carrier waves, as one system-message block.
    No query, no recall, no think/embed. `""` with neither a crystal nor a mood; exactly crystal_block(crys,
    budget) — byte-identical to context(None) — when no mood section renders."""
    lab = {**lake.labels, **(labels or {})}
    crys = _store.newest(lake, "crystal")
    rows = _store.newest_n(lake, "mood", moods)  # newest first
    # §5.7: an items crystal (rendered within RENDER_CAP) that fits is shown whole; positions, then moods, take the rest
    full = len(crystal_block(crys, 1 << 30, lab)) if crys and (crys.meta or {}).get("items") else budget
    room = budget - full - 1 if full < budget else budget
    lines = [lab["position"].format(position=x.position, label=x.label, since=x.since[:10])
             for x in _recall.stances(lake, lake.now())[:STANCES_SHOWN]]
    while lines and len("\n".join([lab["positions_header"], *lines])) > min(budget // 5, room):
        lines.pop()  # §5.7: the least sure position goes first
    pos = "\n".join([lab["positions_header"], *lines]) if lines else ""
    section = "\n\n".join(x for x in (pos, mood_section(rows, min(budget * 2 // 5, room - len(pos) - 2), lab) if rows else "") if x)
    if not section:  # the crystal alone, no trailing separator: the context(None) contract
        return crystal_block(crys, budget, lab) if crys else ""
    if crys is None:
        return section  # positions and moods alone, already within budget * 3 // 5 + 2
    return f"{crystal_block(crys, budget - len(section) - 1, lab)}\n{section}"


def mood_section(rows: Sequence[Delta], cap: int, lab: Mapping[str, str]) -> str:
    """§5.7 block 2: the mood_header, then one entry per mood newest first, kept within `cap`.
    Entries drop from the oldest; the single newest is truncated with '…' if it alone overflows; ''
    when even the header plus one truncated entry will not fit."""
    header = lab["mood_header"]
    if cap < len(header) + 2 or not rows:  # no room for the header plus at least a truncated entry
        return ""
    kept = [mood_entry(d, lab) for d in rows]
    while len(kept) > 1 and len("\n".join([header, *kept])) > cap:
        kept.pop()  # drop the oldest until it fits
    text = "\n".join([header, *kept])
    if len(text) <= cap:
        return text
    room = cap - len(header) - 1  # the single newest still overflows: hard-cut it, ending '…'
    return f"{header}\n{kept[0][: room - 1]}…"


def mood_entry(d: Delta, lab: Mapping[str, str]) -> str:
    """One mood's block: the mood_when stamp, then `{headline} — {subtext}` and `carrier_wave` on the
    next line; a non-JSON-object content falls back to oneline(content, 400)."""
    when = lab["mood_when"].format(ts=d.timestamp[:16])
    try:
        obj = json.loads(d.content)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        body = f"{obj.get('headline', '')} — {obj.get('subtext', '')}\n{obj.get('carrier_wave', '')}"
    else:
        body = oneline(d.content, 400)
    return f"{when}\n{body}"


def assemble(fixed: Sequence[str], strips: Sequence[Part], omitted: int, lab: Mapping[str, str]) -> str:
    """Blocks 1–6 joined with newlines; strip headers, led_to, and more_strips carry their leading blank line."""
    parts = list(fixed)
    for i, p in enumerate(strips):
        if i:
            parts.append("\n" + lab["led_to"])
        parts += ["\n" + p.header, *p.anchors, *p.ambient]
    if omitted:
        parts.append("\n" + lab["more_strips"].format(n=omitted, s=plural(omitted)))
    return "\n".join(parts)


def active_containers(lake: Lake, hits: Sequence[Hit]) -> list[Delta]:
    """§5.6 step 2: container hits, then containers reached by cited_by(depth=3) in first-seen order, cut at 60;
    sorted by level desc, hits (in hit order) before the rest (timestamp desc)."""
    found = {h.delta.id: h.delta for h in hits if h.delta.kind == "container"}
    for h in hits:
        for d in _store.cited_by(lake, h.delta.id, depth=CITE_DEPTH):
            if d.kind == "container" and d.id not in found:
                found[d.id] = d
    order = {h.delta.id: i for i, h in enumerate(hits)}
    cut = sorted(list(found.values())[:MAX_CONTAINERS], key=lambda d: d.timestamp, reverse=True)
    return sorted(cut, key=lambda d: (-d.level, order.get(d.id, len(order))))


def strip_parts(
    strip: Timeline, lab: Mapping[str, str], sure: Mapping[str, str] | None = None, toks: Sequence[str] = (),
    cap: int = ANCHOR_CAP, wide: Collection[str] = (), wide_cap: int = ANCHOR_CAP,
) -> tuple[str, list[str], list[str]]:
    """§5.6.3 block 5 for one strip: (header, anchor lines, [surrounding + ambient lines] when both exist)."""
    s, e, n = strip.t_start[11:16], strip.t_end[11:16], len(strip.anchor_ids)
    header = f"════════ {strip.t_start[:10]} · {s}{'' if s == e else '–' + e} · {n} anchor{plural(n)} ════════"
    anchor_lines = [render_line(r, lab, sure, toks, wide_cap if r.delta.id in wide else cap) for r in strip.rows if isinstance(r, TimelineRow) and r.is_anchor]
    ambient = [render_line(r, lab, sure) for r in strip.rows if isinstance(r, CollapsedRun) or not r.is_anchor]
    return header, anchor_lines, [lab["surrounding"], *ambient] if anchor_lines and ambient else ambient


def render_line(
    entry: TimelineRow | CollapsedRun, labels: Mapping[str, str], sure: Mapping[str, str] | None = None,
    toks: Sequence[str] = (), cap: int = ANCHOR_CAP,
) -> str:
    """§5.6.2 dispatch: collapsed run, container, sediment, mood, engagement, user/assistant, default. `sure` maps a
    live stance's id to its confidence label (§6.3.1)."""
    if isinstance(entry, CollapsedRun):
        return f"  {ts8(entry.t_start)}  {entry.source} × {entry.count} (through {ts8(entry.t_end)})"
    d, anchor = entry.delta, entry.is_anchor
    head = f"{'▸' if anchor else ' '} {ts8(d.timestamp)}  "
    idp = f"[{d.id[:12]}] " if anchor else ""
    media = f" [Image attached: media_hash={d.media_hash}]" if d.media_hash else ""
    ref = refuted_suffix(d, labels)
    src = (d.source or "?").ljust(13)[:13] + "·"
    n = len(d.derived_from)
    if d.kind == "container":
        title = d.meta.get("title") if d.meta else None
        label = ("qa-marker" if d.level == 0 else "container").ljust(13)
        return f"{head}{label}· [L{d.level} · {n} delta{plural(n)} · {d.id}] {oneline(d.content) if title is None else title}{media}{ref}"
    if d.kind == "sediment":
        text = d.content.replace("\n", " ")
        first = text.split(".", 1)[0]
        first = first[:199] + "…" if len(first) > 200 else first + ("…" if len(first) < len(text) else "")
        srcs = f" (from {n} sources)" if n else ""
        ground = " (self-referential)" if d.meta and d.meta.get("grounding") == "self-referential" else ""
        if d.source == "lake:stance":  # §5.6.2: a stance names how sure it is, or that it no longer stands
            st = d.meta.get("stance") if isinstance(d.meta, dict) else None
            state = "retired" if isinstance(st, dict) and st.get("retired") else "superseded" if d.superseded_by else None
            state = state or (None if sure is None else sure.get(d.id, "superseded"))  # not a live head: superseded
            ground += labels["stance"].format(label=state) if state else ""
        return f"{head}sediment     · {idp}{first}{srcs}{ground}{media}{ref}"
    if d.kind == "mood":
        return f"{head}mood         · {idp}feeling: {mood_state(d)}{media}{ref}"
    if d.kind == "engagement" and d.engagement is not None:
        e = d.engagement
        text = e.note if e.note and e.note.strip() else footer(d)
        return f"{head}{src} {idp}[{VERBS[e.kind]} {e.target_id}] {oneline(text)}{media}{ref}"
    role = labels["user_role"] if "user" in d.tags else labels["assistant_role"] if "assistant" in d.tags else ""
    body = window(d.content, toks, cap) if anchor else oneline(d.content)
    return f"{head}{src}{role} {idp}{body}{media}{ref}"


def refuted_suffix(d: Delta, labels: Mapping[str, str]) -> str:
    """§5.6.2 markers: an inline suffix naming the newest refuter, noting the row rests on a corrected
    memory, and/or naming the newest row that supersedes it with the value it states. Empty when none is
    set; falls back to the refuter's source when it left no note (a refute with no note is the common case)."""
    out = ""
    if d.refuted_by:
        r = d.refuted_by[0]
        detail = f"{r.source} · {oneline(r.note, 60)}" if r.note else r.source
        out += labels["refuted"].format(n=len(d.refuted_by), detail=detail)
    if d.rests_on_refuted:
        out += labels["rests_on_refuted"].format(n=len(d.rests_on_refuted))
    if d.superseded_by:  # §5.6.2: the newest superseder and the value it states
        x = d.superseded_by[0]
        out += labels["superseded"].format(id=x.id, value=oneline(x.new_value, 60))
    return out


def mood_state(d: Delta) -> str:
    """The `feeling:` tag suffix, else `state` from the JSON content, else oneline(content, 80)."""
    for t in d.tags:
        if t.startswith("feeling:"):
            return t[8:]
    try:
        obj = json.loads(d.content)
    except ValueError:
        obj = None
    state = obj.get("state") if isinstance(obj, dict) else None
    return oneline(d.content, 80) if state is None else str(state)


def footer(d: Delta) -> str:
    """The snapshot's last line starting `> —`, without the `> ` (content first, then meta.snapshot)."""
    snap = str(d.meta.get("snapshot", "")) if d.meta else ""
    lines = [ln[2:] for ln in f"{d.content}\n{snap}".split("\n") if ln.startswith("> —")]
    return lines[-1] if lines else ""


def ts8(t: str) -> str:
    return t[11:19]


def plural(n: int) -> str:
    return "" if n == 1 else "s"

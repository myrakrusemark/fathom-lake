"""RemoteLake — the §13 HTTP client and its offline write spool.

SPEC §13.6 (client), §13.7 (spool). stdlib only: urllib, fcntl (msvcrt on Windows), json. `RemoteLake` is configured by its
arguments alone; the env file lives in `_env` (re-exported here for older callers).
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import _io, _store, _time
from ._env import read_env_file as read_env_file  # §13.8: `from lake.remote import read_env_file` still works
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from ._types import (
    Bucket, ClosedLoopError, CollapsedRun, ConsolidateError, ConsolidateRun, ContextResult, Delta, DigestRun, DigestStep,
    Duration, EmbedError, Engagement, Hit, LakeError, Lineage, NotFoundError, PlanError, PlanResult,
    Refutation, SchemaError, StepResult, Supersession, Timeline, TimelineRow, TimeSpec,
)

SPOOL_CAP = 10_000  # §13.7: lines kept; older ones drop into spool.dropped's count
HEALTH_TIMEOUT = 5.0  # §13.6: the flush health probe
FLUSH_TIMEOUT = 120.0  # §13.6: the flush's own import
WIRE_ERRORS: MappingProxyType[str, type[Exception]] = MappingProxyType({  # §13.4 client-side mapping
    "SchemaError": SchemaError, "ClosedLoopError": ClosedLoopError, "NotFoundError": NotFoundError,
    "PlanError": PlanError, "ConsolidateError": ConsolidateError, "EmbedError": EmbedError,
    "LakeError": LakeError, "ValueError": ValueError,
})


# --- wire shapes (§13.1) ---------------------------------------------------------------------------


class _Unreachable(Exception):
    """Internal: a §13.6 connection error — no HTTP response was obtained."""


def _mapped(status: int, body: bytes) -> Exception:
    """§13.4: rebuild the concrete class from the envelope's `error` name, not the status."""
    try:
        obj = json.loads(body)
        name, message = obj["error"], obj["message"]
        if not isinstance(name, str) or not isinstance(message, str):
            raise TypeError(name)
    except (ValueError, TypeError, KeyError):
        return LakeError(f"HTTP {status}: {body.decode('utf-8', 'replace')[:200]}")
    cls = WIRE_ERRORS.get(name)
    if cls is None:
        return LakeError(f"{name}: {message}")
    exc = cls(message)
    if isinstance(exc, EmbedError):
        setattr(exc, "stored", None)  # §13.4: neither .delta nor .stored can cross the wire
    return exc


def _delta(obj: dict[str, Any]) -> Delta:
    eng = obj["engagement"]
    refs = obj.get("refuted_by") or ()  # §13.1: present only on a refuted row; wire-only, never in export_line
    dep = obj.get("rests_on_refuted") or ()  # §13.1: present only when the row rests on a corrected memory
    sup = obj.get("superseded_by") or ()  # §13.1: present only on a superseded row; an older server never sends it
    return Delta(
        id=obj["id"], timestamp=obj["timestamp"], content=obj["content"], source=obj["source"],
        kind=obj["kind"], level=obj["level"], tags=list(obj["tags"]),
        derived_from=list(obj["derived_from"]), expires_at=obj["expires_at"],
        media_hash=obj["media_hash"], meta=obj["meta"],
        engagement=None if eng is None else Engagement(eng["target_id"], eng["kind"], eng["by"], eng["note"]),
        refuted_by=tuple(Refutation(r["id"], r["source"], r["timestamp"], r["note"]) for r in refs),
        rests_on_refuted=tuple(dep),
        superseded_by=tuple(Supersession(r["id"], r["by"], r["old_value"], r["new_value"]) for r in sup),
    )


def _hit(obj: dict[str, Any]) -> Hit:
    return Hit(_delta(obj["delta"]), obj["score"], obj["relevance"], obj["recency"], obj["valence"],
               obj["matched"], obj["step"])


def _timeline(obj: dict[str, Any]) -> Timeline:
    rows: list[TimelineRow | CollapsedRun] = [  # §13.1: the key sets discriminate
        TimelineRow(_delta(r["delta"]), r["is_anchor"]) if "delta" in r
        else CollapsedRun(r["source"], r["count"], r["t_start"], r["t_end"])
        for r in obj["rows"]
    ]
    return Timeline(obj["id"], obj["t_start"], obj["t_end"], list(obj["anchor_ids"]), rows)


def _step_result(obj: dict[str, Any]) -> StepResult:
    hits, buckets, timelines = obj["hits"], obj["buckets"], obj["timelines"]
    return StepResult(
        None if hits is None else [_hit(h) for h in hits],
        None if buckets is None else [Bucket(b["key"], b["count"], list(b["delta_ids"])) for b in buckets],
        None if timelines is None else [_timeline(t) for t in timelines],
    )


def _plan_result(obj: dict[str, Any]) -> PlanResult:
    row = obj["sediment"]
    return PlanResult({sid: _step_result(res) for sid, res in obj["steps"]}, list(obj["warnings"]),
                      obj["timing_ms"], None if row is None else _delta(row))


def _context_result(obj: dict[str, Any]) -> ContextResult:
    return ContextResult(
        None if obj["crystal"] is None else _delta(obj["crystal"]), [_hit(h) for h in obj["hits"]],
        [_delta(d) for d in obj["containers"]], [_timeline(t) for t in obj["strips"]],
        obj["omitted_strips"], obj["rendered"], list(obj["warnings"]),
    )


def _run(obj: dict[str, Any]) -> ConsolidateRun:
    window = obj["window"]
    return ConsolidateRun(obj["kind"], [_delta(d) for d in obj["written"]], obj["skipped"],
                          list(obj["warnings"]), obj["think_calls"], None if window is None else (window[0], window[1]))


def _digest_run(obj: dict[str, Any]) -> DigestRun:
    steps = [DigestStep(s["step"], s["due"], None if s["run"] is None else _run(s["run"]), s["prompt_chars"])
             for s in obj["steps"]]
    return DigestRun(steps, list(obj["days"]), list(obj["given_up"]), list(obj["errors"]), list(obj["warnings"]),
                     obj["stopped"], obj["think_calls"], obj["prompt_chars"])


def _ts_str(spec: TimeSpec) -> str:
    """§13.1: a datetime through the §3.7 producer; a string crosses as given."""
    return _time.dt_to_ts(spec) if isinstance(spec, datetime) else spec


def _ts(spec: TimeSpec | None) -> str | None:
    return None if spec is None else _ts_str(spec)


def _window_str(window: Duration | tuple[TimeSpec, TimeSpec] | None) -> str | list[str] | None:
    if window is None or isinstance(window, str):
        return window
    if isinstance(window, timedelta):
        return _time.render_duration(window.total_seconds())
    return [_ts_str(window[0]), _ts_str(window[1])]


def _expires_str(value: Duration | TimeSpec) -> str:
    if isinstance(value, timedelta):
        return _time.render_duration(value.total_seconds())
    return _ts_str(value)


def _list(v: Sequence[str] | None) -> list[str] | None:
    return None if v is None else list(v)


def _one_or_list(v: str | Sequence[str] | None) -> str | list[str] | None:
    return v if v is None or isinstance(v, str) else list(v)


def _drop_none(body: dict[str, Any]) -> dict[str, Any]:
    """§13.1: an omitted key means the signature default, so None-valued keys are not sent."""
    return {k: v for k, v in body.items() if v is not None}


def _query(params: dict[str, str]) -> str:
    return "?" + urllib.parse.urlencode(params) if params else ""


def _validate_write(
    content: str, source: str, tags: Sequence[str] | None, kind: str | None,
    derived_from: Sequence[str] | None, expires: Duration | TimeSpec | None, media: str | None,
    meta: dict[str, Any] | None, timestamp: TimeSpec | None, now: datetime,
) -> tuple[str, str | None, list[str], list[str]]:
    """§13.7: the §4.4 step 1–3 validations that need no file, before any request — the same
    classes online or offline. Returns (timestamp, expires_at, tags, derived_from), stored form."""
    if not content.strip() or "\x00" in content:
        raise ValueError("content must be non-empty and NUL-free")
    _store.check_source(source)
    if kind is not None and kind not in _store.WRITE_KINDS:
        raise ValueError(f"kind must be None or one of {sorted(_store.WRITE_KINDS)}, not {kind!r}")
    if media is not None and not _store.MEDIA_RE.match(media):
        raise ValueError(f"media must be 16 to 64 lowercase hex characters, not {media!r}")
    _store.dump_meta(meta)  # finite numbers only
    ts = _time.dt_to_ts(now) if timestamp is None else _time.render_timespec(timestamp, now)
    expires_at = None
    if expires is not None:
        exp = _time.parse_expires(expires, now)
        if exp <= now:
            raise ValueError(f"expires must be after now: {expires!r}")
        expires_at = _time.dt_to_ts(exp)
    tag_list = _store.check_tags(_store.normalise_tags(tags))
    parents = _store.normalise_tags(derived_from)
    if kind is not None and not parents:
        raise ClosedLoopError(f"kind={kind!r} needs a non-empty derived_from")
    return ts, expires_at, tag_list, parents


# Windows has no flock: msvcrt locks a byte range instead, so one byte far past any spool's end stands in for the
# whole file. msvcrt has no shared mode, so a shared lock is exclusive there.
_WIN_LOCK_AT = 2**31 - 2
# O_NOFOLLOW is POSIX-only; O_BINARY is Windows-only and keeps os.write from translating newlines to CRLF.
_SPOOL_FLAGS = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)


def _lock(fd: int, *, shared: bool = False) -> None:
    """Block until this process holds the spool lock: flock on POSIX, a msvcrt byte lock on Windows."""
    if sys.platform == "win32":
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, _WIN_LOCK_AT, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                break
            except OSError:  # LK_LOCK gives up after about 10 s; keep waiting, as flock does
                continue
        os.lseek(fd, pos, os.SEEK_SET)
    else:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)


def _close(fd: int) -> None:
    """Close a spool fd. flock is released by the close; a msvcrt lock is released first, explicitly."""
    if sys.platform == "win32":
        try:
            os.lseek(fd, _WIN_LOCK_AT, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # not locked: the open succeeded but the lock was never taken
    os.close(fd)


def _read_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(fd, 1 << 20):
        chunks.append(chunk)
    return b"".join(chunks)


# --- the client (§13.6) ----------------------------------------------------------------------------


class RemoteLake:
    """The same public surface as `Lake`, each method over its §13.3 endpoint. One per thread,
    like a `Lake`; holds no connection (`close()` is a no-op)."""

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        consolidate_timeout: float = 1800.0,
        spool_path: str | os.PathLike[str] | None = None,
        **extra: Any,
    ) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"not an http(s) url: {url}")
        if extra:  # §13.6: every Lake keyword RemoteLake does not define, one uniform refusal
            raise LakeError(f"{next(iter(extra))}= is not available over HTTP")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.consolidate_timeout = consolidate_timeout
        self.spool_path = Path(spool_path) if spool_path is not None else Path.home() / ".lake" / "spool.jsonl"
        self.last_warnings: list[str] = []
        self.last_run: ConsolidateRun | None = None

    def close(self) -> None:
        """A no-op: no connection is held (§13.6)."""

    def __enter__(self) -> RemoteLake:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- transport ---------------------------------------------------------------------------------

    def _headers(self, content_type: str | None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if content_type is not None:
            headers["Content-Type"] = content_type
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _send(
        self, method: str, path: str, body: Mapping[str, Any] | None = None, *,
        data: bytes | None = None, content_type: str = "application/json", timeout: float | None = None,
    ) -> Any:
        """One request, decoded JSON back; `_Unreachable` when no HTTP response was obtained."""
        payload = data
        if body is not None:
            try:
                payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ValueError(f"request body is not JSON: {exc}") from exc
        req = urllib.request.Request(
            self.url + path, data=payload, method=method,
            headers=self._headers(content_type if payload is not None else None),
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout if timeout is None else timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise _mapped(exc.code, _http_body(exc)) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise _Unreachable(f"lake unreachable: {exc}") from exc
        return json.loads(raw)

    def _loud(
        self, method: str, path: str, body: Mapping[str, Any] | None = None, *,
        data: bytes | None = None, content_type: str = "application/json", timeout: float | None = None,
    ) -> Any:
        """_send with a §13.6 fail-loud connection error: LakeError('lake unreachable: ...')."""
        try:
            return self._send(method, path, body, data=data, content_type=content_type, timeout=timeout)
        except _Unreachable as exc:
            raise LakeError(str(exc)) from exc

    def _begin(self) -> None:
        """Start a public call: fresh warnings, then the §13.7 lazy flush before the first request."""
        self.last_warnings = []
        self._maybe_flush()

    # --- writes ------------------------------------------------------------------------------------

    def write(
        self,
        content: str,
        source: str,
        tags: Sequence[str] | None = None,
        kind: str | None = None,
        derived_from: Sequence[str] | None = None,
        expires: Duration | TimeSpec | None = None,
        media: str | None = None,
        meta: dict[str, Any] | None = None,
        timestamp: TimeSpec | None = None,
        dedupe: bool = True,
        embed: bool | None = None,
    ) -> Delta:
        self.last_warnings = []
        now = datetime.now(UTC)
        ts, expires_at, tag_list, parents = _validate_write(
            content, source, tags, kind, derived_from, expires, media, meta, timestamp, now,
        )
        body = _drop_none({
            "content": content, "source": source, "tags": tag_list or None, "kind": kind,
            "derived_from": parents or None,
            "expires": None if expires is None else _expires_str(expires),
            "media": media, "meta": meta, "timestamp": _ts(timestamp),
            "dedupe": dedupe, "embed": embed,
        })
        self._maybe_flush()
        try:
            return _delta(self._send("POST", "/v1/write", body))
        except _Unreachable as exc:
            if kind is not None:  # §13.7: a structural write fails loud and spools nothing
                raise LakeError(str(exc)) from exc
            delta = self._spool_write(content, source, ts, expires_at, tag_list, parents, media, meta)
            self.last_warnings.append(f"{exc}; write spooled to {self.spool_path}")
            return delta

    def engage(
        self,
        delta_id: str,
        kind: str,
        *,
        by: str | None = None,
        note: str | None = None,
        tags: Sequence[str] | None = None,
        snapshot: bool = True,
        dedupe: bool = True,
    ) -> Delta:
        self._begin()
        body = _drop_none({"delta_id": delta_id, "kind": kind, "by": by, "note": note,
                           "tags": _list(tags), "snapshot": snapshot, "dedupe": dedupe})
        return _delta(self._loud("POST", "/v1/engage", body))

    def consolidate(
        self, kind: str, window: Duration | tuple[TimeSpec, TimeSpec] | None = None, **opts: object
    ) -> Delta | None:
        self._begin()
        body = _drop_none({"kind": kind, "window": _window_str(window), "opts": dict(opts) or None})
        obj = self._loud("POST", "/v1/consolidate", body, timeout=self.consolidate_timeout)
        self.last_run = None if obj["run"] is None else _run(obj["run"])
        return None if obj["delta"] is None else _delta(obj["delta"])

    def digest(self, *, since: str | None = None, max_units: int | None = None, max_tokens: int | None = None,
               backfill_max: int | None = None, dry_run: bool = False) -> DigestRun:
        self._begin()
        body = _drop_none({"since": since, "max_units": max_units, "max_tokens": max_tokens,
                           "backfill_max": backfill_max, "dry_run": dry_run or None})
        return _digest_run(self._loud("POST", "/v1/digest", body, timeout=self.consolidate_timeout)["run"])

    def sweep(self) -> dict[str, int]:
        self._begin()
        obj = self._loud("POST", "/v1/sweep", {})
        return {"deleted": int(obj["deleted"]), "orphan_media": int(obj["orphan_media"])}

    def embed_missing(self, *, batch_size: int = 64, limit: int | None = None) -> int:
        self._begin()
        obj = self._loud("POST", "/v1/embed-missing", _drop_none({"batch_size": batch_size, "limit": limit}))
        return int(obj["stored"])

    # --- reads -------------------------------------------------------------------------------------

    def get(self, delta_id: str, *, include_expired: bool = False) -> Delta | None:
        self._begin()
        q = {"include_expired": "1"} if include_expired else {}
        obj = self._loud("GET", "/v1/get/" + urllib.parse.quote(delta_id, safe="") + _query(q))
        return None if obj is None else _delta(obj)

    def recall(
        self,
        query: str | None = None,
        *,
        source: str | Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        any_tags: Sequence[str] | None = None,
        exclude_tags: Sequence[str] | None = None,
        kind: str | Sequence[str] | None = None,
        since: TimeSpec | None = None,
        until: TimeSpec | None = None,
        limit: int = 20,
        exclude_sources: Sequence[str] | None = None,
        include_expired: bool = False,
        noise: bool = True,
        recency: bool = True,
        min_relevance: float = 0.0,
        plan: Sequence[dict[str, Any]] | None = None,
        filters: Mapping[str, object] | None = None,
        sediment: bool | None = None,
    ) -> list[Hit]:
        self._begin()
        body = _drop_none({
            "query": query, "source": _one_or_list(source), "tags": _list(tags),
            "any_tags": _list(any_tags), "exclude_tags": _list(exclude_tags),
            "kind": _one_or_list(kind), "since": _ts(since), "until": _ts(until), "limit": limit,
            "exclude_sources": _list(exclude_sources), "include_expired": include_expired,
            "noise": noise, "recency": recency, "min_relevance": min_relevance,
            "plan": None if plan is None else [dict(s) for s in plan],
            "filters": None if filters is None else dict(filters),
            "sediment": sediment,
        })
        try:
            obj = self._send("POST", "/v1/recall", body)
        except _Unreachable as exc:  # §13.6 fail soft: the hook path fails open
            self.last_warnings.append(str(exc))
            return []
        self.last_warnings.extend(obj["warnings"])
        return [_hit(h) for h in obj["hits"]]

    def plan(
        self, steps: Sequence[dict[str, Any]], *, filters: Mapping[str, object] | None = None,
        sediment: bool | None = None,
    ) -> PlanResult:
        self._begin()
        body = _drop_none({"steps": [dict(s) for s in steps],
                           "filters": None if filters is None else dict(filters),
                           "sediment": sediment})
        try:
            obj = self._send("POST", "/v1/plan", body)
        except _Unreachable as exc:
            warning = str(exc)
            self.last_warnings.append(warning)
            return PlanResult({}, [warning], 0.0, None)
        res = _plan_result(obj)
        self.last_warnings.extend(res.warnings)
        return res

    def context(
        self,
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
    ) -> str:
        return self.context_blocks(
            query, budget=budget, limit=limit, source=source, exclude_sources=exclude_sources, tags=tags,
            any_tags=any_tags, exclude_tags=exclude_tags, kind=kind, since=since, until=until,
            crystal=crystal, containers=containers, noise=noise, recency=recency,
            min_relevance=min_relevance, labels=labels,
        ).rendered

    def context_blocks(
        self,
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
        self._begin()
        body = _drop_none({
            "query": query, "budget": budget, "limit": limit, "source": _one_or_list(source),
            "exclude_sources": _list(exclude_sources), "tags": _list(tags), "any_tags": _list(any_tags),
            "exclude_tags": _list(exclude_tags), "kind": _one_or_list(kind), "since": _ts(since),
            "until": _ts(until), "crystal": crystal, "containers": containers, "noise": noise,
            "recency": recency, "min_relevance": min_relevance,
            "labels": None if labels is None else dict(labels),
        })
        try:
            obj = self._send("POST", "/v1/context", body)
        except _Unreachable as exc:
            warning = str(exc)
            self.last_warnings.append(warning)
            return ContextResult(None, [], [], [], 0, "", [warning])
        res = _context_result(obj)
        self.last_warnings.extend(res.warnings)
        return res

    def system_prompt(
        self, *, moods: int = 3, budget: int = 8000, labels: Mapping[str, str] | None = None
    ) -> str:
        self._begin()
        body = _drop_none({"moods": moods, "budget": budget,
                           "labels": None if labels is None else dict(labels)})
        try:
            obj = self._send("POST", "/v1/system-prompt", body)
        except _Unreachable as exc:  # §13.6 fail soft: the hook path fails open
            self.last_warnings.append(str(exc))
            return ""
        except LakeError as exc:  # a server predating this endpoint answers "unknown endpoint"; degrade, don't crash the hook
            if "system-prompt" in str(exc) or "unknown endpoint" in str(exc):
                self.last_warnings.append(f"{exc}; system_prompt unavailable on this server")
                return ""
            raise
        return str(obj["prompt"])

    def lineage(self, delta_id: str, *, depth: int | None = None, include_expired: bool = True) -> Lineage:
        self._begin()
        q: dict[str, str] = {}
        if depth is not None:
            q["depth"] = str(depth)
        if not include_expired:
            q["include_expired"] = "0"
        obj = self._loud("GET", "/v1/lineage/" + urllib.parse.quote(delta_id, safe="") + _query(q))
        return Lineage([_delta(d) for d in obj["rows"]], list(obj["dangling"]))

    def cited_by(self, delta_id: str, *, depth: int = 1, include_expired: bool = False) -> list[Delta]:
        self._begin()
        q: dict[str, str] = {}
        if depth != 1:
            q["depth"] = str(depth)
        if include_expired:
            q["include_expired"] = "1"
        obj = self._loud("GET", "/v1/cited-by/" + urllib.parse.quote(delta_id, safe="") + _query(q))
        return [_delta(d) for d in obj["rows"]]

    def crystal(self) -> Delta | None:
        self._begin()
        obj = self._loud("GET", "/v1/crystal")
        return None if obj is None else _delta(obj)

    def due(self, kind: str) -> bool:
        self._begin()
        return bool(self._loud("GET", "/v1/due/" + urllib.parse.quote(kind, safe=""))["due"])

    def stats(self) -> dict[str, Any]:
        self._begin()
        return dict(self._loud("GET", "/v1/stats"))

    # --- streaming (§13.5) -------------------------------------------------------------------------

    def export(self, path: str | os.PathLike[str], *, include_expired: bool = False, vectors: bool = False) -> int:
        self._begin()
        q: dict[str, str] = {}
        if include_expired:
            q["include_expired"] = "1"
        if vectors:
            q["vectors"] = "1"
        req = urllib.request.Request(self.url + "/v1/export" + _query(q), headers=self._headers(None))
        count = 0
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp, open(path, "wb") as fh:
                while chunk := resp.read(1 << 16):
                    fh.write(chunk)
                    count += chunk.count(b"\n")
        except urllib.error.HTTPError as exc:
            raise _mapped(exc.code, _http_body(exc)) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise LakeError(f"lake unreachable: {exc}") from exc
        return count

    def import_(self, path: str | os.PathLike[str], *, include_expired: bool = False) -> dict[str, int]:
        self._begin()
        q = {"include_expired": "1"} if include_expired else {}
        obj = self._loud("POST", "/v1/import" + _query(q), data=Path(path).read_bytes(),
                         content_type="application/x-ndjson")
        return {"written": int(obj["written"]), "skipped": int(obj["skipped"]), "errors": int(obj["errors"])}

    # --- the file-only surface (§13.6 refusals) ----------------------------------------------------

    def media_path(self, media_hash: str) -> Path | None:
        raise LakeError("media_path is not available over HTTP")

    def meta_get(self, key: str) -> str | None:
        raise LakeError("meta_get is not available over HTTP")

    def meta_set(self, key: str, value: str | None) -> None:
        raise LakeError("meta_set is not available over HTTP")

    # --- the spool (§13.7) -------------------------------------------------------------------------

    def _spool_fd(self) -> int:
        """The spool opened O_RDWR|O_CREAT|O_APPEND mode 0600; flock on this fd is the lock. O_NOFOLLOW
        refuses a symlink planted at the path (arbitrary-file append/truncate); the parent is 0700."""
        self.spool_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return os.open(self.spool_path, _SPOOL_FLAGS, 0o600)

    def _spool_write(
        self, content: str, source: str, ts: str, expires_at: str | None,
        tags: list[str], parents: list[str], media: str | None, meta: dict[str, Any] | None,
    ) -> Delta:
        """One §4.8 lake-format line appended under LOCK_EX; the Delta back, marked meta.spooled."""
        delta = Delta(
            id=uuid.uuid4().hex[:12], timestamp=ts, content=content, source=source, kind=None, level=0,
            tags=tags, derived_from=parents, expires_at=expires_at, media_hash=media,
            meta={**(meta or {}), "spooled": True}, engagement=None,
        )
        line = _io.export_line(delta).encode("utf-8")
        fd = self._spool_fd()
        try:
            _lock(fd)
            os.write(fd, line)
            self._enforce_cap(fd)
        finally:
            _close(fd)
        return delta

    def _enforce_cap(self, fd: int) -> None:
        """§13.7 cap, under the caller's LOCK_EX: keep the newest SPOOL_CAP lines, count the rest."""
        lines = _read_fd(fd).splitlines(keepends=True)
        if len(lines) <= SPOOL_CAP:
            return
        dropped = len(lines) - SPOOL_CAP
        os.ftruncate(fd, 0)
        os.write(fd, b"".join(lines[dropped:]))  # O_APPEND: lands at offset 0 after the truncate
        side = os.open(self.spool_path.with_suffix(".dropped"),
                       os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
        try:
            prior = _read_fd(side).strip()
            os.ftruncate(side, 0)
            os.lseek(side, 0, os.SEEK_SET)
            os.write(side, b"%d\n" % ((int(prior) if prior.isdigit() else 0) + dropped))
        finally:
            os.close(side)

    def spool_status(self) -> tuple[int, str | None]:
        """(line count, oldest line's timestamp) under LOCK_SH; (0, None) when missing or empty."""
        if not self.spool_path.exists():
            return 0, None
        fd = self._spool_fd()
        try:
            _lock(fd, shared=True)
            data = _read_fd(fd)
        finally:
            _close(fd)
        lines = data.splitlines()
        if not lines:
            return 0, None
        try:
            oldest = str(json.loads(lines[0])["timestamp"])
        except (ValueError, KeyError, TypeError):
            oldest = None
        return len(lines), oldest

    def flush_spool(self) -> dict[str, int] | None:
        """`lake spool --flush`: force the §13.7 flush now. None when the spool was empty;
        raises the mapped error (LakeError when unreachable) on failure, leaving the spool intact."""
        try:
            return self._flush()
        except _Unreachable as exc:
            raise LakeError(str(exc)) from exc

    def _flush(self) -> dict[str, int] | None:
        """§13.7 steps 2–5: LOCK_EX, re-check, POST the bytes to /v1/import, truncate under the lock."""
        fd = self._spool_fd()
        try:
            _lock(fd)
            data = _read_fd(fd)
            if not data:
                return None
            obj = self._send("POST", "/v1/import", data=data, content_type="application/x-ndjson",
                             timeout=FLUSH_TIMEOUT)
            os.ftruncate(fd, 0)
            return {"written": int(obj["written"]), "skipped": int(obj["skipped"]), "errors": int(obj["errors"])}
        finally:
            _close(fd)

    def _maybe_flush(self) -> None:
        """§13.7 step 1 and the lazy trigger: a non-empty spool plus a healthy server flushes;
        any failure to probe skips silently, a failed flush lands in this call's last_warnings."""
        try:
            if os.stat(self.spool_path).st_size == 0:
                return
        except OSError:
            return
        try:
            health = self._send("GET", "/v1/health", timeout=HEALTH_TIMEOUT)
        except Exception:
            return  # offline is the normal case; the caller's request proceeds under its own rules
        if not (isinstance(health, dict) and health.get("ok") is True):
            return
        try:
            self._flush()
        except Exception as exc:
            self.last_warnings.append(f"spool flush failed: {exc}")


def _http_body(exc: urllib.error.HTTPError) -> bytes:
    try:
        return exc.read()
    except OSError:
        return b""

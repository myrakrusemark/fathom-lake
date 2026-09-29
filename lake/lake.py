"""The Lake facade (SPEC §4.3–§4.8): exact public signatures, each delegating to a module."""

from __future__ import annotations

import os
import sqlite3
import warnings
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import _consolidate, _context, _db, _digest, _env, _io, _plan, _recall, _store, _time, _vectors
from ._types import DEFAULT_DUE_THRESHOLDS, DEFAULT_LABELS, ConsolidateError, ConsolidateRun, ContextResult, Delta, DigestRun
from ._types import Duration, Hit, LakeError, Lineage, NoiseRules, PlanResult, TimeSpec


class Lake:
    """One SQLite file, one connection, two optional callbacks (§4.3)."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        think: Callable[..., str | dict[str, Any]] | None = None,
        embed: Callable[[list[str]], list[list[float]]] | None = None,
        model_name: str | None = None,
        recency_half_life: Duration = "30d",
        collapse_sources: Sequence[str] = (),
        automation: Sequence[str] | None = None,
        noise: NoiseRules | None = None,
        dedupe_window: Duration | None = None,
        embed_on_write: bool = True,
        write_crystal_file: bool = True,
        labels: Mapping[str, str] | None = None,
        due_thresholds: Mapping[str, object] | None = None,
        clock: Callable[[], datetime] | None = None,
        readonly: bool = False,
    ) -> None:
        init: dict[str, Any] = {k: v for k, v in locals().items() if k not in ("self", "path")}
        self.path = Path(path)
        self.stem = self.path.stem
        self.media_dir = self.path.with_name(self.stem + ".media")
        self.crystal_path = self.path.with_name(self.stem + ".crystal.md")
        self.think = think
        self.embed = embed
        self.model_name = model_name
        self.half_life_s = _time.parse_duration(recency_half_life)
        if self.half_life_s <= 0:
            raise ValueError("recency_half_life must be positive")
        if collapse_sources:  # §4.3: the old name for source: automation rules, kept one release
            warnings.warn("collapse_sources= is deprecated; use automation=['source:<name>']", DeprecationWarning, 2)
        rules = [*(_env.DEFAULT_AUTOMATION if automation is None else automation), *(f"source:{s}" for s in collapse_sources)]
        self.init = {**init, "automation": rules, "collapse_sources": ()}  # the options, for a copy (a dry run's)
        self.automation = tuple((k, v) for k, _, v in (str(r).partition(":") for r in rules))  # §4.3, §6
        if bad := [f"{k}:{v}" for k, v in self.automation if k not in ("tag", "source", "prefix") or not v]:
            raise ValueError(f"an automation rule is tag:<tag>, source:<source> or prefix:<text>, not {bad[0]!r}")
        self.automation_sources = tuple(v for k, v in self.automation if k == "source")  # T5 collapse, 160-char lines
        self.noise = noise if noise is not None else NoiseRules()
        self.dedupe_window_s = None if dedupe_window is None else _time.parse_duration(dedupe_window)
        self.embed_on_write = embed_on_write
        self.write_crystal_file = write_crystal_file
        self.labels: dict[str, str] = {**DEFAULT_LABELS, **(labels or {})}
        self.due_thresholds: dict[str, object] = {**DEFAULT_DUE_THRESHOLDS, **(due_thresholds or {})}
        self.clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self.readonly = readonly
        self.last_warnings: list[str] = []
        self.last_run: ConsolidateRun | None = None
        self._vectors: _vectors.VectorCache | None = None
        self.conn: sqlite3.Connection = _db.open_db(self.path, readonly=readonly, created_at=self.now_str())

    def now(self) -> datetime:
        """The clock's current time as an aware UTC datetime."""
        return _time.to_utc(self.clock())

    def now_str(self) -> str:
        return _time.dt_to_ts(self.clock())

    def tx(self) -> AbstractContextManager[sqlite3.Connection]:
        """BEGIN IMMEDIATE / COMMIT / ROLLBACK on this instance's connection; LakeError when readonly."""
        self.require_writable()
        return _db.tx(self.conn)

    def require_writable(self) -> None:
        if self.readonly:
            raise LakeError(f"{self.path}: opened readonly")

    def call_embed(self, texts: list[str]) -> list[list[float]]:
        """The only way to invoke `embed`: refuses to run while a transaction is open (§4.4 step 7, §9)."""
        if self.embed is None:
            raise LakeError("this Lake has no embed callback")
        if self.conn.in_transaction:
            raise LakeError("embed called with a transaction open")
        return self.embed(texts)

    def call_think(self, prompt: str, *, system: str | None = None, json: bool = False) -> str | dict[str, Any]:
        """The only way to invoke `think`: ConsolidateError without one, never inside a transaction (§6)."""
        if self.think is None:
            raise ConsolidateError("consolidate() needs a think callback")
        if self.conn.in_transaction:
            raise LakeError("think called with a transaction open")
        return self.think(prompt, system=system, json=json)

    @property
    def vectors(self) -> _vectors.VectorCache | None:
        """The per-instance matrix cache; None when the Lake has no `embed`."""
        if self.embed is None:
            return None
        if self._vectors is None:
            self._vectors = _vectors.VectorCache()
        return self._vectors

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Lake:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

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
        return _store.write(
            self, content, source, tags=tags, kind=kind, derived_from=derived_from, expires=expires,
            media=media, meta=meta, timestamp=timestamp, dedupe=dedupe, embed=embed,
        )

    def get(self, delta_id: str, *, include_expired: bool = False) -> Delta | None:
        return _store.get(self, delta_id, include_expired=include_expired)

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
        if plan is not None:
            given = (query, source, tags, any_tags, exclude_tags, kind, since, until, exclude_sources)
            flags = (limit, include_expired, noise, recency, min_relevance) != (20, False, True, True, 0.0)
            if flags or any(v is not None for v in given):
                raise ValueError("recall(plan=...) accepts no other argument than filters and sediment")
            return _plan.recall_hits(self, plan, filters, sediment=sediment)
        if sediment:
            raise ValueError("sediment=True needs a plan (§4.5)")
        return _recall.recall(
            self, query, source=source, tags=tags, any_tags=any_tags, exclude_tags=exclude_tags, kind=kind,
            since=since, until=until, limit=limit, exclude_sources=exclude_sources,
            include_expired=include_expired, noise=noise, recency=recency, min_relevance=min_relevance,
        )

    def plan(
        self, steps: Sequence[dict[str, Any]], *, filters: Mapping[str, object] | None = None,
        sediment: bool | None = None,
    ) -> PlanResult:
        return _plan.run(self, steps, filters, sediment=sediment)

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
        return _context.context_blocks(
            self, query, budget=budget, limit=limit, source=source, exclude_sources=exclude_sources, tags=tags,
            any_tags=any_tags, exclude_tags=exclude_tags, kind=kind, since=since, until=until, crystal=crystal,
            containers=containers, noise=noise, recency=recency, min_relevance=min_relevance, labels=labels,
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
        return _context.context_blocks(
            self, query, budget=budget, limit=limit, source=source, exclude_sources=exclude_sources, tags=tags,
            any_tags=any_tags, exclude_tags=exclude_tags, kind=kind, since=since, until=until, crystal=crystal,
            containers=containers, noise=noise, recency=recency, min_relevance=min_relevance, labels=labels,
        )

    def system_prompt(
        self, *, moods: int = 3, budget: int = 8000, labels: Mapping[str, str] | None = None
    ) -> str:
        return _context.system_prompt(self, moods=moods, budget=budget, labels=labels)

    def lineage(self, delta_id: str, *, depth: int | None = None, include_expired: bool = True) -> Lineage:
        return _store.lineage(self, delta_id, depth=depth, include_expired=include_expired)

    def cited_by(self, delta_id: str, *, depth: int = 1, include_expired: bool = False) -> list[Delta]:
        return _store.cited_by(self, delta_id, depth=depth, include_expired=include_expired)

    def meta_get(self, key: str) -> str | None:
        if not key.startswith("host:"):
            raise ValueError(f"meta key must start with 'host:': {key!r}")
        return _store.meta_get(self, key)

    def meta_set(self, key: str, value: str | None) -> None:
        if not key.startswith("host:"):
            raise ValueError(f"meta key must start with 'host:': {key!r}")
        _store.meta_set(self, key, value)

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
        return _store.engage(self, delta_id, kind, by=by, note=note, tags=tags, snapshot=snapshot, dedupe=dedupe)

    def consolidate(
        self, kind: str, window: Duration | tuple[TimeSpec, TimeSpec] | None = None, **opts: object
    ) -> Delta | None:
        return _consolidate.consolidate(self, kind, window, **opts)

    def digest(self, *, since: str | None = None, max_units: int | None = None, max_tokens: int | None = None,
               backfill_max: int | None = None, dry_run: bool = False) -> DigestRun:
        return _digest.digest(self, since=since, max_units=max_units, max_tokens=max_tokens,
                              backfill_max=backfill_max, dry_run=dry_run)

    def crystal(self) -> Delta | None:
        return _consolidate.crystal(self)

    def due(self, kind: str) -> bool:
        return _consolidate.due(self, kind)

    def sweep(self) -> dict[str, int]:
        return _io.sweep(self)

    def stats(self) -> dict[str, Any]:
        return _io.stats(self)

    def export(self, path: str | os.PathLike[str], *, include_expired: bool = False, vectors: bool = False) -> int:
        return _io.export(self, path, include_expired=include_expired, vectors=vectors)

    def import_(self, path: str | os.PathLike[str], *, include_expired: bool = False) -> dict[str, int]:
        return _io.import_(self, path, include_expired=include_expired)

    def embed_missing(self, *, batch_size: int = 64, limit: int | None = None) -> int:
        return _io.embed_missing(self, batch_size=batch_size, limit=limit)

    def media_path(self, media_hash: str) -> Path | None:
        return _io.media_path(self, media_hash)

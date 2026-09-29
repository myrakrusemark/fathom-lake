"""Vector blobs, the per-instance matrix cache, and cosine search (SPEC §3.4, §5.2 step 2)."""

from __future__ import annotations

import importlib
import math
import sqlite3
import struct
from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING, Any

from . import _db

if TYPE_CHECKING:
    from .lake import Lake


def pack(vec: Sequence[float]) -> bytes:
    """§3.4: struct.pack(f"<{dim}f", *vec)."""
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    """§3.4: struct.unpack(f"<{n}f", blob) with n = len(blob) // 4."""
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def normalise(vec: Sequence[float]) -> list[float] | None:
    """L2-normalised copy, or None when the norm is 0 (§3.4: never stored)."""
    norm = math.sqrt(sum(x * x for x in vec))
    return None if norm == 0.0 else [x / norm for x in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two raw vectors (the crystal's drift, §6.3); 0.0 when either has zero norm."""
    na, nb = normalise(a), normalise(b)
    return 0.0 if na is None or nb is None else sum(x * y for x, y in zip(na, nb, strict=True))


def store_vectors(conn: sqlite3.Connection, items: Sequence[tuple[str, Sequence[float]]]) -> tuple[list[str], list[str]]:
    """Inside the caller's BEGIN IMMEDIATE: check/set meta.embed_dim, insert the usable vectors
    (normalised), bump vectors_gen once when any was inserted. Returns (stored ids, failed ids);
    failed = wrong length or zero norm. Caller decides: EmbedError (write) or embed_failed (embed_missing)."""
    dim_text = _db.meta_get(conn, "embed_dim")
    dim = None if dim_text is None else int(dim_text)
    stored: list[str] = []
    failed: list[str] = []
    for delta_id, vec in items:
        unit = normalise(vec) if dim is None or len(vec) == dim else None
        if unit is None:
            failed.append(delta_id)
            continue
        if dim is None:
            dim = len(unit)
            _db.meta_set(conn, "embed_dim", str(dim))
        conn.execute("INSERT OR REPLACE INTO vectors(delta_id, dim, vec) VALUES (?, ?, ?)", (delta_id, dim, pack(unit)))
        stored.append(delta_id)
    if stored:
        _db.bump_vectors_gen(conn)
    return stored, failed


def top_k(pairs: Sequence[tuple[str, float]], k: int) -> list[tuple[str, float]]:
    """The k pairs with the highest cos > 0, ties by id (so two processes agree on the pool's edge)."""
    kept = [p for p in pairs if p[1] > 0]
    kept.sort(key=lambda p: (-p[1], p[0]))
    return kept[:k]


def _np() -> Any:
    """numpy when importable, else None (imported here only, §5.2; via importlib so mypy under the
    3.11 target never parses numpy's 3.12-syntax stubs)."""
    try:
        return importlib.import_module("numpy")
    except ImportError:
        return None


class VectorCache:
    """In-memory matrix keyed on meta.vectors_gen (§3.4, §5.2). One per Lake instance; created lazily.
    The §3.4 sidecar file is not implemented in v0.1: a fresh process pays the load."""

    def __init__(self) -> None:
        self.gen = -1
        self.ids: list[str] = []
        self.index: dict[str, int] = {}
        self.matrix_data: Any = None  # numpy ndarray (n, dim) float32 when numpy imports, else list[list[float]]

    def matrix(self, lake: Lake) -> tuple[list[str], Any]:
        """(ids, matrix) for every stored vector, reloaded when vectors_gen moved."""
        gen = int(_db.meta_get(lake.conn, "vectors_gen") or 0)
        if gen != self.gen:
            rows = lake.conn.execute("SELECT delta_id, vec FROM vectors ORDER BY rowid").fetchall()
            self.ids = [r[0] for r in rows]
            self.index = {i: n for n, i in enumerate(self.ids)}
            blobs = [bytes(r[1]) for r in rows]
            np = _np()
            if np is None:
                self.matrix_data = [unpack(b) for b in blobs]
            else:
                dim = len(blobs[0]) // 4 if blobs else 0
                self.matrix_data = np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(blobs), dim)
            self.gen = gen
        return self.ids, self.matrix_data

    def _rows(self, lake: Lake, ids: Collection[str] | None) -> list[int]:
        self.matrix(lake)
        idx = [self.index[i] for i in dict.fromkeys(ids) if i in self.index] if ids is not None else []
        return list(range(len(self.ids))) if ids is None or len(idx) == len(self.ids) else idx

    def cosines(self, lake: Lake, q: Sequence[float], ids: Collection[str] | None) -> list[tuple[str, float]]:
        """(id, cos) of the normalised query against every stored vector of `ids` (None: all), unsorted."""
        idx = self._rows(lake, ids)
        if not idx:
            return []
        np = _np()
        if np is None:
            cos = [sum(a * b for a, b in zip(self.matrix_data[i], q, strict=True)) for i in idx]
        else:
            sub = self.matrix_data if len(idx) == len(self.ids) else self.matrix_data[idx]  # no copy when unfiltered
            cos = (sub @ np.asarray(q, dtype="<f4")).tolist()
        return [(self.ids[i], float(c)) for i, c in zip(idx, cos, strict=True)]

    def cosine_top(self, lake: Lake, q: Sequence[float], ids: Collection[str] | None, k: int) -> list[tuple[str, float]]:
        """Dot the normalised query against the vectors of `ids` (None: all), keep cos > 0, top k by cos
        (ties by id, so two processes agree on the pool's edge)."""
        return top_k(self.cosines(lake, q, ids), k)

    def centroid(self, lake: Lake, ids: Sequence[str]) -> list[float] | None:
        """Normalised mean of the stored vectors of `ids` (bridge/chain, §5.5); None when none has one."""
        idx = self._rows(lake, ids)
        if not idx:
            return None
        np = _np()
        if np is None:
            dim = len(self.matrix_data[idx[0]])
            mean = [sum(self.matrix_data[i][j] for i in idx) / len(idx) for j in range(dim)]
        else:
            mean = self.matrix_data[idx].mean(axis=0).tolist()
        return normalise(mean)

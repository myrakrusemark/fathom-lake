"""`lake serve` (SPEC §13.2–§13.5): the stdlib HTTP host — one Lake per request, JSON wire, NDJSON streams."""

from __future__ import annotations

import argparse
import dataclasses
import hmac
import json
import os
import signal
import socket
import sys
import tempfile
import threading
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TextIO, cast

from . import _digest, _io, _time, adapters
from ._env import resolve, setting
from ._types import ConsolidateError, Delta, LakeError, LeaseHeld, NotFoundError, PlanResult
from .cli import host_config, old_flags, warn
from .lake import Lake

LOOPBACK = ("127.0.0.1", "::1", "localhost")  # §13.2 rule 3: deliberately narrow, so 127.0.0.2 refuses
NO_THINK_MSG = (
    "consolidate needs a server-side think: start lake serve with --think or set LAKE_THINK in ~/.lake/env on the server"
)
WRITE_KEYS = ("content", "source", "tags", "kind", "derived_from", "expires", "media", "meta", "timestamp", "dedupe", "embed")
ENGAGE_KEYS = ("delta_id", "kind", "by", "note", "tags", "snapshot", "dedupe")
RECALL_KEYS = ("query", "source", "tags", "any_tags", "exclude_tags", "kind", "since", "until", "limit",
               "exclude_sources", "include_expired", "noise", "recency", "min_relevance", "plan", "filters",
               "sediment")
CONTEXT_KEYS = ("query", "budget", "limit", "source", "exclude_sources", "tags", "any_tags", "exclude_tags", "kind",
                "since", "until", "crystal", "containers", "noise", "recency", "min_relevance", "labels")
SYSTEM_PROMPT_KEYS = ("moods", "budget", "labels")
DIGEST_KEYS = ("since", "max_units", "max_tokens", "backfill_max", "dry_run")
RETRY = 300.0  # seconds before the --digest thread retries a failed check or a held lease


# --- startup (§13.2, §13.8) ------------------------------------------------------------------------


def _sigexit(signum: int, frame: object) -> None:
    raise SystemExit(0)


def run(a: argparse.Namespace) -> int:
    """§13.2 startup, in order: file, token, the loopback rule, one validating open, bind, announce, serve.
    Everything the environment says is read here, once (§13.8)."""
    try:
        t = resolve(a.lake or a.file)
    except ValueError as exc:
        print(f"lake: {exc} (--lake PATH)", file=sys.stderr)
        return 2
    if t.remote:
        print(f"lake serve hosts a file; the lake resolves to the URL {t.target} (pass --lake PATH)", file=sys.stderr)
        return 2
    warn([*old_flags(a), *t.notes])
    try:
        sched = schedule(a.digest)
    except ValueError as exc:
        print(f"lake: {exc}", file=sys.stderr)
        return 2
    file, token = t.target, setting("LAKE_TOKEN")
    token_path = Path("~/.lake/token").expanduser()
    if token is None and token_path.is_file():
        if token_path.stat().st_mode & 0o077:
            print("lake serve: ~/.lake/token is readable by others — chmod 600 ~/.lake/token", file=sys.stderr)
            return 1
        token = token_path.read_text(encoding="utf-8").strip()
    if a.bind not in LOOPBACK and not token:
        print(f"lake serve: refusing to bind {a.bind} without a token — set LAKE_TOKEN or write ~/.lake/token (0600)",
              file=sys.stderr)
        return 1
    c = host_config(a, digest=sched is not None)  # §13.8 the one host rule; the default model only with --digest
    think_spec, embed_spec = c.think, c.embed
    try:
        try:
            think = None if think_spec is None else adapters.make_think(
                think_spec, timeout=a.think_timeout, num_ctx=a.num_ctx, num_predict=a.num_predict)
        except adapters.CmdClaudeError as exc:  # §13.2: a refused think costs consolidation, never the server
            print(f"lake serve: {exc}; serving with no think callback (consolidate fails)", file=sys.stderr)
            think = think_spec = None
        embed = None if embed_spec is None else adapters.make_embed(embed_spec, timeout=a.embed_timeout)
    except ValueError as exc:
        print(f"lake: {exc}", file=sys.stderr)
        return 2
    try:
        Lake(file).close()  # §13.2 step 4: create or validate the file once; a SchemaError is fatal
        server = LakeServer(
            (a.bind, a.port), file=file, token=token or None, think=think, embed=embed,
            model_name=a.model_name or (adapters.model_name(think_spec) if think_spec else None),
            automation=c.automation,
        )
    except (LakeError, OSError) as exc:
        print(f"lake: {exc}", file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, _sigexit)  # §13.2 step 5: SIGINT and SIGTERM exit 0
    port = server.server_address[1]
    print(f"lake serve: serving {file} on http://{a.bind}:{port}", file=sys.stderr, flush=True)
    if sched is not None:  # §13.2 --digest: one daemon thread, the server's own think; the requests never see it
        opts = {k: v for k in ("backfill_max", "max_units", "max_tokens") if (v := getattr(a, k)) is not None}
        threading.Thread(target=digest_loop, args=(server, sched, opts), kwargs={"wait": threading.Event().wait},
                         daemon=True).start()
        when = f"daily at {sched[0]:02d}:{sched[1]:02d} local time" if isinstance(sched, tuple) else (
            f"every {_time.render_duration(sched)}")
        print(f"lake serve: digest {when}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# --- the server ------------------------------------------------------------------------------------


class LakeServer(ThreadingHTTPServer):
    """§13.2: the host's fixed configuration lives on the instance; every request opens its own Lake."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, file: str, token: str | None, **options: Any) -> None:
        """`options`: the Lake options (think, embed, model_name, automation, clock) every request and the digest
        thread open the file with: the server's one configuration."""
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        self.file, self.token, self.options, self.think = file, token, options, options.get("think")
        super().__init__(address, Handler)

    def open(self, *, readonly: bool) -> Lake:
        return Lake(self.file, readonly=readonly, **self.options)


# --- the --digest schedule (§13.2) -----------------------------------------------------------------


def schedule(text: str) -> float | tuple[int, int] | None:
    """--digest: off (None), nightly (01:00), HH:MM daily in the server's local time, or every:<duration> (seconds)."""
    try:
        if text == "off" or not text.startswith("every:"):
            at = None if text == "off" else datetime.strptime("01:00" if text == "nightly" else text, "%H:%M")
            return None if at is None else (at.hour, at.minute)
        if (secs := _time.parse_duration(text[6:])) > 0:
            return secs
    except ValueError:
        pass
    raise ValueError(f"--digest takes off, nightly, HH:MM or every:<duration>, not {text!r}")


def wait_seconds(sched: float | tuple[int, int], now: datetime, last: datetime | None) -> float:
    """Seconds until the next slot, or 0 when the last digest is older than the most recent slot (a missed run,
    the old timer's Persistent=true, runs at once)."""
    if isinstance(sched, tuple):
        local = now.astimezone()
        slot = local.replace(hour=sched[0], minute=sched[1], second=0, microsecond=0)
        slot -= timedelta(days=1) if slot > local else timedelta(0)
        return 0.0 if last is None or last < slot else (slot + timedelta(days=1) - local).total_seconds()
    return 0.0 if last is None else max(0.0, sched - (now - last).total_seconds())


def digest_loop(srv: LakeServer, sched: float | tuple[int, int], opts: Mapping[str, Any], *,
                wait: Callable[[float], bool], log: TextIO = sys.stderr) -> None:
    """The --digest thread: digest when a slot is owed, log one line, wait for the next slot; `wait` returns True
    to stop. Every exception is logged and the loop goes on: a failed digest waits for the next slot like a
    finished one; a failed check (the file locked or missing) or a lease another digest holds (or a killed one
    left, up to 1 h) is retried after RETRY seconds, so the slot is not given up."""
    tried: datetime | None = None
    while True:
        prev, pause = tried, RETRY
        try:
            with srv.open(readonly=True) as lake:
                now, at = lake.now(), (_digest.state(lake, "digest_last") or {}).get("at")
            last = max([t for t in (tried, at and _time.ts_to_dt(at)) if t], default=None)
            if (pause := wait_seconds(sched, now, last)) == 0:
                tried = now
                with srv.open(readonly=False) as lake:
                    done = lake.digest(**opts)
                print(f"lake serve: digest done — {_digest.summary(done)}", file=log, flush=True)
        except Exception as exc:  # the HTTP server never sees a digest's failure
            print(f"lake serve: digest failed: {type(exc).__name__}: {exc}", file=log, flush=True)
            tried, pause = (prev, RETRY) if isinstance(exc, LeaseHeld) or tried is prev else (tried, 0.0)
        if pause > 0 and wait(pause):
            return


# --- serialisation and decoding helpers (§13.1) ----------------------------------------------------


def wire(x: object) -> object:
    """§13.1 shapes: dataclasses field by field, a Delta in the §4.8 export key order, PlanResult steps
    as [id, result] pairs in plan order; tuples become arrays."""
    if x is None or isinstance(x, (str, int, float, bool)):
        return x
    if isinstance(x, Delta):
        obj = json.loads(_io.export_line(x))
        if x.refuted_by:  # derived receipts ride after `meta`; export_line itself never carries them
            obj["refuted_by"] = [wire(r) for r in x.refuted_by]
        if x.rests_on_refuted:
            obj["rests_on_refuted"] = list(x.rests_on_refuted)
        if x.superseded_by:
            obj["superseded_by"] = [wire(r) for r in x.superseded_by]
        return obj
    if isinstance(x, PlanResult):
        return {"steps": [[sid, wire(sr)] for sid, sr in x.steps.items()],
                "warnings": list(x.warnings), "timing_ms": x.timing_ms, "sediment": wire(x.sediment)}
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return {f.name: wire(getattr(x, f.name)) for f in dataclasses.fields(x)}
    if isinstance(x, Mapping):
        return {str(k): wire(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [wire(v) for v in x]
    return x


def status_of(exc: BaseException) -> int:
    """§13.4: 404 NotFoundError, 409 ConsolidateError, 400 the ValueError family, 500 everything else."""
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, ConsolidateError):
        return 409
    return 400 if isinstance(exc, ValueError) else 500


def fields(body: Mapping[str, Any], allowed: Sequence[str], required: Sequence[str] = ()) -> dict[str, Any]:
    """§13.1 request bodies: an unknown top-level field refuses, null equals omitted, required must be there."""
    for key in body:
        if key not in allowed:
            raise ValueError(f"unknown field '{key}'")
    out = {k: v for k, v in body.items() if v is not None}
    for key in required:
        if key not in out:
            raise ValueError(f"missing required field '{key}'")
    return out


def check_query(query: Mapping[str, str], allowed: Sequence[str]) -> None:
    for key, value in query.items():
        if key not in allowed:
            raise ValueError(f"bad query parameter {key}={value}")


def qbool(query: Mapping[str, str], key: str, default: bool) -> bool:
    """§13.1 query booleans: 1/true and 0/false; anything else refuses."""
    if key not in query:
        return default
    value = query[key]
    if value in ("1", "true"):
        return True
    if value in ("0", "false"):
        return False
    raise ValueError(f"bad query parameter {key}={value}")


def qint(query: Mapping[str, str], key: str) -> int | None:
    if key not in query:
        return None
    try:
        return int(query[key])
    except ValueError:
        raise ValueError(f"bad query parameter {key}={query[key]}") from None


# --- the request handler ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    """One request: auth, route, the §13.4 envelope on any error. Connections are not kept alive (§13.2)."""

    protocol_version = "HTTP/1.0"
    replied = False

    @property
    def lsrv(self) -> LakeServer:
        return cast(LakeServer, self.server)

    def do_GET(self) -> None:
        self.handle_one("GET")

    def do_POST(self) -> None:
        self.handle_one("POST")

    def handle_one(self, method: str) -> None:
        split = urllib.parse.urlsplit(self.path)
        try:
            query = dict(urllib.parse.parse_qsl(split.query, keep_blank_values=True))
            if (method, split.path) != ("GET", "/v1/health") and not self.authorized():
                self.send_json(401, {"error": "LakeError", "message": "unauthorized"},
                               headers={"WWW-Authenticate": "Bearer"})
                return
            self.route(method, split.path, query)
        except Exception as exc:  # §13.4: every error crosses as the envelope
            if self.replied:
                self.log_error("failed after headers: %s: %s", type(exc).__name__, exc)
                return
            try:
                name = "ConsolidateError" if isinstance(exc, LeaseHeld) else type(exc).__name__  # the §13.4 names
                self.send_json(status_of(exc), {"error": name, "message": str(exc)})
            except OSError:
                pass

    def authorized(self) -> bool:
        """§13.2 auth: Bearer token compared with hmac.compare_digest; no token configured accepts all."""
        token = self.lsrv.token
        if token is None:
            return True
        return hmac.compare_digest(self.headers.get("Authorization") or "", f"Bearer {token}")

    def route(self, method: str, path: str, query: dict[str, str]) -> None:
        seg = path.removeprefix("/v1/").split("/") if path.startswith("/v1/") else [""]
        name, arg = seg[0], (seg[1] if len(seg) == 2 else None)
        if len(seg) > 2:
            name = ""
        if method == "POST" and arg is None:
            if name == "import":
                return self.ep_import(query)
            posts: dict[str, Callable[[], None]] = {
                "write": self.ep_write, "engage": self.ep_engage, "recall": self.ep_recall, "plan": self.ep_plan,
                "context": self.ep_context, "system-prompt": self.ep_system_prompt,
                "consolidate": self.ep_consolidate, "digest": self.ep_digest, "sweep": self.ep_sweep,
                "embed-missing": self.ep_embed_missing,
            }
            if name in posts:
                check_query(query, ())
                return posts[name]()
        if method == "GET" and arg is not None and name in ("get", "lineage", "cited-by", "due"):
            return self.ep_get_one(name, arg, query)
        if method == "GET" and arg is None and name in ("crystal", "stats", "health", "export"):
            if name == "export":
                return self.ep_export(query)
            check_query(query, ())
            with self.open_lake(readonly=True) as lake:
                if name == "crystal":
                    return self.send_json(200, wire(lake.crystal()))
                if name == "stats":
                    return self.send_json(200, lake.stats())
                rows = lake.conn.execute("SELECT count(*) FROM deltas").fetchone()[0]
                return self.send_json(200, {"ok": True, "rows": int(rows)})
        self.send_json(404, {"error": "LakeError", "message": f"unknown endpoint: {method} {path}"})

    # --- plumbing ----------------------------------------------------------------------------------

    def open_lake(self, *, readonly: bool) -> Lake:
        return self.lsrv.open(readonly=readonly)

    def send_json(self, status: int, obj: object, *, headers: Mapping[str, str] | None = None) -> None:
        data = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.replied = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body_bytes(self) -> bytes:
        length = self.headers.get("Content-Length")
        if length is None:
            raise ValueError("Content-Length required")
        n = int(length)  # non-numeric raises ValueError -> 400, as §13.2's "Content-Length required" family
        if n < 0:  # a negative length would make rfile.read(n) block reading to EOF, pinning the thread
            raise ValueError(f"invalid Content-Length: {length}")
        return self.rfile.read(n)

    def body_object(self) -> dict[str, Any]:
        try:
            obj = json.loads(self.body_bytes().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from None
        if not isinstance(obj, dict):
            raise ValueError("invalid JSON body: not a JSON object")
        return obj

    # --- POST endpoints (§13.3) --------------------------------------------------------------------

    def ep_write(self) -> None:
        kw = fields(self.body_object(), WRITE_KEYS, ("content", "source"))
        with self.open_lake(readonly=False) as lake:
            self.send_json(200, wire(lake.write(**kw)))

    def ep_engage(self) -> None:
        kw = fields(self.body_object(), ENGAGE_KEYS, ("delta_id", "kind"))
        with self.open_lake(readonly=False) as lake:
            self.send_json(200, wire(lake.engage(**kw)))

    def ep_recall(self) -> None:
        kw = fields(self.body_object(), RECALL_KEYS)
        with self.open_lake(readonly=self.lsrv.think is None) as lake:  # §13.2: a deep recall may sediment
            hits = lake.recall(**kw)
            self.send_json(200, {"hits": wire(hits), "warnings": list(lake.last_warnings)})

    def ep_plan(self) -> None:
        kw = fields(self.body_object(), ("steps", "filters", "sediment"), ("steps",))
        with self.open_lake(readonly=self.lsrv.think is None) as lake:  # §13.2: a deep recall may sediment
            self.send_json(200, wire(lake.plan(kw["steps"], filters=kw.get("filters"), sediment=kw.get("sediment"))))

    def ep_context(self) -> None:
        kw = fields(self.body_object(), CONTEXT_KEYS)
        with self.open_lake(readonly=True) as lake:
            self.send_json(200, wire(lake.context_blocks(kw.pop("query", None), **kw)))

    def ep_system_prompt(self) -> None:
        kw = fields(self.body_object(), SYSTEM_PROMPT_KEYS)
        with self.open_lake(readonly=True) as lake:
            self.send_json(200, {"prompt": lake.system_prompt(**kw)})

    def ep_consolidate(self) -> None:
        if self.lsrv.think is None:  # §13.2: answered before opening the Lake, message verbatim
            self.send_json(409, {"error": "ConsolidateError", "message": NO_THINK_MSG})
            return
        kw = fields(self.body_object(), ("kind", "window", "opts"), ("kind",))
        window = kw.get("window")
        if isinstance(window, list):
            if len(window) != 2:
                raise ValueError("window must be a duration string or [start, end]")
            window = (window[0], window[1])
        opts = kw.get("opts") or {}
        if not isinstance(opts, dict):
            raise ValueError("opts must be an object")
        with self.open_lake(readonly=False) as lake:
            delta = lake.consolidate(kw["kind"], window, **opts)
            self.send_json(200, {"delta": wire(delta), "run": wire(lake.last_run)})

    def ep_digest(self) -> None:
        kw = fields(self.body_object(), DIGEST_KEYS)
        if self.lsrv.think is None and not kw.get("dry_run"):  # §13.2: as /v1/consolidate
            self.send_json(409, {"error": "ConsolidateError", "message": NO_THINK_MSG})
            return
        with self.open_lake(readonly=bool(kw.get("dry_run"))) as lake:
            self.send_json(200, {"run": wire(lake.digest(**kw))})

    def ep_sweep(self) -> None:
        fields(self.body_object(), ())
        with self.open_lake(readonly=False) as lake:
            self.send_json(200, lake.sweep())

    def ep_embed_missing(self) -> None:
        kw = fields(self.body_object(), ("batch_size", "limit"))
        with self.open_lake(readonly=False) as lake:
            self.send_json(200, {"stored": lake.embed_missing(**kw)})

    def ep_import(self, query: dict[str, str]) -> None:
        """§13.5: the NDJSON body through the §4.8 per-line path; include_expired is a query parameter."""
        check_query(query, ("include_expired",))
        include_expired = qbool(query, "include_expired", False)
        raw = self.body_bytes()
        fd, tmp = tempfile.mkstemp(prefix="lake-import-", suffix=".jsonl")
        os.close(fd)
        try:
            Path(tmp).write_bytes(raw)
            with self.open_lake(readonly=False) as lake:
                self.send_json(200, lake.import_(tmp, include_expired=include_expired))
        finally:
            os.unlink(tmp)

    # --- GET endpoints (§13.3) ---------------------------------------------------------------------

    def ep_get_one(self, name: str, arg: str, query: dict[str, str]) -> None:
        if name == "due":
            check_query(query, ())
            with self.open_lake(readonly=True) as lake:
                return self.send_json(200, {"due": lake.due(arg)})
        if name == "get":
            check_query(query, ("include_expired",))
            with self.open_lake(readonly=True) as lake:
                return self.send_json(200, wire(lake.get(arg, include_expired=qbool(query, "include_expired", False))))
        check_query(query, ("depth", "include_expired"))
        depth = qint(query, "depth")
        with self.open_lake(readonly=True) as lake:
            if name == "lineage":
                lin = lake.lineage(arg, depth=depth, include_expired=qbool(query, "include_expired", True))
                return self.send_json(200, wire(lin))
            rows = lake.cited_by(arg, depth=1 if depth is None else depth,
                                 include_expired=qbool(query, "include_expired", False))
            self.send_json(200, {"rows": wire(rows)})

    def ep_export(self, query: dict[str, str]) -> None:
        """§13.5: a body byte for byte what export() writes, streamed from a temporary file."""
        check_query(query, ("include_expired", "vectors"))
        include_expired, vectors = qbool(query, "include_expired", False), qbool(query, "vectors", False)
        fd, tmp = tempfile.mkstemp(prefix="lake-export-", suffix=".jsonl")
        os.close(fd)
        try:
            with self.open_lake(readonly=True) as lake:
                lake.export(tmp, include_expired=include_expired, vectors=vectors)
            self.replied = True
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(os.path.getsize(tmp)))
            self.end_headers()
            with open(tmp, "rb") as fh:
                while chunk := fh.read(65536):
                    self.wfile.write(chunk)
        finally:
            os.unlink(tmp)

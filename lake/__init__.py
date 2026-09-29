"""lake — a self-provenance-generating, closed-loop memory for LLM agents (SPEC.md)."""

import os
import pathlib
from typing import Any

from ._types import (
    Bucket, ClosedLoopError, CollapsedRun, ConsolidateError, ConsolidateRun, ContextResult, Delta, DigestRun, DigestStep,
    Duration, EmbedError, Engagement, Hit, LakeError, LeaseHeld, Lineage, NoiseRules, NotFoundError, PlanError, PlanResult,
    Refutation, SchemaError, StepResult, Supersession, Timeline, TimelineRow, TimeSpec,
)
from . import _env, adapters
from ._answer import parse_answer
from ._env import Config, Target, config, resolve, setting
from .lake import Lake
from .remote import RemoteLake

__version__ = "0.1.0"


def open(target: "str | os.PathLike[str] | None" = None, *, token: str | None = None, think: Any = None,
         embed: Any = None, default: "str | os.PathLike[str] | None" = None, readonly: bool = False,
         **kwargs: Any) -> "Lake | RemoteLake":
    """§13.6 factory: an http(s):// target opens a RemoteLake, anything else a Lake. With a target nothing is
    read from the environment; with none, `resolve(default=)` names the lake and, on a local file, `config()`
    fills what was not passed (LAKE_THINK, LAKE_EMBED, LAKE_AUTOMATION; §13.8). think/embed: a callable, a spec
    string, or False for none. A writable local open creates the parent directory."""
    auto, where = target is None, target
    if where is None:
        try:
            t = _env.resolve(token=token, default=default)
        except ValueError as exc:
            raise LakeError(str(exc)) from None
        where, token = t.target, t.token
    if isinstance(where, str) and _env.is_url(where):
        if think or embed:
            raise LakeError(f"{'think' if think else 'embed'}= is not available over HTTP")
        return RemoteLake(where, token=token, **kwargs)
    if auto:  # §13.8 the one host rule: what was not passed comes from LAKE_THINK, LAKE_EMBED, LAKE_AUTOMATION
        c = _env.config(automation=kwargs.pop("automation", None))
        think, embed, kwargs["automation"] = c.think if think is None else think, c.embed if embed is None else embed, c.automation
    if isinstance(think, str):
        kwargs.setdefault("model_name", adapters.model_name(think))
        think = adapters.make_think(think)
    if isinstance(embed, str):
        embed = adapters.make_embed(embed)
    if not readonly:
        pathlib.Path(where).parent.mkdir(parents=True, exist_ok=True)
    return Lake(where, think=think or None, embed=embed or None, readonly=readonly, **kwargs)


__all__ = (
    "Lake", "RemoteLake", "open", "resolve", "setting", "config", "Config", "Target", "parse_answer", "Delta", "Hit", "Engagement",
    "Refutation", "Supersession", "Bucket", "CollapsedRun", "TimelineRow", "Timeline", "StepResult", "PlanResult",
    "ContextResult", "Lineage", "ConsolidateRun", "DigestRun", "DigestStep", "NoiseRules", "Duration", "TimeSpec",
    "LakeError", "ClosedLoopError", "NotFoundError", "PlanError", "ConsolidateError", "LeaseHeld", "EmbedError", "SchemaError",
    "__version__",
)

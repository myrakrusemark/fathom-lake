"""Public dataclasses, errors, and aliases (SPEC §4.1, §4.2, §3.7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, TypeAlias

Duration: TypeAlias = timedelta | str
TimeSpec: TypeAlias = datetime | str

DEFAULT_NOISE_PHRASES: tuple[str, ...] = (  # the 55 N3 strings of §5.4 ("sure, go" is one entry)
    "hey", "ok", "okay", "yeah", "yep", "yes", "no", "nope", "sure", "wait", "stop", "huh",
    "hmm", "uh", "um", "lol", "haha", "what", "what's up", "hi", "hello", "hello!", "howdy",
    "hola", "thanks", "ty", "nvm", "nevermind", "actually", "test", "testing", "please", "gold",
    "confirmed", "approved", "got it", "agreed", "exactly", "perfect", "great", "cool", "done",
    "noted", "go ahead", "sure go", "sure, go", "do it", "do it again", "lgtm", "looks good",
    "sounds good", "ship it", "merge it", "that's not what i wanted", "no not that",
)

DEFAULT_LABELS: MappingProxyType[str, str] = MappingProxyType({  # §5.6.3; Lake(labels=...) overrides per key
    "crystal_header": "Identity crystal (crystallized {ts}):",
    "remember": "--- You remember {n} things ---",
    "query": 'your query "{q}" returned',
    "fts_only": "(keyword-only recall: the embedder was busy or down, so these memories may be a bit less accurate)",
    "containers": "  ── containers active in this recall ──",
    "more_containers": "  … ({n} more container{s})",
    "surrounding": "  ── surrounding context ──",
    "led_to": "  …which led to…",
    "more_strips": "  … ({n} more strip{s} not shown — budget cap)",
    "user_role": " user:",
    "assistant_role": " assistant:",
    "refuted": " ⟵ refuted ×{n} ({detail})",
    "rests_on_refuted": " ⟵ rests on ×{n} corrected",
    "superseded": " ⟵ superseded by {id}: {value}",
    "mood_header": "Recent moods:",  # §5.7 system_prompt() block 2
    "mood_when": "({ts})",
    "crystal_core": "What I hold to",  # §6.3 items crystal: section headers and the "What changed" entries
    "crystal_tension": "Where I'm pulled two ways",
    "crystal_open": "What I haven't settled",
    "crystal_changed": "What changed in me lately",
    "crystal_revise": "I used to hold: {old} Now: {new}",
    "crystal_add": "New in me: {new}",
    "crystal_retire": "I no longer hold: {old}",
    "crystal_resolve": "Settled: {old} Now: {new}",
    "crystal_why": " What changed it: {why}",
    "positions_header": "## Positions I hold (how sure I am)",  # §5.7 positions block and §5.6.2 (stances, §6.3.1)
    "position": "- {position} ({label}; since {since})",
    "stance": " · stance, {label}",
})

DEFAULT_DUE_THRESHOLDS: MappingProxyType[str, object] = MappingProxyType({  # §6.4; Lake(due_thresholds=...)
    "container_min_rows": 3, "mood_source_weights": MappingProxyType({}), "mood_user_tag": "user",
    "mood_max_age": "6h", "mood_pressure": 25.0, "crystal_rows": 50, "crystal_max_age": "3d", "crystal_min_age": "20h",
    "crystal_containers": 5,
})


@dataclass(frozen=True)
class Engagement:
    target_id: str
    kind: str  # 'affirm' | 'refute' | 'reply'
    by: str | None
    note: str | None


@dataclass(frozen=True)
class Refutation:
    id: str  # the refuting engagement row's id
    source: str  # who refuted (the refuter row's source / `by`)
    timestamp: str  # stored format, UTC
    note: str | None  # the refuter's note, or None


@dataclass(frozen=True)
class Supersession:
    id: str  # the superseding (newer) row's id
    by: str  # the container row whose meta.supersedes asserts the link (a stance link: the newer stance row)
    old_value: str  # verbatim quote from the superseded row
    new_value: str  # verbatim quote from the superseding row


@dataclass(frozen=True)
class Delta:
    id: str
    timestamp: str  # stored format, UTC
    content: str
    source: str
    kind: str | None
    level: int
    tags: list[str]
    derived_from: list[str]
    expires_at: str | None
    media_hash: str | None
    meta: dict[str, Any] | None
    engagement: Engagement | None  # set only when kind == 'engagement'
    refuted_by: tuple[Refutation, ...] = ()  # live refutations OF this row, newest first; DERIVED,
    # never stored/exported; () unless a read path attached it (§4.5, §5.3)
    rests_on_refuted: tuple[str, ...] = ()  # ids of live-refuted rows this row derives from (transitively);
    # DERIVED, never stored/exported; () unless a read path attached it (§4.5, §5.3)
    superseded_by: tuple[Supersession, ...] = ()  # live supersession links naming this row as `old`, newest
    # superseder first; DERIVED from containers' meta.supersedes, never stored/exported (§4.5, §5.3)


@dataclass(frozen=True)
class Hit:
    delta: Delta
    score: float  # final score, §5.3; higher is better
    relevance: float  # text relevance 0..1 (1.0 when no query)
    recency: float  # recency factor 0.5..1
    valence: float  # engagement multiplier 0.30..1.30 (§5.3)
    matched: str  # 'fts' | 'vector' | 'both' | 'filter' | 'neighbor' | 'timeline' | 'bridge' | 'chain'
    step: str | None  # plan step id, None for recall()


@dataclass(frozen=True)
class Bucket:
    key: str
    count: int
    delta_ids: list[str]


@dataclass(frozen=True)
class CollapsedRun:
    source: str
    count: int
    t_start: str
    t_end: str


@dataclass(frozen=True)
class TimelineRow:
    delta: Delta
    is_anchor: bool


@dataclass(frozen=True)
class Timeline:
    id: str  # 'tl_<i>'
    t_start: str
    t_end: str
    anchor_ids: list[str]  # sorted
    rows: list[TimelineRow | CollapsedRun]  # chronological


@dataclass(frozen=True)
class StepResult:
    hits: list[Hit] | None
    buckets: list[Bucket] | None
    timelines: list[Timeline] | None


@dataclass(frozen=True)
class PlanResult:
    steps: dict[str, StepResult]  # every step, in plan order
    warnings: list[str]
    timing_ms: float  # the §5.5 execution alone; the §6.5 pass is not timed
    sediment: Delta | None = None  # the §6.5 sediment row, or None when the pass did not write


@dataclass(frozen=True)
class ContextResult:  # context_blocks(), §5.6
    crystal: Delta | None
    hits: list[Hit]  # the anchors, recall order
    containers: list[Delta]  # block 4, in render order
    strips: list[Timeline]  # rendered strips, in render order
    omitted_strips: int  # block 6's n
    rendered: str  # what context() returns
    warnings: list[str]


@dataclass(frozen=True)
class Lineage:  # lineage(), §4.5
    rows: list[Delta]  # ancestors, breadth-first, nearest first; the root is not included
    dangling: list[str]  # parent ids that no longer resolve, first-seen order


@dataclass(frozen=True)
class ConsolidateRun:  # Lake.last_run, §6
    kind: str
    written: list[Delta]  # every row the run wrote, in write order
    skipped: int  # units the model answered "skip" for
    warnings: list[str]  # invalid-twice clusters, lease notes
    think_calls: int
    window: tuple[str, str] | None  # the candidate window actually used, stored format


@dataclass(frozen=True)
class DigestStep:  # DigestRun.steps, §6.6
    step: str  # "container" | "catch-up YYYY-MM-DD" | "mood" | "crystal"
    due: bool  # False: nothing due, no call
    run: ConsolidateRun | None  # None when not due, and for a dry run's mood and crystal
    prompt_chars: int  # system + user characters this step sent


@dataclass(frozen=True)
class DigestRun:  # digest(), §6.6
    steps: list[DigestStep]  # in order; one per catch-up day
    days: list[str]  # catch-up days finished by this digest (YYYY-MM-DD, UTC)
    given_up: list[str]  # catch-up days given up by this digest
    errors: list[str]  # "<step>: <ExceptionName>: <message>"; a failed step never stops the next
    warnings: list[str]
    stopped: str | None  # the cap that stopped work: "max_units" | "max_tokens" | None
    think_calls: int
    prompt_chars: int  # every system + user character sent; est. input tokens = prompt_chars / 2.2


@dataclass(frozen=True)
class NoiseRules:  # Lake(noise=...), §5.4
    drop_chars: int = 10  # N2 threshold (strict)
    soft_chars: int = 24  # soft rule threshold (strict)
    phrases: tuple[str, ...] = DEFAULT_NOISE_PHRASES  # N3 list
    exempt_sources: tuple[str, ...] = ()  # rows from these sources skip every rule


class LakeError(Exception):
    """Base of every library error."""


class SchemaError(LakeError, RuntimeError):
    """The file is not a lake, or its schema_version is not "1" (§4.3)."""


class ClosedLoopError(LakeError, ValueError):
    """A host write under a `lake:` source, or a kinded row with no derived_from (§1)."""


class NotFoundError(LakeError, LookupError):
    """A target, parent, or input id that does not resolve."""


class PlanError(LakeError, ValueError):
    """Plan validation failed (§5.5)."""


class ConsolidateError(LakeError, RuntimeError):
    """No `think`, a twice-invalid mood/crystal, or a held lease (§6)."""


class LeaseHeld(ConsolidateError):
    """Another run holds the lease (§6, §6.6): retry once it ends; never a failure of the work itself."""


class EmbedError(LakeError, RuntimeError):
    """An embedding failure inside write()/engage() (.delta set) or embed_missing() (.stored set)."""

    def __init__(self, message: str, *, delta: Delta | None = None, stored: int = 0) -> None:
        super().__init__(message)
        self.delta = delta
        self.stored = stored

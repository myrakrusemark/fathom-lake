# Fathom-lake

Memory for LLM agents that never throws anything away. Everything your agent hears goes into one SQLite file, exactly
as it was said, and the agent builds its understanding of you (and of itself) on top of that record, citing where
every conclusion came from. You bring the model.

```
pip install fathom-lake
```

## How it scores

[LongMemEval](https://github.com/xiaowu0162/LongMemEval) is the standard test for long-term chat memory: 500
questions, each asked after a long history of past conversations, about things like what the user said three weeks
ago, what changed since, and how many times something happened. It comes in two sizes. **S** gives each question
about 50 past sessions (roughly 115,000 tokens). **M** gives each question about 500 sessions (roughly 1.5 million
tokens), which is too much to paste into any prompt.

| test | Fathom-lake | answering model |
|---|---:|---|
| LongMemEval-S, 500 questions | **94.8%** | Claude Opus 5.5 |
| LongMemEval-S, 500 questions | **88.0%** | Claude Haiku 4.5 |
| LongMemEval-M, 500 questions | **85.0%** | Claude Opus 5.5 |

All three are graded by the benchmark's own grading script with its own grader, `gpt-4o-2024-08-06`, unmodified.

For context, here are the LongMemEval-S scores other memory systems report for themselves. Their answering models and
graders differ from ours and from each other, so read this as a neighbourhood, not a ranking:

| system | reported LongMemEval-S | answering model | source |
|---|---:|---|---|
| Mastra Observational Memory | 94.9% | gpt-5-mini | [mastra.ai](https://mastra.ai/research/observational-memory) |
| **Fathom-lake** | **94.8%** | Claude Opus 5.5 | this page |
| Mem0 | 94.4% (73.8% in an [independent rerun](https://www.maximem.ai/blog/state-of-ai-memory-2026-claimed-vs-observed)) | not stated | [mem0.ai](https://mem0.ai/blog/state-of-ai-agent-memory-2026) |
| ByteRover | 92.8% (Gemini grader) | not stated | [byterover.dev](https://www.byterover.dev/blog/benchmark_ai_agent_memory_real_production_byterover_top_market_accuracy_longmemeval) |
| Hindsight | 91.4% | Gemini 3 | [arXiv](https://arxiv.org/abs/2512.12818) |
| Honcho | 90.4% | Claude Haiku 4.5 | [honcho.dev](https://honcho.dev/evals) |
| **Fathom-lake** | **88.0%** | Claude Haiku 4.5 | this page |
| Supermemory | 81.6% | gpt-4o | [supermemory.ai](https://supermemory.ai/research/longmembench/) |
| Zep / Graphiti | 71.2% | gpt-4o | [arXiv](https://arxiv.org/abs/2501.13956) |

What we think matters more than the second decimal:

- **The M test.** We could not find another published full LongMemEval-M result. Most memory systems run a language
  model over every message as it arrives, to pull out facts, and at 1.5 million tokens per question that bill adds up
  fast. Fathom-lake stores messages as they are, so ingesting M costs nothing but disk.
- **No model call when a memory is saved.** Writing is an insert into SQLite. A model runs later, when you ask it to
  digest (on a timer, overnight, or never), and anything saved while no model was reachable is simply digested later.
- **We say which reader we used.** Swapping Opus for the much smaller Haiku, with the same memory and the same
  retrieved context, costs about seven points. Most of the score comes from what the memory finds and how it lays
  it out.

**How these were measured.** One fresh lake per question, holding every turn of every past session as one row. No
digestion and no summaries, retrieval only. The question goes to `context()` with a 48,000-character budget, using
full-text and vector search together (`nomic-embed-text` embeddings). The answering model sees that context, the
question date, and an instruction to answer from memory and say when it does not know. On S it answered 27 of the
30 trick questions (questions whose premise is false) with "I don't know".

## Nothing is ever deleted

Every row the lake holds is a **delta**: something that happened, with who said it, when, and what it was derived
from. Deltas are never edited and never deleted (unless you gave one an expiry date when you wrote it).

So when a fact changes, the old fact stays, and the correction points at it:

```python
old = mem.write("The drift threshold is 0.35.", source="notes")
mem.write("We changed the drift threshold to 0.15.", source="notes")
mem.engage(old.id, "refute", by="robin", note="We chose 0.15, not 0.35.")
```

Recall still finds the old row, now marked `⟵ refuted ×1 (robin · We chose 0.15, not 0.35.)`, so the agent can say
what it used to believe and why that changed. Digestion notices when a newer row replaces an older one and records a
supersession link instead of overwriting anything.

Everything the lake writes about itself (session summaries, moods, its sense of self) is a delta too, and cites the
rows it was made from. `mem.lineage(id)` walks any conclusion back to the words it rests on, and `mem.cited_by(id)`
walks forward to everything built on a row. Because the raw record is complete, the history can always be rebuilt
from it: `lake export` writes every delta as JSONL, and the derived layer (summaries, moods, the self) can be
regenerated from the rows your hosts wrote, with a newer model, while the old versions stay on record.

## It becomes someone

Given a model, the lake keeps a **crystal**: the agent's own first-person account of what it holds to, where it is
pulled two ways, and what it has not settled, with every item citing the memories behind it. Each new crystal is a
set of cited edits to the last one, so the self changes in explicit, sourced steps, and `lake crystal --log` shows
how it grew. Beside it the lake keeps **stances**, positions whose confidence is read from the evidence for and
against them.

We test this on invented worlds (fictional people, projects and opinions, months of conversation each):

- **Continuity.** Across rebuilds, the crystal kept all but 2 of 64 stable items. Regenerating the self from scratch
  each time lost 16 of 50.
- **Holding a position.** Shown material that looks relevant but does not bear on a stance, the agent held its
  position 6 times out of 6 with its memory, and 3 out of 6 without it.
- **Knowing what is open.** The crystal named 14 of 24 unresolved questions, where the earlier free-prose self named 5.
- **Two lives, two selves.** Twin lakes given different histories split on 2 of 4 contested questions, where the
  free-prose self split on none.

## Use it

```python
import lake

mem = lake.open("agent.lake")                  # a file per agent, reader, machine, or friend
# mem = lake.open("https://host:8377", token="...")   # the same API, served from another machine

mem.write("the rope on the sill is for the window, not the door", source="reader", tags=["book:holes"])
prompt_block = mem.context("what is the rope for")    # recalled memory, rendered as prompt text

mem = lake.open("agent.lake", think="claude:--model claude-opus-5-5[1m]")
mem.digest()      # summaries, mood and the crystal, whatever is due; safe to call as often as you like
```

The model is any callable `think(prompt, *, system=None, json=False)` or a spec string: `claude` (runs `claude -p`
on your machine), `ollama:<model>@<url>`, or `cmd:<shell command>`. Embeddings are optional; without them recall is
full-text only.

```
lake --lake agent.lake write "..." --source reader --tag book:holes
lake --lake agent.lake context "what is the rope for"
lake --lake agent.lake digest --think ollama:qwen3.6@http://localhost:11434
lake serve --lake agent.lake --digest nightly     # one lake shared across machines
```

`plugin/` gives Claude Code and Codex a lake: every prompt and reply is written, and relevant memory is recalled into
each new prompt.

- [`GUIDE.md`](GUIDE.md): building an agent with memory, local or networked.
- [`SPEC.md`](SPEC.md): the complete behaviour, signature by signature.
- [`plugin/README.md`](plugin/README.md): the Claude Code and Codex plugin.

Standard library only (SQLite with FTS5); `pip install "fathom-lake[numpy]"` speeds up vector recall on large lakes.
MIT licensed.

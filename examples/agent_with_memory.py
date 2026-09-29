#!/usr/bin/env python3
"""An agent with memory, public API only (GUIDE.md). The environment names the lake, so the same program runs on a
local file or a served one; only LAKE changes:

    LAKE=/tmp/agent.lake LAKE_THINK=claude python3 examples/agent_with_memory.py
    LAKE=http://127.0.0.1:8377 LAKE_TOKEN=... python3 examples/agent_with_memory.py    # the server's think digests
"""
from datetime import UTC, datetime, timedelta

import lake

mem = lake.open()                                     # LAKE: a file path or an http(s):// URL
yesterday = datetime.now(UTC) - timedelta(days=1)     # a closed session: digest turns it into a container
talk = [("Tomatoes go in the south bed, never the north one.", "Noted: tomatoes south."),
        ("The north bed floods every April.", "So nothing that minds wet feet goes north."),
        ("Basil lives in pots by the kitchen door.", "Basil by the door, in pots.")]
for k, (user, reply) in enumerate(talk):
    at = yesterday + timedelta(minutes=2 * k)
    mem.write(user, "garden-agent", tags=["session:day-1", "user"], timestamp=at)
    mem.write(reply, "garden-agent", tags=["session:day-1", "assistant"], timestamp=at + timedelta(minutes=1))

for hit in mem.recall("where do the tomatoes go", limit=1):   # [] when nothing matches (or a server is down)
    print("recall:", hit.delta.content)
    mem.engage(hit.delta.id, "affirm", by="user", note="yes, south")   # evidence for ranking and the next crystal

try:
    run = mem.digest()                                # whatever is due, in order; runs where the file is
    print("digest:", [(s.step, s.due) for s in run.steps], "calls:", run.think_calls, "errors:", run.errors)
except lake.ConsolidateError as exc:                  # no think here (LAKE_THINK unset), or none on the server
    print("digest skipped:", exc)
print("crystal:\n" + (c.content if (c := mem.crystal()) else "(none yet)"))
print("context:\n" + mem.context("what grows in the north bed?", budget=1500))
mem.close()

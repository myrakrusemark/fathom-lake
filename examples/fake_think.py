#!/usr/bin/env python3
"""A deterministic stand-in model for trying examples/ without one: LAKE_THINK="cmd:python3 examples/fake_think.py".

The cmd: contract (SPEC §8): the prompt on stdin, the system prompt in LAKE_SYSTEM, LAKE_JSON=1 for a JSON answer,
the answer on stdout. It answers each consolidation by the prompt's shape and cites the newest row id it was shown.
Never a measurement of anything. Stdlib only.
"""
import json
import os
import re
import sys

prompt, system = sys.stdin.read(), os.environ.get("LAKE_SYSTEM", "")
ids = re.findall(r"\[([0-9a-f]{12})\]", prompt)
if not os.environ.get("LAKE_JSON"):  # the sediment pass (a prose answer)
    print("## Where I am\n\nI keep the garden and what it taught me.\n\n" + "The south bed, the next thing to plant. " * 25)
elif "══ THE SESSION ══" in prompt:
    print(json.dumps({"title": "A talk about the garden", "summary": "Where things grow and why.", "changes": []}))
elif "══ THE STRETCH ══" in prompt:
    print(json.dumps({"kind": "propose", "title": "Garden notes", "summary": "Rows about the garden.", "rationale": "one topic"}))
elif "pairs" in system:
    print(json.dumps({"pairs": []}))
elif '"items"' in system:  # the crystal (cited edits)
    print(json.dumps({"items": [{"op": "add", "section": s, "cite": ids[-1:], "text": f"I plant by the sun ({s} {k}); "
                                 + "tomatoes south, herbs by the door. " * 6} for k, s in enumerate(("core",) * 3 + ("open",))]}))
else:  # mood
    print(json.dumps({"state": "steady", "headline": "A steady day.", "subtext": "The garden moved.",
                      "carrier_wave": "Still here.", "levels": {"focus": 0.5}, "threads": ["the garden"]}))

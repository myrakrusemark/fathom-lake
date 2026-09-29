#!/usr/bin/env python3
"""lake-catchup.py: now `lake digest --since` (SPEC §6.6); this wrapper maps the old flags onto it.

The catch-up state lives in the lake (meta `digest`), not in a JSON file, and a digest also runs the default
container pass, mood and crystal when they are due. The lake is --file, else the CLI's resolution (SPEC §13.8),
else $LAKE_HOME/claude.lake. --state, --until, --attempts and --chars-per-token are ignored: a day is given up
after three failed digests, and tokens are estimated as characters / 2.2.
"""

import argparse
import os
import sys
from pathlib import Path

try:
    import lake.cli
except ImportError:  # run from a checkout: plugin/scripts/ -> the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import lake.cli

VALUES = ("--since", "--file", "--think", "--model-name", "--think-timeout", "--max-tokens", "--max-units")
IGNORED = ("--state", "--until", "--attempts", "--chars-per-token")

p = argparse.ArgumentParser(prog="lake-catchup.py", description=__doc__.split("\n\n")[0])
for flag in (*VALUES, *IGNORED):
    p.add_argument(flag, required=flag == "--since")
for flag in ("--automation", "--collapse-source"):
    p.add_argument(flag, action="append", default=[])
p.add_argument("--dry-run", action="store_true")
p.add_argument("--json", action="store_true")
a = vars(p.parse_args())
for flag in IGNORED:
    if a[flag[2:].replace("-", "_")] is not None:
        print(f"lake-catchup: {flag} is ignored (lake digest keeps its state in the lake)", file=sys.stderr)
home = os.environ.get("LAKE_HOME") or "~/.lake"
argv = ["--lake", a["file"]] if a["file"] else ["--default", os.path.join(home, "claude.lake")]
argv += ["--json"] * a["json"] + [x for r in a["automation"] for x in ("--automation", r)]
argv += [x for s in a["collapse_source"] for x in ("--automation", f"source:{s}")]
argv += [x for f in ("--think", "--model-name", "--think-timeout") if a[f[2:].replace("-", "_")] for x in (f, a[f[2:].replace("-", "_")])]
argv += ["digest", "--since", a["since"], *["--dry-run"] * a["dry_run"]]
argv += [x for f in ("--max-tokens", "--max-units") if a[f[2:].replace("-", "_")] for x in (f, a[f[2:].replace("-", "_")])]
sys.exit(lake.cli.main(argv))

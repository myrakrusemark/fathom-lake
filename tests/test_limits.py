"""Code limits: the line budget, mypy --strict, and no module-level mutable state.

The spec pins these with the suite, so the bar is held by CI and not by a reviewer's eye.
"""

from __future__ import annotations

import ast
import importlib
import logging
import pkgutil
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent / "lake"
CONST_TYPES = (str, bytes, int, float, bool, type(None), tuple, frozenset, re.Pattern, types.MappingProxyType, logging.Logger)


def py_files() -> list[Path]:
    return sorted(PKG.rglob("*.py"))


def test_line_budget() -> None:
    """lake/ stays small enough to read in an afternoon: a hard budget on physical lines, raised only with a
    stated reason."""
    total = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in py_files())
    assert total < 7150, f"lake/ is {total} physical lines; the budget is under 7150"


def test_no_module_level_mutable_state() -> None:
    """Every module-level binding is a constant, __all__/__version__, or a type alias — never a dict/list/set or a class instance."""
    offenders: list[str] = []
    for path in py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names += [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.append(node.target.id)
        mod = importlib.import_module("lake." + path.stem if path.stem != "__init__" else "lake")
        importlib.reload(mod) if path.stem == "__init__" else None
        for name in names:
            if name in ("__all__", "__version__"):
                continue
            obj = getattr(mod, name, None)
            if isinstance(obj, CONST_TYPES):
                continue
            if isinstance(obj, (type, types.UnionType)) or getattr(obj, "__module__", "") == "typing":
                continue  # a type alias or typing special form
            offenders.append(f"{path.name}:{name} = {type(obj).__name__}")
    assert not offenders, "module-level mutable state: " + ", ".join(offenders)


def test_every_module_imports() -> None:
    for info in pkgutil.iter_modules([str(PKG)]):
        importlib.import_module(f"lake.{info.name}")


@pytest.mark.slow
def test_mypy_strict() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "lake"],
        cwd=str(PKG.parent), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

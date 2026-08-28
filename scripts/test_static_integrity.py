#!/usr/bin/env python3
"""Mechanical checks over the harness that no behavioural test can see.

Two real bugs shipped past a fully green suite because they were *edit damage*,
not logic errors, and nothing exercised the damaged path:

  - A text-slice rewrite deleted `_spans_from_markdown` while leaving its call
    site, so every conversation batch without a `spans.json` raised NameError,
    was swallowed as a warning, and retried forever.
  - A script that collapsed `if backend: ... else: ...` branches kept the first
    half and deleted everything after it — including shared tails. `kg_timeline`
    lost the code that formatted and returned its result and silently returned
    `None`, which the tool dispatcher then iterated.

Both are found in under a second by reading the AST. Neither is found by tests,
because a test only covers the path someone remembered to write. This file is
the standing check so the next large mechanical edit cannot land them again.

Usage:
    python scripts/test_static_integrity.py
"""

from __future__ import annotations

import ast
import builtins
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Modules whose call graph is load-bearing for memory. Broad enough to catch
# collateral damage, narrow enough to stay fast and signal-only.
WATCHED = [
    "harness/palace.py",
    "harness/mongo_palace.py",
    "harness/palace_cursor.py",
    "harness/memory_sync.py",
    "harness/consolidation.py",
    "harness/memory.py",
]


def _module_names(tree: ast.Module) -> set[str]:
    """Everything resolvable at module scope: imports, defs, assignments."""
    names = set(dir(builtins)) | {"__name__", "__file__", "__doc__", "__all__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _bound_in(node: ast.AST) -> set[str]:
    """Every name a function binds: args, assignments, imports, comprehensions."""
    bound = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.arg):
            bound.add(sub.arg)
        elif isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            bound.add(sub.id)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(sub.name)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            bound.add(sub.name)
        elif isinstance(sub, ast.Global) or isinstance(sub, ast.Nonlocal):
            bound.update(sub.names)
    return bound


class UndefinedNameTests(unittest.TestCase):
    """A call site whose target was deleted must fail here, not in production."""

    def test_no_module_references_a_name_that_does_not_exist(self):
        offenders = []
        for relative in WATCHED:
            path = ROOT / relative
            tree = ast.parse(path.read_text(encoding="utf-8"))
            module_names = _module_names(tree)
            functions = [
                n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            for function in functions:
                # Enclosing scopes: this function plus any function containing it.
                visible = set(module_names) | _bound_in(function)
                for outer in functions:
                    if outer is function:
                        continue
                    if outer.lineno <= function.lineno and function.end_lineno <= (outer.end_lineno or 0):
                        visible |= _bound_in(outer)
                for sub in ast.walk(function):
                    if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                        if sub.id not in visible:
                            offenders.append(f"{relative}:{sub.lineno} {function.name}() -> {sub.id}")
        self.assertEqual(offenders, [], "undefined name(s):\n  " + "\n  ".join(offenders))


class ReturnPathTests(unittest.TestCase):
    """A function that returns a value must not be able to fall off the end."""

    @staticmethod
    def _terminates(node: ast.AST) -> bool:
        if isinstance(node, (ast.Return, ast.Raise)):
            return True
        if isinstance(node, ast.Try):
            bodies = [node.body] + [h.body for h in node.handlers]
            if node.orelse:
                bodies.append(node.orelse)
            if node.finalbody and ReturnPathTests._terminates(node.finalbody[-1]):
                return True
            return all(b and ReturnPathTests._terminates(b[-1]) for b in bodies)
        if isinstance(node, ast.If) and node.orelse:
            return (
                bool(node.body) and ReturnPathTests._terminates(node.body[-1])
                and ReturnPathTests._terminates(node.orelse[-1])
            )
        if isinstance(node, ast.With):
            return bool(node.body) and ReturnPathTests._terminates(node.body[-1])
        return False

    def test_value_returning_functions_always_terminate(self):
        offenders = []
        for relative in WATCHED:
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                returns_value = any(
                    isinstance(r, ast.Return) and r.value is not None
                    for r in ast.walk(node)
                )
                if not returns_value or not node.body:
                    continue
                if not self._terminates(node.body[-1]):
                    offenders.append(f"{relative}:{node.lineno} {node.name}()")
        self.assertEqual(
            offenders, [],
            "function(s) return a value on some paths but can fall off the end "
            "(implicitly returning None):\n  " + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

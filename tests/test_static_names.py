"""Every name a module reads must be bound somewhere in that module.

0.2.4 shipped with `CONF_ALERT_STREAM_SUPPORTED` used in device.py but no
longer imported there — a NameError the moment a real panel was set up, and
not one test executed that line. This check is deliberately crude (module-wide,
not scope-accurate): it exists to catch a missing import or constant, which is
exactly that class of bug, without needing Home Assistant or pyflakes.

Run: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import ast
import builtins
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "custom_components" / "bms_intercom"
MODULE_DUNDERS = {"__name__", "__file__", "__doc__", "__package__", "__spec__"}


def _bind_target(node: ast.AST, bound: set[str]) -> None:
    if isinstance(node, ast.Name):
        bound.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            _bind_target(elt, bound)
    elif isinstance(node, ast.Starred):
        _bind_target(node.value, bound)


def bound_names(tree: ast.AST) -> set[str]:
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arguments):
            for arg in [*node.posonlyargs, *node.args, *node.kwonlyargs]:
                bound.add(arg.arg)
            if node.vararg:
                bound.add(node.vararg.arg)
            if node.kwarg:
                bound.add(node.kwarg.arg)
        elif isinstance(node, (ast.Assign,)):
            for target in node.targets:
                _bind_target(target, bound)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            _bind_target(node.target, bound)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            _bind_target(node.target, bound)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            _bind_target(node.optional_vars, bound)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    return bound


def undefined_names(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    known = bound_names(tree) | set(dir(builtins)) | MODULE_DUNDERS
    missing = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id not in known
        ):
            missing.append((node.lineno, node.id))
    return sorted(set(missing))


class TestNoUndefinedNames(unittest.TestCase):
    def test_every_module(self):
        modules = sorted(SRC.glob("*.py"))
        self.assertGreater(len(modules), 10)
        for path in modules:
            with self.subTest(module=path.name):
                self.assertEqual(undefined_names(path), [])

    def test_the_checker_catches_a_missing_import(self):
        """The checker itself must go red on the 0.2.4 bug."""
        tree = ast.parse(
            "from .const import CONF_A\n"
            "def f(entry):\n"
            "    return entry.data.get(CONF_ALERT_STREAM_SUPPORTED, CONF_A)\n"
        )
        known = bound_names(tree) | set(dir(builtins))
        used = {
            n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        self.assertEqual(used - known, {"CONF_ALERT_STREAM_SUPPORTED"})


if __name__ == "__main__":
    unittest.main()

"""Load the integration's pure modules without importing Home Assistant.

`custom_components/bms_intercom/__init__.py` pulls in Home Assistant, which the
logic under test does not need. So we build a stand-in parent package pointing
at the source directory and import the individual modules into it — relative
imports (`from .endpoints import …`) keep working.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "custom_components" / "bms_intercom"
_PKG = "bms_intercom_under_test"


def _ensure_package() -> types.ModuleType:
    pkg = sys.modules.get(_PKG)
    if pkg is None:
        pkg = types.ModuleType(_PKG)
        pkg.__path__ = [str(SRC)]  # type: ignore[attr-defined]
        sys.modules[_PKG] = pkg
    return pkg


def load(name: str):
    """Import `custom_components/bms_intercom/<name>.py` in isolation."""
    _ensure_package()
    full = f"{_PKG}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, SRC / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module

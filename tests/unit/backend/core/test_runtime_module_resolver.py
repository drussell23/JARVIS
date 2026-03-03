"""Tests for canonical backend main module resolution."""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent / "backend"))

from backend.core.runtime_module_resolver import get_main_app, get_main_attr, get_main_module


def _make_backend_main(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = "/tmp/jarvis/backend/main.py"
    module.app = object()
    module.USE_ENHANCED_CONTEXT = True
    return module


def test_get_main_module_canonicalizes_backend_alias(monkeypatch):
    module = _make_backend_main("backend.main")

    monkeypatch.setitem(sys.modules, "backend.main", module)
    monkeypatch.delitem(sys.modules, "main", raising=False)
    monkeypatch.delitem(sys.modules, "__main__", raising=False)

    resolved = get_main_module(strict=True)

    assert resolved is module
    assert sys.modules["main"] is module


def test_get_main_module_promotes_legacy_main_alias(monkeypatch):
    module = _make_backend_main("main")

    monkeypatch.setitem(sys.modules, "main", module)
    monkeypatch.delitem(sys.modules, "backend.main", raising=False)
    monkeypatch.delitem(sys.modules, "__main__", raising=False)

    resolved = get_main_module(strict=True)

    assert resolved is module
    assert sys.modules["backend.main"] is module


def test_get_main_attr_and_app_use_canonical_module(monkeypatch):
    module = _make_backend_main("backend.main")

    monkeypatch.setitem(sys.modules, "backend.main", module)
    monkeypatch.delitem(sys.modules, "main", raising=False)
    monkeypatch.delitem(sys.modules, "__main__", raising=False)

    assert get_main_attr("USE_ENHANCED_CONTEXT", strict=True) is True
    assert get_main_app(strict=True) is module.app

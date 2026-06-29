"""Model-Aware Compaction Routing.

compact_tool_section must not blindly truncate just because J-Prime is serving.
It queries the DEPLOYED model: a small (<=14B) node gets the aggressive cognitive
compaction; a 32B GPU node bypasses it and receives the FULL Claude-level schema.
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.failover_tier as ft
import backend.core.ouroboros.governance.providers as providers
import backend.core.ouroboros.governance.failover_lifecycle as fl


# ---------------------------------------------------------------------------
# Model size parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,b", [
    ("qwen2.5-coder:7b", 7.0),
    ("qwen2.5-coder:32b", 32.0),
    ("qwen2.5-coder:14b-instruct", 14.0),
    ("llama3.1:70b", 70.0),
    ("mystery-model", 0.0),
])
def test_model_param_billions(label, b):
    assert ft.model_param_billions(label) == b


def test_is_small_model_default_threshold():
    assert ft.is_small_model("qwen2.5-coder:7b") is True
    assert ft.is_small_model("qwen2.5-coder:14b") is True
    assert ft.is_small_model("qwen2.5-coder:32b") is False
    assert ft.is_small_model("unknown") is True  # unknown -> compact (conservative)


def test_is_small_model_env_threshold(monkeypatch):
    monkeypatch.setenv("JARVIS_VENOM_COMPACT_MAX_MODEL_B", "8")
    assert ft.is_small_model("qwen2.5-coder:7b") is True
    assert ft.is_small_model("qwen2.5-coder:14b") is False  # now above the 8B line


# ---------------------------------------------------------------------------
# Compaction gate is model-aware
# ---------------------------------------------------------------------------

def _serving_ctrl(model):
    class _C:
        def is_jprime_serving(self):
            return True
        def active_jprime_model(self):
            return model
    return _C()


def test_compacts_for_small_model(monkeypatch):
    monkeypatch.setenv("JARVIS_VENOM_SCHEMA_SIMPLIFY_ENABLED", "true")
    monkeypatch.setattr(fl, "get_failover_controller", lambda: _serving_ctrl("qwen2.5-coder:7b"))
    assert providers._should_compact_for_jprime() is True


def test_bypasses_for_large_gpu_model(monkeypatch):
    """32B node -> NO compaction -> full Claude-level schema."""
    monkeypatch.setenv("JARVIS_VENOM_SCHEMA_SIMPLIFY_ENABLED", "true")
    monkeypatch.setattr(fl, "get_failover_controller", lambda: _serving_ctrl("qwen2.5-coder:32b"))
    assert providers._should_compact_for_jprime() is False


def test_no_compact_when_not_serving(monkeypatch):
    monkeypatch.setenv("JARVIS_VENOM_SCHEMA_SIMPLIFY_ENABLED", "true")
    class _C:
        def is_jprime_serving(self):
            return False
        def active_jprime_model(self):
            return "qwen2.5-coder:7b"
    monkeypatch.setattr(fl, "get_failover_controller", lambda: _C())
    assert providers._should_compact_for_jprime() is False


def test_controller_active_model_defaults_to_survival(monkeypatch):
    """A fresh controller (no tier provisioned) reports the survival model."""
    fl._reset_singleton_for_tests()
    ctrl = fl.FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: True, vm_delete_fn=lambda: True,
        node_ready_fn=lambda e: True, clock_fn=lambda: 1.0,
    )
    assert "7b" in ctrl.active_jprime_model().lower()

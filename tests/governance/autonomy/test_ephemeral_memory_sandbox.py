"""Phase 1b — EphemeralMemorySandbox: isolated, bounded, vaporizable context."""
from __future__ import annotations

import sys

import pytest

from backend.core.ouroboros.governance.autonomy.ephemeral_memory_sandbox import (
    EphemeralMemorySandbox,
    force_gc_enabled,
    sandbox_enabled,
    vaporize_quietly,
)


def _mk(**kw):
    kw.setdefault("worker_id", "w-1")
    kw.setdefault("sub_goal_prompt", "Sub-goal: refactor module X")
    return EphemeralMemorySandbox(**kw)


# --- contents: only sub-goal + local results -------------------------------


def test_seeded_with_subgoal_prompt_only():
    sb = _mk(sub_goal_prompt="MISSION-PROMPT")
    msgs = sb.messages()
    assert len(msgs) == 1
    assert msgs[0]["kind"] == "sub_goal"
    assert msgs[0]["content"] == "MISSION-PROMPT"


def test_append_local_results_only():
    sb = _mk()
    sb.append({"role": "tool", "content": "read_file result"})
    sb.append("raw string tool result")
    msgs = sb.messages()
    assert len(msgs) == 3  # seed + 2
    assert msgs[1]["content"] == "read_file result"
    # raw string is wrapped into a tool turn
    assert msgs[2]["role"] == "tool"
    assert msgs[2]["content"] == "raw string tool result"


def test_no_parent_or_global_message_access():
    """The sandbox has NO attribute/reference to a parent/global conversation.

    The worker reads ONLY sandbox.messages(); there is no back-reference to a
    shared structure to read from.
    """
    sb = _mk()
    # No parent-pointing attributes exist (structural isolation).
    for attr in ("parent", "global_messages", "shared", "commander", "_parent"):
        assert not hasattr(sb, attr)
    # __slots__ guards against accidentally smuggling in a parent ref.
    assert "_parent" not in EphemeralMemorySandbox.__slots__
    # messages() returns a copy — caller cannot reach into the deque.
    out = sb.messages()
    out.append({"role": "intruder"})
    assert len(sb.messages()) == 1


# --- bounded: max_turns + max_tokens eviction ------------------------------


def test_max_turns_evicts_oldest():
    sb = _mk(max_turns=3, sub_goal_prompt="seed")
    sb.append({"content": "a"})
    sb.append({"content": "b"})
    sb.append({"content": "c"})  # evicts seed
    msgs = sb.messages()
    assert len(msgs) == 3
    contents = [m.get("content") for m in msgs]
    assert "seed" not in contents
    assert contents == ["a", "b", "c"]
    assert sb.stats()["turns"] == 3


def test_max_tokens_evicts_oldest():
    # ~4 chars/token. Each 40-char turn ~ 10 tokens; budget 25 holds ~2.
    big = "x" * 40
    sb = _mk(max_turns=100, max_tokens=25, sub_goal_prompt="s" * 40)
    sb.append({"content": big})
    sb.append({"content": big})
    stats = sb.stats()
    assert stats["approx_tokens"] <= 25
    # oldest dropped to fit the token budget; at least one turn always kept
    assert stats["turns"] >= 1
    assert stats["turns"] < 3


def test_never_unbounded():
    sb = _mk(max_turns=5, max_tokens=10_000)
    for i in range(50):
        sb.append({"content": f"turn-{i}"})
    assert sb.stats()["turns"] <= 5


# --- vaporize() -------------------------------------------------------------


def test_vaporize_clears_and_marks():
    sb = _mk()
    sb.append({"content": "scratchpad"})
    assert sb.stats()["turns"] == 2
    info = sb.vaporize(force_gc=False)
    assert info["turns_cleared"] == 2
    assert sb.stats()["turns"] == 0
    assert sb.stats()["approx_tokens"] == 0
    assert sb.vaporized is True
    assert sb.messages() == []


def test_vaporize_idempotent():
    sb = _mk()
    sb.append({"content": "x"})
    first = sb.vaporize(force_gc=False)
    assert first["turns_cleared"] == 2
    second = sb.vaporize(force_gc=False)
    assert second.get("already_vaporized") is True
    assert second["turns_cleared"] == 0
    assert sb.vaporized is True


def test_append_after_vaporize_is_noop():
    sb = _mk()
    sb.vaporize(force_gc=False)
    sb.append({"content": "late"})
    assert sb.stats()["turns"] == 0


def test_force_gc_false_skips_collect_but_still_dels(monkeypatch):
    import backend.core.ouroboros.governance.autonomy.ephemeral_memory_sandbox as mod

    called = {"gc": 0}
    monkeypatch.setattr(mod.gc, "collect", lambda *a, **k: called.__setitem__("gc", called["gc"] + 1) or 0)
    sb = _mk()
    sb.append({"content": "x"})
    sb.vaporize(force_gc=False)
    assert called["gc"] == 0  # collect skipped
    assert sb.stats()["turns"] == 0  # del still happened


def test_force_gc_true_calls_collect(monkeypatch):
    import backend.core.ouroboros.governance.autonomy.ephemeral_memory_sandbox as mod

    called = {"gc": 0}
    monkeypatch.setattr(mod.gc, "collect", lambda *a, **k: called.__setitem__("gc", called["gc"] + 1) or 7)
    sb = _mk()
    info = sb.vaporize(force_gc=True)
    assert called["gc"] == 1
    assert info["collected"] == 7


def test_torch_path_noop_when_torch_absent(monkeypatch):
    # torch not in sys.modules -> the cuda branch is a pure no-op (no import).
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    sb = _mk()
    info = sb.vaporize(force_gc=False)  # must not raise
    assert info["vaporized"] is True
    # torch was not imported as a side effect
    assert "torch" not in sys.modules


def test_torch_path_best_effort_when_torch_present(monkeypatch):
    import types

    fake_cuda = types.SimpleNamespace(
        is_available=lambda: True,
        empty_cache=lambda: fake_cuda.__dict__.__setitem__("flushed", True),
    )
    fake_torch = types.SimpleNamespace(cuda=fake_cuda)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    sb = _mk()
    sb.vaporize(force_gc=False)
    assert fake_cuda.__dict__.get("flushed") is True


# --- gates ------------------------------------------------------------------


def test_sandbox_enabled_default_off(monkeypatch):
    monkeypatch.delenv("JARVIS_SWARM_EPHEMERAL_SANDBOX_ENABLED", raising=False)
    assert sandbox_enabled() is False
    monkeypatch.setenv("JARVIS_SWARM_EPHEMERAL_SANDBOX_ENABLED", "true")
    assert sandbox_enabled() is True


def test_force_gc_default_on(monkeypatch):
    monkeypatch.delenv("JARVIS_SWARM_FORCE_GC", raising=False)
    assert force_gc_enabled() is True
    monkeypatch.setenv("JARVIS_SWARM_FORCE_GC", "false")
    assert force_gc_enabled() is False


def test_vaporize_quietly_none_and_broken():
    # None -> no-op, no raise
    vaporize_quietly(None)

    class _Broken:
        def vaporize(self, **kw):
            raise RuntimeError("boom")

    # broken sandbox -> swallowed (fail-CLOSED, treated as vaporized)
    vaporize_quietly(_Broken())


def test_env_bounds_respected(monkeypatch):
    monkeypatch.setenv("JARVIS_SWARM_SANDBOX_MAX_TURNS", "2")
    sb = EphemeralMemorySandbox(worker_id="w", sub_goal_prompt="s")
    sb.append({"content": "a"})
    sb.append({"content": "b"})
    assert sb.stats()["turns"] == 2
    assert sb.stats()["max_turns"] == 2

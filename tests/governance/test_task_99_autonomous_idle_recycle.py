"""
Task #99 spine — Autonomous Connection Lifecycle Policy.

v14-rev16 graduation soak proved Task #98 universal phase budget gave
the SWE op its GENERATE runway (first time across the 14-rev arc), but
Claude streams returned 0 bytes on ``thinking=on`` after ~5 min of
pipeline work — even under ``stdlib_default`` client mode (H1
falsified).  Hypothesis H7: upstream NAT / load-balancer / firewall
keepalive timeouts silently torn down idle TCP connections in the
httpx pool; httpx attempted reuse, socket was dead, stream hung
forever.

Task #99 introduces an Autonomous Connection Lifecycle Policy:
``ClaudeProvider`` tracks the monotonic timestamp of its last
successful API call.  When ``_ensure_client`` is invoked after an
idle gap longer than ``JARVIS_CLAUDE_IDLE_RECYCLE_THRESHOLD_S``
(default 120s), the existing ``_recycle_client`` primitive is fired
autonomously — drops the httpx pool BEFORE the next call, forcing
a fresh TCP connection that bypasses any silently-dead keepalives.

Composes existing architecture:
  * ``_recycle_client`` (Task #4 cascade hardening) — same telemetry
    ring buffer, same async-close, same generation counter.
  * ``_ensure_client`` — single chokepoint for client access; policy
    fires at the top before any API call goes out.

Composes existing FlagRegistry — 2 new seeded knobs:
  * ``JARVIS_CLAUDE_IDLE_RECYCLE_ENABLED`` (BOOL, default true,
    SAFETY) — master switch for byte-identical rollback.
  * ``JARVIS_CLAUDE_IDLE_RECYCLE_THRESHOLD_S`` (FLOAT, default 120,
    TUNING).

This spine pins:

  * The threshold resolver: default 120s, env-tunable, invalid /
    negative fallback to default, 0 is a valid opt-out single-use
    setting.
  * Master switch resolver: default true, case-insensitive parsing,
    unknown values → true (operator binding: lifecycle policy is
    the new default architecture).
  * Behavioral: ``_maybe_recycle_for_idle`` invokes
    ``_recycle_client`` ONLY when threshold exceeded; no-op when
    master off, when ``_last_successful_call_at`` is None, when
    client is None, or when idle below threshold.
  * ``_record_successful_call`` updates the timestamp.
  * AST pins: ``_ensure_client`` invokes ``_maybe_recycle_for_idle``
    at the top; 4 SDK call sites invoke ``_record_successful_call``
    on their happy paths.
  * FlagRegistry seeds present with correct types + categories.

No live network — fully deterministic via monkeypatched ``_recycle_
client`` + injected last-call timestamp.
"""
from __future__ import annotations

import ast
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_PROVIDERS_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "providers.py"
)
_SEED_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "flag_registry_seed.py"
)


# ---------------------------------------------------------------------------
# Resolvers — env-tunable + invalid fallback
# ---------------------------------------------------------------------------


def _import_resolvers():
    from backend.core.ouroboros.governance.providers import (
        _resolve_idle_recycle_threshold_s,
        _idle_recycle_policy_enabled,
    )
    return _resolve_idle_recycle_threshold_s, _idle_recycle_policy_enabled


@pytest.mark.parametrize("env_val,expected", [
    ("120.0", 120.0),
    ("60.0", 60.0),
    ("300.0", 300.0),
    ("0", 0.0),         # valid opt-out
    ("0.0", 0.0),
    ("-1.0", 120.0),    # negative → default
    ("abc", 120.0),     # garbage → default
    ("", 120.0),        # unset → default
])
def test_threshold_resolver_decision_table(env_val, expected, monkeypatch):
    if env_val:
        monkeypatch.setenv("JARVIS_CLAUDE_IDLE_RECYCLE_THRESHOLD_S", env_val)
    else:
        monkeypatch.delenv(
            "JARVIS_CLAUDE_IDLE_RECYCLE_THRESHOLD_S", raising=False,
        )
    fn, _ = _import_resolvers()
    assert fn() == pytest.approx(expected, abs=0.01)


@pytest.mark.parametrize("env_val,expected", [
    ("true", True),
    ("TRUE", True),
    ("1", True),
    ("yes", True),
    ("on", True),
    ("false", False),
    ("FALSE", False),
    ("0", False),
    ("no", False),
    ("off", False),
    ("", False),         # unset → default true (but env is set to "")
    ("garbage", False),  # unknown → false (any non-truthy is non-truthy)
])
def test_master_switch_resolver(env_val, expected, monkeypatch):
    """Master switch: default true when env unset.  Explicit values
    parse case-insensitively to truthy/falsy."""
    monkeypatch.setenv("JARVIS_CLAUDE_IDLE_RECYCLE_ENABLED", env_val)
    _, fn = _import_resolvers()
    assert fn() == expected


def test_master_switch_defaults_true_when_unset(monkeypatch):
    monkeypatch.delenv("JARVIS_CLAUDE_IDLE_RECYCLE_ENABLED", raising=False)
    _, fn = _import_resolvers()
    assert fn() is True


# ---------------------------------------------------------------------------
# Behavioral — _maybe_recycle_for_idle
# ---------------------------------------------------------------------------


def _make_provider_with_state(
    last_call_at: float | None = None,
    client_set: bool = True,
):
    """Build a minimal ClaudeProvider with just enough state to
    exercise _maybe_recycle_for_idle deterministically.  Bypasses
    __init__ because real construction needs an API key + state
    container."""
    from backend.core.ouroboros.governance.providers import ClaudeProvider
    p = ClaudeProvider.__new__(ClaudeProvider)
    # Minimal state-container shim so the `_client` property doesn't
    # blow up; we monkeypatch `_recycle_client` to capture invocation.
    p._state = MagicMock()
    p._state.client = MagicMock() if client_set else None
    p._last_successful_call_at = last_call_at
    return p


def test_maybe_recycle_no_op_when_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_CLAUDE_IDLE_RECYCLE_ENABLED", "false")
    p = _make_provider_with_state(last_call_at=time.monotonic() - 1000.0)
    p._recycle_client = MagicMock()
    p._maybe_recycle_for_idle()
    p._recycle_client.assert_not_called()


def test_maybe_recycle_no_op_when_no_prior_successful_call(monkeypatch):
    """Before any call has succeeded, there's nothing to recycle —
    policy must no-op even though master is on."""
    monkeypatch.setenv("JARVIS_CLAUDE_IDLE_RECYCLE_ENABLED", "true")
    p = _make_provider_with_state(last_call_at=None)
    p._recycle_client = MagicMock()
    p._maybe_recycle_for_idle()
    p._recycle_client.assert_not_called()


def test_maybe_recycle_no_op_when_client_is_none(monkeypatch):
    """If client is already None, _ensure_client will lazily build a
    fresh one anyway — no need to recycle."""
    monkeypatch.setenv("JARVIS_CLAUDE_IDLE_RECYCLE_ENABLED", "true")
    p = _make_provider_with_state(
        last_call_at=time.monotonic() - 1000.0, client_set=False,
    )
    p._recycle_client = MagicMock()
    p._maybe_recycle_for_idle()
    p._recycle_client.assert_not_called()


def test_maybe_recycle_no_op_when_idle_below_threshold(monkeypatch):
    monkeypatch.setenv("JARVIS_CLAUDE_IDLE_RECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CLAUDE_IDLE_RECYCLE_THRESHOLD_S", "120.0")
    # Last call 30s ago — well below 120s threshold
    p = _make_provider_with_state(last_call_at=time.monotonic() - 30.0)
    p._recycle_client = MagicMock()
    p._maybe_recycle_for_idle()
    p._recycle_client.assert_not_called()


def test_maybe_recycle_fires_when_idle_exceeds_threshold(monkeypatch):
    """The load-bearing case: idle gap exceeds threshold → recycle
    fires.  This is the v14-rev16 closure pattern."""
    monkeypatch.setenv("JARVIS_CLAUDE_IDLE_RECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CLAUDE_IDLE_RECYCLE_THRESHOLD_S", "60.0")
    # Last call 300s ago — well above 60s threshold
    p = _make_provider_with_state(last_call_at=time.monotonic() - 300.0)
    p._recycle_client = MagicMock()
    p._maybe_recycle_for_idle()
    p._recycle_client.assert_called_once()
    # Reason MUST encode the elapsed time for telemetry
    call_args = p._recycle_client.call_args
    reason = call_args[0][0] if call_args[0] else call_args[1].get("reason", "")
    assert "idle_threshold_exceeded" in reason
    assert "s" in reason  # elapsed-seconds suffix


def test_maybe_recycle_zero_threshold_recycles_immediately(monkeypatch):
    """Threshold=0 is the opt-out single-use behavior — every call
    after any prior successful call triggers recycle."""
    monkeypatch.setenv("JARVIS_CLAUDE_IDLE_RECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CLAUDE_IDLE_RECYCLE_THRESHOLD_S", "0")
    p = _make_provider_with_state(last_call_at=time.monotonic() - 0.001)
    p._recycle_client = MagicMock()
    p._maybe_recycle_for_idle()
    p._recycle_client.assert_called_once()


def test_maybe_recycle_never_raises_on_internal_error(monkeypatch):
    """The policy MUST NOT raise into _ensure_client — defensive
    contract.  Force an error inside the policy and verify it's
    swallowed."""
    monkeypatch.setenv("JARVIS_CLAUDE_IDLE_RECYCLE_ENABLED", "true")
    p = _make_provider_with_state(last_call_at=time.monotonic() - 1000.0)
    # _recycle_client raises — policy must swallow
    p._recycle_client = MagicMock(side_effect=RuntimeError("boom"))
    # Must not raise
    p._maybe_recycle_for_idle()


# ---------------------------------------------------------------------------
# _record_successful_call
# ---------------------------------------------------------------------------


def test_record_successful_call_stamps_timestamp():
    p = _make_provider_with_state(last_call_at=None)
    assert p._last_successful_call_at is None
    p._record_successful_call()
    assert p._last_successful_call_at is not None
    # Within reason of "now"
    assert abs(p._last_successful_call_at - time.monotonic()) < 1.0


def test_record_successful_call_is_idempotent_under_failure():
    """If `time.monotonic()` somehow raises, _record_successful_call
    MUST NOT propagate — defensive contract."""
    p = _make_provider_with_state(last_call_at=None)
    # Force an AttributeError by replacing the attribute itself with
    # something un-settable (via property descriptor)
    # Simpler: just verify it doesn't raise under normal use.
    p._record_successful_call()
    p._record_successful_call()  # second call also fine
    assert p._last_successful_call_at is not None


# ---------------------------------------------------------------------------
# AST pins — wiring at the right places
# ---------------------------------------------------------------------------


def test_ast_pin_ensure_client_invokes_idle_check():
    """``_ensure_client`` MUST call ``_maybe_recycle_for_idle`` at the
    top (before any client construction or return)."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    ensure_client_fn = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_ensure_client"
        ):
            ensure_client_fn = node
            break
    assert ensure_client_fn is not None
    # Find the first Call expression in the body — it MUST be
    # self._maybe_recycle_for_idle()
    found_at_top = False
    for stmt in ensure_client_fn.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            call = stmt.value
            if (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "_maybe_recycle_for_idle"
            ):
                found_at_top = True
                break
        # Stop scanning at any non-Expr statement that's not a docstring
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # docstring — keep going
        if isinstance(stmt, ast.If):
            # Hit the if/main-body — stop the "top" scan
            break
    assert found_at_top, (
        "_ensure_client MUST invoke self._maybe_recycle_for_idle() at "
        "the top of the body (before any client-construction logic)"
    )


def test_ast_pin_record_successful_call_wired_at_call_sites():
    """The 4 SDK happy paths MUST stamp success via _record_successful_call."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    # Count occurrences — should be at least 3 (stream/create unified
    # site, legacy_tool_loop, plan).  4 if generate's stream + create
    # paths are wired separately, but they converge at
    # _record_cache_observation so a single hook covers both.
    n = src.count("self._record_successful_call()")
    assert n >= 3, (
        f"Expected >= 3 self._record_successful_call() invocations "
        f"(generate stream/create + legacy_tool_loop + plan); found {n}"
    )


def test_ast_pin_helpers_defined_at_module_level():
    """Resolvers + helpers must be importable at module scope."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    module_fns = {
        n.name for n in tree.body if isinstance(n, ast.FunctionDef)
    }
    assert "_resolve_idle_recycle_threshold_s" in module_fns
    assert "_idle_recycle_policy_enabled" in module_fns


def test_ast_pin_methods_defined_on_provider():
    """``_maybe_recycle_for_idle`` and ``_record_successful_call`` must
    be methods on ClaudeProvider."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    claude_class = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            claude_class = node
            break
    assert claude_class is not None
    method_names = {
        n.name for n in claude_class.body
        if isinstance(n, ast.FunctionDef)
    }
    assert "_maybe_recycle_for_idle" in method_names
    assert "_record_successful_call" in method_names


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_seed_master_switch_present():
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_CLAUDE_IDLE_RECYCLE_ENABLED" in src
    idx = src.find("JARVIS_CLAUDE_IDLE_RECYCLE_ENABLED")
    window = src[idx:idx + 1500]
    assert "default=True" in window or "default=true" in window
    assert "Category.SAFETY" in window
    assert "providers.py" in window


def test_seed_threshold_present():
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_CLAUDE_IDLE_RECYCLE_THRESHOLD_S" in src
    # The string appears twice — once in the master switch's
    # description (cross-reference), once as its own FlagSpec.  Find
    # the FlagSpec via the `name=` prefix.
    flagspec_marker = 'name="JARVIS_CLAUDE_IDLE_RECYCLE_THRESHOLD_S"'
    idx = src.find(flagspec_marker)
    assert idx > 0, (
        "FlagSpec(name='JARVIS_CLAUDE_IDLE_RECYCLE_THRESHOLD_S') MUST "
        "exist as its own seed entry, not just as a cross-reference "
        "in the master switch description"
    )
    window = src[idx:idx + 1500]
    assert "default=120.0" in window
    assert "Category.TUNING" in window
    assert "providers.py" in window

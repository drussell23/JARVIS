"""Regression spine — Venom monitor tool (Ticket #4 Slice 2).

Pins the structural contract for the monitor tool surface:

  1. Policy gate: deny-by-default, allow-on-env + binary-allowlist,
     bad-args DENY matrix.
  2. Handler behavior: happy path, early-exit via pattern, timeout
     enforcement, process-gone clean error, bad regex clean error.
  3. Manifest integrity: registered, read-only capability set, argv
     schema matches the primitive's expectations.
  4. Authority invariant: monitor NOT in _MUTATION_TOOLS; under
     is_read_only scope the scope gate allows it.
  5. Observer invariant: bus publish failure from the primitive does
     NOT break the handler (reuses Slice 1 proof surface).

Tests spawn REAL subprocesses (the current Python interpreter with
-c scripts). No mocking of asyncio.subprocess. The point is proving
the handler + policy + primitive stack works end-to-end.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance.tool_executor import (
    _L1_MANIFESTS,
    GoverningToolPolicy,
    PolicyContext,
    PolicyDecision,
    ToolCall,
    ToolExecStatus,
)
from backend.core.ouroboros.governance.monitor_tool import (
    classify_cmd,
    extract_binary_basename,
    monitor_allowed_binaries,
    monitor_enabled,
    run_monitor_tool,
)
from backend.core.ouroboros.governance.scoped_tool_access import (
    _MUTATION_TOOLS,
)


PYTHON = sys.executable
PYTHON_BASENAME = os.path.basename(PYTHON)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_monitor_env(monkeypatch):
    """Every test gets a clean slate — no cross-test leakage of monitor
    env vars. The monitor tool is deny-by-default, so delenv leaves
    operators seeing the deny path."""
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_TOOL_MONITOR_"):
            monkeypatch.delenv(key, raising=False)
    yield


def _pctx(op_id: str = "op-monitor-test") -> PolicyContext:
    return PolicyContext(
        repo="jarvis",
        repo_root=Path("/tmp"),
        op_id=op_id,
        call_id=f"{op_id}:r0:t0",
        round_index=0,
        risk_tier=None,
        is_read_only=False,
    )


def _call(cmd=None, pattern=None, timeout_s=None, call_id=None) -> ToolCall:
    args: Dict[str, Any] = {}
    if cmd is not None:
        args["cmd"] = cmd
    if pattern is not None:
        args["pattern"] = pattern
    if timeout_s is not None:
        args["timeout_s"] = timeout_s
    return ToolCall(name="monitor", arguments=args)


def _enable_and_allow(monkeypatch, *, binaries: str = "") -> None:
    """Flip the master switch on + set the binary allowlist.

    If ``binaries`` is empty, defaults to allowing PYTHON_BASENAME
    (which is what most tests want — spawning subprocesses via the
    current interpreter)."""
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ENABLED", "true")
    if not binaries:
        binaries = PYTHON_BASENAME
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ALLOWED_BINARIES", binaries)


# ===========================================================================
# 1. Manifest integrity + authority invariant
# ===========================================================================


def test_manifest_registered_with_correct_surface():
    """Slice 2 test 1: manifest is present, read-only capability set,
    argv schema exposes cmd + pattern + timeout_s."""
    assert "monitor" in _L1_MANIFESTS
    m = _L1_MANIFESTS["monitor"]
    # Read-only category — no write capability (§1 Boundary pin).
    assert "write" not in m.capabilities
    assert "subprocess" in m.capabilities
    # Args match the handler contract.
    assert "cmd" in m.arg_schema
    assert "pattern" in m.arg_schema
    assert "timeout_s" in m.arg_schema


def test_monitor_not_in_mutation_tools():
    """Slice 2 test 2 (CRITICAL): monitor must NOT be in _MUTATION_TOOLS.
    Ensures under an is_read_only scope, the ScopedToolGate allows the
    tool instead of blocking it as a mutation."""
    assert "monitor" not in _MUTATION_TOOLS


def test_monitor_allowed_under_read_only_scope():
    """Slice 2 test 3: a read-only scope (ScopedToolGate with
    read_only=True) must still allow monitor. Pins that monitor is
    observability-only and is permitted on read-only ops."""
    from backend.core.ouroboros.governance.scoped_tool_access import (
        ScopedToolGate, ToolScope,
    )
    # Read-only scope, allowlist includes monitor.
    gate = ScopedToolGate(ToolScope(
        read_only=True,
        allowed_tools=frozenset({"read_file", "monitor"}),
    ))
    allowed, _reason = gate.can_use("monitor")
    assert allowed is True


# ===========================================================================
# 2. Policy gate — deny/allow matrix
# ===========================================================================


def test_policy_denies_when_master_switch_explicitly_off(monkeypatch):
    """Slice 2 test 4 (CRITICAL, post-Slice-4): when
    JARVIS_TOOL_MONITOR_ENABLED is explicitly ``"false"`` (operator
    opt-out after Slice 4 graduation), policy DENIES monitor. Proves
    the opt-out path remains intact — operators retain a runtime
    kill switch even after the graduation flip made the default
    ``"true"``."""
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ENABLED", "false")
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ALLOWED_BINARIES", PYTHON_BASENAME)
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(
        _call(cmd=[PYTHON, "-c", "print('hi')"]), _pctx(),
    )
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.monitor_disabled"


def test_policy_denies_master_switch_false_string(monkeypatch):
    """Slice 2 test 5: explicit 'false' also denies. The env parser
    must not succumb to truthy-ish strings."""
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ENABLED", "false")
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ALLOWED_BINARIES", PYTHON_BASENAME)
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(
        _call(cmd=[PYTHON, "-c", "print('hi')"]), _pctx(),
    )
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.monitor_disabled"


def test_policy_denies_binary_not_in_allowlist(monkeypatch):
    """Slice 2 test 6 (CRITICAL): master switch on, but cmd[0] basename
    is not in the allowlist → DENY with binary_not_allowed reason.
    Keeps the tool an observer of authorized binaries, not a generic
    run-anything escape hatch."""
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ENABLED", "true")
    # Explicitly tiny allowlist that DOES NOT include our test binary.
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ALLOWED_BINARIES", "pytest")
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(
        _call(cmd=["/bin/sh", "-c", "rm -rf /"]), _pctx(),
    )
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.monitor_binary_not_allowed"


def test_policy_denies_bad_args_non_list_cmd(monkeypatch):
    """Slice 2 test 7: cmd must be a list. Passing a string fails the
    structural validator BEFORE any allowlist lookup."""
    _enable_and_allow(monkeypatch)
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    call = ToolCall(name="monitor", arguments={"cmd": "pytest -x"})
    result = policy.evaluate(call, _pctx())
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.monitor_bad_args"


def test_policy_denies_bad_args_empty_cmd(monkeypatch):
    """Slice 2 test 8: empty cmd list → bad_args deny."""
    _enable_and_allow(monkeypatch)
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(_call(cmd=[]), _pctx())
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.monitor_bad_args"


def test_policy_denies_bad_args_non_string_element(monkeypatch):
    """Slice 2 test 9: cmd elements must be strings. A numeric arg
    fails structural validation."""
    _enable_and_allow(monkeypatch)
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(_call(cmd=[PYTHON, 42]), _pctx())
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.monitor_bad_args"


def test_policy_allows_when_enabled_and_binary_in_allowlist(monkeypatch):
    """Slice 2 test 10: master switch on + allowlist contains the
    basename → ALLOW. The happy path of the deny/allow matrix."""
    _enable_and_allow(monkeypatch)
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(
        _call(cmd=[PYTHON, "-c", "print('hi')"]), _pctx(),
    )
    assert result.decision == PolicyDecision.ALLOW


def test_policy_allowlist_matches_basename_not_full_path(monkeypatch):
    """Slice 2 test 11: the allowlist gates on basename(cmd[0]), so
    an absolute path invocation (/usr/local/bin/pytest) is equivalent
    to a plain (pytest) invocation. Operators don't need to enumerate
    every install location."""
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ALLOWED_BINARIES", "mybin")
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    # Absolute path with the allowed basename.
    result = policy.evaluate(
        _call(cmd=["/opt/deep/path/mybin", "--flag"]), _pctx(),
    )
    assert result.decision == PolicyDecision.ALLOW


def test_policy_empty_allowlist_denies_everything(monkeypatch):
    """Slice 2 test 12: an explicitly empty allowlist denies every
    binary, even with the master switch on. Useful for kill-switching
    at runtime — operators can disable the tool without toggling the
    master switch (which resets cumulative allow state)."""
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ALLOWED_BINARIES", "")
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    result = policy.evaluate(
        _call(cmd=[PYTHON, "-c", "pass"]), _pctx(),
    )
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.monitor_binary_not_allowed"


# ===========================================================================
# 3. Handler behavior — happy path + failure modes
# ===========================================================================


@pytest.mark.asyncio
async def test_handler_happy_path_returns_structured_json(monkeypatch):
    """Slice 2 test 13: happy-path handler returns SUCCESS with a
    JSON payload carrying exit_code, events, duration_s, etc."""
    _enable_and_allow(monkeypatch)
    call = _call(cmd=[PYTHON, "-c",
                      "print('alpha'); print('beta')"])
    result = await run_monitor_tool(call, _pctx(), timeout=30.0, cap=8192)
    assert result.status == ToolExecStatus.SUCCESS
    payload = json.loads(result.output)
    assert payload["exit_code"] == 0
    assert payload["early_exit"] is False
    assert payload["timed_out"] is False
    assert isinstance(payload["events"], list)
    # At least the two stdout events + the exited event.
    kinds = [e["kind"] for e in payload["events"]]
    assert "stdout" in kinds
    assert "exited" in kinds


@pytest.mark.asyncio
async def test_handler_early_exit_on_pattern_match(monkeypatch):
    """Slice 2 test 14 (CRITICAL): when a pattern is supplied and a
    stdout line matches, the handler stops reading + terminates the
    subprocess. The motivating use case — streaming pytest, stop on
    'FAILED' without waiting for the whole suite."""
    _enable_and_allow(monkeypatch)
    # Loop printing ticks; match on 'STOP' and bail early.
    script = (
        "import sys\n"
        "for i in range(100):\n"
        "    print(f'tick-{i}'); sys.stdout.flush()\n"
        "    if i == 3:\n"
        "        print('STOP HERE'); sys.stdout.flush()\n"
    )
    call = _call(cmd=[PYTHON, "-u", "-c", script], pattern=r"STOP HERE")
    result = await run_monitor_tool(call, _pctx(), timeout=30.0, cap=8192)
    payload = json.loads(result.output)
    assert payload["early_exit"] is True
    assert "STOP HERE" in payload["early_exit_match"]


@pytest.mark.asyncio
async def test_handler_timeout_enforced(monkeypatch):
    """Slice 2 test 15: handler honors effective timeout. A sleeping
    subprocess gets killed when the wall-clock cap elapses. Payload
    carries timed_out=True."""
    _enable_and_allow(monkeypatch)
    # Request 0.5s; env cap defaults higher so the request wins.
    call = _call(
        cmd=[PYTHON, "-c", "import time; time.sleep(10)"],
        timeout_s=0.5,
    )
    result = await run_monitor_tool(call, _pctx(), timeout=30.0, cap=8192)
    payload = json.loads(result.output)
    assert payload["timed_out"] is True


@pytest.mark.asyncio
async def test_handler_env_timeout_ceiling_caps_requested(monkeypatch):
    """Slice 2 test 16: even if the model requests a 999s timeout,
    the env ceiling caps the effective timeout. Operators always
    retain the upper bound."""
    _enable_and_allow(monkeypatch)
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_TIMEOUT_S", "0.5")
    # Model requests 999s — must be capped to 0.5s.
    call = _call(
        cmd=[PYTHON, "-c", "import time; time.sleep(10)"],
        timeout_s=999.0,
    )
    import time as _time
    t0 = _time.monotonic()
    result = await run_monitor_tool(call, _pctx(), timeout=30.0, cap=8192)
    elapsed = _time.monotonic() - t0
    payload = json.loads(result.output)
    # Enforced ceiling kicked in — elapsed well below the 10s sleep.
    assert payload["timed_out"] is True
    assert elapsed < 5.0  # generous slack for CI


@pytest.mark.asyncio
async def test_handler_binary_not_found_clean_error():
    """Slice 2 test 17: if the binary vanishes between policy approval
    and spawn, the handler returns EXEC_ERROR with a clean message —
    no uncaught FileNotFoundError bubbles up. Race-safe."""
    # Bypass policy for this test — we're validating the handler's
    # defensive path. Pass a cmd that has a binary not on the box.
    call = _call(cmd=["/definitely/not/real/xyz", "arg"])
    result = await run_monitor_tool(call, _pctx(), timeout=10.0, cap=8192)
    assert result.status == ToolExecStatus.EXEC_ERROR
    assert "not found" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_handler_bad_regex_clean_error(monkeypatch):
    """Slice 2 test 18: malformed regex pattern produces EXEC_ERROR
    (not a crash)."""
    _enable_and_allow(monkeypatch)
    call = _call(
        cmd=[PYTHON, "-c", "print('x')"],
        pattern="[invalid(",  # unmatched parens
    )
    result = await run_monitor_tool(call, _pctx(), timeout=10.0, cap=8192)
    assert result.status == ToolExecStatus.EXEC_ERROR
    assert "pattern" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_handler_cmd_missing_clean_error():
    """Slice 2 test 19: handler's defense-in-depth arg validation —
    if policy were bypassed and cmd is missing, the handler still
    returns EXEC_ERROR cleanly."""
    call = ToolCall(name="monitor", arguments={})
    result = await run_monitor_tool(call, _pctx(), timeout=10.0, cap=8192)
    assert result.status == ToolExecStatus.EXEC_ERROR


@pytest.mark.asyncio
async def test_handler_respects_max_events_cap(monkeypatch):
    """Slice 2 test 20: the events array in the output is capped at
    JARVIS_TOOL_MONITOR_MAX_EVENTS. Prevents unbounded output growth."""
    _enable_and_allow(monkeypatch)
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_MAX_EVENTS", "5")
    # Emit 20 lines.
    script = "for i in range(20):\n    print(f'line-{i}')"
    call = _call(cmd=[PYTHON, "-c", script])
    result = await run_monitor_tool(call, _pctx(), timeout=30.0, cap=65536)
    payload = json.loads(result.output)
    # Capped at 5.
    assert len(payload["events"]) <= 5


# ===========================================================================
# 4. Observer invariant — bus failure does not break the handler
# ===========================================================================


@pytest.mark.asyncio
async def test_handler_completes_without_event_bus(monkeypatch):
    """Slice 2 test 21 (REUSES Slice 1 invariant): the handler must
    work with event_bus=None — i.e., the current wiring — and produce
    a complete result. Proves that Slice 2 does not accidentally
    require a running TrinityEventBus."""
    _enable_and_allow(monkeypatch)
    # No bus injected — the handler constructs BackgroundMonitor
    # internally with event_bus=None.
    call = _call(cmd=[PYTHON, "-c", "print('ok')"])
    result = await run_monitor_tool(call, _pctx(), timeout=10.0, cap=8192)
    payload = json.loads(result.output)
    assert result.status == ToolExecStatus.SUCCESS
    assert payload["exit_code"] == 0


# ===========================================================================
# 5. Helper-function pins (classify_cmd / extract_binary_basename)
# ===========================================================================


def test_classify_cmd_rejects_non_list():
    assert classify_cmd("pytest") is not None
    assert classify_cmd(None) is not None
    assert classify_cmd(42) is not None


def test_classify_cmd_rejects_empty_list():
    assert classify_cmd([]) is not None


def test_classify_cmd_rejects_non_string_element():
    assert classify_cmd(["pytest", 1]) is not None
    assert classify_cmd(["pytest", None]) is not None
    assert classify_cmd(["pytest", ""]) is not None


def test_classify_cmd_accepts_well_formed_list():
    assert classify_cmd(["pytest", "-x"]) is None


def test_extract_binary_basename_strips_directories():
    assert extract_binary_basename(["/usr/local/bin/pytest"]) == "pytest"
    assert extract_binary_basename(["pytest"]) == "pytest"
    assert extract_binary_basename([]) == ""


def test_monitor_allowed_binaries_parses_csv(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_MONITOR_ALLOWED_BINARIES",
        "pytest, python , , node",
    )
    allowed = monitor_allowed_binaries()
    assert "pytest" in allowed
    assert "python" in allowed
    assert "node" in allowed
    # Empty tokens stripped.
    assert "" not in allowed


def test_monitor_enabled_default_post_graduation_is_true(monkeypatch):
    """Slice 4 graduation pin: after the Ticket #4 graduation,
    ``JARVIS_TOOL_MONITOR_ENABLED`` defaults to ``"true"``.
    Operators on a fresh install see the tool enabled. Explicit
    ``"false"`` is the runtime kill switch (pinned separately)."""
    monkeypatch.delenv("JARVIS_TOOL_MONITOR_ENABLED", raising=False)
    assert monitor_enabled() is True


def test_monitor_enabled_explicit_false_opts_out(monkeypatch):
    """Slice 4 opt-out pin: explicit ``"false"`` reverts to the
    Slice 2 deny-by-default posture. Guarantees the graduation flip
    is reversible at the env layer."""
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ENABLED", "false")
    assert monitor_enabled() is False


def test_monitor_enabled_explicit_true(monkeypatch):
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ENABLED", "true")
    assert monitor_enabled() is True


def test_monitor_enabled_case_insensitive(monkeypatch):
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ENABLED", "TRUE")
    assert monitor_enabled() is True

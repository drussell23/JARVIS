"""Ticket #4 Slice 4 graduation pins — the defaults are live.

Slice 4 flipped two env flags from their Slice-2/3 opt-in state to
graduated production defaults:

  * JARVIS_TOOL_MONITOR_ENABLED           false -> **true**
  * JARVIS_TEST_RUNNER_STREAMING_ENABLED  false -> **true**

This module encodes the graduation contract as tests so the flip is
self-documenting + future regressions fail loudly:

  1. Graduation pins: the two defaults are ``true`` when env is absent.
  2. Opt-out pins: explicit ``"false"`` on each flag reverts to the
     pre-graduation behavior.
  3. Full-revert matrix: all four combinations of the two flags
     (both-graduated / streaming-only / monitor-only / full-revert)
     reach their expected state.
  4. Authority invariants preserved: the graduation does NOT grant
     the model new execution authority. The Venom tool remains
     read-only (manifest capabilities unchanged, NOT in
     _MUTATION_TOOLS). The TestRunner remains infra (does NOT
     import the monitor_tool surface).
  5. Defensive structural gates preserved: per-call binary allowlist
     still applies (JARVIS_TOOL_MONITOR_ALLOWED_BINARIES), policy
     layer still validates argv shape, timeout ceiling still caps
     model-requested budgets.
  6. Docstring bit-rot guards: the two env helpers' docstrings
     carry the graduation language. Future refactors that strip
     the language fail loudly so the graduation is documented in
     the code, not just in git history.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.monitor_tool import (
    monitor_enabled,
    monitor_allowed_binaries,
)
from backend.core.ouroboros.governance.scoped_tool_access import (
    _MUTATION_TOOLS,
)
from backend.core.ouroboros.governance.test_runner import (
    _streaming_enabled,
    _early_exit_on_fail,
    _parity_mode,
)
from backend.core.ouroboros.governance.tool_executor import (
    _L1_MANIFESTS,
    GoverningToolPolicy,
    PolicyContext,
    PolicyDecision,
    ToolCall,
)


# ---------------------------------------------------------------------------
# Fixture: clean env for every graduation test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_ticket4_env(monkeypatch):
    """Every test starts with no Ticket #4 flags set — so the
    graduated defaults are what the tests actually observe."""
    for key in (
        "JARVIS_TOOL_MONITOR_ENABLED",
        "JARVIS_TOOL_MONITOR_ALLOWED_BINARIES",
        "JARVIS_TEST_RUNNER_STREAMING_ENABLED",
        "JARVIS_TEST_RUNNER_EARLY_EXIT_ON_FAIL",
        "JARVIS_TEST_RUNNER_PARITY_MODE",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# 1. Graduation pins — both defaults are now true
# ---------------------------------------------------------------------------


def test_4a_monitor_default_post_graduation_is_true():
    """Slice 4 pin: ``JARVIS_TOOL_MONITOR_ENABLED`` default is
    ``true`` after graduation. Model-facing observability tool is
    enabled on a fresh operator install; opt-out via explicit
    ``"false"``."""
    assert monitor_enabled() is True


def test_4b_streaming_default_post_graduation_is_true():
    """Slice 4 pin: ``JARVIS_TEST_RUNNER_STREAMING_ENABLED`` default
    is ``true`` after graduation. TestRunner's streaming path runs
    by default; legacy ``_exec_with_timeout`` available via
    explicit ``"false"``."""
    assert _streaming_enabled() is True


def test_4c_early_exit_still_defaults_false():
    """Slice 4 did NOT flip early-exit. The opt-in flag stays off
    by default — legacy ``run everything`` semantics preserved
    unless operators explicitly enable first-failure termination."""
    assert _early_exit_on_fail() is False


def test_4d_parity_mode_still_defaults_false():
    """Slice 4 did NOT flip parity-mode. Still opt-in because it
    doubles pytest cost — operators enable it during graduation
    verification, not in steady-state."""
    assert _parity_mode() is False


# ---------------------------------------------------------------------------
# 2. Opt-out pins — explicit false reverts each flag
# ---------------------------------------------------------------------------


def test_4e_monitor_explicit_false_opts_out(monkeypatch):
    """Slice 4 opt-out pin: operators retain a runtime kill switch
    on the Venom monitor tool via ``=false``. Proves the
    graduation flip is reversible at the env layer."""
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ENABLED", "false")
    assert monitor_enabled() is False


def test_4f_streaming_explicit_false_opts_out(monkeypatch):
    """Slice 4 opt-out pin: operators retain a runtime kill switch
    on the TestRunner streaming path via ``=false``."""
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "false")
    assert _streaming_enabled() is False


def test_4g_opt_outs_are_case_insensitive(monkeypatch):
    """Slice 4 opt-out pin: case-insensitive env parsing — operators'
    fat-fingered ``FALSE`` / ``False`` all revert correctly."""
    for val in ("false", "False", "FALSE", "  False  "):
        monkeypatch.setenv("JARVIS_TOOL_MONITOR_ENABLED", val)
        monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", val)
        assert monitor_enabled() is False, (
            "monitor opt-out failed for env value " + repr(val)
        )
        assert _streaming_enabled() is False, (
            "streaming opt-out failed for env value " + repr(val)
        )


# ---------------------------------------------------------------------------
# 3. Full-revert matrix — 4-combination pin (matches Slice 3d pattern)
# ---------------------------------------------------------------------------


def test_4h_full_revert_matrix(monkeypatch):
    """Slice 4 matrix pin: operator contract for full-revert. The
    4 combinations of (monitor_flag, streaming_flag) each produce
    the expected (monitor_state, streaming_state). Documents the
    independence of the two flags — flipping one does NOT affect
    the other. Mirrors the Slice 3d full-revert matrix pattern
    from the SemanticIndex graduation."""
    # (1) Both graduated defaults (no env set) -> both True.
    assert monitor_enabled() is True
    assert _streaming_enabled() is True

    # (2) Monitor explicit false, streaming graduated default.
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ENABLED", "false")
    monkeypatch.delenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", raising=False)
    assert monitor_enabled() is False
    assert _streaming_enabled() is True

    # (3) Streaming explicit false, monitor graduated default.
    monkeypatch.delenv("JARVIS_TOOL_MONITOR_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "false")
    assert monitor_enabled() is True
    assert _streaming_enabled() is False

    # (4) Both explicit false — full revert to Slice 2/3 opt-in state.
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ENABLED", "false")
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "false")
    assert monitor_enabled() is False
    assert _streaming_enabled() is False


# ---------------------------------------------------------------------------
# 4. Authority invariants preserved
# ---------------------------------------------------------------------------


def test_4i_monitor_still_read_only_post_graduation():
    """Slice 4 invariant: the Venom monitor tool's manifest
    capabilities are UNCHANGED by graduation — still read-only
    ``{"subprocess"}``, NOT ``{"subprocess","write"}``. Graduation
    flips the opt-in requirement; it does NOT change the tool's
    authority shape.

    Also still NOT in _MUTATION_TOOLS — under an is_read_only scope
    the tool is still permitted (observation, not mutation)."""
    assert "monitor" in _L1_MANIFESTS
    m = _L1_MANIFESTS["monitor"]
    assert "write" not in m.capabilities, (
        "Slice 4 graduation violation: monitor manifest gained "
        "'write' capability — graduation must NOT escalate "
        "authority. Check the manifest definition in tool_executor.py."
    )
    assert "subprocess" in m.capabilities
    assert "monitor" not in _MUTATION_TOOLS, (
        "Slice 4 invariant violation: monitor added to _MUTATION_TOOLS. "
        "The tool is read-only observation; graduation does NOT "
        "reclassify it as a mutation tool."
    )


def test_4j_monitor_still_requires_allowlist_post_graduation(monkeypatch):
    """Slice 4 invariant: the binary-allowlist gate still fires
    after graduation. Even with the master switch enabled, a
    binary outside the allowlist is DENIED. Graduation removes
    the opt-in requirement on the model's access — it does NOT
    remove the structural safeguard on WHICH binaries the model
    can spawn."""
    # Default defaults (monitor enabled) + tight custom allowlist.
    monkeypatch.setenv("JARVIS_TOOL_MONITOR_ALLOWED_BINARIES", "pytest")
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    call = ToolCall(
        name="monitor",
        arguments={"cmd": ["/bin/sh", "-c", "true"]},
    )
    ctx = PolicyContext(
        repo="jarvis", repo_root=Path("/tmp"),
        op_id="op-slice4", call_id="op-slice4:r0:t0",
        round_index=0, risk_tier=None, is_read_only=False,
    )
    result = policy.evaluate(call, ctx)
    assert result.decision == PolicyDecision.DENY, (
        "Slice 4 safeguard violation: binary-allowlist gate did "
        "not fire post-graduation. The structural safeguard MUST "
        "survive the graduation — operators need the allowlist "
        "even more when the tool is on by default."
    )
    assert result.reason_code == "tool.denied.monitor_binary_not_allowed"


def test_4k_test_runner_still_does_not_import_monitor_tool():
    """Slice 4 invariant: the Slice 3 isolation boundary HOLDS
    post-graduation. test_runner.py MUST NOT import monitor_tool
    regardless of flag defaults. TestRunner stays infra — the
    graduation does NOT merge the two surfaces."""
    src = Path(
        "backend/core/ouroboros/governance/test_runner.py"
    ).read_text()
    forbidden = [
        "from backend.core.ouroboros.governance.monitor_tool",
        "import monitor_tool",
        "run_monitor_tool",
    ]
    for f in forbidden:
        assert f not in src, (
            "Slice 4 graduation violation: test_runner.py imports "
            + repr(f) + ". The isolation boundary from Slice 3 MUST "
            "survive graduation — infra (TestRunner) and "
            "model-facing tool (monitor) remain distinct surfaces."
        )


def test_4l_monitor_tool_does_not_import_orchestrator_gates():
    """Slice 4 invariant: the Venom monitor tool's module MUST NOT
    import any authority-carrying orchestrator / gate module.
    Graduation keeps observability-only posture; it does NOT gain
    the model new authority surface."""
    src = Path(
        "backend/core/ouroboros/governance/monitor_tool.py"
    ).read_text()
    forbidden = [
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.risk_tier_floor",
        "from backend.core.ouroboros.governance.semantic_guardian",
    ]
    for f in forbidden:
        assert f not in src, (
            "Slice 4 graduation violation: monitor_tool.py imports "
            + repr(f) + ". Graduation does NOT grant new "
            "authority surface — observability stays observability."
        )


# ---------------------------------------------------------------------------
# 5. Defensive structural gates preserved
# ---------------------------------------------------------------------------


def test_4m_policy_still_validates_cmd_shape_post_graduation(monkeypatch):
    """Slice 4 invariant: even with the master switch defaulting
    true, policy still rejects malformed ``cmd`` arguments.
    bad_args deny-reason still fires. Structural validation is
    orthogonal to the graduation flip."""
    # Defaults are graduated (monitor enabled).
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    ctx = PolicyContext(
        repo="jarvis", repo_root=Path("/tmp"),
        op_id="op-slice4", call_id="op-slice4:r0:t0",
        round_index=0, risk_tier=None, is_read_only=False,
    )
    # Malformed cmd: empty list.
    call = ToolCall(name="monitor", arguments={"cmd": []})
    result = policy.evaluate(call, ctx)
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.monitor_bad_args"


def test_4n_default_allowlist_still_includes_pytest_family():
    """Slice 4 invariant: the default binary allowlist
    (JARVIS_TOOL_MONITOR_ALLOWED_BINARIES unset) still includes
    pytest / python / node / npm / go / cargo / make — the
    operator-curated list that Slice 2 shipped. Graduation doesn't
    broaden the default allowlist."""
    allowed = monitor_allowed_binaries()
    # Core expected set — the graduation doesn't add anything new.
    expected = {
        "pytest", "python", "python3",
        "node", "npm", "go", "cargo", "make",
    }
    for binary in expected:
        assert binary in allowed, (
            "default allowlist regression: " + binary + " missing. "
            "Slice 4 graduation doesn't remove binaries; check "
            "_DEFAULT_ALLOWED_BINARIES_CSV in monitor_tool.py."
        )


# ---------------------------------------------------------------------------
# 6. Docstring bit-rot guards — graduation language self-documents
# ---------------------------------------------------------------------------


def test_4o_monitor_enabled_docstring_documents_graduation():
    """Slice 4 bit-rot guard: ``monitor_enabled``'s docstring
    carries the graduation date + opt-out language. Future
    refactors that strip the documentation fail loudly, so the
    graduation is recorded in the code rather than only in git
    history."""
    doc = (monitor_enabled.__doc__ or "").lower()
    assert "graduat" in doc, (
        "monitor_enabled docstring must mention graduation"
    )
    assert "true" in doc, (
        "monitor_enabled docstring must name the new default"
    )
    assert "false" in doc or "opt" in doc, (
        "monitor_enabled docstring must document the opt-out path"
    )


def test_4p_streaming_enabled_docstring_documents_graduation():
    """Slice 4 bit-rot guard: ``_streaming_enabled``'s docstring
    carries the graduation date + opt-out language."""
    doc = (_streaming_enabled.__doc__ or "").lower()
    assert "graduat" in doc
    assert "true" in doc
    assert "false" in doc or "opt" in doc or "legacy" in doc


# ---------------------------------------------------------------------------
# 7. Combined Ticket #4 scorecard pin (meta-test for documentation)
# ---------------------------------------------------------------------------


def test_4q_ticket_4_surface_still_isolated_post_graduation():
    """Slice 4 meta-invariant: the three Ticket #4 modules carry
    the expected layered-dependency shape:

      background_monitor.py  <-- primitive (no Venom / TestRunner deps)
      monitor_tool.py        <-- imports background_monitor ONLY
      test_runner.py         <-- imports background_monitor ONLY (not monitor_tool)

    This test re-greps the three modules to pin the
    dependency-direction rule post-graduation. Future code that
    entangles the two consumer surfaces (Venom ↔ TestRunner) fails
    loudly."""
    bm_src = Path(
        "backend/core/ouroboros/governance/background_monitor.py"
    ).read_text()
    mt_src = Path(
        "backend/core/ouroboros/governance/monitor_tool.py"
    ).read_text()
    tr_src = Path(
        "backend/core/ouroboros/governance/test_runner.py"
    ).read_text()

    # Primitive imports NEITHER consumer.
    assert "from backend.core.ouroboros.governance.monitor_tool" not in bm_src
    assert "from backend.core.ouroboros.governance.test_runner" not in bm_src

    # Venom tool imports the primitive.
    assert (
        "from backend.core.ouroboros.governance.background_monitor"
        in mt_src
    )
    # Venom tool does NOT import TestRunner.
    assert "from backend.core.ouroboros.governance.test_runner" not in mt_src

    # TestRunner imports the primitive.
    assert (
        "from backend.core.ouroboros.governance.background_monitor"
        in tr_src
    )
    # TestRunner does NOT import the Venom tool surface.
    assert "from backend.core.ouroboros.governance.monitor_tool" not in tr_src

"""Slice 26 — Asynchronous Process-Linked Power Assertion Engine.

Closes the host-sleep wedge surfaced by v19 (bt-2026-05-27-003843).
Spawns a process-linked ``caffeinate -w <pid>`` subprocess at boot
so the host can't suspend during the soak. Kernel manages the
assertion lifecycle (auto-release when parent exits via ``-w`` flag).

# Test surface (3 AST pins + 7 spine)
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PS_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "power_supervisor.py"
)
GLS_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "governed_loop_service.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_caffeinate_command_shape() -> None:
    """The subprocess invocation MUST be ``caffeinate -w <pid>`` —
    the ``-w`` flag is the load-bearing process-linked semantic (kernel
    waits for the given PID to exit, then releases the assertion).
    Any other invocation shape silently breaks the auto-cleanup."""
    src = PS_FILE.read_text()
    assert "Slice 26" in src, (
        "power_supervisor missing Slice 26 attribution — refactor reverted"
    )
    # The binary constant
    assert "_CAFFEINATE_BINARY" in src
    assert '"caffeinate"' in src or "'caffeinate'" in src
    # AST walk: confirm the create_subprocess_exec call carries the
    # exact argv shape we need.
    tree = ast.parse(src, filename=str(PS_FILE))
    found_correct_call = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_subprocess_exec"
        ):
            # First positional should be the binary path; second "-w";
            # third str(pid)
            if len(node.args) >= 3:
                arg2 = node.args[1]
                if (
                    isinstance(arg2, ast.Constant)
                    and arg2.value == "-w"
                ):
                    found_correct_call = True
                    break
    assert found_correct_call, (
        "power_supervisor: create_subprocess_exec doesn't pass '-w' as "
        "the second argument — process-linked semantic broken"
    )


def test_ast_pin_attested_log_message_verbatim() -> None:
    """The operator-attested §5 transparency message MUST be present
    verbatim. AST-walk the logger.info call and concatenate adjacent
    string literals (Python compile-time concat)."""
    src = PS_FILE.read_text()
    tree = ast.parse(src, filename=str(PS_FILE))
    found_message = ""
    # Find the assert_power_lock function + its logger.info(...) on the
    # success path (the one containing "Active process-linked")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "assert_power_lock"
        ):
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "info"
                    and sub.args
                ):
                    arg0 = sub.args[0]
                    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                        if "Active process-linked" in arg0.value:
                            found_message = arg0.value
                            break
            break
    assert found_message, (
        "assert_power_lock missing logger.info(...) with the §5 "
        "attestation 'Active process-linked' message"
    )
    # Required verbatim clauses
    for clause in (
        "Active process-linked host sleep assertion",
        "established via IOKit/Caffeinate",
        "for PID:",
    ):
        assert clause in found_message, (
            f"§5 attestation clause missing or reworded: {clause!r} "
            f"(actual message: {found_message!r})"
        )


def test_ast_pin_gls_boot_integration_before_preflight() -> None:
    """Slice 26 power assertion MUST fire BEFORE Slice 25B preflight
    so the host can't sleep during the 10s probe window. Source-order
    check + attribution check."""
    src = GLS_FILE.read_text()
    assert "Slice 26" in src, (
        "governed_loop_service missing Slice 26 attribution — wiring reverted"
    )
    assert "assert_power_lock" in src, (
        "governed_loop_service missing assert_power_lock invocation"
    )
    # Source ordering: power assertion call must come BEFORE preflight call
    pwr_pos = src.find("assert_power_lock()")
    pf_pos = src.find("run_boot_preflight(")
    assert pwr_pos > 0 and pf_pos > 0, (
        "could not locate both Slice 26 power assertion + Slice 25B preflight"
    )
    assert pwr_pos < pf_pos, (
        "Slice 26 power assertion is AFTER Slice 25B preflight — host can "
        "sleep during the 10s probe; ordering violation"
    )


# ──────────────────────────────────────────────────────────────────────
# Platform + master flag spine — 3
# ──────────────────────────────────────────────────────────────────────


def test_spine_master_flag_default_on_for_darwin(monkeypatch) -> None:
    """On darwin, the master flag defaults TRUE (load-bearing safety
    net by default since the only failure mode is the same as without
    Slice 26 — boot continues unprotected, identical to legacy)."""
    monkeypatch.delenv("JARVIS_POWER_ASSERTION_ENABLED", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    from backend.core.ouroboros.governance import power_supervisor as ps_mod
    # Force a fresh module-level read (helpers re-read env at call time)
    assert ps_mod.is_power_assertion_enabled() is True


def test_spine_master_flag_default_off_for_non_darwin(monkeypatch) -> None:
    """On non-darwin platforms, default is FALSE (no native primitive
    we can rely on)."""
    monkeypatch.delenv("JARVIS_POWER_ASSERTION_ENABLED", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    from backend.core.ouroboros.governance.power_supervisor import (
        is_power_assertion_enabled,
    )
    assert is_power_assertion_enabled() is False
    monkeypatch.setattr(sys, "platform", "win32")
    assert is_power_assertion_enabled() is False


def test_spine_explicit_off_wins_on_darwin(monkeypatch) -> None:
    """Operator opt-out: explicit OFF on darwin wins over the default-on."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("JARVIS_POWER_ASSERTION_ENABLED", "false")
    from backend.core.ouroboros.governance.power_supervisor import (
        is_power_assertion_enabled,
    )
    assert is_power_assertion_enabled() is False


# ──────────────────────────────────────────────────────────────────────
# Subprocess lifecycle spine — 4
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spine_non_darwin_skips_without_subprocess(monkeypatch) -> None:
    """Non-darwin platforms MUST return None without invoking
    create_subprocess_exec at all (no subprocess pollution on linux
    CI runners)."""
    monkeypatch.delenv("JARVIS_POWER_ASSERTION_ENABLED", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    from backend.core.ouroboros.governance import power_supervisor as ps_mod

    spawn_mock = mock.AsyncMock(
        side_effect=AssertionError("create_subprocess_exec MUST NOT fire on linux"),
    )
    monkeypatch.setattr(
        ps_mod.asyncio, "create_subprocess_exec", spawn_mock,
    )
    result = await ps_mod.assert_power_lock()
    assert result is None
    spawn_mock.assert_not_called()


@pytest.mark.asyncio
async def test_spine_darwin_spawns_caffeinate_with_correct_args(
    monkeypatch,
) -> None:
    """On darwin with master on, MUST invoke create_subprocess_exec
    with ['caffeinate', '-w', '<pid>'] argv."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("JARVIS_POWER_ASSERTION_ENABLED", raising=False)
    from backend.core.ouroboros.governance import power_supervisor as ps_mod

    # Mock shutil.which to claim caffeinate is at a known path
    monkeypatch.setattr(ps_mod.shutil, "which", lambda name: "/usr/bin/caffeinate")

    # Mock the spawn to return a fake proc handle
    fake_proc = mock.MagicMock()
    fake_proc.pid = 99999
    spawn_mock = mock.AsyncMock(return_value=fake_proc)
    monkeypatch.setattr(
        ps_mod.asyncio, "create_subprocess_exec", spawn_mock,
    )

    result = await ps_mod.assert_power_lock(parent_pid=12345)
    assert result is not None
    assert result.platform == "darwin"
    assert result.parent_pid == 12345
    assert result.subprocess_pid == 99999
    assert result.binary == "/usr/bin/caffeinate"

    # Verify the exact argv passed
    spawn_mock.assert_called_once()
    args = spawn_mock.call_args.args
    assert args[0] == "/usr/bin/caffeinate"
    assert args[1] == "-w"
    assert args[2] == "12345", f"Expected '12345' as pid arg, got {args[2]!r}"


@pytest.mark.asyncio
async def test_spine_missing_binary_returns_none_with_warning(
    monkeypatch, caplog,
) -> None:
    """When caffeinate is missing on PATH (rare on macOS, but
    defensive for minimal containers), return None + log WARNING
    without spawning."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("JARVIS_POWER_ASSERTION_ENABLED", raising=False)
    from backend.core.ouroboros.governance import power_supervisor as ps_mod

    monkeypatch.setattr(ps_mod.shutil, "which", lambda name: None)
    spawn_mock = mock.AsyncMock(
        side_effect=AssertionError("spawn MUST NOT fire when binary missing"),
    )
    monkeypatch.setattr(
        ps_mod.asyncio, "create_subprocess_exec", spawn_mock,
    )

    import logging
    with caplog.at_level(logging.WARNING):
        result = await ps_mod.assert_power_lock()
    assert result is None
    spawn_mock.assert_not_called()
    warnings = [r for r in caplog.records if "not found on PATH" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_spine_spawn_exception_swallowed_returns_none(
    monkeypatch, caplog,
) -> None:
    """If create_subprocess_exec raises (e.g. permission error,
    OOM), the exception MUST be swallowed; assert_power_lock
    returns None and boot continues. Power assertion is enhancement,
    never blocks boot."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("JARVIS_POWER_ASSERTION_ENABLED", raising=False)
    from backend.core.ouroboros.governance import power_supervisor as ps_mod

    monkeypatch.setattr(ps_mod.shutil, "which", lambda name: "/usr/bin/caffeinate")
    spawn_mock = mock.AsyncMock(side_effect=PermissionError("denied"))
    monkeypatch.setattr(
        ps_mod.asyncio, "create_subprocess_exec", spawn_mock,
    )

    import logging
    with caplog.at_level(logging.WARNING):
        result = await ps_mod.assert_power_lock()
    assert result is None  # MUST NOT raise — boot must continue
    warnings = [r for r in caplog.records if "failed to spawn" in r.getMessage()]
    assert len(warnings) == 1
    assert "PermissionError" in warnings[0].getMessage()

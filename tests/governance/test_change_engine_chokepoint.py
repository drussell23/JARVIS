"""tests/governance/test_change_engine_chokepoint.py

Task 6 (Anti-Venom hardening): ChangeEngine._pre_write_gate — the universal
mutation chokepoint. Every governed disk write funnels through
``ChangeEngine.execute`` → the single ``target.write_text`` site, so this gate
is the last line of defence before bytes hit disk.

The gate (fail-closed on any internal error) enforces, in order:
  1. canonicalize (realpath/abspath — defeats ``../`` + symlinks)
  2. containment — target must live under the effective write root
  3. immutable governance — hardcoded sentinels (no env off-switch)
  4. protected-path — reuse Venom's ``_is_protected_path`` registry
  5. SemanticGuardian hard findings

Contract: governance/ + .git/hooks + traversal blocked; legit src/ allowed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.change_engine import (
    BlockedPathError,
    ChangeEngine,
    ChangeRequest,
    _IMMUTABLE_GOVERNANCE_SENTINELS,
    _sentinel_matches_path,
    assert_write_path_allowed,
)
from backend.core.ouroboros.governance.ledger import OperationLedger
from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
)


def _engine(tmp_path: Path) -> ChangeEngine:
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    return ChangeEngine(project_root=tmp_path, ledger=ledger)


def _safe_auto_profile(target: Path) -> OperationProfile:
    """A profile the RiskEngine classifies as SAFE_AUTO (so the op reaches
    the APPLY write — the only path that exercises the gate)."""
    return OperationProfile(
        files_affected=[target],
        change_type=ChangeType.MODIFY,
        blast_radius=1,
        crosses_repo_boundary=False,
        touches_security_surface=False,
        touches_supervisor=False,
        test_scope_confidence=1.0,
    )


def _request(target: Path, content: str, op_id: str) -> ChangeRequest:
    return ChangeRequest(
        goal="anti-venom chokepoint test",
        target_file=target,
        proposed_content=content,
        profile=_safe_auto_profile(target),
        op_id=op_id,
    )


def test_sentinels_include_self_enforcers():
    # the immune system protects the files that enforce it
    assert any("change_engine" in s for s in _IMMUTABLE_GOVERNANCE_SENTINELS)
    assert any("sandbox_exec" in s for s in _IMMUTABLE_GOVERNANCE_SENTINELS)
    assert any("semantic_guardian" in s for s in _IMMUTABLE_GOVERNANCE_SENTINELS)


def test_apply_to_git_hooks_blocked(tmp_path):
    eng = _engine(tmp_path)
    target = tmp_path / ".git" / "hooks" / "pre-commit"
    req = _request(target, "#!/bin/sh\n", "op-git")
    res = asyncio.run(eng.execute(req))
    assert res.success is False  # containment/protected-path blocked
    assert not target.exists()


def test_apply_to_governance_blocked(tmp_path):
    eng = _engine(tmp_path)
    target = (
        tmp_path
        / "backend" / "core" / "ouroboros" / "governance"
        / "semantic_guardian.py"
    )
    req = _request(target, "x = 1\n", "op-gov")
    res = asyncio.run(eng.execute(req))
    assert res.success is False
    assert not target.exists()


def test_traversal_escape_blocked(tmp_path):
    eng = _engine(tmp_path)
    # realpath collapses the .. → escapes the write root → containment reject
    target = tmp_path / ".." / ".." / "etc" / "x"
    req = _request(target, "x", "op-esc")
    res = asyncio.run(eng.execute(req))
    assert res.success is False


def test_legitimate_body_edit_still_applies(tmp_path):
    (tmp_path / "src").mkdir()
    eng = _engine(tmp_path)
    target = tmp_path / "src" / "app.py"
    req = _request(target, "x = 1\n", "op-ok")
    res = asyncio.run(eng.execute(req))
    assert res.success is True
    assert target.exists()
    assert "x = 1" in target.read_text()


# ---------------------------------------------------------------------------
# assert_write_path_allowed unit tests (review wave)
# ---------------------------------------------------------------------------


def test_assert_write_path_allowed_blocks_governance(tmp_path):
    """assert_write_path_allowed raises on an immutable-governance sentinel."""
    target = (
        tmp_path
        / "backend" / "core" / "ouroboros" / "governance"
        / "risk_engine.py"
    )
    with pytest.raises(BlockedPathError, match="immutable governance"):
        assert_write_path_allowed(target, tmp_path)


def test_assert_write_path_allowed_blocks_traversal(tmp_path):
    """assert_write_path_allowed raises on a ../ path-traversal attempt."""
    target = tmp_path / ".." / ".." / "etc" / "x"
    with pytest.raises(BlockedPathError, match="escapes write_root"):
        assert_write_path_allowed(target, tmp_path)


def test_assert_write_path_allowed_allows_src(tmp_path):
    """assert_write_path_allowed passes for a legitimate src/ target."""
    target = tmp_path / "src" / "app.py"
    # Should not raise — src/app.py is not a governance sentinel or protected path
    assert_write_path_allowed(target, tmp_path)


def test_sentinel_boundary_anchor_no_false_positive():
    """_sentinel_matches_path: risk_engine_helpers.py must NOT match the risk_engine sentinel.

    The char after the sentinel is '_' — NOT a component boundary — so the
    function must return False.  The broader protected-path check may still
    block the file, but that is a separate concern; the SENTINEL alone must
    be boundary-safe.
    """
    sentinel = "backend/core/ouroboros/governance/risk_engine"
    helpers_path = "backend/core/ouroboros/governance/risk_engine_helpers.py"
    assert not _sentinel_matches_path(helpers_path, sentinel)


def test_sentinel_boundary_anchor_exact_match():
    """_sentinel_matches_path: risk_engine.py MUST match the risk_engine sentinel.

    The char after the sentinel is '.' — a valid component boundary.
    """
    sentinel = "backend/core/ouroboros/governance/risk_engine"
    exact_path = "backend/core/ouroboros/governance/risk_engine.py"
    assert _sentinel_matches_path(exact_path, sentinel)


def test_sentinel_boundary_anchor_subdir_match():
    """_sentinel_matches_path: risk_engine/sub.py MUST match — '/' is a valid boundary."""
    sentinel = "backend/core/ouroboros/governance/risk_engine"
    subdir_path = "backend/core/ouroboros/governance/risk_engine/sub.py"
    assert _sentinel_matches_path(subdir_path, sentinel)


# ---------------------------------------------------------------------------
# Final-review CRITICAL 1 — guardian baselines on the on-disk pre-image, not ""
#
# The chokepoint guardian formerly inspected with ``old_content=""`` which turns
# every MODIFY into a synthetic creation. Delta-gated patterns
# (shell_exec_introduced, credential-shape, dynamic_import_chain, …) then fired
# on PRE-EXISTING legitimate code — any candidate whose file already used
# subprocess/os.system/etc. was BlockedPathError'd at APPLY even though it added
# nothing. The fix baselines ``old`` against the on-disk pre-image so the delta
# is (on-disk → candidate).
# ---------------------------------------------------------------------------


def test_legit_subprocess_edit_allowed_delta_zero(tmp_path):
    """A MODIFY to a file that ALREADY uses subprocess (delta=0) must apply.

    RED under the old ``old_content=""`` baseline (delta vs ∅ = 1 → blocked);
    GREEN once the gate baselines against the on-disk pre-image.
    """
    (tmp_path / "src").mkdir()
    eng = _engine(tmp_path)
    target = tmp_path / "src" / "runner.py"

    # On-disk pre-image already contains a legit subprocess call.
    on_disk = (
        "import subprocess\n\n\n"
        "def run_it():\n"
        "    subprocess.run(['ls'])\n"
    )
    target.write_text(on_disk)

    # Candidate = same file + an added docstring. subprocess STILL present,
    # so the delta against the on-disk baseline is 0 (nothing introduced).
    candidate = (
        "import subprocess\n\n\n"
        "def run_it():\n"
        "    \"\"\"Run ls.\"\"\"\n"
        "    subprocess.run(['ls'])\n"
    )
    req = _request(target, candidate, "op-legit-subproc")
    res = asyncio.run(eng.execute(req))
    assert res.success is True, (
        "legit subprocess-edit (delta=0) must NOT be blocked by the guardian"
    )
    assert "Run ls." in target.read_text()


def test_introducing_os_system_still_blocked_delta_positive(tmp_path):
    """True-positive guard: ADDING os.system to a file that had none → blocked.

    Confirms the on-disk baseline does NOT disarm the guardian — a genuine new
    hard finding (delta > 0) still fails closed.
    """
    (tmp_path / "src").mkdir()
    eng = _engine(tmp_path)
    target = tmp_path / "src" / "clean.py"

    # On-disk pre-image has NO shell-exec at all.
    target.write_text("def run_it():\n    return 1\n")

    # Candidate ADDS os.system → delta = 1 → hard finding → blocked.
    candidate = (
        "import os\n\n\n"
        "def run_it():\n"
        "    os.system('ls')\n"
        "    return 1\n"
    )
    req = _request(target, candidate, "op-add-os-system")
    res = asyncio.run(eng.execute(req))
    assert res.success is False, (
        "introducing os.system (delta>0) must still be blocked"
    )
    # The original on-disk content must be untouched (no os.system written).
    assert "os.system" not in target.read_text()

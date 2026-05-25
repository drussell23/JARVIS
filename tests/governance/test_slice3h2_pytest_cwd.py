"""Slice 3H.2 — pytest cwd via InteractiveRepairLoop construction order.

Closes the final wiring seam surfaced by capability soak
bt-2026-05-25-082441. With ``JARVIS_INTERACTIVE_REPAIR_ENABLED=true``,
the repair loop actually ran (no "disabled" log) — but it bailed every
iter at the hard-guard:

  if err.error_type in {"UnknownError", "TimeoutError"} or err.line_number <= 0:
      logger.warning("[InteractiveRepair] Refusing to patch: ...")
      break

Root cause: ``_run_and_capture`` invokes pytest via
``asyncio.create_subprocess_exec`` with ``cwd=str(self._project_root)``
— the right shape. But ``self._project_root`` was set to
``orch._config.project_root`` (the host JARVIS repo) at constructor
time, not the per-instance worktree. Pytest ran in the JARVIS cwd,
discovered JARVIS tests (not the Ansible tests the patch should have
affected), produced output with no failure attributable to
``lib/ansible/cli/doc.py`` — the ``Traceback`` regex never matched
that path → ``UnknownError`` → hard-guard break.

# Fix mechanism — construction-order reorder

Move ``_repair_root`` resolution ABOVE the ``InteractiveRepairLoop``
constructor and pass it as ``project_root``:

  _repair_root: Path = orch._config.project_root  # default
  try:
      from ...operation_advisor import (
          resolve_envelope_repo_root as _slice3h_resolve_root,
      )
      _wt_override = _slice3h_resolve_root(
          getattr(ctx, "intake_evidence_json", "") or "",
          project_root=orch._config.project_root,
      )
      if _wt_override is not None:
          _repair_root = _wt_override
  except Exception:
      _repair_root = orch._config.project_root

  _repair = InteractiveRepairLoop(
      provider=orch._generator,
      project_root=_repair_root,  # was orch._config.project_root
  )

No InteractiveRepair API change. The existing
``cwd=str(self._project_root)`` line at
``interactive_repair.py:165`` now lands in the worktree.

# Test surface (2 AST pins + 4 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATE_RUNNER_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "phase_runners" / "validate_runner.py"
)
INTERACTIVE_REPAIR_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "interactive_repair.py"
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_interactive_repair_constructed_with_repair_root() -> None:
    """``InteractiveRepairLoop(...)`` MUST be constructed with
    ``project_root=_repair_root`` — NOT ``project_root=
    orch._config.project_root``. Without this Slice 3H.2 reorder,
    the subprocess pytest runs in the JARVIS cwd and the
    bt-2026-05-25-082441 hard-guard trap is open again."""
    src = VALIDATE_RUNNER_FILE.read_text()
    assert "project_root=_repair_root" in src, (
        "InteractiveRepairLoop is NOT constructed with _repair_root "
        "— Slice 3H.2 reorder missing; pytest will run in wrong cwd."
    )
    # The InteractiveRepairLoop SPECIFICALLY must not be constructed
    # with the JARVIS project_root. Other callers of
    # ``project_root=orch._config.project_root`` (e.g. LSPTypeChecker,
    # resolve_envelope_repo_root's allowlist anchor) are legitimate
    # and unaffected — they need the host JARVIS root by design.
    tree = _parse(VALIDATE_RUNNER_FILE)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) and not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "InteractiveRepairLoop"
        ):
            # Match either bare name InteractiveRepairLoop(...)
            # or module.InteractiveRepairLoop(...)
            if not (
                isinstance(node.func, ast.Name)
                and node.func.id == "InteractiveRepairLoop"
            ):
                continue
        if (
            isinstance(node.func, ast.Name)
            and node.func.id != "InteractiveRepairLoop"
        ):
            continue
        for kw in node.keywords:
            if kw.arg == "project_root":
                kw_src = ast.unparse(kw.value)
                assert kw_src != "orch._config.project_root", (
                    f"InteractiveRepairLoop constructed with "
                    f"project_root=orch._config.project_root — "
                    f"Slice 3H.2 reorder regressed."
                )


def test_ast_pin_repair_root_resolved_before_constructor() -> None:
    """The ``_repair_root`` initialization MUST appear textually BEFORE
    the ``InteractiveRepairLoop(...)`` constructor. Python evaluates
    statements in order — without this textual ordering, the loop is
    constructed with the JARVIS root and the override has no effect
    on the subprocess cwd."""
    src = VALIDATE_RUNNER_FILE.read_text()
    repair_root_idx = src.find("_repair_root: Path = orch._config.project_root")
    constructor_idx = src.find("_repair = InteractiveRepairLoop(")
    assert repair_root_idx >= 0, "_repair_root init missing"
    assert constructor_idx >= 0, "InteractiveRepairLoop constructor missing"
    assert repair_root_idx < constructor_idx, (
        "_repair_root MUST be initialized BEFORE InteractiveRepairLoop "
        "construction — Slice 3H.2 ordering invariant broken."
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 4
# ──────────────────────────────────────────────────────────────────────


def test_spine_interactive_repair_subprocess_uses_self_project_root() -> None:
    """Verify the subprocess invocation in InteractiveRepair flows
    ``self._project_root`` to ``cwd=``. This is the contract Slice 3H.2
    depends on — if InteractiveRepair changes its subprocess invocation
    to ignore self._project_root, Slice 3H.2's reorder becomes
    decorative and the bt-2026-05-25-082441 trap reopens."""
    src = INTERACTIVE_REPAIR_FILE.read_text()
    assert "cwd=str(self._project_root)" in src, (
        "InteractiveRepair subprocess no longer uses "
        "cwd=str(self._project_root) — Slice 3H.2's downstream contract "
        "is broken. The construction-time override is now ignored."
    )


def test_spine_interactive_repair_loop_stores_project_root() -> None:
    """End-to-end import + construction test — InteractiveRepairLoop
    stores the passed ``project_root`` as ``self._project_root`` and
    that's what the subprocess uses."""
    from backend.core.ouroboros.governance.interactive_repair import (
        InteractiveRepairLoop,
    )
    from pathlib import Path as _P

    class _StubProvider:
        async def plan(self, prompt, deadline):  # noqa: D401
            return "no_op"

    test_root = _P("/some/test/worktree/instance_x")
    loop = InteractiveRepairLoop(
        provider=_StubProvider(),
        project_root=test_root,
    )
    assert loop._project_root == test_root, (
        f"InteractiveRepairLoop did not store project_root verbatim: "
        f"expected {test_root}, got {loop._project_root}"
    )


def test_spine_repair_root_resolution_pure_logic() -> None:
    """Pure-logic test of the Slice 3H.2 resolution order — the
    overridden ``_repair_root`` (the worktree) flows to the
    constructor, NOT the legacy ``project_root``."""
    import json
    import tempfile
    from pathlib import Path as _P
    from backend.core.ouroboros.governance.operation_advisor import (
        resolve_envelope_repo_root,
    )

    with tempfile.TemporaryDirectory() as proj, \
         tempfile.TemporaryDirectory() as wt_base:
        worktree = _P(wt_base) / "instance_ansible"
        worktree.mkdir()
        evidence_json = json.dumps({"repo_root": str(worktree)})

        # Mirror the Slice 3H.2 reorder
        _repair_root: _P = _P(proj)  # default
        _wt_override = resolve_envelope_repo_root(
            evidence_json,
            project_root=_P(proj),
            extra_allowlist=(_P(wt_base).resolve(),),
        )
        if _wt_override is not None:
            _repair_root = _wt_override

        # Constructor argument = _repair_root (not _P(proj))
        constructor_arg = _repair_root
        assert constructor_arg == worktree.resolve(), (
            f"Slice 3H.2 reorder didn't apply: constructor would "
            f"receive {constructor_arg}, expected {worktree.resolve()}"
        )
        assert constructor_arg != _P(proj), (
            "Constructor would still get JARVIS root — Slice 3H.2 "
            "reorder broken."
        )


def test_spine_legacy_fallback_when_no_envelope_override() -> None:
    """When no envelope override is present (non-SWE-Bench-Pro op),
    the legacy ``orch._config.project_root`` path is preserved —
    Slice 3H.2 is additive only."""
    import json
    import tempfile
    from pathlib import Path as _P
    from backend.core.ouroboros.governance.operation_advisor import (
        resolve_envelope_repo_root,
    )

    with tempfile.TemporaryDirectory() as proj:
        # Empty evidence JSON → resolver returns None → fallback path
        _repair_root: _P = _P(proj)  # default
        _wt_override = resolve_envelope_repo_root(
            "",  # empty evidence
            project_root=_P(proj),
        )
        if _wt_override is not None:
            _repair_root = _wt_override

        # Legacy: constructor gets the project_root from config
        assert _repair_root == _P(proj), (
            "Legacy fallback broken — _repair_root should equal "
            "project_root when no envelope override"
        )

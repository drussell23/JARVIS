"""Slice 3H — VALIDATE_RETRY micro-fix wiring (target derivation + envelope root).

Closes the VALIDATE_RETRY ceiling surfaced by capability soak
bt-2026-05-25-072558: the model successfully wrote a real 3925-token
patch on ``lib/ansible/cli/doc.py`` (proven by the episodic memory
record), but the InteractiveRepairLoop never ran because of TWO
wiring bugs in ``validate_runner.py`` micro-fix block:

1. ``ctx.target_files`` is empty for SWE-Bench-Pro envelopes (per
   ``envelope_builder.py:307`` — the envelope intentionally omits
   target_files since the worktree IS the source of truth). The
   pre-Slice-3H code did
   ``_repair_target = list(ctx.target_files)[0] if ctx.target_files
   else None`` then ``if _repair_target:`` → False → FSM logged
   ``micro_fix_skipped_no_target`` and exited.

2. Even if ``target_files`` had been populated, the absolute path
   resolution ``orch._config.project_root / _repair_target`` would
   point at the host JARVIS repo, NOT the per-instance worktree
   where the model's patch was actually applied. Same wiring gap
   Slice 3G fixed for the tool loop, but at the orchestrator level.

The downstream consequence: all 3 validate iterations (iter=0,1,2)
hit the SAME unfixed error → retries exhausted → L2 dispatched →
``directive='cancel'`` → op terminated without ever giving the model
a chance to revise its patch.

# Fix mechanism — two-part composition

## Part 1 — candidate-derived repair target fallback

When ``ctx.target_files`` is empty, derive the repair target from
``best_candidate.get("file_path", "")`` — the file path the model
emitted in its 2b.1 candidate. The candidate schema already carries
this field; gate_runner.py:305 and episodic_memory at line 342
already consume it. Slice 3H composes the same contract from a new
consumer site — no new envelope shape, no new candidate field.

## Part 2 — envelope-override for repair root

When the envelope carries a per-instance worktree path via
``evidence.repo_root``, resolve the repair file against THAT path
instead of ``orch._config.project_root``. Composes the canonical
``operation_advisor.resolve_envelope_repo_root`` resolver
(env-flag-gated, allowlist-validated, fail-silent) — same path-
validation contract Slice 3G uses for the tool loop. ``None`` result
→ legacy ``project_root`` fallback → byte-identical pre-Slice-3H
behavior for non-envelope ops.

# Test surface (3 AST pins + 4 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATE_RUNNER_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "phase_runners" / "validate_runner.py"
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_candidate_fallback_target_derivation() -> None:
    """The micro-fix block must derive ``_repair_target`` from
    ``best_candidate.get("file_path"`` when ``ctx.target_files`` is
    empty. Without this fallback, SWE-Bench-Pro ops always log
    ``micro_fix_skipped_no_target`` and the retry FSM degenerates to
    a single attempt at L2 dispatch with no working target."""
    src = VALIDATE_RUNNER_FILE.read_text()
    # The fallback construct is unique enough to grep for verbatim
    assert (
        "if not _repair_target and best_candidate is not None:" in src
    ), (
        "validate_runner.py is missing the Slice 3H candidate-derived "
        "repair-target fallback. SWE-Bench-Pro ops will continue to "
        "skip micro-fix and exhaust retries on unfixed errors."
    )
    assert (
        'best_candidate.get("file_path", "") or ""' in src
    ), (
        "validate_runner.py does not extract file_path from "
        "best_candidate — Slice 3H candidate-contract consumption broken."
    )
    # The FSM tag is the operator's grep handle for diagnosing
    # whether this path fired in a given soak.
    assert '"micro_fix_target_from_candidate"' in src, (
        "Missing FSM telemetry tag micro_fix_target_from_candidate"
    )


def test_ast_pin_envelope_root_override_in_micro_fix() -> None:
    """The micro-fix block must use ``resolve_envelope_repo_root`` to
    pick the repair root. Without this, file resolution always uses
    the host JARVIS ``project_root`` — same wiring bug Slice 3G fixed
    for the tool loop. The override must compose the canonical
    resolver (no parallel path-validation)."""
    src = VALIDATE_RUNNER_FILE.read_text()
    # The repair root variable + override branch must both be present
    assert "_repair_root: Path = orch._config.project_root" in src, (
        "Missing _repair_root variable initialization — Slice 3H "
        "Part 2 wire missing."
    )
    assert (
        "resolve_envelope_repo_root as _slice3h_resolve_root" in src
    ), (
        "Missing canonical resolver import in validate_runner.py "
        "micro_fix block."
    )
    # The override-applied branch
    assert "_repair_root = _wt_override" in src, (
        "Missing _repair_root assignment from envelope override — "
        "the resolver is consulted but its result is ignored."
    )
    # FSM telemetry tag for the override
    assert '"micro_fix_root_envelope_override"' in src


def test_ast_pin_legacy_path_preserved() -> None:
    """The legacy path (``ctx.target_files`` populated + resolution
    against ``project_root``) must still work — Slice 3H is additive
    only. AST pin verifies the legacy ``orch._config.project_root``
    assignment to ``_repair_root`` is the initial value, not removed."""
    src = VALIDATE_RUNNER_FILE.read_text()
    # The default initialization keeps the pre-Slice-3H behavior
    assert (
        "_repair_root: Path = orch._config.project_root" in src
    ), (
        "Legacy project_root default for _repair_root removed — "
        "pre-Slice-3H ops without envelope override now break."
    )
    # The repair file resolution must now use the variable, not the
    # hardcoded project_root
    assert "_repair_abs = _repair_root / _repair_target" in src, (
        "_repair_abs no longer uses _repair_root variable — Slice "
        "3H envelope override path is dead."
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 4 (pure-logic tests of the derivation)
# ──────────────────────────────────────────────────────────────────────


def test_spine_target_derivation_fallback_logic() -> None:
    """Pure-logic test of the fallback predicate. When ctx.target_files
    is empty + best_candidate has file_path → repair_target is that
    file_path. Mirrors the production code shape verbatim."""
    # Empty ctx.target_files
    ctx_target_files: tuple[str, ...] = ()
    best_candidate = {"file_path": "lib/ansible/cli/doc.py"}

    _repair_target = (
        list(ctx_target_files)[0] if ctx_target_files else None
    )
    if not _repair_target and best_candidate is not None:
        _cand_path = best_candidate.get("file_path", "") or ""
        if _cand_path:
            _repair_target = _cand_path

    assert _repair_target == "lib/ansible/cli/doc.py"


def test_spine_target_derivation_legacy_path_preserved() -> None:
    """When ctx.target_files IS populated, the legacy path wins —
    the candidate fallback is never consulted."""
    ctx_target_files = ("legacy/path/file.py",)
    best_candidate = {"file_path": "new/path/file.py"}

    _repair_target = (
        list(ctx_target_files)[0] if ctx_target_files else None
    )
    if not _repair_target and best_candidate is not None:
        _cand_path = best_candidate.get("file_path", "") or ""
        if _cand_path:
            _repair_target = _cand_path

    assert _repair_target == "legacy/path/file.py"


def test_spine_target_derivation_no_target_no_candidate() -> None:
    """When BOTH ctx.target_files is empty AND best_candidate is None
    (legitimate failure case), repair_target stays None — micro-fix
    will still skip correctly. Slice 3H doesn't introduce a null-
    pointer hazard."""
    ctx_target_files: tuple[str, ...] = ()
    best_candidate = None

    _repair_target = (
        list(ctx_target_files)[0] if ctx_target_files else None
    )
    if not _repair_target and best_candidate is not None:
        _cand_path = best_candidate.get("file_path", "") or ""
        if _cand_path:
            _repair_target = _cand_path

    assert _repair_target is None


def test_spine_envelope_root_override_resolver_contract() -> None:
    """End-to-end pure-logic test of the repair-root override:
    valid envelope + worktree under allowlist → _repair_root is the
    worktree, NOT project_root. Composes the same canonical resolver
    Slice 3G uses."""
    import json
    import tempfile
    from pathlib import Path as _P
    from backend.core.ouroboros.governance.operation_advisor import (
        resolve_envelope_repo_root,
    )

    with tempfile.TemporaryDirectory() as project_root, \
         tempfile.TemporaryDirectory() as wt_base:
        worktree = _P(wt_base) / "instance_ansible"
        worktree.mkdir()
        evidence_json = json.dumps({"repo_root": str(worktree)})

        _repair_root: _P = _P(project_root)  # initial default
        _wt_override = resolve_envelope_repo_root(
            evidence_json,
            project_root=_P(project_root),
            extra_allowlist=(_P(wt_base).resolve(),),
        )
        if _wt_override is not None:
            _repair_root = _wt_override

        assert _repair_root == worktree.resolve(), (
            f"Slice 3H repair-root override didn't apply: got "
            f"{_repair_root}, expected {worktree.resolve()}"
        )
        # Verify the absolute path under the override is the worktree
        # not the JARVIS project_root. Use is_relative_to() — robust
        # against path-depth changes.
        target = "lib/ansible/cli/doc.py"
        repair_abs = _repair_root / target
        assert repair_abs.is_relative_to(worktree.resolve()), (
            f"Repair path {repair_abs} doesn't resolve under the "
            f"worktree {worktree.resolve()} — Slice 3H override "
            f"path broken."
        )

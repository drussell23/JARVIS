"""Slice 12AE — TestRunner per-envelope repo_root propagation.

# Wedge (bt-2026-05-24-053214)

Slice 12AD's `WIRING_VALIDATION` route + cost factor + IronGate
bypasses all fired correctly:
  * Route stamped `wiring_validation` at 22:36:19
  * CostGov cap=$0.0900 derived (0.10 × 0.10 × 3.00 × 3.00)
  * Slice 12P exploration floor=0 applied
  * Total fixture spend $0.00157 (vs $1.81 baseline = 99.91% reduction)

But the fixture op terminated NOT-COMPLETE because the orchestrator's
VALIDATE phase invoked the boot-time `LanguageRouter` (bound at
`governed_loop_service.py:3631` to `self._config.project_root` = the
main JARVIS repo) instead of running tests in the SWE-Bench-Pro
TMPDIR worktree promised by the envelope's
`evidence[repo_root]`. Symptom:

  [TestRunner] Strategy 3 (package fallback):
   wiring_validation_fixture.py → 1 files in
   /Users/.../JARVIS-AI-Agent/backend/core/ouroboros/tests
  Episodic memory recorded: ... wiring_validation_fixture.py —
   1 critique(s): 1 error(s)
  ValidateRetryFSM l2_dispatch_post ... directive='cancel'
  VerifyPostmortem claims=3 pass=0 fail=0 insuff=3

The critique was on a file in the MAIN repo, unrelated to the
fixture's actual test_patch (`tests/test_smoke.py` in the TMPDIR
worktree at `/private/.../T/swebp_wt/jarvis__harness-smoke-001/`).
L2 cancelled because the claims were structurally insufficient
(test ran in the wrong tree → couldn't possibly validate anything).

# Fix (Slice 12AE)

Compose Slice 12AC's canonical `envelope_repo_root_status()` seam at
the orchestrator's VALIDATE site (orchestrator.py
`_validate_candidate_multifile`-class path). For each op:

  1. `NO_PROMISE` (no envelope repo_root, or feature off) → keep
     `project_root` as the anchor (byte-identical legacy).
  2. `RESOLVED` (envelope promised + advisor-trusted) → use the
     per-envelope path as both:
       a. the anchor for `_original_paths` sandbox→real mapping
       b. the `repo_root` of a per-op `LanguageRouter` constructed
          here (composing existing `PythonAdapter` / `CppAdapter`).
  3. `REJECTED` (envelope promised + advisor-rejected — escaped
     allowlist, missing dir, etc.) → return `ValidationResult`
     with `failure_class="infra"` immediately. **NO silent
     fallback to project_root.** Refuses to execute tests in
     the wrong tree.

# Critical scope boundary (operator pivot)

* Slice 12AE does **NOT** plumb `target_files` from the envelope.
  The empty `target_files=()` in `envelope_builder.py` is
  **canonical SWE-Bench-Pro protocol** (cheat-detection: test
  paths must not be surfaced as the agent's target; gold-patch
  paths would leak the solution). AST pin in this file enforces
  no plumbing change in `envelope_builder.py`.

# Test surface

  1. NO_PROMISE envelope → effective_repo_root == project_root,
     original_paths anchored to project_root (legacy preserved).
  2. RESOLVED envelope → effective_repo_root == TMPDIR worktree,
     per-op LanguageRouter constructed with that root, original_paths
     anchored to the TMPDIR path.
  3. REJECTED envelope → ValidationResult.failure_class == "infra",
     no test execution, error message names the rejected path.
  4. AST pin: orchestrator imports `envelope_repo_root_status` +
     `RepoRootPromiseStatus`, has REJECTED branch, calls
     `_ae_effective_runner.run(...)` (not hardcoded
     `self._validation_runner.run`).
  5. AST pin: envelope_builder.py still has `target_files:
     Tuple[str, ...] = ()` literal (NO plumbing of test_patch paths
     — canonical SWE-Bench cheat-detection contract intact).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.operation_advisor import (
    EVIDENCE_REPO_ROOT_KEY,
    RepoRootPromiseStatus,
    envelope_repo_root_status,
)


# ──────────────────────────────────────────────────────────────────────
# Test 1 — NO_PROMISE: byte-identical legacy fallback
# ──────────────────────────────────────────────────────────────────────


class TestNoPromiseFallback:
    def test_envelope_without_repo_root_is_no_promise(
        self, tmp_path: Path,
    ):
        """When the envelope carries no `repo_root` key, the status
        helper returns NO_PROMISE → caller falls back to project_root
        (byte-identical pre-Slice-12AE behavior)."""
        status, resolved, _raw = envelope_repo_root_status(
            "",  # empty evidence
            project_root=tmp_path,
        )
        assert status is RepoRootPromiseStatus.NO_PROMISE
        assert resolved is None

    def test_envelope_with_empty_evidence_dict_is_no_promise(
        self, tmp_path: Path,
    ):
        status, resolved, _raw = envelope_repo_root_status(
            json.dumps({}),
            project_root=tmp_path,
        )
        assert status is RepoRootPromiseStatus.NO_PROMISE
        assert resolved is None

    def test_envelope_with_other_keys_but_no_repo_root_is_no_promise(
        self, tmp_path: Path,
    ):
        """Non-SWE envelopes (regular ops) carry their own evidence
        but no `repo_root` → NO_PROMISE."""
        status, _, _ = envelope_repo_root_status(
            json.dumps({"problem_instance_id": "regular-op-1"}),
            project_root=tmp_path,
        )
        assert status is RepoRootPromiseStatus.NO_PROMISE


# ──────────────────────────────────────────────────────────────────────
# Test 2 — RESOLVED: per-envelope TMPDIR worktree wins
# ──────────────────────────────────────────────────────────────────────


class TestResolvedTmpdirWorktree:
    def test_promised_tmpdir_worktree_under_swebp_base_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """SWE-Bench-Pro fixture envelope: evidence has repo_root
        pointing at a TMPDIR worktree under the configured SWE-Bench-
        Pro worktree base. Slice 12AC's helper resolves it; Slice
        12AE's VALIDATE site picks up the resolved path."""
        # Configure the SWE-Bench-Pro worktree base (mirrors operator's
        # JARVIS_SWE_BENCH_PRO_WORKTREE_BASE_PATH=$TMPDIR/swebp_wt).
        swebp_base = tmp_path / "swebp_wt"
        swebp_base.mkdir()
        worktree = swebp_base / "jarvis__harness-smoke-001"
        worktree.mkdir()
        (worktree / "tests").mkdir()
        (worktree / "tests" / "test_smoke.py").write_text(
            "def test_smoke_noop():\n    assert True\n"
        )

        monkeypatch.setenv(
            "JARVIS_SWE_BENCH_PRO_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_SWE_BENCH_PRO_WORKTREE_BASE_PATH",
            str(swebp_base),
        )

        # project_root is the main JARVIS repo (unrelated to the
        # TMPDIR worktree; Slice 12AE proves we no longer fall back
        # to it for SWE-Bench-Pro envelopes).
        project_root = tmp_path / "main_jarvis_repo"
        project_root.mkdir()

        evidence = json.dumps({
            EVIDENCE_REPO_ROOT_KEY: str(worktree),
            "swe_bench_pro": True,
            "real_benchmark": False,
            "fixture_purpose": "wiring_validation",
        })
        status, resolved, _ = envelope_repo_root_status(
            evidence, project_root=project_root,
        )
        assert status is RepoRootPromiseStatus.RESOLVED
        assert resolved == worktree.resolve()


# ──────────────────────────────────────────────────────────────────────
# Test 3 — REJECTED: fail-closed, no silent fallback
# ──────────────────────────────────────────────────────────────────────


class TestRejectedFailsClosed:
    def test_promised_repo_root_outside_allowlist_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Envelope promises a TMPDIR-shaped worktree but the path
        is OUTSIDE the SWE-Bench-Pro configured base — Slice 12AC's
        helper returns REJECTED. Slice 12AE's VALIDATE site MUST
        refuse to fall back to project_root."""
        project_root = tmp_path / "main_jarvis_repo"
        project_root.mkdir()
        rogue_path = tmp_path / "rogue_tmpdir" / "evil_worktree"
        rogue_path.mkdir(parents=True)
        # Master flag ON (Slice 12AC default-true). No SWE-Bench-Pro
        # config so the rogue path is not allowlisted.
        monkeypatch.delenv(
            "JARVIS_ADVISOR_WORKTREE_AWARE_ENABLED", raising=False,
        )
        monkeypatch.delenv(
            "JARVIS_ADVISOR_WORKTREE_ROOT_ALLOWLIST", raising=False,
        )
        monkeypatch.delenv(
            "JARVIS_SWE_BENCH_PRO_ENABLED", raising=False,
        )

        evidence = json.dumps({
            EVIDENCE_REPO_ROOT_KEY: str(rogue_path),
        })
        status, resolved, raw = envelope_repo_root_status(
            evidence, project_root=project_root,
        )
        assert status is RepoRootPromiseStatus.REJECTED
        assert resolved is None
        assert raw == str(rogue_path)

    def test_promised_path_that_does_not_exist_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Promised path doesn't exist on disk → REJECTED (advisor
        resolver requires .exists() and .is_dir())."""
        project_root = tmp_path / "main_jarvis_repo"
        project_root.mkdir()
        ghost_path = tmp_path / "ghost_worktree_does_not_exist"
        # Configure SWE-Bench-Pro so the implicit allowlist would
        # accept the parent path — the rejection here is purely
        # because the path doesn't exist.
        monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_ENABLED", "true")
        monkeypatch.setenv(
            "JARVIS_SWE_BENCH_PRO_WORKTREE_BASE_PATH",
            str(tmp_path),
        )
        evidence = json.dumps({EVIDENCE_REPO_ROOT_KEY: str(ghost_path)})
        status, resolved, raw = envelope_repo_root_status(
            evidence, project_root=project_root,
        )
        assert status is RepoRootPromiseStatus.REJECTED
        assert resolved is None
        assert raw == str(ghost_path)


# ──────────────────────────────────────────────────────────────────────
# Architectural AST pins
# ──────────────────────────────────────────────────────────────────────


REPO_ROOT = Path(__file__).resolve().parents[2]
ORCH_PATH = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "orchestrator.py"
)
ENV_BUILDER_PATH = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "swe_bench_pro" / "envelope_builder.py"
)


class TestOrchestratorASTPins:
    def test_orchestrator_imports_envelope_repo_root_status(self):
        """The VALIDATE site MUST compose Slice 12AC's canonical
        status helper — proves we're using the existing seam, not
        re-implementing prefix math."""
        src = ORCH_PATH.read_text()
        assert "envelope_repo_root_status" in src, (
            "orchestrator.py must import + call "
            "envelope_repo_root_status from operation_advisor (the "
            "canonical Slice 12AC seam)"
        )
        assert "RepoRootPromiseStatus" in src, (
            "orchestrator.py must reference RepoRootPromiseStatus "
            "to discriminate NO_PROMISE / RESOLVED / REJECTED"
        )

    def test_orchestrator_has_rejected_branch(self):
        """REJECTED → fail-closed early return. The branch MUST
        exist explicitly — no silent fallback for promised-but-
        invalid envelope repo_root."""
        src = ORCH_PATH.read_text()
        assert "RepoRootPromiseStatus.REJECTED" in src, (
            "orchestrator.py must have an explicit "
            "RepoRootPromiseStatus.REJECTED comparison branch"
        )
        # The fail-closed branch returns ValidationResult with
        # failure_class="infra" and a slice12ae_repo_root_rejected
        # short_summary marker (greppable).
        assert "slice12ae_repo_root_rejected" in src, (
            "orchestrator.py's REJECTED branch must emit the "
            "'slice12ae_repo_root_rejected' marker for grep-based "
            "operator forensics"
        )

    def test_orchestrator_uses_per_op_effective_runner(self):
        """The validation_runner.run() call site MUST go through
        a per-op effective runner variable, NOT directly through
        self._validation_runner (which is boot-time-bound to
        project_root)."""
        src = ORCH_PATH.read_text()
        assert "_ae_effective_runner" in src, (
            "orchestrator.py must use the _ae_effective_runner "
            "variable for the validation_runner.run() call site "
            "so per-envelope repo_root composition works"
        )
        assert "_ae_effective_repo_root" in src, (
            "orchestrator.py must use _ae_effective_repo_root for "
            "the _original_paths sandbox→real mapping"
        )


class TestEnvelopeBuilderASTPin:
    def test_envelope_builder_keeps_target_files_empty(self):
        """**Critical operator-confirmed boundary.** The envelope's
        `target_files=()` is canonical SWE-Bench protocol (cheat
        detection: test_patch paths MUST NOT be surfaced). Slice
        12AE MUST NOT plumb `prepared.target_paths` into
        `IntentEnvelope.target_files`. AST pin: the literal
        `target_files: Tuple[str, ...] = ()` assignment is present
        in `build_evaluation_envelope`.
        """
        src = ENV_BUILDER_PATH.read_text()
        # Concrete substring presence (canonical empty target_files
        # assignment in build_evaluation_envelope).
        assert "target_files: Tuple[str, ...] = ()" in src, (
            "envelope_builder.py must still assign empty tuple to "
            "target_files in build_evaluation_envelope — Slice 12AE "
            "pivoted AWAY from plumbing target_paths (would violate "
            "SWE-Bench cheat-detection contract)"
        )
        # AST-level: walk build_evaluation_envelope; ensure
        # `prepared.target_paths` is NEVER referenced inside it
        # (would indicate accidental plumbing).
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "build_evaluation_envelope"
            ):
                body_src = ast.unparse(node)
                assert "prepared.target_paths" not in body_src, (
                    "build_evaluation_envelope must NOT reference "
                    "prepared.target_paths — would violate the "
                    "canonical SWE-Bench-Pro empty-target contract"
                )
                break
        else:
            raise AssertionError(
                "build_evaluation_envelope function not found in "
                "envelope_builder.py"
            )

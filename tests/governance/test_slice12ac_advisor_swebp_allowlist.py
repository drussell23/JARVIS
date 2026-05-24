"""Slice 12AC — advisor allowed-prefix extension for SWE-Bench-Pro.

# Wedge (bt-2026-05-24-014841)

Phase-1 SWE-Bench-Pro wiring-validation fixture died in 5 seconds at
the orchestrator's pre-check with::

    [Orchestrator] FAIL-CLOSED op=...-cau: swebp_repo_root_rejected:
    promised isolated repo_root '/private/tmp/claude-501/swebp_wt/
    jarvis__harness-smoke-001' escaped the advisor allowed-prefix
    anchor — refusing silent fallback to the shared tree —
    POSTMORTEM (no shared-tree fallback)

The SWE-Bench-Pro soak runbook
(``docs/operations/swe_bench_pro_soak_runbook.md``) explicitly mandates
``JARVIS_SWE_BENCH_PRO_{WORKTREE_BASE,REPO_CACHE}_PATH`` under
``$TMPDIR`` for sandbox-restricted environments — but the
``operation_advisor`` allowed-prefix list was never told. Result:
runbook-vs-advisor contract violation, $0 spend, but no Slice 12AA-fix
empirical validation either.

# Fix

Extend ``_parse_allowlist_env()`` with a NEW helper
``_swe_bench_pro_implicit_allowlist()`` that composes THREE existing
canonical surfaces — no new env vars, no parallel path-reading, no
``/tmp`` or ``/private/tmp`` literals:

  * ``swe_bench_pro_enabled()`` (``JARVIS_SWE_BENCH_PRO_ENABLED``) —
    master-flag gate. Off → ``()``, byte-identical legacy behavior.
  * ``worktree_base_path()`` — env accessor
    (``JARVIS_SWE_BENCH_PRO_WORKTREE_BASE_PATH`` w/ default fallback).
  * ``repo_cache_path()`` — env accessor
    (``JARVIS_SWE_BENCH_PRO_REPO_CACHE_PATH`` w/ default fallback).

Each path is ``.resolve(strict=False)``-sanitized BEFORE allowlist
insertion so symlink + ``..`` traversal escapes are collapsed by the
same canonicalization the candidate path will undergo.

# Test surface (per operator spec)

  1. Env-configured TMPDIR SWE worktree path → ACCEPTED.
  2. Default in-repo SWE worktree path → ACCEPTED (unchanged).
  3. Sibling TMPDIR path NOT in SWE config → REJECTED.
  4. Master flag off → behavior matches legacy (no SWE-Bench-Pro
     paths leaked into allowlist).
  5. Unknown external worktree (``/etc``) → still REJECTED.

# Architectural pins

  * AST pin: ``_parse_allowlist_env`` calls
    ``_swe_bench_pro_implicit_allowlist``.
  * AST pin: ``_swe_bench_pro_implicit_allowlist`` does NOT
    hardcode any ``/tmp`` or ``/private/tmp`` literal.
  * AST pin: master-flag gate (``swe_bench_pro_enabled``) is
    referenced — no unconditional composition.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.governance.operation_advisor import (
    ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR,
    ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR,
    EVIDENCE_REPO_ROOT_KEY,
    _parse_allowlist_env,
    _swe_bench_pro_implicit_allowlist,
    resolve_envelope_repo_root,
)
from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    MASTER_FLAG_ENV_VAR as SWEBP_MASTER_FLAG_ENV_VAR,
)
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (  # noqa: E501
    REPO_CACHE_PATH_ENV_VAR as SWEBP_REPO_CACHE_PATH_ENV_VAR,
    WORKTREE_BASE_PATH_ENV_VAR as SWEBP_WORKTREE_BASE_PATH_ENV_VAR,
)


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset every env var Slice 12AC reads from. Each test then
    sets only what it needs — no cross-test bleed."""
    for var in (
        ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR,
        ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR,
        SWEBP_MASTER_FLAG_ENV_VAR,
        SWEBP_WORKTREE_BASE_PATH_ENV_VAR,
        SWEBP_REPO_CACHE_PATH_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """A scratch in-repo project_root."""
    root = tmp_path / "main_repo"
    root.mkdir()
    return root


def _evidence(repo_root: str) -> str:
    return json.dumps({EVIDENCE_REPO_ROOT_KEY: repo_root})


# ──────────────────────────────────────────────────────────────────────
# Operator scenario 1: env-configured TMPDIR worktree → ACCEPTED
# ──────────────────────────────────────────────────────────────────────


class TestEnvConfiguredTmpdirAccepted:
    def test_tmpdir_worktree_accepted_when_swebp_enabled(
        self,
        clean_env: None,
        project_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The bt-2026-05-24-014841 wedge in regression form: a
        worktree under the operator's configured TMPDIR base, with
        SWE-Bench-Pro enabled, MUST be accepted by the advisor."""
        tmpdir_wt_base = tmp_path / "swebp_wt"
        tmpdir_wt_base.mkdir()
        worktree = tmpdir_wt_base / "jarvis__harness-smoke-001"
        worktree.mkdir()

        monkeypatch.setenv(SWEBP_MASTER_FLAG_ENV_VAR, "true")
        monkeypatch.setenv(
            SWEBP_WORKTREE_BASE_PATH_ENV_VAR, str(tmpdir_wt_base),
        )

        resolved = resolve_envelope_repo_root(
            _evidence(str(worktree)), project_root=project_root,
        )
        assert resolved == worktree.resolve(), (
            f"TMPDIR worktree under configured SWE-Bench-Pro base "
            f"should be accepted; got {resolved!r}"
        )

    def test_tmpdir_repo_cache_accepted_when_swebp_enabled(
        self,
        clean_env: None,
        project_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The repo_cache half of the wedge — also covered by
        Slice 12AC since the runbook routes both base paths
        under TMPDIR together."""
        tmpdir_cache_base = tmp_path / "swebp_cache"
        tmpdir_cache_base.mkdir()
        repo = tmpdir_cache_base / "github.com_octocat_Hello-World"
        repo.mkdir()

        monkeypatch.setenv(SWEBP_MASTER_FLAG_ENV_VAR, "true")
        monkeypatch.setenv(
            SWEBP_REPO_CACHE_PATH_ENV_VAR, str(tmpdir_cache_base),
        )

        resolved = resolve_envelope_repo_root(
            _evidence(str(repo)), project_root=project_root,
        )
        assert resolved == repo.resolve()


# ──────────────────────────────────────────────────────────────────────
# Operator scenario 2: default in-repo SWE worktree → ACCEPTED
# ──────────────────────────────────────────────────────────────────────


class TestDefaultInRepoWorktreeAccepted:
    def test_default_in_repo_worktree_still_works_when_swebp_off(
        self,
        clean_env: None,
        project_root: Path,
    ) -> None:
        """Master flag OFF (legacy default) — in-repo worktrees
        still pass via the project_root anchor."""
        in_repo_wt = project_root / ".jarvis" / "swe_bench_pro" / "worktrees" / "inst-001"
        in_repo_wt.mkdir(parents=True)

        resolved = resolve_envelope_repo_root(
            _evidence(str(in_repo_wt)), project_root=project_root,
        )
        assert resolved == in_repo_wt.resolve()

    def test_default_in_repo_worktree_works_when_swebp_enabled_no_overrides(
        self,
        clean_env: None,
        project_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Master flag ON + no env overrides — the default
        ``.jarvis/swe_bench_pro/...`` paths still resolve under
        project_root via the existing anchor (Slice 12AC adds
        them to the allowlist too via the implicit composition,
        but the project_root anchor already covers them)."""
        in_repo_wt = project_root / ".jarvis" / "swe_bench_pro" / "worktrees" / "inst-002"
        in_repo_wt.mkdir(parents=True)
        monkeypatch.setenv(SWEBP_MASTER_FLAG_ENV_VAR, "true")

        resolved = resolve_envelope_repo_root(
            _evidence(str(in_repo_wt)), project_root=project_root,
        )
        assert resolved == in_repo_wt.resolve()


# ──────────────────────────────────────────────────────────────────────
# Operator scenario 3: sibling TMPDIR path → REJECTED
# ──────────────────────────────────────────────────────────────────────


class TestSiblingTmpdirRejected:
    def test_sibling_tmpdir_not_in_swebp_config_is_rejected(
        self,
        clean_env: None,
        project_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The bug-bait scenario: SWE-Bench-Pro is enabled and
        configured for ``$TMPDIR/swebp_wt``, but the envelope
        promises ``$TMPDIR/random_other_dir/...`` — the sibling
        path MUST be rejected. Slice 12AC must not blanket-allow
        ALL of TMPDIR."""
        configured = tmp_path / "swebp_wt"
        configured.mkdir()
        sibling = tmp_path / "random_other_dir" / "evil"
        sibling.mkdir(parents=True)

        monkeypatch.setenv(SWEBP_MASTER_FLAG_ENV_VAR, "true")
        monkeypatch.setenv(
            SWEBP_WORKTREE_BASE_PATH_ENV_VAR, str(configured),
        )

        resolved = resolve_envelope_repo_root(
            _evidence(str(sibling)), project_root=project_root,
        )
        assert resolved is None, (
            f"Sibling TMPDIR path NOT under any SWE-Bench-Pro "
            f"configured base must be rejected; got {resolved!r}"
        )

    def test_dot_dot_traversal_from_configured_path_collapses_to_sibling(
        self,
        clean_env: None,
        project_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Path traversal probe: ``$configured/../evil`` resolves
        BEFORE the allowlist check, so it collapses to a sibling
        of ``$configured``, which is NOT in the allowlist."""
        configured = tmp_path / "swebp_wt"
        configured.mkdir()
        traversal_target = tmp_path / "evil"
        traversal_target.mkdir()

        monkeypatch.setenv(SWEBP_MASTER_FLAG_ENV_VAR, "true")
        monkeypatch.setenv(
            SWEBP_WORKTREE_BASE_PATH_ENV_VAR, str(configured),
        )
        traversal = configured / ".." / "evil"
        resolved = resolve_envelope_repo_root(
            _evidence(str(traversal)), project_root=project_root,
        )
        assert resolved is None


# ──────────────────────────────────────────────────────────────────────
# Operator scenario 4: master flag unset → byte-identical legacy
# ──────────────────────────────────────────────────────────────────────


class TestMasterFlagOffPreservesBehavior:
    def test_swebp_off_does_not_add_paths_to_allowlist(
        self,
        clean_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """When SWE-Bench-Pro is OFF (default), the implicit
        allowlist returns ``()`` — even if path env vars are
        set, those values must NOT leak through to the
        advisor without the master gate."""
        monkeypatch.setenv(
            SWEBP_WORKTREE_BASE_PATH_ENV_VAR, str(tmp_path / "swebp_wt"),
        )
        monkeypatch.setenv(
            SWEBP_REPO_CACHE_PATH_ENV_VAR, str(tmp_path / "swebp_cache"),
        )
        # Master flag NOT set
        assert _swe_bench_pro_implicit_allowlist() == ()

    def test_swebp_off_tmpdir_envelope_still_rejected(
        self,
        clean_env: None,
        project_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: master flag OFF, envelope promises a
        TMPDIR path → the advisor still REJECTS it via the
        legacy project_root-only anchor."""
        tmpdir_wt = tmp_path / "swebp_wt" / "inst-001"
        tmpdir_wt.mkdir(parents=True)
        monkeypatch.setenv(
            SWEBP_WORKTREE_BASE_PATH_ENV_VAR, str(tmpdir_wt.parent),
        )
        # Master flag NOT set.
        resolved = resolve_envelope_repo_root(
            _evidence(str(tmpdir_wt)), project_root=project_root,
        )
        assert resolved is None, (
            "Without SWE-Bench-Pro master flag, advisor must "
            "preserve legacy fail-closed on TMPDIR paths"
        )


# ──────────────────────────────────────────────────────────────────────
# Operator scenario 5: unknown external worktree → still REJECTED
# ──────────────────────────────────────────────────────────────────────


class TestUnknownExternalRejected:
    def test_etc_rejected_even_when_swebp_enabled(
        self,
        clean_env: None,
        project_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Defense in depth: ``/etc`` (a real system dir, exists)
        is NEVER under any SWE-Bench-Pro configured base —
        must stay rejected even with the master flag on."""
        monkeypatch.setenv(SWEBP_MASTER_FLAG_ENV_VAR, "true")
        resolved = resolve_envelope_repo_root(
            _evidence("/etc"), project_root=project_root,
        )
        assert resolved is None

    def test_unrelated_real_dir_rejected_when_swebp_enabled(
        self,
        clean_env: None,
        project_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A real directory that isn't under project_root, isn't
        under explicit ALLOWLIST, and isn't under any
        SWE-Bench-Pro configured base → rejected."""
        unrelated = tmp_path / "unrelated_workspace" / "some_project"
        unrelated.mkdir(parents=True)
        monkeypatch.setenv(SWEBP_MASTER_FLAG_ENV_VAR, "true")
        # No SWE-Bench-Pro path env vars set — defaults are
        # in-repo, so this TMPDIR sibling is unreachable.
        resolved = resolve_envelope_repo_root(
            _evidence(str(unrelated)), project_root=project_root,
        )
        assert resolved is None


# ──────────────────────────────────────────────────────────────────────
# Architectural pins — composition + anti-hardcode
# ──────────────────────────────────────────────────────────────────────


ADVISOR_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "operation_advisor.py"
)


def _load_advisor_ast() -> ast.Module:
    return ast.parse(ADVISOR_PATH.read_text())


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in operation_advisor.py")


class TestArchitecturalPins:
    def test_parse_allowlist_env_calls_swebp_implicit_helper(self) -> None:
        """``_parse_allowlist_env`` MUST compose
        ``_swe_bench_pro_implicit_allowlist``. Drift would silently
        regress the wedge."""
        tree = _load_advisor_ast()
        parse_fn = _find_function(tree, "_parse_allowlist_env")
        src = ast.unparse(parse_fn)
        assert "_swe_bench_pro_implicit_allowlist" in src, (
            "_parse_allowlist_env must call "
            "_swe_bench_pro_implicit_allowlist for Slice 12AC"
        )

    def test_implicit_helper_consults_master_flag(self) -> None:
        """No unconditional composition — the master-flag gate
        (``swe_bench_pro_enabled``) MUST be present in the helper."""
        tree = _load_advisor_ast()
        helper = _find_function(
            tree, "_swe_bench_pro_implicit_allowlist",
        )
        src = ast.unparse(helper)
        assert "swe_bench_pro_enabled" in src, (
            "Implicit allowlist helper must consult the "
            "swe_bench_pro_enabled master-flag accessor"
        )

    def test_implicit_helper_does_not_hardcode_tmp_paths(self) -> None:
        """Operator binding: 'must be env/config-driven, not
        hardcoded /tmp or /private/tmp'. Scan the helper's AST
        for any Constant node whose string value contains those
        literals."""
        tree = _load_advisor_ast()
        helper = _find_function(
            tree, "_swe_bench_pro_implicit_allowlist",
        )
        forbidden_substrings = ("/tmp", "/private/tmp", "/var/tmp")
        for node in ast.walk(helper):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, str):
                continue
            for forbidden in forbidden_substrings:
                assert forbidden not in node.value, (
                    f"Forbidden hardcoded path fragment "
                    f"{forbidden!r} in literal {node.value!r} — "
                    f"Slice 12AC must be env/config-driven"
                )

    def test_implicit_helper_resolves_before_compare(self) -> None:
        """Defense against symlink + path-traversal escapes:
        the helper MUST call ``.resolve(`` on its accessor
        outputs before returning. The ``_is_under`` comparison
        at the call site assumes resolved paths on both sides."""
        tree = _load_advisor_ast()
        helper = _find_function(
            tree, "_swe_bench_pro_implicit_allowlist",
        )
        src = ast.unparse(helper)
        assert ".resolve(" in src, (
            "Implicit allowlist helper must sanitize accessor "
            "paths via .resolve() before allowlist insertion"
        )

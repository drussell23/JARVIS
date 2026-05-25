"""Slice 3G — envelope-override for per-op worktree repo_root.

Closes bt-2026-05-25-060538 NOOP wiring gap surfaced by the autopsy:
the SWE-Bench-Pro harness creates per-instance worktrees at
``/tmp/swebp_wt/instance_*/`` containing the ACTUAL problem code, and
stamps the worktree path on the envelope as
``evidence.repo_root``. But ``ToolLoopCoordinator.run()`` was
unconditionally resolving the tool loop's working directory via
``policy.repo_root_for(ctx.primary_repo)``, which only knew about the
host JARVIS repo — so the model's ``read_file`` / ``glob_files`` /
``list_dir`` searched the wrong codebase.

The model correctly emitted ``2b.1-noop`` with verbatim reason::

  "The target file lib/ansible/cli/doc.py does not exist in this
  repository. This is the JARVIS Trinity AI Ecosystem codebase,
  not an Ansible repository."

That is intellectually-honest model behavior identifying our wiring
bug. The capability infrastructure is fine; the harness wiring needed
to plumb the envelope's worktree path into the tool executor.

# Fix mechanism — surgical envelope-override path

Two-site change:

* ``tool_executor.py::ToolLoopCoordinator.run()`` accepts a new
  ``repo_root_override: Optional[Path] = None`` kwarg. When set, the
  resolved ``repo_root`` honors it INSTEAD of the
  ``policy.repo_root_for(repo)`` fallback. ``None`` preserves
  byte-identical legacy behavior.

* ``providers.py`` (ClaudeProvider site + PrimeProvider site)
  extracts the override from ``ctx.intake_evidence_json`` via the
  pre-existing canonical resolver
  ``operation_advisor.resolve_envelope_repo_root`` (env-flag-gated,
  allowlist-validated, fail-silent). Composes the same path-validation
  contract the Advisor already uses — no parallel resolver, no
  shared-state mutation.

The path validation in ``resolve_envelope_repo_root`` is the security
guarantee: every override must resolve under ``project_root`` or an
explicitly-allowed prefix; symlink escapes are defeated via
``Path.resolve(strict=False)``; missing/wrong-type evidence is
silently treated as "no override". The tool executor itself does NO
validation — the trust is anchored at the resolver.

# Test surface (3 AST pins + 6 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_EXECUTOR_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance" / "tool_executor.py"
)
PROVIDERS_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance" / "providers.py"
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_tool_loop_run_accepts_repo_root_override() -> None:
    """``ToolLoopCoordinator.run()`` must declare a
    ``repo_root_override`` parameter. Without it the provider can't
    pass the envelope's worktree path through, and the
    bt-2026-05-25-060538 NOOP wiring trap is open."""
    tree = _parse(TOOL_EXECUTOR_FILE)
    coordinator = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ToolLoopCoordinator":
            coordinator = node
            break
    assert coordinator is not None, "ToolLoopCoordinator not found"
    run_method = None
    for sub in coordinator.body:
        if isinstance(sub, ast.AsyncFunctionDef) and sub.name == "run":
            run_method = sub
            break
    assert run_method is not None, "ToolLoopCoordinator.run not found"
    arg_names = [a.arg for a in run_method.args.args] + [
        a.arg for a in run_method.args.kwonlyargs
    ]
    assert "repo_root_override" in arg_names, (
        "ToolLoopCoordinator.run signature missing repo_root_override "
        "kwarg — Slice 3G wiring incomplete."
    )


def test_ast_pin_tool_loop_run_honors_override_in_body() -> None:
    """The ``run()`` body must reference ``repo_root_override`` AND
    assign to ``repo_root`` based on it. Without the body wiring the
    parameter is decorative — the policy.repo_root_for(repo) fallback
    still wins."""
    tree = _parse(TOOL_EXECUTOR_FILE)
    coordinator = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ToolLoopCoordinator":
            coordinator = node
            break
    assert coordinator is not None
    run_method = None
    for sub in coordinator.body:
        if isinstance(sub, ast.AsyncFunctionDef) and sub.name == "run":
            run_method = sub
            break
    assert run_method is not None
    body_src = ast.unparse(run_method)
    assert "repo_root_override" in body_src, (
        "run() body does not reference repo_root_override"
    )
    # The override branch MUST assign to repo_root — otherwise the
    # parameter exists but doesn't influence behavior.
    assert "repo_root = Path(repo_root_override)" in body_src, (
        "run() body does not assign repo_root from override — Slice 3G "
        "wiring is decorative; legacy fallback still wins."
    )


def test_ast_pin_providers_compose_resolve_envelope_repo_root() -> None:
    """Both provider call sites (ClaudeProvider + PrimeProvider) must
    compose ``resolve_envelope_repo_root`` AND pass
    ``repo_root_override=`` to ``_tool_loop.run()``. Without this the
    coordinator's new kwarg is never set — Slice 3G is dead."""
    src = PROVIDERS_FILE.read_text()
    # Both sites use the same import shape
    assert (
        "resolve_envelope_repo_root as _resolve_evidence_root" in src
    ), (
        "providers.py is missing the canonical resolver import — "
        "Slice 3G providers wiring incomplete."
    )
    # Both call sites must pass repo_root_override
    assert (
        src.count("repo_root_override=_evidence_override") >= 2
    ), (
        "providers.py has fewer than 2 sites passing "
        "repo_root_override — expected exactly 2 (Claude + Prime)."
    )
    # Both sites must consume intake_evidence_json
    assert src.count("intake_evidence_json") >= 2, (
        "providers.py missing intake_evidence_json consumption at "
        "both call sites."
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 6
# ──────────────────────────────────────────────────────────────────────


def test_spine_resolve_envelope_repo_root_handles_swebench_evidence() -> None:
    """The canonical resolver accepts the SWE-Bench-Pro envelope shape
    (evidence dict with ``repo_root`` key) and returns the resolved
    Path when the worktree exists under an allowed prefix."""
    import json
    import tempfile
    from backend.core.ouroboros.governance.operation_advisor import (
        resolve_envelope_repo_root,
    )
    with tempfile.TemporaryDirectory() as tmp_root:
        # Build the SWE-Bench-Pro envelope shape
        worktree = Path(tmp_root) / "swebp_wt" / "instance_ansible"
        worktree.mkdir(parents=True)
        evidence_json = json.dumps({"repo_root": str(worktree)})
        # project_root = the host JARVIS repo (or here, the temp root
        # so the allowlist accepts the worktree)
        result = resolve_envelope_repo_root(
            evidence_json,
            project_root=Path(tmp_root),
        )
        assert result is not None, (
            "Canonical resolver failed to accept a valid worktree "
            "under the allowlist anchor"
        )
        assert result == worktree.resolve()


def test_spine_resolve_returns_none_for_empty_evidence() -> None:
    """Empty / malformed / missing-key evidence returns None gracefully.
    The Slice 3G code path treats None as 'no override' — preserves
    byte-identical legacy behavior for non-SWE-Bench-Pro ops."""
    from backend.core.ouroboros.governance.operation_advisor import (
        resolve_envelope_repo_root,
    )
    # Empty string
    assert resolve_envelope_repo_root("", project_root=Path(".")) is None
    # Malformed JSON
    assert (
        resolve_envelope_repo_root("not-json", project_root=Path("."))
        is None
    )
    # Wrong type (not a dict)
    assert (
        resolve_envelope_repo_root('"a-string"', project_root=Path("."))
        is None
    )
    # Dict without repo_root key
    assert (
        resolve_envelope_repo_root('{"other": "x"}', project_root=Path("."))
        is None
    )


def test_spine_resolve_rejects_path_outside_allowlist() -> None:
    """The resolver's allowlist contract: paths that don't resolve
    under ``project_root`` (or env-derived extra prefixes) are
    rejected. This is the security boundary Slice 3G inherits — no
    arbitrary-path override attacks."""
    import json
    import tempfile
    import os
    from backend.core.ouroboros.governance.operation_advisor import (
        resolve_envelope_repo_root,
    )
    with tempfile.TemporaryDirectory() as outside_anchor, \
         tempfile.TemporaryDirectory() as escape_target:
        # Clear any env-derived allowlist for this test
        # (JARVIS_ADVISOR_WORKTREE_AWARE_ALLOWLIST may otherwise let it through)
        saved = os.environ.pop(
            "JARVIS_ADVISOR_WORKTREE_AWARE_ALLOWLIST", None,
        )
        try:
            # Anchor is outside_anchor; evidence points elsewhere
            evidence_json = json.dumps({"repo_root": str(escape_target)})
            result = resolve_envelope_repo_root(
                evidence_json,
                project_root=Path(outside_anchor),
            )
            # Should be rejected (escape_target NOT under outside_anchor)
            assert result is None, (
                "Resolver accepted a path outside the allowlist — "
                "security boundary breached."
            )
        finally:
            if saved is not None:
                os.environ["JARVIS_ADVISOR_WORKTREE_AWARE_ALLOWLIST"] = saved


def test_spine_resolve_explicit_allowlist_accepts_worktree() -> None:
    """When the caller supplies ``extra_allowlist`` explicitly,
    paths under those prefixes are accepted even if outside
    project_root. Production providers can use this to whitelist
    ``JARVIS_SWE_BENCH_PRO_WORKTREE_BASE_PATH`` (= ``/tmp/swebp_wt``)."""
    import json
    import tempfile
    from backend.core.ouroboros.governance.operation_advisor import (
        resolve_envelope_repo_root,
    )
    with tempfile.TemporaryDirectory() as proj, \
         tempfile.TemporaryDirectory() as wt_base:
        worktree = Path(wt_base) / "instance_ansible"
        worktree.mkdir()
        evidence_json = json.dumps({"repo_root": str(worktree)})
        result = resolve_envelope_repo_root(
            evidence_json,
            project_root=Path(proj),
            extra_allowlist=(Path(wt_base).resolve(),),
        )
        assert result is not None and result == worktree.resolve()


def test_spine_resolve_handles_nonexistent_path() -> None:
    """Paths that don't exist on disk → None. The worktree must
    actually be there for the override to fire — defends against
    stale envelope state."""
    import json
    from backend.core.ouroboros.governance.operation_advisor import (
        resolve_envelope_repo_root,
    )
    evidence_json = json.dumps(
        {"repo_root": "/nonexistent/path/to/worktree"}
    )
    result = resolve_envelope_repo_root(
        evidence_json,
        project_root=Path("/"),
    )
    assert result is None


def test_spine_tool_loop_signature_keyword_compatible() -> None:
    """The new ``repo_root_override`` kwarg must be keyword-only or
    have a default — existing callers (test suites, alternate provider
    impls) that don't know about it must continue to work
    byte-identically. Verified via signature inspection."""
    from backend.core.ouroboros.governance.tool_executor import (
        ToolLoopCoordinator,
    )
    import inspect
    sig = inspect.signature(ToolLoopCoordinator.run)
    assert "repo_root_override" in sig.parameters, (
        "ToolLoopCoordinator.run signature missing the new parameter"
    )
    param = sig.parameters["repo_root_override"]
    assert param.default is None, (
        f"repo_root_override default is {param.default!r}, expected "
        f"None to preserve byte-identical legacy behavior for "
        f"callers that don't pass it."
    )

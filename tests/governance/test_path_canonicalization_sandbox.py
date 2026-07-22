"""Path Canonicalization Sandbox + Universal Target-Existence Gate + I/O Offload.

Root cause (soak bt-2026-07-21-230753): ``ChangeEngine._redirect_target``
joined ANY relative candidate path verbatim onto the write root, so a model
payload that embedded the root's own path suffix DOUBLED the root —
``<worktree>/Documents/repos/.../<worktree>/backend/soak_probes/x.py`` —
contained (passes assert_write_path_allowed) but nonsensical, surfacing as a
hard APPLY ENOENT. The same soak recorded a 5.4s SidecarProfiler STUCK_FRAME
in ``pathlib.stat`` from ``derive_locality_roots`` probing a cold FS on the
event loop.

This suite pins the three mandated edge cases plus the composing gates:

1. **Duplicated root prefix** — an LLM payload embedding the write root's
   path suffix canonicalizes cleanly to the true target (pure Path-component
   mathematics; multiply-duplicated prefixes collapse; absolute under-root
   echoes too).
2. **Directory traversal** — ``../`` climbs and absolute host paths raise the
   typed :class:`PathTraversalError` (a :class:`BlockedPathError` subclass so
   every existing fail-closed catch site handles it identically).
3. **Event-loop unblocking** — ``derive_locality_roots`` dispatches to the
   dedicated ``advisor-blast`` executor; a concurrent heartbeat proves the
   running loop stays unblocked while a slow (cold-FS-simulating) derivation
   runs.

Plus: the universal target-existence gate (benchmark semantics byte-identical
via the ``allow_new_files`` default; host lane keeps legitimate new-file
creation while flagging the phantom-parent doubled-path class), and the
``_redirect_target`` integration (doubled payload → clean rebase; escape →
raise; master OFF → legacy verbatim join).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.change_engine import (
    BlockedPathError,
    ChangeEngine,
    PathTraversalError,
    canonicalize_candidate_path,
)
from backend.core.ouroboros.governance.target_existence_guard import (
    build_retry_feedback,
    find_missing_targets,
    universal_guard_enabled,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A write root shaped like the soak's worktree, with a real package."""
    root = (
        tmp_path / "Documents" / "repos" / "JARVIS-AI-Agent.nosync"
        / ".worktrees" / "ouroboros__auto__bt-test-1234"
    )
    (root / "backend" / "soak_probes").mkdir(parents=True)
    (root / "backend" / "soak_probes" / "existing.py").write_text("X = 1\n")
    return root


# ---------------------------------------------------------------------------
# Edge case 1 — duplicated root prefix canonicalizes cleanly
# ---------------------------------------------------------------------------


def test_home_stripped_root_echo_canonicalizes(worktree: Path) -> None:
    """The EXACT soak shape: the payload embeds the write root's absolute
    path minus its leading anchor components."""
    # Suffix of the root's own parts, as the model emitted it.
    echoed = "/".join(worktree.parts[-5:])  # Documents/repos/.../ouroboros__auto__bt-test-1234
    raw = f"{echoed}/backend/soak_probes/probe.py"
    out = canonicalize_candidate_path(raw, worktree)
    assert out == (worktree / "backend" / "soak_probes" / "probe.py").resolve()
    # Mathematically: the root's name never appears twice in the result.
    assert str(out).count(worktree.name) == 1


def test_double_duplication_collapses_iteratively(worktree: Path) -> None:
    echoed = "/".join(worktree.parts[-3:])
    raw = f"{echoed}/{echoed}/backend/soak_probes/probe.py"
    out = canonicalize_candidate_path(raw, worktree)
    assert out == (worktree / "backend" / "soak_probes" / "probe.py").resolve()


def test_absolute_under_root_echo_canonicalizes(worktree: Path) -> None:
    """An ABSOLUTE payload under the root that re-enters the root's suffix
    (the doubled path as an absolute string) collapses identically."""
    echoed = "/".join(worktree.parts[-4:])
    raw = str(worktree / echoed / "backend" / "soak_probes" / "probe.py")
    out = canonicalize_candidate_path(raw, worktree)
    assert out == (worktree / "backend" / "soak_probes" / "probe.py").resolve()


def test_clean_relative_path_passes_through(worktree: Path) -> None:
    out = canonicalize_candidate_path(
        "backend/soak_probes/existing.py", worktree,
    )
    assert out == (worktree / "backend" / "soak_probes" / "existing.py").resolve()


def test_single_component_coincidence_is_not_stripped(tmp_path: Path) -> None:
    """A 1-component overlap with the root's dirname is NOT duplication —
    the minimum-alignment guard protects a genuine file living in a
    directory that shares the root's name."""
    root = tmp_path / "myrepo"
    (root / "myrepo").mkdir(parents=True)  # legit nested dir sharing the name
    out = canonicalize_candidate_path("myrepo/inner.py", root)
    assert out == (root / "myrepo" / "inner.py").resolve()


# ---------------------------------------------------------------------------
# Edge case 2 — traversal raises PathTraversalError
# ---------------------------------------------------------------------------


def test_dotdot_climb_raises_path_traversal(worktree: Path) -> None:
    with pytest.raises(PathTraversalError):
        canonicalize_candidate_path("../../outside.py", worktree)


def test_nested_dotdot_escape_raises(worktree: Path) -> None:
    with pytest.raises(PathTraversalError):
        canonicalize_candidate_path(
            "backend/../../../../../../etc/passwd", worktree,
        )


def test_absolute_outside_root_raises(worktree: Path) -> None:
    with pytest.raises(PathTraversalError):
        canonicalize_candidate_path("/etc/passwd", worktree)


def test_root_degenerate_target_raises(worktree: Path) -> None:
    echoed = "/".join(worktree.parts[-3:])
    with pytest.raises(PathTraversalError):
        canonicalize_candidate_path(echoed, worktree)  # collapses to root itself


def test_path_traversal_error_is_blocked_path_error() -> None:
    """Taxonomy composition: every existing fail-closed catch site
    (``except BlockedPathError``) must handle the new type identically."""
    assert issubclass(PathTraversalError, BlockedPathError)


# ---------------------------------------------------------------------------
# _redirect_target integration (the actual APPLY seam)
# ---------------------------------------------------------------------------


def _engine(project_root: Path) -> ChangeEngine:
    """Minimal engine — _redirect_target touches only _project_root."""
    eng = ChangeEngine.__new__(ChangeEngine)
    eng._project_root = project_root
    return eng


def test_redirect_target_collapses_doubled_payload(
    tmp_path: Path, worktree: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    eng = _engine(project_root)
    echoed = "/".join(worktree.parts[-5:])
    doubled = Path(f"{echoed}/backend/soak_probes/probe.py")
    out = eng._redirect_target(doubled, request_write_root=worktree)
    assert out == (worktree / "backend" / "soak_probes" / "probe.py").resolve()


def test_redirect_target_raises_on_escape(
    tmp_path: Path, worktree: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    eng = _engine(project_root)
    with pytest.raises(PathTraversalError):
        eng._redirect_target(
            Path("../../escape.py"), request_write_root=worktree,
        )


def test_redirect_target_master_off_restores_legacy_join(
    tmp_path: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_CHANGE_PATH_CANONICALIZATION_ENABLED", "false")
    project_root = tmp_path / "project"
    project_root.mkdir()
    eng = _engine(project_root)
    rel = Path("backend/x.py")
    out = eng._redirect_target(rel, request_write_root=worktree)
    assert out == worktree / rel  # verbatim legacy join, byte-identical


# ---------------------------------------------------------------------------
# Universal target-existence gate
# ---------------------------------------------------------------------------


def test_universal_gate_default_on() -> None:
    assert universal_guard_enabled() is True


def test_universal_gate_flags_phantom_parent_doubled_path(
    worktree: Path,
) -> None:
    """The doubled-path class: parent chain doesn't exist → flagged even in
    the new-file-tolerant universal lane."""
    echoed = "/".join(worktree.parts[-5:])
    cand = {"file_path": f"{echoed}/backend/soak_probes/probe.py"}
    missing = find_missing_targets([cand], worktree, allow_new_files=True)
    assert missing, "phantom-parent doubled path must be flagged"


def test_universal_gate_allows_new_file_in_existing_package(
    worktree: Path,
) -> None:
    cand = {"file_path": "backend/soak_probes/brand_new_module.py"}
    missing = find_missing_targets([cand], worktree, allow_new_files=True)
    assert missing == []


def test_universal_gate_allows_new_nested_package_under_anchor(
    worktree: Path,
) -> None:
    """Anchor semantics (2026-07-22): a new nested package under an
    EXISTING top-level dir is legitimate scaffolding — the ChangeEngine's
    Sandboxed Ephemeral Instantiation creates the parents. Only a missing
    ANCHOR (hallucinated top-level tree / root echo) flags."""
    cand = {"file_path": "backend/new_pkg/sub/module.py"}
    assert find_missing_targets([cand], worktree, allow_new_files=True) == []


def test_universal_gate_allows_existing_file(worktree: Path) -> None:
    cand = {"file_path": "backend/soak_probes/existing.py"}
    assert find_missing_targets([cand], worktree, allow_new_files=True) == []


def test_benchmark_semantics_byte_identical(worktree: Path) -> None:
    """Default (no kwarg) keeps Slice 72 strictness: a new file — even in an
    existing package — is missing for a benchmark op."""
    cand = {"file_path": "backend/soak_probes/brand_new_module.py"}
    assert find_missing_targets([cand], worktree) == [
        "backend/soak_probes/brand_new_module.py"
    ]


def test_retry_feedback_lane_wording() -> None:
    bench = build_retry_feedback(["a.py"])
    univ = build_retry_feedback(["a.py"], benchmark=False)
    assert "THIRD-PARTY" in bench
    assert "THIRD-PARTY" not in univ
    assert "repeats the repository root" in univ


# ---------------------------------------------------------------------------
# Edge case 3 — derive_locality_roots offload keeps the loop unblocked
# ---------------------------------------------------------------------------


async def test_locality_root_derivation_never_blocks_running_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow (cold-FS-simulating) root derivation must run on the
    advisor-blast executor: ``asyncio.get_running_loop()`` keeps scheduling
    a concurrent heartbeat throughout the 0.4s derivation window. Before
    the offload, the same derivation wedged the loop (the 5.4s
    ``pathlib.stat`` STUCK_FRAME)."""
    from backend.core.ouroboros.governance import operation_advisor

    derive_thread: dict = {}

    def _slow_derive(target_files, scan_root, **kwargs):
        import threading
        derive_thread["name"] = threading.current_thread().name
        time.sleep(0.4)  # the cold-FS stat storm
        return (tmp_path,)

    # operation_advisor binds derive_locality_roots at module import; the
    # executor dispatch resolves the module global at call time, so this
    # patch intercepts the real seam.
    monkeypatch.setattr(
        operation_advisor, "derive_locality_roots", _slow_derive,
    )

    advisor = operation_advisor.OperationAdvisor(project_root=tmp_path)

    ticks = 0
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.005)

    hb = asyncio.ensure_future(_heartbeat())
    try:
        await advisor._localized_blast_lower_bound_async(
            ("x.py",), {"x"}, tmp_path,
        )
    finally:
        stop.set()
        await hb

    # 0.4s of derivation at a 5ms heartbeat: a blocked loop yields ~1 tick;
    # an unblocked loop yields dozens. Generous floor stays flake-proof.
    assert ticks >= 20, (
        f"event loop starved during root derivation (ticks={ticks}) — "
        "derive_locality_roots is running ON the loop again"
    )
    # And the derivation provably ran on the dedicated advisor-blast pool.
    assert derive_thread["name"].startswith("advisor-blast"), (
        f"derivation ran on {derive_thread['name']!r} — Task #88f isolation "
        "contract broken (must be the dedicated advisor-blast executor)"
    )

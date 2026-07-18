"""Anti-Drift Sentinel — AST parity between inline and extracted phase twins.

Every FSM phase has TWO implementations: the extracted phase-runner (the default
path) and the legacy inline twin in orchestrator.py (the kill switch). The Slice-1
drift audit (2026-07-18) proved they diverge silently — Proof Carrier was
inline-only (DEAD on the default path), the ORDER_2_GOVERNANCE floor was
runner-only (dropped by the kill switch), and the F2 evidence stamps were
runner-only. A safety hook that exists on one twin is a cage with a hole in it.

This sentinel permanently neutralizes the drift VECTOR: it parses the AST of the
orchestrator and of each extracted runner, extracts every identifier
(function/attribute/name) used inside the inline phase region vs the runner
module, and asserts that each GOVERNANCE MARKER (safety hooks, risk floors,
proof/evidence seams, phase gates) is present on BOTH twins or NEITHER. Adding a
governance call to one path without the other fails this test.

Markers are an extensible allowlist — when a new governance seam ships, add its
identifier here and the sentinel enforces twin parity forever after.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_GOV = Path("backend/core/ouroboros/governance")
_ORCH = _GOV / "orchestrator.py"

# ---------------------------------------------------------------------------
# Twin-pair table: (runner file, inline region start anchor, end anchor, markers)
# Region anchors are the canonical inline phase comments in orchestrator.py.
# ---------------------------------------------------------------------------

_PAIRS = [
    (
        "gate_runner.py",
        "# ---- Phase 5: GATE ----",
        "# ---- Phase 6: APPROVE",
        [
            # Slice-1 repaired drift (each was one-sided before 2026-07-18):
            "apply_order2_floor_safe",       # RR Pass B ORDER_2 floor (was runner-only)
            "emit_gate_proof_carrier",       # Slice 101 Proof Carrier (was inline-only)
            # Attribution floors (Slice 6/8 — must ride BOTH GATE paths):
            "_attribution_scope_risk_floor",
            "_attribution_test_only_notify_floor",
            "_value_ceiling_risk_floor",
        ],
    ),
    (
        "phase_runners/slice4b_runner.py",
        "# ---- Phase 6: APPROVE",
        "# ---- Phase 8c",
        [
            # Predictive Phase-Aware Checkpoint (wired on both, 2026-07-18):
            "PRE_APPLY_TAIL_PHASES",
            "predictive_suspend",
            # Pre-APPLY git checkpoint + apply seams:
            "WorkspaceCheckpointManager",
            "_live_work_apply_gate",
            "_apply_multi_file_candidate",
            # F2 evidence stamps (Slice-1 repaired: were runner-only):
            "stamp_target_files_pre_async",
            "stamp_apply_evidence_post_async",
            # Slice 9/11 VERIFY discipline + promotion (both paths per design):
            "_scoped_verify_runner",
            "run_workspace_promotion",
            "_emit_terminal_durability_probe",
        ],
    ),
]


def _identifiers(tree: ast.AST, lo: int = 0, hi: int = 10**9) -> set:
    """Every Name id / Attribute attr / def name / imported canonical symbol
    whose lineno falls in [lo, hi]. ImportFrom aliases record the CANONICAL
    name (``from x import foo as _foo`` → ``foo``), so a twin that imports a
    governance helper under a local alias still counts as carrying it."""
    out: set = set()
    for node in ast.walk(tree):
        ln = getattr(node, "lineno", None)
        if ln is None or not (lo <= ln <= hi):
            continue
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
        elif isinstance(node, (ast.ImportFrom, ast.Import)):
            for a in node.names:
                out.add(a.name.rsplit(".", 1)[-1])
    return out


def _anchor_lines(src_lines: list, start_pat: str, end_pat: str) -> "tuple[int, int]":
    start = end = None
    for i, ln in enumerate(src_lines, start=1):
        if start is None and start_pat in ln:
            start = i
        elif start is not None and end_pat in ln:
            end = i
            break
    if start is None or end is None:
        pytest.fail(
            f"inline phase anchors not found: {start_pat!r} -> {end_pat!r} "
            "(orchestrator anchor comments moved? update the sentinel's pair table)"
        )
    return start, end


@pytest.fixture(scope="module")
def orch():
    src = _ORCH.read_text()
    return src.splitlines(), ast.parse(src)


@pytest.mark.parametrize(
    "runner_rel,start_pat,end_pat,markers",
    _PAIRS,
    ids=[p[0] for p in _PAIRS],
)
def test_twin_paths_have_identical_governance_markers(
    orch, runner_rel, start_pat, end_pat, markers,
):
    src_lines, orch_tree = orch
    lo, hi = _anchor_lines(src_lines, start_pat, end_pat)
    inline_ids = _identifiers(orch_tree, lo, hi)

    runner_path = _GOV / runner_rel if "/" in runner_rel else _GOV / "phase_runners" / runner_rel
    if not runner_path.exists():
        runner_path = _GOV / "phase_runners" / Path(runner_rel).name
    runner_ids = _identifiers(ast.parse(runner_path.read_text()))

    drifted = [
        m for m in markers
        if (m in inline_ids) != (m in runner_ids)
    ]
    assert not drifted, (
        f"TWIN-PATH DRIFT in {runner_rel} vs inline [{start_pat} .. {end_pat}]: "
        f"{[(m, 'inline-only' if m in inline_ids else 'runner-only') for m in drifted]} "
        "— a governance hook exists on one twin but not the other. Port it to "
        "the missing path (single-source helper preferred) or, if deliberately "
        "retired from both, remove it from this sentinel's marker table."
    )


def test_all_markers_actually_exist_somewhere(orch):
    """Guards the sentinel itself against rot: a marker absent from BOTH twins
    is vacuously 'in parity' — flag it so the table stays honest."""
    src_lines, orch_tree = orch
    all_orch = _identifiers(orch_tree)
    for runner_rel, _s, _e, markers in _PAIRS:
        runner_path = _GOV / "phase_runners" / Path(runner_rel).name
        runner_ids = _identifiers(ast.parse(runner_path.read_text()))
        dead = [m for m in markers if m not in all_orch and m not in runner_ids]
        assert not dead, (
            f"sentinel markers dead in both twins for {runner_rel}: {dead} — "
            "remove them from the table or restore the hooks"
        )

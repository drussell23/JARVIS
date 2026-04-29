"""Priority F2/F3 + Priority C consumer wiring — regression spine.

Closes the empirical loop on the happy path:

  * F2: stamp target_files_pre BEFORE change_engine.execute, full
    post-evidence at APPLY-success
  * F3: stamp test_files_pre at PLAN entry
  * C: PLAN-runner invokes probe_trivial_op_assumption to refute
    the trivial-op assumption when target files are non-trivial-sized

Pins:
  §1   evidence_capture master flag default true
  §2   capture_test_files_inventory pure-stdlib glob
  §3   capture_test_files_inventory empty on missing dir
  §4   capture_test_files_inventory cap respected
  §5   stamp_test_files_pre stamps ctx attr
  §6   stamp_test_files_pre idempotent (existing pre wins)
  §7   stamp_test_files_pre master-off no-op
  §8   stamp_test_files_post always overwrites
  §9   snapshot_target_files reads file content
  §10  snapshot_target_files records missing files
  §11  snapshot_target_files truncates oversized files
  §12  stamp_target_files_pre + stamp_target_files_post round-trip
  §13  compute_unified_diff identical snapshots → empty diff
  §14  compute_unified_diff content delta → unified-diff text
  §15  compute_unified_diff file added in post → addition diff
  §16  compute_unified_diff file removed in post → deletion diff
  §17  compute_unified_diff respects byte cap
  §18  stamp_diff_text reads from ctx pre/post, stamps ctx.diff_text
  §19  stamp_apply_evidence_post composite stamps all 3
  §20  All stamp helpers NEVER raise on garbage ctx
  §21  hypothesis_consumers master flag default true
  §22  probe_trivial_op_assumption — disabled flags → legacy default
  §23  probe_trivial_op_assumption — no qualifying file → defer to legacy
  §24  probe_trivial_op_assumption — large file qualifies → REFUTES
  §25  probe_trivial_op_assumption — small files only → defer
  §26  Curiosity / CapabilityGap / SelfGoalFormation scaffolds return
       safe defaults
  §27  Authority invariants — no orchestrator imports
  §28  End-to-end through evidence_collectors registry: PLAN stamps
       test_files_pre → APPLY stamps target_files_post + diff_text →
       Slice F1 dispatcher returns rich evidence (NOT INSUFFICIENT)
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

import pytest

from backend.core.ouroboros.governance.verification.evidence_capture import (
    capture_test_files_inventory,
    compute_unified_diff,
    evidence_capture_enabled,
    snapshot_target_files,
    stamp_apply_evidence_post,
    stamp_diff_text,
    stamp_target_files_post,
    stamp_target_files_pre,
    stamp_test_files_post,
    stamp_test_files_pre,
)
from backend.core.ouroboros.governance.verification.hypothesis_consumers import (
    HYPOTHESIS_CONSUMERS_SCHEMA_VERSION,
    TrivialityVerdict,
    hypothesis_consumers_enabled,
    probe_capability_gap,
    probe_goal_disambiguation,
    probe_intent_dismissal,
    probe_trivial_op_assumption,
)


# ===========================================================================
# §1 — Master flag
# ===========================================================================


def test_evidence_capture_master_flag_default_true(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_EVIDENCE_CAPTURE_ENABLED", raising=False)
    assert evidence_capture_enabled() is True


@pytest.mark.parametrize("val", ["", " ", "  "])
def test_evidence_capture_empty_default_true(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_EVIDENCE_CAPTURE_ENABLED", val)
    assert evidence_capture_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "off", "no"])
def test_evidence_capture_falsy_disables(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_EVIDENCE_CAPTURE_ENABLED", val)
    assert evidence_capture_enabled() is False


# ===========================================================================
# §2-§4 — capture_test_files_inventory
# ===========================================================================


def test_inventory_globs_python_test_files(tmp_path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_x(): pass\n")
    (tmp_path / "tests" / "test_b.py").write_text("def test_y(): pass\n")
    (tmp_path / "tests" / "not_a_test.txt").write_text("ignored")
    inv = capture_test_files_inventory(str(tmp_path))
    assert any("test_a.py" in p for p in inv)
    assert any("test_b.py" in p for p in inv)
    assert not any("not_a_test.txt" in p for p in inv)


def test_inventory_returns_empty_on_missing_dir() -> None:
    inv = capture_test_files_inventory("/nonexistent/path/that/should/not/exist")
    assert inv == ()


def test_inventory_respects_cap(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVIS_EVIDENCE_MAX_TEST_FILES", "3")
    (tmp_path / "tests").mkdir()
    for i in range(10):
        (tmp_path / "tests" / f"test_{i}.py").write_text("pass\n")
    inv = capture_test_files_inventory(str(tmp_path))
    assert len(inv) == 3


# ===========================================================================
# §5-§8 — stamp_test_files_pre/post
# ===========================================================================


def test_stamp_pre_stamps_ctx_attr(tmp_path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("pass\n")
    ctx = SimpleNamespace()
    n = stamp_test_files_pre(ctx, target_dir=str(tmp_path))
    assert n >= 1
    assert hasattr(ctx, "test_files_pre")
    assert any("test_x.py" in p for p in ctx.test_files_pre)


def test_stamp_pre_idempotent_existing_wins(tmp_path) -> None:
    pre_existing = ("custom/test_a.py",)
    ctx = SimpleNamespace(test_files_pre=pre_existing)
    n = stamp_test_files_pre(ctx, target_dir=str(tmp_path))
    assert ctx.test_files_pre == pre_existing


def test_stamp_pre_master_off_noop(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVIS_EVIDENCE_CAPTURE_ENABLED", "false")
    ctx = SimpleNamespace()
    n = stamp_test_files_pre(ctx, target_dir=str(tmp_path))
    assert n == 0
    assert not hasattr(ctx, "test_files_pre")


def test_stamp_post_always_overwrites(tmp_path) -> None:
    """Unlike pre, post overwrites whatever's there."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("pass\n")
    ctx = SimpleNamespace(test_files_post=("stale_value",))
    stamp_test_files_post(ctx, target_dir=str(tmp_path))
    assert "stale_value" not in ctx.test_files_post
    assert any("test_x.py" in p for p in ctx.test_files_post)


# ===========================================================================
# §9-§12 — snapshot_target_files
# ===========================================================================


def test_snapshot_reads_file_content(tmp_path) -> None:
    f = tmp_path / "a.py"
    f.write_text("x = 42\n")
    snap = snapshot_target_files((str(f),))
    assert len(snap) == 1
    assert snap[0]["path"] == str(f)
    assert snap[0]["content"] == "x = 42\n"
    assert snap[0]["exists"] is True


def test_snapshot_records_missing_files(tmp_path) -> None:
    snap = snapshot_target_files((str(tmp_path / "nope.py"),))
    assert len(snap) == 1
    assert snap[0]["exists"] is False
    assert snap[0]["content"] == ""


def test_snapshot_truncates_oversized_files(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVIS_EVIDENCE_MAX_FILE_BYTES", "100")
    f = tmp_path / "big.py"
    f.write_text("x = 1\n" * 100)  # ~600 bytes
    snap = snapshot_target_files((str(f),))
    content = snap[0]["content"]
    assert "clipped" in content
    assert len(content.encode("utf-8")) < 600


def test_stamp_target_files_round_trip(tmp_path) -> None:
    f = tmp_path / "real.py"
    f.write_text("a = 1\n")
    ctx = SimpleNamespace(target_files=(str(f),))
    stamp_target_files_pre(ctx)
    assert hasattr(ctx, "target_files_pre")
    # Modify the file
    f.write_text("a = 2\n")
    stamp_target_files_post(ctx)
    assert hasattr(ctx, "target_files_post")
    pre_content = ctx.target_files_pre[0]["content"]
    post_content = ctx.target_files_post[0]["content"]
    assert pre_content != post_content


# ===========================================================================
# §13-§17 — compute_unified_diff
# ===========================================================================


def test_diff_identical_snapshots_empty() -> None:
    snap = ({"path": "a.py", "content": "x = 1\n"},)
    diff = compute_unified_diff(snap, snap)
    assert diff == ""


def test_diff_content_delta_produces_unified_diff() -> None:
    pre = ({"path": "a.py", "content": "x = 1\n"},)
    post = ({"path": "a.py", "content": "x = 2\n"},)
    diff = compute_unified_diff(pre, post)
    assert "a.py" in diff
    assert "-x = 1" in diff
    assert "+x = 2" in diff


def test_diff_addition_in_post() -> None:
    pre = ()
    post = ({"path": "new.py", "content": "def f(): pass\n"},)
    diff = compute_unified_diff(pre, post)
    assert "new.py" in diff
    assert "+def f()" in diff


def test_diff_deletion_in_post() -> None:
    pre = ({"path": "old.py", "content": "x = 1\n"},)
    post = ()
    diff = compute_unified_diff(pre, post)
    assert "old.py" in diff
    assert "-x = 1" in diff


def test_diff_respects_byte_cap(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_EVIDENCE_MAX_DIFF_BYTES", "100")
    pre = ({"path": "a.py", "content": "x = 1\n" * 50},)
    post = ({"path": "a.py", "content": "y = 2\n" * 50},)
    diff = compute_unified_diff(pre, post)
    assert "clipped" in diff
    assert len(diff.encode("utf-8")) < 250


# ===========================================================================
# §18-§19 — Composite stamping
# ===========================================================================


def test_stamp_diff_text_from_ctx(tmp_path) -> None:
    f = tmp_path / "a.py"
    pre = ({"path": str(f), "content": "x = 1\n"},)
    post = ({"path": str(f), "content": "x = 2\n"},)
    ctx = SimpleNamespace(
        target_files_pre=pre, target_files_post=post,
    )
    n = stamp_diff_text(ctx)
    assert n > 0
    assert hasattr(ctx, "diff_text")
    assert "x = 1" in ctx.diff_text or "x = 2" in ctx.diff_text


def test_stamp_apply_evidence_post_composite(tmp_path) -> None:
    f = tmp_path / "src.py"
    f.write_text("a = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("pass\n")
    ctx = SimpleNamespace(target_files=(str(f),))
    # Stamp pre first (simulates pre-APPLY snapshot)
    stamp_target_files_pre(ctx)
    # Modify file (simulates change_engine.execute)
    f.write_text("a = 2\n")
    # Composite post-stamp
    diag = stamp_apply_evidence_post(ctx, target_dir=str(tmp_path))
    assert diag["enabled"] == 1
    assert diag["target_files_post"] == 1
    assert diag["test_files_post"] == 1
    assert diag["diff_text_bytes"] > 0
    assert hasattr(ctx, "target_files_post")
    assert hasattr(ctx, "test_files_post")
    assert hasattr(ctx, "diff_text")


# ===========================================================================
# §20 — Defensive (never raises)
# ===========================================================================


def test_all_stamps_never_raise_on_garbage_ctx() -> None:
    # None ctx
    assert stamp_test_files_pre(None) == 0
    assert stamp_target_files_pre(None) == 0
    assert stamp_target_files_post(None) == 0
    assert stamp_diff_text(None) == 0
    assert stamp_apply_evidence_post(None) == {"enabled": 1, "target_files_post": 0, "test_files_post": 0, "diff_text_bytes": 0}
    # ctx without expected attrs
    ctx = SimpleNamespace()
    assert stamp_target_files_pre(ctx) == 0
    assert stamp_diff_text(ctx) == 0


# ===========================================================================
# §21 — hypothesis_consumers master flag
# ===========================================================================


def test_consumers_master_flag_default_true(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_HYPOTHESIS_CONSUMERS_ENABLED", raising=False)
    assert hypothesis_consumers_enabled() is True


# ===========================================================================
# §22-§25 — probe_trivial_op_assumption
# ===========================================================================


def test_probe_trivial_consumers_off_legacy_default(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("JARVIS_HYPOTHESIS_CONSUMERS_ENABLED", "false")
    f = tmp_path / "huge.py"
    f.write_text("x = 1\n" * 1000)
    verdict = asyncio.run(
        probe_trivial_op_assumption(
            target_files=(str(f),),
            op_id="op-test",
            description="trivial",
        ),
    )
    assert isinstance(verdict, TrivialityVerdict)
    assert verdict.treat_as_trivial is True
    assert verdict.convergence_state == "disabled"


def test_probe_trivial_probe_off_legacy_default(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", "false")
    f = tmp_path / "huge.py"
    f.write_text("x = 1\n" * 1000)
    verdict = asyncio.run(
        probe_trivial_op_assumption(
            target_files=(str(f),),
            op_id="op-test",
            description="trivial",
        ),
    )
    assert verdict.treat_as_trivial is True


def test_probe_trivial_no_qualifying_file_defers(
    monkeypatch, tmp_path,
) -> None:
    """Small files don't qualify as 'non-trivial-sized' → probe
    has no signal → defers to legacy heuristic."""
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TRIVIAL_OP_NON_TRIVIAL_BYTES", "8192")
    f = tmp_path / "tiny.py"
    f.write_text("a = 1\n")  # ~6 bytes
    verdict = asyncio.run(
        probe_trivial_op_assumption(
            target_files=(str(f),),
            op_id="op-test",
            description="ok",
        ),
    )
    assert verdict.treat_as_trivial is True
    assert verdict.convergence_state == "inconclusive"


def test_probe_trivial_large_file_refutes(
    monkeypatch, tmp_path,
) -> None:
    """Large file (above non-trivial threshold) should REFUTE the
    trivial-op assumption."""
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HYPOTHESIS_LEDGER_PATH",
                       str(tmp_path / "failed.jsonl"))
    monkeypatch.setenv("JARVIS_TRIVIAL_OP_NON_TRIVIAL_BYTES", "100")
    f = tmp_path / "big.py"
    f.write_text("x = 1\n" * 100)  # ~600 bytes
    verdict = asyncio.run(
        probe_trivial_op_assumption(
            target_files=(str(f),),
            op_id="op-trivial-refute",
            description="should-be-non-trivial",
        ),
    )
    # Large file qualifies → probe CONFIRMS file_exists → REFUTES
    # the trivial assumption
    assert verdict.treat_as_trivial is False
    assert "non-trivial" in verdict.observation_summary.lower()


# ===========================================================================
# §26 — Scaffold helpers return safe defaults
# ===========================================================================


def test_curiosity_scaffold() -> None:
    v = asyncio.run(probe_intent_dismissal(
        intent_summary="x", op_id="op", urgency="low",
    ))
    assert v.safe_to_dismiss is True
    assert v.convergence_state == "scaffold"


def test_capability_gap_scaffold() -> None:
    v = asyncio.run(probe_capability_gap(
        gap_summary="x", evidence_path="y", op_id="op",
    ))
    assert v.gap_is_real is True
    assert v.convergence_state == "scaffold"


def test_self_goal_disambiguation_scaffold() -> None:
    v = asyncio.run(probe_goal_disambiguation(
        candidates=("a", "b"), op_id="op",
    ))
    assert v.selected_index == 0
    v_empty = asyncio.run(probe_goal_disambiguation(
        candidates=(), op_id="op",
    ))
    assert v_empty.selected_index == -1


# ===========================================================================
# §27 — Authority invariants
# ===========================================================================


def test_no_authority_imports_evidence_capture() -> None:
    from backend.core.ouroboros.governance.verification import evidence_capture
    src = inspect.getsource(evidence_capture)
    forbidden = (
        "orchestrator", "phase_runner", "candidate_generator",
        "iron_gate", "change_engine", "policy", "semantic_guardian",
    )
    for token in forbidden:
        assert (
            f"from backend.core.ouroboros.governance.{token}" not in src
        )


def test_no_authority_imports_hypothesis_consumers() -> None:
    from backend.core.ouroboros.governance.verification import hypothesis_consumers
    src = inspect.getsource(hypothesis_consumers)
    forbidden = (
        "orchestrator", "phase_runner", "candidate_generator",
        "iron_gate", "change_engine", "policy", "semantic_guardian",
    )
    for token in forbidden:
        assert (
            f"from backend.core.ouroboros.governance.{token}" not in src
        )


# ===========================================================================
# §28 — End-to-end: PLAN→APPLY stamps drive Slice F1 evidence
# ===========================================================================


def test_end_to_end_pre_stamp_drives_evidence_collector(tmp_path) -> None:
    """The Big One — verify F2/F3 stamps make F1 collectors return
    rich evidence (not INSUFFICIENT_EVIDENCE)."""
    from backend.core.ouroboros.governance.verification import (
        dispatch_evidence_gather,
    )
    # Build a real project tree
    src = tmp_path / "src.py"
    src.write_text("def f(): return 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")

    # Simulate PLAN entry
    ctx = SimpleNamespace(
        op_id="op-e2e",
        target_files=(str(src),),
        target_dir=str(tmp_path),
    )
    stamp_test_files_pre(ctx, target_dir=str(tmp_path))
    assert hasattr(ctx, "test_files_pre")

    # Simulate APPLY pre-snapshot
    stamp_target_files_pre(ctx)

    # Simulate change-engine writing
    src.write_text("def f(): return 2\n")  # changed!
    (tmp_path / "tests" / "test_y.py").write_text("def test_y(): pass\n")

    # Simulate APPLY-success: composite post-stamp
    stamp_apply_evidence_post(ctx, target_dir=str(tmp_path))

    # Now verify the F1 dispatcher returns RICH evidence (not empty)
    claim_fp = SimpleNamespace(
        property=SimpleNamespace(kind="file_parses_after_change", name="t"),
    )
    fp_evidence = asyncio.run(dispatch_evidence_gather(claim_fp, ctx))
    assert "target_files_post" in fp_evidence
    assert len(fp_evidence["target_files_post"]) >= 1

    claim_diff = SimpleNamespace(
        property=SimpleNamespace(kind="no_new_credential_shapes", name="t"),
    )
    diff_evidence = asyncio.run(dispatch_evidence_gather(claim_diff, ctx))
    assert "diff_text" in diff_evidence
    assert "return 1" in diff_evidence["diff_text"] or "return 2" in diff_evidence["diff_text"]

    claim_test = SimpleNamespace(
        property=SimpleNamespace(kind="test_set_hash_stable", name="t"),
    )
    test_evidence = asyncio.run(dispatch_evidence_gather(claim_test, ctx))
    assert "test_files_pre" in test_evidence
    assert "test_files_post" in test_evidence
    assert len(test_evidence["test_files_post"]) >= 2  # added test_y.py

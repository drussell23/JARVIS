"""Atomic Flush & Freeze — post-APPLY diff capture, HiveEmitter flush, park.

Pins the anti-cascade interceptor: (1) default-OFF master; (2) git-diff
capture for tracked mutations AND untracked new files (read-only — never
touches index/HEAD); (3) the full capture→persist→flush→freeze sequence with
the HiveEmitter enqueue DEMONSTRATED via the stats delta; (4) containment
over observability — a raising emitter still closes the latch; (5) the
mutation budget knob; (6) the ChangeEngine wiring (freeze check before any
byte is staged, hook at the post-VERIFY success seam).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import apply_flush_freeze as aff


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_latch(monkeypatch: pytest.MonkeyPatch):
    aff._reset_for_tests()
    monkeypatch.delenv("JARVIS_APPLY_FLUSH_FREEZE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_APPLY_FREEZE_MAX_OPS", raising=False)
    monkeypatch.delenv("JARVIS_APPLY_FREEZE_MAX_FILES_PER_OP", raising=False)
    yield
    aff._reset_for_tests()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real git repo with one committed file."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "module.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "module.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "-m", "seed"],
        cwd=tmp_path, check=True,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Master + latch semantics
# ---------------------------------------------------------------------------


def test_master_defaults_off() -> None:
    assert aff.flush_freeze_enabled() is False


def test_latch_starts_open() -> None:
    assert aff.is_frozen() is False


# ---------------------------------------------------------------------------
# Diff capture
# ---------------------------------------------------------------------------


async def test_capture_tracked_mutation(git_repo: Path) -> None:
    (git_repo / "module.py").write_text("VALUE = 2\n")
    diff = await aff.capture_mutation_diff(git_repo, ["module.py"])
    assert "-VALUE = 1" in diff
    assert "+VALUE = 2" in diff


async def test_capture_untracked_new_file(git_repo: Path) -> None:
    (git_repo / "brand_new.py").write_text("NEW = True\n")
    diff = await aff.capture_mutation_diff(git_repo, ["brand_new.py"])
    assert "+NEW = True" in diff


async def test_capture_outside_repo_is_empty_not_raise(
    tmp_path: Path,
) -> None:
    bare = tmp_path / "not_a_repo"
    bare.mkdir()
    diff = await aff.capture_mutation_diff(bare, ["x.py"])
    assert diff == ""  # fail-soft: no repo → no capture, no exception


# ---------------------------------------------------------------------------
# The full sequence — capture → persist → flush → freeze
# ---------------------------------------------------------------------------


async def test_flush_and_freeze_full_sequence(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.api.hive_emitter as hive_emitter_mod

    monkeypatch.setenv("JARVIS_APPLY_FLUSH_FREEZE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HIVE_EMITTERS_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_APPLY_FREEZE_ARTIFACT_DIR", str(tmp_path / "artifacts"),
    )
    # Fresh emitter so the stats delta is unambiguous; lazily self-binds to
    # this test's running loop on first emit.
    monkeypatch.setattr(hive_emitter_mod, "_default", None)

    (git_repo / "module.py").write_text("VALUE = 99\n")
    summary = await aff.flush_and_freeze(
        "op-test-1234", ["module.py"], git_repo, goal="test mutation",
    )

    assert summary["captured"] is True
    assert summary["diff_lines"] > 0
    assert summary["flushed"] is True, (
        "HiveEmitter enqueue must be demonstrated via the stats delta"
    )
    assert summary["frozen"] is True
    assert aff.is_frozen() is True
    # Durable artifact carries the diff (severed-socket insurance).
    artifact = Path(summary["artifact"])
    assert artifact.is_file()
    assert "+VALUE = 99" in artifact.read_text()
    # The envelope actually sits in the emitter queue.
    emitter = hive_emitter_mod.get_default_emitter()
    assert emitter.stats["emitted"] >= 1


async def test_emitter_failure_still_freezes(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Containment over observability: a raising emitter must never keep
    the latch open."""
    import backend.api.hive_emitter as hive_emitter_mod

    monkeypatch.setenv("JARVIS_APPLY_FLUSH_FREEZE_ENABLED", "true")

    def _boom(**kwargs):
        raise RuntimeError("severed socket")

    monkeypatch.setattr(hive_emitter_mod, "hive_emit", _boom)
    (git_repo / "module.py").write_text("VALUE = 3\n")
    summary = await aff.flush_and_freeze(
        "op-test-sever", ["module.py"], git_repo,
    )
    assert summary["flushed"] is False
    assert summary["frozen"] is True
    assert aff.is_frozen() is True


async def test_op_budget_knob_counts_transactions_not_files(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transactional Op-Scoping: the budget counts DISTINCT base ops.
    Under MAX_OPS=2, the first op's files never spend the second slot;
    a second op takes over the token and closes the latch."""
    monkeypatch.setenv("JARVIS_APPLY_FREEZE_MAX_OPS", "2")
    (git_repo / "module.py").write_text("VALUE = 4\n")
    s1 = await aff.flush_and_freeze("op-a::00", ["module.py"], git_repo)
    assert s1["frozen"] is False
    # A sibling file of op-a does NOT consume the second op slot.
    s1b = await aff.flush_and_freeze("op-a::01", ["module.py"], git_repo)
    assert s1b["frozen"] is False
    assert aff.is_frozen("op-c::00") is False  # budget not yet spent
    s2 = await aff.flush_and_freeze("op-b::00", ["module.py"], git_repo)
    assert s2["frozen"] is True
    assert aff.is_frozen("op-c::00") is True


# ---------------------------------------------------------------------------
# Transactional Op-Scoping — sibling admission, foreign denial, fan-out
# ---------------------------------------------------------------------------


async def test_sibling_files_pass_foreign_ops_denied(
    git_repo: Path,
) -> None:
    """The 2PC atomicity contract: after file ::00 of an op lands and
    acquires the token, sibling ::01 passes the consult while any
    DISTINCT base op is denied — the bt-2026-07-22-022146 rollback
    class (budget denying file 2 of an atomic batch) is dead."""
    (git_repo / "module.py").write_text("VALUE = 7\n")
    s = await aff.flush_and_freeze(
        "op-txn-1::00", ["module.py"], git_repo,
    )
    assert s["frozen"] is True  # latch owned, budget (1) spent for NEW ops
    # Sibling of the owning transaction: passes.
    assert aff.is_frozen("op-txn-1::01") is False
    # Foreign op: denied, with the frozen taxonomy.
    assert aff.is_frozen("op-txn-2::00") is True
    assert aff.denial_reason("op-txn-2::00") == aff.FROZEN_DENIAL_REASON
    # Legacy no-arg consult stays conservative.
    assert aff.is_frozen() is True


async def test_bounded_fanout_ceiling_denies_runaway_mono_op(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runaway mono-op cannot tunnel unlimited writes through one
    token: past MAX_FILES_PER_OP the sibling consult denies with the
    fan-out taxonomy."""
    monkeypatch.setenv("JARVIS_APPLY_FREEZE_MAX_FILES_PER_OP", "2")
    (git_repo / "module.py").write_text("VALUE = 8\n")
    await aff.flush_and_freeze("op-mono::00", ["module.py"], git_repo)
    assert aff.is_frozen("op-mono::01") is False   # under ceiling
    await aff.flush_and_freeze("op-mono::01", ["module.py"], git_repo)
    assert aff.is_frozen("op-mono::02") is True    # ceiling reached
    assert aff.denial_reason("op-mono::02") == aff.FANOUT_DENIAL_REASON


# ---------------------------------------------------------------------------
# MANDATED integration test — a 2-file transaction through the op-scoped
# latch: both 2PC commits succeed, the git diff encompasses both files,
# and a subsequent distinct op_id is immediately rejected.
# ---------------------------------------------------------------------------


async def test_two_file_transaction_lands_then_latch_rejects_foreign_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess as _sp
    from backend.core.ouroboros.governance.change_engine import (
        ChangeEngine, ChangeRequest,
    )
    from backend.core.ouroboros.governance.ledger import OperationLedger
    from backend.core.ouroboros.governance.risk_engine import (
        ChangeType, OperationProfile,
    )

    monkeypatch.setenv("JARVIS_APPLY_FLUSH_FREEZE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_APPLY_FREEZE_ARTIFACT_DIR", str(tmp_path / "artifacts"),
    )

    root = tmp_path / "worktree"
    root.mkdir()
    _sp.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "alpha.py").write_text("A = 1\n")
    _sp.run(["git", "add", "alpha.py"], cwd=root, check=True)
    _sp.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "-m", "seed"],
        cwd=root, check=True,
    )

    engine = ChangeEngine(
        project_root=root, ledger=OperationLedger(storage_dir=tmp_path / "l"),
    )

    def _req(op_id: str, target: Path, content: str) -> ChangeRequest:
        return ChangeRequest(
            goal="two-file transaction",
            target_file=target,
            proposed_content=content,
            profile=OperationProfile(
                files_affected=[target],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=1.0,
            ),
            op_id=op_id,
        )

    # File 1 (::00): modifies a tracked file — lands + acquires the token.
    r1 = await engine.execute(
        _req("op-txn::00", root / "alpha.py", "A = 2\n"),
    )
    assert r1.success is True, f"file 1 failed: {r1.error}"

    # File 2 (::01): a NEW sibling file — the 2PC batch completes under
    # the owned token (the exact shape the per-file budget rolled back).
    r2 = await engine.execute(
        _req("op-txn::01", root / "beta.py", "B = 2\n"),
    )
    assert r2.success is True, f"sibling denied/failed: {r2.error}"
    assert (root / "alpha.py").read_text().startswith("A = 2") or (
        "A = 2" in (root / "alpha.py").read_text()
    )
    assert (root / "beta.py").is_file()

    # The mutation's git diff encompasses BOTH files.
    diff = await aff.capture_mutation_diff(root, ["alpha.py", "beta.py"])
    assert "alpha.py" in diff and "+A = 2" in diff
    assert "beta.py" in diff and "+B = 2" in diff

    # A subsequent, DISTINCT op is immediately rejected by the closed
    # latch — before any byte stages.
    r3 = await engine.execute(
        _req("op-other::00", root / "alpha.py", "A = 3\n"),
    )
    assert r3.success is False
    assert r3.error == aff.FROZEN_DENIAL_REASON
    assert "A = 3" not in (root / "alpha.py").read_text()


# ---------------------------------------------------------------------------
# ChangeEngine wiring pins
# ---------------------------------------------------------------------------


def _engine_src() -> str:
    from backend.core.ouroboros.governance import change_engine
    import inspect
    return Path(inspect.getfile(change_engine)).read_text(encoding="utf-8")


def test_engine_checks_freeze_before_any_byte() -> None:
    """The freeze consult must sit at the execute() entry (generation-fence
    doctrine) and short-circuit with the typed denial reason."""
    src = _engine_src()
    # Transactional Op-Scoping (2026-07-22): the consult is op-aware and
    # the denial string comes from the taxonomy dispatcher.
    assert "_apply_flush_freeze.is_frozen(op_id)" in src
    assert "_apply_flush_freeze.denial_reason(op_id)" in src
    # The check precedes the pre-write gate (source-order pin).
    assert src.index("_apply_flush_freeze.is_frozen(op_id)") < src.index(
        "self._pre_write_gate(target, signed_content, request)"
    )


def test_engine_hooks_flush_at_success_seam() -> None:
    """The interceptor fires at the post-VERIFY success seam."""
    src = _engine_src()
    assert "_apply_flush_freeze.flush_and_freeze(" in src
    # After the APPLIED ledger record, before the success return.
    applied_idx = src.index('state=OperationState.APPLIED')
    hook_idx = src.index("_apply_flush_freeze.flush_and_freeze(")
    assert hook_idx > applied_idx


def test_denial_reason_taxonomy() -> None:
    assert aff.FROZEN_DENIAL_REASON.startswith("POLICY_DENIED reason=")

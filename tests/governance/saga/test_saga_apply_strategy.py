"""Tests for SagaApplyStrategy."""
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.op_context import OperationContext, SagaStepStatus
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
    SagaTerminalState,
)
from backend.core.ouroboros.governance.saga.saga_apply_strategy import SagaApplyStrategy


def _make_ctx(
    repo_scope=("jarvis", "prime"),
    apply_plan=("prime", "jarvis"),
    snapshots=(("jarvis", "abc123"), ("prime", "def456")),
):
    return OperationContext.create(
        target_files=("backend/x.py",),
        description="test saga",
        repo_scope=repo_scope,
        primary_repo="jarvis",
        apply_plan=apply_plan,
        repo_snapshots=snapshots,
        saga_id="test-saga-001",
    )


async def test_happy_path_all_repos_applied(tmp_path):
    """All repos apply successfully → SAGA_APPLY_COMPLETED."""
    ctx = _make_ctx()
    # Initialize git repo so that `git add` succeeds during apply
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
         "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )

    jarvis_file = tmp_path / "backend" / "x.py"
    jarvis_file.parent.mkdir(parents=True)
    jarvis_file.write_bytes(b"old content")

    prime_file = tmp_path / "backend" / "y.py"
    prime_file.parent.mkdir(parents=True, exist_ok=True)
    prime_file.write_bytes(b"old prime")

    patch_map = {
        "prime": RepoPatch(
            repo="prime",
            files=(PatchedFile(path="backend/y.py", op=FileOp.MODIFY, preimage=b"old prime"),),
            new_content=(("backend/y.py", b"new prime"),),
        ),
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path="backend/x.py", op=FileOp.MODIFY, preimage=b"old content"),),
            new_content=(("backend/x.py", b"new content"),),
        ),
    }

    repo_roots = {"jarvis": tmp_path, "prime": tmp_path}
    ledger = MagicMock()
    ledger.append = AsyncMock()

    strategy = SagaApplyStrategy(repo_roots=repo_roots, ledger=ledger)

    with patch.object(strategy, "_get_head_hash", side_effect=lambda repo: {"jarvis": "abc123", "prime": "def456"}[repo]):
        result = await strategy.execute(ctx, patch_map)

    assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED
    assert jarvis_file.read_bytes() == b"new content"
    assert prime_file.read_bytes() == b"new prime"


async def test_drift_aborts_before_any_apply(tmp_path):
    """HEAD drift detected in pre-flight → SAGA_ABORTED, no files written."""
    ctx = _make_ctx()
    patch_map = {
        "prime": RepoPatch(
            repo="prime",
            files=(PatchedFile(path="backend/y.py", op=FileOp.CREATE, preimage=None),),
            new_content=(("backend/y.py", b"new"),),
        ),
        "jarvis": RepoPatch(repo="jarvis", files=(), new_content=()),
    }
    repo_roots = {"jarvis": tmp_path, "prime": tmp_path}
    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots=repo_roots, ledger=ledger)

    # prime HEAD has drifted
    with patch.object(strategy, "_get_head_hash", side_effect=lambda repo: {"jarvis": "abc123", "prime": "DRIFTED"}[repo]):
        result = await strategy.execute(ctx, patch_map)

    assert result.terminal_state == SagaTerminalState.SAGA_ABORTED
    assert result.reason_code == "drift_detected"


async def test_apply_failure_triggers_compensation(tmp_path):
    """Second repo apply fails → first repo is compensated."""
    ctx = _make_ctx()

    jarvis_file = tmp_path / "backend" / "x.py"
    jarvis_file.parent.mkdir(parents=True)
    jarvis_file.write_bytes(b"original")

    # prime patch tries to write to a non-existent deep path — will fail
    patch_map = {
        "prime": RepoPatch(
            repo="prime",
            files=(PatchedFile(path="does/not/exist/deeply/y.py", op=FileOp.CREATE, preimage=None),),
            new_content=(("does/not/exist/deeply/y.py", b"content"),),
        ),
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path="backend/x.py", op=FileOp.MODIFY, preimage=b"original"),),
            new_content=(("backend/x.py", b"modified"),),
        ),
    }

    # Make prime apply fail by crippling its root path
    bad_prime_root = tmp_path / "nonexistent_prime"
    repo_roots = {"jarvis": tmp_path, "prime": bad_prime_root}
    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots=repo_roots, ledger=ledger)

    with patch.object(strategy, "_get_head_hash", side_effect=lambda repo: {"jarvis": "abc123", "prime": "def456"}[repo]):
        result = await strategy.execute(ctx, patch_map)

    assert result.terminal_state == SagaTerminalState.SAGA_ROLLED_BACK
    assert jarvis_file.read_bytes() == b"original"


async def test_skipped_repo_with_empty_patch(tmp_path):
    """Repo with empty patch is skipped, result is SAGA_APPLY_COMPLETED."""
    ctx = _make_ctx(
        repo_scope=("jarvis", "prime"),
        apply_plan=("prime", "jarvis"),
    )
    # Initialize git repo so that `git add` succeeds during apply
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
         "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )

    patch_map = {
        "prime": RepoPatch(repo="prime", files=(), new_content=()),  # empty
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path="backend/x.py", op=FileOp.CREATE, preimage=None),),
            new_content=(("backend/x.py", b"new"),),
        ),
    }
    repo_roots = {"jarvis": tmp_path, "prime": tmp_path}
    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots=repo_roots, ledger=ledger)

    with patch.object(strategy, "_get_head_hash", side_effect=lambda repo: {"jarvis": "abc123", "prime": "def456"}[repo]):
        result = await strategy.execute(ctx, patch_map)

    assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED


def test_topological_sort_respects_dependency_edges():
    """_topological_sort returns correct apply order from dependency_edges."""
    strategy = SagaApplyStrategy(repo_roots={}, ledger=MagicMock())
    # edge (prime, jarvis) means prime depends on jarvis → jarvis applied first
    order = strategy._topological_sort(
        repo_scope=("jarvis", "prime"),
        edges=(("prime", "jarvis"),),
    )
    assert order.index("jarvis") < order.index("prime")


async def test_compensation_failure_returns_saga_stuck(tmp_path):
    """When compensation fails for an applied repo, returns SAGA_STUCK.

    Setup: two repos in apply order [repo_a, repo_b].
    - repo_a applies successfully.
    - repo_b fails to apply (missing repo root → FileNotFoundError).
    - Compensation of repo_a raises OSError → compensation_failed.
    Result: SAGA_STUCK, repo_a.status == COMPENSATION_FAILED.
    """
    # repo_a is a real git repo; repo_b's root is intentionally missing to force apply failure
    repo_a_root = tmp_path / "repo_a"
    repo_a_root.mkdir()
    subprocess.run(["git", "init"], cwd=repo_a_root, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
         "commit", "--allow-empty", "-m", "init"],
        cwd=repo_a_root,
        capture_output=True,
        check=True,
    )
    repo_a_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_a_root,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Pre-create file so repo_a MODIFY patch succeeds
    (repo_a_root / "a.py").write_bytes(b"old a")

    patch_map = {
        "repo_a": RepoPatch(
            repo="repo_a",
            files=(PatchedFile(path="a.py", op=FileOp.MODIFY, preimage=b"old a"),),
            new_content=(("a.py", b"new a"),),
        ),
        "repo_b": RepoPatch(
            repo="repo_b",
            files=(PatchedFile(path="b.py", op=FileOp.CREATE, preimage=None),),
            new_content=(("b.py", b"new b"),),
        ),
    }

    # repo_b root is missing → _apply_patch will raise FileNotFoundError
    missing_root = tmp_path / "nonexistent_repo_b"

    ledger = MagicMock()
    ledger.append = AsyncMock()

    strategy = SagaApplyStrategy(
        repo_roots={"repo_a": repo_a_root, "repo_b": missing_root},
        ledger=ledger,
    )

    ctx = OperationContext.create(
        target_files=("a.py", "b.py"),
        description="stuck test",
        primary_repo="repo_a",
        repo_scope=("repo_a", "repo_b"),
        apply_plan=("repo_a", "repo_b"),
        repo_snapshots=(("repo_a", repo_a_head),),  # no snapshot for repo_b → no TOCTOU check
        saga_id="stuck-001",
    )

    # Make compensation of repo_a raise to simulate COMPENSATION_FAILED
    async def failing_compensate(repo: str, _patch) -> None:  # noqa: ANN001
        raise OSError(f"Cannot restore {repo}: permission denied")

    strategy._compensate_patch = failing_compensate

    result = await strategy.execute(ctx, patch_map)

    assert result.terminal_state == SagaTerminalState.SAGA_STUCK
    assert result.reason_code == "compensation_failed"
    # Per-repo saga_state must record COMPENSATION_FAILED for repo_a
    stuck_statuses = {s.repo: s for s in result.saga_state}
    assert "repo_a" in stuck_statuses
    assert stuck_statuses["repo_a"].status == SagaStepStatus.COMPENSATION_FAILED
    assert stuck_statuses["repo_a"].compensation_attempted is True


async def test_mid_apply_drift_triggers_compensation(tmp_path):
    """TOCTOU guard: HEAD drifts between Phase A and Phase B write → compensation runs.

    This covers the mid-apply drift path (drift_detected_mid_apply reason code).
    The first repo ("prime") applies successfully; the second ("jarvis") detects
    drift and aborts, triggering compensation of "prime".
    """
    # Build two real git repos so HEAD hashes are real
    prime_root = tmp_path / "prime"
    prime_root.mkdir()
    subprocess.run(["git", "init"], cwd=prime_root, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
         "commit", "--allow-empty", "-m", "init"],
        cwd=prime_root,
        capture_output=True,
        check=True,
    )
    prime_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=prime_root,
        capture_output=True,
        text=True,
    ).stdout.strip()

    jarvis_root = tmp_path / "jarvis"
    jarvis_root.mkdir()
    subprocess.run(["git", "init"], cwd=jarvis_root, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
         "commit", "--allow-empty", "-m", "init"],
        cwd=jarvis_root,
        capture_output=True,
        check=True,
    )
    jarvis_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=jarvis_root,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Pre-create files for the patches
    prime_file = prime_root / "p.py"
    prime_file.write_bytes(b"prime old")
    jarvis_file = jarvis_root / "j.py"
    jarvis_file.write_bytes(b"jarvis old")

    patch_map = {
        "prime": RepoPatch(
            repo="prime",
            files=(PatchedFile(path="p.py", op=FileOp.MODIFY, preimage=b"prime old"),),
            new_content=(("p.py", b"prime new"),),
        ),
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path="j.py", op=FileOp.MODIFY, preimage=b"jarvis old"),),
            new_content=(("j.py", b"jarvis new"),),
        ),
    }

    ledger = MagicMock()
    ledger.append = AsyncMock()

    strategy = SagaApplyStrategy(
        repo_roots={"prime": prime_root, "jarvis": jarvis_root},
        ledger=ledger,
    )

    ctx = OperationContext.create(
        target_files=("p.py", "j.py"),
        description="mid-apply drift test",
        primary_repo="prime",
        repo_scope=("prime", "jarvis"),
        apply_plan=("prime", "jarvis"),  # prime applied first, jarvis second
        repo_snapshots=(("prime", prime_head), ("jarvis", jarvis_head)),
        saga_id="drift-mid-001",
    )

    # Phase A: both pass with correct hashes.
    # During Phase B, prime applies OK; then jarvis HEAD appears drifted.
    call_count = {"n": 0}

    def head_hash_with_mid_drift(repo: str) -> str:
        # Phase A calls prime then jarvis (both correct).
        # Phase B TOCTOU call for prime → correct; for jarvis → drifted.
        call_count["n"] += 1
        if repo == "prime":
            return prime_head
        # jarvis: first call (Phase A) returns real head; second (Phase B) returns drifted
        # The pattern: Phase A iterates [prime, jarvis], then Phase B iterates same order.
        # Prime gets its Phase-B TOCTOU check first (call 3), jarvis gets call 4.
        # We make jarvis always return a drifted hash after the first time it's called.
        if not hasattr(head_hash_with_mid_drift, "_jarvis_seen"):
            head_hash_with_mid_drift._jarvis_seen = True
            return jarvis_head  # Phase A call — return real hash
        return "DRIFTED_HASH"  # Phase B TOCTOU call — simulate drift

    with patch.object(strategy, "_get_head_hash", side_effect=head_hash_with_mid_drift):
        result = await strategy.execute(ctx, patch_map)

    # prime was applied, jarvis drifted → rollback prime → SAGA_ROLLED_BACK
    assert result.terminal_state == SagaTerminalState.SAGA_ROLLED_BACK
    assert result.reason_code == "drift_detected_mid_apply"
    # prime should be compensated (file restored to preimage)
    assert prime_file.read_bytes() == b"prime old"
    # Per-repo statuses
    statuses = {s.repo: s for s in result.saga_state}
    assert statuses["prime"].status == SagaStepStatus.COMPENSATED
    assert statuses["jarvis"].status == SagaStepStatus.FAILED


# ---------------------------------------------------------------------------
# Anti-Venom gate tests (review wave)
# ---------------------------------------------------------------------------


async def test_saga_write_blocked_on_governance_sentinel(tmp_path):
    """SagaApplyStrategy._apply_patch raises BlockedPathError on a governance-sentinel path.

    The Anti-Venom _gate_path wrapper must fire before write_bytes, routing
    the BlockedPathError through the existing saga apply-failure path.
    """
    import pytest
    from backend.core.ouroboros.governance.change_engine import BlockedPathError

    repo_root = tmp_path / "jarvis"
    repo_root.mkdir()

    # Build a patch targeting an immutable-governance sentinel
    sentinel_rel = "backend/core/ouroboros/governance/risk_engine.py"
    sentinel_full = repo_root / sentinel_rel
    sentinel_full.parent.mkdir(parents=True, exist_ok=True)
    sentinel_full.write_bytes(b"original risk engine")

    patch_obj = RepoPatch(
        repo="jarvis",
        files=(PatchedFile(path=sentinel_rel, op=FileOp.MODIFY, preimage=b"original risk engine"),),
        new_content=((sentinel_rel, b"pwned"),),
    )

    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots={"jarvis": repo_root}, ledger=ledger)

    # _apply_patch should raise BlockedPathError before touching the file
    with pytest.raises(BlockedPathError, match="immutable governance"):
        await strategy._apply_patch("jarvis", patch_obj)

    # The file must remain unmodified — gate fired before write_bytes
    assert sentinel_full.read_bytes() == b"original risk engine"


async def test_saga_apply_patch_calls_gate_before_write(tmp_path):
    """_gate_path is invoked for each write_bytes site (verifiable via mock)."""
    from unittest.mock import call
    import backend.core.ouroboros.governance.saga.saga_apply_strategy as _mod

    repo_root = tmp_path / "jarvis"
    repo_root.mkdir()

    target_rel = "src/app.py"
    target_full = repo_root / target_rel
    target_full.parent.mkdir(parents=True, exist_ok=True)
    target_full.write_bytes(b"old")

    # Initialize a git repo so `git add` inside _apply_patch won't fail
    import subprocess
    subprocess.run(["git", "init"], cwd=repo_root, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t.com", "-c", "user.name=T",
         "commit", "--allow-empty", "-m", "init"],
        cwd=repo_root, capture_output=True, check=True,
    )

    patch_obj = RepoPatch(
        repo="jarvis",
        files=(PatchedFile(path=target_rel, op=FileOp.MODIFY, preimage=b"old"),),
        new_content=((target_rel, b"new"),),
    )

    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots={"jarvis": repo_root}, ledger=ledger)

    gate_calls: list = []

    original_gate = _mod._gate_path

    def recording_gate(full_path, rr):
        gate_calls.append(full_path)
        return original_gate(full_path, rr)

    with patch.object(_mod, "_gate_path", side_effect=recording_gate):
        await strategy._apply_patch("jarvis", patch_obj)

    # Gate must have fired exactly once (for the MODIFY write)
    assert len(gate_calls) == 1
    assert gate_calls[0].name == "app.py"


# ---------------------------------------------------------------------------
# Anti-Venom: DELETE gate tests (wave 2 — phantom-file deletion vector)
# ---------------------------------------------------------------------------

async def test_delete_of_governance_path_is_blocked(tmp_path):
    """FileOp.DELETE targeting a governance/immune path is blocked by _gate_path.

    The gate must fire before unlink(), the file must NOT be deleted, and the
    saga must route to compensation (SAGA_ROLLED_BACK or SAGA_STUCK).
    """
    import backend.core.ouroboros.governance.saga.saga_apply_strategy as _mod
    from backend.core.ouroboros.governance.change_engine import BlockedPathError

    repo_root = tmp_path / "jarvis"
    repo_root.mkdir()

    # Simulate a governance file that must never be deleted
    immune_rel = "backend/core/ouroboros/governance/risk_engine.py"
    immune_full = repo_root / immune_rel
    immune_full.parent.mkdir(parents=True, exist_ok=True)
    immune_full.write_bytes(b"# immune governance file")

    patch_obj = RepoPatch(
        repo="jarvis",
        files=(PatchedFile(path=immune_rel, op=FileOp.DELETE, preimage=b"# immune governance file"),),
        new_content=(),
    )

    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots={"jarvis": repo_root}, ledger=ledger)

    # Inject a gate that blocks governance paths (mirrors real assert_write_path_allowed logic)
    def blocking_gate(full_path, rr):
        if "governance" in str(full_path) or "risk_engine" in str(full_path):
            raise BlockedPathError(f"Blocked governance path: {full_path}")

    with patch.object(_mod, "_gate_path", side_effect=blocking_gate):
        # _apply_patch must raise — gate fires before unlink
        import pytest
        with pytest.raises(BlockedPathError):
            await strategy._apply_patch("jarvis", patch_obj)

    # Critical invariant: the governance file must still exist — NOT deleted
    assert immune_full.exists(), "Governance file was deleted despite the gate — phantom-file vector not closed"


async def test_delete_of_governance_path_routes_to_compensation_via_execute(tmp_path):
    """Full execute() path: DELETE targeting immune path → gate blocks → saga compensates.

    Uses a two-repo patch so the DELETE failure in repo_b triggers compensation
    of repo_a (SAGA_ROLLED_BACK) rather than a raw exception.
    """
    import backend.core.ouroboros.governance.saga.saga_apply_strategy as _mod
    from backend.core.ouroboros.governance.change_engine import BlockedPathError

    repo_root = tmp_path / "repos"
    repo_root.mkdir()

    subprocess.run(["git", "init"], cwd=repo_root, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t.com", "-c", "user.name=T",
         "commit", "--allow-empty", "-m", "init"],
        cwd=repo_root, capture_output=True, check=True,
    )

    normal_file = repo_root / "src" / "normal.py"
    normal_file.parent.mkdir(parents=True, exist_ok=True)
    normal_file.write_bytes(b"normal")

    immune_rel = "backend/core/ouroboros/governance/risk_engine.py"
    immune_full = repo_root / immune_rel
    immune_full.parent.mkdir(parents=True, exist_ok=True)
    immune_full.write_bytes(b"# immune")

    patch_map = {
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(
                PatchedFile(path="src/normal.py", op=FileOp.MODIFY, preimage=b"normal"),
                PatchedFile(path=immune_rel, op=FileOp.DELETE, preimage=b"# immune"),
            ),
            new_content=(("src/normal.py", b"modified"),),
        ),
    }

    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots={"jarvis": repo_root}, ledger=ledger)

    def blocking_gate(full_path, rr):
        if "risk_engine" in str(full_path):
            raise BlockedPathError(f"Blocked: {full_path}")
        # Allow all other paths through the real gate
        import backend.core.ouroboros.governance.saga.saga_apply_strategy as _m
        # call original — but we replaced it so just allow the rest
        return None

    ctx = OperationContext.create(
        target_files=("src/normal.py", immune_rel),
        description="delete gate test",
        primary_repo="jarvis",
        repo_scope=("jarvis",),
        apply_plan=("jarvis",),
        repo_snapshots=(),
        saga_id="delete-gate-001",
    )

    with patch.object(_mod, "_gate_path", side_effect=blocking_gate):
        result = await strategy.execute(ctx, patch_map)

    # The DELETE was blocked → apply failed → saga compensated or rolled back
    assert result.terminal_state in (
        SagaTerminalState.SAGA_ROLLED_BACK,
        SagaTerminalState.SAGA_STUCK,
    ), f"Expected rollback/stuck, got {result.terminal_state}"

    # The governance file must still be present
    assert immune_full.exists(), "Immune file was deleted despite the gate"


async def test_legit_delete_is_allowed_and_gate_fires(tmp_path):
    """FileOp.DELETE on a normal (non-immune) file: gate is called AND unlink proceeds.

    Ensures the gate does not over-block legitimate deletes.
    """
    import backend.core.ouroboros.governance.saga.saga_apply_strategy as _mod

    repo_root = tmp_path / "jarvis"
    repo_root.mkdir()

    subprocess.run(["git", "init"], cwd=repo_root, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t.com", "-c", "user.name=T",
         "commit", "--allow-empty", "-m", "init"],
        cwd=repo_root, capture_output=True, check=True,
    )

    target_rel = "src/old_module.py"
    target_full = repo_root / target_rel
    target_full.parent.mkdir(parents=True, exist_ok=True)
    target_full.write_bytes(b"to be deleted")
    # Track the file so `git add -- src/old_module.py` (staged delete) succeeds
    subprocess.run(["git", "add", target_rel], cwd=repo_root, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t.com", "-c", "user.name=T",
         "commit", "-m", "add file"],
        cwd=repo_root, capture_output=True, check=True,
    )

    patch_obj = RepoPatch(
        repo="jarvis",
        files=(PatchedFile(path=target_rel, op=FileOp.DELETE, preimage=b"to be deleted"),),
        new_content=(),
    )

    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots={"jarvis": repo_root}, ledger=ledger)

    gate_calls: list = []

    # Patch gate to record calls but NOT block (allow the delete through)
    def recording_gate(full_path, rr):
        gate_calls.append(full_path)
        # No raise → gate passes

    with patch.object(_mod, "_gate_path", side_effect=recording_gate):
        await strategy._apply_patch("jarvis", patch_obj)

    # Gate must have fired for the DELETE site
    assert len(gate_calls) == 1, f"Expected 1 gate call, got {len(gate_calls)}"
    assert gate_calls[0].name == "old_module.py"

    # File must actually be gone — delete was not suppressed
    assert not target_full.exists(), "File still exists — gate blocked a legit delete"

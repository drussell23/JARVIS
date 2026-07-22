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
    monkeypatch.delenv("JARVIS_APPLY_FREEZE_MAX_MUTATIONS", raising=False)
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


async def test_mutation_budget_knob(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_APPLY_FREEZE_MAX_MUTATIONS", "2")
    (git_repo / "module.py").write_text("VALUE = 4\n")
    s1 = await aff.flush_and_freeze("op-a", ["module.py"], git_repo)
    assert s1["frozen"] is False and aff.is_frozen() is False
    s2 = await aff.flush_and_freeze("op-b", ["module.py"], git_repo)
    assert s2["frozen"] is True and aff.is_frozen() is True


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
    assert "_apply_flush_freeze.is_frozen()" in src
    assert "FROZEN_DENIAL_REASON" in src
    # The check precedes the pre-write gate (source-order pin).
    assert src.index("_apply_flush_freeze.is_frozen()") < src.index(
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

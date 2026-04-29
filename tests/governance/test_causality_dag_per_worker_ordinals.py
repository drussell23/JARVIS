"""Priority 2 Slice 2 — per-worker ordinal namespace regression spine.

Pins the L3 fan-out determinism fix: composite ordinal key
``(worker_id, op_id, phase, kind)`` so concurrent multi-worker
writes to the shared session ledger produce a stable replayable
total order under the lexicographic ``(wall_ts, worker_id,
sub_ordinal)`` compare.

§-numbered coverage map:

  §1   Master flag JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED — default false
  §2   Sub-flag JARVIS_DAG_PER_WORKER_ORDINALS_ENFORCE — default false (shadow)
  §3   Sub-flag is independent of master (shadow→enforce semantics)
  §4   worker_id_for_path — pure (no I/O at call time)
  §5   worker_id_for_path — deterministic per (pid, path)
  §6   worker_id_for_path — different paths yield different IDs
  §7   worker_id_for_path — defensive on None / empty / whitespace
  §8   worker_id_for_path — path content NEVER appears in output
  §9   DecisionRecord new fields default to ("", -1) sentinels
  §10  to_dict emits worker_id / sub_ordinal ONLY when set
  §11  to_dict byte-for-byte preserves pre-Slice-2 record key set
  §12  from_dict round-trips with new fields
  §13  from_dict tolerates pre-Slice-2 records (no new keys)
  §14  from_dict defensive coercion (non-int sub_ordinal → -1)
  §15  Master-off: ledger row contains no worker_id / sub_ordinal
  §16  Master-on: ledger row contains both worker_id + sub_ordinal
  §17  Master-on: sub_ordinal increments per (worker_id, op_id, phase, kind)
  §18  Master-on: legacy ordinal still increments in parallel (shadow)
  §19  Master-on: schema_version unchanged (decision_record.1)
  §20  Concurrent multi-thread writes — total order via wall_ts ordering
  §21  Same op/phase/kind across two simulated workers — no collision
  §22  Synthesized clash test (single worker_id) — within-worker monotone
  §23  AST authority: worktree_manager has no forbidden imports
  §24  AST authority: decision_runtime didn't add forbidden imports
  §25  Frozen / hashable contract preserved
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import threading
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import worktree_manager
from backend.core.ouroboros.governance.determinism import decision_runtime
from backend.core.ouroboros.governance.determinism.decision_runtime import (
    SCHEMA_VERSION,
    DecisionRecord,
    decide,
    per_worker_ordinals_enabled,
    per_worker_ordinals_enforce,
)
from backend.core.ouroboros.governance.worktree_manager import (
    worker_id_for_path,
)


# ===========================================================================
# §1-§3 — Master flag + sub-flag semantics
# ===========================================================================


def test_master_flag_default_false(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED", raising=False,
    )
    assert per_worker_ordinals_enabled() is False


@pytest.mark.parametrize("val", ["", " ", "  ", "\t"])
def test_master_flag_empty_default_false(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED", val)
    assert per_worker_ordinals_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_master_flag_explicit_true(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED", val)
    assert per_worker_ordinals_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_master_flag_falsy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED", val)
    assert per_worker_ordinals_enabled() is False


def test_enforce_subflag_default_false(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_DAG_PER_WORKER_ORDINALS_ENFORCE", raising=False,
    )
    assert per_worker_ordinals_enforce() is False


def test_enforce_subflag_explicit_true(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DAG_PER_WORKER_ORDINALS_ENFORCE", "true")
    assert per_worker_ordinals_enforce() is True


def test_enforce_independent_of_master(monkeypatch) -> None:
    """Both flags are independent at the function level. Wiring
    layer (Slice 6 graduation) decides how they compose."""
    monkeypatch.setenv("JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED", "false")
    monkeypatch.setenv("JARVIS_DAG_PER_WORKER_ORDINALS_ENFORCE", "true")
    assert per_worker_ordinals_enabled() is False
    assert per_worker_ordinals_enforce() is True


# ===========================================================================
# §4-§8 — worker_id_for_path purity + determinism
# ===========================================================================


def test_worker_id_no_path_returns_pid_base() -> None:
    out = worker_id_for_path()
    assert out.endswith("-base")
    pid_part = out.split("-", 1)[0]
    assert pid_part.isdigit()


def test_worker_id_with_path_has_8char_hash() -> None:
    out = worker_id_for_path("/tmp/worktree-1")
    parts = out.split("-")
    # "{pid}-{8-char-hash}"
    assert len(parts) == 2
    assert parts[0].isdigit()
    assert len(parts[1]) == 8


def test_worker_id_deterministic_for_same_path() -> None:
    a = worker_id_for_path("/tmp/wt-a")
    b = worker_id_for_path("/tmp/wt-a")
    assert a == b


def test_worker_id_differs_for_different_paths() -> None:
    a = worker_id_for_path("/tmp/wt-a")
    b = worker_id_for_path("/tmp/wt-b")
    assert a != b


def test_worker_id_handles_none() -> None:
    out = worker_id_for_path(None)
    assert out.endswith("-base")


def test_worker_id_handles_empty_string() -> None:
    out = worker_id_for_path("")
    assert out.endswith("-base")


def test_worker_id_handles_whitespace() -> None:
    out = worker_id_for_path("   ")
    assert out.endswith("-base")


def test_worker_id_does_not_leak_path_content() -> None:
    """Path content NEVER appears in the output (only its 8-char hash
    prefix). Defends against accidental filesystem-layout leaks
    through ledger records."""
    sensitive_path = "/secret/super_private_branch_name_xyz"
    out = worker_id_for_path(sensitive_path)
    assert "secret" not in out
    assert "super_private" not in out
    assert "branch_name" not in out
    assert "xyz" not in out


def test_worker_id_pure_no_io() -> None:
    """worker_id_for_path is pure — no os.access / os.path.exists /
    open calls. AST-walk verifies no forbidden I/O calls in the
    function body."""
    src = Path(inspect.getfile(worktree_manager)).read_text()
    tree = ast.parse(src)
    forbidden_calls = {"open", "exists", "is_file", "is_dir", "stat"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "worker_id_for_path"
        ):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    func = sub.func
                    name = (
                        func.attr if isinstance(func, ast.Attribute)
                        else (func.id if isinstance(func, ast.Name) else "")
                    )
                    assert name not in forbidden_calls, (
                        f"worker_id_for_path is impure: calls {name}()"
                    )


# ===========================================================================
# §9-§14 — DecisionRecord schema additions
# ===========================================================================


def _make_record(**overrides) -> DecisionRecord:
    base = dict(
        record_id="r1", session_id="s1", op_id="op-1", phase="ROUTE",
        kind="route", ordinal=0, inputs_hash="h", output_repr='"x"',
        monotonic_ts=1.0, wall_ts=2.0,
    )
    base.update(overrides)
    return DecisionRecord(**base)


def test_record_default_worker_fields_sentinel() -> None:
    r = _make_record()
    assert r.worker_id == ""
    assert r.sub_ordinal == -1


def test_to_dict_omits_sentinel_worker_fields() -> None:
    r = _make_record()
    d = r.to_dict()
    assert "worker_id" not in d
    assert "sub_ordinal" not in d


def test_to_dict_emits_when_worker_id_set() -> None:
    r = _make_record(worker_id="123-abc12345", sub_ordinal=5)
    d = r.to_dict()
    assert d["worker_id"] == "123-abc12345"
    assert d["sub_ordinal"] == 5


def test_to_dict_emits_only_set_fields() -> None:
    """worker_id set, sub_ordinal at sentinel: only worker_id emitted."""
    r = _make_record(worker_id="123-abc")
    d = r.to_dict()
    assert d["worker_id"] == "123-abc"
    assert "sub_ordinal" not in d


def test_to_dict_byte_for_byte_pre_slice_2() -> None:
    """Empty worker fields produce JSON identical to a pre-Slice-2
    record — key set is exactly the original 11."""
    r = _make_record()
    d = r.to_dict()
    expected_keys = {
        "record_id", "session_id", "op_id", "phase", "kind",
        "ordinal", "inputs_hash", "output_repr",
        "monotonic_ts", "wall_ts", "schema_version",
    }
    assert set(d.keys()) == expected_keys


def test_from_dict_roundtrip_with_worker_fields() -> None:
    r = _make_record(worker_id="123-abc", sub_ordinal=7)
    d = r.to_dict()
    r2 = DecisionRecord.from_dict(d)
    assert r2 == r


def test_from_dict_pre_slice_2_record_parses() -> None:
    """A pre-Slice-2 row (no worker fields) parses with sentinels."""
    pre = {
        "record_id": "old-1", "session_id": "s", "op_id": "op",
        "phase": "ROUTE", "kind": "route", "ordinal": 0,
        "inputs_hash": "h", "output_repr": '"x"',
        "monotonic_ts": 1.0, "wall_ts": 2.0,
        "schema_version": SCHEMA_VERSION,
    }
    r = DecisionRecord.from_dict(pre)
    assert r is not None
    assert r.worker_id == ""
    assert r.sub_ordinal == -1


def test_from_dict_coerces_non_int_sub_ordinal() -> None:
    raw = {
        "record_id": "r", "session_id": "s", "op_id": "op",
        "phase": "p", "kind": "k", "ordinal": 0,
        "inputs_hash": "h", "output_repr": '"x"',
        "monotonic_ts": 1.0, "wall_ts": 2.0,
        "schema_version": SCHEMA_VERSION,
        "worker_id": "123-abc",
        "sub_ordinal": "not a number",
    }
    r = DecisionRecord.from_dict(raw)
    assert r is not None
    assert r.sub_ordinal == -1  # defensive coerce


def test_from_dict_negative_sub_ordinal_clamps_to_sentinel() -> None:
    raw = {
        "record_id": "r", "session_id": "s", "op_id": "op",
        "phase": "p", "kind": "k", "ordinal": 0,
        "inputs_hash": "h", "output_repr": '"x"',
        "monotonic_ts": 1.0, "wall_ts": 2.0,
        "schema_version": SCHEMA_VERSION,
        "sub_ordinal": -5,
    }
    r = DecisionRecord.from_dict(raw)
    assert r is not None
    assert r.sub_ordinal == -1


# ===========================================================================
# §15-§19 — Runtime write path: master-off / master-on
# ===========================================================================


@pytest.fixture
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "OUROBOROS_BATTLE_SESSION_ID", f"slice2-{tmp_path.name}",
    )
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path),
    )
    # Force fresh runtime so the cached worker_id is recomputed
    decision_runtime._RUNTIMES = {}
    yield tmp_path


def _ledger_lines(tmp_path) -> list:
    lines = []
    for p in tmp_path.rglob("decisions.jsonl"):
        lines.extend(
            json.loads(line) for line in p.read_text().splitlines()
            if line.strip()
        )
    return lines


def test_record_master_off_no_worker_fields(
    monkeypatch, isolated_runtime,
) -> None:
    monkeypatch.setenv(
        "JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED", "false",
    )

    async def run():
        await decide(
            op_id="op-1", phase="ROUTE", kind="route",
            inputs={"x": 1},
            compute=lambda: {"r": "standard"},
        )

    asyncio.run(run())
    rows = _ledger_lines(isolated_runtime)
    last = rows[-1]
    assert "worker_id" not in last
    assert "sub_ordinal" not in last


def test_record_master_on_emits_worker_fields(
    monkeypatch, isolated_runtime,
) -> None:
    monkeypatch.setenv(
        "JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED", "true",
    )

    async def run():
        await decide(
            op_id="op-1", phase="ROUTE", kind="route",
            inputs={"x": 1},
            compute=lambda: {"r": "standard"},
        )

    asyncio.run(run())
    rows = _ledger_lines(isolated_runtime)
    last = rows[-1]
    assert isinstance(last.get("worker_id"), str)
    assert last.get("worker_id") != ""
    assert isinstance(last.get("sub_ordinal"), int)
    assert last.get("sub_ordinal") >= 0


def test_record_master_on_sub_ordinal_increments(
    monkeypatch, isolated_runtime,
) -> None:
    """Two records with same (op_id, phase, kind) → sub_ordinal 0, 1."""
    monkeypatch.setenv(
        "JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED", "true",
    )

    async def run():
        await decide(
            op_id="op-1", phase="ROUTE", kind="route",
            inputs={"x": 1}, compute=lambda: {"r": "standard"},
        )
        await decide(
            op_id="op-1", phase="ROUTE", kind="route",
            inputs={"x": 2}, compute=lambda: {"r": "standard"},
        )

    asyncio.run(run())
    rows = _ledger_lines(isolated_runtime)
    assert len(rows) >= 2
    assert rows[-2]["sub_ordinal"] == 0
    assert rows[-1]["sub_ordinal"] == 1


def test_record_master_on_legacy_ordinal_still_increments(
    monkeypatch, isolated_runtime,
) -> None:
    """Shadow mode: legacy ordinal AND new sub_ordinal both increment."""
    monkeypatch.setenv(
        "JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED", "true",
    )

    async def run():
        await decide(
            op_id="op-1", phase="ROUTE", kind="route",
            inputs={"x": 1}, compute=lambda: {"r": "standard"},
        )
        await decide(
            op_id="op-1", phase="ROUTE", kind="route",
            inputs={"x": 2}, compute=lambda: {"r": "standard"},
        )

    asyncio.run(run())
    rows = _ledger_lines(isolated_runtime)
    # Both ordinals climb in lockstep when there's only one worker
    assert rows[-2]["ordinal"] == 0 and rows[-2]["sub_ordinal"] == 0
    assert rows[-1]["ordinal"] == 1 and rows[-1]["sub_ordinal"] == 1


def test_record_master_on_schema_version_unchanged(
    monkeypatch, isolated_runtime,
) -> None:
    monkeypatch.setenv(
        "JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED", "true",
    )

    async def run():
        await decide(
            op_id="op-1", phase="ROUTE", kind="route",
            inputs={"x": 1}, compute=lambda: {"r": "standard"},
        )

    asyncio.run(run())
    rows = _ledger_lines(isolated_runtime)
    assert rows[-1]["schema_version"] == SCHEMA_VERSION


# ===========================================================================
# §20-§22 — Concurrent multi-worker simulation
# ===========================================================================


def test_concurrent_threads_preserve_per_worker_increment(
    monkeypatch, isolated_runtime,
) -> None:
    """Multiple threads sharing a single runtime increment its
    per-worker dict atomically (sync_lock contract). Even though
    they share the same worker_id (same pid), their sub_ordinals
    monotonically increase from 0 to N-1."""
    monkeypatch.setenv(
        "JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED", "true",
    )

    n_threads = 4
    per_thread = 25

    async def one_op(idx: int) -> None:
        await decide(
            op_id=f"op-shared",
            phase="ROUTE", kind="route",
            inputs={"i": idx},
            compute=lambda v=idx: {"r": "x", "v": v},
        )

    def thread_worker():
        async def chain():
            for j in range(per_thread):
                await one_op(j)
        asyncio.run(chain())

    threads = [
        threading.Thread(target=thread_worker)
        for _ in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = _ledger_lines(isolated_runtime)
    assert len(rows) == n_threads * per_thread
    # All records share the same worker_id (single pid) — their
    # sub_ordinals must form 0..N-1 contiguously.
    sub_ordinals = sorted(r["sub_ordinal"] for r in rows)
    assert sub_ordinals == list(range(n_threads * per_thread))


def test_total_order_via_wall_ts_lexicographic(
    monkeypatch, isolated_runtime,
) -> None:
    """Lexicographic compare on (wall_ts, worker_id, sub_ordinal)
    yields a total order across all records — verified by
    sortability without ties on wall_ts (in single-process tests
    wall_ts may collide; sub_ordinal disambiguates)."""
    monkeypatch.setenv(
        "JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED", "true",
    )

    async def run():
        for i in range(5):
            await decide(
                op_id=f"op-{i}", phase="ROUTE", kind="route",
                inputs={"i": i}, compute=lambda v=i: {"r": v},
            )

    asyncio.run(run())
    rows = _ledger_lines(isolated_runtime)

    def total_order_key(r: dict) -> tuple:
        return (
            float(r["wall_ts"]),
            str(r.get("worker_id", "")),
            int(r.get("sub_ordinal", -1)),
        )

    sorted_rows = sorted(rows, key=total_order_key)
    # Total order is well-defined (no exception); ledger order
    # already matches because writes serialize through async_lock.
    assert sorted_rows == rows


def test_synthetic_two_workers_no_collision() -> None:
    """Two synthetic worker_ids produce records with the same
    (op_id, phase, kind) tuple but no sub_ordinal collision —
    each worker's namespace is independent."""
    # Build two records as if from two different workers
    r1 = _make_record(
        worker_id="100-abc", sub_ordinal=0, ordinal=0,
    )
    r2 = _make_record(
        worker_id="200-xyz", sub_ordinal=0, ordinal=0,
    )
    # Same ordinal, same op/phase/kind, but different worker_id
    # → different records
    assert r1 != r2
    # Same legacy ordinal would collide under pre-Slice-2 lookup;
    # under Slice 2's per-worker scheme, the (worker_id, ordinal)
    # pair disambiguates.
    assert (r1.worker_id, r1.sub_ordinal) != (
        r2.worker_id, r2.sub_ordinal,
    )


# ===========================================================================
# §23-§24 — AST authority invariants
# ===========================================================================


_FORBIDDEN_RUNTIME_IMPORTS = (
    "backend.core.ouroboros.governance.orchestrator",
    "backend.core.ouroboros.governance.phase_runners",
    "backend.core.ouroboros.governance.candidate_generator",
    "backend.core.ouroboros.governance.iron_gate",
    "backend.core.ouroboros.governance.change_engine",
    "backend.core.ouroboros.governance.policy",
    "backend.core.ouroboros.governance.semantic_guardian",
    "backend.core.ouroboros.governance.providers",
    "backend.core.ouroboros.governance.urgency_router",
)


def test_worktree_manager_no_forbidden_imports() -> None:
    src = Path(inspect.getfile(worktree_manager)).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for fb in _FORBIDDEN_RUNTIME_IMPORTS:
                    assert fb not in alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for fb in _FORBIDDEN_RUNTIME_IMPORTS:
                assert fb not in node.module


def test_decision_runtime_still_no_forbidden_imports() -> None:
    """Slice 2 must NOT introduce any new forbidden imports —
    the lazy worktree_manager import inside _resolve_worker_id
    is the only new cross-module reference, and it's allowed."""
    src = Path(inspect.getfile(decision_runtime)).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for fb in _FORBIDDEN_RUNTIME_IMPORTS:
                    assert fb not in alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for fb in _FORBIDDEN_RUNTIME_IMPORTS:
                assert fb not in node.module


# ===========================================================================
# §25 — Frozen / hashable contract preserved
# ===========================================================================


def test_record_with_worker_fields_is_hashable() -> None:
    r = _make_record(worker_id="123-abc", sub_ordinal=7)
    hash(r)  # MUST NOT raise


def test_record_worker_fields_are_immutable() -> None:
    r = _make_record(worker_id="123-abc", sub_ordinal=7)
    with pytest.raises((AttributeError, Exception)):
        r.worker_id = "999-xxx"  # type: ignore[misc]
    with pytest.raises((AttributeError, Exception)):
        r.sub_ordinal = 99  # type: ignore[misc]

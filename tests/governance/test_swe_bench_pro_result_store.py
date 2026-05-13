"""Regression spine - SWE-Bench-Pro Phase D result substrate.

Phase D bridges per-problem (EvaluationResult, ScoringResult) pairs
into an in-memory + optionally JSONL-persisted store.

Spine invariants
----------------

  1. EvaluationRecord schema round-trips (to_dict / from_dict).
  2. record() always updates in-memory cache; persistence is opt-in.
  3. record() dedupes by (instance_id, op_id) in-memory; JSONL retains
     full audit history.
  4. Master flag OFF: record returns True but no JSONL write.
  5. Master flag ON: JSONL appended via canonical flock_append_line.
  6. query() filters by instance_id / score_outcome /
     evaluation_outcome and honors limit.
  7. aggregate_score_outcomes covers all 5 ScoreOutcome values.
  8. aggregate_evaluation_outcomes covers all 7 EvaluationOutcome values.
  9. pass_rate excludes SKIPPED from denominator.
 10. replay_from_disk reconstructs the in-memory cache; idempotent;
     skips malformed rows.
 11. Module-level singleton helpers preserve identity until reset.
 12. record / query / replay never raise.

AST pins
--------

 13. Store imports canonical flock_append_line.
 14. Imports EvaluationResult + ScoringResult + outcome enums.
 15. No fcntl import anywhere in result_store.py.
 16. record / query / replay wrapped in try/except.

 17. FlagRegistry seeds: 2 specs; persistence default FALSE per
     section 33.1.
"""
from __future__ import annotations

import ast
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.governance.swe_bench_pro.evaluator import (
    EvaluationOutcome,
    EvaluationResult,
)
from backend.core.ouroboros.governance.swe_bench_pro.result_store import (
    RESULT_PATH_ENV_VAR,
    RESULT_PERSISTENCE_ENABLED_ENV_VAR,
    RESULT_RECORD_SCHEMA_VERSION,
    EvaluationRecord,
    EvaluationResultStore,
    get_default_store,
    record_evaluation,
    register_flags,
    replay_default_store_from_disk,
    reset_default_store,
)
from backend.core.ouroboros.governance.swe_bench_pro.scorer import (
    ScoreOutcome,
    ScoringResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(RESULT_PERSISTENCE_ENABLED_ENV_VAR, raising=False)
    monkeypatch.delenv(RESULT_PATH_ENV_VAR, raising=False)
    reset_default_store()
    yield
    reset_default_store()


def _make_eval(
    instance_id: str = "inst-1",
    op_id: str = "op-1",
    outcome: EvaluationOutcome = EvaluationOutcome.RESOLVED,
) -> EvaluationResult:
    return EvaluationResult(
        outcome=outcome,
        problem_instance_id=instance_id,
        op_id=op_id,
        terminal_state="applied",
        captured_patch="dummy_patch",
    )


def _make_score(
    instance_id: str = "inst-1",
    outcome: ScoreOutcome = ScoreOutcome.PASS,
    tests_passed: int = 3,
    tests_total: int = 3,
) -> ScoringResult:
    return ScoringResult(
        outcome=outcome,
        problem_instance_id=instance_id,
        tests_passed=tests_passed,
        tests_failed=tests_total - tests_passed,
        tests_total=tests_total,
        pass_rate=(tests_passed / tests_total) if tests_total else 0.0,
    )


# ---------------------------------------------------------------------------
# 1. Schema roundtrip
# ---------------------------------------------------------------------------


def test_record_to_dict_from_dict_roundtrip() -> None:
    e = _make_eval()
    s = _make_score()
    iso = datetime(2026, 5, 12, tzinfo=timezone.utc).isoformat()
    r = EvaluationRecord(evaluation=e, scoring=s, recorded_at_iso=iso)
    payload = r.to_dict()
    serialized = json.dumps(payload, sort_keys=True, default=str)
    restored = EvaluationRecord.from_dict(json.loads(serialized))
    assert restored.evaluation.problem_instance_id == e.problem_instance_id
    assert restored.evaluation.op_id == e.op_id
    assert restored.scoring.outcome == s.outcome
    assert restored.scoring.pass_rate == s.pass_rate
    assert restored.recorded_at_iso == iso
    assert restored.schema_version == RESULT_RECORD_SCHEMA_VERSION


def test_record_dedup_key_is_instance_and_op() -> None:
    r = EvaluationRecord(
        evaluation=_make_eval("inst-X", "op-Y"),
        scoring=_make_score("inst-X"),
        recorded_at_iso="",
    )
    assert r.dedup_key == ("inst-X", "op-Y")


# ---------------------------------------------------------------------------
# 2. In-memory record + query + dedup
# ---------------------------------------------------------------------------


def test_record_updates_in_memory_cache(clean_env: None) -> None:
    store = EvaluationResultStore(persistence_enabled=False)
    asyncio.run(store.record(_make_eval(), _make_score()))
    assert len(store) == 1


def test_record_dedupes_by_instance_op_pair(clean_env: None) -> None:
    store = EvaluationResultStore(persistence_enabled=False)
    asyncio.run(store.record(
        _make_eval("inst-1", "op-1"), _make_score("inst-1"),
    ))
    asyncio.run(store.record(
        _make_eval("inst-1", "op-1"),
        _make_score("inst-1", outcome=ScoreOutcome.FAIL),
    ))
    assert len(store) == 1
    records = store.query(instance_id="inst-1")
    assert records[0].scoring.outcome == ScoreOutcome.FAIL

    asyncio.run(store.record(
        _make_eval("inst-1", "op-2"), _make_score("inst-1"),
    ))
    assert len(store) == 2


def test_query_filters_by_instance_id(clean_env: None) -> None:
    store = EvaluationResultStore(persistence_enabled=False)
    asyncio.run(store.record(_make_eval("a", "op-a"), _make_score("a")))
    asyncio.run(store.record(_make_eval("b", "op-b"), _make_score("b")))
    asyncio.run(store.record(_make_eval("c", "op-c"), _make_score("c")))
    assert len(store.query(instance_id="b")) == 1
    assert len(store.query(instance_id="missing")) == 0


def test_query_filters_by_score_outcome(clean_env: None) -> None:
    store = EvaluationResultStore(persistence_enabled=False)
    asyncio.run(store.record(
        _make_eval("a", "op-a"),
        _make_score("a", outcome=ScoreOutcome.PASS),
    ))
    asyncio.run(store.record(
        _make_eval("b", "op-b"),
        _make_score("b", outcome=ScoreOutcome.FAIL),
    ))
    asyncio.run(store.record(
        _make_eval("c", "op-c"),
        _make_score("c", outcome=ScoreOutcome.PASS),
    ))
    assert len(store.query(score_outcome=ScoreOutcome.PASS)) == 2
    assert len(store.query(score_outcome=ScoreOutcome.FAIL)) == 1
    assert len(store.query(score_outcome=ScoreOutcome.PARTIAL)) == 0


def test_query_filters_by_evaluation_outcome(clean_env: None) -> None:
    store = EvaluationResultStore(persistence_enabled=False)
    asyncio.run(store.record(
        _make_eval("a", "op-a", outcome=EvaluationOutcome.RESOLVED),
        _make_score("a"),
    ))
    asyncio.run(store.record(
        _make_eval("b", "op-b", outcome=EvaluationOutcome.UNRESOLVED),
        _make_score("b", outcome=ScoreOutcome.SKIPPED),
    ))
    assert len(store.query(
        evaluation_outcome=EvaluationOutcome.RESOLVED,
    )) == 1
    assert len(store.query(
        evaluation_outcome=EvaluationOutcome.UNRESOLVED,
    )) == 1


def test_query_limit_honored(clean_env: None) -> None:
    store = EvaluationResultStore(persistence_enabled=False)
    for i in range(10):
        asyncio.run(store.record(
            _make_eval(f"inst-{i}", f"op-{i}"),
            _make_score(f"inst-{i}"),
        ))
    assert len(store.query(limit=3)) == 3
    assert len(store.query(limit=100)) == 10
    assert len(store.query()) == 10


# ---------------------------------------------------------------------------
# 3. Aggregates
# ---------------------------------------------------------------------------


def test_aggregate_score_outcomes_covers_all_five(clean_env: None) -> None:
    store = EvaluationResultStore(persistence_enabled=False)
    agg = store.aggregate_score_outcomes()
    assert agg == {"pass": 0, "partial": 0, "fail": 0, "scoring_error": 0, "skipped": 0}
    asyncio.run(store.record(
        _make_eval("a", "op-a"),
        _make_score("a", outcome=ScoreOutcome.PASS),
    ))
    asyncio.run(store.record(
        _make_eval("b", "op-b"),
        _make_score("b", outcome=ScoreOutcome.PASS),
    ))
    asyncio.run(store.record(
        _make_eval("c", "op-c"),
        _make_score("c", outcome=ScoreOutcome.FAIL),
    ))
    asyncio.run(store.record(
        _make_eval("d", "op-d"),
        _make_score("d", outcome=ScoreOutcome.PARTIAL),
    ))
    agg = store.aggregate_score_outcomes()
    assert agg == {"pass": 2, "partial": 1, "fail": 1, "scoring_error": 0, "skipped": 0}


def test_aggregate_evaluation_outcomes_covers_all_seven(
    clean_env: None,
) -> None:
    store = EvaluationResultStore(persistence_enabled=False)
    agg = store.aggregate_evaluation_outcomes()
    expected_keys = {o.value for o in EvaluationOutcome}
    assert set(agg.keys()) == expected_keys
    assert all(v == 0 for v in agg.values())


def test_pass_rate_excludes_skipped_from_denominator(
    clean_env: None,
) -> None:
    store = EvaluationResultStore(persistence_enabled=False)
    assert store.pass_rate() == 0.0
    asyncio.run(store.record(
        _make_eval("a", "op-a"),
        _make_score("a", outcome=ScoreOutcome.PASS),
    ))
    asyncio.run(store.record(
        _make_eval("b", "op-b"),
        _make_score("b", outcome=ScoreOutcome.FAIL),
    ))
    asyncio.run(store.record(
        _make_eval("c", "op-c"),
        _make_score("c", outcome=ScoreOutcome.SKIPPED),
    ))
    assert store.pass_rate() == 0.5


# ---------------------------------------------------------------------------
# 4. Persistence
# ---------------------------------------------------------------------------


def test_master_flag_off_no_jsonl_write(
    clean_env: None, tmp_path: Path,
) -> None:
    p = tmp_path / "r.jsonl"
    store = EvaluationResultStore(
        persistence_path=p, persistence_enabled=False,
    )
    ok = asyncio.run(store.record(_make_eval(), _make_score()))
    assert ok is True
    assert not p.exists()


def test_master_flag_on_writes_jsonl(
    clean_env: None, tmp_path: Path,
) -> None:
    p = tmp_path / "subdir" / "r.jsonl"
    store = EvaluationResultStore(
        persistence_path=p, persistence_enabled=True,
    )
    ok = asyncio.run(store.record(_make_eval(), _make_score()))
    assert ok is True
    assert p.exists()
    lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["schema_version"] == RESULT_RECORD_SCHEMA_VERSION
    assert parsed["evaluation"]["problem_instance_id"] == "inst-1"
    assert parsed["scoring"]["outcome"] == "pass"


def test_jsonl_retains_audit_history(
    clean_env: None, tmp_path: Path,
) -> None:
    p = tmp_path / "r.jsonl"
    store = EvaluationResultStore(
        persistence_path=p, persistence_enabled=True,
    )
    asyncio.run(store.record(_make_eval(), _make_score(
        outcome=ScoreOutcome.PASS,
    )))
    asyncio.run(store.record(_make_eval(), _make_score(
        outcome=ScoreOutcome.FAIL,
    )))
    lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert len(store) == 1
    records = store.query()
    assert records[0].scoring.outcome == ScoreOutcome.FAIL


def test_persistence_env_master_flag_honored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    reset_default_store()
    p = tmp_path / "r.jsonl"
    monkeypatch.setenv(RESULT_PERSISTENCE_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(RESULT_PATH_ENV_VAR, str(p))
    store = EvaluationResultStore()
    assert store.is_persistence_enabled() is True
    assert store.persistence_path == p
    reset_default_store()


# ---------------------------------------------------------------------------
# 5. Replay from disk
# ---------------------------------------------------------------------------


def test_replay_reconstructs_in_memory_cache(
    clean_env: None, tmp_path: Path,
) -> None:
    p = tmp_path / "r.jsonl"
    writer = EvaluationResultStore(
        persistence_path=p, persistence_enabled=True,
    )
    asyncio.run(writer.record(_make_eval("a", "op-a"), _make_score("a")))
    asyncio.run(writer.record(_make_eval("b", "op-b"), _make_score(
        "b", outcome=ScoreOutcome.FAIL,
    )))

    reader = EvaluationResultStore(
        persistence_path=p, persistence_enabled=True,
    )
    n = asyncio.run(reader.replay_from_disk())
    assert n == 2
    assert len(reader) == 2
    assert len(reader.query(score_outcome=ScoreOutcome.PASS)) == 1
    assert len(reader.query(score_outcome=ScoreOutcome.FAIL)) == 1


def test_replay_idempotent(clean_env: None, tmp_path: Path) -> None:
    p = tmp_path / "r.jsonl"
    writer = EvaluationResultStore(
        persistence_path=p, persistence_enabled=True,
    )
    asyncio.run(writer.record(_make_eval("a", "op-a"), _make_score("a")))

    reader = EvaluationResultStore(
        persistence_path=p, persistence_enabled=True,
    )
    n1 = asyncio.run(reader.replay_from_disk())
    n2 = asyncio.run(reader.replay_from_disk())
    assert n1 == 1
    assert n2 == 1
    assert len(reader) == 1


def test_replay_skips_malformed_rows(
    clean_env: None, tmp_path: Path,
) -> None:
    p = tmp_path / "r.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    valid_row = EvaluationRecord(
        evaluation=_make_eval(),
        scoring=_make_score(),
        recorded_at_iso="2026-05-12T00:00:00+00:00",
    ).to_dict()
    p.write_text(
        json.dumps(valid_row) + "\n"
        + "not-json\n"
        + json.dumps({"missing_keys": True}) + "\n"
        + json.dumps(valid_row) + "\n"
    )
    store = EvaluationResultStore(
        persistence_path=p, persistence_enabled=True,
    )
    n = asyncio.run(store.replay_from_disk())
    assert n == 2


def test_replay_missing_file_returns_zero(
    clean_env: None, tmp_path: Path,
) -> None:
    p = tmp_path / "does-not-exist.jsonl"
    store = EvaluationResultStore(
        persistence_path=p, persistence_enabled=True,
    )
    n = asyncio.run(store.replay_from_disk())
    assert n == 0
    assert len(store) == 0


# ---------------------------------------------------------------------------
# 6. Singleton helpers
# ---------------------------------------------------------------------------


def test_get_default_store_returns_singleton(clean_env: None) -> None:
    s1 = get_default_store()
    s2 = get_default_store()
    assert s1 is s2


def test_reset_default_store_replaces_singleton(clean_env: None) -> None:
    s1 = get_default_store()
    reset_default_store()
    s2 = get_default_store()
    assert s1 is not s2


def test_record_evaluation_module_helper(clean_env: None) -> None:
    ok = asyncio.run(record_evaluation(_make_eval(), _make_score()))
    assert ok is True
    assert len(get_default_store()) == 1


def test_replay_default_store_helper(
    clean_env: None, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = tmp_path / "r.jsonl"
    monkeypatch.setenv(RESULT_PERSISTENCE_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(RESULT_PATH_ENV_VAR, str(p))
    reset_default_store()
    asyncio.run(record_evaluation(_make_eval(), _make_score()))
    reset_default_store()
    n = asyncio.run(replay_default_store_from_disk())
    assert n == 1
    assert len(get_default_store()) == 1


# ---------------------------------------------------------------------------
# 7. Fail-closed contract
# ---------------------------------------------------------------------------


def test_record_handles_malformed_payload_gracefully(
    clean_env: None,
) -> None:
    store = EvaluationResultStore(persistence_enabled=False)

    class _Broken:
        outcome = None

    ok = asyncio.run(store.record(_Broken(), _make_score()))
    assert ok is False
    assert len(store) == 0


def test_query_never_raises_with_no_filters(clean_env: None) -> None:
    store = EvaluationResultStore(persistence_enabled=False)
    assert store.query() == ()
    asyncio.run(store.record(_make_eval(), _make_score()))
    assert len(store.query()) == 1


def test_clear_drops_in_memory_but_not_jsonl(
    clean_env: None, tmp_path: Path,
) -> None:
    p = tmp_path / "r.jsonl"
    store = EvaluationResultStore(
        persistence_path=p, persistence_enabled=True,
    )
    asyncio.run(store.record(_make_eval(), _make_score()))
    assert len(store) == 1
    assert p.exists()
    store.clear()
    assert len(store) == 0
    assert p.exists()
    assert len(p.read_text().splitlines()) >= 1


# ---------------------------------------------------------------------------
# 8. AST pins - composition discipline
# ---------------------------------------------------------------------------


def _store_source() -> str:
    from backend.core.ouroboros.governance.swe_bench_pro import result_store
    return Path(result_store.__file__).read_text()


def test_ast_pin_imports_canonical_flock_append_line() -> None:
    src = _store_source()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "cross_process_jsonl" in (node.module or ""):
                for alias in node.names:
                    if alias.name == "flock_append_line":
                        found = True
    assert found, (
        "result_store.py does not import canonical flock_append_line"
    )


def test_ast_pin_imports_canonical_dataclasses() -> None:
    src = _store_source()
    tree = ast.parse(src)
    needed = {
        "EvaluationResult", "EvaluationOutcome",
        "ScoringResult", "ScoreOutcome",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "evaluator" in module or "scorer" in module:
                for alias in node.names:
                    needed.discard(alias.name)
    assert not needed, (
        f"missing canonical imports: {sorted(needed)}"
    )


def test_ast_pin_no_fcntl_in_substrate() -> None:
    src = _store_source()
    assert "import fcntl" not in src, (
        "result_store.py imports fcntl - use canonical primitive"
    )
    assert "fcntl." not in src, (
        "result_store.py references fcntl - use canonical primitive"
    )


def test_ast_pin_record_query_replay_wrapped_in_try_except() -> None:
    src = _store_source()
    tree = ast.parse(src)
    methods_needing_try = {"record", "query", "replay_from_disk"}
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        if cls.name != "EvaluationResultStore":
            continue
        for fn in cls.body:
            name = getattr(fn, "name", "")
            if name not in methods_needing_try:
                continue
            text = ast.unparse(fn)
            if "try:" not in text or "except" not in text:
                raise AssertionError(
                    f"EvaluationResultStore.{name} not wrapped in try/except"
                )
            methods_needing_try.discard(name)
    assert not methods_needing_try, (
        f"missing methods: {sorted(methods_needing_try)}"
    )


# ---------------------------------------------------------------------------
# 9. FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_register_flags_seeds_two_specs() -> None:
    captured: list = []

    class _Capturer:
        def register(self, spec) -> None:
            captured.append(spec)

    count = register_flags(_Capturer())
    assert count == 2
    names = {s.name for s in captured}
    assert names == {RESULT_PERSISTENCE_ENABLED_ENV_VAR, RESULT_PATH_ENV_VAR}


def test_register_flags_persistence_default_false() -> None:
    captured: list = []

    class _Capturer:
        def register(self, spec) -> None:
            captured.append(spec)

    register_flags(_Capturer())
    master = next(
        s for s in captured
        if s.name == RESULT_PERSISTENCE_ENABLED_ENV_VAR
    )
    assert master.default is False


def test_register_flags_never_raises_on_capturer_failure() -> None:
    class _Boom:
        def register(self, spec) -> None:
            raise RuntimeError("kaboom")

    assert register_flags(_Boom()) == 0

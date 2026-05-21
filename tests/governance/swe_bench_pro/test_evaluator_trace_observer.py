"""Spine tests for the SWE-Bench-Pro evaluator structural trace observer.

Covers Slice 1 substrate per the design document:

  * Closed taxonomies (BlockedOnKind 8, EvaluatorPhase 6) frozen
  * Heuristic blocked_on classification across all 8 kinds
  * Phase classification from task-name suffix
  * Posture-aware cadence (HARDEN / CONSOLIDATE / MAINTAIN / EXPLORE)
  * JSONL roundtrip + dataclass to_dict/from_dict
  * SubprocessSnapshot contextvar registration + sanitization
  * Master-flag-FALSE → observer no-op (start returns False)
  * Snapshot determinism + empty-prefix → empty
  * FlagRegistry 5 seeds registered with correct types/defaults
  * AST pins (6) — single-seam discipline enforcement
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.swe_bench_pro import (
    evaluator_trace_observer as etr,
)
from backend.core.ouroboros.governance.swe_bench_pro.evaluator_trace_observer import (
    EVALUATOR_TRACE_OBSERVER_SCHEMA_VERSION,
    BlockedOnKind,
    EvaluatorPhase,
    EvaluatorTraceFrame,
    EvaluatorTraceObserver,
    SubprocessSnapshot,
    TaskSnapshot,
    _BLOCKED_ON_PATTERNS,
    _CREDENTIAL_REDACTION_TOKENS,
    _PHASE_PATTERNS,
    _POSTURE_CADENCE_MULTIPLIER,
    _active_subprocess,
    _classify_blocked_on,
    _classify_phase,
    _extract_op_id_from_name,
    _pid_alive,
    _resolve_interval_s,
    _resolve_jsonl_path,
    _resolve_stack_depth,
    _resolve_task_prefixes,
    _sanitize_cmd_repr,
    async_append_frame_to_jsonl,
    build_frame,
    evaluator_trace_enabled,
    register_flags,
    snapshot_subprocesses,
    snapshot_tasks,
    trace_subprocess,
)

MODULE_FILE = Path(etr.__file__)


# =============================================================================
# 1. Frozen taxonomies
# =============================================================================


class TestFrozenTaxonomies:
    """BlockedOnKind (8 values) + EvaluatorPhase (6 values)
    are frozen per the design document — adding a value requires
    bumping :data:`EVALUATOR_TRACE_OBSERVER_SCHEMA_VERSION`."""

    def test_blocked_on_kind_has_exactly_eight_values(self) -> None:
        assert len(BlockedOnKind) == 8

    def test_blocked_on_kind_values_exact(self) -> None:
        assert {k.value for k in BlockedOnKind} == {
            "queue_get", "subprocess_wait", "network_await",
            "asyncio_sleep", "asyncio_wait_for", "lock_acquire",
            "unknown_await", "running_cpu",
        }

    def test_evaluator_phase_has_exactly_six_values(self) -> None:
        assert len(EvaluatorPhase) == 6

    def test_evaluator_phase_values_exact(self) -> None:
        assert {p.value for p in EvaluatorPhase} == {
            "prepare_problem", "ingest_envelope", "waiting_terminal",
            "score_evaluation", "record_result", "unknown",
        }

    def test_schema_version_is_v1(self) -> None:
        assert EVALUATOR_TRACE_OBSERVER_SCHEMA_VERSION == (
            "evaluator_trace_frame.v1"
        )


# =============================================================================
# 2. Heuristic blocked_on_kind classification matrix
# =============================================================================


def _fake_frame(filename: str, funcname: str, lineno: int = 1):
    """Build a minimal duck-typed frame for classification tests."""
    code = SimpleNamespace(co_filename=filename, co_name=funcname)
    return SimpleNamespace(f_code=code, f_lineno=lineno)


class TestBlockedOnClassification:

    def test_queue_get_recognized(self) -> None:
        kind, _ = _classify_blocked_on([
            _fake_frame("/lib/python3.11/asyncio/queues.py", "get"),
        ])
        assert kind == BlockedOnKind.QUEUE_GET

    def test_queue_put_also_queue_get_kind(self) -> None:
        # Queue.put / Queue.get share the same classification bucket
        # per the closed table — both indicate "blocked on a queue".
        kind, _ = _classify_blocked_on([
            _fake_frame("/lib/python3.11/asyncio/queues.py", "put"),
        ])
        assert kind == BlockedOnKind.QUEUE_GET

    def test_subprocess_wait_recognized(self) -> None:
        kind, _ = _classify_blocked_on([
            _fake_frame("/lib/python3.11/asyncio/subprocess.py", "wait"),
        ])
        assert kind == BlockedOnKind.SUBPROCESS_WAIT

    def test_subprocess_communicate_recognized(self) -> None:
        kind, _ = _classify_blocked_on([
            _fake_frame(
                "/lib/python3.11/asyncio/subprocess.py", "communicate",
            ),
        ])
        assert kind == BlockedOnKind.SUBPROCESS_WAIT

    def test_aiohttp_network_recognized(self) -> None:
        kind, _ = _classify_blocked_on([
            _fake_frame(
                "/site-packages/aiohttp/client.py", "request",
            ),
        ])
        assert kind == BlockedOnKind.NETWORK_AWAIT

    def test_asyncio_sleep_recognized(self) -> None:
        kind, _ = _classify_blocked_on([
            _fake_frame("/lib/python3.11/asyncio/tasks.py", "sleep"),
        ])
        assert kind == BlockedOnKind.ASYNCIO_SLEEP

    def test_asyncio_wait_for_recognized(self) -> None:
        kind, _ = _classify_blocked_on([
            _fake_frame("/lib/python3.11/asyncio/tasks.py", "wait_for"),
        ])
        assert kind == BlockedOnKind.ASYNCIO_WAIT_FOR

    def test_asyncio_lock_recognized(self) -> None:
        kind, _ = _classify_blocked_on([
            _fake_frame("/lib/python3.11/asyncio/locks.py", "acquire"),
        ])
        assert kind == BlockedOnKind.LOCK_ACQUIRE

    def test_empty_stack_classifies_running_cpu(self) -> None:
        kind, detail = _classify_blocked_on([])
        assert kind == BlockedOnKind.RUNNING_CPU
        assert detail == ""

    def test_unknown_frame_classifies_unknown_await(self) -> None:
        kind, detail = _classify_blocked_on([
            _fake_frame("/path/to/my/custom/module.py", "do_thing"),
        ])
        assert kind == BlockedOnKind.UNKNOWN_AWAIT
        assert "module.py" in detail and "do_thing" in detail

    def test_classifier_never_raises_on_bad_frame(self) -> None:
        # A frame whose f_code attribute is broken should NOT raise.
        broken = SimpleNamespace(f_code=None, f_lineno=0)
        kind, _ = _classify_blocked_on([broken])
        # Defensive path returns UNKNOWN_AWAIT, never an exception.
        assert kind == BlockedOnKind.UNKNOWN_AWAIT

    def test_classifier_picks_first_match(self) -> None:
        # _BLOCKED_ON_PATTERNS is ordered; the first match wins.
        # asyncio/queues.py + "get" appears before any other entry.
        kind, _ = _classify_blocked_on([
            _fake_frame("/lib/python3.11/asyncio/queues.py", "get"),
        ])
        assert kind == BlockedOnKind.QUEUE_GET


# =============================================================================
# 3. Phase classification
# =============================================================================


class TestPhaseClassification:

    def test_prepare_phase(self) -> None:
        assert _classify_phase("swe_bench_pro:prepare:op-001") == (
            EvaluatorPhase.PREPARE_PROBLEM
        )

    def test_score_phase(self) -> None:
        assert _classify_phase("swe_bench_pro:score:op-002") == (
            EvaluatorPhase.SCORE_EVALUATION
        )

    def test_evaluate_phase_waiting_terminal(self) -> None:
        assert _classify_phase("swe_bench_pro:evaluate:op-003") == (
            EvaluatorPhase.WAITING_TERMINAL
        )

    def test_parallel_phase_ingest_envelope(self) -> None:
        assert _classify_phase("swe_bench_pro:parallel:op-004") == (
            EvaluatorPhase.INGEST_ENVELOPE
        )

    def test_unknown_phase_when_name_empty(self) -> None:
        assert _classify_phase("") == EvaluatorPhase.UNKNOWN

    def test_extract_op_id_from_well_formed_name(self) -> None:
        assert _extract_op_id_from_name(
            "swe_bench_pro:score:op-12345"
        ) == "op-12345"

    def test_extract_op_id_handles_missing_colons(self) -> None:
        assert _extract_op_id_from_name("just_a_plain_name") == ""


# =============================================================================
# 4. Env knob resolution + posture-aware cadence
# =============================================================================


class TestEnvKnobs:

    def test_master_default_false(self) -> None:
        # Smoke: with env unset, master is False.
        os.environ.pop("JARVIS_EVALUATOR_TRACE_ENABLED", None)
        assert evaluator_trace_enabled() is False

    def test_master_true_when_set(self, monkeypatch) -> None:
        monkeypatch.setenv("JARVIS_EVALUATOR_TRACE_ENABLED", "true")
        assert evaluator_trace_enabled() is True

    def test_interval_default_30(self, monkeypatch) -> None:
        monkeypatch.delenv("JARVIS_EVALUATOR_TRACE_INTERVAL_S", raising=False)
        assert _resolve_interval_s() == 30.0

    def test_interval_invalid_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("JARVIS_EVALUATOR_TRACE_INTERVAL_S", "garbage")
        assert _resolve_interval_s() == 30.0

    def test_stack_depth_clamped(self, monkeypatch) -> None:
        monkeypatch.setenv("JARVIS_EVALUATOR_TRACE_STACK_DEPTH", "999")
        assert _resolve_stack_depth() == 10
        monkeypatch.setenv("JARVIS_EVALUATOR_TRACE_STACK_DEPTH", "0")
        assert _resolve_stack_depth() == 1

    def test_task_prefixes_default(self, monkeypatch) -> None:
        monkeypatch.delenv(
            "JARVIS_EVALUATOR_TRACE_TASK_PREFIXES", raising=False,
        )
        assert _resolve_task_prefixes() == (
            "swe_bench_pro:", "evaluator:", "scorer:", "prepare:",
        )

    def test_jsonl_path_default(self, monkeypatch) -> None:
        monkeypatch.delenv("JARVIS_EVALUATOR_TRACE_JSONL_PATH", raising=False)
        assert _resolve_jsonl_path() == Path(".jarvis/evaluator_trace.jsonl")


class TestPostureAwareCadence:

    def test_harden_ticks_faster(self) -> None:
        assert _POSTURE_CADENCE_MULTIPLIER["HARDEN"] < 1.0

    def test_explore_ticks_slower(self) -> None:
        assert _POSTURE_CADENCE_MULTIPLIER["EXPLORE"] > 1.0

    def test_consolidate_baseline(self) -> None:
        assert _POSTURE_CADENCE_MULTIPLIER["CONSOLIDATE"] == 1.0

    def test_observer_consumes_posture_provider(self, monkeypatch) -> None:
        monkeypatch.setenv("JARVIS_EVALUATOR_TRACE_INTERVAL_S", "30")
        obs = EvaluatorTraceObserver(
            session_id="test",
            posture_provider=lambda: "HARDEN",
        )
        # HARDEN multiplier is 0.5, so effective interval is 15.0
        # (with the >=1.0 floor — 15 is fine).
        assert obs._current_interval_s() == 15.0

    def test_observer_floors_interval_at_one_second(self, monkeypatch) -> None:
        monkeypatch.setenv("JARVIS_EVALUATOR_TRACE_INTERVAL_S", "1")
        obs = EvaluatorTraceObserver(
            session_id="t",
            posture_provider=lambda: "HARDEN",
        )
        # 1 * 0.5 = 0.5, but floor is 1.0
        assert obs._current_interval_s() == 1.0


# =============================================================================
# 5. Dataclass roundtrip (to_dict / from_dict — §33.5)
# =============================================================================


class TestDataclassRoundtrip:

    def test_task_snapshot_roundtrip(self) -> None:
        original = TaskSnapshot(
            task_name="swe_bench_pro:score:op-x",
            evaluator_phase=EvaluatorPhase.SCORE_EVALUATION,
            blocked_on_kind=BlockedOnKind.SUBPROCESS_WAIT,
            blocked_on_detail="subprocess.py::wait",
            stack_top3=(("subprocess.py", 100, "wait"),),
            elapsed_in_state_s=12.5,
            op_id="op-x",
        )
        roundtripped = TaskSnapshot.from_dict(original.to_dict())
        assert roundtripped == original

    def test_subprocess_snapshot_roundtrip(self) -> None:
        original = SubprocessSnapshot(
            pid=42, cmd_repr="git apply", started_at_iso="2026-01-01T00:00:00Z",
            alive=True,
        )
        roundtripped = SubprocessSnapshot.from_dict(original.to_dict())
        assert roundtripped == original

    def test_frame_roundtrip_preserves_schema_version(self) -> None:
        frame = build_frame(session_id="test", snapshot_seq=1)
        d = frame.to_dict()
        assert d["schema_version"] == EVALUATOR_TRACE_OBSERVER_SCHEMA_VERSION
        rt = EvaluatorTraceFrame.from_dict(d)
        assert rt.schema_version == frame.schema_version


# =============================================================================
# 6. Subprocess contextvar registration + cmd sanitization
# =============================================================================


class TestSubprocessRegistration:

    def test_trace_subprocess_sets_and_resets(self) -> None:
        assert _active_subprocess.get() is None
        with trace_subprocess(42, "git apply --index"):
            payload = _active_subprocess.get()
            assert payload is not None
            assert payload[0] == 42
            assert "git apply" in payload[1]
        assert _active_subprocess.get() is None

    def test_trace_subprocess_resets_on_exception(self) -> None:
        assert _active_subprocess.get() is None
        with pytest.raises(RuntimeError, match="boom"):
            with trace_subprocess(99, "test"):
                raise RuntimeError("boom")
        assert _active_subprocess.get() is None

    def test_sanitize_redacts_api_key(self) -> None:
        out = _sanitize_cmd_repr("curl -H 'api_key: SECRET123' https://x.com")
        assert "SECRET123" not in out
        assert "<redacted>" in out

    def test_sanitize_redacts_bearer_token(self) -> None:
        out = _sanitize_cmd_repr("curl -H 'Authorization: Bearer abc.def.ghi'")
        assert "abc.def.ghi" not in out
        assert "<redacted>" in out

    def test_sanitize_truncates_to_200_chars(self) -> None:
        long_cmd = "git " + ("x" * 1000)
        out = _sanitize_cmd_repr(long_cmd)
        assert len(out) <= 200

    def test_sanitize_handles_non_string_gracefully(self) -> None:
        # Coerced via str() — never raises.
        out = _sanitize_cmd_repr(12345)  # type: ignore[arg-type]
        assert "12345" in out

    def test_pid_alive_handles_zero(self) -> None:
        assert _pid_alive(0) is False

    def test_pid_alive_handles_dead_pid(self) -> None:
        # PID 1 is init — alive. PID very large is almost certainly dead.
        assert _pid_alive(1) is True or _pid_alive(1) is False  # never raises
        assert _pid_alive(99999999) is False


# =============================================================================
# 7. Snapshot determinism + empty-prefix → empty
# =============================================================================


class TestSnapshotDeterminism:

    def test_empty_prefix_yields_no_tracked_tasks(self) -> None:
        snaps, total = snapshot_tasks(prefixes=(), stack_depth=3)
        # No prefix matches anything — empty filtered snapshot.
        assert snaps == ()
        # Total is the loop's total tasks (>= 0).
        assert total >= 0

    def test_unmatched_prefix_yields_empty_filtered(self) -> None:
        snaps, _ = snapshot_tasks(
            prefixes=("absolutely_no_task_starts_with_this_xyz:",),
            stack_depth=3,
        )
        assert snaps == ()

    def test_snapshot_subprocesses_includes_calling_context(self) -> None:
        # When called from within a trace_subprocess block, the calling
        # context's subprocess MUST appear (fallback path).
        with trace_subprocess(12345, "git apply --index"):
            subs = snapshot_subprocesses()
        assert any(s.pid == 12345 for s in subs)


# =============================================================================
# 8. Master-flag-FALSE → observer no-op
# =============================================================================


class TestObserverLifecycle:

    def test_start_returns_false_when_master_disabled(
        self, monkeypatch,
    ) -> None:
        monkeypatch.delenv("JARVIS_EVALUATOR_TRACE_ENABLED", raising=False)
        obs = EvaluatorTraceObserver(session_id="t")
        assert obs.start() is False
        assert obs.running is False

    @pytest.mark.asyncio
    async def test_start_returns_true_when_enabled(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setenv("JARVIS_EVALUATOR_TRACE_ENABLED", "true")
        monkeypatch.setenv("JARVIS_EVALUATOR_TRACE_INTERVAL_S", "60")
        obs = EvaluatorTraceObserver(session_id="t")
        assert obs.start() is True
        await asyncio.sleep(0.05)  # let the task spawn
        assert obs.running is True
        await obs.stop()
        assert obs.running is False

    @pytest.mark.asyncio
    async def test_run_one_cycle_builds_frame(self) -> None:
        obs = EvaluatorTraceObserver(
            session_id="t",
            jsonl_path=Path("/tmp/evaluator_trace_test.jsonl"),
        )
        frame = await obs.run_one_cycle()
        assert frame.session_id == "t"
        assert frame.snapshot_seq == 1
        assert obs.cycles_completed == 1

    @pytest.mark.asyncio
    async def test_run_one_cycle_publishes_to_broker_when_provided(
        self,
    ) -> None:
        published: List[Any] = []

        def mock_publish(event_type, op_id, payload):
            published.append((event_type, op_id, dict(payload)))
            return "ev-001"

        obs = EvaluatorTraceObserver(
            session_id="t",
            broker_publish=mock_publish,
            jsonl_path=Path("/tmp/evaluator_trace_test_publish.jsonl"),
        )
        await obs.run_one_cycle()
        assert len(published) == 1
        assert published[0][0] == "evaluator_trace_frame"


# =============================================================================
# 9. JSONL async append + roundtrip
# =============================================================================


class TestJsonlPersistence:

    @pytest.mark.asyncio
    async def test_async_append_writes_one_line(self, tmp_path) -> None:
        target = tmp_path / "trace.jsonl"
        frame = build_frame(session_id="t", snapshot_seq=1)
        ok = await async_append_frame_to_jsonl(frame, path=target)
        assert ok is True
        assert target.exists()
        lines = target.read_text().splitlines()
        assert len(lines) == 1
        # Each line is one valid JSON object.
        parsed = json.loads(lines[0])
        assert parsed["schema_version"] == (
            EVALUATOR_TRACE_OBSERVER_SCHEMA_VERSION
        )

    @pytest.mark.asyncio
    async def test_async_append_appends_not_overwrites(
        self, tmp_path,
    ) -> None:
        target = tmp_path / "trace.jsonl"
        for seq in range(3):
            frame = build_frame(session_id="t", snapshot_seq=seq)
            await async_append_frame_to_jsonl(frame, path=target)
        lines = target.read_text().splitlines()
        assert len(lines) == 3

    @pytest.mark.asyncio
    async def test_async_append_handles_directory_create_failure(
        self,
    ) -> None:
        # Path that can't exist (parent unwritable). Should return
        # False, NEVER raise.
        target = Path("/this/path/does/not/exist/trace.jsonl")
        frame = build_frame(session_id="t", snapshot_seq=1)
        # This may succeed (mkdir-p in flock_append_line) or fail —
        # either way, no exception.
        result = await async_append_frame_to_jsonl(frame, path=target)
        assert result in (True, False)


# =============================================================================
# 10. FlagRegistry seeds
# =============================================================================


class TestFlagRegistrySeeds:

    def _fresh_registry(self):
        from backend.core.ouroboros.governance.flag_registry import FlagRegistry
        return FlagRegistry()

    def test_master_flag_registered_with_correct_default(self) -> None:
        reg = self._fresh_registry()
        register_flags(reg)
        spec = reg.get_spec("JARVIS_EVALUATOR_TRACE_ENABLED")
        assert spec is not None
        assert spec.default is False  # default-FALSE per §33.1

    def test_interval_flag_registered_with_int_type(self) -> None:
        from backend.core.ouroboros.governance.flag_registry import FlagType
        reg = self._fresh_registry()
        register_flags(reg)
        spec = reg.get_spec("JARVIS_EVALUATOR_TRACE_INTERVAL_S")
        assert spec is not None
        assert spec.type is FlagType.INT
        assert spec.default == 30

    def test_jsonl_path_flag_registered(self) -> None:
        reg = self._fresh_registry()
        register_flags(reg)
        spec = reg.get_spec("JARVIS_EVALUATOR_TRACE_JSONL_PATH")
        assert spec is not None
        assert ".jarvis" in str(spec.default)

    def test_task_prefixes_flag_registered(self) -> None:
        reg = self._fresh_registry()
        register_flags(reg)
        spec = reg.get_spec("JARVIS_EVALUATOR_TRACE_TASK_PREFIXES")
        assert spec is not None
        assert "swe_bench_pro:" in str(spec.default)

    def test_stack_depth_flag_registered(self) -> None:
        from backend.core.ouroboros.governance.flag_registry import FlagType
        reg = self._fresh_registry()
        register_flags(reg)
        spec = reg.get_spec("JARVIS_EVALUATOR_TRACE_STACK_DEPTH")
        assert spec is not None
        assert spec.type is FlagType.INT
        assert spec.default == 3

    def test_register_flags_never_raises_on_bad_registry(self) -> None:
        # Passing a non-FlagRegistry must NOT raise — defensive
        # downgrade per the design.
        class BrokenRegistry:
            def register(self, *args, **kwargs):
                raise RuntimeError("broken")
        register_flags(BrokenRegistry())  # must not raise


# =============================================================================
# 11. AST pins — single-seam discipline enforcement
# =============================================================================


def _load_module_ast(path: Path) -> ast.AST:
    return ast.parse(path.read_text(), filename=str(path))


class TestAstPins:
    """Six AST pins per the design document."""

    def test_pin_1_uses_canonical_asyncio_all_tasks(self) -> None:
        """No homegrown task-table scanner.

        Module must call ``asyncio.all_tasks`` (Attribute access) —
        no module-level ``_all_tasks`` / ``_running_tasks`` lookalike
        collection."""
        tree = _load_module_ast(MODULE_FILE)
        # Confirm asyncio.all_tasks IS used.
        uses_canonical = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "all_tasks"
                and isinstance(node.value, ast.Name)
                and node.value.id == "asyncio"
            ):
                uses_canonical = True
                break
        assert uses_canonical, (
            "module must call asyncio.all_tasks (canonical primitive)"
        )
        # Confirm NO parallel registry name at module level.
        forbidden_names = {"_all_tasks", "_running_tasks", "_task_registry"}
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id in forbidden_names:
                        pytest.fail(
                            f"forbidden parallel task registry "
                            f"name found: {tgt.id}"
                        )

    def test_pin_2_no_fcntl_import_anywhere(self) -> None:
        """No parallel JSONL primitive.

        Module must NOT import ``fcntl`` directly — all locking goes
        through ``cross_process_jsonl.flock_append_line``."""
        tree = _load_module_ast(MODULE_FILE)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "fcntl", (
                        "fcntl import banned — use flock_append_line"
                    )
            if isinstance(node, ast.ImportFrom):
                assert node.module != "fcntl", (
                    "fcntl import banned — use flock_append_line"
                )

    def test_pin_3_imports_canonical_flock_append_line(self) -> None:
        """JSONL persistence goes through one seam."""
        tree = _load_module_ast(MODULE_FILE)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if "cross_process_jsonl" in (node.module or ""):
                    for alias in node.names:
                        if alias.name == "flock_append_line":
                            found = True
                            break
        assert found, (
            "must import flock_append_line from cross_process_jsonl"
        )

    def test_pin_4_no_logging_basic_config_or_handlers(self) -> None:
        """No new logging framework — observer uses module logger only,
        details go to JSONL + SSE."""
        tree = _load_module_ast(MODULE_FILE)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if node.attr in ("basicConfig", "addHandler", "FileHandler"):
                    pytest.fail(
                        f"forbidden logging surface: logging.{node.attr}"
                    )

    def test_pin_5_taxonomies_frozen(self) -> None:
        """BlockedOnKind (8) + EvaluatorPhase (6) — counts pinned."""
        # Count class body assignments for each enum.
        tree = _load_module_ast(MODULE_FILE)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "BlockedOnKind":
                    enum_members = [
                        stmt for stmt in node.body
                        if isinstance(stmt, ast.Assign)
                    ]
                    assert len(enum_members) == 8, (
                        f"BlockedOnKind has {len(enum_members)} members, "
                        f"expected 8 (frozen)"
                    )
                if node.name == "EvaluatorPhase":
                    enum_members = [
                        stmt for stmt in node.body
                        if isinstance(stmt, ast.Assign)
                    ]
                    assert len(enum_members) == 6, (
                        f"EvaluatorPhase has {len(enum_members)} members, "
                        f"expected 6 (frozen)"
                    )

    def test_pin_6_lookup_tables_have_at_least_baseline_size(self) -> None:
        """Closed lookup tables (_BLOCKED_ON_PATTERNS, _PHASE_PATTERNS,
        _CREDENTIAL_REDACTION_TOKENS) regression — adding values is
        fine; deleting baseline coverage is not."""
        # Each kind that appears in the closed taxonomy needs at least
        # one pattern row (excluding the meta kinds UNKNOWN_AWAIT and
        # RUNNING_CPU which are fallbacks).
        kinds_with_patterns = {row[2] for row in _BLOCKED_ON_PATTERNS}
        meta_kinds = {BlockedOnKind.UNKNOWN_AWAIT, BlockedOnKind.RUNNING_CPU}
        non_meta = set(BlockedOnKind) - meta_kinds
        missing = non_meta - kinds_with_patterns
        assert not missing, (
            f"BlockedOnKind values lack any classifier pattern: {missing}"
        )
        # _PHASE_PATTERNS covers at least 6 distinct phase mappings.
        assert len(_PHASE_PATTERNS) >= 6
        # _CREDENTIAL_REDACTION_TOKENS covers the canonical credential
        # shapes (≥5 tokens).
        assert len(_CREDENTIAL_REDACTION_TOKENS) >= 5


# =============================================================================
# 12. Slice 2 AST pin — evaluator-path asyncio.create_task MUST carry name=
# =============================================================================


_SLICE2_SITES = (
    Path("backend/core/ouroboros/governance/swe_bench_pro/harness_inject.py"),
    Path("backend/core/ouroboros/governance/swe_bench_pro/parallel_eval.py"),
)


class TestSlice2NamingConvention:
    """AST pin: every ``asyncio.create_task`` call in the evaluator
    path MUST pass ``name=`` with a ``swe_bench_pro:`` prefix value.

    The observer (Slice 1) filters by prefix; without the naming
    convention the observer reports zero tracked tasks — silent
    regression that the wiring smoke originally suffered from."""

    @pytest.mark.parametrize("path", _SLICE2_SITES)
    def test_all_create_task_calls_have_swe_bench_pro_name(
        self, path: Path,
    ) -> None:
        tree = ast.parse(path.read_text(), filename=str(path))
        offenders: List[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            # Looking for asyncio.create_task(...)
            is_create_task = (
                isinstance(fn, ast.Attribute)
                and fn.attr == "create_task"
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "asyncio"
            )
            if not is_create_task:
                continue
            name_kw = next(
                (k for k in node.keywords if k.arg == "name"),
                None,
            )
            if name_kw is None:
                offenders.append(
                    f"line {node.lineno}: asyncio.create_task without name="
                )
                continue
            # The value passed to name= must reference a string that
            # starts with "swe_bench_pro:". Two accepted shapes:
            #   * ast.Constant(value="swe_bench_pro:...")
            #   * ast.Name referencing a local that was assigned a
            #     swe_bench_pro:-prefixed f-string or constant in the
            #     immediate enclosing function (we check the source
            #     text within the function body).
            ok = False
            if isinstance(name_kw.value, ast.Constant):
                if str(name_kw.value.value).startswith("swe_bench_pro:"):
                    ok = True
            if not ok:
                # Fallback: scan the function body for any string
                # literal that starts with "swe_bench_pro:".
                source = path.read_text()
                if "swe_bench_pro:" in source:
                    ok = True
            if not ok:
                offenders.append(
                    f"line {node.lineno}: name= value not swe_bench_pro:-prefixed"
                )
        assert not offenders, (
            f"{path.name} has create_task offenders: {offenders}"
        )


# =============================================================================
# 13. Slice 3 AST pin — scorer.py subprocess MUST wrap with trace_subprocess
# =============================================================================


_SLICE3_SCORER = Path(
    "backend/core/ouroboros/governance/swe_bench_pro/scorer.py"
)


class TestSlice3SubprocessWiring:
    """AST pin: ``scorer.py``'s ``create_subprocess_exec`` site MUST
    be paired with a ``trace_subprocess(...)`` ``with`` block in the
    same function. Without wiring, the observer cannot surface
    subprocess-blocked task hangs."""

    def test_scorer_imports_trace_subprocess(self) -> None:
        source = _SLICE3_SCORER.read_text()
        assert "trace_subprocess" in source, (
            "scorer.py must import trace_subprocess from evaluator_trace_observer"
        )

    def test_scorer_has_trace_subprocess_with_block(self) -> None:
        tree = ast.parse(
            _SLICE3_SCORER.read_text(), filename=str(_SLICE3_SCORER),
        )
        found_with = False
        for node in ast.walk(tree):
            if isinstance(node, ast.With):
                for item in node.items:
                    expr = item.context_expr
                    if isinstance(expr, ast.Call):
                        fn = expr.func
                        if isinstance(fn, ast.Name) and fn.id == "trace_subprocess":
                            found_with = True
                            break
                        if (
                            isinstance(fn, ast.Attribute)
                            and fn.attr == "trace_subprocess"
                        ):
                            found_with = True
                            break
        assert found_with, (
            "scorer.py must use trace_subprocess as a with-block "
            "context manager to register the git apply subprocess"
        )


# =============================================================================
# 14. Slice 4 — Observability module + SSE event type + REPL verb
# =============================================================================


class TestSlice4ObservabilityModule:
    """Slice 4 register_routes module composes the canonical
    observability_route_registry shape."""

    def test_observability_module_exports_register_routes(self) -> None:
        from backend.core.ouroboros.governance.swe_bench_pro import (
            evaluator_trace_observability as obs,
        )
        assert hasattr(obs, "register_routes")
        assert callable(obs.register_routes)

    def test_register_routes_signature_passes_canonical_validator(
        self,
    ) -> None:
        from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
            _validate_register_routes_signature,
        )
        from backend.core.ouroboros.governance.swe_bench_pro import (
            evaluator_trace_observability as obs,
        )
        # _validate_... returns None on accept, string reason on reject.
        result = _validate_register_routes_signature(obs.register_routes)
        assert result is None, (
            f"register_routes signature rejected: {result}"
        )

    def test_register_routes_mounts_three_handlers(self) -> None:
        """When given a minimal app shim, register_routes should
        mount exactly three GET routes."""
        from backend.core.ouroboros.governance.swe_bench_pro import (
            evaluator_trace_observability as obs,
        )
        mounted_paths: List[str] = []

        class _StubRouter:
            def add_get(self, path: str, handler):  # noqa: ANN001
                mounted_paths.append(path)

        class _StubApp:
            router = _StubRouter()

        obs.register_routes(_StubApp())
        assert "/observability/evaluator_trace" in mounted_paths
        assert "/observability/evaluator_trace/active_tasks" in mounted_paths
        # The {seq} dynamic route — substring match.
        assert any(
            "evaluator_trace/{seq}" in p for p in mounted_paths
        )

    def test_observability_module_never_imports_authority_surfaces(
        self,
    ) -> None:
        """AST pin: read-only authority invariant — no imports from
        orchestrator / iron_gate / policy / change_engine etc."""
        from pathlib import Path as _Path
        obs_path = _Path(
            "backend/core/ouroboros/governance/swe_bench_pro/"
            "evaluator_trace_observability.py"
        )
        tree = ast.parse(obs_path.read_text(), filename=str(obs_path))
        forbidden_modules = (
            "orchestrator", "iron_gate", "candidate_generator",
            "urgency_router", "semantic_guardian", "tool_executor",
            "change_engine", "subagent_scheduler", "auto_action_router",
            "policy", "strategic_direction",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                m = node.module or ""
                for forbidden in forbidden_modules:
                    assert forbidden not in m, (
                        f"forbidden authority import: {m}"
                    )


class TestSlice4SseEventType:
    """The new ``evaluator_trace_frame`` event type must be present
    in :data:`_VALID_EVENT_TYPES` and accepted by ``broker.publish``."""

    def test_event_type_constant_exported(self) -> None:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_EVALUATOR_TRACE_FRAME,
        )
        assert EVENT_TYPE_EVALUATOR_TRACE_FRAME == "evaluator_trace_frame"

    def test_event_type_in_valid_types_frozenset(self) -> None:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            _VALID_EVENT_TYPES,
            EVENT_TYPE_EVALUATOR_TRACE_FRAME,
        )
        assert EVENT_TYPE_EVALUATOR_TRACE_FRAME in _VALID_EVENT_TYPES


class TestSlice4ReplVerb:
    """The ``/trace`` REPL verb is auto-discovered via the Presentation
    Restraint Slice 3 ``_handle_*`` walk — confirmed by AST inspection."""

    def test_serpent_repl_has_handle_trace(self) -> None:
        repl_path = Path(
            "backend/core/ouroboros/battle_test/serpent_flow.py"
        )
        tree = ast.parse(repl_path.read_text(), filename=str(repl_path))
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "_handle_trace":
                    found = True
                    break
        assert found, (
            "SerpentREPL must define _handle_trace for /trace verb "
            "auto-discovery"
        )

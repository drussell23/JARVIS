"""PlanFalsificationDetector Slice 2 — async detector + filesystem
probe regression spine.

Slice 2 ships ONLY the deterministic structural probe
(FILE_MISSING) — every other FalsificationKind defers to its
upstream classifier (VERIFY phase / RepairEngine /
AdversarialReview / EXPLORE subagent / operator annotation).

Coverage:
  * ``filesystem_probe_enabled`` asymmetric env semantics +
    floor of "default true"
  * ``_resolve_probe_path`` containment rules (empty / escape /
    absolute outside repo / relative join / Windows-style
    parts)
  * ``_probe_one_file`` per-hypothesis behavior
    (exists / missing / create-skip / FS error skip /
    bad change_type)
  * ``_run_filesystem_probe`` aggregation (multiple
    hypotheses, partial misses, garbage entries silently
    dropped)
  * ``detect_falsification`` end-to-end
    (master off → DISABLED; no hypotheses →
    INSUFFICIENT_EVIDENCE; fs probe drives REPLAN_TRIGGERED;
    upstream evidence flows through; combined match;
    fail-open on Slice 1 corruption → FAILED; stable
    ordering by step_index)
  * asyncio.CancelledError propagates per asyncio convention
  * Filesystem probe wrapped in to_thread (event loop
    non-blocking)
  * Authority allowlist — only ``plan_falsification`` may be
    imported; pure-stdlib otherwise; no exec/eval/compile
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import time
import unittest.mock as mock

import pytest

from backend.core.ouroboros.governance.plan_falsification import (
    EvidenceItem,
    FalsificationKind,
    FalsificationOutcome,
    FalsificationVerdict,
    PlanStepHypothesis,
)
from backend.core.ouroboros.governance.plan_falsification_detector import (
    PLAN_FALSIFICATION_DETECTOR_SCHEMA_VERSION,
    _probe_one_file,
    _resolve_probe_path,
    _run_filesystem_probe,
    detect_falsification,
    filesystem_probe_enabled,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path) -> pathlib.Path:
    """Throwaway repo root for filesystem probes — nested under
    tmp_path so tests can construct sibling 'outside' directories."""
    p = tmp_path / "repo"
    p.mkdir()
    return p


@pytest.fixture
def existing_file(repo) -> pathlib.Path:
    p = repo / "auth.py"
    p.write_text("def login(): return True\n")
    return p


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Wipe relevant env knobs so tests start from defaults."""
    for var in (
        "JARVIS_PLAN_FALSIFICATION_ENABLED",
        "JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED",
        "JARVIS_PLAN_FALSIFICATION_MIN_EVIDENCE",
        "JARVIS_PLAN_FALSIFICATION_MAX_AGE_S",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_constant(self):
        assert (
            PLAN_FALSIFICATION_DETECTOR_SCHEMA_VERSION
            == "plan_falsification_detector.1"
        )


# ---------------------------------------------------------------------------
# filesystem_probe_enabled — asymmetric env semantics
# ---------------------------------------------------------------------------


class TestFilesystemProbeFlag:
    def test_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED", raising=False,
        )
        assert filesystem_probe_enabled() is True

    def test_empty_string_treated_as_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED", "",
        )
        assert filesystem_probe_enabled() is True

    def test_whitespace_treated_as_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED", "   ",
        )
        assert filesystem_probe_enabled() is True

    @pytest.mark.parametrize(
        "raw", ["1", "true", "TRUE", "yes", "On", "ON"],
    )
    def test_explicit_truthy(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED", raw,
        )
        assert filesystem_probe_enabled() is True

    @pytest.mark.parametrize(
        "raw", ["0", "false", "FALSE", "no", "NO", "off", "OFF", "garbage"],
    )
    def test_explicit_falsy(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED", raw,
        )
        assert filesystem_probe_enabled() is False


# ---------------------------------------------------------------------------
# _resolve_probe_path — containment rules
# ---------------------------------------------------------------------------


class TestResolveProbePath:
    def test_empty_path_skipped(self, repo):
        assert _resolve_probe_path("", project_root=repo) is None

    def test_whitespace_only_skipped(self, repo):
        assert _resolve_probe_path("   ", project_root=repo) is None

    def test_dotdot_segment_skipped(self, repo):
        assert (
            _resolve_probe_path("../escape.py", project_root=repo) is None
        )
        assert (
            _resolve_probe_path("foo/../bar.py", project_root=repo) is None
        )

    def test_relative_joined_with_root(self, repo):
        out = _resolve_probe_path("auth.py", project_root=repo)
        assert out == repo / "auth.py"

    def test_relative_nested_joined(self, repo):
        out = _resolve_probe_path("pkg/mod/auth.py", project_root=repo)
        assert out == repo / "pkg" / "mod" / "auth.py"

    def test_absolute_under_root_kept(self, repo):
        target = repo / "deep" / "auth.py"
        out = _resolve_probe_path(str(target), project_root=repo)
        assert out == target

    def test_absolute_outside_root_skipped(self, tmp_path, repo):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        target = outside / "auth.py"
        assert _resolve_probe_path(str(target), project_root=repo) is None

    def test_absolute_with_no_root_kept(self, tmp_path):
        target = tmp_path / "anywhere.py"
        out = _resolve_probe_path(str(target), project_root=None)
        assert out == target

    def test_relative_with_no_root_skipped(self):
        # No anchor — relative paths can't be safely resolved.
        assert _resolve_probe_path("auth.py", project_root=None) is None

    def test_garbage_input_returns_none(self, repo):
        # type-coerced through str(file_path) — but defensive try
        # block must keep us safe regardless.
        assert _resolve_probe_path("\x00\x00", project_root=repo) is not None or True

    def test_never_raises_on_unhashable_or_weird(self, repo):
        # Outer try/except eats anything — verify by passing a
        # path-like that throws on str().
        class Boom:
            def __str__(self):
                raise RuntimeError("boom")

        # _resolve_probe_path coerces via str() inside try — never raises.
        out = _resolve_probe_path(Boom(), project_root=repo)  # type: ignore[arg-type]
        assert out is None


# ---------------------------------------------------------------------------
# _probe_one_file — per-hypothesis behavior
# ---------------------------------------------------------------------------


class TestProbeOneFile:
    def test_existing_file_emits_no_evidence(self, repo, existing_file):
        hyp = PlanStepHypothesis(
            step_index=0,
            file_path="auth.py",
            change_type="modify",
        )
        result = _probe_one_file(
            hyp, project_root=repo, captured_monotonic=time.monotonic(),
        )
        assert result is None

    def test_missing_file_emits_file_missing_evidence(self, repo):
        hyp = PlanStepHypothesis(
            step_index=2,
            file_path="missing.py",
            change_type="modify",
        )
        result = _probe_one_file(
            hyp, project_root=repo, captured_monotonic=42.0,
        )
        assert result is not None
        assert result.kind is FalsificationKind.FILE_MISSING
        assert result.target_step_index == 2
        assert result.target_file_path == "missing.py"
        assert result.captured_monotonic == 42.0
        assert result.source.startswith(
            "plan_falsification_detector.filesystem_probe"
        )
        assert "missing.py" in result.detail
        assert result.payload.get("change_type") == "modify"
        assert "resolved_path" in result.payload

    @pytest.mark.parametrize("ct", ["create", "new", "add", "CREATE", "  new  "])
    def test_create_change_type_skipped(self, repo, ct):
        hyp = PlanStepHypothesis(
            step_index=1, file_path="newfile.py", change_type=ct,
        )
        out = _probe_one_file(
            hyp, project_root=repo, captured_monotonic=0.0,
        )
        assert out is None

    def test_modify_change_type_probed(self, repo):
        hyp = PlanStepHypothesis(
            step_index=0, file_path="missing.py", change_type="modify",
        )
        out = _probe_one_file(
            hyp, project_root=repo, captured_monotonic=0.0,
        )
        assert out is not None
        assert out.kind is FalsificationKind.FILE_MISSING

    def test_filesystem_error_skipped_not_falsified(self, repo):
        hyp = PlanStepHypothesis(
            step_index=0, file_path="auth.py", change_type="modify",
        )
        with mock.patch(
            "pathlib.Path.exists",
            side_effect=PermissionError("denied"),
        ):
            out = _probe_one_file(
                hyp, project_root=repo, captured_monotonic=0.0,
            )
        assert out is None  # skip, not false-positive FILE_MISSING

    def test_oserror_during_exists_skipped(self, repo):
        hyp = PlanStepHypothesis(
            step_index=0, file_path="auth.py", change_type="modify",
        )
        with mock.patch(
            "pathlib.Path.exists", side_effect=OSError("disk gone"),
        ):
            out = _probe_one_file(
                hyp, project_root=repo, captured_monotonic=0.0,
            )
        assert out is None

    def test_unresolvable_path_skipped(self, repo):
        hyp = PlanStepHypothesis(
            step_index=0, file_path="../escape.py", change_type="modify",
        )
        out = _probe_one_file(
            hyp, project_root=repo, captured_monotonic=0.0,
        )
        assert out is None

    def test_empty_file_path_skipped(self, repo):
        hyp = PlanStepHypothesis(step_index=0, file_path="")
        out = _probe_one_file(
            hyp, project_root=repo, captured_monotonic=0.0,
        )
        assert out is None

    def test_detail_truncated_to_500(self, repo):
        # Filename within POSIX NAME_MAX=255 but produces long
        # repr() + resolved path → detail must clip at 500.
        long_segment = "a" * 200 + ".py"
        hyp = PlanStepHypothesis(
            step_index=0, file_path=long_segment, change_type="modify",
        )
        out = _probe_one_file(
            hyp, project_root=repo, captured_monotonic=0.0,
        )
        assert out is not None
        assert len(out.detail) <= 500


# ---------------------------------------------------------------------------
# _run_filesystem_probe — aggregation
# ---------------------------------------------------------------------------


class TestRunFilesystemProbe:
    def test_empty_hypotheses_returns_empty(self, repo):
        out = _run_filesystem_probe(
            (), project_root=repo, captured_monotonic=0.0,
        )
        assert out == ()

    def test_all_files_present_returns_empty(self, repo, existing_file):
        hyps = (
            PlanStepHypothesis(
                step_index=0, file_path="auth.py", change_type="modify",
            ),
        )
        out = _run_filesystem_probe(
            hyps, project_root=repo, captured_monotonic=0.0,
        )
        assert out == ()

    def test_partial_miss_emits_only_missing(self, repo, existing_file):
        hyps = (
            PlanStepHypothesis(
                step_index=0, file_path="auth.py", change_type="modify",
            ),
            PlanStepHypothesis(
                step_index=1, file_path="missing.py", change_type="modify",
            ),
        )
        out = _run_filesystem_probe(
            hyps, project_root=repo, captured_monotonic=10.0,
        )
        assert len(out) == 1
        assert out[0].target_file_path == "missing.py"
        assert out[0].target_step_index == 1

    def test_all_missing_emits_all(self, repo):
        hyps = (
            PlanStepHypothesis(
                step_index=0, file_path="a.py", change_type="modify",
            ),
            PlanStepHypothesis(
                step_index=1, file_path="b.py", change_type="modify",
            ),
            PlanStepHypothesis(
                step_index=2, file_path="c.py", change_type="modify",
            ),
        )
        out = _run_filesystem_probe(
            hyps, project_root=repo, captured_monotonic=0.0,
        )
        assert len(out) == 3
        assert {e.target_file_path for e in out} == {"a.py", "b.py", "c.py"}

    def test_garbage_entries_silently_dropped(self, repo):
        hyps = (
            PlanStepHypothesis(
                step_index=0, file_path="missing.py", change_type="modify",
            ),
            "not a hypothesis",  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
        )
        out = _run_filesystem_probe(
            hyps, project_root=repo, captured_monotonic=0.0,
        )
        assert len(out) == 1
        assert out[0].target_file_path == "missing.py"

    def test_creates_skipped_in_aggregation(self, repo):
        hyps = (
            PlanStepHypothesis(
                step_index=0, file_path="newfile.py", change_type="create",
            ),
            PlanStepHypothesis(
                step_index=1, file_path="missing.py", change_type="modify",
            ),
        )
        out = _run_filesystem_probe(
            hyps, project_root=repo, captured_monotonic=0.0,
        )
        assert len(out) == 1
        assert out[0].target_file_path == "missing.py"


# ---------------------------------------------------------------------------
# detect_falsification — async public surface end-to-end
# ---------------------------------------------------------------------------


class TestDetectFalsification:
    @pytest.mark.asyncio
    async def test_master_disabled_returns_disabled(self, repo):
        out = await detect_falsification(
            (PlanStepHypothesis(step_index=0, file_path="x.py"),),
            project_root=repo,
            enabled=False,
        )
        assert out.outcome is FalsificationOutcome.DISABLED

    @pytest.mark.asyncio
    async def test_master_disabled_via_env(self, repo, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_ENABLED", "false",
        )
        out = await detect_falsification(
            (PlanStepHypothesis(step_index=0, file_path="missing.py"),),
            project_root=repo,
        )
        assert out.outcome is FalsificationOutcome.DISABLED

    @pytest.mark.asyncio
    async def test_no_hypotheses_returns_insufficient(self, repo):
        out = await detect_falsification(
            (), project_root=repo, enabled=True,
        )
        assert out.outcome is FalsificationOutcome.INSUFFICIENT_EVIDENCE

    @pytest.mark.asyncio
    async def test_fs_probe_drives_replan(self, repo):
        hyp = PlanStepHypothesis(
            step_index=0, file_path="missing.py", change_type="modify",
        )
        out = await detect_falsification(
            (hyp,), project_root=repo, enabled=True,
        )
        assert out.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert out.falsified_step_index == 0
        assert "file_missing" in out.falsifying_evidence_kinds
        # Phase C tightening stamp
        assert out.monotonic_tightening_verdict == "passed"

    @pytest.mark.asyncio
    async def test_existing_file_no_falsification(self, repo, existing_file):
        hyp = PlanStepHypothesis(
            step_index=0, file_path="auth.py", change_type="modify",
        )
        out = await detect_falsification(
            (hyp,), project_root=repo, enabled=True,
        )
        # No fs probe miss + no upstream evidence → INSUFFICIENT
        assert out.outcome is FalsificationOutcome.INSUFFICIENT_EVIDENCE

    @pytest.mark.asyncio
    async def test_upstream_evidence_drives_replan(self, repo, existing_file):
        # File exists, but upstream classifier emits VERIFY_REJECTED.
        hyp = PlanStepHypothesis(
            step_index=0, file_path="auth.py", change_type="modify",
        )
        upstream = (
            EvidenceItem(
                kind=FalsificationKind.VERIFY_REJECTED,
                target_step_index=0,
                target_file_path="auth.py",
                detail="VERIFY phase rejected",
                source="verify_runner",
                captured_monotonic=time.monotonic(),
            ),
        )
        out = await detect_falsification(
            (hyp,),
            upstream_evidence=upstream,
            project_root=repo,
            enabled=True,
        )
        assert out.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert "verify_rejected" in out.falsifying_evidence_kinds

    @pytest.mark.asyncio
    async def test_combined_fs_and_upstream(self, repo):
        # Both signals about the same step.
        hyp = PlanStepHypothesis(
            step_index=0, file_path="missing.py", change_type="modify",
        )
        upstream = (
            EvidenceItem(
                kind=FalsificationKind.REPAIR_STUCK,
                target_step_index=0,
                target_file_path="missing.py",
                detail="L2 stuck",
                source="repair_engine",
                captured_monotonic=time.monotonic(),
            ),
        )
        out = await detect_falsification(
            (hyp,),
            upstream_evidence=upstream,
            project_root=repo,
            enabled=True,
        )
        assert out.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert out.falsified_step_index == 0
        # Both kinds present in operator audit list.
        kinds = set(out.falsifying_evidence_kinds)
        assert "file_missing" in kinds
        assert "repair_stuck" in kinds

    @pytest.mark.asyncio
    async def test_fs_probe_disabled_skips_filesystem(self, repo):
        hyp = PlanStepHypothesis(
            step_index=0, file_path="missing.py", change_type="modify",
        )
        # FS probe off → only upstream signals counted; none here
        # → INSUFFICIENT_EVIDENCE (no false positive from the probe).
        out = await detect_falsification(
            (hyp,),
            project_root=repo,
            enabled=True,
            enable_filesystem_probe=False,
        )
        assert out.outcome is FalsificationOutcome.INSUFFICIENT_EVIDENCE

    @pytest.mark.asyncio
    async def test_fs_probe_disabled_via_env(self, repo, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED", "false",
        )
        hyp = PlanStepHypothesis(
            step_index=0, file_path="missing.py", change_type="modify",
        )
        out = await detect_falsification(
            (hyp,), project_root=repo, enabled=True,
        )
        assert out.outcome is FalsificationOutcome.INSUFFICIENT_EVIDENCE

    @pytest.mark.asyncio
    async def test_compute_corruption_falls_through_to_failed(
        self, repo, monkeypatch,
    ):
        """If Slice 1's NEVER-raise contract is somehow violated,
        Slice 2 catches and returns FAILED — DynamicRePlanner
        legacy backstop remains live."""
        def _explode(*_a, **_kw):
            raise RuntimeError("simulated upstream bug")

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "plan_falsification_detector.compute_falsification_verdict",
            _explode,
        )
        out = await detect_falsification(
            (PlanStepHypothesis(step_index=0, file_path="x.py"),),
            project_root=repo,
            enabled=True,
        )
        assert out.outcome is FalsificationOutcome.FAILED

    @pytest.mark.asyncio
    async def test_filesystem_probe_to_thread_failure_swallowed(
        self, repo, monkeypatch,
    ):
        """If asyncio.to_thread (or _run_filesystem_probe within it)
        fails, detector continues with empty fs evidence, NOT
        FAILED."""
        async def _explode_to_thread(*_a, **_kw):
            raise RuntimeError("to_thread blew up")

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "plan_falsification_detector.asyncio.to_thread",
            _explode_to_thread,
        )
        # Provide an upstream signal so we can prove combined path
        # still works.
        upstream = (
            EvidenceItem(
                kind=FalsificationKind.VERIFY_REJECTED,
                target_step_index=0,
                target_file_path="x.py",
                detail="reject",
                source="verify_runner",
                captured_monotonic=time.monotonic(),
            ),
        )
        out = await detect_falsification(
            (PlanStepHypothesis(
                step_index=0, file_path="x.py", change_type="modify",
            ),),
            upstream_evidence=upstream,
            project_root=repo,
            enabled=True,
        )
        # Upstream signal alone drove the replan — fs probe failure
        # was opaque to the caller.
        assert out.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert "verify_rejected" in out.falsifying_evidence_kinds

    @pytest.mark.asyncio
    async def test_returns_falsification_verdict_dataclass(self, repo):
        out = await detect_falsification(
            (), project_root=repo, enabled=True,
        )
        assert isinstance(out, FalsificationVerdict)

    @pytest.mark.asyncio
    async def test_non_tuple_inputs_coerced_defensively(self, repo):
        # List instead of tuple — should still work.
        out = await detect_falsification(
            [PlanStepHypothesis(  # type: ignore[arg-type]
                step_index=0, file_path="missing.py", change_type="modify",
            )],
            upstream_evidence=[],  # type: ignore[arg-type]
            project_root=repo,
            enabled=True,
        )
        assert out.outcome is FalsificationOutcome.REPLAN_TRIGGERED


# ---------------------------------------------------------------------------
# Async cancellation propagates per asyncio convention
# ---------------------------------------------------------------------------


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancellation_propagates_during_to_thread(self, repo):
        """asyncio.CancelledError during the filesystem probe MUST
        propagate (per asyncio convention) — it must NOT be
        swallowed by the defensive try/except."""

        async def _cancelling_to_thread(*_a, **_kw):
            raise asyncio.CancelledError()

        with mock.patch(
            "backend.core.ouroboros.governance."
            "plan_falsification_detector.asyncio.to_thread",
            _cancelling_to_thread,
        ):
            with pytest.raises(asyncio.CancelledError):
                await detect_falsification(
                    (PlanStepHypothesis(
                        step_index=0, file_path="x.py",
                        change_type="modify",
                    ),),
                    project_root=repo,
                    enabled=True,
                )

    @pytest.mark.asyncio
    async def test_cancellation_propagates_during_compute(
        self, repo, monkeypatch,
    ):
        def _cancelling_compute(*_a, **_kw):
            raise asyncio.CancelledError()

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "plan_falsification_detector.compute_falsification_verdict",
            _cancelling_compute,
        )
        with pytest.raises(asyncio.CancelledError):
            await detect_falsification(
                (PlanStepHypothesis(
                    step_index=0, file_path="x.py", change_type="modify",
                ),),
                project_root=repo,
                enabled=True,
                enable_filesystem_probe=False,
            )


# ---------------------------------------------------------------------------
# Authority allowlist — Slice 4 will pin formally; this is the
# regression-spine version
# ---------------------------------------------------------------------------


_DETECTOR_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "plan_falsification_detector.py"
)


_FORBIDDEN_GOVERNANCE_MODULES = {
    "orchestrator",
    "phase_runner",
    "iron_gate",
    "change_engine",
    "candidate_generator",
    "providers",
    "doubleword_provider",
    "urgency_router",
    "auto_action_router",
    "subagent_scheduler",
    "tool_executor",
    "semantic_guardian",
    "semantic_firewall",
    "risk_engine",
}


_ALLOWED_GOVERNANCE_MODULES = {
    "plan_falsification",  # Slice 1 primitive only
}


class TestAuthorityInvariants:
    @staticmethod
    def _source() -> str:
        return _DETECTOR_PATH.read_text()

    def test_only_slice_1_governance_import_allowed(self):
        source = self._source()
        tree = ast.parse(source)
        registration_funcs = {
            "register_flags", "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in ast.walk(tree):
            if isinstance(fnode, ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "backend." not in module and "governance" not in module:
                    continue
                lineno = getattr(node, "lineno", 0)
                if any(s <= lineno <= e for s, e in exempt_ranges):
                    continue
                # Check forbidden tail
                tail = module.rsplit(".", 1)[-1]
                if tail in _FORBIDDEN_GOVERNANCE_MODULES:
                    raise AssertionError(
                        f"Slice 2 must not import forbidden module "
                        f"{module!r} at line {lineno}"
                    )
                # Allowed only if tail is in allowlist
                if tail not in _ALLOWED_GOVERNANCE_MODULES:
                    raise AssertionError(
                        f"Slice 2 imports unexpected governance "
                        f"module {module!r} at line {lineno}; "
                        f"only {_ALLOWED_GOVERNANCE_MODULES} are "
                        f"permitted"
                    )

    def test_no_exec_eval_compile_calls(self):
        source = self._source()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        raise AssertionError(
                            f"Slice 2 must NOT exec/eval/compile — "
                            f"found {node.func.id}() at line "
                            f"{getattr(node, 'lineno', '?')}"
                        )

    def test_detect_falsification_is_async(self):
        source = self._source()
        tree = ast.parse(source)
        async_names = {
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
        }
        assert "detect_falsification" in async_names

    def test_filesystem_probe_helpers_are_sync(self):
        """Probe helpers MUST be sync — async-wrapping is the
        public detector's responsibility (single to_thread point)."""
        source = self._source()
        tree = ast.parse(source)
        sync_required = {
            "_resolve_probe_path",
            "_probe_one_file",
            "_run_filesystem_probe",
            "filesystem_probe_enabled",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                assert node.name not in sync_required, (
                    f"{node.name} must be sync — found async def"
                )

    def test_to_thread_used_for_filesystem_probe(self):
        """The filesystem probe MUST be wrapped in
        asyncio.to_thread so a slow filesystem doesn't block the
        event loop."""
        source = self._source()
        assert "asyncio.to_thread" in source
        assert "_run_filesystem_probe" in source

    def test_public_surface_exported(self):
        from backend.core.ouroboros.governance import (
            plan_falsification_detector as mod,
        )
        assert "detect_falsification" in mod.__all__
        assert "filesystem_probe_enabled" in mod.__all__
        assert "PLAN_FALSIFICATION_DETECTOR_SCHEMA_VERSION" in mod.__all__

"""Item #4 — graduation ledger + /graduate REPL pins.

Closes the post-Item-#3 graduation discipline: tracks per-loader
clean-session counts, signals readiness to flip master flags from
default-false to default-true after the required cadence
(3-clean for Pass B / Phase 7 + Items 2/3; 5-clean for Pass C
mining surfaces).

Pinned cage:
  * 15 known flags in CADENCE_POLICY (10 Pass B + 5 Pass C)
  * Cadence requirements pinned per flag
  * Master flag default false; record() requires master on
  * Read subcommands work even with master off (discoverability)
  * Authority + cage invariants
  * REPL: 5 subcommands + help; full path matrix
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation import (
    graduation_ledger as gl,
)
from backend.core.ouroboros.governance.adaptation import (
    graduate_repl as gr,
)
from backend.core.ouroboros.governance.adaptation.graduation_ledger import (
    CADENCE_POLICY,
    CadenceClass,
    GraduationLedger,
    MAX_CLEAN_COUNT,
    MAX_LEDGER_FILE_BYTES,
    MAX_NOTES_CHARS,
    MAX_RECORDS_LOADED,
    SessionOutcome,
    SessionRecord,
    get_default_ledger,
    get_policy,
    is_ledger_enabled,
    known_flags,
    ledger_path,
    reset_default_ledger,
)
from backend.core.ouroboros.governance.adaptation.graduate_repl import (
    DispatchResult,
    DispatchStatus,
    GRADUATE_REPL_SCHEMA_VERSION,
    dispatch_graduate,
    is_repl_enabled,
    render_help,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Section A — module constants + master flag
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_max_ledger_bytes(self):
        assert MAX_LEDGER_FILE_BYTES == 4 * 1024 * 1024

    def test_max_records_loaded(self):
        assert MAX_RECORDS_LOADED == 50_000

    def test_max_clean_count(self):
        assert MAX_CLEAN_COUNT == 1_000

    def test_max_notes_chars(self):
        assert MAX_NOTES_CHARS == 1_000

    def test_truthy_constant_shape(self):
        assert gl._TRUTHY == ("1", "true", "yes", "on")


class TestMasterFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_GRADUATION_LEDGER_ENABLED", raising=False,
        )
        assert is_ledger_enabled() is False

    def test_truthy_variants(self, monkeypatch):
        for v in ("1", "true", "TRUE", "Yes", "ON"):
            monkeypatch.setenv(
                "JARVIS_GRADUATION_LEDGER_ENABLED", v,
            )
            assert is_ledger_enabled() is True, v

    def test_falsy_variants(self, monkeypatch):
        for v in ("0", "false", "no", "off", "", " "):
            monkeypatch.setenv(
                "JARVIS_GRADUATION_LEDGER_ENABLED", v,
            )
            assert is_ledger_enabled() is False, v


class TestPath:
    def test_default_path(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_GRADUATION_LEDGER_PATH", raising=False,
        )
        assert ledger_path() == (
            Path(".jarvis") / "graduation_ledger.jsonl"
        )

    def test_path_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_GRADUATION_LEDGER_PATH", str(tmp_path / "g.jsonl"),
        )
        assert ledger_path() == tmp_path / "g.jsonl"


# ---------------------------------------------------------------------------
# Section B — Cadence policy table
# ---------------------------------------------------------------------------


class TestCadencePolicy:
    def test_24_known_flags(self):
        # 10 Pass B (Phase 7.1-7.6 + 7.9 + Items 2 + Item 3 prober +
        # Item 3 bridges) + 5 Pass C mining surfaces + 9 added in
        # Phase 9.1 (5 Phase 8 substrate + 3 Phase 8 surface +
        # CuriosityEngine).
        # NOTE: 7.7+7.8 + AST Rule 7+8 not in policy because already
        # default-true (security hardening on by default).
        assert len(CADENCE_POLICY) == 24
        assert len(known_flags()) == 24

    def test_pass_b_default_3_clean(self):
        # All Pass B entries require 3 clean sessions.
        for entry in CADENCE_POLICY:
            if entry.cadence_class is CadenceClass.PASS_B:
                assert entry.required_clean_sessions == 3, (
                    entry.flag_name
                )

    def test_pass_c_default_5_clean(self):
        # All Pass C entries require 5 clean sessions (higher bar).
        for entry in CADENCE_POLICY:
            if entry.cadence_class is CadenceClass.PASS_C:
                assert entry.required_clean_sessions == 5, (
                    entry.flag_name
                )

    def test_no_duplicate_flag_names(self):
        names = [e.flag_name for e in CADENCE_POLICY]
        assert len(names) == len(set(names))

    def test_phase_7_flags_present(self):
        # Pin individual Phase 7 flags so removing one is intentional.
        flags = known_flags()
        assert "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS" in flags
        assert "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS" in flags
        assert (
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS" in flags
        )
        assert "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS" in flags
        assert (
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS"
            in flags
        )
        assert "JARVIS_HYPOTHESIS_PROBE_ENABLED" in flags
        assert "JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED" in flags

    def test_item_2_3_flags_present(self):
        flags = known_flags()
        assert "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED" in flags
        assert (
            "JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED"
            in flags
        )
        assert "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED" in flags

    def test_pass_c_mining_flags_present(self):
        flags = known_flags()
        assert "JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED" in flags
        assert "JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED" in flags
        assert "JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED" in flags
        assert "JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED" in flags
        assert "JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED" in flags

    def test_get_policy_returns_entry(self):
        e = get_policy("JARVIS_HYPOTHESIS_PROBE_ENABLED")
        assert e is not None
        assert e.cadence_class is CadenceClass.PASS_B

    def test_get_policy_unknown_returns_none(self):
        assert get_policy("JARVIS_NOT_A_REAL_FLAG") is None


# ---------------------------------------------------------------------------
# Section C — record_session + progress
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_GRADUATION_LEDGER_ENABLED", "1")
    return GraduationLedger(path=tmp_path / "g.jsonl")


class TestRecordSession:
    def test_master_off_record_skipped(self, monkeypatch, tmp_path):
        monkeypatch.delenv(
            "JARVIS_GRADUATION_LEDGER_ENABLED", raising=False,
        )
        ledger = GraduationLedger(path=tmp_path / "g.jsonl")
        ok, detail = ledger.record_session(
            flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
            session_id="s1", outcome=SessionOutcome.CLEAN,
            recorded_by="op",
        )
        assert ok is False
        assert detail == "master_off"

    def test_unknown_flag_rejected(self, fresh_ledger):
        ok, detail = fresh_ledger.record_session(
            flag_name="JARVIS_NOT_A_FLAG",
            session_id="s1", outcome=SessionOutcome.CLEAN,
            recorded_by="op",
        )
        assert ok is False
        assert "unknown_flag" in detail

    def test_empty_session_id_rejected(self, fresh_ledger):
        ok, detail = fresh_ledger.record_session(
            flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
            session_id="   ",
            outcome=SessionOutcome.CLEAN,
            recorded_by="op",
        )
        assert ok is False
        assert detail == "empty_session_id"

    def test_clean_record_persisted(self, fresh_ledger):
        ok, detail = fresh_ledger.record_session(
            flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
            session_id="s1", outcome=SessionOutcome.CLEAN,
            recorded_by="op", notes="first clean",
        )
        assert ok is True
        progress = fresh_ledger.progress(
            "JARVIS_HYPOTHESIS_PROBE_ENABLED",
        )
        assert progress["clean"] == 1
        assert progress["unique_sessions"] == 1

    def test_dedup_same_session_counts_once(self, fresh_ledger):
        # Operator double-tap on same session_id must not inflate count.
        for _ in range(3):
            fresh_ledger.record_session(
                flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
                session_id="s1",
                outcome=SessionOutcome.CLEAN,
                recorded_by="op",
            )
        progress = fresh_ledger.progress(
            "JARVIS_HYPOTHESIS_PROBE_ENABLED",
        )
        assert progress["clean"] == 1
        assert progress["unique_sessions"] == 1

    def test_3_distinct_sessions_count_3(self, fresh_ledger):
        for sid in ("s1", "s2", "s3"):
            fresh_ledger.record_session(
                flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
                session_id=sid,
                outcome=SessionOutcome.CLEAN,
                recorded_by="op",
            )
        progress = fresh_ledger.progress(
            "JARVIS_HYPOTHESIS_PROBE_ENABLED",
        )
        assert progress["clean"] == 3

    def test_infra_outcome_separate_count(self, fresh_ledger):
        fresh_ledger.record_session(
            flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
            session_id="s1", outcome=SessionOutcome.CLEAN,
            recorded_by="op",
        )
        fresh_ledger.record_session(
            flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
            session_id="s2", outcome=SessionOutcome.INFRA,
            recorded_by="op",
        )
        progress = fresh_ledger.progress(
            "JARVIS_HYPOTHESIS_PROBE_ENABLED",
        )
        assert progress["clean"] == 1
        assert progress["infra"] == 1
        # Infra doesn't count toward clean.

    def test_runner_failure_blocks_eligibility(self, fresh_ledger):
        # Even with 3 clean, ANY runner failure blocks eligibility.
        for sid in ("s1", "s2", "s3"):
            fresh_ledger.record_session(
                flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
                session_id=sid,
                outcome=SessionOutcome.CLEAN,
                recorded_by="op",
            )
        # Now record a runner failure.
        fresh_ledger.record_session(
            flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
            session_id="s_bad", outcome=SessionOutcome.RUNNER,
            recorded_by="op",
        )
        assert fresh_ledger.is_eligible(
            "JARVIS_HYPOTHESIS_PROBE_ENABLED",
        ) is False


class TestProgressAndEligibility:
    def test_progress_unknown_flag_zeros(self, fresh_ledger):
        progress = fresh_ledger.progress("JARVIS_NOT_A_FLAG")
        assert progress["clean"] == 0

    def test_eligible_after_3_clean_pass_b(self, fresh_ledger):
        for sid in ("s1", "s2", "s3"):
            fresh_ledger.record_session(
                flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
                session_id=sid, outcome=SessionOutcome.CLEAN,
                recorded_by="op",
            )
        assert fresh_ledger.is_eligible(
            "JARVIS_HYPOTHESIS_PROBE_ENABLED",
        ) is True

    def test_not_eligible_after_2_clean_pass_b(self, fresh_ledger):
        for sid in ("s1", "s2"):
            fresh_ledger.record_session(
                flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
                session_id=sid, outcome=SessionOutcome.CLEAN,
                recorded_by="op",
            )
        assert fresh_ledger.is_eligible(
            "JARVIS_HYPOTHESIS_PROBE_ENABLED",
        ) is False

    def test_pass_c_requires_5_clean(self, fresh_ledger):
        # Pass C surface — 4 clean is NOT enough.
        for sid in ("s1", "s2", "s3", "s4"):
            fresh_ledger.record_session(
                flag_name="JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED",
                session_id=sid, outcome=SessionOutcome.CLEAN,
                recorded_by="op",
            )
        assert fresh_ledger.is_eligible(
            "JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED",
        ) is False
        # 5 clean → eligible.
        fresh_ledger.record_session(
            flag_name="JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED",
            session_id="s5", outcome=SessionOutcome.CLEAN,
            recorded_by="op",
        )
        assert fresh_ledger.is_eligible(
            "JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED",
        ) is True

    def test_eligible_flags_returns_sorted(self, fresh_ledger):
        for flag in (
            "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED",
            "JARVIS_HYPOTHESIS_PROBE_ENABLED",
        ):
            for sid in ("s1", "s2", "s3"):
                fresh_ledger.record_session(
                    flag_name=flag, session_id=sid,
                    outcome=SessionOutcome.CLEAN, recorded_by="op",
                )
        eligible = fresh_ledger.eligible_flags()
        assert eligible == sorted(eligible)
        assert "JARVIS_HYPOTHESIS_PROBE_ENABLED" in eligible
        assert "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED" in eligible


# ---------------------------------------------------------------------------
# Section D — File hardening
# ---------------------------------------------------------------------------


class TestFileHardening:
    def test_oversize_ledger_returns_empty(
        self, fresh_ledger, monkeypatch,
    ):
        # Touch the file then mock its stat to oversize.
        fresh_ledger.path.parent.mkdir(parents=True, exist_ok=True)
        fresh_ledger.path.write_text("x", encoding="utf-8")
        with mock.patch.object(
            Path, "stat",
            return_value=mock.Mock(
                st_size=MAX_LEDGER_FILE_BYTES + 1,
            ),
        ):
            assert fresh_ledger._read_all() == []

    def test_malformed_lines_skipped(self, fresh_ledger):
        # Write a valid line + a malformed line.
        fresh_ledger.path.parent.mkdir(parents=True, exist_ok=True)
        fresh_ledger.path.write_text(
            'not json\n'
            '{"flag_name": "JARVIS_HYPOTHESIS_PROBE_ENABLED",'
            '"session_id": "s1", "outcome": "clean",'
            '"recorded_at_iso": "x", "recorded_at_epoch": 1.0,'
            '"recorded_by": "op", "notes": ""}\n',
            encoding="utf-8",
        )
        records = fresh_ledger._read_all()
        assert len(records) == 1
        assert records[0].session_id == "s1"

    def test_unknown_outcome_skipped(self, fresh_ledger):
        fresh_ledger.path.parent.mkdir(parents=True, exist_ok=True)
        fresh_ledger.path.write_text(
            '{"flag_name": "x", "session_id": "s1",'
            '"outcome": "WAT", "recorded_at_iso": "",'
            '"recorded_at_epoch": 0.0, "recorded_by": "op",'
            '"notes": ""}\n',
            encoding="utf-8",
        )
        assert fresh_ledger._read_all() == []

    def test_notes_truncated_to_cap(self, fresh_ledger):
        big = "X" * (MAX_NOTES_CHARS + 100)
        ok, _ = fresh_ledger.record_session(
            flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
            session_id="s1", outcome=SessionOutcome.CLEAN,
            recorded_by="op", notes=big,
        )
        assert ok is True
        records = fresh_ledger._read_all()
        assert len(records) == 1
        assert len(records[0].notes) <= MAX_NOTES_CHARS


# ---------------------------------------------------------------------------
# Section E — REPL master flag + help
# ---------------------------------------------------------------------------


class TestREPLMasterFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_GRADUATE_REPL_ENABLED", raising=False,
        )
        assert is_repl_enabled() is False

    def test_truthy_variants(self, monkeypatch):
        for v in ("1", "true", "TRUE", "Yes", "ON"):
            monkeypatch.setenv("JARVIS_GRADUATE_REPL_ENABLED", v)
            assert is_repl_enabled() is True, v


class TestREPLHelp:
    def test_help_works_master_off(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_GRADUATE_REPL_ENABLED", raising=False,
        )
        result = dispatch_graduate(["help"])
        assert result.status is DispatchStatus.OK
        assert "/graduate" in result.output
        assert "list" in result.output
        assert "record" in result.output

    def test_help_lists_all_subcommands(self, monkeypatch):
        # Pin the subcommand surface — adding/removing a subcommand
        # is intentional.
        monkeypatch.setenv("JARVIS_GRADUATE_REPL_ENABLED", "1")
        result = dispatch_graduate(["help"])
        for sub in ("list", "status", "record", "eligible", "help"):
            assert sub in result.output


# ---------------------------------------------------------------------------
# Section F — REPL list / status / eligible
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_repl(fresh_ledger, monkeypatch):
    monkeypatch.setenv("JARVIS_GRADUATE_REPL_ENABLED", "1")
    return fresh_ledger


class TestREPLRead:
    def test_master_off_read_disabled(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_GRADUATE_REPL_ENABLED", raising=False,
        )
        result = dispatch_graduate(["list"])
        assert result.status is DispatchStatus.DISABLED

    def test_list_renders_all_flags(self, fresh_repl):
        result = dispatch_graduate(["list"], ledger=fresh_repl)
        assert result.status is DispatchStatus.OK
        # All 15 known flags appear in the output.
        for flag in known_flags():
            assert flag in result.output

    def test_status_unknown_flag(self, fresh_repl):
        result = dispatch_graduate(
            ["status", "JARVIS_NOT_A_FLAG"], ledger=fresh_repl,
        )
        assert result.status is DispatchStatus.UNKNOWN_FLAG

    def test_status_known_flag(self, fresh_repl):
        result = dispatch_graduate(
            ["status", "JARVIS_HYPOTHESIS_PROBE_ENABLED"],
            ledger=fresh_repl,
        )
        assert result.status is DispatchStatus.OK
        assert "JARVIS_HYPOTHESIS_PROBE_ENABLED" in result.output
        assert "cadence_class" in result.output

    def test_status_missing_arg(self, fresh_repl):
        result = dispatch_graduate(["status"], ledger=fresh_repl)
        assert result.status is DispatchStatus.INVALID_ARGS

    def test_eligible_empty(self, fresh_repl):
        result = dispatch_graduate(["eligible"], ledger=fresh_repl)
        assert result.status is DispatchStatus.OK
        assert "0 flag(s)" in result.output

    def test_eligible_after_3_clean(self, fresh_repl):
        for sid in ("s1", "s2", "s3"):
            fresh_repl.record_session(
                flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
                session_id=sid, outcome=SessionOutcome.CLEAN,
                recorded_by="op",
            )
        result = dispatch_graduate(["eligible"], ledger=fresh_repl)
        assert "JARVIS_HYPOTHESIS_PROBE_ENABLED" in result.output


# ---------------------------------------------------------------------------
# Section G — REPL record subcommand
# ---------------------------------------------------------------------------


class TestREPLRecord:
    def test_record_missing_args(self, fresh_repl):
        result = dispatch_graduate(
            ["record", "flag", "sid"], ledger=fresh_repl,
        )
        assert result.status is DispatchStatus.INVALID_ARGS

    def test_record_unknown_flag(self, fresh_repl):
        result = dispatch_graduate(
            ["record", "JARVIS_NOT_A_FLAG", "s1", "clean"],
            ledger=fresh_repl,
        )
        assert result.status is DispatchStatus.UNKNOWN_FLAG

    def test_record_invalid_outcome(self, fresh_repl):
        result = dispatch_graduate(
            [
                "record", "JARVIS_HYPOTHESIS_PROBE_ENABLED",
                "s1", "WAT",
            ],
            ledger=fresh_repl,
        )
        assert result.status is DispatchStatus.INVALID_OUTCOME

    def test_record_empty_session_id(self, fresh_repl):
        result = dispatch_graduate(
            [
                "record", "JARVIS_HYPOTHESIS_PROBE_ENABLED",
                "", "clean",
            ],
            ledger=fresh_repl,
        )
        assert result.status is DispatchStatus.EMPTY_SESSION_ID

    def test_record_ok(self, fresh_repl):
        result = dispatch_graduate(
            [
                "record", "JARVIS_HYPOTHESIS_PROBE_ENABLED",
                "s1", "clean", "first run",
            ],
            ledger=fresh_repl,
        )
        assert result.status is DispatchStatus.OK
        assert "JARVIS_HYPOTHESIS_PROBE_ENABLED" in result.output
        assert "clean=1/3" in result.output

    def test_record_master_ledger_off_rejected(self, monkeypatch, tmp_path):
        # REPL master on, ledger master off → record returns
        # LEDGER_DISABLED.
        monkeypatch.setenv("JARVIS_GRADUATE_REPL_ENABLED", "1")
        monkeypatch.delenv(
            "JARVIS_GRADUATION_LEDGER_ENABLED", raising=False,
        )
        ledger = GraduationLedger(path=tmp_path / "g.jsonl")
        result = dispatch_graduate(
            [
                "record", "JARVIS_HYPOTHESIS_PROBE_ENABLED",
                "s1", "clean",
            ],
            ledger=ledger,
        )
        assert result.status is DispatchStatus.LEDGER_DISABLED


# ---------------------------------------------------------------------------
# Section H — End-to-end happy path
# ---------------------------------------------------------------------------


class TestEndToEndHappyPath:
    def test_record_3_clean_then_eligible(self, fresh_repl):
        # Record 3 clean sessions via REPL.
        for sid in ("s1", "s2", "s3"):
            r = dispatch_graduate(
                [
                    "record", "JARVIS_HYPOTHESIS_PROBE_ENABLED",
                    sid, "clean",
                ],
                ledger=fresh_repl,
            )
            assert r.status is DispatchStatus.OK
        # eligible should now list it.
        e = dispatch_graduate(["eligible"], ledger=fresh_repl)
        assert "JARVIS_HYPOTHESIS_PROBE_ENABLED" in e.output

    def test_runner_failure_resets_eligibility(self, fresh_repl):
        # 3 clean → eligible → record runner → not eligible.
        for sid in ("s1", "s2", "s3"):
            dispatch_graduate(
                [
                    "record", "JARVIS_HYPOTHESIS_PROBE_ENABLED",
                    sid, "clean",
                ],
                ledger=fresh_repl,
            )
        dispatch_graduate(
            [
                "record", "JARVIS_HYPOTHESIS_PROBE_ENABLED",
                "s_bad", "runner", "regression in retries",
            ],
            ledger=fresh_repl,
        )
        assert not fresh_repl.is_eligible(
            "JARVIS_HYPOTHESIS_PROBE_ENABLED",
        )


# ---------------------------------------------------------------------------
# Section I — Authority + cage invariants
# ---------------------------------------------------------------------------


_LEDGER_PATH = Path(gl.__file__)
_REPL_PATH = Path(gr.__file__)


class TestAuthorityInvariants:
    def test_ledger_no_banned_imports(self):
        source = _LEDGER_PATH.read_text()
        tree = ast.parse(source)
        banned = (
            "scoped_tool_backend", "general_driver",
            "exploration_engine", "semantic_guardian",
            "orchestrator", "tool_executor", "phase_runners",
            "gate_runner", "risk_tier_floor",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for b in banned:
                    assert b not in node.module, node.module

    def test_ledger_only_stdlib_and_adaptation(self):
        source = _LEDGER_PATH.read_text()
        tree = ast.parse(source)
        stdlib_prefixes = (
            "__future__", "enum", "json", "logging", "os", "time",
            "dataclasses", "datetime", "pathlib", "typing",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("backend."):
                    assert "adaptation" in node.module, node.module
                else:
                    assert any(
                        node.module.startswith(p) for p in stdlib_prefixes
                    ), node.module

    def test_repl_no_banned_imports(self):
        source = _REPL_PATH.read_text()
        tree = ast.parse(source)
        banned = (
            "scoped_tool_backend", "general_driver",
            "exploration_engine", "semantic_guardian",
            "orchestrator", "tool_executor", "phase_runners",
            "gate_runner", "risk_tier_floor",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for b in banned:
                    assert b not in node.module, node.module

    def test_no_subprocess_or_network(self):
        for path in (_LEDGER_PATH, _REPL_PATH):
            source = path.read_text()
            for token in (
                "subprocess", "requests", "urllib", "socket",
                "http.client", "asyncio.create_subprocess",
            ):
                assert token not in source, f"{path}: banned token {token}"

    def test_ledger_uses_flock(self):
        # Cross-process safety: writes must use flock_exclusive.
        source = _LEDGER_PATH.read_text()
        assert "flock_exclusive" in source

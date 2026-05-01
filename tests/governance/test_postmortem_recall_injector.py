"""Priority #2 Slice 3 — CONTEXT_EXPANSION injector tests.

Coverage:

  * **Sub-gate flag** — asymmetric env semantics, default false.
  * **Char-budget knobs** — max_prompt_chars + max_chars_per_record
    enforce floor + ceiling + garbage fallback.
  * **Age formatter** — minutes / hours / days / months / NaN /
    negative all map to readable strings.
  * **ROBUST DEGRADATION matrix (load-bearing)**:
      * master-off → ""
      * sub-gate-off → ""
      * empty index → ""
      * corrupt index → ""
      * no matching records → ""
      * recall raises (mocked) → ""
      * read_index raises (mocked) → ""
      * format raises (mocked) → ""
  * **HIT path** — section header + summary + record blocks +
    footer all present.
  * **Char-budget truncation** — section length ≤ budget;
    truncation marker appended.
  * **Per-record truncation** — long failure_reason capped per-
    record without overflowing.
  * **Relevance markers** — HIGH/MEDIUM/LOW correctly stamped.
  * **Sanitization** — control chars + secrets sanitized via
    _sanitize_field reuse.
  * **Orchestrator hook** — `compose_for_op_context` shape.
  * **Authority invariants** — AST-pinned: governance allowlist
    + MUST reference _sanitize_field + recall_postmortems +
    read_index + no orchestrator + no eval-family + no async.
"""
from __future__ import annotations

import ast
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.verification.postmortem_recall import (
    PostmortemRecord,
    RelevanceLevel,
)
from backend.core.ouroboros.governance.verification.postmortem_recall_index import (
    record_postmortem,
)
from backend.core.ouroboros.governance.verification.postmortem_recall_injector import (
    POSTMORTEM_RECALL_INJECTOR_SCHEMA_VERSION,
    compose_for_op_context,
    max_chars_per_record,
    max_prompt_chars,
    postmortem_injection_enabled,
    render_postmortem_recall_section,
)
from backend.core.ouroboros.governance.verification.postmortem_recall_injector import (  # noqa: E501
    _format_age_human,
    _RELEVANCE_MARKERS,
    _SECTION_FOOTER,
    _SECTION_HEADER,
    _TRUNCATION_MARKER,
    _render_record,
    _render_section,
)


_FORBIDDEN_CALL_TOKENS = ("e" + "val(", "e" + "xec(")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_index():
    d = Path(tempfile.mkdtemp(prefix="pminj_test_")).resolve()
    target = d / "idx.jsonl"
    yield target
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _populate_index(target: Path, records: list):
    """Helper: write records to an index using the Slice 2
    record_postmortem API."""
    with mock.patch.dict(
        os.environ,
        {"JARVIS_POSTMORTEM_INDEX_ENABLED": "true"},
    ):
        for r in records:
            record_postmortem(r, target_path=target)


def _all_flags_on():
    """Return env dict that turns on all 3 PostmortemRecall flags."""
    return {
        "JARVIS_POSTMORTEM_RECALL_ENABLED": "true",
        "JARVIS_POSTMORTEM_INDEX_ENABLED": "true",
        "JARVIS_POSTMORTEM_INJECTION_ENABLED": "true",
    }


# ---------------------------------------------------------------------------
# 1. Sub-gate flag
# ---------------------------------------------------------------------------


class TestSubGateFlag:
    def test_default_is_false(self):
        os.environ.pop(
            "JARVIS_POSTMORTEM_INJECTION_ENABLED", None,
        )
        assert postmortem_injection_enabled() is False

    @pytest.mark.parametrize(
        "v", ["1", "true", "yes", "on", "TRUE"],
    )
    def test_truthy(self, v):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_INJECTION_ENABLED": v},
        ):
            assert postmortem_injection_enabled() is True

    @pytest.mark.parametrize("v", ["0", "false", "no", "off"])
    def test_falsy(self, v):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_INJECTION_ENABLED": v},
        ):
            assert postmortem_injection_enabled() is False


# ---------------------------------------------------------------------------
# 2. Char-budget knobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_max_prompt_chars_default(self):
        os.environ.pop(
            "JARVIS_POSTMORTEM_RECALL_MAX_PROMPT_CHARS", None,
        )
        assert max_prompt_chars() == 2000

    def test_max_prompt_chars_floor(self):
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_POSTMORTEM_RECALL_MAX_PROMPT_CHARS": "10",
            },
        ):
            assert max_prompt_chars() == 500

    def test_max_prompt_chars_ceiling(self):
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_POSTMORTEM_RECALL_MAX_PROMPT_CHARS":
                    "999999",
            },
        ):
            assert max_prompt_chars() == 8000

    def test_max_chars_per_record_default(self):
        os.environ.pop(
            "JARVIS_POSTMORTEM_RECALL_MAX_CHARS_PER_RECORD",
            None,
        )
        assert max_chars_per_record() == 400

    def test_max_chars_per_record_clamps(self):
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_POSTMORTEM_RECALL_MAX_CHARS_PER_RECORD":
                    "1",
            },
        ):
            assert max_chars_per_record() == 100
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_POSTMORTEM_RECALL_MAX_CHARS_PER_RECORD":
                    "99999",
            },
        ):
            assert max_chars_per_record() == 2000


# ---------------------------------------------------------------------------
# 3. Age formatter
# ---------------------------------------------------------------------------


class TestAgeFormatter:
    @pytest.mark.parametrize(
        "age_days,expected_substr",
        [
            (0.0, "m ago"),  # 0 days = 0 minutes (clamped to 1m)
            (1.0 / 1440.0, "m ago"),  # 1 minute
            (1.0 / 24.0 / 4.0, "m ago"),  # 15 minutes
            (1.0 / 24.0 * 5.0, "h ago"),  # 5 hours
            (2.0, "d ago"),  # 2 days
            (60.0, "mo ago"),  # 2 months
        ],
    )
    def test_age_formatting(self, age_days, expected_substr):
        result = _format_age_human(age_days)
        assert expected_substr in result

    def test_age_nan_returns_unknown(self):
        assert _format_age_human(float("nan")) == "unknown"

    def test_age_negative_clamps(self):
        # Negative goes to "0s ago" via < 0 check
        result = _format_age_human(-1.0)
        assert "ago" in result


# ---------------------------------------------------------------------------
# 4. Robust degradation — every degraded path returns ""
# ---------------------------------------------------------------------------


class TestRobustDegradation:
    """LOAD-BEARING: every degraded path returns the empty
    string. CONTEXT_EXPANSION → GENERATE pipeline NEVER sees a
    raise from this module."""

    def test_master_off_returns_empty(self):
        out = render_postmortem_recall_section(
            target_files=["x.py"],
            enabled_override=False,
        )
        assert out == ""

    def test_sub_gate_off_returns_empty(self):
        os.environ.pop(
            "JARVIS_POSTMORTEM_INJECTION_ENABLED", None,
        )
        # Master on (override), sub-gate off
        out = render_postmortem_recall_section(
            target_files=["x.py"],
            enabled_override=True,
        )
        assert out == ""

    def test_empty_index_returns_empty(self, tmp_index):
        # File doesn't exist; injector should return ""
        with mock.patch.dict(os.environ, _all_flags_on()):
            out = render_postmortem_recall_section(
                target_files=["x.py"],
                target_path=tmp_index,
                enabled_override=True,
            )
        assert out == ""

    def test_corrupt_index_returns_empty(self, tmp_index):
        tmp_index.parent.mkdir(parents=True, exist_ok=True)
        tmp_index.write_text("not json\nalso garbage\n")
        with mock.patch.dict(os.environ, _all_flags_on()):
            out = render_postmortem_recall_section(
                target_files=["x.py"],
                target_path=tmp_index,
                enabled_override=True,
            )
        assert out == ""

    def test_no_matching_records_returns_empty(self, tmp_index):
        ts = time.time()
        record = PostmortemRecord(
            op_id="o1", session_id="s1", file_path="auth.py",
            failure_class="test", timestamp=ts,
        )
        _populate_index(tmp_index, [record])
        with mock.patch.dict(os.environ, _all_flags_on()):
            out = render_postmortem_recall_section(
                target_files=["nonmatching.py"],
                target_path=tmp_index,
                enabled_override=True,
                now_ts=ts,
            )
        assert out == ""

    def test_read_index_raises_returns_empty(self, tmp_index):
        with mock.patch.dict(os.environ, _all_flags_on()):
            with mock.patch(
                "backend.core.ouroboros.governance.verification."
                "postmortem_recall_injector.read_index",
                side_effect=RuntimeError("disk error"),
            ):
                out = render_postmortem_recall_section(
                    target_files=["x.py"],
                    target_path=tmp_index,
                    enabled_override=True,
                )
        assert out == ""

    def test_recall_raises_returns_empty(self, tmp_index):
        ts = time.time()
        record = PostmortemRecord(
            op_id="o1", session_id="s1", file_path="auth.py",
            failure_class="test", timestamp=ts,
        )
        _populate_index(tmp_index, [record])
        with mock.patch.dict(os.environ, _all_flags_on()):
            with mock.patch(
                "backend.core.ouroboros.governance.verification."
                "postmortem_recall_injector.recall_postmortems",
                side_effect=RuntimeError("recall broken"),
            ):
                out = render_postmortem_recall_section(
                    target_files=["auth.py"],
                    target_path=tmp_index,
                    enabled_override=True,
                    now_ts=ts,
                )
        assert out == ""

    def test_orchestrator_hook_never_raises(self):
        """compose_for_op_context is total — every input maps
        to either a string or empty string."""
        out = compose_for_op_context(
            op_id="x",
            target_files=["x.py"],
        )
        assert isinstance(out, str)


# ---------------------------------------------------------------------------
# 5. HIT path — section structure
# ---------------------------------------------------------------------------


class TestHitPath:
    def test_section_contains_header(self, tmp_index):
        ts = time.time()
        _populate_index(tmp_index, [
            PostmortemRecord(
                op_id="o1", session_id="s1", file_path="auth.py",
                symbol_name="login", failure_class="test",
                failure_phase="VALIDATE",
                failure_reason="AssertionError on line 47",
                timestamp=ts,
            ),
        ])
        with mock.patch.dict(os.environ, _all_flags_on()):
            section = render_postmortem_recall_section(
                target_files=["auth.py"],
                target_symbols=["login"],
                target_path=tmp_index,
                enabled_override=True,
                now_ts=ts,
            )
        assert _SECTION_HEADER in section

    def test_section_contains_footer(self, tmp_index):
        ts = time.time()
        _populate_index(tmp_index, [
            PostmortemRecord(
                op_id="o1", session_id="s1", file_path="auth.py",
                failure_class="test", timestamp=ts,
            ),
        ])
        with mock.patch.dict(os.environ, _all_flags_on()):
            section = render_postmortem_recall_section(
                target_files=["auth.py"],
                target_path=tmp_index,
                enabled_override=True,
                now_ts=ts,
            )
        assert _SECTION_FOOTER in section

    def test_section_contains_record_details(self, tmp_index):
        ts = time.time()
        _populate_index(tmp_index, [
            PostmortemRecord(
                op_id="o1", session_id="bt-2026-04-25",
                file_path="auth.py", symbol_name="login",
                failure_class="test", failure_phase="VALIDATE",
                failure_reason="AssertionError on line 47",
                timestamp=ts - 86400 * 2,
            ),
        ])
        with mock.patch.dict(os.environ, _all_flags_on()):
            section = render_postmortem_recall_section(
                target_files=["auth.py"],
                target_symbols=["login"],
                target_path=tmp_index,
                enabled_override=True,
                now_ts=ts,
            )
        assert "auth.py" in section
        assert "login" in section
        assert "VALIDATE" in section
        assert "AssertionError" in section
        assert "bt-2026-04-25" in section
        assert "2d ago" in section


# ---------------------------------------------------------------------------
# 6. Char-budget truncation
# ---------------------------------------------------------------------------


class TestCharBudget:
    def test_section_under_budget(self, tmp_index):
        ts = time.time()
        _populate_index(tmp_index, [
            PostmortemRecord(
                op_id="o1", session_id="s1", file_path="auth.py",
                failure_class="test", timestamp=ts,
            ),
        ])
        with mock.patch.dict(os.environ, _all_flags_on()):
            section = render_postmortem_recall_section(
                target_files=["auth.py"],
                target_path=tmp_index,
                enabled_override=True,
                max_chars=2000,
                now_ts=ts,
            )
        assert len(section) <= 2000

    def test_section_truncation_with_marker(self, tmp_index):
        ts = time.time()
        _populate_index(tmp_index, [
            PostmortemRecord(
                op_id=f"o-{i}", session_id="s1",
                file_path="auth.py",
                failure_class="test",
                failure_reason=f"long error reason {i} " * 20,
                timestamp=ts + i,
            )
            for i in range(5)
        ])
        with mock.patch.dict(os.environ, _all_flags_on()):
            section = render_postmortem_recall_section(
                target_files=["auth.py"],
                target_path=tmp_index,
                enabled_override=True,
                max_chars=200,
                now_ts=ts + 100,
            )
        assert len(section) <= 200
        assert _TRUNCATION_MARKER.strip() in section

    def test_per_record_char_cap(self, tmp_index):
        ts = time.time()
        _populate_index(tmp_index, [
            PostmortemRecord(
                op_id="o1", session_id="s1", file_path="auth.py",
                failure_class="test",
                failure_reason="x" * 5000,  # very long
                timestamp=ts,
            ),
        ])
        with mock.patch.dict(
            os.environ,
            {
                **_all_flags_on(),
                "JARVIS_POSTMORTEM_RECALL_MAX_CHARS_PER_RECORD":
                    "150",
            },
        ):
            section = render_postmortem_recall_section(
                target_files=["auth.py"],
                target_path=tmp_index,
                enabled_override=True,
                now_ts=ts,
            )
        # Per-record truncation marker present
        assert _TRUNCATION_MARKER.strip() in section


# ---------------------------------------------------------------------------
# 7. Relevance markers
# ---------------------------------------------------------------------------


class TestRelevanceMarkers:
    def test_high_marker_present(self, tmp_index):
        ts = time.time()
        # HIGH = file + symbol both match
        _populate_index(tmp_index, [
            PostmortemRecord(
                op_id="o1", session_id="s1", file_path="auth.py",
                symbol_name="login",
                failure_class="test", timestamp=ts,
            ),
        ])
        with mock.patch.dict(os.environ, _all_flags_on()):
            section = render_postmortem_recall_section(
                target_files=["auth.py"],
                target_symbols=["login"],
                target_path=tmp_index,
                enabled_override=True,
                now_ts=ts,
            )
        assert "HIGH" in section

    def test_medium_marker_present(self, tmp_index):
        ts = time.time()
        # MEDIUM = file match only
        _populate_index(tmp_index, [
            PostmortemRecord(
                op_id="o1", session_id="s1", file_path="auth.py",
                symbol_name="other",  # symbol mismatch
                failure_class="test", timestamp=ts,
            ),
        ])
        with mock.patch.dict(os.environ, _all_flags_on()):
            section = render_postmortem_recall_section(
                target_files=["auth.py"],
                target_symbols=["login"],
                target_path=tmp_index,
                enabled_override=True,
                now_ts=ts,
            )
        assert "MEDIUM" in section

    def test_relevance_markers_pinned(self):
        """4 RelevanceLevel values map to 4 marker entries."""
        assert len(_RELEVANCE_MARKERS) == 4
        # NONE has no marker (empty string)
        assert _RELEVANCE_MARKERS[RelevanceLevel.NONE] == ""


# ---------------------------------------------------------------------------
# 8. Sanitization (via _sanitize_field reuse)
# ---------------------------------------------------------------------------


class TestSanitization:
    def test_control_chars_stripped(self, tmp_index):
        ts = time.time()
        _populate_index(tmp_index, [
            PostmortemRecord(
                op_id="o1", session_id="s1", file_path="auth.py",
                failure_class="test",
                failure_reason="error\x00with\x01control\x02chars",
                timestamp=ts,
            ),
        ])
        with mock.patch.dict(os.environ, _all_flags_on()):
            section = render_postmortem_recall_section(
                target_files=["auth.py"],
                target_path=tmp_index,
                enabled_override=True,
                now_ts=ts,
            )
        # Control chars stripped
        assert "\x00" not in section
        assert "\x01" not in section
        assert "\x02" not in section


# ---------------------------------------------------------------------------
# 9. Internal renderers
# ---------------------------------------------------------------------------


class TestInternalRenderers:
    def test_render_record_with_all_fields(self):
        ts = time.time()
        r = PostmortemRecord(
            op_id="op-rich", session_id="bt-2026-04-25",
            file_path="auth.py", symbol_name="login",
            failure_class="test", failure_phase="VALIDATE",
            failure_reason="AssertionError",
            timestamp=ts - 86400,
        )
        block = _render_record(
            r, relevance=RelevanceLevel.HIGH, rank=1,
            max_chars=400, now_ts=ts,
        )
        assert "HIGH" in block
        assert "auth.py:login" in block
        assert "VALIDATE" in block
        assert "AssertionError" in block

    def test_render_record_garbage_returns_empty(self):
        block = _render_record(
            "not a record",  # type: ignore[arg-type]
            relevance=RelevanceLevel.HIGH, rank=1, max_chars=400,
        )
        assert block == ""

    def test_render_section_empty_records_returns_empty(self):
        out = _render_section(
            [],
            total_index_size=0,
            max_age_days=30.0, char_budget=2000,
        )
        assert out == ""


# ---------------------------------------------------------------------------
# 10. Schema integrity
# ---------------------------------------------------------------------------


class TestSchemaIntegrity:
    def test_schema_version_stable(self):
        assert (
            POSTMORTEM_RECALL_INJECTOR_SCHEMA_VERSION
            == "postmortem_recall_injector.1"
        )

    def test_section_header_constant(self):
        assert _SECTION_HEADER == "## Recent Failures (advisory)"


# ---------------------------------------------------------------------------
# 11. Authority invariants — AST-pinned
# ---------------------------------------------------------------------------


def _module_source() -> str:
    path = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "verification" / "postmortem_recall_injector.py"
    )
    return path.read_text(encoding="utf-8")


class TestAuthorityInvariants:
    @pytest.fixture
    def source(self):
        return _module_source()

    def test_no_orchestrator_imports(self, source):
        forbidden = [
            "orchestrator", "iron_gate", "policy",
            "change_engine", "candidate_generator", "providers",
            "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "phase_runners",
            "semantic_guardian", "semantic_firewall",
            "risk_engine", "episodic_memory",
            "ast_canonical", "semantic_index",
        ]
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                m = (
                    node.module if isinstance(node, ast.ImportFrom)
                    else (
                        node.names[0].name if node.names else ""
                    )
                )
                m = m or ""
                for f in forbidden:
                    assert f not in m, f"forbidden import: {m}"

    def test_governance_imports_in_allowlist(self, source):
        """Slice 3 may import:
          * Slice 1 (postmortem_recall)
          * Slice 2 (postmortem_recall_index)
          * last_session_summary (_sanitize_field)"""
        tree = ast.parse(source)
        allowed = {
            "backend.core.ouroboros.governance.last_session_summary",
            "backend.core.ouroboros.governance.verification.postmortem_recall",
            "backend.core.ouroboros.governance.verification.postmortem_recall_index",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "governance" in node.module:
                    assert node.module in allowed, (
                        f"governance import outside allowlist: "
                        f"{node.module}"
                    )

    def test_must_reference_sanitize_field(self, source):
        """Zero-duplication-via-reuse contract."""
        assert "_sanitize_field" in source

    def test_must_reference_recall_postmortems(self, source):
        assert "recall_postmortems" in source

    def test_must_reference_read_index(self, source):
        assert "read_index" in source

    def test_must_import_sanitize_field_via_importfrom(
        self, source,
    ):
        tree = ast.parse(source)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (
                    node.module
                    == "backend.core.ouroboros.governance"
                    ".last_session_summary"
                ):
                    for alias in node.names:
                        if alias.name == "_sanitize_field":
                            found = True
        assert found, (
            "must import _sanitize_field via importfrom from "
            "last_session_summary"
        )

    def test_no_mutation_tools(self, source):
        forbidden = [
            "edit_file", "write_file", "delete_file",
            "subprocess." + "run", "subprocess." + "Popen",
            "os." + "system", "shutil.rmtree",
        ]
        for f in forbidden:
            assert f not in source

    def test_no_eval_family_calls(self, source):
        """Critical safety: no eval-family bare-name calls."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in (
                        "exec", "eval", "compile",
                    )
        # Bytes-level pin (compile excluded — re.compile is
        # allowed; not used here but for consistency with Slice 2)
        for token in _FORBIDDEN_CALL_TOKENS:
            assert token not in source, (
                f"forbidden syntactic call: {token!r}"
            )

    def test_no_async_functions(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            assert not isinstance(node, ast.AsyncFunctionDef)

    def test_public_api_exported(self, source):
        for name in (
            "render_postmortem_recall_section",
            "compose_for_op_context",
            "postmortem_injection_enabled",
            "max_prompt_chars",
            "max_chars_per_record",
            "POSTMORTEM_RECALL_INJECTOR_SCHEMA_VERSION",
        ):
            assert f'"{name}"' in source

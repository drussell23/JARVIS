"""Producer-driven schema contract for the battle-test report renderers.

``tests/test_ouroboros_governance/test_battle_test_notebook_generator.py``
validates :class:`NotebookGenerator` against ``_make_summary`` — a HAND-WRITTEN
shape that no writer in this repo has ever produced. It nests the outcome
counters under ``operations``, the per-op records under ``operation_log``, and
convergence / cost / branch under sub-objects.
:meth:`SessionRecorder.save_summary` writes none of that: the counters live in
``stats``, ``operations`` **is** the record list, and the rest is flattened into
``convergence_state`` / ``cost_total`` / ``branch_stats``.

Because every existing test validated the renderer against the renderer's own
fiction, the mismatch survived to production, where it surfaced as::

    AttributeError: 'list' object has no attribute 'get'

at the counter read in ``generate_markdown`` — and, on the notebook path, as
cells that would have rendered a well-formed report full of zeroes without
raising at all. That second mode is the worse one: a crash leaves a fault log,
a blank report looks like a finished artifact.

The tests here drive the REAL producer. They cannot drift from the writer,
because they call it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.notebook_generator import (
    NotebookGenerator,
    normalise_summary,
)
from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder

# A known, asymmetric mix — every counter distinct so a transposed field
# cannot pass by coincidence.
_OP_MIX = [
    ("completed", 0.80, "OpportunityMinerSensor"),
    ("completed", 0.75, "OpportunityMinerSensor"),
    ("failed", 0.0, "TestFailureSensor"),
    ("cancelled", 0.0, "TestFailureSensor"),
    ("queued", 0.0, "DocStalenessSensor"),
]

_BRANCH_STATS = {"commits": 2, "files_changed": 3, "insertions": 40, "deletions": 7}


def _record_real_session(session_id: str = "bt-producer-driven-001") -> SessionRecorder:
    """Build a recorder carrying the fixed op mix above."""
    recorder = SessionRecorder(session_id)
    for idx, (status, score, sensor) in enumerate(_OP_MIX):
        recorder.record_operation(
            op_id=f"op-{idx}",
            status=status,
            sensor=sensor,
            technique="module_mutation",
            composite_score=score,
            elapsed_s=1.5,
            provider="local_jprime",
            cost_usd=0.0,
        )
    return recorder


def _save_real_summary(recorder: SessionRecorder, output_dir: Path) -> Path:
    """Persist through the PRODUCTION writer and return the summary path."""
    return Path(
        recorder.save_summary(
            output_dir=output_dir,
            stop_reason="idle_timeout",
            duration_s=670.0,
            cost_total=0.0,
            cost_breakdown={},
            branch_stats=dict(_BRANCH_STATS),
            convergence_state="INSUFFICIENT_DATA",
            convergence_slope=0.0,
            convergence_r2=0.0,
            session_outcome="complete",
        )
    )


class TestRecorderSchemaIsRenderable:
    """The shape SessionRecorder actually writes must render, not crash."""

    def test_markdown_renders_from_recorder_output(self, tmp_path):
        summary_path = _save_real_summary(_record_real_session(), tmp_path)

        generator = NotebookGenerator(summary_path)
        content = generator.generate_markdown(tmp_path / "md").read_text()

        # The exact production regression: this read raised AttributeError
        # against the recorder's list-shaped ``operations``.
        assert "| Attempted | 5 |" in content
        assert "| Completed | 2 |" in content
        assert "| Failed | 1 |" in content
        assert "| Cancelled | 1 |" in content
        assert "| Queued | 1 |" in content

    def test_recorder_output_is_not_silently_blank(self, tmp_path):
        """Every section must carry real values, not schema-miss defaults."""
        summary_path = _save_real_summary(_record_real_session(), tmp_path)
        view = NotebookGenerator(summary_path)._view

        assert view["session_id"] == "bt-producer-driven-001"
        assert view["stop_reason"] == "idle_timeout"
        assert view["duration_s"] == pytest.approx(670.0)
        assert view["convergence"]["state"] == "INSUFFICIENT_DATA"
        assert len(view["operation_log"]) == len(_OP_MIX)
        assert view["scores"] == pytest.approx([0.80, 0.75, 0.0, 0.0, 0.0])
        assert view["branch"] == _BRANCH_STATS
        assert dict(view["top_sensors"])["OpportunityMinerSensor"] == 2

    def test_counters_match_the_recorders_own_stats(self, tmp_path):
        """The view must agree with ``SessionRecorder.stats`` field for field.

        This is the anti-drift assertion. If the writer's counter schema moves
        again, this fails at the seam instead of in a shutdown-path traceback.
        """
        recorder = _record_real_session()
        summary_path = _save_real_summary(recorder, tmp_path)

        assert NotebookGenerator(summary_path)._view["op_counts"] == recorder.stats

    def test_notebook_path_embeds_the_normalised_view(self, tmp_path):
        """The ``.ipynb`` must carry real numbers, not a phantom-schema blank."""
        nbformat = pytest.importorskip("nbformat")

        summary_path = _save_real_summary(_record_real_session(), tmp_path)
        nb_path = NotebookGenerator(summary_path).generate_notebook(
            tmp_path / "report.ipynb"
        )
        notebook = nbformat.read(str(nb_path), as_version=4)
        source = "\n".join(cell.source for cell in notebook.cells)

        # No cell may reach for a shape the recorder does not write.
        assert "data.get('operation_log'" not in source
        assert "data.get('operations'" not in source
        assert "_VIEW_JSON" in source
        assert '"attempted": 5' in source


class TestSchemaAdapterAcceptsBothGenerations:
    """One seam, two schemas — neither renderer should know which it holds."""

    def test_nested_and_flat_summaries_normalise_alike(self):
        nested = {
            "session_id": "bt-test-session-001",
            "stop_reason": "budget",
            "duration_s": 300.0,
            "operations": {
                "attempted": 10,
                "completed": 8,
                "failed": 1,
                "cancelled": 1,
                "queued": 2,
            },
            "cost": {"total": 0.48, "breakdown": {"doubleword_397b": 0.41}},
            "branch": dict(_BRANCH_STATS),
            "convergence": {"state": "improving", "slope": -0.014, "r_squared_log": 0.73},
            "top_sensors": [["OpportunityMinerSensor", 5]],
            "top_techniques": [["module_mutation", 6]],
            "operation_log": [{"op_id": "op-0", "status": "completed", "composite_score": 0.8}],
        }
        flat = {
            "session_id": nested["session_id"],
            "stop_reason": nested["stop_reason"],
            "duration_s": nested["duration_s"],
            "stats": nested["operations"],
            "operations": nested["operation_log"],
            "cost_total": nested["cost"]["total"],
            "cost_breakdown": nested["cost"]["breakdown"],
            "branch_stats": nested["branch"],
            "convergence_state": nested["convergence"]["state"],
            "convergence_slope": nested["convergence"]["slope"],
            "convergence_r2": nested["convergence"]["r_squared_log"],
            "top_sensors": nested["top_sensors"],
            "top_techniques": nested["top_techniques"],
        }

        assert normalise_summary(nested) == normalise_summary(flat)

    def test_records_without_counters_are_counted_from_the_records(self):
        """A partial (signal-killed) summary still deserves a real table."""
        view = normalise_summary(
            {
                "session_id": "bt-partial",
                "operations": [
                    {"op_id": "a", "status": "completed"},
                    {"op_id": "b", "status": "failed"},
                    {"op_id": "c", "status": "failed"},
                ],
            }
        )

        assert view["op_counts"]["attempted"] == 3
        assert view["op_counts"]["failed"] == 2
        assert view["op_counts"]["completed"] == 1

    def test_null_numerics_do_not_raise_in_a_format_spec(self, tmp_path):
        """``None`` where a float is expected must degrade, not traceback.

        A summary written from a signal handler is exactly when the report
        matters most, and exactly when fields are absent.
        """
        summary_path = tmp_path / "summary.json"
        summary_path.write_text(
            json.dumps(
                {
                    "session_id": "bt-signal-killed",
                    "stop_reason": "sigterm",
                    "duration_s": None,
                    "convergence_slope": None,
                    "cost_total": None,
                    "operations": [],
                }
            )
        )

        content = NotebookGenerator(summary_path).generate_markdown(tmp_path).read_text()

        assert "bt-signal-killed" in content
        assert "**Total cost:** $0.0000" in content

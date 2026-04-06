"""Tests for the Ouroboros Battle Test NotebookGenerator."""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from backend.core.ouroboros.battle_test.notebook_generator import NotebookGenerator


def _make_summary(tmp_path: Path) -> Path:
    """Create a minimal summary.json with all required fields for testing."""
    summary = {
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
        "cost": {
            "total": 0.48,
            "breakdown": {"doubleword_397b": 0.41, "anthropic": 0.07},
        },
        "branch": {
            "commits": 8,
            "files_changed": 12,
            "insertions": 200,
            "deletions": 50,
        },
        "convergence": {
            "state": "improving",
            "slope": -0.014,
            "r_squared_log": 0.73,
        },
        "top_sensors": [
            ["OpportunityMinerSensor", 5],
            ["TestFailureSensor", 3],
        ],
        "top_techniques": [
            ["module_mutation", 6],
            ["metrics_feedback", 2],
        ],
        "operation_log": [
            {
                "op_id": "op-0",
                "status": "completed",
                "composite_score": 0.8,
                "technique": "module_mutation",
            },
            {
                "op_id": "op-1",
                "status": "completed",
                "composite_score": 0.75,
                "technique": "metrics_feedback",
            },
            {
                "op_id": "op-2",
                "status": "failed",
                "composite_score": None,
                "technique": "module_mutation",
            },
        ],
    }
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    return summary_path


class TestGenerateMarkdownFallback:
    """test_generate_markdown_fallback: verify .md created with required content."""

    def test_generate_markdown_fallback(self, tmp_path):
        summary_path = _make_summary(tmp_path)
        gen = NotebookGenerator(summary_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = gen.generate_markdown(output_dir)

        assert result.exists(), "Markdown file should exist"
        assert result.suffix == ".md", "Output should be a .md file"
        content = result.read_text()

        # Must contain session_id
        assert "bt-test-session-001" in content, "Should contain session_id"

        # Must contain convergence state
        assert "improving" in content, "Should contain convergence state"

        # Must contain sensor names
        assert "OpportunityMinerSensor" in content, "Should contain first sensor name"
        assert "TestFailureSensor" in content, "Should contain second sensor name"


class TestGenerateNotebook:
    """test_generate_notebook: verify .ipynb created with valid structure."""

    def test_generate_notebook(self, tmp_path):
        try:
            import nbformat  # noqa: F401
        except ImportError:
            pytest.skip("nbformat not installed")

        summary_path = _make_summary(tmp_path)
        gen = NotebookGenerator(summary_path)
        output_path = tmp_path / "report.ipynb"

        result = gen.generate_notebook(output_path)

        assert result.exists(), ".ipynb file should exist"
        assert result.suffix == ".ipynb", "Output should be a .ipynb file"

        # Must be valid JSON with "cells" key
        nb_data = json.loads(result.read_text())
        assert "cells" in nb_data, "Notebook JSON must have 'cells' key"
        assert len(nb_data["cells"]) >= 5, "Notebook must have at least 5 cells"

        # Verify cells alternate markdown/code appropriately
        cell_types = [cell["cell_type"] for cell in nb_data["cells"]]
        assert "markdown" in cell_types, "Should have markdown cells"
        assert "code" in cell_types, "Should have code cells"

    def test_notebook_contains_session_id(self, tmp_path):
        """Session ID should appear in notebook content (self-contained)."""
        try:
            import nbformat  # noqa: F401
        except ImportError:
            pytest.skip("nbformat not installed")

        summary_path = _make_summary(tmp_path)
        gen = NotebookGenerator(summary_path)
        output_path = tmp_path / "report.ipynb"

        gen.generate_notebook(output_path)
        content = output_path.read_text()

        assert "bt-test-session-001" in content, "Session ID must be embedded in notebook"


class TestGenerateAutoDetects:
    """test_generate_auto_detects: verify auto-detection produces either .ipynb or .md."""

    def test_generate_auto_detects(self, tmp_path):
        summary_path = _make_summary(tmp_path)
        gen = NotebookGenerator(summary_path)
        output_dir = tmp_path / "auto_output"
        output_dir.mkdir()

        result = gen.generate(output_dir)

        assert result.exists(), "Output file should exist"
        assert result.suffix in (".ipynb", ".md"), (
            f"Output must be .ipynb or .md, got: {result.suffix}"
        )

    def test_generate_returns_path(self, tmp_path):
        """generate() should return a Path object pointing to the created file."""
        summary_path = _make_summary(tmp_path)
        gen = NotebookGenerator(summary_path)
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        result = gen.generate(output_dir)

        assert isinstance(result, Path), "generate() must return a Path"
        assert result.is_absolute() or result.exists(), "Returned path must exist"


class TestNotebookGeneratorInit:
    """Verify NotebookGenerator loads summary data on construction."""

    def test_loads_summary_data(self, tmp_path):
        summary_path = _make_summary(tmp_path)
        gen = NotebookGenerator(summary_path)

        assert gen._data["session_id"] == "bt-test-session-001"
        assert gen._data["convergence"]["state"] == "improving"

    def test_raises_on_missing_file(self, tmp_path):
        missing = tmp_path / "nonexistent.json"
        with pytest.raises((FileNotFoundError, OSError)):
            NotebookGenerator(missing)

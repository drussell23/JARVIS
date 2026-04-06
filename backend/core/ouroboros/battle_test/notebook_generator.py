"""NotebookGenerator — creates a Jupyter notebook or Markdown report from battle test session data.

On session shutdown the BattleTestHarness calls :meth:`NotebookGenerator.generate`
which auto-detects whether ``nbformat`` is importable and falls back to a
Markdown report if it is not.

The summary JSON is embedded *directly* into the notebook code cells so the
notebook is fully self-contained and needs no external file references.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class NotebookGenerator:
    """Generate a Jupyter notebook or Markdown report from a battle test summary.

    Parameters
    ----------
    summary_path:
        Path to the ``summary.json`` file written by the BattleTestHarness.
        The file is loaded eagerly on construction.
    """

    def __init__(self, summary_path: Path) -> None:
        self._summary_path = Path(summary_path)
        raw = self._summary_path.read_text()
        self._data: Dict[str, Any] = json.loads(raw)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, output_dir: Path) -> Path:
        """Auto-detect and generate a notebook or Markdown report.

        Tries to import ``nbformat``; if successful creates a ``.ipynb``
        notebook, otherwise falls back to a Markdown ``report.md``.

        Parameters
        ----------
        output_dir:
            Directory where the output file will be written.

        Returns
        -------
        Path
            Absolute path to the generated file.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            import nbformat  # noqa: F401 — availability check only

            output_path = output_dir / "report.ipynb"
            return self.generate_notebook(output_path)
        except ImportError:
            logger.info(
                "NotebookGenerator: nbformat not available — falling back to Markdown"
            )
            return self.generate_markdown(output_dir)

    def generate_notebook(self, output_path: Path) -> Path:
        """Create a self-contained ``.ipynb`` notebook from summary data.

        The notebook has 12 cells covering: session info, composite score
        trend, convergence state, operations breakdown, sensor activation,
        and cost/branch summary.

        Parameters
        ----------
        output_path:
            Full path (including filename) for the output ``.ipynb`` file.

        Returns
        -------
        Path
            Absolute path to the written notebook file.
        """
        import nbformat

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Embed the raw JSON as a string literal so the notebook is self-contained.
        json_literal = json.dumps(self._data, indent=2)
        sid = self._data.get("session_id", "unknown")
        stop_reason = self._data.get("stop_reason", "unknown")
        duration = self._data.get("duration_s", 0.0)
        convergence = self._data.get("convergence", {})
        conv_state = convergence.get("state", "unknown")
        conv_slope = convergence.get("slope", 0.0)
        conv_r2 = convergence.get("r_squared_log", 0.0)

        cells: List[nbformat.NotebookNode] = [
            # ── Cell 1: title + session info ────────────────────────────────
            nbformat.v4.new_markdown_cell(
                f"# Ouroboros Battle Test Report\n\n"
                f"| Field | Value |\n"
                f"|-------|-------|\n"
                f"| Session ID | `{sid}` |\n"
                f"| Stop Reason | `{stop_reason}` |\n"
                f"| Duration | {duration:.1f} s |\n"
            ),
            # ── Cell 2: load summary data, extract scores ────────────────────
            nbformat.v4.new_code_cell(
                "import json\n"
                "import math\n"
                "\n"
                "# Summary data embedded directly — notebook is self-contained\n"
                "_SUMMARY_JSON = '''\n"
                f"{json_literal}\n"
                "'''\n"
                "\n"
                "data = json.loads(_SUMMARY_JSON)\n"
                "\n"
                "# Extract composite scores from operation_log\n"
                "scores = [\n"
                "    op['composite_score']\n"
                "    for op in data.get('operation_log', [])\n"
                "    if op.get('composite_score') is not None\n"
                "]\n"
                "print(f\"Session: {data['session_id']}\")\n"
                "print(f\"Scores extracted: {len(scores)}\")\n"
                "print(f\"Scores: {scores}\")\n"
            ),
            # ── Cell 3: composite score trend header ─────────────────────────
            nbformat.v4.new_markdown_cell(
                "## Composite Score Trend\n\n"
                "Plot of composite scores over operation index with a "
                "logarithmic fit overlay."
            ),
            # ── Cell 4: matplotlib plot of scores with log fit ───────────────
            nbformat.v4.new_code_cell(
                "import matplotlib.pyplot as plt\n"
                "import numpy as np\n"
                "\n"
                "if len(scores) >= 2:\n"
                "    x = np.arange(1, len(scores) + 1)\n"
                "    y = np.array(scores)\n"
                "\n"
                "    fig, ax = plt.subplots(figsize=(10, 5))\n"
                "    ax.scatter(x, y, label='Composite Score', color='steelblue', zorder=3)\n"
                "    ax.plot(x, y, color='steelblue', alpha=0.4)\n"
                "\n"
                "    # Logarithmic fit overlay\n"
                "    try:\n"
                "        log_x = np.log(x)\n"
                "        coeffs = np.polyfit(log_x, y, 1)\n"
                "        fit_y = coeffs[0] * log_x + coeffs[1]\n"
                "        ax.plot(x, fit_y, 'r--', label='Log fit', linewidth=2)\n"
                "    except Exception as _e:\n"
                "        print(f'Log fit failed: {_e}')\n"
                "\n"
                "    ax.set_xlabel('Operation Index')\n"
                "    ax.set_ylabel('Composite Score')\n"
                "    ax.set_title('Composite Score Trend')\n"
                "    ax.legend()\n"
                "    ax.grid(True, alpha=0.3)\n"
                "    plt.tight_layout()\n"
                "    plt.show()\n"
                "else:\n"
                "    print('Not enough scored operations to plot trend.')\n"
            ),
            # ── Cell 5: convergence state header ────────────────────────────
            nbformat.v4.new_markdown_cell(
                "## Convergence State\n\n"
                "Analysis of score convergence based on logarithmic regression."
            ),
            # ── Cell 6: convergence state/slope/r2 with interpretation ───────
            nbformat.v4.new_code_cell(
                "convergence = data.get('convergence', {})\n"
                "state = convergence.get('state', 'unknown')\n"
                "slope = convergence.get('slope', 0.0)\n"
                "r2 = convergence.get('r_squared_log', 0.0)\n"
                "\n"
                "print(f'Convergence State : {state}')\n"
                "print(f'Slope             : {slope:.6f}')\n"
                "print(f'R² (log fit)      : {r2:.4f}')\n"
                "print()\n"
                "\n"
                "# Human-readable interpretation\n"
                "if state == 'improving':\n"
                "    print('Interpretation: The session shows a consistent improvement trend.')\n"
                "elif state == 'converged':\n"
                "    print('Interpretation: Scores have stabilised — further iterations are unlikely to help.')\n"
                "elif state == 'stagnant':\n"
                "    print('Interpretation: No meaningful progress detected; consider changing strategy.')\n"
                "elif state == 'diverging':\n"
                "    print('Interpretation: WARNING — scores are getting worse over time.')\n"
                "else:\n"
                "    print(f'Interpretation: Convergence state \"{state}\" is not recognised.')\n"
            ),
            # ── Cell 7: operations breakdown header ─────────────────────────
            nbformat.v4.new_markdown_cell(
                "## Operations Breakdown\n\n"
                "Pie chart of operation outcomes."
            ),
            # ── Cell 8: pie chart completed/failed/cancelled/queued ──────────
            nbformat.v4.new_code_cell(
                "ops = data.get('operations', {})\n"
                "labels = ['Completed', 'Failed', 'Cancelled', 'Queued']\n"
                "values = [\n"
                "    ops.get('completed', 0),\n"
                "    ops.get('failed', 0),\n"
                "    ops.get('cancelled', 0),\n"
                "    ops.get('queued', 0),\n"
                "]\n"
                "colors = ['#4caf50', '#f44336', '#ff9800', '#2196f3']\n"
                "\n"
                "# Filter out zero-value slices\n"
                "pairs = [(l, v, c) for l, v, c in zip(labels, values, colors) if v > 0]\n"
                "if pairs:\n"
                "    _labels, _values, _colors = zip(*pairs)\n"
                "    fig, ax = plt.subplots(figsize=(6, 6))\n"
                "    ax.pie(_values, labels=_labels, colors=_colors, autopct='%1.1f%%', startangle=140)\n"
                "    ax.set_title('Operations Breakdown')\n"
                "    plt.tight_layout()\n"
                "    plt.show()\n"
                "else:\n"
                "    print('No operation data available.')\n"
            ),
            # ── Cell 9: sensor activation header ────────────────────────────
            nbformat.v4.new_markdown_cell(
                "## Sensor Activation\n\n"
                "Horizontal bar chart of top sensor trigger counts."
            ),
            # ── Cell 10: horizontal bar chart of sensor counts ───────────────
            nbformat.v4.new_code_cell(
                "top_sensors = data.get('top_sensors', [])\n"
                "\n"
                "if top_sensors:\n"
                "    sensor_names = [s[0] for s in top_sensors]\n"
                "    sensor_counts = [s[1] for s in top_sensors]\n"
                "\n"
                "    fig, ax = plt.subplots(figsize=(8, max(3, len(sensor_names) * 0.6)))\n"
                "    bars = ax.barh(sensor_names, sensor_counts, color='#7e57c2')\n"
                "    ax.set_xlabel('Trigger Count')\n"
                "    ax.set_title('Top Sensor Activations')\n"
                "    ax.bar_label(bars, padding=3)\n"
                "    ax.invert_yaxis()\n"
                "    plt.tight_layout()\n"
                "    plt.show()\n"
                "else:\n"
                "    print('No sensor data available.')\n"
            ),
            # ── Cell 11: cost & branch summary header ───────────────────────
            nbformat.v4.new_markdown_cell(
                "## Cost & Branch Summary\n\n"
                "Breakdown of API costs and git branch statistics."
            ),
            # ── Cell 12: cost breakdown and branch stats ─────────────────────
            nbformat.v4.new_code_cell(
                "cost = data.get('cost', {})\n"
                "branch = data.get('branch', {})\n"
                "\n"
                "print('=== Cost Summary ===')\n"
                "print(f\"Total cost : ${cost.get('total', 0.0):.4f}\")\n"
                "print('Breakdown  :')\n"
                "for provider, amount in cost.get('breakdown', {}).items():\n"
                "    print(f'  {provider:<30} ${amount:.4f}')\n"
                "\n"
                "print()\n"
                "print('=== Branch Summary ===')\n"
                "print(f\"Commits       : {branch.get('commits', 0)}\")\n"
                "print(f\"Files changed : {branch.get('files_changed', 0)}\")\n"
                "print(f\"Insertions    : {branch.get('insertions', 0)}\")\n"
                "print(f\"Deletions     : {branch.get('deletions', 0)}\")\n"
            ),
        ]

        nb = nbformat.v4.new_notebook(cells=cells)
        nb.metadata["kernelspec"] = {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        }
        nb.metadata["language_info"] = {
            "name": "python",
            "version": "3.9",
        }

        nbformat.write(nb, str(output_path))
        logger.info("NotebookGenerator: notebook written to %s", output_path)
        return output_path.resolve()

    def generate_markdown(self, output_dir: Path) -> Path:
        """Create a Markdown report from summary data.

        Produces ``report.md`` in *output_dir* with the same information
        as the notebook: session info, convergence, operations, sensors,
        cost, and branch statistics.

        Parameters
        ----------
        output_dir:
            Directory where ``report.md`` will be written.

        Returns
        -------
        Path
            Absolute path to the written Markdown file.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "report.md"

        data = self._data
        sid = data.get("session_id", "unknown")
        stop_reason = data.get("stop_reason", "unknown")
        duration = data.get("duration_s", 0.0)
        convergence = data.get("convergence", {})
        conv_state = convergence.get("state", "unknown")
        conv_slope = convergence.get("slope", 0.0)
        conv_r2 = convergence.get("r_squared_log", 0.0)
        ops = data.get("operations", {})
        cost = data.get("cost", {})
        branch = data.get("branch", {})
        top_sensors = data.get("top_sensors", [])
        top_techniques = data.get("top_techniques", [])
        operation_log = data.get("operation_log", [])

        # Extract composite scores
        scores = [
            op["composite_score"]
            for op in operation_log
            if op.get("composite_score") is not None
        ]

        lines: List[str] = [
            "# Ouroboros Battle Test Report",
            "",
            "## Session Info",
            "",
            "| Field | Value |",
            "|-------|-------|",
            f"| Session ID | `{sid}` |",
            f"| Stop Reason | `{stop_reason}` |",
            f"| Duration | {duration:.1f} s |",
            "",
            "## Composite Score Trend",
            "",
        ]

        if scores:
            lines.append(f"Composite scores over {len(scores)} scored operations:\n")
            lines.append("| Index | Score |")
            lines.append("|-------|-------|")
            for i, s in enumerate(scores, 1):
                lines.append(f"| {i} | {s:.4f} |")
        else:
            lines.append("_No scored operations in this session._")

        lines += [
            "",
            "## Convergence State",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| State | **{conv_state}** |",
            f"| Slope | {conv_slope:.6f} |",
            f"| R² (log fit) | {conv_r2:.4f} |",
            "",
        ]

        # Human-readable interpretation
        interpretations = {
            "improving": "The session shows a consistent improvement trend.",
            "converged": "Scores have stabilised — further iterations are unlikely to help.",
            "stagnant": "No meaningful progress detected; consider changing strategy.",
            "diverging": "WARNING — scores are getting worse over time.",
        }
        interp = interpretations.get(conv_state, f'Convergence state "{conv_state}" is not recognised.')
        lines.append(f"**Interpretation:** {interp}")
        lines.append("")

        lines += [
            "## Operations Breakdown",
            "",
            "| Outcome | Count |",
            "|---------|-------|",
            f"| Attempted | {ops.get('attempted', 0)} |",
            f"| Completed | {ops.get('completed', 0)} |",
            f"| Failed | {ops.get('failed', 0)} |",
            f"| Cancelled | {ops.get('cancelled', 0)} |",
            f"| Queued | {ops.get('queued', 0)} |",
            "",
            "## Sensor Activation",
            "",
        ]

        if top_sensors:
            lines.append("| Sensor | Count |")
            lines.append("|--------|-------|")
            for name, count in top_sensors:
                lines.append(f"| {name} | {count} |")
        else:
            lines.append("_No sensor data available._")

        lines += [
            "",
            "## Top Techniques",
            "",
        ]

        if top_techniques:
            lines.append("| Technique | Count |")
            lines.append("|-----------|-------|")
            for name, count in top_techniques:
                lines.append(f"| {name} | {count} |")
        else:
            lines.append("_No technique data available._")

        lines += [
            "",
            "## Cost Summary",
            "",
            f"**Total cost:** ${cost.get('total', 0.0):.4f}",
            "",
            "| Provider | Cost (USD) |",
            "|----------|------------|",
        ]

        for provider, amount in cost.get("breakdown", {}).items():
            lines.append(f"| {provider} | ${amount:.4f} |")

        lines += [
            "",
            "## Branch Summary",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Commits | {branch.get('commits', 0)} |",
            f"| Files Changed | {branch.get('files_changed', 0)} |",
            f"| Insertions | {branch.get('insertions', 0)} |",
            f"| Deletions | {branch.get('deletions', 0)} |",
            "",
        ]

        output_path.write_text("\n".join(lines))
        logger.info("NotebookGenerator: markdown report written to %s", output_path)
        return output_path.resolve()

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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


def _atomic_write(path: "Path", render: "Callable[[Path], None]") -> "Path":
    """Write via a temp sibling, fsync, then `os.replace`. NEVER leaves a partial.

    This runs during SHUTDOWN, which is exactly when the process is least likely to
    be allowed to finish: a SIGTERM from the harness's own deadline, a preemption on
    a volatile cloud node, an operator's second Ctrl-C. A plain write interrupted
    mid-stream leaves a truncated `.ipynb` — and a corrupt notebook is worse than an
    absent one, because it looks like an artifact and fails only when someone tries
    to open it months later.

    Three steps, none of them optional:

      * a TEMP SIBLING in the same directory, so the final `replace` is a rename
        within one filesystem and therefore atomic. A temp file in `/tmp` would make
        it a cross-device copy, which is not.
      * `flush()` then `os.fsync()`, because a rename can otherwise be committed
        while the DATA is still in the page cache — the metadata lands, the bytes do
        not, and the file reads as zeros after a hard kill.
      * `os.replace()`, which is atomic on POSIX and Windows alike: a reader either
        sees the old file or the complete new one, never a half-written notebook.

    The temp file is removed on ANY failure, so a crashed run does not leave
    `.report.ipynb.tmp` litter behind for the next session to wonder about.
    """
    import os

    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        render(tmp)
        # `render` closed its own handle; reopen to force the bytes to disk. The
        # fsync has to happen on the DATA file, before the rename, or the ordering
        # guarantee this whole function exists for is not there.
        with open(tmp, "rb+") as fh:
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return path
    except BaseException:
        # BaseException, not Exception, and that distinction is the whole point.
        # The interruption this function exists to survive is a SIGTERM or a
        # Ctrl-C, which arrive as `KeyboardInterrupt` / `SystemExit` — neither of
        # which is an `Exception`. Catching the narrower type left the half-written
        # `.report.ipynb.tmp` on disk in precisely the scenario the atomicity was
        # for, and a test that simulated a mid-write SIGTERM is what found it.
        #
        # Re-raised unchanged below: this cleans up, it does not decide that a
        # shutdown signal should be ignored.
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:  # noqa: BLE001
            pass
        raise


# ---------------------------------------------------------------------------
# Schema adaptation
# ---------------------------------------------------------------------------

_OP_COUNT_KEYS: Tuple[str, ...] = ("attempted", "completed", "failed", "cancelled", "queued")
_BRANCH_KEYS: Tuple[str, ...] = ("commits", "files_changed", "insertions", "deletions")

# Statuses a recorded operation can carry that map 1:1 onto a counter above.
# ``attempted`` is deliberately absent: it is the total, not a status.
_COUNTABLE_STATUSES: Tuple[str, ...] = ("completed", "failed", "cancelled", "queued")


def _as_float(value: Any, default: float = 0.0) -> float:
    """Coerce a summary field to a float that a format spec can consume.

    Every numeric in the view model is rendered through ``:.4f`` / ``:.1f``.
    A ``None`` from a partial (signal-killed) summary would raise there, which
    is precisely when the report matters most — so absence degrades to the
    default rather than to a second traceback on the shutdown path.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(result) or math.isinf(result) else result


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce a counter field to an int; see :func:`_as_float` for the why."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_pairs(value: Any) -> List[Tuple[str, int]]:
    """Normalise a top-N ranking to ``[(name, count), ...]``.

    ``SessionRecorder.top_sensors()`` returns tuples, which survive a JSON
    round-trip as two-element lists. Both are accepted; anything that is not a
    usable pair is dropped rather than crashing the render.
    """
    pairs: List[Tuple[str, int]] = []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return pairs
    for entry in value:
        if isinstance(entry, Mapping):
            name, count = entry.get("name"), entry.get("count")
        elif isinstance(entry, Sequence) and not isinstance(entry, (str, bytes)) and len(entry) >= 2:
            name, count = entry[0], entry[1]
        else:
            continue
        if name is None:
            continue
        pairs.append((str(name), _as_int(count)))
    return pairs


def normalise_summary(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Adapt any battle-test summary generation to ONE stable report view.

    This exists because the report renderers and the summary writer drifted
    into two different schemas and nothing connected them.
    :meth:`SessionRecorder.write_summary` (``schema_version`` 2) writes the
    outcome counters under ``stats``, the per-operation records under
    ``operations``, and flattens the rest into ``convergence_state`` /
    ``convergence_slope`` / ``convergence_r2``, ``cost_total`` /
    ``cost_breakdown`` and ``branch_stats``. The renderers read a nested shape
    — ``operations`` as a counter dict, the records under ``operation_log``,
    and ``convergence`` / ``cost`` / ``branch`` as sub-objects — that the
    recorder has never emitted. The counter read (``operations.get``) raised
    ``AttributeError`` against a list; every *other* mismatched read failed
    silently, rendering a well-formed report full of zeroes and "unknown".

    Both shapes are read here, at one seam, so that (a) a report can be
    rendered from any artifact already on disk, and (b) neither renderer has
    to know which generation it is holding. The discriminator is the type of
    ``operations`` — a list is the recorder's schema, a mapping is the nested
    one — never a version number, because partial summaries written from a
    signal handler are not guaranteed to carry one.

    Returns a dict with fixed keys and fully-typed values: ``session_id``,
    ``stop_reason``, ``duration_s``, ``convergence`` (state/slope/
    r_squared_log), ``op_counts`` (the five counters), ``operation_log``,
    ``scores``, ``cost`` (total/breakdown), ``branch``, ``top_sensors`` and
    ``top_techniques``.
    """
    operations = data.get("operations")
    operation_log = data.get("operation_log")

    if isinstance(operations, list):
        # Recorder schema: ``operations`` IS the per-op log, counters in ``stats``.
        records: List[Any] = operations
        counters: Any = data.get("stats")
    else:
        records = operation_log if isinstance(operation_log, list) else []
        # Nested schema: ``operations`` IS the counter mapping. ``stats`` is
        # still consulted so a hybrid artifact is not silently zeroed.
        counters = operations if isinstance(operations, Mapping) else data.get("stats")

    records = [rec for rec in records if isinstance(rec, Mapping)]

    if not isinstance(counters, Mapping):
        counters = {}
    op_counts = {key: _as_int(counters.get(key)) for key in _OP_COUNT_KEYS}

    # A summary that carries records but no counters still deserves a real
    # table — derive it from the records rather than printing five zeroes.
    if records and not any(op_counts.values()):
        op_counts["attempted"] = len(records)
        for rec in records:
            status = str(rec.get("status", "")).strip().lower()
            if status in _COUNTABLE_STATUSES:
                op_counts[status] += 1

    convergence = data.get("convergence")
    if not isinstance(convergence, Mapping):
        convergence = {
            "state": data.get("convergence_state", "unknown"),
            "slope": data.get("convergence_slope", 0.0),
            "r_squared_log": data.get("convergence_r2", 0.0),
        }
    convergence_view = {
        "state": str(convergence.get("state", "unknown") or "unknown"),
        "slope": _as_float(convergence.get("slope")),
        "r_squared_log": _as_float(convergence.get("r_squared_log")),
    }

    cost = data.get("cost")
    if not isinstance(cost, Mapping):
        cost = {
            "total": data.get("cost_total", 0.0),
            "breakdown": data.get("cost_breakdown", {}),
        }
    breakdown = cost.get("breakdown")
    cost_view = {
        "total": _as_float(cost.get("total")),
        "breakdown": (
            {str(k): _as_float(v) for k, v in breakdown.items()}
            if isinstance(breakdown, Mapping)
            else {}
        ),
    }

    branch = data.get("branch")
    if not isinstance(branch, Mapping):
        branch = data.get("branch_stats")
    if not isinstance(branch, Mapping):
        branch = {}
    branch_view = {key: _as_int(branch.get(key)) for key in _BRANCH_KEYS}

    scores = [
        _as_float(rec.get("composite_score"))
        for rec in records
        if rec.get("composite_score") is not None
    ]

    return {
        "session_id": str(data.get("session_id", "unknown") or "unknown"),
        "stop_reason": str(data.get("stop_reason", "unknown") or "unknown"),
        "duration_s": _as_float(data.get("duration_s")),
        "convergence": convergence_view,
        "op_counts": op_counts,
        "operation_log": records,
        "scores": scores,
        "cost": cost_view,
        "branch": branch_view,
        "top_sensors": _as_pairs(data.get("top_sensors")),
        "top_techniques": _as_pairs(data.get("top_techniques")),
    }


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
        # Both renderers read the view, never ``_data`` directly, so the
        # schema question is answered exactly once per generator instance.
        # ``_data`` is retained verbatim for provenance — it is what gets
        # embedded in the notebook so the artifact stays self-contained.
        self._view: Dict[str, Any] = normalise_summary(self._data)

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

        # Embed BOTH: the raw summary for provenance (so the notebook remains
        # a faithful copy of the artifact) and the normalised view the cells
        # actually compute from. Cells that read the raw shape directly are
        # how this renderer silently drifted from the recorder's schema; the
        # view is the single place that question is answered.
        json_literal = json.dumps(self._data, indent=2)
        view_literal = json.dumps(self._view, indent=2)
        view = self._view
        sid = view["session_id"]
        stop_reason = view["stop_reason"]
        duration = view["duration_s"]
        convergence = view["convergence"]
        conv_state = convergence["state"]
        conv_slope = convergence["slope"]
        conv_r2 = convergence["r_squared_log"]

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
                "# Summary data embedded directly — notebook is self-contained.\n"
                "# `data` is the raw artifact (provenance); `view` is the\n"
                "# schema-normalised projection every cell below reads.\n"
                "_SUMMARY_JSON = '''\n"
                f"{json_literal}\n"
                "'''\n"
                "\n"
                "_VIEW_JSON = '''\n"
                f"{view_literal}\n"
                "'''\n"
                "\n"
                "data = json.loads(_SUMMARY_JSON)\n"
                "view = json.loads(_VIEW_JSON)\n"
                "\n"
                "scores = view['scores']\n"
                "print(f\"Session: {view['session_id']}\")\n"
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
                "convergence = view['convergence']\n"
                "state = convergence['state']\n"
                "slope = convergence['slope']\n"
                "r2 = convergence['r_squared_log']\n"
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
                "ops = view['op_counts']\n"
                "labels = ['Completed', 'Failed', 'Cancelled', 'Queued']\n"
                "values = [\n"
                "    ops['completed'],\n"
                "    ops['failed'],\n"
                "    ops['cancelled'],\n"
                "    ops['queued'],\n"
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
                "top_sensors = view['top_sensors']\n"
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
                "cost = view['cost']\n"
                "branch = view['branch']\n"
                "\n"
                "print('=== Cost Summary ===')\n"
                "print(f\"Total cost : ${cost['total']:.4f}\")\n"
                "print('Breakdown  :')\n"
                "if cost['breakdown']:\n"
                "    for provider, amount in cost['breakdown'].items():\n"
                "        print(f'  {provider:<30} ${amount:.4f}')\n"
                "else:\n"
                "    print('  (no billed providers)')\n"
                "\n"
                "print()\n"
                "print('=== Branch Summary ===')\n"
                "print(f\"Commits       : {branch['commits']}\")\n"
                "print(f\"Files changed : {branch['files_changed']}\")\n"
                "print(f\"Insertions    : {branch['insertions']}\")\n"
                "print(f\"Deletions     : {branch['deletions']}\")\n"
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

        _atomic_write(output_path,
                      lambda tmp: nbformat.write(nb, str(tmp)))
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

        view = self._view
        sid = view["session_id"]
        stop_reason = view["stop_reason"]
        duration = view["duration_s"]
        convergence = view["convergence"]
        conv_state = convergence["state"]
        conv_slope = convergence["slope"]
        conv_r2 = convergence["r_squared_log"]
        ops = view["op_counts"]
        cost = view["cost"]
        branch = view["branch"]
        top_sensors = view["top_sensors"]
        top_techniques = view["top_techniques"]
        scores = view["scores"]

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
            f"| Attempted | {ops['attempted']} |",
            f"| Completed | {ops['completed']} |",
            f"| Failed | {ops['failed']} |",
            f"| Cancelled | {ops['cancelled']} |",
            f"| Queued | {ops['queued']} |",
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
            f"**Total cost:** ${cost['total']:.4f}",
            "",
            "| Provider | Cost (USD) |",
            "|----------|------------|",
        ]

        if cost["breakdown"]:
            for provider, amount in cost["breakdown"].items():
                lines.append(f"| {provider} | ${amount:.4f} |")
        else:
            # A zero-cost session is the *point* of the local lane, not a gap
            # in the data — say so rather than leaving an empty table body.
            lines.append("| _no billed providers_ | $0.0000 |")

        lines += [
            "",
            "## Branch Summary",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Commits | {branch['commits']} |",
            f"| Files Changed | {branch['files_changed']} |",
            f"| Insertions | {branch['insertions']} |",
            f"| Deletions | {branch['deletions']} |",
            "",
        ]

        _atomic_write(
            output_path,
            lambda tmp: tmp.write_text("\n".join(lines)),
        )
        logger.info("NotebookGenerator: markdown report written to %s", output_path)
        return output_path.resolve()

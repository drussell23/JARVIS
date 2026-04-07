"""Ouroboros Pytest Plugin — Phase 2 Event Spine Integration.

Writes structured test results to ``.jarvis/test_results.json`` on session
finish.  The FileSystemEventBridge (Phase 1) detects this file change and
publishes ``fs.changed.modified`` to TrinityEventBus, where
TestFailureSensor consumes it for streak-based failure detection.

This plugin has **zero** runtime dependencies on the Ouroboros stack — it
is a pure pytest plugin that writes a JSON file.  The async integration
happens entirely on the consumer side.

Disable by setting env var::

    OUROBOROS_PYTEST_PLUGIN_DISABLED=1

Manifesto §7 (Absolute Observability): test results are structured data,
not regex-parsed stdout.  The skeleton (deterministic JSON schema) does
not think; the nervous system (sensor) interprets.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_RESULTS_FILENAME = "test_results.json"


# ---------------------------------------------------------------------------
# Result collector (session-scoped, stashed on config)
# ---------------------------------------------------------------------------


class _ResultCollector:
    """Accumulates per-test results during a pytest session."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.session_id = str(uuid.uuid4())[:12]
        self.session_start = time.time()
        self._results: List[Dict[str, Any]] = []
        self._failures: List[Dict[str, Any]] = []
        self._passed_nodeids: List[str] = []
        self._error_count = 0

    def add_report(self, report: pytest.TestReport) -> None:
        """Record one test phase result."""
        if report.when == "call":
            entry = {
                "nodeid": report.nodeid,
                "outcome": report.outcome,  # "passed", "failed", "skipped"
                "duration_s": report.duration,
            }
            self._results.append(entry)

            if report.outcome == "passed":
                self._passed_nodeids.append(report.nodeid)
            elif report.outcome == "failed":
                # Extract file path from nodeid (e.g. "tests/test_x.py::test_y" → "tests/test_x.py")
                file_path = report.nodeid.split("::")[0]
                error_text = ""
                if report.longrepr:
                    # longrepr can be a string or ReprExceptionInfo
                    error_text = str(report.longrepr).split("\n")[-1][:500]
                self._failures.append({
                    "nodeid": report.nodeid,
                    "file_path": file_path,
                    "error_text": error_text,
                    "duration_s": report.duration,
                })

        elif report.when in ("setup", "teardown") and report.failed:
            # Fixture/setup failures also count
            file_path = report.nodeid.split("::")[0]
            error_text = str(report.longrepr).split("\n")[-1][:500] if report.longrepr else ""
            self._failures.append({
                "nodeid": report.nodeid,
                "file_path": file_path,
                "error_text": f"[{report.when}] {error_text}",
                "duration_s": report.duration,
            })
            self._error_count += 1

    def write_results(self, exit_status: int) -> None:
        """Atomically write results to .jarvis/test_results.json."""
        passed = len(self._passed_nodeids)
        failed = len(self._failures)

        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "session_id": self.session_id,
            "timestamp": time.time(),
            "duration_s": round(time.time() - self.session_start, 2),
            "exit_status": exit_status,
            "summary": {
                "passed": passed,
                "failed": failed,
                "error": self._error_count,
                "total": len(self._results),
            },
            "failures": self._failures,
            "passed_nodeids": self._passed_nodeids,
        }

        output_path = self.output_dir / _RESULTS_FILENAME

        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)

            # Atomic write: temp file + os.replace
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.output_dir), suffix=".tmp", prefix=".test_results_",
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(manifest, f, indent=2)
                os.replace(tmp_path, str(output_path))
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            logger.info(
                "ouroboros_pytest_plugin: wrote %s "
                "(passed=%d, failed=%d, total=%d)",
                output_path, passed, failed, len(self._results),
            )
        except Exception as exc:
            # Never fail the test session because of a write error
            logger.warning(
                "ouroboros_pytest_plugin: failed to write results: %s", exc,
            )


# ---------------------------------------------------------------------------
# Pytest hooks
# ---------------------------------------------------------------------------

_ATTR = "_ouroboros_collector"


def pytest_configure(config: pytest.Config) -> None:
    """Register the result collector unless disabled."""
    if os.environ.get("OUROBOROS_PYTEST_PLUGIN_DISABLED", "").strip() in ("1", "true", "yes"):
        return

    # xdist worker nodes should not write results
    if hasattr(config, "workerinput"):
        return

    output_dir = Path(getattr(config, "rootpath", Path.cwd())) / ".jarvis"
    collector = _ResultCollector(output_dir=output_dir)
    setattr(config, _ATTR, collector)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo,  # type: ignore[type-arg]
) -> Any:
    """Capture each test phase result via hookwrapper."""
    outcome = yield
    report: pytest.TestReport = outcome.get_result()

    collector: Optional[_ResultCollector] = getattr(item.config, _ATTR, None)
    if collector is not None:
        collector.add_report(report)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(
    session: pytest.Session, exitstatus: int,
) -> None:
    """Write the results file at the end of the session."""
    collector: Optional[_ResultCollector] = getattr(session.config, _ATTR, None)
    if collector is not None:
        collector.write_results(exit_status=exitstatus)

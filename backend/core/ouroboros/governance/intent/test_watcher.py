"""
Test Watcher — Pytest Polling & Stable Failure Detection
=========================================================

Watches the test suite by periodically invoking ``pytest`` in a subprocess,
parsing the output for ``FAILED`` lines, and detecting **stable failures**
(i.e. tests that fail in two or more consecutive polling runs).

Stable failures are emitted as :class:`IntentSignal` instances with
``source="intent:test_failure"`` and ``stable=True``, ready for downstream
classification and governance.

Key design decisions:

* **Subprocess isolation** -- pytest runs via ``asyncio.create_subprocess_exec``
  so it can't crash the host event loop and is trivially timeout-able.
* **Streak-based stability** -- a single transient failure doesn't trigger
  an autonomous fix; the test must fail in *two* consecutive polls.
* **Confidence escalation** -- confidence grows with the streak length,
  capping at 0.95 to leave room for human override.
* **Environment-driven defaults** -- poll interval and repo path are read
  from env vars so operators can tune without code changes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .signals import IntentSignal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex for parsing pytest FAILED lines (--tb=short -q --no-header format)
# ---------------------------------------------------------------------------
_FAILED_RE = re.compile(
    r"^FAILED\s+(\S+)(?:\s+-\s+(.+))?$", re.MULTILINE,
)


# ---------------------------------------------------------------------------
# TestFailure dataclass
# ---------------------------------------------------------------------------


@dataclass
class TestFailure:
    """A single parsed test failure from pytest output.

    Parameters
    ----------
    test_id:
        Fully qualified test identifier, e.g.
        ``"tests/test_utils.py::test_edge_case"``.
    file_path:
        The file portion of the test id, e.g. ``"tests/test_utils.py"``.
    error_text:
        The error summary text captured from the FAILED line.
    traceback_evidence:
        Repair Context Bridge (Slice 1) enrichment — additive evidence merged
        into the emitted signal: ``{traceback_frames, fault_node_keys}`` mapping
        the failing call stack to Oracle node keys. ``None`` until the bridge
        enriches it (gated ``JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED``); ``None``
        leaves the signal byte-identical to pre-bridge behavior.
    """

    test_id: str
    file_path: str
    error_text: str
    traceback_evidence: Optional[Dict[str, Any]] = field(default=None)


# ---------------------------------------------------------------------------
# TestWatcher
# ---------------------------------------------------------------------------


class TestWatcher:
    """Polls pytest, detects stable failures, emits IntentSignals.

    Parameters
    ----------
    repo:
        Repository label used in emitted signals (e.g. ``"jarvis"``).
    test_dir:
        Relative path to the test directory within the repo (default
        ``"tests/"``).
    repo_path:
        Absolute path to the repository root.  Falls back to env var
        ``JARVIS_REPO_PATH`` or ``"."`` if not provided.
    poll_interval_s:
        Seconds between polling runs.  Falls back to env var
        ``JARVIS_INTENT_TEST_INTERVAL_S`` or ``300`` if not provided.
    pytest_timeout_s:
        Maximum seconds to wait for a single pytest invocation before
        killing the subprocess.
    """

    def __init__(
        self,
        repo: str,
        test_dir: str = "tests/",
        repo_path: Optional[str] = None,
        poll_interval_s: Optional[float] = None,
        pytest_timeout_s: float = 30.0,
        node_resolver: Optional[Any] = None,
    ) -> None:
        self.repo = repo
        self.test_dir = os.environ.get("JARVIS_INTENT_TEST_DIR", test_dir)
        self.repo_path = repo_path or os.environ.get("JARVIS_REPO_PATH", ".")
        # Repair Context Bridge (Slice 1): injectable Oracle line->node resolver.
        # ``None`` → lazily resolved from the global Oracle backend at enrich time
        # (fail-soft). Tests inject a fake. Only consulted when the bridge is on.
        self._node_resolver = node_resolver
        self.poll_interval_s = (
            poll_interval_s
            if poll_interval_s is not None
            else float(os.environ.get("JARVIS_INTENT_TEST_INTERVAL_S", "300"))
        )
        # Env-overridable (Sovereign Exec Engine, 2026-06-19): high-compute
        # hosts raise this past the 30s default so a full-repo pytest sweep
        # completes instead of being SIGKILLed mid-run (the local M1 foil that
        # kept an isolated seed defect from ever being detected).
        try:
            self.pytest_timeout_s = float(
                os.environ.get(
                    "JARVIS_INTENT_PYTEST_TIMEOUT_S", str(pytest_timeout_s),
                )
            )
        except (TypeError, ValueError):
            self.pytest_timeout_s = pytest_timeout_s

        # Streak tracking: test_id -> consecutive failure count
        self._failure_streak: Dict[str, int] = {}
        # Track which test_ids failed in the *current* run for reset logic
        self._last_failed_ids: Set[str] = set()

        self._running = False

    # ------------------------------------------------------------------
    # Subprocess invocation
    # ------------------------------------------------------------------

    async def run_pytest(self) -> Tuple[str, int]:
        """Run pytest via the Slice 9 canonical helper.

        Slice 9 (operator-bound, empirical from bt-2026-05-22-000838):
        Slice 8's per-site ``stdin=DEVNULL`` patch was correct but
        narrow — the live runtime has 9 pytest spawn paths and
        Slice 8 only covered 2. The remaining sites kept the
        post-Slice-7-proof event-loop starvation alive (PIDs 3554 +
        3558 at STAT=SN for 9+ minutes). Slice 9 routes EVERY
        pytest invocation through ``run_pytest_subprocess`` —
        single source of pipe-discipline + provenance + process-
        group cleanup. AST pin in
        ``test_slice9_canonical_pytest_helper.py`` forbids any
        other path."""
        from backend.core.ouroboros.governance.test_subprocess_helper import (  # noqa: E501
            run_pytest_subprocess,
        )
        argv = [
            "python3",
            "-m",
            "pytest",
            str(self.test_dir),
            "--tb=short",
            "-q",
            "--no-header",
            "--color=no",
        ]
        result = await run_pytest_subprocess(
            argv,
            cwd=str(self.repo_path),
            timeout_s=float(self.pytest_timeout_s),
            caller="intent.test_watcher.TestWatcher.run_pytest",
        )
        if result.timed_out:
            logger.warning(
                "pytest timed out after %.1fs — killed via "
                "PytestHelper (kill_reason=%s)",
                self.pytest_timeout_s,
                result.kill_reason.value,
            )
            return "", -1
        return result.stdout, result.returncode

    # ------------------------------------------------------------------
    # Output parsing
    # ------------------------------------------------------------------

    def parse_pytest_output(
        self, output: str, exit_code: int
    ) -> List[TestFailure]:
        """Parse pytest output for FAILED lines.

        Parameters
        ----------
        output:
            Raw stdout from pytest.
        exit_code:
            pytest exit code.  If 0 (all tests passed), an empty list is
            returned regardless of output content.

        Returns
        -------
        List of :class:`TestFailure` instances, one per FAILED line.
        """
        if exit_code == 0:
            return []

        failures: List[TestFailure] = []
        for match in _FAILED_RE.finditer(output):
            test_id = match.group(1)
            error_text = match.group(2) or ""
            file_path = self.extract_file(test_id)
            failures.append(
                TestFailure(
                    test_id=test_id,
                    file_path=file_path,
                    error_text=error_text,
                )
            )
        return failures

    # ------------------------------------------------------------------
    # Stability tracking
    # ------------------------------------------------------------------

    def process_failures(
        self, failures: List[TestFailure]
    ) -> List[IntentSignal]:
        """Track consecutive failure streaks and emit stable signals.

        A test is considered **stable** when it has failed in at least two
        consecutive polling runs.  Passing tests (absent from *failures*)
        have their streak reset to zero.

        Parameters
        ----------
        failures:
            Failures from the current polling run (output of
            :meth:`parse_pytest_output`).

        Returns
        -------
        List of :class:`IntentSignal` for newly stable failures.
        """
        current_failed_ids = {f.test_id for f in failures}

        # Reset streaks for tests that passed (were failing before, now absent)
        for prev_id in list(self._failure_streak.keys()):
            if prev_id not in current_failed_ids:
                del self._failure_streak[prev_id]

        # Update streaks for current failures
        signals: List[IntentSignal] = []
        for f in failures:
            streak = self._failure_streak.get(f.test_id, 0) + 1
            self._failure_streak[f.test_id] = streak

            # Stable = at least 2 consecutive failures
            if streak >= 2:
                confidence = min(0.95, 0.7 + 0.1 * streak)
                evidence: Dict[str, Any] = {
                    "signature": f"{f.error_text}:{f.file_path}",
                    "test_id": f.test_id,
                    "streak": streak,
                    "error_text": f.error_text,
                }
                # Repair Context Bridge (Slice 1): additive traceback enrichment.
                # Only present when the bridge enriched this failure; merging
                # ``None``/empty leaves the evidence byte-identical to before.
                if f.traceback_evidence:
                    evidence.update(f.traceback_evidence)
                signal = IntentSignal(
                    source="intent:test_failure",
                    target_files=(f.file_path,),
                    repo=self.repo,
                    description=(
                        f"Stable test failure: {f.test_id} "
                        f"(streak={streak}): {f.error_text}"
                    ),
                    evidence=evidence,
                    confidence=confidence,
                    stable=True,
                )
                signals.append(signal)

        self._last_failed_ids = current_failed_ids
        return signals

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def extract_file(test_id: str) -> str:
        """Extract the file path from a fully qualified test ID.

        Splits on ``"::"`` and returns the first component.

        Examples
        --------
        >>> TestWatcher.extract_file("tests/test_utils.py::test_edge_case")
        'tests/test_utils.py'
        >>> TestWatcher.extract_file("tests/test_utils.py::TestClass::test_method")
        'tests/test_utils.py'
        """
        return test_id.split("::")[0]

    # ------------------------------------------------------------------
    # Repair Context Bridge (Slice 1) — deep semantic failure ingestion
    # ------------------------------------------------------------------

    def _get_node_resolver(self) -> Optional[Any]:
        """Return the Oracle line->node resolver (``nodes_in_file`` + ``get_node``).

        Uses the injected resolver if provided (tests); otherwise lazily reaches
        the global Oracle's graph backend. Fail-soft: any error → ``None``, which
        degrades enrichment to frame-only (no node mapping), never raising."""
        if self._node_resolver is not None:
            return self._node_resolver
        try:
            from backend.core.ouroboros.oracle import get_oracle

            backend = getattr(get_oracle()._graph, "_backend", None)
            if backend is not None and hasattr(backend, "nodes_in_file"):
                self._node_resolver = backend
                return backend
        except Exception as exc:  # noqa: BLE001 — resolver is best-effort
            logger.debug("[RepairBridge] node resolver unavailable: %s", exc)
        return None

    async def _enrich_failures(
        self, failures: List[TestFailure], output: str
    ) -> None:
        """AST-map each failure's traceback to Oracle node keys (in place).

        Gated by ``JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED`` (default OFF). The
        parse + sqlite line->node mapping is offloaded to a worker thread
        (``asyncio.to_thread``) so the sensor's primary poll loop never blocks on
        graph I/O. Fail-soft: any error leaves ``traceback_evidence`` as ``None``
        and the signal degrades to today's graph-blind behavior."""
        try:
            from .repair_traceback import bridge_enabled, build_traceback_map
        except Exception:  # noqa: BLE001 — module import must never break the sensor
            return
        if not bridge_enabled() or not failures or not output:
            return
        resolver = self._get_node_resolver()
        repo_roots = [str(self.repo_path)]

        def _map_all() -> None:
            for f in failures:
                tb = build_traceback_map(output, f.test_id, repo_roots, resolver)
                if tb.frames:
                    f.traceback_evidence = tb.to_evidence()

        try:
            await asyncio.to_thread(_map_all)
        except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
            logger.debug("[RepairBridge] failure enrichment failed: %s", exc)

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def poll_once(self) -> List[IntentSignal]:
        """Run one poll cycle: invoke pytest, parse output, process failures.

        If pytest times out (exit_code == -1), the cycle is skipped and
        existing failure streaks are preserved -- a timeout does NOT mean
        tests are passing.

        Returns
        -------
        List of :class:`IntentSignal` emitted for stable failures.
        """
        output, exit_code = await self.run_pytest()
        if exit_code == -1:
            return []  # timeout -- skip cycle, preserve streaks
        failures = self.parse_pytest_output(output, exit_code)
        # Repair Context Bridge (Slice 1): non-blocking AST traceback enrichment
        # (gated, fail-soft) — populates f.traceback_evidence before signal build.
        await self._enrich_failures(failures, output)
        return self.process_failures(failures)

    async def start(self) -> None:
        """Long-running poll loop.

        Sets ``self._running = True`` and repeatedly calls :meth:`poll_once`
        followed by an async sleep.  Exits cleanly on ``CancelledError``.
        """
        self._running = True
        logger.info(
            "TestWatcher started: repo=%s test_dir=%s interval=%.1fs",
            self.repo,
            self.test_dir,
            self.poll_interval_s,
        )
        try:
            while self._running:
                signals = await self.poll_once()
                if signals:
                    logger.info(
                        "TestWatcher emitted %d stable failure signal(s)",
                        len(signals),
                    )
                    for sig in signals:
                        logger.debug("  signal: %s", sig.description)
                await asyncio.sleep(self.poll_interval_s)
        except asyncio.CancelledError:
            logger.info("TestWatcher cancelled — shutting down")

    def stop(self) -> None:
        """Request the poll loop to stop after the current iteration."""
        self._running = False

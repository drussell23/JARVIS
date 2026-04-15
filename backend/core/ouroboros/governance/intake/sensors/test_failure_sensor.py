"""
TestFailureSensor (Sensor B) — Adapter over existing TestWatcher.

Converts stable IntentSignal(source='intent:test_failure') objects into
IntentEnvelope(source='test_failure') objects and ingests them via the router.

Phase 2 Event Spine: also consumes ``.jarvis/test_results.json`` written by
the ouroboros_pytest_plugin, providing structured test results without
spawning a subprocess.

The existing TestWatcher (intent/test_watcher.py) handles pytest polling and
streak-based stability detection. This sensor wraps it as an adapter.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.intent.signals import IntentSignal
from backend.core.ouroboros.governance.intent.test_watcher import TestFailure
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-flight dedup (bt-2026-04-15-010727 findings)
# ---------------------------------------------------------------------------
#
# TestFailureSensor polls every ``poll_interval_s`` (default 30s via
# JARVIS_INTENT_TEST_INTERVAL_S). When an op for a broken test file is
# already in flight, subsequent polls at t+30s / t+60s / … continue to
# observe the same broken test (the in-flight op hasn't APPLIED yet) and
# re-emit signals. Each re-emission is accepted by the router because:
#
#   (a) The router's ``register_active_op`` hook — which would populate
#       ``_active_file_ops`` and trigger ``_find_file_conflict`` → queued_behind
#       — is defined but NEVER called from any caller. Dead code as of this
#       fix. That path would require wiring in GLS and an orchestration
#       ordering guarantee (register before the next ingest arrives), which
#       has its own race window.
#   (b) GLS's *separate* ``_active_file_ops`` set (line 966 in
#       governed_loop_service.py) IS populated at dispatch time and rejects
#       duplicates with ``reason_code="file_in_flight"`` — but only *after*
#       the router has already accepted the envelope, burned a WAL entry,
#       created an op_id, and handed it to GLS. In v5 the test_failure
#       concurrency storm (3 ops × same file × 88s under 85s Claude first-
#       token) bypassed GLS's check entirely, probably because the three
#       workers raced past the check window.
#
# Sensor-side dedup is the narrow, race-free fix: reject the re-emission at
# the earliest possible point (before even calling ``router.ingest``) using
# an in-process dict keyed by target_file. TTL-based cleanup means a stuck
# op eventually releases the slot automatically — we don't need a completion
# callback from the orchestrator.
#
# Env gate: ``JARVIS_TEST_FAILURE_INFLIGHT_TTL_S`` (default 300s). Set to 0
# or negative to disable the dedup entirely.

_INFLIGHT_TTL_S: float = float(
    os.environ.get("JARVIS_TEST_FAILURE_INFLIGHT_TTL_S", "300")
)


class TestFailureSensor:
    """Adapter that bridges TestWatcher → UnifiedIntakeRouter.

    Parameters
    ----------
    repo:
        Repository name (e.g. ``"jarvis"``).
    router:
        UnifiedIntakeRouter instance.
    test_watcher:
        Optional existing TestWatcher. If None, sensor operates in
        signal-push mode only (caller calls ``handle_signals()``).
    """

    def __init__(
        self,
        repo: str,
        router: Any,
        test_watcher: Any = None,
    ) -> None:
        self._repo = repo
        self._router = router
        self._watcher = test_watcher
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        # In-flight dedup: target_file_path -> monotonic submitted_at
        # See module docstring above ``_INFLIGHT_TTL_S`` for the rationale.
        self._pending_target_keys: Dict[str, float] = {}

    def _prune_stale_pending(self) -> None:
        """Drop pending target entries that have exceeded their TTL.

        Bounds the dict size and ensures a stuck op (orchestrator crash,
        hibernation, forgotten release callback) eventually releases the
        slot so the next legitimate signal for the same file can flow.
        """
        if _INFLIGHT_TTL_S <= 0 or not self._pending_target_keys:
            return
        now = time.monotonic()
        stale = [
            k for k, ts in self._pending_target_keys.items()
            if now - ts > _INFLIGHT_TTL_S
        ]
        for k in stale:
            del self._pending_target_keys[k]

    def _in_flight_target(self, signal: IntentSignal) -> Optional[str]:
        """Return the first target_file from *signal* that is already
        marked in-flight (within TTL), or None if all targets are free.

        Called before ``router.ingest`` to short-circuit re-emission of
        a signal whose target file already has an op working on it.
        """
        if _INFLIGHT_TTL_S <= 0:
            return None
        self._prune_stale_pending()
        for target in (signal.target_files or ()):
            if target in self._pending_target_keys:
                return target
        return None

    def _mark_targets_in_flight(self, signal: IntentSignal) -> None:
        """Record the signal's target files as in-flight. Called only
        after a successful ``router.ingest`` with status ``"enqueued"`` —
        dropped / deduplicated / queued signals do NOT mark targets,
        because the router is going to re-ingest them later and that
        re-ingest should not be self-suppressed by sensor-side dedup.
        """
        if _INFLIGHT_TTL_S <= 0:
            return
        now = time.monotonic()
        for target in (signal.target_files or ()):
            self._pending_target_keys[target] = now

    def release_target(self, target_file: str) -> None:
        """Manually release an in-flight target slot. Public API for
        orchestrator / GLS completion hooks that want to unblock the
        next signal immediately instead of waiting for TTL expiry.
        Idempotent — no-op if the target was not tracked.
        """
        self._pending_target_keys.pop(target_file, None)

    async def _signal_to_envelope_and_ingest(
        self, signal: IntentSignal
    ) -> Optional[IntentEnvelope]:
        """Convert one IntentSignal to IntentEnvelope and ingest it.

        Returns the envelope if ingested, None if skipped.
        """
        if not signal.stable:
            return None

        # In-flight dedup: reject re-emission while an op is already
        # working on any of the signal's target files. This is the
        # earliest-possible short-circuit — before envelope creation,
        # before router.ingest, before any WAL / queue / op_id burn.
        in_flight_target = self._in_flight_target(signal)
        if in_flight_target is not None:
            logger.info(
                "TestFailureSensor: suppressing re-emission — target "
                "%s already in-flight (%.0fs ago): %s",
                in_flight_target,
                time.monotonic() - self._pending_target_keys[in_flight_target],
                signal.description[:80],
            )
            return None

        confidence = min(1.0, signal.confidence)
        envelope = make_envelope(
            source="test_failure",
            description=signal.description,
            target_files=signal.target_files,
            repo=self._repo,
            confidence=confidence,
            urgency="high",
            evidence=dict(signal.evidence),
            requires_human_ack=False,
            causal_id=signal.signal_id,  # signal_id becomes causal_id
            signal_id=signal.signal_id,
        )
        try:
            result = await self._router.ingest(envelope)
            if result == "enqueued":
                self._mark_targets_in_flight(signal)
                logger.info(
                    "TestFailureSensor: enqueued test failure: %s",
                    signal.description,
                )
            return envelope
        except Exception:
            logger.exception("TestFailureSensor: ingest failed: %s", signal.description)
            return None

    async def handle_signals(
        self, signals: List[IntentSignal]
    ) -> List[Optional[IntentEnvelope]]:
        """Process a batch of IntentSignals. Returns per-signal results."""
        results = []
        for sig in signals:
            result = await self._signal_to_envelope_and_ingest(sig)
            results.append(result)
        return results

    async def start(self) -> None:
        """Start background polling via TestWatcher (if provided)."""
        if self._watcher is None:
            return
        self._running = True
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="test_failure_sensor_poll",
        )

    async def stop(self) -> None:
        """Cancel the poll task and stop the underlying watcher.

        Previously this method was sync and only set ``_running=False``
        + stopped the watcher — the poll task reference was never
        captured, so asyncio emitted "Task was destroyed but pending"
        on every teardown (battle test bt-2026-04-13-031119). Now
        async so callers can ``await`` clean drain; task handle is
        tracked from ``start()`` and cancelled deterministically.
        """
        self._running = False
        if self._watcher is not None:
            try:
                self._watcher.stop()
            except Exception:
                logger.debug("TestFailureSensor: watcher.stop() raised", exc_info=True)
        task = self._poll_task
        self._poll_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # ------------------------------------------------------------------
    # Event-driven path (Manifesto §3: zero polling, pure reflex)
    # ------------------------------------------------------------------

    async def subscribe_to_bus(self, event_bus: Any) -> None:
        """Subscribe to file system events.

        Two event paths:
        1. .jarvis/test_results.json changes → instant structured consumption (Phase 2)
        2. .py file changes → debounced subprocess pytest run (Phase 1 fallback)
        """
        await event_bus.subscribe("fs.changed.*", self._on_fs_event)
        self._debounce_task: Optional[asyncio.Task] = None
        self._last_plugin_ts: float = 0.0  # monotonic — suppresses redundant runs
        logger.info("TestFailureSensor: subscribed to fs.changed.* events (Phase 2)")

    async def _on_fs_event(self, event: Any) -> None:
        """Route events: test_results.json → instant consume; .py → debounced pytest."""
        rel_path = event.payload.get("relative_path", "")

        # Phase 2: ouroboros_pytest_plugin results file
        if rel_path.endswith("test_results.json") and ".jarvis" in rel_path:
            await self._on_test_results_changed(event)
            return

        # Phase 1 fallback: .py changes → debounced subprocess
        if event.payload.get("extension") != ".py":
            return
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(
            self._debounced_pytest_run(),
            name="test_failure_debounced_run",
        )

    # ------------------------------------------------------------------
    # Phase 2: Structured results from ouroboros_pytest_plugin
    # ------------------------------------------------------------------

    async def _on_test_results_changed(self, event: Any) -> None:
        """Consume .jarvis/test_results.json written by the pytest plugin."""
        path = event.payload.get("path", "")
        failures = self._parse_results_file(path)

        if self._watcher is not None:
            signals = self._watcher.process_failures(failures)
            if signals:
                logger.info(
                    "TestFailureSensor: plugin results → %d stable signals",
                    len(signals),
                )
                await self.handle_signals(signals)
            else:
                logger.debug(
                    "TestFailureSensor: plugin results consumed "
                    "(%d failures, no stable signals yet)",
                    len(failures),
                )

        self._last_plugin_ts = time.monotonic()

    def _parse_results_file(self, path: str) -> List[TestFailure]:
        """Parse the JSON results file into TestFailure objects."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.debug("TestFailureSensor: failed to read results file: %s", exc)
            return []

        if data.get("schema_version") != 1:
            logger.debug(
                "TestFailureSensor: unknown schema_version %s",
                data.get("schema_version"),
            )
            return []

        # Staleness check — ignore results older than 60s
        ts = data.get("timestamp", 0)
        if time.time() - ts > 60.0:
            logger.debug("TestFailureSensor: stale results file (%.0fs old)", time.time() - ts)
            return []

        failures: List[TestFailure] = []
        for entry in data.get("failures", []):
            nodeid = entry.get("nodeid", "")
            file_path = entry.get("file_path", nodeid.split("::")[0])
            error_text = entry.get("error_text", "")
            failures.append(TestFailure(
                test_id=nodeid,
                file_path=file_path,
                error_text=error_text,
            ))

        return failures

    # ------------------------------------------------------------------
    # Phase 1 fallback: debounced subprocess pytest run
    # ------------------------------------------------------------------

    async def _debounced_pytest_run(self) -> None:
        """Wait 2s for edits to settle, then trigger a pytest run."""
        try:
            await asyncio.sleep(2.0)
            # Suppress if plugin results were consumed recently (Phase 2 active)
            if time.monotonic() - self._last_plugin_ts < 10.0:
                logger.debug(
                    "TestFailureSensor: skipping subprocess run — "
                    "plugin results consumed %.1fs ago",
                    time.monotonic() - self._last_plugin_ts,
                )
                return
            if self._watcher is not None:
                signals = await self._watcher.poll_once()
                if signals:
                    await self.handle_signals(signals)
        except asyncio.CancelledError:
            pass  # Newer edit arrived — debounce reset
        except Exception:
            logger.debug("TestFailureSensor: debounced run error", exc_info=True)

    # ------------------------------------------------------------------
    # Poll fallback (safety net when event spine is unavailable)
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._running and self._watcher is not None:
            try:
                signals = await self._watcher.poll_once()
                if signals:
                    await self.handle_signals(signals)
            except Exception:
                logger.exception("TestFailureSensor: poll error")
            try:
                await asyncio.sleep(self._watcher.poll_interval_s)
            except asyncio.CancelledError:
                break

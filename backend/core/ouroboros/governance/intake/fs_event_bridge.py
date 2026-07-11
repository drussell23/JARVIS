"""FileSystemEventBridge — publishes file system events to TrinityEventBus.

Owns a single FileWatchGuard watching the project root. On each real file
change (debounced, checksum-verified), publishes to topic ``fs.changed.*``
so intake sensors can react in sub-second time instead of polling.

Boundary Principle (Manifesto §3 / §5):
  Deterministic: File watching, debounce, checksum, topic routing.
  Agentic: What to *do* with the change (sensor-level decision).
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import time as _time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_HEARTBEAT_EVERY_N = int(os.environ.get("JARVIS_FS_BRIDGE_HEARTBEAT_EVERY", "100"))

_DEFAULT_IGNORE_GLOBS = (
    "*/.worktrees/*", "*/__pycache__/*", "*/.git/*", "*/.ouroboros/*",
    "*/node_modules/*", "*/venv/*", "*/.venv/*", "*/*.egg-info/*",
)

_SENTINEL_BASENAME = "fs_watch_sentinel.json"


def _sentinel_enabled() -> bool:
    return os.environ.get(
        "JARVIS_FS_BRIDGE_SENTINEL_ENABLED", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


def _ready_budget_s() -> float:
    raw = os.environ.get("JARVIS_FS_BRIDGE_READY_BUDGET_S", "300")
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "[FSEventBridge] malformed JARVIS_FS_BRIDGE_READY_BUDGET_S=%r "
            "— falling back to default 300s", raw,
        )
        return 300.0


def _sentinel_retouch_s() -> float:
    raw = os.environ.get("JARVIS_FS_BRIDGE_SENTINEL_RETOUCH_S", "15")
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "[FSEventBridge] malformed JARVIS_FS_BRIDGE_SENTINEL_RETOUCH_S=%r "
            "— falling back to default 15s", raw,
        )
        return 15.0


def _ignore_globs_from_env() -> List[str]:
    """Deep-glob ignore entries, env-tunable (Slice 5 T1, mandate 2).

    ``JARVIS_FS_BRIDGE_IGNORE_GLOBS`` — comma-separated full-path fnmatch
    globs. Unset -> defaults; set-but-empty -> [] (legacy basename-only).
    """
    raw = os.environ.get("JARVIS_FS_BRIDGE_IGNORE_GLOBS")
    if raw is None:
        return list(_DEFAULT_IGNORE_GLOBS)
    return [g.strip() for g in raw.split(",") if g.strip()]


class FileSystemEventBridge:
    """Bridges file system events from FileWatchGuard to TrinityEventBus.

    Parameters
    ----------
    project_root:
        Directory to watch recursively.
    event_bus:
        TrinityEventBus instance for publishing events.
    watch_config:
        Optional FileWatchConfig override. Defaults are tuned for
        source code monitoring (*.py, *.json, debounce 0.3s).
    """

    def __init__(
        self,
        project_root: Path,
        event_bus: Any,
        watch_config: Any = None,
    ) -> None:
        self._project_root = project_root.resolve()
        self._event_bus = event_bus
        self._watch_config = watch_config
        self._guard: Optional[Any] = None
        self._events_published: int = 0
        # Placeholder — re-resolved schedule-aware in start() (see
        # _resolve_sentinel_dir): the parent dir must be one the guard
        # actually schedules, which ".jarvis" is NOT (exclude_top_level_dirs).
        self._sentinel_path: Path = self._project_root / _SENTINEL_BASENAME
        self._sentinel_observed: Optional[asyncio.Event] = None  # armed in start()
        self._verify_task: Optional[asyncio.Task] = None
        self._watch_confirmed: bool = False
        self._watch_config_active: Any = None  # the config start() handed the guard

    async def start(self) -> None:
        """Start the FileWatchGuard and begin publishing events."""
        from backend.core.resilience.file_watch_guard import (
            FileWatchGuard,
            FileWatchConfig,
        )

        config = self._watch_config or FileWatchConfig(
            patterns=["*.py", "*.json", "*.yaml", "*.yml"],
            ignore_patterns=[
                "__pycache__/*", ".git/*", "*.pyc", "node_modules/*",
                "venv/*", ".venv/*", "*.egg-info/*", ".ouroboros/*",
                ".worktrees/*", "*.swp", "*.tmp", "*~",
            ] + _ignore_globs_from_env(),
            recursive=True,
            debounce_seconds=0.3,
            verify_checksum=True,
            dedup_ttl_seconds=2.0,
        )

        self._watch_config_active = config
        self._guard = FileWatchGuard(
            watch_dir=self._project_root,
            on_event=self._on_file_event,
            config=config,
        )
        ok = await self._guard.start()
        if ok:
            logger.info(
                "[FSEventBridge] Watching %s (patterns=%s)",
                self._project_root, config.patterns,
            )
            if _sentinel_enabled():
                self._sentinel_path = (
                    self._resolve_sentinel_parent() / _SENTINEL_BASENAME
                )
                self._sentinel_observed = asyncio.Event()
                self._verify_task = asyncio.create_task(
                    self._verify_pipeline_live(), name="fs_bridge_watch_verify",
                )
                self._verify_task.add_done_callback(self._on_verify_task_done)
        else:
            logger.warning(
                "[FSEventBridge] FileWatchGuard failed to start for %s",
                self._project_root,
            )

    async def stop(self) -> None:
        """Stop the FileWatchGuard."""
        if self._verify_task is not None and not self._verify_task.done():
            self._verify_task.cancel()
            try:
                await self._verify_task
            except asyncio.CancelledError:
                # Only re-raise when stop() ITSELF is being cancelled — not
                # merely the verify_task cancellation requested above, which
                # is the expected outcome of the cancel() call. Python
                # 3.11+ can tell the two apart via ``Task.cancelling()``
                # (PEP 682, same pattern as cancellation_shield.py); earlier
                # versions fall back to the prior swallow-everything
                # behavior for this specific case.
                current = asyncio.current_task()
                cancelling_fn = getattr(current, "cancelling", None) if current is not None else None
                if cancelling_fn is not None and cancelling_fn() > 0:
                    raise
            except Exception:
                pass
        if self._guard is not None:
            await self._guard.stop()
            logger.info(
                "[FSEventBridge] Stopped (published %d events)",
                self._events_published,
            )

    async def _on_file_event(self, event: Any) -> None:
        """Translate a FileEvent into a TrinityEventBus publication.

        Logs the FIRST published event at INFO so battle test logs carry a
        positive signal that the watchdog → bridge → bus chain is alive.
        Subsequent events log only at DEBUG to avoid spam, with a periodic
        heartbeat every ``_HEARTBEAT_EVERY_N`` events. The "did the chain
        ever fire" question that bt-2026-04-12-005521 could not answer
        from logs alone is now a single grep away.
        """
        try:
            # Basename-filter tradeoff: ANY file named fs_watch_sentinel.json
            # anywhere in the watch root is suppressed from sensors — even
            # when the sentinel master switch is off. The name is reserved
            # for the liveness probe; a user file with this exact basename
            # will never reach fs.changed.* subscribers.
            if event.path.name == _SENTINEL_BASENAME:
                if self._sentinel_observed is not None and not self._sentinel_observed.is_set():
                    self._sentinel_observed.set()
                return  # internal liveness probe — never published downstream

            topic = f"fs.changed.{event.event_type.value}"

            # Compute relative path safely
            try:
                rel_path = str(event.path.relative_to(self._project_root))
            except ValueError:
                rel_path = str(event.path)

            extension = event.path.suffix
            is_test = (
                rel_path.startswith("tests/")
                or event.path.name.startswith("test_")
                or event.path.name.endswith("_test.py")
            )
            is_config = (
                extension in (".json", ".yaml", ".yml")
                and ".jarvis" in rel_path
            )

            await self._event_bus.publish_raw(
                topic=topic,
                data={
                    "path": str(event.path),
                    "relative_path": rel_path,
                    "extension": extension,
                    "checksum": event.checksum,
                    "is_test_file": is_test,
                    "is_config_file": is_config,
                    "is_directory": event.is_directory,
                    "timestamp": event.timestamp,
                },
                persist=False,  # High-volume, no need to WAL file events
            )
            self._events_published += 1

            if self._events_published == 1:
                logger.info(
                    "[FSEventBridge] First fs.changed event published: "
                    "topic=%s path=%s — chain is live",
                    topic, rel_path,
                )
            elif self._events_published % _HEARTBEAT_EVERY_N == 0:
                logger.info(
                    "[FSEventBridge] Heartbeat: %d events published "
                    "(latest topic=%s path=%s)",
                    self._events_published, topic, rel_path,
                )
        except Exception:
            logger.debug("[FSEventBridge] Failed to publish event", exc_info=True)

    def _sentinel_dir_from_schedule(self) -> Optional[Path]:
        """First sorted scheduled root at the root/depth-1 tier, or None.

        T4 re-review Important: the guard's ACTUAL schedule is the
        authoritative truth — ``JARVIS_FILE_WATCH_EXCLUDE_DIRS`` fully
        REPLACES ``config.exclude_top_level_dirs`` in
        ``_resolve_excluded_dirs()`` (file_watch_guard.py:1071), and
        ``exclude_path_patterns`` + its env twin can also drop depth-1
        dirs — so mirroring the config object alone can reintroduce the
        unobservable-sentinel failure. Consulting ``_scheduled_paths``
        (set by ``guard.start()``) automatically respects any current or
        future exclusion mechanism. Fail-soft: returns None when the
        attribute is unavailable (e.g. test doubles) so the caller can
        fall back to the config mirror.
        """
        scheduled = getattr(self._guard, "_scheduled_paths", None)
        if not scheduled:
            return None
        try:
            candidates = sorted(
                {
                    Path(p) for p, _recursive in scheduled
                    if Path(p) == self._project_root
                    or Path(p).parent == self._project_root
                },
                key=str,
            )
        except Exception:
            logger.debug(
                "[FSEventBridge] schedule introspection failed", exc_info=True,
            )
            return None
        return candidates[0] if candidates else None

    def _resolve_sentinel_parent(self) -> Path:
        """Sentinel parent: guard schedule (authoritative) → config mirror."""
        chosen = self._sentinel_dir_from_schedule()
        if chosen is not None:
            logger.debug(
                "[FSEventBridge] sentinel dir resolved via guard_schedule: %s",
                chosen,
            )
            return chosen
        chosen = self._resolve_sentinel_dir(self._watch_config_active)
        logger.debug(
            "[FSEventBridge] sentinel dir resolved via config_mirror: %s",
            chosen,
        )
        return chosen

    def _resolve_sentinel_dir(self, config: Any) -> Path:
        """Config-mirror FALLBACK for the sentinel parent (T4 review Critical).

        Used only when ``_sentinel_dir_from_schedule`` cannot read the
        guard's authoritative schedule (test doubles, exotic guards).

        ``.jarvis`` sits in FileWatchGuard's ``exclude_top_level_dirs``
        (Slice 12I) — in the NON-coalesced scheduling regime,
        ``_resolve_watch_paths`` never passes it to ``observer.schedule()``,
        so a sentinel there is unobservable and WATCH NOT CONFIRMED would
        fire falsely every boot. (In the HARD-COALESCED regime the single
        recursive root schedule DOES emit for re-included subtrees — the
        fragility is regime-dependent, so this resolution must be
        regime-independent.)

        Chooses the first (sorted) depth-1 subdirectory of the watch root
        that is (a) a real directory, (b) not in ``exclude_top_level_dirs``,
        (c) not matched by any ignore pattern (probed against a
        representative child path, both absolute and root-relative, per
        Task 1's deep-glob semantics), and (d) not dot-prefixed unless
        nothing else qualifies. Falls back to the watch root itself.
        """
        exclude = getattr(config, "exclude_top_level_dirs", None) or frozenset()
        ignore_patterns = list(getattr(config, "ignore_patterns", None) or [])

        def _watchable(d: Path) -> bool:
            if d.name in exclude:
                return False
            probe_abs = str(d / _SENTINEL_BASENAME)
            probe_rel = f"{d.name}/{_SENTINEL_BASENAME}"
            for pat in ignore_patterns:
                if fnmatch.fnmatch(probe_abs, pat) or fnmatch.fnmatch(probe_rel, pat):
                    return False
            return True

        try:
            children = sorted(
                (p for p in self._project_root.iterdir() if p.is_dir()),
                key=lambda p: p.name,
            )
        except OSError:
            return self._project_root
        for allow_hidden in (False, True):
            for child in children:
                if not allow_hidden and child.name.startswith("."):
                    continue
                if _watchable(child):
                    return child
        return self._project_root

    def _on_verify_task_done(self, task: "asyncio.Task[Any]") -> None:
        """Surface any unhandled exception from the verify task (review fix).

        A malformed-env raise (or any other exception) inside
        ``_verify_pipeline_live`` previously died silently — the task's
        exception went unobserved and NEITHER "WATCH ACTIVE" nor
        "WATCH NOT CONFIRMED" was ever logged, leaving a misleading
        silent-third-state for the iso driver. Cancellation is expected
        (stop()) and not logged as an error.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning(
                "[FSEventBridge] verify task died with an unhandled "
                "exception — neither WATCH ACTIVE nor WATCH NOT CONFIRMED "
                "will be emitted: %r", exc, exc_info=exc,
            )

    async def _verify_pipeline_live(self) -> None:
        """Prove the watch pipeline delivers events (Slice 5 F1/F6, Run #15 L1).

        Touches a sentinel inside the watch root and awaits its own event.
        Observed  -> publish ``fs.watch.ready`` + the WATCH ACTIVE marker
        (the isomorphic driver gates chaos injection on that marker).
        Budget exhausted -> the WATCH NOT CONFIRMED warning (F6 boot half).
        Fail-soft by construction: background task, never blocks start().
        """
        assert self._sentinel_observed is not None  # armed in start() before spawn
        t0 = _time.monotonic()
        budget = _ready_budget_s()
        retouch = _sentinel_retouch_s()
        attempt = 0
        logger.debug(
            "[FSEventBridge] sentinel verify starting — path=%s budget=%.0fs "
            "retouch=%.1fs", self._sentinel_path, budget, retouch,
        )
        try:
            while (_time.monotonic() - t0) < budget:
                # Content NONCE per attempt: the guard checksum-gates
                # events once a baseline md5 exists (file_watch_guard
                # :1933), so identical-bytes retouches could be dropped.
                attempt += 1
                content = '{"probe": "fs-watch-liveness", "attempt": %d}\n' % attempt
                try:
                    self._sentinel_path.write_text(content, encoding="utf-8")
                except OSError:
                    # Parent may have vanished mid-verify. Re-resolve ONCE
                    # from the authoritative schedule and retry — never
                    # mkdir-resurrect arbitrary top-level dirs.
                    try:
                        self._sentinel_path = (
                            self._resolve_sentinel_parent() / _SENTINEL_BASENAME
                        )
                        self._sentinel_path.write_text(content, encoding="utf-8")
                    except OSError:
                        logger.debug(
                            "[FSEventBridge] sentinel touch failed after "
                            "re-resolve", exc_info=True,
                        )
                try:
                    # Clamp so the final wait never overshoots the budget.
                    wait_s = min(retouch, max(0.1, budget - (_time.monotonic() - t0)))
                    await asyncio.wait_for(self._sentinel_observed.wait(), timeout=wait_s)
                except asyncio.TimeoutError:
                    continue  # bounded re-touch; the wait itself is event-driven
                elapsed = _time.monotonic() - t0
                self._watch_confirmed = True
                logger.info(
                    "[FSEventBridge] WATCH ACTIVE — pipeline verified live "
                    "(sentinel observed after %.1fs)", elapsed,
                )
                try:
                    await self._event_bus.publish_raw(
                        topic="fs.watch.ready",
                        data={"elapsed_s": elapsed, "watch_root": str(self._project_root)},
                        persist=False,
                    )
                except Exception:
                    logger.debug("[FSEventBridge] fs.watch.ready publish failed", exc_info=True)
                return
            logger.warning(
                "[FSEventBridge] WATCH NOT CONFIRMED — zero sentinel "
                "observations after %.0fs (events_published=%d); fs.changed "
                "consumers may be blind to changes in this window",
                budget, self._events_published,
            )
        finally:
            try:
                self._sentinel_path.unlink(missing_ok=True)
            except OSError:
                pass

    def get_metrics(self) -> Dict[str, Any]:
        """Return bridge metrics for observability."""
        guard_metrics = {}
        if self._guard is not None and hasattr(self._guard, "get_metrics"):
            guard_metrics = self._guard.get_metrics()
        return {
            "events_published": self._events_published,
            "guard_healthy": self._guard.is_healthy if self._guard else False,
            "watch_confirmed": self._watch_confirmed,
            **guard_metrics,
        }

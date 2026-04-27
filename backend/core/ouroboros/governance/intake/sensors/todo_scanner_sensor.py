"""
TodoScannerSensor — Finds unfinished work in the Trinity codebase.

Scans all Python files for TODO, FIXME, HACK, XXX, NOQA, and
DEPRECATED markers. Classifies by priority and routes actionable
items through the Ouroboros pipeline.

Boundary Principle:
  Deterministic: Regex scan, file enumeration, priority classification.
  Agentic: Resolution of the TODO (code generation) via pipeline.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import asyncio

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = float(os.environ.get("JARVIS_TODO_SCAN_INTERVAL_S", "86400"))

# --- Gap #4 migration: FS-event primary mode (Slice 7) ---------------------
#
# When ``JARVIS_TODO_FS_EVENTS_ENABLED=true``, the FileSystemEventBridge
# (``fs.changed.*`` on ``TrinityEventBus``) becomes the primary trigger:
# a ``.py`` file change → single-file scan at pub/sub latency, not a
# 24-hour whole-tree sweep. The poll demotes to
# ``JARVIS_TODO_FALLBACK_INTERVAL_S`` (default 6h — tight enough to
# catch a dropped FS event without waiting another day, matching the
# DocStalenessSensor cadence).
#
# Shadow pattern: flag defaults OFF so current pure-poll behavior is
# preserved until a 3-session battle-test arc graduates the flag (same
# precedent as every other gap-#4 sensor migration).
_TODO_FALLBACK_INTERVAL_S: float = float(
    os.environ.get("JARVIS_TODO_FALLBACK_INTERVAL_S", "21600")
)


def fs_events_enabled() -> bool:
    """Re-read ``JARVIS_TODO_FS_EVENTS_ENABLED`` at call-time."""
    return os.environ.get(
        "JARVIS_TODO_FS_EVENTS_ENABLED", "true",
    ).lower() in ("true", "1", "yes")

# Trigger tag: a parenthesized suffix on any marker that elevates that single
# item to high urgency, max confidence, and bypasses dedup. Used by battle
# tests and the human seeding workflow to land a deterministic emission that
# beats coalescing — the standard `normal`/`low` urgency markers (TODO,
# DEPRECATED, OPTIMIZE, REFACTOR, NOTE) never emit on their own, so without
# this tag a battle test cannot inject a TODO and have it survive the gate.
#
# Default tag is ``rsi-trigger`` and matches case-insensitively. Example:
#   # TODO(rsi-trigger): scan this file
#   # FIXME(rsi-trigger): seeded by bt-2026-04-12-005521
# Override via JARVIS_TODO_SCANNER_TRIGGER_TAG. Set to an empty string to
# disable the bypass entirely (production tightening).
_TRIGGER_TAG = os.environ.get("JARVIS_TODO_SCANNER_TRIGGER_TAG", "rsi-trigger").strip()
_TRIGGER_PATTERN = (
    re.compile(r"\(\s*" + re.escape(_TRIGGER_TAG) + r"\s*\)", re.IGNORECASE)
    if _TRIGGER_TAG
    else None
)

# Markers to scan for, with priority weights
_MARKERS: Dict[str, Tuple[str, float]] = {
    "FIXME":      ("high", 0.9),
    "HACK":       ("high", 0.8),
    "XXX":        ("high", 0.7),
    "BUG":        ("high", 0.9),
    "TODO":       ("normal", 0.5),
    "DEPRECATED": ("normal", 0.4),
    "OPTIMIZE":   ("low", 0.3),
    "REFACTOR":   ("low", 0.3),
    "NOTE":       ("low", 0.1),  # Low priority — informational only
}

# Marker pattern allows an optional parenthesized tag immediately after the
# marker name. The tag itself is captured by the standalone _TRIGGER_PATTERN
# scan on the body so we can detect the trigger anywhere in the comment text,
# not just adjacent to the marker.
_MARKER_PATTERN = re.compile(
    r"#\s*(" + "|".join(_MARKERS.keys()) + r")\b(?:\([^)]*\))?[:\s]*(.*)",
    re.IGNORECASE,
)

_SCAN_DIRS = (
    "backend/",
    "tests/",
    "scripts/",
)

_SKIP_DIRS = frozenset({
    "venv", "__pycache__", "node_modules", ".git",
    "site-packages", ".worktrees", "venv_py39_backup",
})


# Slice 11.6.a — Merkle Cartographer consultation. When the per-sensor
# flag JARVIS_TODO_USE_MERKLE is on AND the cartographer's master flag
# is on, the scan loop short-circuits to the cached prior result when
# nothing under _SCAN_DIRS has changed since the last successful scan.
# Cuts O(N) disk reads to O(1) on the steady state — most poll cycles
# in JARVIS are no-op (no code changed in the last 24h between scans).
#
# Default false to preserve byte-identical legacy behavior. Per-sensor
# graduation: each Slice 11.6.{a,b,c,d} flag flips independently after
# its own forced-clean once-proof cadence.


def merkle_consult_enabled() -> bool:
    """Re-read ``JARVIS_TODO_USE_MERKLE`` at call time so monkeypatch
    works in tests + operator can flip live without re-init."""
    raw = os.environ.get(
        "JARVIS_TODO_USE_MERKLE", "",
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


@dataclass
class TodoItem:
    """One TODO/FIXME/HACK found in the codebase."""
    file_path: str
    line_number: int
    marker: str                # TODO, FIXME, HACK, etc.
    text: str                  # The comment text after the marker
    urgency: str               # high, normal, low
    priority: float            # 0.0–1.0
    auto_resolvable: bool = False  # Can Ouroboros fix this?
    trigger_tag: bool = False  # Battle-test seeded trigger — bypasses gate + dedup


def _parse_marker_line(line: str) -> Optional[Tuple[str, str, str, float, bool]]:
    """Parse one source line for a TODO marker.

    Returns ``(marker, text, urgency, priority, has_trigger_tag)`` or
    ``None`` if no marker is present. ``has_trigger_tag`` is True when the
    line contains the trigger tag (default ``rsi-trigger``); when set, the
    caller should elevate the item to high urgency / max priority and bypass
    dedup so battle-test seeds always land.
    """
    match = _MARKER_PATTERN.search(line)
    if not match:
        return None
    marker = match.group(1).upper()
    text = match.group(2).strip()
    urgency, priority = _MARKERS.get(marker, ("low", 0.1))

    has_trigger = bool(_TRIGGER_PATTERN and _TRIGGER_PATTERN.search(line))
    if has_trigger:
        # Trigger tag wins regardless of base marker — even a bare TODO can
        # pierce the high-urgency gate when explicitly seeded.
        urgency = "high"
        priority = 1.0
    return marker, text, urgency, priority, has_trigger


class TodoScannerSensor:
    """Scans Trinity codebases for unfinished work markers.

    Finds TODO, FIXME, HACK, XXX, BUG, DEPRECATED, OPTIMIZE, REFACTOR
    comments and classifies them by urgency. High-urgency items (FIXME,
    HACK, BUG) are emitted as IntentEnvelopes for Ouroboros resolution.

    Follows the implicit sensor protocol: start(), stop(), scan_once().
    """

    def __init__(
        self,
        repo: str,
        router: Any,
        poll_interval_s: float = _POLL_INTERVAL_S,
        project_root: Optional[Path] = None,
    ) -> None:
        self._repo = repo
        self._router = router
        self._poll_interval_s = poll_interval_s
        self._root = project_root or Path(".")
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._seen: set[str] = set()
        # Gap #4 migration — captured at __init__ time. When True, the
        # poll loop demotes to the fallback interval and
        # ``subscribe_to_bus`` becomes the authoritative trigger.
        self._fs_events_mode: bool = fs_events_enabled()
        self._fs_events_handled: int = 0
        self._fs_events_ignored: int = 0
        # Slice 11.6.a — cached scan result for merkle short-circuit.
        # When JARVIS_TODO_USE_MERKLE is on AND the cartographer's
        # current root hash matches what we recorded after the last
        # full scan, we skip the disk walk and return cached items.
        # The "baseline tracking" pattern: sensor records the
        # cartographer's root hash at the END of each successful scan,
        # then compares against current_root_hash() at the start of
        # the next cycle. Decoupled from cartographer's persisted-vs-
        # in-memory comparison so sensors detect changes between two
        # sensor cycles regardless of whether the persisted snapshot
        # has been updated.
        self._merkle_cached_items: List[TodoItem] = []
        self._merkle_last_seen_root_hash: str = ""
        self._merkle_short_circuits: int = 0
        self._merkle_full_scans: int = 0

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"todo_scanner_{self._repo}",
        )
        effective = (
            _TODO_FALLBACK_INTERVAL_S
            if self._fs_events_mode
            else self._poll_interval_s
        )
        mode = (
            "fs-events-primary (.py change → scan_file; poll=fallback)"
            if self._fs_events_mode
            else "poll-primary"
        )
        logger.info(
            "[TodoScanner] Started for repo=%s poll_interval=%ds mode=%s",
            self._repo, int(effective), mode,
        )

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    # ------------------------------------------------------------------
    # Event-driven path (Manifesto §3: zero polling, pure reflex)
    # ------------------------------------------------------------------

    async def subscribe_to_bus(self, event_bus: Any) -> None:
        """Subscribe to file-system events via ``TrinityEventBus``.

        Gated by ``JARVIS_TODO_FS_EVENTS_ENABLED`` (default OFF). When
        the flag is off this method is a logged no-op so legacy
        pure-poll behavior is preserved exactly (no silent regression
        when the graduation flip lands). Caller contract matches the
        TestFailureSensor + DocStalenessSensor pattern:
        ``IntakeLayerService`` unconditionally calls ``subscribe_to_bus``
        on every sensor that exposes it; the flag check lives here so
        one sensor's decision doesn't require special-casing at the
        call site.

        Subscription failures are caught locally — the intake layer
        must never regress just because TrinityEventBus rejected a
        subscription.
        """
        if not self._fs_events_mode:
            logger.debug(
                "[TodoScanner] FS-event subscription skipped "
                "(JARVIS_TODO_FS_EVENTS_ENABLED=false). "
                "Poll-primary mode active — no gap #4 resolution.",
            )
            return

        try:
            await event_bus.subscribe("fs.changed.*", self._on_fs_event)
        except Exception as exc:
            logger.warning(
                "[TodoScanner] FS-event subscription failed: %s "
                "(poll-fallback at %ds continues)",
                exc, int(_TODO_FALLBACK_INTERVAL_S),
            )
            return

        logger.info(
            "[TodoScanner] subscribed to fs.changed.* — "
            "FS events now PRIMARY (poll demoted to %ds fallback)",
            int(_TODO_FALLBACK_INTERVAL_S),
        )

    async def _on_fs_event(self, event: Any) -> None:
        """React to file change — scan only the changed file."""
        payload = event.payload
        if payload.get("extension") != ".py":
            self._fs_events_ignored += 1
            return
        if event.topic == "fs.changed.deleted":
            self._fs_events_ignored += 1
            return
        file_path = Path(payload["path"])
        if any(skip in file_path.parts for skip in _SKIP_DIRS):
            self._fs_events_ignored += 1
            return
        self._fs_events_handled += 1
        try:
            await self.scan_file(file_path)
        except Exception:
            logger.debug("[TodoScanner] Event-driven scan error", exc_info=True)

    async def scan_file(self, py_file: Path) -> List[TodoItem]:
        """Scan a single file for TODO markers and emit high-priority items."""
        items: List[TodoItem] = []
        try:
            content = py_file.read_text(errors="replace")
            for line_num, line in enumerate(content.split("\n"), 1):
                parsed = _parse_marker_line(line)
                if parsed is None:
                    continue
                marker, text, urgency, priority, has_trigger = parsed
                auto = self._is_auto_resolvable(marker, text)
                rel_path = str(py_file.relative_to(self._root))
                items.append(TodoItem(
                    file_path=rel_path,
                    line_number=line_num,
                    marker=marker,
                    text=text[:200],
                    urgency=urgency,
                    priority=priority,
                    auto_resolvable=auto,
                    trigger_tag=has_trigger,
                ))
        except Exception:
            return items

        await self._emit_items(items)
        return items

    # ------------------------------------------------------------------
    # Poll fallback (safety net when event spine is unavailable)
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        await asyncio.sleep(180)  # Delay after boot
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("[TodoScanner] Poll error", exc_info=True)
            effective_interval = (
                _TODO_FALLBACK_INTERVAL_S
                if self._fs_events_mode
                else self._poll_interval_s
            )
            try:
                await asyncio.sleep(effective_interval)
            except asyncio.CancelledError:
                break

    async def scan_once(self) -> List[TodoItem]:
        """Scan all Python files for TODO markers. Returns found items.

        Slice 11.6.a — when ``JARVIS_TODO_USE_MERKLE=true`` AND the
        Merkle Cartographer says nothing has changed under ``_SCAN_DIRS``
        since the last successful scan, short-circuit to the cached
        items (skip disk walk + emission). Reduces O(N) full-tree walks
        to O(1) for the typical no-change case (most poll cycles in
        JARVIS find no new TODOs between intervals).

        When master flag(s) off OR cartographer reports change → full
        scan as legacy behavior.
        """
        current_hash = self._merkle_current_root_hash()
        if self._merkle_should_short_circuit(current_hash):
            self._merkle_short_circuits += 1
            logger.debug(
                "[TodoScanner] Merkle short-circuit "
                "(scan #%d skipped, %d cached items)",
                self._merkle_short_circuits + self._merkle_full_scans,
                len(self._merkle_cached_items),
            )
            return list(self._merkle_cached_items)

        self._merkle_full_scans += 1
        loop = asyncio.get_running_loop()
        items = await loop.run_in_executor(None, self._scan_files_sync)
        # Cache the result so a subsequent merkle-says-no-change cycle
        # has accurate state to return. Stored regardless of merkle
        # flag so flipping the flag mid-session doesn't blank state.
        self._merkle_cached_items = list(items)
        # Refresh baseline AFTER the scan completes — captures the
        # cartographer's current state so the next cycle can detect
        # post-scan changes.
        self._merkle_last_seen_root_hash = current_hash
        await self._emit_items(items)
        return items

    def _merkle_current_root_hash(self) -> str:
        """Read the cartographer's current root hash. Returns empty
        string on any failure path — fail-safe to legacy scan."""
        if not merkle_consult_enabled():
            return ""
        try:
            from backend.core.ouroboros.governance.merkle_cartographer import (
                get_default_cartographer,
            )
            c = get_default_cartographer(repo_root=self._root)
            return c.current_root_hash()
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[TodoScanner] current_root_hash read failed; "
                "falling through to full scan", exc_info=True,
            )
            return ""

    def _merkle_should_short_circuit(self, current_hash: str) -> bool:
        """Decide whether to skip the disk walk based on cartographer
        state. Returns False (i.e. proceed with full scan) on any
        failure path — fail-safe to legacy behavior.

        Conditions for short-circuit:
          1. Per-sensor flag ``JARVIS_TODO_USE_MERKLE`` is true
          2. Cartographer master flag enabled (its
             ``current_root_hash`` returns "" when off — sensor
             treats empty as "always changed" → fail-safe)
          3. The cartographer's current root hash equals the hash
             we recorded after the last full scan
          4. We have a prior cached scan result (no point short-
             circuiting on cold-start since cache is empty)
        """
        if not merkle_consult_enabled():
            return False
        if not self._merkle_cached_items:
            return False  # cold start — must populate cache
        if not current_hash:
            return False  # cartographer disabled / cold-start / error
        if not self._merkle_last_seen_root_hash:
            return False  # first scan — no baseline yet
        return current_hash == self._merkle_last_seen_root_hash

    def _scan_files_sync(self) -> List[TodoItem]:
        """CPU-bound scan — runs in a thread via run_in_executor."""
        items: List[TodoItem] = []

        for scan_dir in _SCAN_DIRS:
            full_dir = self._root / scan_dir
            if not full_dir.exists():
                continue

            for py_file in full_dir.rglob("*.py"):
                if any(skip in py_file.parts for skip in _SKIP_DIRS):
                    continue

                try:
                    content = py_file.read_text(errors="replace")
                    for line_num, line in enumerate(content.split("\n"), 1):
                        parsed = _parse_marker_line(line)
                        if parsed is None:
                            continue
                        marker, text, urgency, priority, has_trigger = parsed
                        auto = self._is_auto_resolvable(marker, text)
                        rel_path = str(py_file.relative_to(self._root))
                        items.append(TodoItem(
                            file_path=rel_path,
                            line_number=line_num,
                            marker=marker,
                            text=text[:200],
                            urgency=urgency,
                            priority=priority,
                            auto_resolvable=auto,
                            trigger_tag=has_trigger,
                        ))
                except Exception:
                    pass

        return items

    # ------------------------------------------------------------------
    # Shared emission logic
    # ------------------------------------------------------------------

    async def _emit_items(self, items: List[TodoItem]) -> int:
        """Emit high-priority items as IntentEnvelopes. Returns count emitted.

        Standard items must be ``high`` urgency to emit (FIXME, HACK, BUG, XXX).
        Trigger-tagged items (``# TODO(rsi-trigger): ...``) bypass both the
        urgency gate and the dedup set so battle-test seeds re-fire on every
        scan even if the file/line is unchanged. Without the bypass, a seed
        committed once would never be picked up by subsequent scans because
        ``self._seen`` persists for the lifetime of the sensor.
        """
        emitted = 0
        trigger_count = 0
        for item in items:
            if not item.trigger_tag and item.urgency not in ("high",):
                continue

            dedup_key = f"{item.file_path}:{item.line_number}:{item.marker}"
            if not item.trigger_tag and dedup_key in self._seen:
                continue
            # Standard items get added to dedup; trigger items intentionally
            # do NOT — every scan should re-emit the same seed.
            if not item.trigger_tag:
                self._seen.add(dedup_key)
            else:
                trigger_count += 1

            try:
                envelope = make_envelope(
                    source="todo_scanner",
                    description=(
                        f"{item.marker} at {item.file_path}:{item.line_number}: "
                        f"{item.text}"
                    ),
                    target_files=(item.file_path,),
                    repo=self._repo,
                    confidence=item.priority,
                    urgency=item.urgency,
                    evidence={
                        "category": "todo_marker",
                        "marker": item.marker,
                        "line_number": item.line_number,
                        "text": item.text,
                        "auto_resolvable": item.auto_resolvable,
                        "sensor": "TodoScannerSensor",
                        "trigger_tag": item.trigger_tag,
                    },
                    requires_human_ack=not item.auto_resolvable and not item.trigger_tag,
                )
                result = await self._router.ingest(envelope)
                if result == "enqueued":
                    emitted += 1
            except Exception:
                pass

        if items:
            by_marker: Dict[str, int] = {}
            for item in items:
                by_marker[item.marker] = by_marker.get(item.marker, 0) + 1
            logger.info(
                "[TodoScanner] Found %d markers: %s (%d emitted, %d trigger-tagged)",
                len(items),
                ", ".join(f"{k}={v}" for k, v in sorted(by_marker.items())),
                emitted,
                trigger_count,
            )
        return emitted

    @staticmethod
    def _is_auto_resolvable(marker: str, text: str) -> bool:
        """Determine if Ouroboros can auto-resolve this TODO."""
        text_lower = text.lower()

        # Auto-resolvable patterns
        if marker in ("FIXME", "BUG") and any(w in text_lower for w in (
            "import", "typo", "rename", "unused", "missing return",
            "type error", "none check",
        )):
            return True

        # NOT auto-resolvable
        if any(w in text_lower for w in (
            "design", "decide", "discuss", "should we",
            "architecture", "breaking change",
        )):
            return False

        # HACK/XXX are often quick fixes that can be resolved
        if marker in ("HACK", "XXX") and any(w in text_lower for w in (
            "temporary", "workaround", "cleanup", "remove", "replace", "simplify", "quick fix", "todo",
        )):
            return True

        return False

    def health(self) -> Dict[str, Any]:
        return {
            "sensor": "TodoScannerSensor",
            "repo": self._repo,
            "running": self._running,
            "items_seen": len(self._seen),
            # Slice 11.6.a — Merkle consultation observability
            "merkle_consult_enabled": merkle_consult_enabled(),
            "merkle_short_circuits": self._merkle_short_circuits,
            "merkle_full_scans": self._merkle_full_scans,
            "merkle_cached_items": len(self._merkle_cached_items),
        }

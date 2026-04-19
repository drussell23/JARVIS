"""VisionSensor — read-only consumer of the Ferrari frame stream.

Task 8 of the VisionSensor + Visual VERIFY implementation plan. Slice 1
scope: **deterministic-only** (Tier 0 dhash dedup + Tier 1 OCR regex).
Tier 2 VLM classifier is Task 15; FP budget / cooldowns / chain cap are
Task 11; retention purge is Task 9; full threat-model regression spine is
Task 12.

Authority boundary
------------------
This module is a *consumer* of the Ferrari Engine frame stream produced
by ``VisionCortex`` (``backend/vision/realtime/vision_cortex.py``) via
``backend/vision/frame_server.py``. It **never**:

* calls ``_ensure_frame_server()``,
* spawns ``frame_server.py``,
* imports Quartz / ScreenCaptureKit / AVFoundation capture APIs,
* opens any capture device.

When the frame stream is absent, the sensor fails closed (I8): it emits
zero signals and leaves a rate-limited INFO breadcrumb. This is enforced
structurally by ``tests/governance/test_vision_threat_model.py`` which
greps the module source for forbidden symbols (Task 10).

Spec
----
``docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md``
§Sensor Contract + §Invariant I8.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import re
import signal as _signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)
from backend.core.ouroboros.governance.intent.signals import (
    SignalSource,
    build_vision_signal_evidence,
)
from backend.core.ouroboros.governance.operation_id import generate_operation_id

logger = logging.getLogger("Ouroboros.VisionSensor")


# ---------------------------------------------------------------------------
# Tier 1 deterministic regex patterns
# ---------------------------------------------------------------------------
#
# Order matters *only* in that the set of all matches determines the
# verdict (see ``_classify_from_matches``). Patterns are intentionally
# conservative — OCR output is noisy and a false-positive here costs a
# human approval round (vision-originated ops default to NOTIFY_APPLY
# at minimum per I2, so no silent auto-apply is possible).

_INJECTION_PATTERNS: Dict[str, re.Pattern] = {
    # Python / generic tracebacks — language-agnostic first line is the
    # strongest deterministic error signal on a screen.
    "traceback": re.compile(
        r"Traceback \(most recent call last\)",
    ),
    # Go panic / Rust panic line — ``panic:`` prefix is distinctive.
    "panic": re.compile(
        r"\bpanic:",
        re.IGNORECASE,
    ),
    # Low-level crash — segfault / SIGSEGV / "segmentation fault".
    "segfault": re.compile(
        r"\b(segmentation fault|segfault|SIGSEGV)\b",
        re.IGNORECASE,
    ),
    # Modal dialog titles — "Error" followed by UI-ish affordances.
    # The two-phrase requirement (title word + dialog button) dampens
    # false-fires on code that merely contains the word "Error". Uses
    # DOTALL so the gap between title and button can cross OCR newlines
    # (screenshot OCR typically renders modal body text + button on
    # separate lines).
    "modal_error": re.compile(
        r"(Error|Failed|Exception).{0,200}?(OK|Cancel|Dismiss|Retry)",
        re.DOTALL,
    ),
    # Red-squiggle language errors from IDEs / linters.
    "linter_red": re.compile(
        r"\b(TypeError|ReferenceError|SyntaxError|NameError|"
        r"AttributeError|ImportError|ValueError)\s*:",
    ),
}

# Which patterns escalate to "error_visible" (severity=error, urgency=high)
# versus "bug_visible" (severity=warning).
_ERROR_PATTERNS: frozenset = frozenset({"traceback", "panic", "segfault"})
_BUG_PATTERNS: frozenset = frozenset({"modal_error", "linter_red"})


_DEFAULT_FRAME_PATH = "/tmp/claude/latest_frame.jpg"
_DEFAULT_METADATA_PATH = "/tmp/claude/latest_frame.json"
_DEFAULT_POLL_INTERVAL_S = 1.0
_ADAPTIVE_MAX_INTERVAL_S = 8.0
_ADAPTIVE_STATIC_BEFORE_DOWNSHIFT = 3   # unchanged polls before interval doubles
_HASH_COOLDOWN_S = 10.0                 # dhash-dedup window
_OCR_SNIPPET_LEN = 256                  # matches schema v1 cap

# Retention settings (spec §Retention + threat T7).
_DEFAULT_RETENTION_ROOT = ".jarvis/vision_frames"
_DEFAULT_FRAME_TTL_S = float(os.environ.get("JARVIS_VISION_FRAME_TTL_S", "600"))
_TTL_PURGE_INTERVAL_S = 60.0            # how often the poll loop triggers a TTL scan


@dataclass(frozen=True)
class FrameData:
    """Parsed Ferrari frame + metadata. Pure data, no I/O."""

    frame_path: str
    dhash: str
    ts: float
    app_id: Optional[str]
    window_id: Optional[int]


@dataclass
class VisionSensorStats:
    frames_polled: int = 0
    dropped_hash_dedup: int = 0
    dropped_no_match: int = 0
    dropped_ferrari_absent: int = 0
    signals_emitted: int = 0
    degraded_ticks: int = 0
    frames_retained: int = 0
    frames_purged_ttl: int = 0
    frames_purged_shutdown: int = 0


# ---------------------------------------------------------------------------
# Pure helpers — testable without I/O
# ---------------------------------------------------------------------------


def _classify_from_matches(
    matched_pattern_names: List[str],
) -> Optional[Dict[str, str]]:
    """Map deterministic-regex hit set → (verdict, severity, urgency).

    Returns ``None`` when the hit set is empty (no signal to emit).
    """
    if not matched_pattern_names:
        return None
    matches = set(matched_pattern_names)
    if matches & _ERROR_PATTERNS:
        return {
            "classifier_verdict": "error_visible",
            "severity": "error",
            "urgency": "high",
        }
    if matches & _BUG_PATTERNS:
        return {
            "classifier_verdict": "bug_visible",
            "severity": "warning",
            "urgency": "normal",
        }
    return None


def _run_deterministic_patterns(ocr_text: str) -> List[str]:
    """Run all regex patterns over OCR text, return names that hit.

    Order is preserved (dict insertion order) for deterministic test
    assertions. Empty text yields empty list.
    """
    if not ocr_text:
        return []
    out: List[str] = []
    for name, pattern in _INJECTION_PATTERNS.items():
        if pattern.search(ocr_text):
            out.append(name)
    return out


def _truncate_snippet(text: str, *, max_len: int = _OCR_SNIPPET_LEN) -> str:
    """Clamp OCR snippet to the schema v1 max length (256 chars)."""
    if not text:
        return ""
    return text[:max_len]


# ---------------------------------------------------------------------------
# VisionSensor
# ---------------------------------------------------------------------------


class VisionSensor:
    """Read-only consumer of the Ferrari frame stream.

    The sensor polls ``frame_path`` + ``metadata_path`` at an adaptive
    interval, runs deterministic regex over OCR output (Tier 1), and
    emits one :class:`IntentEnvelope` per distinct frame whose OCR hits
    at least one ``_INJECTION_PATTERNS`` entry.

    Parameters
    ----------
    router:
        ``UnifiedIntakeRouter``-compatible ``ingest(envelope)`` target.
    repo_root:
        Repository root; stamped on the envelope's ``repo`` field.
        Defaults to ``"jarvis"``.
    poll_interval_s:
        Base poll cadence (default ``1.0`` seconds). Adaptive downshift
        doubles the interval after N consecutive unchanged frames up to
        ``_ADAPTIVE_MAX_INTERVAL_S`` (8s).
    frame_path:
        Absolute path to the JPEG frame produced by Ferrari.
    metadata_path:
        Absolute path to the JSON sidecar produced by Ferrari.
    ocr_fn:
        Callable ``(frame_path) -> str`` returning OCR text. When
        ``None`` (the default), Tier 1 regex fires on an empty string
        and therefore emits nothing — tests inject a canned OCR
        implementation.
    hash_cooldown_s:
        How long a ``dhash`` stays in the dedup window (default 10s).
    """

    def __init__(
        self,
        router: Any,
        *,
        repo: str = "jarvis",
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        frame_path: str = _DEFAULT_FRAME_PATH,
        metadata_path: str = _DEFAULT_METADATA_PATH,
        ocr_fn: Optional[Callable[[str], str]] = None,
        hash_cooldown_s: float = _HASH_COOLDOWN_S,
        session_id: Optional[str] = None,
        retention_root: Optional[str] = None,
        frame_ttl_s: Optional[float] = None,
        register_shutdown_hooks: bool = True,
    ) -> None:
        self._router = router
        self._repo = repo
        self._base_poll_interval_s = float(poll_interval_s)
        self._current_poll_interval_s = float(poll_interval_s)
        self._frame_path = frame_path
        self._metadata_path = metadata_path
        self._ocr_fn = ocr_fn
        self._hash_cooldown_s = float(hash_cooldown_s)

        # Tier 0 state: most recent hash → monotonic timestamp.
        self._recent_hashes: Dict[str, float] = {}

        # Adaptive throttle — consecutive unchanged frames.
        self._consecutive_unchanged = 0

        # Rate-limit the degraded-ferrari-absent log to once per minute.
        # ``None`` sentinel (not 0.0) because ``time.monotonic()`` can be
        # small on recently-started processes — a zero initializer would
        # silently short-circuit the first breadcrumb.
        self._last_degraded_log: Optional[float] = None

        self.stats = VisionSensorStats()

        # Lifecycle
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

        # ------------------------------------------------------------------
        # Retention (Task 9 — §Retention + threat T7)
        # ------------------------------------------------------------------
        # Session ID scopes the on-disk retention directory. Two sensors
        # on the same machine with distinct session_ids will not collide,
        # and the shutdown hook only nukes *this* sensor's subtree.
        self._session_id = session_id or generate_operation_id("vis-sess")
        root = retention_root or str(
            Path.cwd() / _DEFAULT_RETENTION_ROOT
        )
        self._session_retention_dir: Path = Path(root) / self._session_id
        # TTL <= 0 selects "memory-only mode" — no disk writes at all.
        self._frame_ttl_s = float(
            frame_ttl_s if frame_ttl_s is not None else _DEFAULT_FRAME_TTL_S
        )
        self._last_ttl_purge_monotonic: float = 0.0

        # Shutdown hooks (atexit + SIGTERM). Opt-out for tests so each
        # test fixture doesn't leak a permanent atexit entry or clobber
        # the process-level signal handler.
        self._shutdown_hooks_registered = False
        self._prev_sigterm_handler: Any = None
        if register_shutdown_hooks:
            self._register_shutdown_hooks()

    # ------------------------------------------------------------------
    # Pure inner unit — testable without disk or router
    # ------------------------------------------------------------------

    async def _ingest_frame(self, frame: FrameData) -> Optional[IntentEnvelope]:
        """Classify *frame* and return an envelope, or ``None`` to drop.

        Order of operations:

        1. **Tier 0 hash dedup** — drop if ``frame.dhash`` was seen
           within ``hash_cooldown_s``.
        2. **Tier 1 OCR + regex** — run patterns on OCR text (empty
           when ``ocr_fn`` is unset).
        3. **Verdict mapping** — no hits → drop; hits → build
           :class:`VisionSignalEvidence` schema v1 and wrap in an
           :class:`IntentEnvelope`.

        This method does **not** call the router. Callers (scan_once)
        own the ingest side-effect.
        """
        self.stats.frames_polled += 1

        # Tier 0 dedup.
        now = time.monotonic()
        last_seen = self._recent_hashes.get(frame.dhash)
        if last_seen is not None and (now - last_seen) < self._hash_cooldown_s:
            self.stats.dropped_hash_dedup += 1
            self._consecutive_unchanged += 1
            return None
        # Fresh frame — remember its hash.
        self._recent_hashes[frame.dhash] = now
        # Opportunistic purge of expired entries so the dict doesn't
        # grow unbounded across long sessions.
        self._prune_hashes(now)

        # A change occurred — reset the adaptive throttle.
        self._consecutive_unchanged = 0

        # Tier 1 OCR + regex.
        ocr_text = ""
        if self._ocr_fn is not None:
            try:
                ocr_text = self._ocr_fn(frame.frame_path) or ""
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[VisionSensor] ocr_fn raised on %s: %s",
                    frame.frame_path, exc,
                )
                ocr_text = ""

        matched = _run_deterministic_patterns(ocr_text)
        verdict_meta = _classify_from_matches(matched)
        if verdict_meta is None:
            self.stats.dropped_no_match += 1
            return None

        # Retain the triggering frame so downstream Visual VERIFY (and
        # Orange-PR reviewers) read a stable path instead of Ferrari's
        # volatile ``/tmp/claude/latest_frame.jpg`` that gets overwritten
        # at 15 fps. Memory-only mode (TTL <= 0) and best-effort failure
        # both fall back to the volatile source path — the signal still
        # emits, only its ``frame_path`` differs.
        retained_path = self._retain_frame(frame)
        evidence_frame_path = retained_path or frame.frame_path

        # Build schema v1 evidence — raises if any field is malformed,
        # which the orchestrator would treat as a sensor bug. Preferred
        # over silent defaults per Invariant I1.
        evidence = build_vision_signal_evidence(
            frame_hash=frame.dhash,
            frame_ts=frame.ts,
            frame_path=evidence_frame_path,
            classifier_verdict=verdict_meta["classifier_verdict"],
            classifier_model="deterministic",
            classifier_confidence=1.0,
            deterministic_matches=tuple(matched),
            ocr_snippet=_truncate_snippet(ocr_text),
            severity=verdict_meta["severity"],
            app_id=frame.app_id,
            window_id=frame.window_id,
        )

        signature = (
            f"vision:{verdict_meta['classifier_verdict']}:"
            f"{frame.app_id or '-'}"
        )
        envelope = make_envelope(
            source=SignalSource.VISION_SENSOR.value,
            description=(
                f"vision-detected {verdict_meta['classifier_verdict']} "
                f"(matches: {','.join(matched)})"
            ),
            target_files=(),
            repo=self._repo,
            confidence=1.0,
            urgency=verdict_meta["urgency"],
            evidence={
                "signature": signature,
                "vision_signal": dict(evidence),
            },
            requires_human_ack=False,
        )
        self.stats.signals_emitted += 1
        return envelope

    def _prune_hashes(self, now: float) -> None:
        """Drop hashes whose cooldown has fully expired."""
        expired = [
            h for h, ts in self._recent_hashes.items()
            if (now - ts) >= self._hash_cooldown_s * 2
        ]
        for h in expired:
            self._recent_hashes.pop(h, None)

    # ------------------------------------------------------------------
    # Retention (Task 9)
    # ------------------------------------------------------------------

    def _retain_frame(self, frame: FrameData) -> Optional[str]:
        """Copy ``frame.frame_path`` into this sensor's retention directory.

        The retained copy's path is substituted for ``frame_path`` in the
        emitted evidence, so downstream consumers (Visual VERIFY) read a
        stable path under ``.jarvis/vision_frames/<session>/`` instead of
        the volatile Ferrari path which gets overwritten at 15 fps.

        Retention is best-effort: any filesystem error drops the copy
        with a DEBUG log and the evidence falls back to the volatile
        source path. The signal still emits.

        When ``_frame_ttl_s <= 0`` (memory-only mode), retention is
        skipped entirely — the evidence keeps the Ferrari path.

        Returns the retained path on success, or ``None`` when retention
        is skipped / failed.
        """
        if self._frame_ttl_s <= 0:
            return None
        try:
            self._session_retention_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.debug(
                "[VisionSensor] retention mkdir failed: %s",
                exc,
            )
            return None
        ext = os.path.splitext(frame.frame_path)[1] or ".jpg"
        target = self._session_retention_dir / f"{frame.dhash}{ext}"
        if target.exists():
            # Same dhash previously retained → idempotent reuse.
            return str(target)
        try:
            with open(frame.frame_path, "rb") as src:
                data = src.read()
            with open(target, "wb") as dst:
                dst.write(data)
        except (OSError, FileNotFoundError) as exc:
            logger.debug(
                "[VisionSensor] retention copy failed: %s",
                exc,
            )
            return None
        self.stats.frames_retained += 1
        return str(target)

    def _purge_expired_frames(self, *, now: Optional[float] = None) -> int:
        """Unlink retained frames older than ``_frame_ttl_s``.

        Uses file mtime (wall clock) for age comparison. Retention is a
        privacy measure, not a crypto primitive — wall-clock drift is
        acceptable.

        Returns the number of files removed. Missing / unreadable
        directory returns 0 without error.
        """
        if self._frame_ttl_s <= 0:
            return 0
        if not self._session_retention_dir.exists():
            return 0
        ref_wall = now if now is not None else time.time()
        removed = 0
        try:
            entries = list(self._session_retention_dir.iterdir())
        except OSError:
            return 0
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                age = ref_wall - entry.stat().st_mtime
            except OSError:
                continue
            if age >= self._frame_ttl_s:
                try:
                    entry.unlink()
                    removed += 1
                except OSError:
                    continue
        if removed:
            self.stats.frames_purged_ttl += removed
        return removed

    def _purge_session_dir_safe(self) -> int:
        """Shutdown purge — removes every retained file + the session dir.

        Guaranteed non-raising. Safe to call from ``atexit`` or a signal
        handler. Idempotent (subsequent calls against a missing dir
        return 0).
        """
        try:
            return self._purge_session_dir_impl()
        except Exception:  # noqa: BLE001
            # atexit / signal handlers must never bubble — a shutdown
            # error that takes down the whole process would be worse
            # than leaving retained frames on disk.
            return 0

    def _purge_session_dir_impl(self) -> int:
        if not self._session_retention_dir.exists():
            return 0
        removed = 0
        try:
            entries = list(self._session_retention_dir.iterdir())
        except OSError:
            return 0
        for entry in entries:
            try:
                if entry.is_file():
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        try:
            self._session_retention_dir.rmdir()
        except OSError:
            # Non-empty (subdir?) or permission issue — leave the dir,
            # but we did our best on the frame files.
            pass
        self.stats.frames_purged_shutdown += removed
        return removed

    # ------------------------------------------------------------------
    # Shutdown hooks
    # ------------------------------------------------------------------

    def _register_shutdown_hooks(self) -> None:
        """Install ``atexit`` + ``SIGTERM`` purge hooks.

        ``SIGTERM`` registration only succeeds in the main thread — in
        other threads (e.g. some test runners) ``signal.signal`` raises
        ``ValueError``. We silently skip that branch; atexit alone is
        already enough for the common cooperative-shutdown case.
        """
        atexit.register(self._purge_session_dir_safe)
        try:
            self._prev_sigterm_handler = _signal.signal(
                _signal.SIGTERM, self._on_sigterm,
            )
        except (ValueError, OSError, AttributeError):
            self._prev_sigterm_handler = None
        self._shutdown_hooks_registered = True

    def _on_sigterm(self, signum: int, frame: Any) -> None:
        """SIGTERM handler — purge, then re-dispatch to the prior handler.

        Must never raise. If the previous handler was the OS default, we
        re-raise SIGTERM via ``signal.raise_signal`` so the process still
        terminates. Otherwise we chain through whichever callable the
        previous handler was.
        """
        self._purge_session_dir_safe()
        prev = self._prev_sigterm_handler
        if callable(prev):
            try:
                prev(signum, frame)
            except Exception:  # noqa: BLE001
                pass
            return
        # Default handler (``SIG_DFL``) or ``SIG_IGN`` — re-dispatch so
        # the process actually terminates. ``raise_signal`` honours the
        # currently-installed handler, which at this point is ours, so
        # we unset it first to avoid recursion.
        try:
            _signal.signal(_signal.SIGTERM, _signal.SIG_DFL)
            _signal.raise_signal(_signal.SIGTERM)
        except (ValueError, OSError, AttributeError):
            pass

    # ------------------------------------------------------------------
    # Disk ingress — fail-closed when Ferrari is absent
    # ------------------------------------------------------------------

    def _read_frame(self) -> Optional[FrameData]:
        """Read the Ferrari frame + sidecar. Fails closed on absence.

        Returns ``None`` when either file is missing or the sidecar JSON
        is unparseable. Emits a rate-limited DEGRADED breadcrumb in the
        absence case so operators can spot the condition without a
        flood.

        **I8**: this method *only* reads files. It never spawns
        ``frame_server.py``, never calls Quartz/SCK APIs, and never
        imports capture modules.
        """
        frame_exists = os.path.exists(self._frame_path)
        meta_exists = os.path.exists(self._metadata_path)
        if not (frame_exists and meta_exists):
            self.stats.dropped_ferrari_absent += 1
            self._emit_degraded_breadcrumb()
            return None

        try:
            raw = open(self._metadata_path, "r", encoding="utf-8").read()
            meta = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug(
                "[VisionSensor] sidecar unreadable at %s: %s",
                self._metadata_path, exc,
            )
            self.stats.dropped_ferrari_absent += 1
            self._emit_degraded_breadcrumb()
            return None

        dhash = str(meta.get("dhash", "")).strip()
        ts = meta.get("ts")
        if not dhash or not isinstance(ts, (int, float)):
            logger.debug(
                "[VisionSensor] sidecar missing dhash/ts fields: %r", meta,
            )
            return None

        raw_app = meta.get("app_id")
        app_id = str(raw_app) if isinstance(raw_app, str) and raw_app else None
        raw_win = meta.get("window_id")
        window_id = int(raw_win) if isinstance(raw_win, int) and not isinstance(raw_win, bool) else None

        return FrameData(
            frame_path=self._frame_path,
            dhash=dhash,
            ts=float(ts),
            app_id=app_id,
            window_id=window_id,
        )

    def _emit_degraded_breadcrumb(self) -> None:
        """Log ``degraded reason=ferrari_absent`` at most once per 60s."""
        now = time.monotonic()
        if (
            self._last_degraded_log is not None
            and (now - self._last_degraded_log) < 60.0
        ):
            return
        self._last_degraded_log = now
        self.stats.degraded_ticks += 1
        logger.info(
            "[VisionSensor] degraded reason=ferrari_absent "
            "frame_path=%s metadata_path=%s",
            self._frame_path, self._metadata_path,
        )

    # ------------------------------------------------------------------
    # Public scan + lifecycle
    # ------------------------------------------------------------------

    async def scan_once(self) -> List[IntentEnvelope]:
        """Run one poll. Returns the envelopes produced and ingested."""
        frame = self._read_frame()
        if frame is None:
            return []

        envelope = await self._ingest_frame(frame)
        if envelope is None:
            return []

        try:
            await self._router.ingest(envelope)
        except Exception:
            logger.exception(
                "[VisionSensor] router.ingest raised for signal %s",
                envelope.signal_id,
            )
            return []
        return [envelope]

    async def start(self) -> None:
        self._running = True
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="vision_sensor_poll",
        )

    async def stop(self) -> None:
        self._running = False
        task = self._poll_task
        self._poll_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self.scan_once()
            except Exception:
                logger.exception("[VisionSensor] poll error")
            self._adjust_adaptive_interval()
            self._maybe_ttl_purge()
            try:
                await asyncio.sleep(self._current_poll_interval_s)
            except asyncio.CancelledError:
                break

    def _maybe_ttl_purge(self) -> None:
        """Trigger a TTL purge at most once per ``_TTL_PURGE_INTERVAL_S``.

        Called from the poll loop after every scan. Keeps disk usage
        bounded without adding a second background task.
        """
        if self._frame_ttl_s <= 0:
            return
        now = time.monotonic()
        if (now - self._last_ttl_purge_monotonic) < _TTL_PURGE_INTERVAL_S:
            return
        self._last_ttl_purge_monotonic = now
        try:
            self._purge_expired_frames()
        except Exception:  # noqa: BLE001
            logger.debug("[VisionSensor] TTL purge raised", exc_info=True)

    def _adjust_adaptive_interval(self) -> None:
        """Adaptive throttle: double interval after N static polls.

        Static means ``_consecutive_unchanged`` (incremented on every
        Tier 0 dedup hit and reset on any fresh frame) has crossed the
        threshold. Interval caps at ``_ADAPTIVE_MAX_INTERVAL_S`` (8s).
        Any change inside ``_ingest_frame`` resets the counter, which
        will push the interval back down to base on the next adjust.
        """
        if self._consecutive_unchanged >= _ADAPTIVE_STATIC_BEFORE_DOWNSHIFT:
            new_interval = min(
                self._current_poll_interval_s * 2.0,
                _ADAPTIVE_MAX_INTERVAL_S,
            )
            if new_interval != self._current_poll_interval_s:
                self._current_poll_interval_s = new_interval
        else:
            self._current_poll_interval_s = self._base_poll_interval_s

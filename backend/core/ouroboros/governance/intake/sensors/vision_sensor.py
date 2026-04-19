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
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

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

# Policy layer settings (spec §Policy Layer + Task 11).
_DEFAULT_FP_LEDGER_PATH = ".jarvis/vision_sensor_fp_ledger.json"
_DEFAULT_FP_BUDGET = float(os.environ.get("JARVIS_VISION_SENSOR_FP_BUDGET", "0.3"))
_DEFAULT_FP_WINDOW_SIZE = int(os.environ.get("JARVIS_VISION_SENSOR_FP_WINDOW", "20"))
_DEFAULT_FINDING_COOLDOWN_S = float(
    os.environ.get("JARVIS_VISION_SENSOR_FINDING_COOLDOWN_S", "120"),
)
# Default chain cap is ``1`` for Slices 1–2 entry per §Policy Layer →
# Cooldowns. Flipped to ``3`` only as part of Slice 2 graduation.
_DEFAULT_CHAIN_MAX = int(os.environ.get("JARVIS_VISION_CHAIN_MAX", "1"))
_DEFAULT_PENALTY_S = float(os.environ.get("JARVIS_VISION_SENSOR_PENALTY_S", "300"))
_CONSECUTIVE_FAILURES_PAUSE_THRESHOLD = 3

# Tier 2 VLM classifier settings (spec §Sensor Contract Tier 2 + Task 15).
_DEFAULT_TIER2_ENABLED = os.environ.get(
    "JARVIS_VISION_SENSOR_TIER2_ENABLED", "false",
).strip().lower() in ("1", "true", "yes", "on")
# Default cost per VLM call (Qwen3-VL-235B pricing).
_DEFAULT_TIER2_COST_USD = float(
    os.environ.get("JARVIS_VISION_TIER2_COST_USD", "0.005"),
)
# Daily cost cap per §Cost / Latency Envelope.
_DEFAULT_DAILY_COST_CAP_USD = float(
    os.environ.get("JARVIS_VISION_DAILY_COST_CAP_USD", "1.00"),
)
# Confidence threshold below which severity downgrades to info.
_DEFAULT_MIN_CONFIDENCE = float(
    os.environ.get("JARVIS_VISION_SENSOR_MIN_CONFIDENCE", "0.70"),
)
# Cost ledger path — one per working directory.
_DEFAULT_COST_LEDGER_PATH = ".jarvis/vision_cost_ledger.json"
# Cascade thresholds per §Cost / Latency Envelope.
_COST_DOWNSHIFT_THRESHOLD = 0.80   # Tier 2 VLM skipped; Tier 1 still runs
_COST_PAUSE_THRESHOLD = 0.95       # entire sensor pauses

# Operator-triggered cost-cascade bypass (``/vision boost <seconds>``).
# Spec §Cost / Latency Envelope: bounded to 300s, TTY-gated (REPL-only),
# disk-persisted, auto-expires. Used for "survival over cost" moments
# where the operator explicitly accepts the spend.
_BOOST_MAX_DURATION_S = 300.0

# VLM classifier recognised verdicts.
_VLM_VERDICTS_EMIT: frozenset = frozenset(
    {"bug_visible", "error_visible", "unclear"},
)
_VLM_VERDICT_OK = "ok"             # drop — no signal emitted

# ---------------------------------------------------------------------------
# T1 / T2 defenses — prompt injection + credential + app denylist
# ---------------------------------------------------------------------------
#
# Spec §Threat Model:
#   T1 — Prompt injection via screen text.
#   T2 — Credential leak via screenshot (three layers: hard-coded app
#        denylist + user-extensible FORBIDDEN_APP memory + OCR
#        credential-shape regex).

# Credential patterns mirror the last 5 entries of the semantic-firewall
# injection pattern set (sk-*, AKIA*, ghp_*, xox[bp]-*, PEM blocks). An
# OCR hit drops the whole frame; we never forward credential-shaped
# bytes to any downstream consumer.
_CREDENTIAL_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[bp]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
)

# Hard-coded app denylist — never overridable downward. Additional
# deny entries come from ``FORBIDDEN_APP`` memories (Task 4). Matches
# are case-insensitive on macOS bundle-id strings.
_HARDCODED_APP_DENYLIST: frozenset = frozenset({
    "com.1password.mac",
    "com.1password7.mac",
    "com.agilebits.onepassword",
    "com.agilebits.onepassword4",
    "com.agilebits.onepassword7",
    "com.bitwarden.desktop",
    "com.apple.keychainaccess",
    "com.apple.mobilesms",            # Messages
    "com.apple.mail",
    "com.apple.mailcompose",
    "org.whispersystems.signal-desktop",
})


# Outcome categories for the FP ledger.
OUTCOME_REJECTED = "rejected"              # FP
OUTCOME_APPLIED_GREEN = "applied_green"    # TP
OUTCOME_STALE = "stale"                    # FP
OUTCOME_UNCERTAIN = "uncertain"            # neither (dropped from rate calc)
_VALID_OUTCOMES = frozenset({
    OUTCOME_REJECTED, OUTCOME_APPLIED_GREEN, OUTCOME_STALE, OUTCOME_UNCERTAIN,
})
_FP_OUTCOMES = frozenset({OUTCOME_REJECTED, OUTCOME_STALE})
_TP_OUTCOMES = frozenset({OUTCOME_APPLIED_GREEN})

# Pause reasons
PAUSE_REASON_FP_BUDGET = "fp_budget_exhausted"
PAUSE_REASON_CHAIN_CAP = "chain_cap_exhausted"
PAUSE_REASON_CONSECUTIVE_FAILURES = "consecutive_failures"
PAUSE_REASON_MANUAL = "manual"
PAUSE_REASON_COST_CAP = "cost_cap_exhausted"
# Pauses that time out automatically (wall-clock deadline).
_AUTO_EXPIRING_PAUSE_REASONS = frozenset({PAUSE_REASON_CONSECUTIVE_FAILURES})


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
    dropped_schema_malformed: int = 0
    dropped_finding_cooldown: int = 0
    dropped_paused: int = 0
    dropped_app_denied: int = 0
    dropped_credential_shape: int = 0
    injection_sanitized: int = 0
    signals_emitted: int = 0
    degraded_ticks: int = 0
    frames_retained: int = 0
    frames_purged_ttl: int = 0
    frames_purged_shutdown: int = 0
    # Policy-layer counters
    outcomes_recorded: int = 0
    consecutive_failures: int = 0
    pause_events: int = 0
    chain_starts: int = 0
    # Tier 2 / cost counters
    tier2_calls: int = 0
    tier2_ok_dropped: int = 0               # VLM verdict=ok → no signal
    tier2_signals: int = 0                  # VLM-emitted signals
    tier2_skipped_disabled: int = 0
    tier2_skipped_tier1_matched: int = 0
    tier2_skipped_cost_downshift: int = 0
    tier2_skipped_dhash_dedup: int = 0
    tier2_exceptions: int = 0
    tier2_confidence_downgrades: int = 0
    cost_usd_today: float = 0.0
    cost_pause_events: int = 0


@dataclass(frozen=True)
class OutcomeEntry:
    """One row in the FP budget rolling window.

    ``ts`` is wall-clock (``time.time()``) so the ledger survives
    restart; monotonic timestamps would be meaningless across processes.
    """

    op_id: str
    outcome: str
    ts: float


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


def _utc_date_iso() -> str:
    """Return today's date in ``YYYY-MM-DD`` form in UTC.

    The cost ledger uses this for UTC-midnight rollover: a ledger
    whose ``utc_date`` differs from the current call's result is stale
    and its spend resets to zero.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ocr_has_credential_shape(text: str) -> bool:
    """Return ``True`` iff *text* matches any known credential pattern.

    T2c mitigation — an OCR payload carrying a credential-shaped token
    drops the entire frame. Never forward bytes we know to contain a
    secret shape to any downstream consumer.
    """
    if not text:
        return False
    for pat in _CREDENTIAL_PATTERNS:
        if pat.search(text):
            return True
    return False


def _is_app_on_hardcoded_denylist(app_id: Optional[str]) -> bool:
    """Return ``True`` iff *app_id* matches the hard-coded T2 denylist.

    Case-insensitive, whitespace-tolerant. The denylist covers credential
    managers, secure messengers, and mail clients — frames from these
    apps are dropped before OCR even runs, so no credential bytes ever
    enter the classifier.
    """
    if not app_id:
        return False
    norm = app_id.strip().lower()
    return norm in _HARDCODED_APP_DENYLIST


def _is_app_on_memory_denylist(app_id: Optional[str]) -> bool:
    """Return ``True`` iff *app_id* matches a user FORBIDDEN_APP memory.

    Consults the module-level provider hook installed by
    ``UserPreferenceStore._provide_protected_apps`` (Task 4). Silently
    returns ``False`` on any error — the hard-coded denylist still
    applies, so a broken memory provider doesn't open the attack
    surface.
    """
    if not app_id:
        return False
    try:
        from backend.core.ouroboros.governance.user_preference_memory import (
            get_protected_app_provider,
        )
        provider = get_protected_app_provider()
        if provider is None:
            return False
        forbidden = list(provider())
    except Exception:  # noqa: BLE001
        return False
    norm = app_id.strip().lower()
    for entry in forbidden:
        try:
            if str(entry).strip().lower() == norm:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _is_app_denied(app_id: Optional[str]) -> bool:
    """Combined T2 app-denylist predicate (hard-coded OR memory)."""
    return (
        _is_app_on_hardcoded_denylist(app_id)
        or _is_app_on_memory_denylist(app_id)
    )


def _sanitize_ocr_for_evidence(text: str) -> Tuple[str, bool]:
    """Pass OCR text through the semantic firewall.

    Returns ``(sanitized, injection_detected)``:

    * ``sanitized`` — the firewall's output (credentials redacted to
      ``[REDACTED]``, injection phrases *not* scrubbed by the
      firewall's own behavior). When the firewall reports an
      injection, we additionally overwrite the entire snippet with
      ``"[sanitized:prompt_injection_detected]"`` so downstream
      prompts can't carry the adversarial phrase even if a consumer
      forgets the untrusted-fence wrapper.
    * ``injection_detected`` — tells the caller whether to bump the
      ``injection_sanitized`` counter for observability.

    Falls through with ``(text, False)`` if the firewall import fails
    — the sensor keeps working against the rest of its defenses.
    """
    try:
        from backend.core.ouroboros.governance.semantic_firewall import (
            sanitize_for_firewall,
        )
    except Exception:  # noqa: BLE001
        return (text or "", False)
    result = sanitize_for_firewall(text or "", field_name="vision_ocr")
    if result.rejected:
        # Inspect reasons to distinguish injection from credential hits
        # — credential shapes were already dropped at the caller, but
        # length-cap rejections can also set rejected=True.
        injection = any(
            "injection pattern hit" in r for r in result.reasons
        )
        if injection:
            return ("[sanitized:prompt_injection_detected]", True)
    return (result.sanitized, False)


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
        ledger_path: Optional[str] = None,
        fp_budget: Optional[float] = None,
        fp_window_size: Optional[int] = None,
        finding_cooldown_s: Optional[float] = None,
        chain_max: Optional[int] = None,
        penalty_s: Optional[float] = None,
        vlm_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
        tier2_enabled: Optional[bool] = None,
        tier2_cost_usd: Optional[float] = None,
        daily_cost_cap_usd: Optional[float] = None,
        min_confidence: Optional[float] = None,
        cost_ledger_path: Optional[str] = None,
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
        # ``None`` sentinel (not 0.0) mirrors ``_last_degraded_log`` —
        # ``time.monotonic()`` can be small on recently-started processes
        # and a zero initializer would skip the first purge opportunity.
        self._last_ttl_purge_monotonic: Optional[float] = None

        # Shutdown hooks (atexit + SIGTERM). Opt-out for tests so each
        # test fixture doesn't leak a permanent atexit entry or clobber
        # the process-level signal handler.
        self._shutdown_hooks_registered = False
        self._prev_sigterm_handler: Any = None
        if register_shutdown_hooks:
            self._register_shutdown_hooks()

        # ------------------------------------------------------------------
        # Policy layer (Task 11 — spec §Policy Layer)
        # ------------------------------------------------------------------
        self._ledger_path: Path = Path(
            ledger_path
            or (Path.cwd() / _DEFAULT_FP_LEDGER_PATH)
        )
        self._fp_budget: float = (
            _DEFAULT_FP_BUDGET if fp_budget is None else float(fp_budget)
        )
        self._fp_window_size: int = (
            _DEFAULT_FP_WINDOW_SIZE if fp_window_size is None else int(fp_window_size)
        )
        self._finding_cooldown_s: float = (
            _DEFAULT_FINDING_COOLDOWN_S
            if finding_cooldown_s is None
            else float(finding_cooldown_s)
        )
        self._chain_max: int = (
            _DEFAULT_CHAIN_MAX if chain_max is None else int(chain_max)
        )
        self._penalty_s: float = (
            _DEFAULT_PENALTY_S if penalty_s is None else float(penalty_s)
        )

        # Rolling window of outcomes (newest-right). Capacity = window size.
        self._outcomes: Deque[OutcomeEntry] = deque(maxlen=self._fp_window_size)
        # Per-finding cooldown: ``"verdict|app|m1,m2"`` → wall-clock ``ts``.
        self._finding_cooldowns: Dict[str, float] = {}
        # Chain tracker: op_ids we've "started" as vision-originated this
        # session. Does NOT persist — every boot begins with a fresh chain.
        self._chain_started: set = set()
        # Consecutive rejected/stale streak (persisted — a pattern that
        # spans restarts is still worth suppressing).
        self._consecutive_failures: int = 0
        # Pause state (NOT persisted — §Policy Layer "fresh budget
        # window on next session boot"; operator gets a clean slate).
        self._paused: bool = False
        self._pause_reason: str = ""
        self._pause_until_ts: Optional[float] = None     # wall-clock deadline

        # Load persisted ledger (outcomes + finding cooldowns +
        # consecutive failure count) if it exists.
        self._load_ledger()

        # ------------------------------------------------------------------
        # Tier 2 VLM classifier (Task 15 — Slice 2)
        # ------------------------------------------------------------------
        self._vlm_fn = vlm_fn
        self._tier2_enabled: bool = (
            _DEFAULT_TIER2_ENABLED if tier2_enabled is None else bool(tier2_enabled)
        )
        self._tier2_cost_usd: float = (
            _DEFAULT_TIER2_COST_USD
            if tier2_cost_usd is None
            else float(tier2_cost_usd)
        )
        self._daily_cost_cap_usd: float = (
            _DEFAULT_DAILY_COST_CAP_USD
            if daily_cost_cap_usd is None
            else float(daily_cost_cap_usd)
        )
        self._min_confidence: float = (
            _DEFAULT_MIN_CONFIDENCE
            if min_confidence is None
            else float(min_confidence)
        )
        self._cost_ledger_path: Path = Path(
            cost_ledger_path
            or (Path.cwd() / _DEFAULT_COST_LEDGER_PATH)
        )
        # Last dhash the VLM classified — Tier 2 skips when the current
        # frame matches (avoids paying for the same screen twice in a
        # row; Tier 0 dhash dedup catches most but Tier 2 re-runs on
        # frames that escaped Tier 1 with zero hits).
        self._last_tier2_dhash: Optional[str] = None
        # Cost ledger fields — loaded from disk on construction,
        # persisted after each VLM call + UTC-rollover check.
        self._cost_today_usd: float = 0.0
        self._cost_ledger_date: str = _utc_date_iso()
        self._cost_ledger_calls: int = 0
        # Boost state — operator-triggered cost-cascade bypass via
        # ``/vision boost <seconds>`` (Task 21). Disk-persisted on the
        # cost ledger so a restart mid-boost respects the remaining
        # window. ``None`` = no boost active.
        self._boost_until_ts: Optional[float] = None
        self._load_cost_ledger()

    # ------------------------------------------------------------------
    # Pure inner unit — testable without disk or router
    # ------------------------------------------------------------------

    async def _ingest_frame(self, frame: FrameData) -> Optional[IntentEnvelope]:
        """Classify *frame* and return an envelope, or ``None`` to drop.

        Order of operations:

        0. **Pause gate** — if the sensor is paused (FP budget, chain
           cap, or penalty), drop immediately.
        1. **Tier 0 hash dedup** — drop if ``frame.dhash`` was seen
           within ``hash_cooldown_s``.
        2. **Tier 1 OCR + regex** — run patterns on OCR text (empty
           when ``ocr_fn`` is unset).
        3. **Finding cooldown** — drop if the same verdict+app+matches
           tuple fired within ``finding_cooldown_s``.
        4. **Verdict mapping + emit** — build :class:`VisionSignalEvidence`
           schema v1 and wrap in an :class:`IntentEnvelope`.

        This method does **not** call the router. Callers (scan_once)
        own the ingest side-effect.
        """
        # Pause gate — short-circuit before any work.
        if self.is_paused():
            self.stats.dropped_paused += 1
            return None

        # T2 app denylist — drop *before OCR* so credential-bearing
        # frames from sensitive apps never reach the classifier.
        if _is_app_denied(frame.app_id):
            self.stats.dropped_app_denied += 1
            logger.debug(
                "[VisionSensor] dropped frame — app denylist (app_id=%s)",
                frame.app_id,
            )
            return None

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

        # T2c credential-shape check — a hit drops the whole frame.
        # This runs on the raw OCR output (before firewall sanitation)
        # because the firewall redacts credentials to ``[REDACTED]``
        # which we specifically want to observe pre-redaction.
        if _ocr_has_credential_shape(ocr_text):
            self.stats.dropped_credential_shape += 1
            logger.debug(
                "[VisionSensor] dropped frame — credential shape in OCR "
                "(dhash=%s, app_id=%s)",
                frame.dhash, frame.app_id,
            )
            return None

        matched = _run_deterministic_patterns(ocr_text)
        verdict_meta = _classify_from_matches(matched)
        tier2_result: Optional[Dict[str, Any]] = None
        classifier_model = "deterministic"
        classifier_confidence = 1.0
        reasoning_snippet = ""

        if verdict_meta is not None:
            # Tier 1 deterministic hit — Tier 2 doesn't need to run.
            if self._tier2_enabled:
                self.stats.tier2_skipped_tier1_matched += 1
        else:
            # Tier 1 quiet — consider Tier 2 VLM classifier.
            tier2_result = await self._maybe_run_tier2(frame)
            if tier2_result is None:
                self.stats.dropped_no_match += 1
                return None
            verdict_meta = self._tier2_verdict_meta(
                verdict=tier2_result["verdict"],
                confidence=tier2_result["confidence"],
                min_confidence=self._min_confidence,
            )
            # Track confidence-downgrade separately from "normal" VLM emits.
            if (
                tier2_result["confidence"] < self._min_confidence
                and tier2_result["verdict"] in ("error_visible", "bug_visible")
            ):
                self.stats.tier2_confidence_downgrades += 1
            classifier_model = tier2_result["model"]
            classifier_confidence = tier2_result["confidence"]
            reasoning_snippet = tier2_result["reasoning"]
            self.stats.tier2_signals += 1

        # Finding cooldown — same verdict+app+match-set within the
        # window collapses. Unlike Tier 0 dedup (which is frame-hash
        # scoped), this catches repeat-detection of the *same issue*
        # across differently-hashed frames (scrolling, tooltip
        # flickers, etc.).
        verdict_name = verdict_meta["classifier_verdict"]
        matches_tuple = tuple(matched)
        if self._finding_cooldown_active(
            verdict=verdict_name, app_id=frame.app_id, matches=matches_tuple,
        ):
            self.stats.dropped_finding_cooldown += 1
            return None

        # Retain the triggering frame so downstream Visual VERIFY (and
        # Orange-PR reviewers) read a stable path instead of Ferrari's
        # volatile ``/tmp/claude/latest_frame.jpg`` that gets overwritten
        # at 15 fps. Memory-only mode (TTL <= 0) and best-effort failure
        # both fall back to the volatile source path — the signal still
        # emits, only its ``frame_path`` differs.
        retained_path = self._retain_frame(frame)
        evidence_frame_path = retained_path or frame.frame_path

        # T1 sanitization — pass OCR through the semantic firewall
        # before the text lands in ``evidence``. On injection, the
        # snippet is replaced with ``[sanitized:prompt_injection_detected]``
        # so an adversarial phrase cannot propagate even if a
        # downstream consumer forgets to wrap vision output in the
        # untrusted fence.
        #
        # For Tier 2 emits we fall back to the (already sanitized)
        # reasoning string — OCR may be empty or uninformative when
        # the VLM classified based on layout/visual features alone.
        if tier2_result is not None:
            snippet_source = reasoning_snippet or ocr_text
        else:
            snippet_source = ocr_text
        ocr_sanitized, had_injection = _sanitize_ocr_for_evidence(snippet_source)
        if had_injection:
            self.stats.injection_sanitized += 1

        # Build schema v1 evidence. ``build_vision_signal_evidence``
        # raises ``ValueError`` on any I1 violation (negative ts, bad
        # hash format, etc.) — Ferrari could in principle produce a
        # malformed sidecar (bug, schema drift, corruption). We fail
        # **closed** rather than loud: drop the frame, bump a counter,
        # never raise past the sensor boundary. The orchestrator sees
        # "no signal" which is the safe failure mode.
        try:
            evidence = build_vision_signal_evidence(
                frame_hash=frame.dhash,
                frame_ts=frame.ts,
                frame_path=evidence_frame_path,
                classifier_verdict=verdict_meta["classifier_verdict"],
                classifier_model=classifier_model,
                classifier_confidence=classifier_confidence,
                deterministic_matches=tuple(matched),
                ocr_snippet=_truncate_snippet(ocr_sanitized),
                severity=verdict_meta["severity"],
                app_id=frame.app_id,
                window_id=frame.window_id,
            )
        except ValueError as exc:
            self.stats.dropped_schema_malformed += 1
            logger.debug(
                "[VisionSensor] dropped frame with malformed evidence "
                "(dhash=%s): %s",
                frame.dhash, exc,
            )
            return None

        # Mark the finding cooldown now that we've decided to emit.
        self._mark_finding_emitted(
            verdict=verdict_name, app_id=frame.app_id, matches=matches_tuple,
        )
        self._persist_ledger()

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

    # ------------------------------------------------------------------
    # Policy layer (Task 11)
    # ------------------------------------------------------------------

    # ---- Ledger persistence ----

    def _load_ledger(self) -> None:
        """Load persisted FP ledger + finding cooldowns + failure streak.

        Silent on missing / corrupted files: the sensor starts with an
        empty state rather than crashing. Pause state is deliberately
        NOT loaded (§Policy Layer: "fresh budget window on next session
        boot").
        """
        try:
            raw = self._ledger_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        outcomes = data.get("outcomes")
        if isinstance(outcomes, list):
            for entry in outcomes[-self._fp_window_size:]:
                if not isinstance(entry, dict):
                    continue
                op_id = entry.get("op_id")
                outcome = entry.get("outcome")
                ts = entry.get("ts")
                if (
                    isinstance(op_id, str)
                    and outcome in _VALID_OUTCOMES
                    and isinstance(ts, (int, float))
                    and not isinstance(ts, bool)
                ):
                    self._outcomes.append(
                        OutcomeEntry(op_id=op_id, outcome=outcome, ts=float(ts))
                    )
        cooldowns = data.get("finding_cooldowns")
        if isinstance(cooldowns, dict):
            for key, ts in cooldowns.items():
                if (
                    isinstance(key, str)
                    and isinstance(ts, (int, float))
                    and not isinstance(ts, bool)
                ):
                    self._finding_cooldowns[key] = float(ts)
        cf = data.get("consecutive_failures")
        if isinstance(cf, int) and not isinstance(cf, bool) and cf >= 0:
            self._consecutive_failures = cf
            self.stats.consecutive_failures = cf

    def _persist_ledger(self) -> None:
        """Atomically persist ledger state. Best-effort — never raises."""
        try:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            data: Dict[str, Any] = {
                "outcomes": [asdict(o) for o in self._outcomes],
                "finding_cooldowns": dict(self._finding_cooldowns),
                "consecutive_failures": self._consecutive_failures,
                "last_updated_ts": time.time(),
            }
            tmp = self._ledger_path.with_suffix(self._ledger_path.suffix + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(str(tmp), str(self._ledger_path))
        except (OSError, TypeError, ValueError):
            logger.debug("[VisionSensor] ledger persist failed", exc_info=True)

    # ---- Finding cooldown ----

    @staticmethod
    def _finding_key(
        verdict: str, app_id: Optional[str], matches: Tuple[str, ...],
    ) -> str:
        """Canonical cooldown key for ``(verdict, app, sorted matches)``."""
        app = app_id or "-"
        matches_csv = ",".join(sorted(matches))
        return f"{verdict}|{app}|{matches_csv}"

    def _finding_cooldown_active(
        self,
        verdict: str,
        app_id: Optional[str],
        matches: Tuple[str, ...],
        *,
        now: Optional[float] = None,
    ) -> bool:
        """True when this verdict+app+match-set fired within the cooldown."""
        if self._finding_cooldown_s <= 0:
            return False
        key = self._finding_key(verdict, app_id, matches)
        last = self._finding_cooldowns.get(key)
        if last is None:
            return False
        ref = now if now is not None else time.time()
        return (ref - last) < self._finding_cooldown_s

    def _mark_finding_emitted(
        self,
        verdict: str,
        app_id: Optional[str],
        matches: Tuple[str, ...],
        *,
        now: Optional[float] = None,
    ) -> None:
        ref = now if now is not None else time.time()
        key = self._finding_key(verdict, app_id, matches)
        self._finding_cooldowns[key] = ref
        # Opportunistic GC — drop keys older than 2×cooldown so the dict
        # doesn't grow unbounded across long sessions.
        stale_cutoff = ref - (self._finding_cooldown_s * 2)
        stale = [k for k, t in self._finding_cooldowns.items() if t < stale_cutoff]
        for k in stale:
            self._finding_cooldowns.pop(k, None)

    # ---- FP budget ----

    def fp_rate(self) -> Optional[float]:
        """Return FP rate over the current rolling window, or ``None``.

        ``None`` when:

        * the window is not yet full (spec §Policy Layer specifies
          "rolling N-op window" — the rate only meaningfully exists
          once the window is populated; early tripping on tiny samples
          would pause the sensor on the first FP every boot);
        * the window holds zero FP-or-TP outcomes (only ``uncertain``).

        Callers that treat ``None`` as "don't pause" get the right
        behavior automatically.
        """
        if len(self._outcomes) < self._fp_window_size:
            return None
        fp = sum(1 for o in self._outcomes if o.outcome in _FP_OUTCOMES)
        tp = sum(1 for o in self._outcomes if o.outcome in _TP_OUTCOMES)
        total = fp + tp
        if total == 0:
            return None
        return fp / total

    # ---- Outcome intake + pause logic ----

    def record_outcome(self, *, op_id: str, outcome: str) -> None:
        """Record the outcome of a vision-originated op.

        Called by the orchestrator after an op reaches a terminal
        phase. Accepted values: ``"rejected"`` (FP), ``"applied_green"``
        (TP), ``"stale"`` (FP), ``"uncertain"`` (neither — doesn't
        contribute to the rate).

        Side effects:
        * Appends to the rolling window (drops oldest on overflow).
        * Updates the consecutive-failures streak.
        * May pause the sensor (FP budget / consecutive failures).
        * Persists the ledger to disk.
        """
        if outcome not in _VALID_OUTCOMES:
            raise ValueError(
                f"unknown outcome {outcome!r}; must be one of "
                f"{sorted(_VALID_OUTCOMES)}"
            )
        if not op_id:
            raise ValueError("record_outcome requires a non-empty op_id")
        self._outcomes.append(
            OutcomeEntry(op_id=op_id, outcome=outcome, ts=time.time())
        )
        self.stats.outcomes_recorded += 1

        # Update consecutive-failure streak.
        if outcome in _FP_OUTCOMES:
            self._consecutive_failures += 1
        elif outcome in _TP_OUTCOMES:
            self._consecutive_failures = 0
        # Uncertain outcomes neither bump nor reset.
        self.stats.consecutive_failures = self._consecutive_failures

        # Check FP-budget exhaustion first — it dominates (operator
        # intervention required) over the auto-expiring consecutive
        # failure penalty.
        rate = self.fp_rate()
        if rate is not None and rate > self._fp_budget:
            self._pause(reason=PAUSE_REASON_FP_BUDGET, duration_s=None)
        elif self._consecutive_failures >= _CONSECUTIVE_FAILURES_PAUSE_THRESHOLD:
            self._pause(
                reason=PAUSE_REASON_CONSECUTIVE_FAILURES,
                duration_s=self._penalty_s,
            )

        self._persist_ledger()

    # ---- Chain cap ----

    def record_chain_start(self, op_id: str) -> None:
        """Register a new vision-originated op in this session's chain.

        Pauses the sensor with ``chain_cap_exhausted`` once the chain
        length reaches ``chain_max``. Resume requires manual
        intervention (``/vision resume``) or next session boot.
        """
        if not op_id:
            raise ValueError("record_chain_start requires a non-empty op_id")
        if op_id in self._chain_started:
            return  # idempotent
        self._chain_started.add(op_id)
        self.stats.chain_starts += 1
        if len(self._chain_started) >= self._chain_max:
            self._pause(reason=PAUSE_REASON_CHAIN_CAP, duration_s=None)

    @property
    def chain_budget_remaining(self) -> int:
        """How many more vision-originated ops can start in this session."""
        return max(0, self._chain_max - len(self._chain_started))

    # ---- Pause / resume ----

    def _pause(self, *, reason: str, duration_s: Optional[float]) -> None:
        self._paused = True
        self._pause_reason = reason
        if duration_s is not None and duration_s > 0:
            self._pause_until_ts = time.time() + float(duration_s)
        else:
            self._pause_until_ts = None
        self.stats.pause_events += 1
        logger.info(
            "[VisionSensor] paused reason=%s pause_until_ts=%s",
            reason,
            f"{self._pause_until_ts:.1f}" if self._pause_until_ts else "manual",
        )

    def is_paused(self) -> bool:
        """True when the sensor is currently suppressing emissions.

        Auto-expiring pauses (``consecutive_failures``) check the
        wall-clock deadline on every call — once it passes, the sensor
        resumes itself transparently. Manual / budget pauses have no
        deadline and require ``resume()``.
        """
        if not self._paused:
            return False
        if (
            self._pause_reason in _AUTO_EXPIRING_PAUSE_REASONS
            and self._pause_until_ts is not None
            and time.time() >= self._pause_until_ts
        ):
            # Auto-resume — consecutive-failure penalty expired.
            self._paused = False
            self._pause_reason = ""
            self._pause_until_ts = None
            logger.info("[VisionSensor] auto-resumed after penalty expired")
            return False
        return True

    @property
    def paused(self) -> bool:
        """Public pause predicate (delegates to :meth:`is_paused`)."""
        return self.is_paused()

    @property
    def pause_reason(self) -> str:
        return self._pause_reason

    def resume(self) -> None:
        """Manually resume the sensor (``/vision resume`` REPL command).

        Clears the pause state and the chain tracker — the operator has
        attested that they want a fresh budget/chain window.
        """
        self._paused = False
        self._pause_reason = ""
        self._pause_until_ts = None
        self._chain_started.clear()
        logger.info("[VisionSensor] manually resumed")

    # ------------------------------------------------------------------
    # Tier 2 VLM classifier + cost ledger (Task 15)
    # ------------------------------------------------------------------

    def _load_cost_ledger(self) -> None:
        """Load today's spend from disk. Silent on missing / corrupt.

        Deliberately does NOT load pause state (§Policy Layer: operator
        gets a fresh budget window each session). The spend counter
        persists so a restart mid-day picks up where we left off.
        """
        try:
            raw = self._cost_ledger_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        stored_date = data.get("utc_date")
        stored_spend = data.get("spend_usd")
        stored_calls = data.get("vlm_calls")
        today = _utc_date_iso()
        if stored_date == today and isinstance(stored_spend, (int, float)):
            self._cost_today_usd = float(stored_spend)
            self._cost_ledger_date = today
            self.stats.cost_usd_today = self._cost_today_usd
            if isinstance(stored_calls, int) and not isinstance(stored_calls, bool):
                self._cost_ledger_calls = stored_calls
        # Different UTC day → ledger is stale, spend starts at 0.

        # Boost state — persists regardless of date rollover (a boost
        # started at 23:59 should survive midnight crossing for its
        # remaining window). Wall-clock-expiry handled by
        # :meth:`is_boost_active`.
        boost_ts = data.get("boost_until_ts")
        if isinstance(boost_ts, (int, float)) and not isinstance(boost_ts, bool):
            # Only restore if still in the future — a stale flag is
            # silently dropped.
            if boost_ts > time.time():
                self._boost_until_ts = float(boost_ts)

    def _persist_cost_ledger(self) -> None:
        """Atomic write of today's cost ledger. Best-effort."""
        try:
            self._cost_ledger_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "schema_version": 1,
                "utc_date": self._cost_ledger_date,
                "spend_usd": self._cost_today_usd,
                "vlm_calls": self._cost_ledger_calls,
                "boost_until_ts": self._boost_until_ts,
                "last_updated_ts": time.time(),
            }
            tmp = self._cost_ledger_path.with_suffix(
                self._cost_ledger_path.suffix + ".tmp",
            )
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(str(tmp), str(self._cost_ledger_path))
        except (OSError, TypeError, ValueError):
            logger.debug("[VisionSensor] cost ledger persist failed", exc_info=True)

    def _maybe_rollover_cost_ledger(self) -> None:
        """Reset the ledger at UTC midnight.

        Called before every cost-aware decision so an overnight session
        doesn't stay locked out once the day rolls over.
        """
        today = _utc_date_iso()
        if self._cost_ledger_date != today:
            logger.info(
                "[VisionSensor] cost ledger UTC rollover %s → %s "
                "(spend_reset from $%.4f to $0)",
                self._cost_ledger_date, today, self._cost_today_usd,
            )
            self._cost_ledger_date = today
            self._cost_today_usd = 0.0
            self._cost_ledger_calls = 0
            self.stats.cost_usd_today = 0.0
            self._persist_cost_ledger()
            # Clear a cost-cap pause if one is active — the new day
            # means the operator gets their fresh budget back.
            if self._paused and self._pause_reason == PAUSE_REASON_COST_CAP:
                self._paused = False
                self._pause_reason = ""
                self._pause_until_ts = None

    def _cost_fraction(self) -> float:
        """Current spend as a fraction of the daily cap."""
        if self._daily_cost_cap_usd <= 0:
            return 0.0
        return self._cost_today_usd / self._daily_cost_cap_usd

    def _cost_downshift_active(self) -> bool:
        """True when we should skip the VLM but keep Tier 1 running.

        Respects the operator's explicit ``/vision boost`` override —
        during a boost window the cost cascade is suppressed
        regardless of actual spend (spec §"survival over cost").
        """
        if self.is_boost_active():
            return False
        return self._cost_fraction() >= _COST_DOWNSHIFT_THRESHOLD

    # ------------------------------------------------------------------
    # /vision boost — operator-triggered cost-cascade bypass (Task 21)
    # ------------------------------------------------------------------

    def enable_boost(self, duration_s: float) -> float:
        """Enable the cost-cascade bypass for ``duration_s`` seconds.

        ``duration_s`` is clamped to ``[1.0, _BOOST_MAX_DURATION_S]`` —
        operators can't open the budget door indefinitely (the 300s
        ceiling is hard-coded per spec). Persisted to the cost ledger
        so a restart mid-boost honours the remaining window.

        Returns the effective duration granted (after clamp).

        If the sensor is currently paused for ``cost_cap_exhausted``,
        boost additionally clears that pause — the operator has
        explicitly accepted the spend.
        """
        clamped = max(1.0, min(float(duration_s), _BOOST_MAX_DURATION_S))
        self._boost_until_ts = time.time() + clamped
        # Clear a cost-cap pause if one is active.
        if self._paused and self._pause_reason == PAUSE_REASON_COST_CAP:
            self._paused = False
            self._pause_reason = ""
            self._pause_until_ts = None
        logger.info(
            "[VisionSensor] boost enabled duration_s=%.1f until_ts=%.1f",
            clamped, self._boost_until_ts,
        )
        self._persist_cost_ledger()
        return clamped

    def is_boost_active(self) -> bool:
        """True while the boost window is open."""
        return (
            self._boost_until_ts is not None
            and time.time() < self._boost_until_ts
        )

    def boost_remaining_s(self) -> float:
        """Seconds remaining in the current boost window (``0.0`` if
        inactive). Never raises."""
        if self._boost_until_ts is None:
            return 0.0
        remaining = self._boost_until_ts - time.time()
        return max(0.0, remaining)

    def _record_tier2_spend(self) -> None:
        """Add one VLM call to the ledger and trigger cascade if needed."""
        self._cost_today_usd += self._tier2_cost_usd
        self._cost_ledger_calls += 1
        self.stats.cost_usd_today = self._cost_today_usd
        # Cascade step 2 (95%): pause the sensor entirely — unless the
        # operator has explicitly opened a boost window accepting the
        # spend.
        if (
            self._cost_fraction() >= _COST_PAUSE_THRESHOLD
            and not self._paused
            and not self.is_boost_active()
        ):
            self.stats.cost_pause_events += 1
            self._pause(reason=PAUSE_REASON_COST_CAP, duration_s=None)
        self._persist_cost_ledger()

    async def _maybe_run_tier2(
        self,
        frame: FrameData,
    ) -> Optional[Dict[str, Any]]:
        """Run the VLM classifier if allowed; return verdict dict or ``None``.

        Return value is a dict with keys ``verdict`` / ``confidence`` /
        ``model`` / ``reasoning`` (sanitized). ``None`` means one of:
        Tier 2 disabled, no ``vlm_fn`` set, same-dhash dedup, cost
        downshift active, VLM raised, VLM returned malformed output.

        Never raises — callers treat ``None`` as "no VLM-emitted signal".
        """
        # Rollover check before any gate — new day may restore the budget.
        self._maybe_rollover_cost_ledger()

        if not self._tier2_enabled:
            self.stats.tier2_skipped_disabled += 1
            return None
        if self._vlm_fn is None:
            self.stats.tier2_skipped_disabled += 1
            return None
        # Same-dhash dedup — don't pay twice for the same screen.
        if self._last_tier2_dhash == frame.dhash:
            self.stats.tier2_skipped_dhash_dedup += 1
            return None
        # Cascade step 1 (80%): skip the VLM; Tier 1 still runs.
        if self._cost_downshift_active():
            self.stats.tier2_skipped_cost_downshift += 1
            return None

        # Call the VLM. Exceptions never bubble past the sensor.
        self.stats.tier2_calls += 1
        self._last_tier2_dhash = frame.dhash
        try:
            raw = self._vlm_fn(frame.frame_path)
        except Exception as exc:  # noqa: BLE001
            self.stats.tier2_exceptions += 1
            logger.debug("[VisionSensor] Tier 2 VLM raised: %s", exc)
            self._record_tier2_spend()   # we paid for the call even if it errored
            return None

        # Charge the call against the daily cap before inspecting output.
        self._record_tier2_spend()

        if not isinstance(raw, dict):
            logger.debug(
                "[VisionSensor] Tier 2 VLM returned non-dict: %r", type(raw).__name__,
            )
            return None

        verdict = str(raw.get("verdict", "")).strip().lower()
        if verdict == _VLM_VERDICT_OK:
            self.stats.tier2_ok_dropped += 1
            return None
        if verdict not in _VLM_VERDICTS_EMIT:
            logger.debug(
                "[VisionSensor] Tier 2 VLM unknown verdict=%r — dropping", verdict,
            )
            return None

        confidence_raw = raw.get("confidence")
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 0.0
        if not (0.0 <= confidence <= 1.0):
            confidence = max(0.0, min(1.0, confidence))

        model = str(raw.get("model", "qwen3-vl-235b"))[:64] or "qwen3-vl-235b"
        reasoning = str(raw.get("reasoning", ""))
        # T1 sanitization on VLM reasoning — same firewall as OCR.
        # We bump ``injection_sanitized`` here rather than at the evidence
        # step below because by then ``sanitized_reasoning`` is already
        # the fixed placeholder and the second sanitize-call wouldn't
        # see an injection to count.
        sanitized_reasoning, had_injection = _sanitize_ocr_for_evidence(reasoning)
        if had_injection:
            self.stats.injection_sanitized += 1

        return {
            "verdict": verdict,
            "confidence": confidence,
            "model": model,
            "reasoning": sanitized_reasoning,
        }

    @staticmethod
    def _tier2_verdict_meta(
        verdict: str, confidence: float, min_confidence: float,
    ) -> Dict[str, str]:
        """Map Tier-2 verdict + confidence → severity/urgency.

        Per spec §Severity → route, VLM-only signals always route
        ``BACKGROUND`` (low priority). Low confidence additionally
        downgrades severity to ``info``.
        """
        if verdict in ("error_visible", "bug_visible"):
            severity = "error" if verdict == "error_visible" else "warning"
        else:  # "unclear"
            severity = "info"
        if confidence < min_confidence:
            severity = "info"
        return {
            "classifier_verdict": verdict,
            "severity": severity,
            "urgency": "low",
        }

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
        # Policy gate first — a paused sensor doesn't even touch disk.
        if self.is_paused():
            self.stats.dropped_paused += 1
            return []
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
        if (
            self._last_ttl_purge_monotonic is not None
            and (now - self._last_ttl_purge_monotonic) < _TTL_PURGE_INTERVAL_S
        ):
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

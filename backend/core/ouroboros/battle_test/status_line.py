"""Glanceable one-line operator status for the Ouroboros battle-test CLI.

Closes UX Priority 2B: operators want a scannable one-liner of current
state. Target format (example):

    Phase: L2 Repair 2/8 · Cost: $0.22 / $0.50 · Idle: 847s / 2400s
    · Op: 019d9368 [complex·claude]

This module owns the data aggregation + format contract. The flowing
SerpentFlow CLI consumes it via the ``/status`` REPL command and via
event-driven receipt lines on op completion (UI Slices 5-6, 2026-04-30).
The legacy ``render_prompt_toolkit()`` path that fed a persistent
bottom toolbar is retired as of UI Slice 3 — see
``memory/project_move_2_closure.md`` for context on why fixed UI panels
were removed in favor of a pure flowing CLI.

Architectural mandates (matching stream_renderer / diff_preview):

  • **Pull model, no subscriptions** — builder holds weak refs to the
    ``CostTracker``, ``IdleWatchdog``, ``GovernedLoopService``,
    ``RepairEngine``. On each render call (~500ms via
    ``PromptSession(refresh_interval=…)``), it pulls current state,
    formats the one-liner, returns. No event wiring, no background task.
  • **Kill switch** — ``JARVIS_UI_STATUS_LINE_ENABLED`` (default on).
    When off, ``render()`` returns the empty string and SerpentFlow's
    toolbar falls back to its legacy verbose content.
  • **TTY gate** — same pattern as diff_preview / stream_renderer:
    non-TTY → skip rendering.
  • **Compact mode** — ``JARVIS_UI_STATUS_LINE_COMPACT=1`` drops
    route badge + op tail; keeps Phase + Cost + Idle.
  • **Super-beef extras** (all env-tunable):
        - Color gradient (green <50%, yellow 50-80%, red >80%) on
          Cost/Idle bars
        - Phase sub-detail (``L2 Repair 2/8``, ``GENERATE 47s``,
          ``APPLY mode=multi/4``, ``VALIDATE retry 1/2``)
        - Route + provider badge (``[complex·claude]`` / ``[bg·dw]``)
        - Multi-op indicator (``Op: 019d9368 (+2)``)
        - Proactive warnings inline at >80% cost / idle
        - 500ms refresh (``JARVIS_UI_STATUS_LINE_REFRESH_MS``)
        - Op-id truncation (last 10 chars)

Authority invariant: this module writes ONLY to the terminal's status
region. It does NOT mutate cost, idle timer, FSM state, risk tier,
cancel flag, or any governance surface. Pure read-only.
"""
from __future__ import annotations

import logging
import os
import sys
import time
import dataclasses
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.StatusLine")

_ENV_ENABLED = "JARVIS_UI_STATUS_LINE_ENABLED"
_ENV_COMPACT = "JARVIS_UI_STATUS_LINE_COMPACT"
_ENV_REFRESH_MS = "JARVIS_UI_STATUS_LINE_REFRESH_MS"
_ENV_WARN_PCT = "JARVIS_UI_STATUS_LINE_WARN_PCT"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def status_line_enabled() -> bool:
    """Master kill switch. Default: ON."""
    return os.environ.get(_ENV_ENABLED, "1").strip().lower() in _TRUTHY


def compact_mode_enabled() -> bool:
    """Compact layout gate. Default: OFF (full line)."""
    return os.environ.get(_ENV_COMPACT, "0").strip().lower() in _TRUTHY


def refresh_interval_s() -> float:
    """Refresh cadence used by the PromptSession. Default: 500ms."""
    try:
        ms = int(os.environ.get(_ENV_REFRESH_MS, "500"))
    except (TypeError, ValueError):
        ms = 500
    return max(0.1, min(5.0, ms / 1000.0))


def warn_threshold_pct() -> int:
    """Threshold above which Cost/Idle bars show the ⚠ marker. Default 80."""
    try:
        pct = int(os.environ.get(_ENV_WARN_PCT, "80"))
    except (TypeError, ValueError):
        pct = 80
    return max(1, min(99, pct))


# ---------------------------------------------------------------------------
# StatusSnapshot — immutable snapshot used by render()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusSnapshot:
    """Point-in-time aggregate of everything the one-liner shows.

    Held immutable so test cases can construct one by hand and exercise
    the rendering contract without booting the full harness.
    """

    # Phase + sub-detail
    phase: str = "IDLE"                # e.g. "GENERATE", "VALIDATE", "L2", "APPLY"
    phase_detail: str = ""             # e.g. "2/8" for L2, "47s" for elapsed
    # Cost
    cost_spent_usd: float = 0.0
    cost_budget_usd: float = 0.0
    #: WHY the budget is what it is — e.g. "observed — 98 sessions,
    #: p95=$0.24 x3". A ceiling without its basis is indistinguishable from
    #: an arbitrary constant, which is how an operator ends up asking where
    #: their own number came from.
    cost_budget_basis: str = ""
    #: How the cost chip should READ — "" | partial | unfunded | local.
    #: The ceiling is a policy cap; this says whether anything can spend
    #: against it, and is lane-specific rather than a blanket verdict.
    funding_mode: str = ""
    #: The lanes that are out (partial), or the serving endpoint (local).
    funding_label: str = ""
    # Idle window
    idle_elapsed_s: float = 0.0
    idle_timeout_s: float = 0.0
    # Active op
    primary_op_id: str = ""
    extra_op_count: int = 0            # >0 triggers "(+N)" suffix
    # Route / provider badge
    route: str = ""                    # "complex" / "standard" / "background" / ...
    provider: str = ""                 # "claude" / "dw" / "prime" / ""
    #: The MODEL, not the family. `provider` answers "which lane"; this
    #: answers "which brain" — `claude-sonnet-4-6`, `Qwen/Qwen3.5-397B-A17B`.
    #: Carried on `ctx.generation.model_id` since op_context.py:304 and read by
    #: nothing: the badge showed the lane, and `_statusline_payload` reported
    #: the LANE under the key `model`, so a CC-compatible script asking for
    #: `.model.id` got "claude" and never the model it was actually billed for.
    model: str = ""
    # Circadian liquidity (item #5) — populated ONLY when a provider's
    # declared token runway is exhausted (presentation restraint: the
    # healthy state renders nothing).
    liquidity_exhausted: bool = False
    liquidity_provider: str = ""       # first exhausted provider name
    liquidity_reset_s: Optional[float] = None


# ---------------------------------------------------------------------------
# StatusLineBuilder — aggregates live state → one-line render
# ---------------------------------------------------------------------------


_CUSTOM_SEGMENT_CACHE = {"at": 0.0, "text": ""}


def _custom_segment() -> str:
    """An operator-supplied status segment (CC's custom statusline):
    ``JARVIS_STATUSLINE_CMD`` names a command whose first stdout line is
    appended to the status line. Bounded (1s timeout, 80-char cap) and
    cached (``JARVIS_STATUSLINE_TTL_S``, default 10) — a slow script may
    lag the segment, never the repaint. NEVER raises."""
    try:
        cmd = os.environ.get("JARVIS_STATUSLINE_CMD", "").strip()
        if not cmd:
            return ""
        try:
            ttl = max(2.0, float(os.environ.get("JARVIS_STATUSLINE_TTL_S",
                                                "10")))
        except (TypeError, ValueError):
            ttl = 10.0
        now = time.monotonic()
        if now - _CUSTOM_SEGMENT_CACHE["at"] < ttl:
            return _CUSTOM_SEGMENT_CACHE["text"]
        # Stamp FIRST so a hanging script is retried at TTL cadence, not
        # on every repaint.
        _CUSTOM_SEGMENT_CACHE["at"] = now
        import subprocess
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=1.0,
            # THE contract, not just a pipe. Claude Code's status line
            # "receives JSON session data on stdin and displays whatever your
            # script prints", and scripts written against it parse that
            # object — so a runner that offered stdout-only would run
            # somebody's existing statusline and hand it nothing to read.
            #
            # Shaped like CC's payload rather than like our internals for the
            # same reason: the whole value of a status-line contract is that
            # a script written once works in both, and inventing a private
            # schema would make every such script ov-specific.
            input=_statusline_payload(),
        )
        line = (proc.stdout or "").strip().splitlines()
        text = line[0][:80] if line else ""
        _CUSTOM_SEGMENT_CACHE["text"] = text
        return text
    except Exception:  # noqa: BLE001
        return _CUSTOM_SEGMENT_CACHE.get("text", "")


def _statusline_payload(snapshot: Any = None) -> str:
    """The JSON a status-line script reads on stdin. NEVER raises.

    Mirrors Claude Code's documented shape — `model`, `workspace`, `cost`,
    `context_window`, `session_id` — so a script written for CC runs here
    unchanged. Keys ov cannot answer are OMITTED rather than faked: a script
    reading `.cost.total_cost_usd` gets a real number or nothing, never a
    zero that looks like a free session.

    Additive by the same rule the heartbeat follows: a consumer that has
    never heard of a key ignores it, and one expecting a key we do not send
    falls back the way it would against an older CC.
    """
    import json

    payload: Dict[str, Any] = {"schema_version": STATUS_LINE_SCHEMA_VERSION}
    try:
        import os as _os
        cwd = _os.getcwd()
        payload["cwd"] = cwd
        payload["workspace"] = {"current_dir": cwd, "project_dir": cwd}
    except Exception:  # noqa: BLE001
        pass
    try:
        snap = snapshot if snapshot is not None else _current_snapshot()
        if snap is not None:
            # `phase` and `route` have no CC counterpart and are the two
            # things an O+V operator most wants on a status line, so they
            # ride alongside rather than being contorted into CC's shape.
            payload["phase"] = getattr(snap, "phase", "") or "IDLE"
            # The qualifier travels WITH the phase or it does not travel at
            # all. "IDLE" and "IDLE, 17 sensors armed" are different claims,
            # and a payload carrying only the first forces every downstream
            # surface to render the ambiguous one.
            _detail = getattr(snap, "phase_detail", "") or ""
            if _detail:
                payload["phase_detail"] = _detail
            if getattr(snap, "route", ""):
                payload["route"] = snap.route
            # `.model.id` must be the MODEL. This reported `snap.provider`,
            # so a status-line script written against CC's documented shape
            # read "claude" where it expected "claude-sonnet-4-6" — a key that
            # answered a different question than the one it was named for.
            _model = getattr(snap, "model", "") or ""
            _prov = getattr(snap, "provider", "") or ""
            if _model or _prov:
                payload["model"] = {
                    "id": _model or _prov,
                    "display_name": _short_model(_model) or _prov,
                }
                if _prov:
                    payload["provider"] = _prov
            spent = getattr(snap, "cost_spent_usd", None)
            if spent is not None:
                cost: Dict[str, Any] = {"total_cost_usd": round(float(spent), 6)}
                budget = getattr(snap, "cost_budget_usd", None)
                if budget:
                    cost["budget_usd"] = round(float(budget), 6)
                payload["cost"] = cost
            if getattr(snap, "primary_op_id", ""):
                payload["session_id"] = snap.primary_op_id
    except Exception:  # noqa: BLE001
        pass
    try:
        return json.dumps(payload)
    except Exception:  # noqa: BLE001
        return "{}"


def _current_snapshot() -> Any:
    """The live snapshot, or None. NEVER raises."""
    try:
        builder = get_status_line_builder()
        return builder.snapshot() if builder is not None else None
    except Exception:  # noqa: BLE001
        return None


class StatusLineBuilder:
    """Pull-model aggregator for the glanceable status line.

    Holds references to the four live state sources (cost tracker, idle
    watchdog, GLS, repair engine). Any ref may be ``None`` — the builder
    degrades to sensible defaults (e.g. no ref → phase="IDLE").
    """

    def __init__(
        self,
        *,
        cost_tracker: Any = None,
        idle_watchdog: Any = None,
        governed_loop_service: Any = None,
        repair_engine: Any = None,
        intake_service: Any = None,
    ) -> None:
        self._cost = cost_tracker
        self._idle = idle_watchdog
        self._gls = governed_loop_service
        # The intake layer, so "IDLE" can stop meaning two opposite things.
        #
        # This builder used to sample ONLY the orchestrator's live FSM
        # contexts, and returned IDLE whenever there were none. But work
        # exists before an op does: sensors arm, sweep, and enqueue signals
        # for a minute or more before the first FSM context is created. An
        # operator watching a real boot saw `IDLE · $0.00` for two minutes
        # while four genuine test failures sat queued behind it, and
        # reasonably concluded the organism was broken.
        #
        # Read-only and zero-authority — the same pull model the attach
        # bridge's providers use. Consulted fresh at every sample so the
        # counts are current rather than cached at construction.
        self._intake = intake_service
        # Repair engine may be passed explicitly (tests, direct wiring)
        # OR resolved lazily from ``gls._orchestrator._config.repair_engine``
        # during each snapshot — preferred because the harness doesn't
        # hold the engine directly (it's owned by GLS / the orchestrator).
        self._repair_explicit = repair_engine

    def _resolve_repair_engine(self) -> Any:
        """Prefer explicit ref when provided; else walk GLS.

        Defensive: any attribute error returns None — missing repair
        engine just means the status line skips the L2-iter sub-detail.
        """
        if self._repair_explicit is not None:
            return self._repair_explicit
        if self._gls is None:
            return None
        try:
            orch = getattr(self._gls, "_orchestrator", None)
            if orch is None:
                return None
            cfg = getattr(orch, "_config", None)
            if cfg is None:
                return None
            return getattr(cfg, "repair_engine", None)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # Public API — snapshot + render
    # ------------------------------------------------------------------

    def snapshot(self) -> StatusSnapshot:
        """Sample current state from all refs and return an immutable snapshot.

        Never raises: any missing attribute / exception degrades to the
        field's default. The status line must never break the TUI even
        if the harness is mid-reload / mid-boot.
        """
        phase, phase_detail = self._sample_phase_and_detail()
        cost_spent, cost_budget = self._sample_cost()
        cost_basis = self._sample_cost_basis()
        idle_elapsed, idle_timeout = self._sample_idle()
        primary_op, extra_ops = self._sample_ops()
        route, provider, model = self._sample_route_and_provider(primary_op)
        liq_exhausted, liq_provider, liq_reset = self._sample_liquidity()
        funding_mode, funding_label = self._sample_funding()

        # §37 Slice 5 — feed cost-band-crossing observer.
        # Chatter-suppression is structural in the observer; this call
        # is safe to make every render tick (~500ms). Defensive:
        # observer NEVER raises; status-line never breaks on this.
        try:
            from backend.core.ouroboros.governance.cost_warning_observer import (
                get_default_observer,
            )
            get_default_observer().record(
                spent_usd=cost_spent,
                budget_usd=cost_budget,
            )
        except Exception:  # noqa: BLE001 — defensive
            pass

        return StatusSnapshot(
            phase=phase,
            phase_detail=phase_detail,
            cost_spent_usd=cost_spent,
            cost_budget_usd=cost_budget,
            cost_budget_basis=cost_basis,
            idle_elapsed_s=idle_elapsed,
            idle_timeout_s=idle_timeout,
            primary_op_id=primary_op,
            extra_op_count=extra_ops,
            route=route,
            provider=provider,
            model=model,
            liquidity_exhausted=liq_exhausted,
            liquidity_provider=liq_provider,
            liquidity_reset_s=liq_reset,
            funding_mode=funding_mode,
            funding_label=funding_label,
        )

    def render_plain(self) -> str:
        """Plain ANSI-free rendering for logs / unit tests."""
        if not status_line_enabled():
            return ""
        try:
            snap = self.snapshot()
            rendered = _format_plain(snap, compact=compact_mode_enabled())
            # The trust dial's chip — appended at THIS seam because every
            # surface (daemon toolbar, attach heartbeat, both cockpits)
            # mirrors this one line, so a Shift+Tab in any pane shows in
            # all of them within a heartbeat. Empty at the safe_auto
            # resting state so the line stays calm.
            try:
                from backend.core.ouroboros.governance.trust_repl import (
                    floor_chip,
                )
                chip = floor_chip()
                if chip:
                    rendered = f"{rendered} · {chip}" if rendered else chip
            except Exception:  # noqa: BLE001
                pass
            # Chat spend — silent until money moves, loud once it has.
            try:
                from backend.core.ouroboros.governance.chat_cost_breaker import (  # noqa: E501
                    chat_budget_chip,
                )
                chip2 = chat_budget_chip()
                if chip2:
                    rendered = (f"{rendered} · {chip2}" if rendered
                                else chip2)
            except Exception:  # noqa: BLE001
                pass
            custom = _custom_segment()
            if custom:
                rendered = f"{rendered} · {custom}" if rendered else custom
            return rendered
        except Exception:  # noqa: BLE001
            logger.debug(
                "[StatusLine] plain render failed", exc_info=True,
            )
            return ""

    # ------------------------------------------------------------------
    # Samplers — each guards against missing refs / missing attrs
    # ------------------------------------------------------------------

    def _sample_cost(self) -> tuple:
        if self._cost is None:
            return (0.0, 0.0)
        try:
            spent = float(getattr(self._cost, "total_spent", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            spent = 0.0
        try:
            budget = float(
                getattr(self._cost, "budget_usd", 0.0)
                or getattr(self._cost, "_budget_usd", 0.0)
                or 0.0
            )
        except Exception:  # noqa: BLE001
            budget = 0.0
        return (spent, budget)

    def _sample_cost_basis(self) -> str:
        """The basis the spawner recorded for this session's ceiling.

        Read from the environment rather than re-derived: the basis belongs to
        the decision that set the cap. Re-deriving would sample a different
        set of sessions and could explain the number with evidence that did
        not produce it. NEVER raises.
        """
        try:
            return str(os.environ.get(
                "OUROBOROS_BATTLE_COST_CAP_BASIS", "") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _sample_idle(self) -> tuple:
        if self._idle is None:
            return (0.0, 0.0)
        try:
            timeout = float(
                getattr(self._idle, "timeout_s", 0.0)
                or getattr(self._idle, "_timeout_s", 0.0)
                or 0.0
            )
        except Exception:  # noqa: BLE001
            timeout = 0.0
        # IdleWatchdog doesn't expose ``elapsed`` as a public property;
        # we compute it from ``_last_poke`` with defensive fallbacks.
        elapsed = 0.0
        try:
            last_poke = getattr(self._idle, "_last_poke", None)
            if last_poke is not None:
                elapsed = max(0.0, time.monotonic() - float(last_poke))
            else:
                # Try diagnostics snapshot if private field shape changed.
                diag = getattr(self._idle, "diagnostics", None)
                if diag is not None:
                    elapsed = float(
                        getattr(diag, "seconds_since_last_poke", 0.0) or 0.0
                    )
        except Exception:  # noqa: BLE001
            elapsed = 0.0
        return (elapsed, timeout)

    def _sample_ops(self) -> tuple:
        """Return (primary_op_id, extra_op_count).

        Picks the op whose FSM context was most recently advanced (proxy
        for "what the operator is watching"). ``extra_op_count`` is the
        number of additional in-flight ops.
        """
        if self._gls is None:
            return ("", 0)
        try:
            active = getattr(self._gls, "_active_ops", None) or set()
            fsm_contexts = getattr(self._gls, "_fsm_contexts", None) or {}
        except Exception:  # noqa: BLE001
            return ("", 0)

        if not active and not fsm_contexts:
            return ("", 0)

        # Prefer FSM-context ordering; pick the op with the largest
        # ``phase_entered_at`` (= most recent transition).
        primary: Optional[str] = None
        primary_ts: float = -1.0
        ids: List[str] = []
        for op_id, fsm_ctx in fsm_contexts.items():
            ids.append(op_id)
            try:
                pe = getattr(fsm_ctx, "phase_entered_at", None)
                # ``phase_entered_at`` is a datetime on OperationContext.
                # Comparison via .timestamp() — missing → skip.
                if pe is not None:
                    ts = float(pe.timestamp())
                    if ts > primary_ts:
                        primary_ts = ts
                        primary = op_id
            except Exception:  # noqa: BLE001
                pass

        if primary is None and ids:
            primary = ids[0]

        total = len(fsm_contexts) if fsm_contexts else len(active)
        extras = max(0, total - 1)
        return (primary or "", extras)

    def _sample_intake_state(self) -> tuple:
        """Return (phase, detail) describing the intake layer.

        The state BELOW the orchestrator, and the reason this exists: an op
        is the last thing to happen, not the first. Sensors register, sweep,
        and enqueue signals well before any FSM context is created, and until
        now every moment of that was rendered ``IDLE`` — the same word used
        for an organism with genuinely nothing to do.

        Precedence is most-actionable first, and every branch is decided by a
        live count rather than a clock. Nothing here waits, sleeps, or guesses
        at how long a boot "should" take:

          queued > 0        WORK EXISTS and no op has claimed it yet. This is
                            the state that was invisible: four real test
                            failures sat here for two minutes reading IDLE.
          sensors armed     Genuinely nothing to do — but SAY so, with the
                            count, so an operator can tell "quiet" from
                            "nothing is watching".
          nothing yet       Still arming. Distinguished from idle because a
                            sensor that has not registered cannot find
                            anything, and reporting that as idle is the
                            failure this whole change is about.

        Degrades to the historical ``("IDLE", "")`` whenever intake is absent
        or unreadable — a status line must never be the reason a cockpit
        raises.
        """
        # Resolved at SAMPLE time, never at construction — the same lazy
        # discipline this file already applies to the repair engine, and for
        # the same reason: the builder is constructed early in the boot and
        # the intake layer is assigned later. Capturing it in __init__ would
        # freeze a permanent None and this whole state machine would report
        # ARMING forever, which is a more confident lie than the IDLE it
        # replaces. A callable is therefore accepted as well as an object.
        intake = self._intake
        if callable(intake):
            try:
                intake = intake()
            except Exception:  # noqa: BLE001
                intake = None
        if intake is None:
            return ("IDLE", "")

        try:
            sensors = len(getattr(intake, "_sensors", ()) or ())
        except Exception:  # noqa: BLE001
            sensors = 0

        queued = None
        try:
            router = getattr(intake, "_router", None)
            depth = getattr(router, "intake_queue_depth", None)
            if callable(depth):
                queued = int(depth())
        except Exception:  # noqa: BLE001
            queued = None

        if queued:
            return ("QUEUED", f"{queued} signal{'' if queued == 1 else 's'}")
        if sensors:
            # Deliberately still "IDLE" — the word is correct here. What was
            # missing was the evidence beside it.
            return ("IDLE", f"{sensors} sensors")
        return ("ARMING", "")

    def _sample_phase_and_detail(self) -> tuple:
        """Return (phase, phase_detail).

        Sub-detail resolution order (first match wins):
          1. L2 Repair iteration (``repair_engine.is_running``)
          2. FSM phase name of the primary op + elapsed-in-phase
          3. Intake state — sensors arming / signals queued / idle-with-count
        """
        # L2 Repair has highest-priority detail — it's the only phase
        # where operators explicitly asked for an iter/max breakdown.
        _repair = self._resolve_repair_engine()
        if _repair is not None:
            try:
                if getattr(_repair, "is_running", False):
                    cur = int(
                        getattr(_repair, "current_iteration", 0) or 0
                    )
                    mx = int(
                        getattr(_repair, "max_iterations_live", 0) or 0
                    )
                    if mx > 0:
                        return ("L2 Repair", f"{cur}/{mx}")
                    return ("L2 Repair", str(cur) if cur else "")
            except Exception:  # noqa: BLE001
                pass

        if self._gls is None:
            return self._sample_intake_state()

        try:
            fsm_contexts = getattr(self._gls, "_fsm_contexts", None) or {}
        except Exception:  # noqa: BLE001
            fsm_contexts = {}
        if not fsm_contexts:
            return self._sample_intake_state()

        # Pick the most-recently-entered phase across all ops (same
        # selector as _sample_ops for consistency).
        primary_phase: str = ""
        primary_ts: float = -1.0
        primary_entered_at = None
        for fsm_ctx in fsm_contexts.values():
            try:
                pe = getattr(fsm_ctx, "phase_entered_at", None)
                phase_obj = getattr(fsm_ctx, "phase", None)
                if pe is None or phase_obj is None:
                    continue
                ts = float(pe.timestamp())
                if ts > primary_ts:
                    primary_ts = ts
                    primary_phase = _phase_label(phase_obj)
                    primary_entered_at = pe
            except Exception:  # noqa: BLE001
                continue

        if not primary_phase:
            return self._sample_intake_state()

        # Elapsed-in-phase sub-detail (compact, e.g. "47s"). Only show
        # for phases where "how long has this been running?" is useful —
        # GENERATE / VALIDATE / APPLY / VERIFY.
        detail = ""
        try:
            if primary_entered_at is not None and primary_phase in {
                "GENERATE", "VALIDATE", "APPLY", "VERIFY",
            }:
                from datetime import datetime, timezone
                elapsed_s = (
                    datetime.now(tz=timezone.utc) - primary_entered_at
                ).total_seconds()
                if elapsed_s >= 1.0:
                    detail = f"{int(elapsed_s)}s"
        except Exception:  # noqa: BLE001
            pass

        return (primary_phase, detail)

    def _sample_liquidity(self) -> tuple:
        """Which lane is dry, for the ``⚠ … dry`` token. NEVER raises.

        Asks :func:`economic_state.display_liquidity`, NOT
        `provider_liquidity_ledger.runway_exhausted`. That swap is the whole
        fix: `runway_exhausted` is a ROUTING predicate and fail-opens by
        design — it folds in `quota_exhausted`, which is `t < until`, a WINDOW
        test that lapses on a TTL so a topped-up wallet resumes routing without
        manual clearing. Correct for routing; catastrophic for a dashboard.
        Once both windows lapsed this returned ``(False, "", None)`` while
        Claude was answering 400 `credit balance too low` and DoubleWord 402,
        and the cockpit reported healthy lanes over two empty accounts. The
        ledger even still declared 5,000,000 tokens remaining, recorded before
        the money ran out, so the token path agreed.

        `display_liquidity` keeps a lapsed-but-unverified lane DRY, because
        time passing is not payment.

        FRACTIONAL, NOT BLANKET — the name returned is lane-specific whenever
        exactly one lane is out, so "doubleword dry" never becomes a claim
        about Anthropic. Only when EVERY known lane is dry does it collapse to
        one phrase, and that phrase says so rather than naming an arbitrary
        first offender.

        Master ``JARVIS_STATUS_LIQUIDITY_SEGMENT_ENABLED`` (default on).
        """
        try:
            if os.environ.get(
                "JARVIS_STATUS_LIQUIDITY_SEGMENT_ENABLED", "1",
            ).strip().lower() in ("0", "false", "no", "off"):
                return (False, "", None)
            from backend.core.ouroboros.governance.economic_state import (
                display_liquidity,
            )
            from backend.core.ouroboros.governance.provider_liquidity_ledger import (  # noqa: E501
                liquidity,
            )
            view = display_liquidity()
            dry = view.get("dry") or []
            if not dry:
                return (False, "", None)
            if len(dry) == 1:
                # ONE lane out — name it, even when it is the only lane the
                # ledger knows. "all lanes" is technically true of a
                # single-lane roster and useless: it hides the one fact the
                # operator needs, which is WHICH lane.
                label = str(dry[0])
            elif view.get("all_dry"):
                label = "all lanes"
            else:
                label = ", ".join(str(d) for d in dry)
            # The reset horizon is only meaningful for a single named lane;
            # two lanes recover on two clocks and one number would be a guess.
            secs = None
            if len(dry) == 1:
                try:
                    _tokens, secs = liquidity(dry[0])
                except Exception:  # noqa: BLE001
                    secs = None
            return (True, label, secs)
        except Exception:  # noqa: BLE001 — status line never breaks
            return (False, "", None)

    def _sample_funding(self) -> tuple:
        """``(mode, label)`` telling the cost chip what kind of number it is.

        The ceiling and the balance are different quantities, and the line was
        printing the first as though it were the second: ``$0.00/$0.71`` reads
        as "$0.71 of headroom" when the ceiling is a POLICY cap derived from
        spend HISTORY (p95 of prior sessions x3) and the actual spendable
        balance is zero. The cap still matters when lanes are funded — it is
        what stops a runaway session — so it is qualified here, never deleted.

        Modes, in the order they are decided:

          ``"local"``     every paid lane is dry BUT a sovereign local tier is
                          serving. Not paralysis: work continues at zero
                          marginal cost, and a ceiling denominated in dollars
                          is simply not the constraint any more.
          ``"unfunded"``  every paid lane is dry and nothing else can serve.
                          The ceiling is inert — say so.
          ``"partial"``   SOME lane is dry. The ceiling is still real, because
                          the funded lane can still spend against it. This is
                          the case a blanket "unfunded" would get wrong, and
                          it is why the decision is made over the lane ROSTER
                          rather than an any()/all() on a single boolean.
          ``""``          nothing to say; the chip renders unchanged.

        Local state is read through `CapabilityEvaluator._read_remote`, which
        is already the one non-blocking probe of the gateway (in-memory breaker
        state, no `resident_models()` round-trip) — this runs on the render
        path at ~500ms and must never wait on a LAN.
        """
        try:
            from backend.core.ouroboros.governance.economic_state import (
                display_liquidity,
            )
            view = display_liquidity()
            # The ECONOMIC subset, not the display union. A rate-limited lane
            # is dry — it earns the "⚠ … dry" token and its reset countdown —
            # but it is not UNFUNDED, and a ceiling qualified as unfunded on
            # the strength of a per-minute token cap would send an operator to
            # a billing page to fix a problem a clock fixes.
            econ = view.get("economic_dry") or []
            if not view.get("readable") or not econ:
                return ("", "")
            if not view.get("all_economic_dry"):
                # Fractional: name the lanes that are out of money, and leave
                # the ceiling alone — it is still spendable through the rest.
                return ("partial", ", ".join(econ))
            try:
                from backend.core.ouroboros.governance.capability_state import (
                    CapabilityEvaluator,
                )
                remote_state, endpoint, _readable = (
                    CapabilityEvaluator._read_remote())
            except Exception:  # noqa: BLE001
                remote_state, endpoint = "absent", ""
            if remote_state == "serving":
                return ("local", str(endpoint or "local"))
            return ("unfunded", "")
        except Exception:  # noqa: BLE001
            return ("", "")

    def _sample_route_and_provider(self, op_id: str) -> tuple:
        """Pull route + provider + MODEL for the primary op. All optional.

        `model_id` has ridden on the generation result since op_context.py:304
        ("provider model identifier; empty = not reported") and was read by
        nothing. An operator asking "which model am I running" could not find
        out from the cockpit, while every op carried the answer.
        """
        if not op_id or self._gls is None:
            return ("", "", "")
        try:
            fsm_contexts = getattr(self._gls, "_fsm_contexts", None) or {}
            ctx = fsm_contexts.get(op_id)
            if ctx is None:
                return ("", "", "")
            route = str(getattr(ctx, "provider_route", "") or "").lower()
            # Provider usually on ctx.generation.provider_name post-GENERATE.
            provider, model = "", ""
            gen = getattr(ctx, "generation", None)
            if gen is not None:
                provider = str(getattr(gen, "provider_name", "") or "").lower()
                # NOT lowercased: model ids are case-carrying identifiers
                # ("Qwen/Qwen3.5-397B-A17B-FP8"), and folding them would make
                # the badge disagree with the provider's own logs.
                model = str(getattr(gen, "model_id", "") or "").strip()
            return (route, provider, model)
        except Exception:  # noqa: BLE001
            return ("", "", "")


# ---------------------------------------------------------------------------
# Helpers — phase label, formatting, color thresholds
# ---------------------------------------------------------------------------


def _phase_label(phase_obj: Any) -> str:
    """Coerce an OperationPhase enum (or any object) into a short label."""
    try:
        name = getattr(phase_obj, "name", None)
        if name:
            return str(name)
        return str(phase_obj)
    except Exception:  # noqa: BLE001
        return "?"


def _cost_fraction(spent: float, budget: float) -> float:
    if budget <= 0:
        return 0.0
    return max(0.0, min(1.0, spent / budget))


def _idle_fraction(elapsed: float, timeout: float) -> float:
    if timeout <= 0:
        return 0.0
    return max(0.0, min(1.0, elapsed / timeout))


def _level_for_fraction(fraction: float) -> str:
    """Gradient level key: 'ok' (<50%) → 'warn' (50-80%) → 'hot' (>80%)."""
    if fraction >= (warn_threshold_pct() / 100.0):
        return "hot"
    if fraction >= 0.5:
        return "warn"
    return "ok"


def _short_op_id(op_id: str) -> str:
    if not op_id:
        return ""
    # Trim the suffix variants the orchestrator appends ("-cau", "-lse", etc.).
    core = op_id.split("-", 1)[1] if op_id.count("-") >= 1 else op_id
    # Show just the first prefix-chunk for scannability.
    return core.split("-", 1)[0] if "-" in core else core[:10]


def _format_phase(snap: StatusSnapshot) -> str:
    if snap.phase_detail:
        return f"{snap.phase} {snap.phase_detail}"
    return snap.phase or "IDLE"


def _short_model(model: str) -> str:
    """A model id trimmed to what an operator reads at a glance.

    ``Qwen/Qwen3.5-397B-A17B-FP8-dottxt`` in a one-line status bar costs more
    width than the rest of the line together, so the vendor prefix and the
    build suffixes go and the identity stays. Purely subtractive — no
    abbreviation table, because a table is a second place to update every time
    a model ships and the id itself already carries the name.
    """
    try:
        text = str(model or "").strip()
        if not text:
            return ""
        text = text.rsplit("/", 1)[-1]          # drop the vendor namespace
        # Trailing build/quantisation markers carry no identity at a glance.
        for suffix in ("-dottxt", "-FP8", "-fp8", "-instruct", "-Instruct"):
            if text.endswith(suffix):
                text = text[: -len(suffix)]
        return text[:28]
    except Exception:  # noqa: BLE001
        return ""


def _format_badge(route: str, provider: str, model: str = "") -> str:
    """Compact route·provider·model badge. Empty when none present.

    ``model`` is additive and last: the lane answers "where did this go", the
    model answers "what actually ran", and an operator watching a 3-tier
    cascade needs both — `[std·claude]` cannot distinguish a sonnet fallback
    from an opus one. Optional so every existing caller keeps working.
    """
    if not route and not provider and not model:
        return ""
    # Abbreviate long route names.
    route_abbrev = {
        "immediate": "imm",
        "standard": "std",
        "complex": "complex",
        "background": "bg",
        "speculative": "spec",
    }.get(route, route)
    prov_abbrev = {
        "claude": "claude",
        "doubleword": "dw",
        "dw": "dw",
        "prime": "prime",
        "j-prime": "prime",
    }.get(provider, provider)
    short = _short_model(model)
    # Suppressed when it merely repeats the lane — `[std·claude·claude]` is
    # noise, and a badge that repeats itself trains the eye to skip it.
    if short and prov_abbrev and short.lower() == prov_abbrev.lower():
        short = ""
    parts = [p for p in (route_abbrev, prov_abbrev, short) if p]
    return "[" + "·".join(parts) + "]" if parts else ""


# ---------------------------------------------------------------------------
# Render backends
# ---------------------------------------------------------------------------


def _format_plain(snap: StatusSnapshot, *, compact: bool) -> str:
    """Plain-text rendering for tests / non-TTY logs.

    Gap #7 Slice 2 (2026-05-04): when phase is IDLE and presentation
    restraint is enabled, return the compact breadcrumb format
    (``IDLE · main · $0.04/$0.50 · EXPLORE``) instead of the verbose
    ``Phase: IDLE · Cost: $0.00 / $0.50 · Idle: 0s / 0s``. Operators
    always see at-a-glance state without the full label noise.
    """
    # Gap #7 Slice 2 — idle breadcrumb (master-flag-gated)
    try:
        from backend.core.ouroboros.battle_test.presentation_restraint import (
            format_idle_breadcrumb,
            is_restraint_enabled,
        )
        if (
            is_restraint_enabled()
            and isinstance(snap.phase, str)
            and snap.phase.upper() in ("IDLE", "")
        ):
            crumb = format_idle_breadcrumb(
                cost_spent=snap.cost_spent_usd,
                cost_budget=snap.cost_budget_usd,
                op_id=snap.primary_op_id or "",
                # The sampler already worked out WHY it is idle — how many
                # sensors are armed, or that nothing has registered yet.
                # Not forwarding it meant the breadcrumb re-asserted a bare
                # "IDLE" over an answer that had already been computed.
                detail=getattr(snap, "phase_detail", "") or "",
                # The ceiling is a POLICY cap, not a balance. Without this
                # the chip renders `$0.00/$0.71` over two empty accounts.
                funding=getattr(snap, "funding_mode", "") or "",
                funding_label=getattr(snap, "funding_label", "") or "",
            )
            # A dry provider runway is MOST relevant while idle — the
            # organism may be idle BECAUSE it is dry. The one exception
            # to the breadcrumb's minimalism.
            if snap.liquidity_exhausted:
                tok = f"⚠ {snap.liquidity_provider or 'provider'} dry"
                if (
                    snap.liquidity_reset_s is not None
                    and snap.liquidity_reset_s > 0
                ):
                    tok += f" ~{max(1, int(snap.liquidity_reset_s / 60))}m"
                crumb = f"{crumb} · {tok}" if crumb else tok
            return crumb
    except Exception:  # noqa: BLE001 — defensive
        pass

    cost_fr = _cost_fraction(snap.cost_spent_usd, snap.cost_budget_usd)
    idle_fr = _idle_fraction(snap.idle_elapsed_s, snap.idle_timeout_s)
    parts: List[str] = []

    # §38 Slice 1 (PRD v2.57→v2.58, 2026-05-07) — posture
    # mood-ring badge in LEAD position. Posture is O+V's most
    # unique signal (CC structurally cannot replicate it);
    # putting it first makes the differentiation visually
    # omnipresent. Master-flag-gated; pre-§38-Slice-1 byte-
    # identical render preserved when off.
    if not compact:
        try:
            posture_tok = _format_posture_badge_token()
            if posture_tok:
                parts.append(posture_tok)
        except Exception:  # noqa: BLE001 — defensive
            pass

    phase_txt = _format_phase(snap)
    parts.append(f"Phase: {phase_txt}")

    # §38 Slice 2 (PRD v2.58→v2.59, 2026-05-07) — pipeline
    # progress bar appended after the phase label. Renders the
    # 11-phase deterministic FSM position uniquely (CC's loose
    # stages structurally cannot match this granularity).
    # Master-flag-gated; pre-§38-Slice-2 byte-identical when
    # off.
    if not compact:
        try:
            progress_tok = _format_pipeline_progress_token(
                phase=snap.phase,
            )
            if progress_tok:
                parts.append(progress_tok)
        except Exception:  # noqa: BLE001 — defensive
            pass

    cost_txt = f"Cost: ${snap.cost_spent_usd:.2f} / ${snap.cost_budget_usd:.2f}"
    if cost_fr >= (warn_threshold_pct() / 100.0):
        cost_txt += " ⚠"
    parts.append(cost_txt)

    idle_txt = (
        f"Idle: {int(snap.idle_elapsed_s)}s / {int(snap.idle_timeout_s)}s"
    )
    if idle_fr >= (warn_threshold_pct() / 100.0):
        idle_txt += " ⚠"
    parts.append(idle_txt)

    # Circadian liquidity (item #5) — renders ONLY when a provider's
    # declared runway is dry (restraint: healthy = invisible). Reset
    # horizon in minutes when the provider declared one.
    if snap.liquidity_exhausted:
        liq_txt = f"⚠ {snap.liquidity_provider or 'provider'} dry"
        if snap.liquidity_reset_s is not None and snap.liquidity_reset_s > 0:
            liq_txt += f" ~{max(1, int(snap.liquidity_reset_s / 60))}m"
        parts.append(liq_txt)

    if not compact and snap.primary_op_id:
        op_txt = f"Op: {_short_op_id(snap.primary_op_id)}"
        if snap.extra_op_count > 0:
            op_txt += f" (+{snap.extra_op_count})"
        parts.append(op_txt)

    if not compact:
        badge = _format_badge(snap.route, snap.provider)
        if badge:
            parts.append(badge)

    # Phase 1 (PRD §37 v2.53→v2.54, 2026-05-07) — operation
    # mode + hotkey legend. Composes canonical sources:
    #
    #   * Mode token from operation_mode.current_mode() — single
    #     source of truth for PLAN/ANALYZE/APPLY/AUTO axis.
    #     Gated on operation_mode.master_enabled() so mode-master-
    #     off renders byte-identical to pre-Phase-1.
    #   * Hotkey legend from keybinding_registry.format_footer_-
    #     legend() — single source of truth for operator-visible
    #     bindings; AST-pinned no-hardcoded-strings.
    #
    # Compact mode skips both (matches pre-existing compact
    # behavior — minimum-noise breadcrumb only).
    if not compact:
        try:
            mode_tok = _format_mode_token()
            if mode_tok:
                parts.append(mode_tok)
        except Exception:  # noqa: BLE001 — defensive
            pass
        try:
            thinking_tok = _format_thinking_token(
                op_id=snap.primary_op_id,
            )
            if thinking_tok:
                parts.append(thinking_tok)
        except Exception:  # noqa: BLE001 — defensive
            pass
        try:
            legend_tok = _format_hotkey_legend()
            if legend_tok:
                parts.append(legend_tok)
        except Exception:  # noqa: BLE001 — defensive
            pass

    return " · ".join(parts)


def _format_mode_token() -> str:
    """Compose ``operation_mode.current_mode()`` into a footer
    token. NEVER raises — failure returns empty string.

    Renders ``mode:plan`` / ``mode:analyze`` / ``mode:apply`` /
    ``mode:auto``. Master-off → empty (no token surfaces;
    pre-Phase-1 byte-identical render preserved).
    """
    try:
        from backend.core.ouroboros.governance.operation_mode import (  # noqa: E501
            current_mode,
            master_enabled,
        )
        if not master_enabled():
            return ""
        mode = current_mode()
        # mode is OperationMode enum; .value is the canonical
        # short string used by /mode REPL verb + AST pins.
        return f"mode:{mode.value}"
    except Exception:  # noqa: BLE001 — defensive
        return ""


def _format_hotkey_legend(*, max_entries: int = 4) -> str:
    """Compose ``keybinding_registry.format_footer_legend()``.
    NEVER raises. Returns empty string when registry is empty
    OR seeding failed."""
    try:
        from backend.core.ouroboros.governance.keybinding_registry import (  # noqa: E501
            format_footer_legend,
        )
        return format_footer_legend(max_entries=max_entries)
    except Exception:  # noqa: BLE001 — defensive
        return ""


def _format_pipeline_progress_token(*, phase: Any) -> str:
    """Compose ``pipeline_progress.format_pipeline_progress(phase)``.
    NEVER raises. Returns empty string when:

      * Master flag off (JARVIS_PIPELINE_PROGRESS_BAR_ENABLED)
      * phase is empty / not in canonical forward-flow

    §38 Slice 2 (PRD v2.58→v2.59, 2026-05-07) — composes
    canonical `governance/pipeline_progress.format_pipeline_-
    progress` which in turn composes
    `op_context.OperationPhase` (the canonical phase enum) +
    `_FORWARD_FLOW_PHASE_NAMES` (11-phase canonical pipeline,
    AST-pinned subset of OperationPhase).

    Phase string from ``StatusSnapshot.phase`` flows in as a
    string (e.g., ``"GENERATE"``); the aggregator's
    ``phase_index`` accepts strings via the same defensive
    coercion as ``palette_for_posture``."""
    try:
        if not phase:
            return ""
        from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
            format_pipeline_progress,
        )
        return format_pipeline_progress(
            phase=phase,
            show_phase_name=False,  # phase already shown by _format_phase
            show_position=True,
        )
    except Exception:  # noqa: BLE001 — defensive
        return ""


def _format_posture_badge_token() -> str:
    """Compose the lead-position posture badge into a status-line
    token. NEVER raises. Returns empty string when:
      * both master flags off
        (JARVIS_POSTURE_MOOD_RING_ENABLED and
        JARVIS_POSTURE_AURORA_ENABLED)
      * posture store unwired (boot incomplete / test harness)
      * no current reading on disk

    §37 Tier 2 (PRD §37.7, 2026-05-10) — when
    ``JARVIS_POSTURE_AURORA_ENABLED`` is on, composes
    :func:`posture_aurora.format_posture_aurora_badge` which adds
    confidence-band intensity modulation on top of the canonical
    palette. The aurora variant returns Rich markup
    (``[success]EXPLORE[/success]``) so the
    plain-text status line embeds the markup directly — Rich
    consumers render the color, plain stdout sees the brackets
    as visual decoration. Aurora flag default-FALSE.

    §38 Slice 1 (PRD v2.57→v2.58, 2026-05-07) — fallback
    composes canonical
    `governance/posture_palette.format_posture_badge` which in
    turn composes `posture_repl._default_store` →
    `PostureStore.load_current()` → `PostureReading.posture`.
    The fallback emits plain text (no markup) — graduated
    behavior preserved verbatim."""
    # Aurora path — confidence-modulated badge with Rich markup.
    try:
        from backend.core.ouroboros.governance.posture_aurora import (  # noqa: E501
            aurora_enabled,
            format_posture_aurora_badge,
        )
        if aurora_enabled():
            aurora_token = format_posture_aurora_badge(plain=False)
            if aurora_token:
                return aurora_token
            # Aurora master is on but no reading available — fall
            # through to canonical badge (which will also return
            # empty in that case, but the contract is the same).
    except Exception:  # noqa: BLE001 — defensive
        pass
    # Canonical path — graduated palette badge (plain text).
    try:
        from backend.core.ouroboros.governance.posture_palette import (  # noqa: E501
            format_posture_badge,
        )
        return format_posture_badge(plain=True)
    except Exception:  # noqa: BLE001 — defensive
        return ""


def _format_thinking_token(*, op_id: str) -> str:
    """Compose ``thinking_progress_aggregator`` for the given
    op_id. NEVER raises. Returns empty string when:
      * master flag off
      * op_id is empty / unknown
      * no active THINKING frame for the op

    Phase 2 (PRD §37 v2.54→v2.55, 2026-05-07)."""
    try:
        if not op_id:
            return ""
        from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
            master_enabled,
            get_default_observer,
            format_thinking_line,
        )
        if not master_enabled():
            return ""
        observer = get_default_observer()
        # update() composes the canonical sources and stores the
        # snapshot. Returns (snapshot, sse_eligible). We use the
        # snapshot directly; SSE publish is handled separately by
        # observer consumers (orchestrator hooks).
        snapshot, _ = observer.update(op_id=op_id)
        if snapshot is None:
            return ""
        return format_thinking_line(snapshot)
    except Exception:  # noqa: BLE001 — defensive
        return ""


# NOTE: ``_format_html`` (prompt_toolkit HTML rendering for the legacy
# bottom_toolbar) was retired in UI Slice 3 (2026-04-30) along with the
# ``render_prompt_toolkit`` method. The plain text formatter above is
# the sole renderer; consumers (Slice 5 ``/status`` REPL command,
# Slice 6 op-completion receipts) format inline with Rich markup at
# their own emission seam — keeping status_line.py free of any
# specific terminal-rendering library beyond stdlib.


# ---------------------------------------------------------------------------
# TTY gate + module-level singleton
# ---------------------------------------------------------------------------


def should_render() -> bool:
    """Combined gate: env enabled + stdout is a real TTY.

    Gap #7 Slice 2 fix (2026-05-04): check ``sys.__stdout__`` (the
    unpatched original) instead of ``sys.stdout``.
    ``prompt_toolkit.patch_stdout(raw=True)`` (active during the REPL
    main loop) replaces ``sys.stdout`` with a non-TTY proxy — the
    legacy isatty() check on the proxy returned False even on real
    interactive terminals, which is why the Gap #1+5 live status line
    never surfaced during normal operation. ``sys.__stdout__`` is
    Python's saved reference to the original stdout, untouched by
    patch_stdout. Falls back to ``sys.stdout`` only when ``__stdout__``
    is None (rare: Windows pythonw, daemonized processes).
    """
    if not status_line_enabled():
        return False
    try:
        from backend.core.ouroboros.battle_test.presentation_restraint import (
            real_stdout_isatty,
        )
        return real_stdout_isatty()
    except ImportError:
        # Fallback to legacy behavior if presentation_restraint is
        # somehow unavailable (e.g. partial install). Still better
        # than crashing the render path.
        try:
            return bool(sys.stdout.isatty())
        except Exception:  # noqa: BLE001
            return False


_DEFAULT_BUILDER: Optional[StatusLineBuilder] = None


def register_status_line_builder(builder: Optional[StatusLineBuilder]) -> None:
    """Harness calls this at boot once CostTracker / IdleWatchdog / GLS /
    RepairEngine are all constructed. SerpentFlow's toolbar looks up
    via :func:`get_status_line_builder`."""
    global _DEFAULT_BUILDER
    _DEFAULT_BUILDER = builder


def get_status_line_builder() -> Optional[StatusLineBuilder]:
    return _DEFAULT_BUILDER


def reset_status_line_builder() -> None:
    """Clear the singleton. Primarily for tests."""
    global _DEFAULT_BUILDER
    _DEFAULT_BUILDER = None


# ===========================================================================
# Transport + width adaptation — the cockpit mount (2026-07-28)
# ===========================================================================
#
# `StatusLineBuilder` is a daemon singleton, and the surface that most needs
# to draw it is `ov attach` — a DIFFERENT PROCESS whose builder is empty by
# construction. Rendering the client's own would draw an eternally idle
# organism, which looks exactly like a healthy quiet one.
#
# `StatusSnapshot` is already a frozen dataclass of plain fields, and
# `_format_plain` is already a pure function over it. So the split costs a
# serializer and a rehydrator, and BOTH processes keep calling the one
# renderer — no second opinion about what a status line looks like.
#
# The other half of mounting it is width. `_format_plain` composes up to ten
# `·`-joined segments and has never known how wide the terminal is: on the
# daemon's `bottom_toolbar` prompt_toolkit clipped it, but a fixed-height
# cockpit row with `wrap_lines=False` truncates mid-token, which reads as a
# corrupted line rather than an abbreviated one.

STATUS_LINE_SCHEMA_VERSION = "status_line.v1"

#: Segment KEEP-priority. Higher survives longer.
#:
#: By meaning, not by position, and expressed as an allowlist of things worth
#: keeping rather than a denylist of things to drop. The first draft guessed
#: prefixes for the hotkey legend, guessed wrong, and at 120 columns shed
#: `Phase:` while keeping `enter to submit` — the headline lost to a hint.
#:
#: An UNRECOGNISED segment scores lowest, which is the safe direction: a new
#: token added by some future slice is decoration until someone says
#: otherwise, and the alternative is that it silently outranks the phase.
_KEEP_PRIORITY: tuple = (
    ("Phase:", 100),        # what the organism is doing — the headline
    ("⚠", 90),              # a warning is why the operator looked
    ("dry", 90),            # a dry provider runway may BE the explanation
    ("Cost:", 80),          # the budget is the other standing question
    ("[", 60),              # [route·provider] badge
    ("Op:", 50),
    ("Idle:", 40),          # a countdown nobody watches while work runs
)


def _segment_priority(segment: str) -> int:
    """Keep-priority for one `·`-joined segment. NEVER raises."""
    try:
        text = segment.strip()
        for prefix, score in _KEEP_PRIORITY:
            if text.startswith(prefix):
                return score
        return 0
    except Exception:  # noqa: BLE001
        return 0


def snapshot_to_payload(snap: "StatusSnapshot") -> dict:
    """A snapshot as a transport-safe dict. NEVER raises."""
    try:
        return {
            "schema_version": STATUS_LINE_SCHEMA_VERSION,
            "phase": snap.phase,
            "phase_detail": snap.phase_detail,
            "cost_spent_usd": round(float(snap.cost_spent_usd), 4),
            "cost_budget_usd": round(float(snap.cost_budget_usd), 4),
            "idle_elapsed_s": round(float(snap.idle_elapsed_s), 1),
            "idle_timeout_s": round(float(snap.idle_timeout_s), 1),
            "primary_op_id": snap.primary_op_id,
            "extra_op_count": int(snap.extra_op_count),
            "route": snap.route,
            "provider": snap.provider,
            "liquidity_exhausted": bool(snap.liquidity_exhausted),
            "liquidity_provider": snap.liquidity_provider,
            "liquidity_reset_s": snap.liquidity_reset_s,
        }
    except Exception:  # noqa: BLE001
        return {}


def payload_to_snapshot(payload: object) -> Optional["StatusSnapshot"]:
    """Rehydrate a snapshot that arrived over the bridge, or None.

    Unknown keys are IGNORED rather than passed through: a newer daemon may
    add a field, and `StatusSnapshot(**payload)` would raise on the first
    frame from it — turning an additive change into a client crash.
    """
    try:
        if not isinstance(payload, dict) or not payload:
            return None
        fields = {f.name for f in dataclasses.fields(StatusSnapshot)}
        return StatusSnapshot(**{
            k: v for k, v in payload.items() if k in fields
        })
    except Exception:  # noqa: BLE001
        return None


def fit_to_width(line: str, width: Optional[int]) -> str:
    """Shed whole SEGMENTS, lowest keep-priority first, until the line fits.

    Whole segments, never a slice: `Cost: $0.04 / $0.5` is a number the
    operator will misread and `Phase: GENERAT` is a phase that does not
    exist. An abbreviated line has to stay true.

    NEVER raises.
    """
    try:
        if not line or not width or int(width) <= 0:
            return line
        cols = int(width)
        if len(line) <= cols:
            return line
        parts = line.split(" · ")
        # Stable order among equals: drop the RIGHTMOST of the least
        # important, so a line sheds from the end it was appended at.
        while len(" · ".join(parts)) > cols and len(parts) > 1:
            worst, worst_score = 0, None
            for i, part in enumerate(parts):
                score = _segment_priority(part)
                if worst_score is None or score <= worst_score:
                    worst, worst_score = i, score
            parts.pop(worst)
        out = " · ".join(parts)
        # One segment left and still over: an honest clip WITH a marker, so
        # the operator can see the line was cut rather than guess.
        return out if len(out) <= cols else (out[: max(1, cols - 1)] + "…")
    except Exception:  # noqa: BLE001
        return line


def render_snapshot(
    snap: Optional["StatusSnapshot"],
    *,
    compact: Optional[bool] = None,
    width: Optional[int] = None,
) -> str:
    """Render ANY snapshot — local or rehydrated — at a given width.

    ``compact=None`` resolves from the terminal rather than from the env
    alone. The env gate stays the explicit override, because an operator who
    asked for compact means it at every width; what changes is that a narrow
    terminal no longer needs to be told. NEVER raises.
    """
    try:
        if snap is None or not status_line_enabled():
            return ""
        mode = compact_mode_enabled() if compact is None else bool(compact)
        if compact is None and width and int(width) < _COMPACT_BELOW_COLS:
            mode = True
        return fit_to_width(_format_plain(snap, compact=mode), width)
    except Exception:  # noqa: BLE001
        return ""


#: Below this many columns the full line cannot hold its segments, so compact
#: is the honest default. An env override still wins in both directions.
_COMPACT_BELOW_COLS = 100

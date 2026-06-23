"""failover_lifecycle.py -- Phase 3b: the keystone Failover Lifecycle FSM.

Sovereign Provider Failover Lifecycle (spec 2026-06-23, sections 2/4/5).

The FSM over the provider-fleet health state:

    DORMANT --(cryo-trigger: R > C*margin)--> AWAKENING
    AWAKENING --(node observed ready)--> SERVING
    SERVING --(observed-gated recovery + hysteresis + min-uptime)--> HANDBACK
    HANDBACK --(delete-to-snapshot)--> DORMANT

Load-bearing invariant (spec section 2 / 5 / 10)
-----------------------------------------------
The forecast only PACES / decides WHEN-to-pay the spin-up. Awaken-READINESS
and HANDBACK are OBSERVED-gated:
  * AWAKENING -> SERVING only when an ensure-ready probe of the node succeeds.
  * SERVING -> HANDBACK only when the quarantine gradient is FULL and recovered
    across N consecutive probe cycles AND J-Prime has met its min-uptime floor.
A WRONG forecast is therefore a BOUNDED COST WOBBLE (slightly early/late
spin-up), NEVER a correctness break or a lost op. The quarantine Cryo-DLQ
remains the backstop -- this controller never owns the op.

Cryo-trigger (spec section 5a)
------------------------------
On a deduced global DW outage while DORMANT:
  R = forecast.p50_s  (remaining recovery time estimate)
  C = JARVIS_JPRIME_COLDSTART_S  (cold-start cost, default 180s)
  AWAKEN iff  R > C * JARVIS_CRYO_AWAKEN_MARGIN  (default 1.5)
Confidence gate: forecast.confidence == "LOW_CONFIDENCE" -> R is unreliable ->
fall back to the reactive floor: awaken after a fixed JARVIS_OUTAGE_CONFIRM_S
sustained-outage window. Blip-skip: HIGH confidence and R < C -> do NOT awaken
(the Cryo-DLQ holds the op; DW likely back before J-Prime boots).

Handback (spec section 4)
-------------------------
is_recovered(route) = gradient window FULL AND success_rate >= threshold,
held across JARVIS_RECOVERY_HYSTERESIS_CYCLES consecutive probe cycles, AND
jprime_uptime >= JARVIS_JPRIME_MIN_UPTIME_S. On recovery -> emit
[SOVEREIGN YIELD: UPSTREAM RECOVERED] -> route back to DW -> vm_delete_fn() ->
DORMANT. A JARVIS_HANDBACK_COOLDOWN_S cooldown blocks immediate re-awaken.

Env gates
---------
JARVIS_FAILOVER_LIFECYCLE_ENABLED   default "false" (flips after a soak)
    OFF -> controller is inert: stays DORMANT, never awakens. Today's
    behavior exactly (quarantine -> Cryo-DLQ).
JARVIS_FAILOVER_ROUTE                default "dw" (the quarantine route key)
JARVIS_JPRIME_COLDSTART_S           default 180.0
JARVIS_CRYO_AWAKEN_MARGIN           default 1.5
JARVIS_OUTAGE_CONFIRM_S             default 120.0 (reactive-floor confirm window)
JARVIS_RECOVERY_THRESHOLD           default 0.6
JARVIS_RECOVERY_HYSTERESIS_CYCLES   default 2
JARVIS_JPRIME_MIN_UPTIME_S          default 300.0
JARVIS_HANDBACK_COOLDOWN_S          default 300.0
JARVIS_JPRIME_FAILOVER_PORT         default 11434
JARVIS_FAILOVER_TICK_S              default 5.0 (run() loop base cadence)
JARVIS_FAILOVER_AWAKEN_TIMEOUT_S    default 600.0 (AWAKENING self-heal deadline)
GCP_PROJECT_ID / GCP_ZONE / etc.    awaken target identity (gcloud wrapper)

Fail-soft: every provisioning / probe call is wrapped. An awaken failure -> log
+ stay DORMANT (retry next tick); a delete failure -> log + still DORMANT (the
node's Dead-Man's Switch is the cost backstop). The op is never lost.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import subprocess
from typing import Any, Awaitable, Callable, List, Optional

# Phase 3c -- Cryo-DLQ re-entry. Imported at module level (bound as a module
# attribute) so tests can monkeypatch ``fl.replay_dlq``. intake_dlq is a
# stdlib-only leaf module with no import-cycle risk into this controller.
from backend.core.ouroboros.governance.intake_dlq import replay_dlq

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env helpers (fail-soft, never raise)
# ---------------------------------------------------------------------------

def _enabled(name: str, default: str = "true") -> bool:
    """Generic boolean env reader. default 'false' -> off unless explicitly on."""
    val = (os.environ.get(name, default) or "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def _env_str(name: str, default: str) -> str:
    return (os.environ.get(name, default) or default).strip()


def lifecycle_enabled() -> bool:
    """Master gate. Default FALSE -- OFF means inert (stays DORMANT)."""
    return _enabled("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "false")


def _route() -> str:
    return _env_str("JARVIS_FAILOVER_ROUTE", "dw")


def _coldstart_s() -> float:
    return max(1.0, _env_float("JARVIS_JPRIME_COLDSTART_S", 180.0))


def _awaken_margin() -> float:
    return max(0.0, _env_float("JARVIS_CRYO_AWAKEN_MARGIN", 1.5))


def _outage_confirm_s() -> float:
    return max(0.0, _env_float("JARVIS_OUTAGE_CONFIRM_S", 120.0))


def _recovery_threshold() -> float:
    return max(0.0, min(1.0, _env_float("JARVIS_RECOVERY_THRESHOLD", 0.6)))


def _hysteresis_cycles() -> int:
    return max(1, _env_int("JARVIS_RECOVERY_HYSTERESIS_CYCLES", 2))


def _min_uptime_s() -> float:
    return max(0.0, _env_float("JARVIS_JPRIME_MIN_UPTIME_S", 300.0))


def _handback_cooldown_s() -> float:
    return max(0.0, _env_float("JARVIS_HANDBACK_COOLDOWN_S", 300.0))


def _failover_port() -> int:
    return _env_int("JARVIS_JPRIME_FAILOVER_PORT", 11434)


def _tick_s() -> float:
    return max(0.1, _env_float("JARVIS_FAILOVER_TICK_S", 5.0))


def _awaken_timeout_s() -> float:
    return max(1.0, _env_float("JARVIS_FAILOVER_AWAKEN_TIMEOUT_S", 600.0))


# ---------------------------------------------------------------------------
# FailoverState
# ---------------------------------------------------------------------------

class FailoverState(enum.Enum):
    """The four lifecycle states (spec section 2)."""

    DORMANT = "DORMANT"
    AWAKENING = "AWAKENING"
    SERVING = "SERVING"
    HANDBACK = "HANDBACK"


# ---------------------------------------------------------------------------
# Default boundary implementations (gcloud subprocess wrapper -- fail-soft)
# ---------------------------------------------------------------------------

_GCLOUD_NODE_NAME = "jarvis-prime-failover"
_GCLOUD_IMAGE_FAMILY = "jarvis-prime-coder"
_GCLOUD_MACHINE_TYPE = "e2-highmem-2"


def _gcloud_run(cmd: List[str], *, timeout_s: float = 180.0):
    """The single subprocess boundary. Fail-soft -- returns (rc, output).

    Mirrors scripts/bake_jprime_golden_image.py::_run. Never raises.
    """
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 1, "[gcloud run failed: {!r}]".format(exc)


def _default_vm_awaken_fn(*, startup_script: str) -> bool:
    """Create the J-Prime failover node from the golden image (Spot-first).

    Awaken target (spec): image family jarvis-prime-coder, node
    jarvis-prime-failover, machine e2-highmem-2, Spot-first with on-demand
    fallback, --instance-termination-action=DELETE, SA + cloud-platform scope
    (the Dead-Man's Switch self-delete needs it), startup-script = the deadman.

    Writes the startup-script to a temp file and shells out to gcloud. Returns
    True on a 0 return code, False otherwise (fail-soft -- never raises). The
    on-ready transition is gated separately by the ensure-ready probe.
    """
    import tempfile

    project = _env_str("GCP_PROJECT_ID", "") or _env_str("GOOGLE_CLOUD_PROJECT", "")
    zone = _env_str("GCP_ZONE", "us-central1-a")
    node = _env_str("JARVIS_FAILOVER_NODE_NAME", _GCLOUD_NODE_NAME)
    image_family = _env_str("JPRIME_IMAGE_FAMILY", _GCLOUD_IMAGE_FAMILY)
    machine = _env_str("JARVIS_FAILOVER_MACHINE_TYPE", _GCLOUD_MACHINE_TYPE)

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".sh", prefix="jprime_deadman_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(startup_script)

        base = [
            "gcloud", "compute", "instances", "create", node,
            "--zone={}".format(zone),
            "--machine-type={}".format(machine),
            "--image-family={}".format(image_family),
            "--instance-termination-action=DELETE",
            "--scopes=cloud-platform",
            "--metadata-from-file=startup-script={}".format(tmp_path),
        ]
        if project:
            base.insert(5, "--project={}".format(project))
            base.insert(6, "--image-project={}".format(project))

        # Spot-first.
        spot_cmd = list(base) + ["--provisioning-model=SPOT"]
        rc, out = _gcloud_run(spot_cmd)
        if rc == 0:
            logger.info("[FailoverLifecycle] awaken: node=%s created (Spot)", node)
            return True

        logger.warning(
            "[FailoverLifecycle] awaken: Spot create failed rc=%s -- on-demand fallback. out=%s",
            rc, out[-400:],
        )
        # On-demand fallback (no --provisioning-model).
        rc2, out2 = _gcloud_run(base)
        if rc2 == 0:
            logger.info("[FailoverLifecycle] awaken: node=%s created (on-demand)", node)
            return True
        logger.warning(
            "[FailoverLifecycle] awaken: on-demand create failed rc=%s out=%s",
            rc2, out2[-400:],
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("[FailoverLifecycle] awaken fail-soft err=%r", exc)
        return False
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _default_vm_delete_fn() -> bool:
    """Delete-to-snapshot: tear down the J-Prime failover node. Fail-soft.

    The golden-image snapshot persists; only the live VM + disk are deleted.
    Returns True on rc==0. Never raises. Even on failure the node's Dead-Man's
    Switch self-deletes after idle, so cost is still bounded.
    """
    project = _env_str("GCP_PROJECT_ID", "") or _env_str("GOOGLE_CLOUD_PROJECT", "")
    zone = _env_str("GCP_ZONE", "us-central1-a")
    node = _env_str("JARVIS_FAILOVER_NODE_NAME", _GCLOUD_NODE_NAME)

    cmd = [
        "gcloud", "compute", "instances", "delete", node,
        "--zone={}".format(zone), "--quiet",
    ]
    if project:
        cmd.append("--project={}".format(project))

    rc, out = _gcloud_run(cmd)
    if rc == 0:
        logger.info("[FailoverLifecycle] delete-to-snapshot: node=%s deleted", node)
        return True
    logger.warning(
        "[FailoverLifecycle] delete-to-snapshot failed rc=%s out=%s "
        "(Dead-Man's Switch remains the cost backstop)",
        rc, out[-400:],
    )
    return False


def _default_dw_probe_fn() -> bool:
    """Cheap DW health probe verdict (NEVER a full generation).

    Reuses the dw_surface_health verdict (HEALTHY = "last probe completed
    without error"). Fail-soft -> on any error returns False (treated as a
    failed probe; the gradient stays unrecovered -- conservative). The throttle
    paces how often this is called.
    """
    try:
        from backend.core.ouroboros.governance import dw_surface_health  # noqa: PLC0415
        # Prefer a tiny, side-effect-free health verdict if exposed.
        for attr in ("is_healthy", "healthy", "surface_healthy", "is_surface_healthy"):
            fn = getattr(dw_surface_health, attr, None)
            if callable(fn):
                return bool(fn())
        verdict = getattr(dw_surface_health, "verdict", None)
        if callable(verdict):
            v = verdict()
            return str(getattr(v, "name", v)).upper() == "HEALTHY"
    except Exception as exc:  # noqa: BLE001
        logger.debug("[FailoverLifecycle] dw_probe fail-soft err=%r", exc)
    return False


def _default_clock_fn() -> float:
    return __import__("time").monotonic()


def _default_node_ready_fn(endpoint: str) -> bool:
    """Observed ensure-ready probe of the awakened node's :PORT endpoint.

    Fail-soft -> False until the node answers. Uses a tiny stdlib HTTP GET
    (NEVER a generation). The default uses urllib so tests can inject a fake
    without any real network.
    """
    try:
        import urllib.request  # noqa: PLC0415

        with urllib.request.urlopen(endpoint, timeout=3.0) as resp:  # noqa: S310
            return 200 <= getattr(resp, "status", 200) < 500
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# FailoverLifecycleController
# ---------------------------------------------------------------------------

class FailoverLifecycleController:
    """The keystone failover FSM. Observed-gated; forecast-paced; fail-soft.

    All external boundaries are injectable for testability -- tests inject
    fakes so NO real GCE / network is touched.
    """

    def __init__(
        self,
        *,
        vm_awaken_fn: Optional[Callable[..., bool]] = None,
        vm_delete_fn: Optional[Callable[[], bool]] = None,
        dw_probe_fn: Optional[Callable[[], bool]] = None,
        node_ready_fn: Optional[Callable[[str], bool]] = None,
        clock_fn: Optional[Callable[[], float]] = None,
        route: Optional[str] = None,
        on_serving_fn: Optional[Callable[[], Awaitable[Any]]] = None,
    ) -> None:
        self._vm_awaken_fn = vm_awaken_fn or _default_vm_awaken_fn
        self._vm_delete_fn = vm_delete_fn or _default_vm_delete_fn
        self._dw_probe_fn = dw_probe_fn or _default_dw_probe_fn
        self._node_ready_fn = node_ready_fn or _default_node_ready_fn
        self._clock_fn = clock_fn or _default_clock_fn
        self._route = route or _route()
        # Phase 3c -- DORMANT/AWAKENING -> SERVING re-entry hook. Fired once on
        # the transition into SERVING so the Cryo-DLQ ops sealed during the
        # outage can be drained back through intake (which re-routes them to
        # J-Prime via the generation seam). Default: the controller's own
        # drain_cryo_dlq bound to the intake router ingest (resolved lazily).
        # Injectable for testability. Fail-soft -- a hook error never blocks
        # the SERVING transition.
        self._on_serving_fn = on_serving_fn or self._default_on_serving

        self._state = FailoverState.DORMANT
        self._endpoint: Optional[str] = None

        # Timestamps (monotonic via clock_fn).
        self._outage_started_at: Optional[float] = None  # set on note_outage
        self._awakening_started_at: Optional[float] = None  # set on -> AWAKENING
        self._serving_started_at: Optional[float] = None  # set on -> SERVING
        self._last_probe_at: Optional[float] = None
        self._last_handback_at: Optional[float] = None  # cooldown anchor

        # Hysteresis: consecutive recovered probe cycles.
        self._recovered_streak = 0

        # Live probe trajectory (latencies) -- biases the forecast velocity.
        self._probe_trajectory: List[float] = []

        self._lock = asyncio.Lock()
        self._run_task: Optional["asyncio.Task"] = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Public read surface (consumed by T4 DAG re-entry)
    # ------------------------------------------------------------------

    @property
    def state(self) -> FailoverState:
        return self._state

    def is_jprime_serving(self) -> bool:
        """True iff J-Prime is the active Tier-2 generation provider."""
        return self._state == FailoverState.SERVING

    def jprime_endpoint(self) -> Optional[str]:
        """The awakened node's :PORT URL (LocalPrimeClient target), or None."""
        if self._state == FailoverState.SERVING:
            return self._endpoint
        return None

    # ------------------------------------------------------------------
    # Event hooks (gated, fail-soft)
    # ------------------------------------------------------------------

    def note_outage(self) -> None:
        """Mark a sustained-outage observation. Anchors the reactive-floor
        confirm-window clock. Idempotent -- only the first call anchors."""
        if not lifecycle_enabled():
            return
        if self._outage_started_at is None:
            try:
                self._outage_started_at = self._clock_fn()
            except Exception:  # noqa: BLE001
                self._outage_started_at = 0.0

    def note_dw_success(self) -> None:
        """A successful DW dispatch was observed -- clear the outage anchor."""
        if not lifecycle_enabled():
            return
        self._outage_started_at = None

    # ------------------------------------------------------------------
    # Cryo-DLQ re-entry (Phase 3c)
    # ------------------------------------------------------------------

    async def drain_cryo_dlq(
        self,
        ingest_fn: Callable[[Any], Awaitable[Any]],
        *,
        path: Optional[str] = None,
    ) -> int:
        """Re-ingest the Cryo-DLQ ops sealed during the outage. Fail-soft.

        Reuses ``intake_dlq.replay_dlq`` (the existing replay + atomic-rewrite
        path -- NO new queue). The drained ops flow back through *ingest_fn*
        (the intake router ingest), which re-dispatches them; while the FSM is
        SERVING the generation seam routes them to J-Prime (Tier-2), bypassing
        DW. ``replay_dlq`` is itself fail-soft: a per-entry ingest error keeps
        that entry in the DLQ for the next attempt -- the op is never lost.

        OFF (master gate false) -> no-op (returns 0): byte-identical legacy
        (the FSM stays DORMANT and this is never reached on the SERVING path).
        Returns the count of successfully drained entries (0 on any error).
        """
        if not lifecycle_enabled():
            return 0
        try:
            return int(await replay_dlq(path, ingest_fn))
        except Exception as exc:  # noqa: BLE001 -- drain must never break the FSM
            logger.warning(
                "[FailoverLifecycle] drain_cryo_dlq fail-soft err=%r "
                "-- DLQ left intact for the next attempt", exc,
            )
            return 0

    async def _default_on_serving(self) -> None:
        """Default SERVING hook: drain the Cryo-DLQ through the intake router.

        Resolves the live intake router lazily (so there is no import cycle and
        no hard dependency when the router isn't wired -- e.g. unit tests).
        The ingest_fn reconstructs an ``IntentEnvelope`` from each persisted
        ``to_dict`` payload and forwards it to ``router.ingest``. When no router
        is reachable the drain is a fail-soft no-op (the DLQ stays intact for a
        later boot-time replay -- the op is never lost)."""
        router = self._resolve_intake_router()
        if router is None:
            logger.info(
                "[FailoverLifecycle] on_serving: no intake router reachable "
                "-- Cryo-DLQ drain deferred (DLQ intact)"
            )
            return

        async def _ingest(env: Any) -> Any:
            # Reconstruct the typed envelope from the persisted dict so the
            # router receives the same shape it would on a live emit. A raw
            # (non-dict) payload is forwarded as-is (router decides).
            envelope: Any = env
            if isinstance(env, dict):
                try:
                    from backend.core.ouroboros.governance.intake.intent_envelope import (  # noqa: E501,PLC0415
                        IntentEnvelope,
                    )
                    envelope = IntentEnvelope.from_dict(env)
                except Exception:  # noqa: BLE001 -- fall back to the raw dict
                    envelope = env
            return await router.ingest(envelope)

        drained = await self.drain_cryo_dlq(_ingest)
        logger.info(
            "[FailoverLifecycle] on_serving: Cryo-DLQ re-entry drained=%d ops "
            "-> intake (re-routes to J-Prime Tier-2)", drained,
        )

    @staticmethod
    def _resolve_intake_router() -> Optional[Any]:
        """Best-effort lookup of the live intake router singleton. Fail-soft."""
        try:
            from backend.core.ouroboros.governance.intake import (  # noqa: PLC0415
                unified_intake_router as _uir,
            )
            for attr in (
                "get_default_intake_router",
                "get_intake_router",
                "get_router",
            ):
                fn = getattr(_uir, attr, None)
                if callable(fn):
                    return fn()
        except Exception:  # noqa: BLE001
            logger.debug(
                "[FailoverLifecycle] intake router resolve fail-soft", exc_info=True
            )
        return None

    # ------------------------------------------------------------------
    # Endpoint construction
    # ------------------------------------------------------------------

    def _build_endpoint(self) -> str:
        host = _env_str("JARVIS_FAILOVER_NODE_HOST", _GCLOUD_NODE_NAME)
        port = _failover_port()
        return "http://{}:{}".format(host, port)

    # ------------------------------------------------------------------
    # Forecast helpers
    # ------------------------------------------------------------------

    def _get_forecast(self):
        from backend.core.ouroboros.governance.recovery_forecaster import (  # noqa: PLC0415
            get_recovery_forecaster,
        )
        return get_recovery_forecaster().forecast(
            live_probe_trajectory=list(self._probe_trajectory) or None,
        )

    def _gradient(self):
        from backend.core.ouroboros.governance.provider_quarantine import (  # noqa: PLC0415
            get_provider_health_gradient,
        )
        return get_provider_health_gradient()

    # ------------------------------------------------------------------
    # Cryo-trigger decision (spec section 5a)
    # ------------------------------------------------------------------

    def _should_awaken(self, *, now: float) -> bool:
        """Pure-ish decision: should we pay the spin-up now?

        Observed precondition: quarantine.is_global_outage(route) must be True
        (the caller checks this). This method applies the forecast-shaped
        cost gate. Fail-soft -> on any error, fall back to the reactive floor.
        """
        try:
            forecast = self._get_forecast()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] forecast fail-soft err=%r", exc)
            forecast = None

        confidence = getattr(forecast, "confidence", "LOW_CONFIDENCE")
        coldstart = _coldstart_s()
        margin = _awaken_margin()

        if confidence != "HIGH":
            # LOW_CONFIDENCE: R is unreliable -> reactive floor. Awaken only
            # after a sustained confirm window (anchored by note_outage).
            if self._outage_started_at is None:
                # No anchor yet -> anchor now; not yet confirmed.
                self._outage_started_at = now
                return False
            elapsed = now - self._outage_started_at
            confirmed = elapsed >= _outage_confirm_s()
            if confirmed:
                logger.info(
                    "[FailoverLifecycle] cryo-trigger: LOW_CONFIDENCE reactive-floor "
                    "confirmed (elapsed=%.1fs >= %.1fs) -> AWAKEN",
                    elapsed, _outage_confirm_s(),
                )
            return confirmed

        # HIGH confidence: cost math. R = p50.
        r = float(getattr(forecast, "p50_s", 0.0))
        threshold = coldstart * margin
        decision = r > threshold
        logger.info(
            "[FailoverLifecycle] cryo-trigger: HIGH conf R(p50)=%.1f C=%.1f "
            "margin=%.2f threshold=%.1f -> %s",
            r, coldstart, margin, threshold,
            "AWAKEN" if decision else "BLIP-SKIP (hold in Cryo-DLQ)",
        )
        return decision

    # ------------------------------------------------------------------
    # Recovery decision (spec section 4)
    # ------------------------------------------------------------------

    def _is_recovered(self) -> bool:
        """Observed recovery predicate: gradient window FULL and
        success_rate >= threshold. Fail-soft -> False (conservative)."""
        try:
            grad = self._gradient()
            # Window FULL check mirrors is_global_outage: reuse success_rate +
            # the FULL gate. is_global_outage(False) does NOT imply recovered,
            # so we require an explicit success_rate threshold over a full
            # window. The FULL gate is the public window_full() predicate
            # (Phase 3c clean-seam fix -- no private _get_window access).
            if not grad.window_full(self._route):
                return False
            return grad.success_rate(self._route) >= _recovery_threshold()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] is_recovered fail-soft err=%r", exc)
            return False

    def _jprime_uptime(self, *, now: float) -> float:
        if self._serving_started_at is None:
            return 0.0
        return max(0.0, now - self._serving_started_at)

    # ------------------------------------------------------------------
    # Single tick of the FSM (the testable core)
    # ------------------------------------------------------------------

    async def tick(self) -> FailoverState:
        """Advance the FSM one step. Fail-soft. Returns the (possibly new)
        state. OFF -> inert (stays DORMANT, never awakens)."""
        if not lifecycle_enabled():
            # OFF byte-identical: inert. Force DORMANT defensively.
            self._state = FailoverState.DORMANT
            return self._state

        async with self._lock:
            try:
                await self._tick_inner()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[FailoverLifecycle] tick fail-soft err=%r", exc)
            return self._state

    async def _tick_inner(self) -> None:
        now = self._clock_fn()

        if self._state == FailoverState.DORMANT:
            await self._tick_dormant(now=now)
        elif self._state == FailoverState.AWAKENING:
            await self._tick_awakening(now=now)
        elif self._state == FailoverState.SERVING:
            await self._tick_serving(now=now)
        elif self._state == FailoverState.HANDBACK:
            await self._tick_handback(now=now)

    async def _tick_dormant(self, *, now: float) -> None:
        # Anti-thrash: a recent handback blocks immediate re-awaken.
        if self._last_handback_at is not None:
            if (now - self._last_handback_at) < _handback_cooldown_s():
                return

        grad = self._gradient()
        try:
            outage = grad.is_global_outage(self._route)
        except Exception:  # noqa: BLE001
            outage = False
        if not outage:
            return

        # Observed outage. Apply the cryo-trigger cost gate.
        if not self._should_awaken(now=now):
            return

        # AWAKEN: build deadman startup-script, spin up the node.
        self._state = FailoverState.AWAKENING
        self._awakening_started_at = now
        self._recovered_streak = 0
        self._probe_trajectory = []
        await self._do_awaken()

    async def _do_awaken(self) -> None:
        """Invoke vm_awaken_fn with the Dead-Man's Switch startup-script.

        Fail-soft: on any failure, revert to DORMANT (retry next tick). The op
        is never lost -- the quarantine Cryo-DLQ remains the backstop."""
        try:
            startup_script = self._build_startup_script()
            ok = await self._maybe_await(
                self._vm_awaken_fn, startup_script=startup_script
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FailoverLifecycle] awaken raised -- stay DORMANT err=%r", exc)
            self._state = FailoverState.DORMANT
            self._awakening_started_at = None
            return
        if not ok:
            logger.warning("[FailoverLifecycle] awaken returned falsy -- revert DORMANT")
            self._state = FailoverState.DORMANT
            self._awakening_started_at = None
            return
        self._endpoint = self._build_endpoint()
        logger.info("[FailoverLifecycle] AWAKENING node endpoint=%s", self._endpoint)

    def _build_startup_script(self) -> str:
        from backend.core.ouroboros.governance.failover_deadman import (  # noqa: PLC0415
            build_deadman_startup_script,
        )
        return build_deadman_startup_script(port=_failover_port())

    async def _tick_awakening(self, *, now: float) -> None:
        # AWAKENING deadline: if the node never becomes ready within the timeout,
        # proactively tear it down and revert to DORMANT + arm the cooldown so we
        # do not immediately retry-storm the same wedged image.
        awakening_started = self._awakening_started_at
        if awakening_started is not None:
            elapsed = now - awakening_started
            if elapsed > _awaken_timeout_s():
                logger.warning(
                    "[FailoverLifecycle] AWAKENING timed out after %.1fs -- "
                    "node never became ready, tearing down + reverting to DORMANT",
                    elapsed,
                )
                # Proactive teardown (fail-soft -- dead-man's switch is the backstop).
                try:
                    await self._maybe_await(self._vm_delete_fn)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[FailoverLifecycle] AWAKENING timeout vm_delete fail-soft "
                        "err=%r -- reverting DORMANT anyway (Dead-Man's Switch backstop)",
                        exc,
                    )
                # Revert to DORMANT + arm cooldown (re-use handback cooldown anchor
                # so the same anti-thrash window blocks an immediate re-awaken).
                self._state = FailoverState.DORMANT
                self._awakening_started_at = None
                self._endpoint = None
                self._last_handback_at = now
                return

        # Observed ensure-ready gate: only -> SERVING when the node answers.
        endpoint = self._endpoint or self._build_endpoint()
        try:
            ready = await self._maybe_await(self._node_ready_fn, endpoint)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] node_ready probe fail-soft err=%r", exc)
            ready = False
        if not ready:
            return  # keep waiting; next tick re-probes (fail-soft).
        self._awakening_started_at = None
        self._state = FailoverState.SERVING
        self._serving_started_at = now
        self._last_probe_at = None
        self._recovered_streak = 0
        logger.info("[FailoverLifecycle] SERVING via J-Prime endpoint=%s", self._endpoint)

        # Phase 3c -- Cryo-DLQ re-entry. Fire the on-serving hook so the ops
        # sealed during the outage drain back through intake and re-route to
        # J-Prime via the generation seam. Fail-soft ABSOLUTE: a hook error
        # never blocks (or reverts) the SERVING transition -- the op is never
        # lost (the DLQ replay is itself fail-soft + leaves survivors intact).
        try:
            if self._on_serving_fn is not None:
                await self._maybe_await(self._on_serving_fn)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[FailoverLifecycle] on_serving (Cryo-DLQ drain) fail-soft "
                "err=%r -- SERVING transition holds", exc,
            )

    async def _tick_serving(self, *, now: float) -> None:
        # Pace the DW-recovery probe by probe_interval(t_outage, forecast).
        interval = self._probe_interval(now=now)
        if self._last_probe_at is not None and (now - self._last_probe_at) < interval:
            return  # not yet time to probe

        # Fire the cheap DW probe; record the verdict into the gradient.
        self._last_probe_at = now
        verdict = False
        probe_start = now
        try:
            verdict = bool(await self._maybe_await(self._dw_probe_fn))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] dw_probe fail-soft err=%r", exc)
            verdict = False
        # Trajectory: latency proxy (probe wall-time). Bounded.
        try:
            latency = max(0.0, self._clock_fn() - probe_start)
            self._probe_trajectory.append(latency)
            if len(self._probe_trajectory) > 20:
                self._probe_trajectory = self._probe_trajectory[-20:]
        except Exception:  # noqa: BLE001
            pass

        try:
            self._gradient().record_sweep(self._route, success=verdict)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] record_sweep fail-soft err=%r", exc)

        # Observed-gated handback evaluation.
        if self._is_recovered():
            self._recovered_streak += 1
        else:
            self._recovered_streak = 0  # hysteresis reset on any non-recovered

        hyst_ok = self._recovered_streak >= _hysteresis_cycles()
        uptime_ok = self._jprime_uptime(now=now) >= _min_uptime_s()

        if hyst_ok and uptime_ok:
            logger.info(
                "[FailoverLifecycle] HANDBACK gate passed: recovered_streak=%d (>=%d) "
                "uptime=%.1fs (>=%.1fs)",
                self._recovered_streak, _hysteresis_cycles(),
                self._jprime_uptime(now=now), _min_uptime_s(),
            )
            self._state = FailoverState.HANDBACK
            await self._tick_handback(now=now)

    def _probe_interval(self, *, now: float) -> float:
        try:
            from backend.core.ouroboros.governance.recovery_throttle import (  # noqa: PLC0415
                probe_interval,
            )
            t_outage = 0.0
            if self._outage_started_at is not None:
                t_outage = max(0.0, now - self._outage_started_at)
            elif self._serving_started_at is not None:
                t_outage = max(0.0, now - self._serving_started_at)
            return float(probe_interval(t_outage, self._get_forecast()))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] probe_interval fail-soft err=%r", exc)
            return 60.0  # safe interval

    async def _tick_handback(self, *, now: float) -> None:
        """Emit the sovereign yield, route back to DW, delete-to-snapshot.

        Fail-soft: even if vm_delete_fn fails, we go DORMANT and arm the
        cooldown -- the node's Dead-Man's Switch is the cost backstop."""
        # 1. Emit [SOVEREIGN YIELD: UPSTREAM RECOVERED].
        try:
            from backend.core.ouroboros.governance.convergence_watchdog import (  # noqa: PLC0415
                emit_sovereign_yield,
            )
            emit_sovereign_yield(
                self._route,
                lineage_id=self._route,
                ratio=1.0,
                consecutive_stalls=0,
                parent_chars=0,
                child_chars=0,
                tier="provider",
                reason="UPSTREAM RECOVERED",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] emit yield fail-soft err=%r", exc)

        # 2. Route generation back to DW: drop the J-Prime endpoint NOW so
        #    is_jprime_serving()/jprime_endpoint() stop pointing at the node
        #    before teardown (T4 re-routes to DW).
        self._endpoint = None

        # 3. Delete-to-snapshot.
        try:
            ok = await self._maybe_await(self._vm_delete_fn)
            if not ok:
                logger.warning(
                    "[FailoverLifecycle] delete returned falsy -- Dead-Man's Switch "
                    "remains the cost backstop"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[FailoverLifecycle] delete raised -- Dead-Man's Switch backstop err=%r",
                exc,
            )

        # 4. DORMANT + arm anti-thrash cooldown.
        self._state = FailoverState.DORMANT
        self._awakening_started_at = None
        self._serving_started_at = None
        self._last_probe_at = None
        self._recovered_streak = 0
        self._probe_trajectory = []
        self._outage_started_at = None
        self._last_handback_at = now
        logger.info("[FailoverLifecycle] DORMANT (delete-to-snapshot complete)")

    # ------------------------------------------------------------------
    # Await helper (boundaries may be sync or async)
    # ------------------------------------------------------------------

    @staticmethod
    async def _maybe_await(fn: Callable, *args, **kwargs):
        result = fn(*args, **kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result

    # ------------------------------------------------------------------
    # Async run() driver
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Tick loop. Gated -- OFF returns immediately (inert). Fail-soft.

        Cadence = JARVIS_FAILOVER_TICK_S (uses asyncio.sleep, NOT
        asyncio.timeout -- Python 3.9+ safe)."""
        if not lifecycle_enabled():
            logger.debug("[FailoverLifecycle] run(): disabled -- inert")
            return
        self._stopped = False
        logger.info("[FailoverLifecycle] run() loop started route=%s", self._route)
        while not self._stopped:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[FailoverLifecycle] run tick fail-soft err=%r", exc)
            try:
                await asyncio.sleep(_tick_s())
            except asyncio.CancelledError:
                break

    def stop(self) -> None:
        self._stopped = True


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_singleton: Optional[FailoverLifecycleController] = None


def get_failover_controller() -> FailoverLifecycleController:
    """Return (or lazily create) the process-wide controller singleton."""
    global _singleton  # noqa: PLW0603
    if _singleton is None:
        _singleton = FailoverLifecycleController()
    return _singleton


def _reset_singleton_for_tests() -> None:
    """Test hook: drop the singleton so a fresh controller is created."""
    global _singleton  # noqa: PLW0603
    _singleton = None


__all__ = [
    "FailoverState",
    "FailoverLifecycleController",
    "get_failover_controller",
    "lifecycle_enabled",
]

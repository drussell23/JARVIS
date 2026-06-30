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
JARVIS_FAILOVER_LIFECYCLE_ENABLED   default "true" (GRADUATED 2026-06-23; hot-revert=false)
    OFF -> controller is inert: stays DORMANT, never awakens. Today's
    behavior exactly (quarantine -> Cryo-DLQ).
JARVIS_FAILOVER_ROUTE                default "dw" (the quarantine route key)
JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED default "true" (graduated 2026-06-27,
    Task 4 Isomorphic Local Sandbox). The reactive awaken fires on ANY tracked
    generation route reaching the SAME full-window rate==0 ``is_global_outage``
    -- the AUTHORITATIVE real-generation-failure signal -- not only the single
    ``JARVIS_FAILOVER_ROUTE`` key. Closes the run-#11 blindspot (DW's cheap probe
    passed while the BACKGROUND route collapsed). Hot-revert: set to "false" ->
    byte-identical single-route check (legacy).
JARVIS_FAILOVER_OUTAGE_ROUTES        default "" (comma-separated explicit extra
    routes to fold into the any-route check; the gradient's tracked routes are
    authoritative -- this is an operator escape hatch only).
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
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

# Phase 3c -- Cryo-DLQ re-entry. Imported at module level (bound as a module
# attribute) so tests can monkeypatch ``fl.replay_dlq``. intake_dlq is a
# stdlib-only leaf module with no import-cycle risk into this controller.
from backend.core.ouroboros.governance.intake_dlq import replay_dlq

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Awaken-reason taxonomy (Task CR2 -- Multi-Vector Awaken Trigger)
# ---------------------------------------------------------------------------
# The controller REMEMBERS *why* it awakened so a later recovery strategy can
# branch on the vector. DW stays primary; J-Prime is the fallback. A data-plane
# outage and a cloud-budget exhaustion are BOTH valid awaken vectors.
AWAKEN_REASON_DATA_PLANE = "DATA_PLANE_OUTAGE"
AWAKEN_REASON_BUDGET = "BUDGET_EXHAUSTED"
AWAKEN_REASON_RATE_LIMIT = "RATE_LIMITED"  # reserved for CR5 (set up now)


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
    """Master gate. GRADUATED to default TRUE (2026-06-23) after the Adversarial
    Cognitive Soak proved the full net live: VRAM pre-warm + Hybrid Epistemic
    Diff injection + temperature decay + budget-exhaustion [SOVEREIGN YIELD:
    UNRESOLVABLE PATH] + semantic symbol-scoped decompose + graceful retry (no
    lockup, no dropped state); composed with the chaos-gauntlet (phantom-recovery,
    no-thrash) + the LIVE dead-man self-delete drill (T+221s) + Opus-reviewed
    OFF-byte-identical / op-never-lost / 3-independent-teardowns.
    Hot-revert: ``export JARVIS_FAILOVER_LIFECYCLE_ENABLED=false`` -> inert
    (stays DORMANT, today's behavior exactly)."""
    return _enabled("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")


def budget_awaken_enabled() -> bool:
    """Master gate for the budget-exhaustion awaken vector (Task CR2). Default
    OFF -> byte-identical (only a data-plane outage awakens). When ARMED, a
    ``note_budget_exhausted()`` anchor awakens J-Prime on the next dormant tick
    with reason ``BUDGET_EXHAUSTED``."""
    return os.environ.get(
        "JARVIS_FAILOVER_BUDGET_AWAKEN_ENABLED", "false"
    ).strip().lower() in ("1", "true", "yes")


def header_aware_recovery_enabled() -> bool:
    """Master gate for header-aware DW-recovery sleep (Task CR5). Default OFF ->
    byte-identical (the SERVING probe paces itself by the forecast-driven jitter
    backoff, exactly as today). When ARMED, a DW 429 that carried a
    ``Retry-After`` / ``x-ratelimit-reset`` header (anchored via
    ``note_rate_limited``) makes the recovery probe suspend until the provider's
    OWN reset deadline before falling through to the semantic deep probe. Reuses
    the existing deep probe + jitter backoff -- it ONLY changes the next-probe
    interval while the rate-limit anchor is live."""
    return os.environ.get(
        "JARVIS_FAILOVER_HEADER_AWARE_RECOVERY_ENABLED", "false"
    ).strip().lower() in ("1", "true", "yes")


def violent_teardown_enabled() -> bool:
    """Master gate for the Violent Ephemeral Teardown (Task CR4). Default OFF ->
    byte-identical (only the passive HANDBACK/dead-man reaps J-Prime). When ARMED,
    the GPU node + node + both /32 firewalls vacate the INSTANT the A1 DAG hits a
    terminal state (PR opened OR fail-closed at any gate) -- zero idle GPU while a
    g2 sits waiting for human review."""
    return os.environ.get(
        "JARVIS_FAILOVER_VIOLENT_TEARDOWN_ENABLED", "false"
    ).strip().lower() in ("1", "true", "yes")


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
    """The single config-driven inference port -- fed to the firewall rule, the
    Reachability Racer's candidate endpoints, AND the endpoint publisher, so a
    config change adapts the WHOLE mesh. Resolution: an explicit failover pin
    (``JARVIS_JPRIME_FAILOVER_PORT``) wins; else the unified ``JARVIS_PRIME_PORT``
    (what the inference daemon actually serves -- e.g. 8000 in .env.gcp); else
    the legacy default. NO hardcoding past the final fallback."""
    explicit = (os.environ.get("JARVIS_JPRIME_FAILOVER_PORT", "") or "").strip()
    if explicit:
        try:
            return int(explicit)
        except (ValueError, TypeError):
            pass
    return _env_int("JARVIS_PRIME_PORT", 11434)


def _tick_s() -> float:
    return max(0.1, _env_float("JARVIS_FAILOVER_TICK_S", 5.0))


def _awaken_timeout_s() -> float:
    return max(1.0, _env_float("JARVIS_FAILOVER_AWAKEN_TIMEOUT_S", 600.0))


def _early_prewarm_enabled() -> bool:
    """Gap-2 sub-gate: degradation -> early pre-warm. Default FALSE.

    Composes UNDER the master ``JARVIS_FAILOVER_LIFECYCLE_ENABLED`` gate. When
    OFF (or when the master is off) the controller only awakens on the legacy
    REACTIVE ``is_global_outage`` path -- byte-identical. When ON, a DEGRADED
    (not-yet-outage) heartbeat reading plus a slow recovery forecast can awaken
    J-Prime EARLY so the node is warm by the time DW formally collapses and the
    op drops into the Cryo-DLQ. The operator arms this at soak time."""
    return _enabled("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "false")


def _any_route_outage_enabled() -> bool:
    """Authoritative-signal sub-gate: awaken on ANY route's real-generation
    ``is_global_outage`` (the record_sweep-driven gradient), not only the single
    configured ``JARVIS_FAILOVER_ROUTE`` key.

    **Default TRUE** (flipped from "false" by Task 4 of the Isomorphic Local
    Sandbox, 2026-06-27). The fix closes the run-#11/#12 blindspot:

      DW's cheap HeavyProbe (``GET /models``) passed (partial single-token OK)
      while the BACKGROUND *generation* route collapsed to rate==0 over a full
      window (``dw_severed_queued``). The reactive awaken checked only
      ``is_global_outage("dw")`` -- a key that is NEVER the urgency-routing key
      ``candidate_generator.record_sweep`` populates (it uses "background" /
      "standard" / "complex" / "realtime") -- so ``_real_outage()`` always
      returned False and J-Prime never awoke.

    When TRUE (default), the FSM reacts to ANY tracked route hitting the SAME
    full-window rate==0 outage threshold (fail-CLOSED: a transient blip /
    not-yet-full window does NOT trip it -- identical threshold to the quarantine
    seal). Hot-revert: ``export JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED=false``
    -> byte-identical single-route check (legacy). Composes UNDER the master gate."""
    return _enabled("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")


def _outage_extra_routes() -> List[str]:
    """Operator-pinnable explicit extra routes to fold into the any-route outage
    check (comma-separated ``JARVIS_FAILOVER_OUTAGE_ROUTES``). Default empty ->
    only the dynamically-tracked routes (plus ``self._route``) are considered.
    No hardcoded route table -- the gradient's tracked routes are authoritative;
    this is just an operator escape hatch for a route that hasn't recorded a
    sweep yet. NEVER raises."""
    raw = (os.environ.get("JARVIS_FAILOVER_OUTAGE_ROUTES", "") or "").strip()
    if not raw:
        return []
    return [r.strip() for r in raw.split(",") if r.strip()]


def _hard_escalation_enabled() -> bool:
    """Gap 2 -- a sustained deep-probe drop streak forcefully promotes to
    AWAKENING (forecast/confirm-window BYPASS). Default ON. A DEAD data plane
    must not wait for a slow-recovery forecast. Hot-revert:
    ``JARVIS_DW_HARD_OUTAGE_ESCALATION_ENABLED=false`` -> legacy (degrade only)."""
    return _enabled("JARVIS_DW_HARD_OUTAGE_ESCALATION_ENABLED", "true")


def _ephemeral_fw_enabled() -> bool:
    """REST-native ephemeral firewall micro-perimeter at AWAKENING. Default OFF
    (production may run in-VPC where it's unnecessary). The hybrid soak opts in
    via ``JARVIS_FAILOVER_EPHEMERAL_FW_ENABLED=true`` -- a /32 rule for the
    orchestrator's OWN detected egress IP, torn down with the node."""
    return _enabled("JARVIS_FAILOVER_EPHEMERAL_FW_ENABLED", "false")


def _ephemeral_fw_name() -> str:
    return _env_str("JARVIS_FAILOVER_FW_RULE_NAME", "jarvis-ephemeral-failover-allow")


def _ready_backoff_base_s() -> float:
    return max(0.01, _env_float("JARVIS_FAILOVER_READY_BACKOFF_BASE_S", 1.0))


def _ready_backoff_cap_s() -> float:
    return max(0.01, _env_float("JARVIS_FAILOVER_READY_BACKOFF_CAP_S", 15.0))


def _handback_drain_budget_s() -> float:
    """Max seconds HANDBACK waits for in-flight J-Prime ops to drain to 0 before
    tearing down anyway (bounded -- never deadlock the FSM; the Dead-Man's Switch
    is the backstop). Default 120s (covers a slow CPU generation)."""
    return max(0.0, _env_float("JARVIS_HANDBACK_DRAIN_BUDGET_S", 120.0))


def _handback_drain_poll_s() -> float:
    return max(0.01, _env_float("JARVIS_HANDBACK_DRAIN_POLL_S", 1.0))


def _recovery_streak_n() -> int:
    """Consecutive HEALTHY deep-probes that confirm DW recovery -> HANDBACK."""
    return max(1, _env_int("JARVIS_DW_RECOVERY_STREAK", 3))


def _recovery_max_latency_s() -> float:
    """A healthy DW probe slower than this is NOT 'recovered' (still degraded)."""
    return max(0.1, _env_float("JARVIS_DW_RECOVERY_MAX_LATENCY_S", 5.0))


def _ready_backoff_budget_s() -> float:
    """Per-AWAKENING-tick L7-readiness poll budget (bounded so the tick stays
    responsive). The AWAKENING timeout (default 600s) is the hard outer bound
    ACROSS ticks, so a short per-tick burst still gives the daemon minutes to
    come up; operators raise it to poll harder within a single tick."""
    return max(0.5, _env_float("JARVIS_FAILOVER_READY_BACKOFF_BUDGET_S", 5.0))


def _hard_outage_streak() -> int:
    """Consecutive deep-probe drops that constitute a CONFIRMED data-plane
    outage (the mathematical confirmation that replaces the forecast wait).
    Default 3; clamped >= 2 (strictly above a transient blip). Env-tunable."""
    try:
        return max(2, int(os.environ.get("JARVIS_DW_HARD_OUTAGE_STREAK", "3")))
    except (ValueError, TypeError):
        return 3


def _gcloud_fallback_enabled() -> bool:
    """Last-resort gcloud-CLI fallback gate. Default FALSE.

    The native metadata-token Compute REST client (gcp_compute_rest) is the
    PRIMARY -- and only -- path on the Sovereign awaken/delete. The gcloud
    subprocess wrappers are retained ONLY as an explicit operator escape hatch
    for the (rare) case where the metadata server is unreachable but a gcloud
    binary + ambient credentials happen to exist. Default OFF: the sovereign
    path has ZERO gcloud dependency."""
    return _enabled("JARVIS_FAILOVER_GCLOUD_FALLBACK", "false")


def _warmup_enabled() -> bool:
    """VRAM pre-warm gate. Default TRUE -- OFF -> straight to SERVING (legacy)."""
    return _enabled("JARVIS_FAILOVER_WARMUP_ENABLED", "true")


def _warmup_timeout_s() -> float:
    """Cold-load budget for the dummy generation. Distinct from per-op clock."""
    return max(1.0, _env_float("JARVIS_FAILOVER_WARMUP_TIMEOUT_S", 180.0))


# ---------------------------------------------------------------------------
# FailoverState
# ---------------------------------------------------------------------------

class _NodeNotReachable(RuntimeError):
    """Internal sentinel: a Reachability-Racer candidate endpoint did not answer
    a healthy 200. Used to lose the race without binding the endpoint."""


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


async def _default_vm_awaken_fn(*, startup_script: str) -> bool:
    """Create the J-Prime failover node from the golden image (Spot-first).

    Sovereign path (PRIMARY, zero gcloud): native async GCP Compute REST via the
    metadata-token client (gcp_compute_rest). The flow is:

      1. ``verify_compute_scopes()`` -- dynamic IAM self-verification. A missing
         compute scope (or unreachable metadata) yields a graceful
         ``IAM_PERMISSION_DENIED`` locus -> awaken aborts cleanly (returns
         False). The lifecycle stays DORMANT, the op stays sealed in the
         Cryo-DLQ -- the cognitive loop is NEVER crashed.
      2. ``create_instance()`` -- async instances.insert of the golden image
         (sourceImage family), dynamic zone/project from metadata, Spot-first
         with on-demand fallback, startup-script = the deadman.

    Returns True iff the insert was accepted. Fail-soft -- never raises. The
    on-ready transition + IP wiring are gated separately downstream.

    LAST-RESORT fallback (only if metadata is unreachable AND
    ``JARVIS_FAILOVER_GCLOUD_FALLBACK`` is armed): the legacy gcloud subprocess
    wrapper. Default OFF -- the sovereign path has ZERO gcloud dependency.
    """
    try:
        from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
            get_compute_rest,
        )
        client = get_compute_rest()
        ok_scope, detail = await client.verify_compute_scopes()
        if not ok_scope:
            # Graceful IAM_PERMISSION_DENIED locus -- the loop is NOT crashed.
            if "metadata_unreachable" in detail and _gcloud_fallback_enabled():
                logger.warning(
                    "[FailoverLifecycle] awaken: metadata unreachable for IAM "
                    "self-verify -- gcloud last-resort fallback armed"
                )
                return _gcloud_vm_awaken_fn(startup_script=startup_script)
            logger.warning(
                "[FailoverLifecycle] awaken ABORTED (graceful): %s -- staying "
                "DORMANT, op stays sealed in Cryo-DLQ (loop not crashed)", detail,
            )
            return False
        # Adaptive Workload Provisioning: resolve the tier (survival 7B-CPU by
        # default; quality 32B-GPU only when the gate is ON for a high-priority
        # op). Inject the tier's machine type, golden image (== baked model), and
        # any GPU accelerator into the REST provision. Deterministic from env ->
        # consistent with the controller's stored active model.
        from backend.core.ouroboros.governance.failover_tier import (  # noqa: PLC0415
            resolve_tier,
        )
        _tier = resolve_tier(
            urgency=_env_str("JARVIS_FAILOVER_AWAKEN_URGENCY", ""),
            complexity=_env_str("JARVIS_FAILOVER_AWAKEN_COMPLEXITY", ""),
        )
        ok_create, cdetail = await client.create_instance(
            startup_script=startup_script,
            machine_type=_tier.machine_type,
            image_family=_tier.image_family,
            accelerator_type=_tier.accelerator_type,
            accelerator_count=_tier.accelerator_count,
        )
        if ok_create:
            logger.info(
                "[FailoverLifecycle] awaken: %s-tier node created via native "
                "Compute REST (%s, model=%s, gpu=%s) -- zero gcloud",
                _tier.name, cdetail, _tier.model_label, _tier.is_gpu,
            )
            return True
        logger.warning(
            "[FailoverLifecycle] awaken: Compute REST insert failed (%s)", cdetail,
        )
        if _gcloud_fallback_enabled():
            logger.warning(
                "[FailoverLifecycle] awaken: gcloud last-resort fallback armed "
                "-- attempting gcloud create"
            )
            return _gcloud_vm_awaken_fn(startup_script=startup_script)
        return False
    except Exception as exc:  # noqa: BLE001 -- awaken must never crash the loop
        logger.warning("[FailoverLifecycle] awaken REST fail-soft err=%r", exc)
        if _gcloud_fallback_enabled():
            return _gcloud_vm_awaken_fn(startup_script=startup_script)
        return False


def _gcloud_vm_awaken_fn(*, startup_script: str) -> bool:
    """LAST-RESORT gcloud-CLI awaken (gated by JARVIS_FAILOVER_GCLOUD_FALLBACK).

    Writes the startup-script to a temp file and shells out to gcloud. Returns
    True on a 0 return code, False otherwise (fail-soft -- never raises).
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


async def _default_vm_delete_fn() -> bool:
    """Delete-to-snapshot: tear down the J-Prime failover node. Fail-soft.

    Sovereign path (PRIMARY, zero gcloud): native async Compute REST DELETE via
    the metadata-token client -- the SAME REST contract as the bash dead-man.
    Deleting the instance does NOT touch the golden image (the snapshot
    persists). Returns True iff the delete was accepted (or the node was already
    gone -- idempotent). Never raises. Even on failure the node's Dead-Man's
    Switch self-deletes after idle, so cost is still bounded.

    LAST-RESORT gcloud fallback only if metadata is unreachable AND
    ``JARVIS_FAILOVER_GCLOUD_FALLBACK`` is armed (default OFF).
    """
    try:
        from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
            get_compute_rest,
        )
        ok, detail = await get_compute_rest().delete_instance()
        if ok:
            logger.info(
                "[FailoverLifecycle] delete-to-snapshot via Compute REST (%s) "
                "-- zero gcloud", detail,
            )
            return True
        logger.warning(
            "[FailoverLifecycle] delete via Compute REST failed (%s)", detail,
        )
        if "metadata_unreachable" in detail and _gcloud_fallback_enabled():
            return _gcloud_vm_delete_fn()
        return False
    except Exception as exc:  # noqa: BLE001 -- delete must never crash the loop
        logger.warning("[FailoverLifecycle] delete REST fail-soft err=%r", exc)
        if _gcloud_fallback_enabled():
            return _gcloud_vm_delete_fn()
        return False


def _gcloud_vm_delete_fn() -> bool:
    """LAST-RESORT gcloud-CLI delete (gated by JARVIS_FAILOVER_GCLOUD_FALLBACK)."""
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
            # Strict Layer-7 success: a 2xx from the inference endpoint. A
            # connection refused / RST / timeout raises -> False (keep polling).
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:  # noqa: BLE001
        return False


async def _default_resolve_node_ip() -> str:
    """Resolve the awakened failover node's internal IP via native Compute REST.

    Sovereign path (PRIMARY, zero gcloud): poll instances.get until status
    RUNNING and extract networkInterfaces[0].networkIP (the internal IP -- the
    Body shares the VPC). Dynamic zone/project/IP from metadata + the API
    response -- NOTHING is hardcoded.

    Returns the IP string, or "" if it cannot be resolved within the bounded
    budget (fail-soft -- the caller treats "" as 'publish nothing' and
    PrimeProvider keeps its existing configured target; the op is never lost).
    NEVER raises.

    LAST-RESORT gcloud fallback only when ``JARVIS_FAILOVER_GCLOUD_FALLBACK``
    is armed (default OFF).
    """
    try:
        from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
            get_compute_rest,
        )
        ip = await get_compute_rest().await_running_ip()
        if ip:
            return ip
    except Exception as exc:  # noqa: BLE001
        logger.debug("[FailoverLifecycle] REST node IP resolve fail-soft err=%r", exc)
    if _gcloud_fallback_enabled():
        return _gcloud_resolve_node_ip()
    return ""


def _gcloud_resolve_node_ip() -> str:
    """LAST-RESORT gcloud-describe node-IP resolution (gated). Prefers the
    external NAT IP, falls back to the internal IP. NEVER raises."""
    project = _env_str("GCP_PROJECT_ID", "") or _env_str("GOOGLE_CLOUD_PROJECT", "")
    zone = _env_str("GCP_ZONE", "us-central1-a")
    node = _env_str("JARVIS_FAILOVER_NODE_NAME", _GCLOUD_NODE_NAME)

    fmt_external = (
        "get(networkInterfaces[0].accessConfigs[0].natIP)"
    )
    fmt_internal = "get(networkInterfaces[0].networkIP)"
    for fmt in (fmt_external, fmt_internal):
        cmd = [
            "gcloud", "compute", "instances", "describe", node,
            "--zone={}".format(zone), "--format={}".format(fmt),
        ]
        if project:
            cmd.append("--project={}".format(project))
        rc, out = _gcloud_run(cmd, timeout_s=30.0)
        ip = (out or "").strip().splitlines()[0].strip() if out else ""
        if rc == 0 and ip:
            return ip
    return ""


# Module-level boundary name kept stable for tests (they monkeypatch
# ``fl._resolve_node_ip`` with a sync lambda). The default is the async
# REST-primary resolver; _resolve_ip awaits it transparently when it is a
# coroutine, so both a sync test lambda and the async default work.
_resolve_node_ip = _default_resolve_node_ip


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
        warmup_fn: Optional[Callable[[], Awaitable[bool]]] = None,
        is_degrading_fn: Optional[Callable[[], bool]] = None,
        endpoint_publish_fn: Optional[Callable[[str], Any]] = None,
        flare_fn: Optional[Callable[[Dict[str, Any]], Any]] = None,
        degrade_streak_fn: Optional[Callable[[], int]] = None,
        in_flight_fn: Optional[Callable[[], int]] = None,
    ) -> None:
        self._vm_awaken_fn = vm_awaken_fn or _default_vm_awaken_fn
        self._vm_delete_fn = vm_delete_fn or _default_vm_delete_fn
        self._dw_probe_fn = dw_probe_fn or _default_dw_probe_fn
        self._node_ready_fn = node_ready_fn or _default_node_ready_fn
        self._clock_fn = clock_fn or _default_clock_fn
        self._route = route or _route()
        # Gap 2 -- early-degradation signal source. Default: the DW heartbeat
        # singleton's is_degrading() (resolved lazily so the heartbeat module
        # only loads when the early-prewarm sub-gate is armed). Injectable for
        # tests. Fail-soft: a missing/erroring source reads as "not degrading"
        # (fail-CLOSED -> reactive path only, no false pre-warm).
        self._is_degrading_fn = is_degrading_fn  # None -> lazy default
        # Gap 2 (hard escalation) -- the deep-probe drop STREAK source. Default:
        # the heartbeat singleton's consecutive_failures(). A sustained streak IS
        # the outage confirmation (forecast bypass). Injectable + fail-soft.
        self._degrade_streak_fn = degrade_streak_fn  # None -> lazy default
        # Zero-Drop Drain: count of in-flight J-Prime ops (HANDBACK awaits this
        # to reach 0 before teardown so no op is severed mid-generation).
        # Injectable; default resolves the live count or 0 (immediate teardown).
        self._in_flight_fn = in_flight_fn
        # Gap 3a -- endpoint publish boundary. Default: resolve the node IP and
        # write JARVIS_PRIME_URL / JARVIS_PRIME_HOST (where PrimeClient reads
        # its endpoint) + best-effort hot-swap a live PrimeClient. Injectable
        # for tests. Fail-soft: a publish error never blocks the SERVING
        # transition (the op is never lost).
        self._endpoint_publish_fn = endpoint_publish_fn  # None -> default
        # VRAM pre-warm boundary (Phase 3b+). Injectable for tests. Default:
        # construct a LocalPrimeClient pointed at jprime_endpoint() and call
        # warmup(timeout_s=_warmup_timeout_s()). None defers to _default_warmup_fn.
        self._warmup_fn = warmup_fn  # None -> default built lazily in _tick_awakening
        # Phase 3c -- DORMANT/AWAKENING -> SERVING re-entry hook. Fired once on
        # the transition into SERVING so the Cryo-DLQ ops sealed during the
        # outage can be drained back through intake (which re-routes them to
        # J-Prime via the generation seam). Default: the controller's own
        # drain_cryo_dlq bound to the intake router ingest (resolved lazily).
        # Injectable for testability. Fail-soft -- a hook error never blocks
        # the SERVING transition.
        self._on_serving_fn = on_serving_fn or self._default_on_serving

        # Immutable Trigger-Attribution Flare sink. Default: a high-priority
        # WARNING to the WAL (debug.log). Injectable for tests / a GCS sidecar.
        self._flare_fn = flare_fn or self._default_flare

        self._state = FailoverState.DORMANT
        self._endpoint: Optional[str] = None
        # IaC ephemeral firewall micro-perimeter: the rule name opened at AWAKEN,
        # torn down (alongside the node) on EVERY exit path. None == no hole open.
        self._ephemeral_fw_rule: Optional[str] = None
        # The model label of the actively-provisioned tier (drives model-aware
        # schema compaction). None until a tier is provisioned -> survival default.
        self._active_model_label: Optional[str] = None
        # Elastic GPU escalation lane (lazily built on first demand). Manages the
        # SECOND, concurrent GPU node lifecycle with crypto-namespaced assets.
        self._gpu_lane: Optional[Any] = None

        # Multi-Vector Awaken (Task CR2). The controller remembers WHY it
        # awakened (data-plane outage vs cloud-budget exhaustion) so a later
        # recovery strategy can branch on the vector. Inert ("") until a
        # transition stamps it; default-OFF keeps it byte-identical.
        self._awaken_reason: str = ""
        # Budget-exhaustion anchor (monotonic via clock_fn). Set by
        # note_budget_exhausted(); consumed (single-shot) by the dormant tick's
        # budget branch when the budget-awaken master flag is armed.
        self._budget_exhausted_at: Optional[float] = None
        # Rate-limit recovery anchor (Task CR5). An absolute WALL-CLOCK
        # (time.time()) wake-up deadline parsed from the DW 429's own
        # Retry-After / x-ratelimit-reset header. Set by note_rate_limited();
        # consumed by _probe_interval's header-aware branch (master flag armed)
        # to suspend the SERVING recovery probe until the provider's stated
        # reset, then cleared once the deadline passes. None -> legacy blind
        # forecast-driven interval.
        self._rate_limit_reset_ts: Optional[float] = None

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

    def active_jprime_model(self) -> str:
        """The model label of the actively-provisioned tier (drives model-aware
        compaction: a small model -> compact, a 32B GPU node -> full schema).
        Falls back to the configured survival-tier model. NEVER raises."""
        if self._active_model_label:
            return self._active_model_label
        try:
            from backend.core.ouroboros.governance.failover_tier import (  # noqa: PLC0415
                resolve_tier,
            )
            return resolve_tier().model_label  # default (survival) tier model
        except Exception:  # noqa: BLE001
            return "qwen2.5-coder:7b"

    # ------------------------------------------------------------------
    # Elastic GPU escalation lane (the SECOND, concurrent node)
    # ------------------------------------------------------------------
    @property
    def gpu_lane(self):
        """Lazily-built elastic GPU lane wired to REAL provision/reap boundaries.
        Provisions a crypto-namespaced ``gpu``-class node (its own VM + /32
        firewall, never colliding with the live CPU node), registers it in the
        Fleet Registry, and reaps it the instant its in-flight drains. Returns
        ``None`` if the lane module is unavailable (fail-soft)."""
        if self._gpu_lane is None:
            try:
                from backend.core.ouroboros.governance.failover_gpu_lane import (  # noqa: PLC0415
                    GpuEscalationLane,
                )
                self._gpu_lane = GpuEscalationLane(
                    provision_fn=self._provision_gpu_node,
                    reap_fn=self._reap_gpu_node,
                    outage_confirmed_fn=self._hard_outage_confirmed,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[FailoverLifecycle] gpu_lane build fail-soft err=%r", exc)
                return None
        return self._gpu_lane

    async def _provision_gpu_node(self) -> Optional[str]:
        """Provision the crypto-namespaced GPU node (quality tier): instances.insert
        + its OWN /32 firewall rule + race readiness -> register the external
        endpoint in the Fleet Registry. Returns the endpoint or None. NEVER raises."""
        try:
            from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
                get_compute_rest, resolve_local_public_ip,
            )
            from backend.core.ouroboros.governance.failover_tier import (  # noqa: PLC0415
                resolve_tier_for_op,
            )
            from backend.core.ouroboros.governance.failover_naming import (  # noqa: PLC0415
                node_name, firewall_name,
            )
            from backend.core.ouroboros.governance.fleet_registry import (  # noqa: PLC0415
                get_fleet_registry,
            )
            tier = resolve_tier_for_op(urgency="immediate", complexity="complex")
            if not tier.is_gpu:
                return None  # quality gate OFF -> never spend
            client = get_compute_rest()
            gpu_vm = node_name("gpu")
            ok, detail = await client.create_instance(
                startup_script=self._build_startup_script(require_gpu=True),
                name=gpu_vm,
                machine_type=tier.machine_type,
                image_family=tier.image_family,
                accelerator_type=tier.accelerator_type,
                accelerator_count=tier.accelerator_count,
            )
            if not ok:
                logger.warning("[FailoverLifecycle] GPU provision insert failed: %s", detail)
                return None
            # Own /32 firewall rule (crypto-namespaced -> no collision with cpu).
            if _ephemeral_fw_enabled():
                ip = await resolve_local_public_ip()
                if ip:
                    await client.create_firewall_rule(
                        name=firewall_name("gpu"), source_ip=ip, port=_failover_port(),
                    )
            internal_ip, external_ip = await client.get_node_endpoints(gpu_vm)
            candidates = [
                "http://{}:{}".format(h, _failover_port())
                for h in (external_ip, internal_ip) if h
            ]
            winner = await self._race_node_ready(candidates) if candidates else None
            if not winner:
                logger.warning("[FailoverLifecycle] GPU node never became ready -> reap")
                await self._reap_gpu_node()
                return None
            get_fleet_registry().register("gpu", winner)
            logger.info("[FailoverLifecycle] GPU node SERVING endpoint=%s vm=%s", winner, gpu_vm)
            return winner
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FailoverLifecycle] GPU provision fail-soft err=%r", exc)
            return None

    async def _reap_gpu_node(self) -> None:
        """Reap the GPU node: delete the VM + its firewall rule (by reconstructed
        crypto-namespaced names) + unregister from the Fleet Registry. The CPU
        node is untouched. Idempotent + fail-soft. NEVER raises."""
        try:
            from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
                get_compute_rest,
            )
            from backend.core.ouroboros.governance.failover_naming import (  # noqa: PLC0415
                node_name, firewall_name,
            )
            from backend.core.ouroboros.governance.fleet_registry import (  # noqa: PLC0415
                get_fleet_registry,
            )
            client = get_compute_rest()
            await asyncio.gather(
                client.delete_instance(node_name("gpu")),
                client.delete_firewall_rule(firewall_name("gpu")),
                return_exceptions=True,
            )
            get_fleet_registry().unregister("gpu")
            logger.info("[FailoverLifecycle] GPU node REAPED (CPU survives)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FailoverLifecycle] GPU reap fail-soft err=%r", exc)

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

    def note_budget_exhausted(self) -> None:
        """Anchor a cloud-budget-exhaustion event (DW refused on budget, no cloud
        fallback). The next dormant tick awakens J-Prime with reason
        BUDGET_EXHAUSTED. Idempotent; gated by the budget-awaken master flag at
        the tick."""
        if self._budget_exhausted_at is None:
            try:
                self._budget_exhausted_at = self._clock_fn()
            except Exception:  # noqa: BLE001
                self._budget_exhausted_at = 0.0

    def note_rate_limited(self, reset_ts: Optional[float] = None) -> None:
        """Anchor a DW rate-limit (429) recovery deadline from the provider's own
        Retry-After/x-ratelimit-reset. The SERVING probe sleeps until reset_ts
        instead of a blind interval. Fail-soft; ignored if reset_ts is past/None."""
        if reset_ts is not None and reset_ts > time.time():
            self._rate_limit_reset_ts = reset_ts

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

    async def _publish_endpoint(self) -> None:
        """Gap 3a -- resolve the node IP + write it where PrimeClient reads it.

        Steps (deterministic + logged; NEVER logs secrets):
          1. Resolve the awakened node's reachable IP (gcloud describe boundary,
             injectable). On "" -> publish nothing (PrimeProvider keeps its
             configured target). This is the fail-soft no-op.
          2. Compose ``http://<ip>:<port>`` and set it as ``self._endpoint``
             (so jprime_endpoint() advertises the live URL).
          3. Export ``JARVIS_PRIME_URL`` + ``JARVIS_PRIME_HOST`` (the two env
             vars PrimeClient resolves its base_url from) so a fresh PrimeClient
             picks the node up, and best-effort hot-swap any already-live
             PrimeClient via its update_endpoint() (Gap 3a completion).

        If a custom publish boundary was injected, defer to it entirely.
        """
        # Prefer the Reachability-Racer WINNER (already a reachable http://ip:port
        # bound on self._endpoint). Extract its host so we publish the SAME
        # address the racer just proved healthy -- not a re-resolved guess.
        won_ip = ""
        won_ep = self._endpoint or ""
        if won_ep.startswith("http://") or won_ep.startswith("https://"):
            try:
                host = won_ep.split("://", 1)[1].split("/", 1)[0]
                won_ip = host.rsplit(":", 1)[0] if ":" in host else host
            except Exception:  # noqa: BLE001
                won_ip = ""

        publish_fn = self._endpoint_publish_fn
        if publish_fn is not None:
            ip = won_ip or await self._resolve_ip()
            if ip:
                publish_fn(ip)
            return

        ip = won_ip or await self._resolve_ip()
        if not ip:
            logger.info(
                "[FailoverLifecycle] endpoint publish: node IP unresolved "
                "-- PrimeProvider keeps configured target (fail-soft no-op)"
            )
            return

        port = _failover_port()
        url = "http://{}:{}".format(ip, port)
        self._endpoint = url
        # Write the two env vars PrimeClient reads its endpoint from.
        os.environ["JARVIS_PRIME_URL"] = url
        os.environ["JARVIS_PRIME_HOST"] = ip
        logger.info(
            "[FailoverLifecycle] endpoint WIRED: JARVIS_PRIME_URL=%s "
            "JARVIS_PRIME_HOST=%s (PrimeProvider now targets the live node)",
            url, ip,
        )
        # Best-effort hot-swap a live PrimeClient (if the provider state already
        # holds one). Fail-soft -- the env vars above are the durable wire.
        self._hot_swap_prime_client(host=ip, port=port)

    @staticmethod
    async def _resolve_ip() -> str:
        """Call the module-level _resolve_node_ip boundary. Fail-soft -> "".

        The boundary may be sync (an injected test lambda) or async (the native
        Compute REST default) -- a coroutine result is awaited transparently."""
        try:
            result = _resolve_node_ip()
            if asyncio.iscoroutine(result):
                result = await result
            return str(result or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FailoverLifecycle] node IP resolve fail-soft err=%r", exc)
            return ""

    async def _race_node_ready(self, candidates: List[str]) -> Optional[str]:
        """The Asynchronous Reachability Racer -- dynamic topology resolution.

        Concurrently probe ALL candidate endpoints (internal hostname/IP +
        external natIP) and bind whichever returns a healthy 200 FIRST
        (``asyncio.wait(FIRST_COMPLETED)``). ZERO environment guessing -- no
        IS_LOCAL flag, no hardcoded host swap. Works identically on a local Mac
        (external natIP wins), a GCP pod (internal IP wins), or anywhere else.

        Returns the winning endpoint URL, or None if none answer this tick (the
        AWAKENING deadline remains the hard bound). Fail-soft throughout: a probe
        that raises is simply 'not reachable' and never wins."""
        cands = [c for c in (candidates or []) if c]
        if not cands:
            return None

        async def _probe(ep: str) -> str:
            ok = await self._maybe_await(self._node_ready_fn, ep)
            if ok:
                return ep
            raise _NodeNotReachable(ep)

        pending = {asyncio.ensure_future(_probe(ep)) for ep in cands}
        winner: Optional[str] = None
        try:
            while pending and winner is None:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED,
                )
                for d in done:
                    try:
                        winner = d.result()
                        break
                    except Exception:  # noqa: BLE001 -- not-reachable / probe error
                        continue
        finally:
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if winner is not None:
            logger.info(
                "[FailoverLifecycle] Reachability Racer: %d candidate(s) -> WINNER "
                "%s (dynamically bound; no env flag)", len(cands), winner,
            )
        return winner

    async def _l7_ready_backoff(
        self, candidates: List[str], *, budget_s: float,
    ) -> Optional[str]:
        """L7 Readiness Poller. Race the candidates; on a connection refused / RST
        (the VM is up but the inference daemon is still initializing -- NOT a
        failure), keep polling with EXPONENTIAL BACKOFF until a candidate returns
        a Layer-7 healthy 200, or the budget is exhausted. Bounded; the AWAKENING
        deadline is the hard outer bound across ticks. Returns winner or None."""
        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + max(0.0, budget_s)
        except Exception:  # noqa: BLE001
            loop = None
            deadline = None
        delay = max(0.01, _ready_backoff_base_s())
        cap = max(delay, _ready_backoff_cap_s())
        while True:
            winner = await self._race_node_ready(candidates)
            if winner:
                return winner
            if deadline is not None and loop is not None and loop.time() >= deadline:
                return None
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return None
            delay = min(delay * 2.0, cap)  # exponential, capped

    async def _candidate_endpoints(self) -> List[str]:
        """Build the Reachability-Racer candidate set with NO env-flag branching:
        the external natIP + the internal IP (single instances.get) PLUS the
        configured hostname endpoint as a last resort. The racer probes them all
        concurrently and binds whichever answers first. Fail-soft -> at minimum
        the hostname candidate (early ticks before the node has IPs)."""
        port = _failover_port()
        cands: List[str] = []
        try:
            from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
                get_compute_rest,
            )
            internal, external = await get_compute_rest().get_node_endpoints()
            for ip in (external, internal):  # external first (off-VPC most common)
                if ip:
                    ep = "http://{}:{}".format(ip, port)
                    if ep not in cands:
                        cands.append(ep)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] candidate-endpoint resolve fail-soft err=%r", exc)
        host_ep = self._build_endpoint()
        if host_ep not in cands:
            cands.append(host_ep)
        return cands

    @staticmethod
    def _hot_swap_prime_client(*, host: str, port: int) -> None:
        """Best-effort: hot-swap a live PrimeClient to the awakened node.

        Resolves the PrimeProviderState's injected client lazily (no import
        cycle, no hard dependency in unit tests). If the client exposes an
        async ``update_endpoint(host, port)`` we schedule it; otherwise this is
        a no-op (the env-var wire above is the durable path). Fail-soft."""
        try:
            from backend.core.ouroboros.governance._governance_state import (  # noqa: PLC0415
                get_prime_provider_state,
            )
            state = get_prime_provider_state()
            client = getattr(state, "client", None)
            update = getattr(client, "update_endpoint", None)
            if not callable(update):
                return
            result = update(host, port)
            if asyncio.iscoroutine(result):
                # Schedule on the running loop without blocking the FSM tick.
                try:
                    asyncio.ensure_future(result)
                except RuntimeError:
                    # No running loop (sync context) -- close the coroutine to
                    # avoid a "never awaited" warning. Env wire still applies.
                    result.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] prime client hot-swap fail-soft err=%r", exc)

    def _build_default_warmup_fn(self) -> Callable[[], Awaitable[bool]]:
        """Build the default warmup callable: LocalPrimeClient pointed at
        the awakened endpoint, calling warmup(timeout_s=_warmup_timeout_s()).

        Built lazily here (not in __init__) so the endpoint is known and the
        local_inference_director import only happens when needed (OFF path
        never touches this). Returns an async callable -> bool.
        """
        from backend.core.ouroboros.governance.local_inference_director import (  # noqa: PLC0415
            LocalConfig,
            LocalPrimeClient,
        )
        endpoint = self._endpoint or self._build_endpoint()
        timeout = _warmup_timeout_s()

        import dataclasses  # noqa: PLC0415
        # Build a config that targets the awakened node's base URL.
        base_cfg = LocalConfig.from_env()
        node_cfg = dataclasses.replace(base_cfg, base_url=endpoint)
        client = LocalPrimeClient(node_cfg)

        async def _warmup() -> bool:
            try:
                return await client.warmup(timeout_s=timeout)
            finally:
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001
                    pass

        return _warmup

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

        outage = self._real_outage()

        if outage:
            # Observed full outage. Apply the cryo-trigger cost gate (reactive).
            if not self._should_awaken(now=now):
                return
            await self._enter_awakening(
                now=now, trigger="reactive_outage", route=self._first_outage_route(),
            )
            return

        # Gap 2 -- HARD escalation. A sustained streak of deep-probe drops IS the
        # outage confirmation (the data plane is confirmed dead). Forcefully
        # awaken WITHOUT waiting for a slow-recovery forecast / 120s confirm
        # window -- the streak threshold already encodes that confirmation.
        if self._hard_outage_confirmed():
            logger.warning(
                "[FailoverLifecycle] HARD OUTAGE escalation: deep-probe drop "
                "streak=%d >= %d -> FORCED AWAKEN (forecast/confirm bypass)",
                self._degrade_streak_value(), _hard_outage_streak(),
            )
            await self._enter_awakening(
                now=now, trigger="heartbeat_hard_outage", route=self._route,
            )
            return

        # Gap 2 -- EARLY pre-warm path. NOT yet a full outage, but the heartbeat
        # reports DEGRADATION. If the forecast also says recovery is slow
        # (R > C*margin, HIGH confidence), pre-warm J-Prime NOW so the node is
        # warm by the time DW formally collapses and the op drops into the
        # Cryo-DLQ. Fail-CLOSED: not-degrading / low-confidence forecast / blip
        # (R < C*margin) -> fall through (no behavior loss; the reactive path
        # above remains the backstop on a later tick once the window fills).
        if self._should_early_prewarm(now=now):
            logger.info(
                "[FailoverLifecycle] EARLY PRE-WARM: DW degrading + slow "
                "forecast (R>C*margin) -> awakening J-Prime ahead of formal "
                "outage (route=%s)", self._route,
            )
            await self._enter_awakening(
                now=now, trigger="heartbeat_early_prewarm", route=self._route,
            )
            return

        # Multi-Vector Awaken (Task CR2) -- BUDGET-EXHAUSTION vector. The LAST
        # branch so a REAL data-plane outage (the branches above) always takes
        # precedence. The cloud primary refused on budget with NO cloud fallback
        # (anchored by note_budget_exhausted from candidate_generator's
        # no-fallback exhaustion exit). Gated default-OFF -> byte-identical.
        # Single-shot: consume the anchor before awakening so it can't re-fire
        # every tick.
        if budget_awaken_enabled() and self._budget_exhausted_at is not None:
            self._budget_exhausted_at = None
            await self._enter_awakening(
                now=now, trigger="session_budget_exhausted", route=self._route,
            )
            return

    def _real_outage(self) -> bool:
        """The AUTHORITATIVE real-generation-failure awaken signal.

        Default (sub-gate OFF): the legacy single-route check --
        ``is_global_outage(self._route)`` -- byte-identical to before.

        When ``JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED`` is ARMED: ANY tracked
        generation route (plus the configured ``self._route`` + any explicit
        ``JARVIS_FAILOVER_OUTAGE_ROUTES``) reaching the SAME full-window rate==0
        ``is_global_outage`` trips the awaken. This closes the run-#11 blindspot
        where the BACKGROUND route collapsed (rate==0) while the FSM watched only
        ``"dw"`` -- a key the live record_sweep path never populates. Same
        threshold as the quarantine Cryo-DLQ seal (fail-CLOSED: a transient blip
        or not-yet-full window never trips it). Fail-soft -> False (stay
        reactive; no spurious awaken)."""
        try:
            grad = self._gradient()
        except Exception:  # noqa: BLE001
            return False
        if not _any_route_outage_enabled():
            try:
                return bool(grad.is_global_outage(self._route))
            except Exception:  # noqa: BLE001
                return False
        # Authoritative any-route path. Fold in the configured route + any
        # operator-pinned extras so a route that hasn't recorded a sweep yet is
        # still considered (harmless -- an empty/not-full window reads not-outage).
        extra = [self._route] + _outage_extra_routes()
        try:
            any_fn = getattr(grad, "any_route_in_outage", None)
            if callable(any_fn):
                return bool(any_fn(extra_routes=extra))
            # Fail-CLOSED fallback if the gradient predates the helper: union of
            # the configured route + extras (no dynamic enumeration available).
            return any(
                bool(grad.is_global_outage(r)) for r in extra if r
            )
        except Exception:  # noqa: BLE001
            return False

    def _should_early_prewarm(self, *, now: float) -> bool:
        """Gap-2 decision: degradation + slow forecast -> pre-warm early?

        Composes three independent gates (ALL must hold; fail-CLOSED on any):
          1. The early-prewarm sub-gate is armed (env).
          2. The heartbeat reports is_degrading() (DEGRADED, not yet outage).
          3. The cost gate says it is worth paying the spin-up: HIGH-confidence
             forecast with R(p50) > C*margin. LOW_CONFIDENCE -> decline (R is
             unreliable; the reactive floor still applies once the window fills).
        Pure-ish + fail-soft: any error -> False (decline -> reactive only).
        """
        if not _early_prewarm_enabled():
            return False
        try:
            if not self._is_degrading():
                return False
        except Exception:  # noqa: BLE001
            return False

        try:
            forecast = self._get_forecast()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] early-prewarm forecast fail-soft err=%r", exc)
            return False

        confidence = getattr(forecast, "confidence", "LOW_CONFIDENCE")
        if confidence != "HIGH":
            # Fail-CLOSED: R unreliable -> do NOT speculatively pre-warm; the
            # reactive is_global_outage path handles the real outage.
            return False

        r = float(getattr(forecast, "p50_s", 0.0))
        threshold = _coldstart_s() * _awaken_margin()
        decision = r > threshold
        logger.info(
            "[FailoverLifecycle] early-prewarm gate: degrading=True HIGH conf "
            "R(p50)=%.1f threshold(C*margin)=%.1f -> %s",
            r, threshold,
            "PRE-WARM" if decision else "BLIP-SKIP (hold; reactive backstop)",
        )
        return decision

    def _degrade_streak_value(self) -> int:
        """Resolve the deep-probe drop STREAK (Gap 2). Default: the DW heartbeat
        singleton's consecutive_failures(). Injectable + fail-soft (0 on error)."""
        fn = self._degrade_streak_fn
        if fn is None:
            try:
                from backend.core.ouroboros.governance.provider_heartbeat import (  # noqa: PLC0415
                    get_dw_heartbeat,
                )
                fn = get_dw_heartbeat().consecutive_failures
            except Exception as exc:  # noqa: BLE001
                logger.debug("[FailoverLifecycle] streak resolve fail-soft err=%r", exc)
                return 0
        try:
            return int(fn())
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] degrade_streak fail-soft err=%r", exc)
            return 0

    def _hard_outage_confirmed(self) -> bool:
        """Gap 2 -- True iff the sustained deep-probe drop streak has reached the
        hard-outage threshold (a CONFIRMED dead data plane). Fail-soft -> False."""
        if not _hard_escalation_enabled():
            return False
        return self._degrade_streak_value() >= _hard_outage_streak()

    def _is_degrading(self) -> bool:
        """Resolve the early-degradation signal. Default: the DW heartbeat
        singleton's is_degrading(). Injectable + fail-soft (False on error)."""
        fn = self._is_degrading_fn
        if fn is None:
            try:
                from backend.core.ouroboros.governance.provider_heartbeat import (  # noqa: PLC0415
                    get_dw_heartbeat,
                )
                fn = get_dw_heartbeat().is_degrading
            except Exception as exc:  # noqa: BLE001
                logger.debug("[FailoverLifecycle] heartbeat resolve fail-soft err=%r", exc)
                return False
        try:
            return bool(fn())
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] is_degrading fail-soft err=%r", exc)
            return False

    def _default_flare(self, payload: Dict[str, Any]) -> None:
        """Default Trigger-Attribution Flare sink: a high-priority WARNING to the
        WAL (debug.log). The ``[FailoverFlare]`` prefix is grep-stable for the
        flight recorder. Fail-soft -- a logging error never blocks the awaken."""
        try:
            import json  # noqa: PLC0415
            logger.warning("[FailoverFlare] %s", json.dumps(payload, sort_keys=True))
        except Exception:  # noqa: BLE001
            logger.warning("[FailoverFlare] %r", payload)

    def _emit_flare(self, *, trigger: str, route: str, now: float) -> None:
        """Synchronously flush the immutable trigger-attribution payload at the
        DORMANT -> AWAKENING instant -- BEFORE the GCE boot is attempted, so the
        attribution survives even a fail-soft awaken. Fail-soft."""
        payload = {
            "event": "awaken_trigger",
            "trigger": trigger,
            "route": route,
            "state_from": "DORMANT",
            "state_to": "AWAKENING",
            "ts": now,
            "node": _env_str("JARVIS_FAILOVER_NODE_NAME", _GCLOUD_NODE_NAME),
        }
        try:
            self._flare_fn(payload)
        except Exception as exc:  # noqa: BLE001 -- telemetry never blocks failover
            logger.debug("[FailoverLifecycle] flare fail-soft err=%r", exc)

    def _first_outage_route(self) -> str:
        """The first tracked route in a full outage (for flare attribution).
        Falls back to the configured route. NEVER raises."""
        try:
            grad = self._gradient()
            for r in list(grad.tracked_routes()) + [self._route] + _outage_extra_routes():
                if r and grad.is_global_outage(r):
                    return str(r)
        except Exception:  # noqa: BLE001
            pass
        return self._route

    async def _enter_awakening(
        self, *, now: float, trigger: str = "unknown", route: str = "",
    ) -> None:
        """Shared DORMANT -> AWAKENING transition (reactive + early-prewarm).

        Emits the immutable Trigger-Attribution Flare FIRST so the flight
        recorder captures which signal initiated the failover, regardless of the
        GCE boot outcome."""
        self._emit_flare(trigger=trigger, route=route, now=now)
        # Multi-Vector Awaken (Task CR2): remember WHY we awakened so a later
        # recovery strategy can branch on the vector. Derive from the trigger;
        # default to DATA_PLANE (the data-plane outage is the legacy vector).
        _reason_by_trigger = {
            "session_budget_exhausted": AWAKEN_REASON_BUDGET,
            "reactive_outage": AWAKEN_REASON_DATA_PLANE,
            "heartbeat_hard_outage": AWAKEN_REASON_DATA_PLANE,
            "heartbeat_early_prewarm": AWAKEN_REASON_DATA_PLANE,
            "rate_limited": AWAKEN_REASON_RATE_LIMIT,  # CR5
        }
        self._awaken_reason = _reason_by_trigger.get(
            trigger, AWAKEN_REASON_DATA_PLANE
        )
        # CR5: a live rate-limit anchor (set by note_rate_limited from the DW
        # 429's own Retry-After/x-ratelimit-reset header) means the recovery
        # strategy is header-aware regardless of which trigger initiated the
        # awaken. Override to RATE_LIMIT so _probe_interval's header-aware branch
        # can suspend the SERVING probe until the provider's stated reset.
        if self._rate_limit_reset_ts is not None:
            self._awaken_reason = AWAKEN_REASON_RATE_LIMIT
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
            # The primary node's runtime GPU gate follows its resolved tier (the
            # survival 7B/CPU tier has no GPU -> no gate; a quality GPU tier does).
            _require_gpu = False
            try:
                from backend.core.ouroboros.governance.failover_tier import (  # noqa: PLC0415
                    resolve_tier,
                )
                _require_gpu = resolve_tier(
                    urgency=_env_str("JARVIS_FAILOVER_AWAKEN_URGENCY", ""),
                    complexity=_env_str("JARVIS_FAILOVER_AWAKEN_COMPLEXITY", ""),
                ).is_gpu
            except Exception:  # noqa: BLE001
                _require_gpu = False
            startup_script = self._build_startup_script(require_gpu=_require_gpu)
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
        # Record the active tier's model (drives model-aware schema compaction).
        # Deterministic from the same env the awaken_fn resolved, so they agree.
        try:
            from backend.core.ouroboros.governance.failover_tier import (  # noqa: PLC0415
                resolve_tier,
            )
            self._active_model_label = resolve_tier(
                urgency=_env_str("JARVIS_FAILOVER_AWAKEN_URGENCY", ""),
                complexity=_env_str("JARVIS_FAILOVER_AWAKEN_COMPLEXITY", ""),
            ).model_label
        except Exception:  # noqa: BLE001
            self._active_model_label = None
        # IaC ephemeral micro-perimeter: open a /32 hole for THIS orchestrator's
        # detected egress IP so the Reachability Racer's external path can reach
        # the node. Fail-soft -- a firewall miss never blocks awaken (the racer
        # simply won't win the external candidate; the node still self-reaps).
        await self._open_ephemeral_perimeter()

    async def _open_ephemeral_perimeter(self) -> None:
        """Programmatically open a /32 ``tcp:PORT`` firewall rule bound to the
        orchestrator's OWN dynamically-resolved public egress IP. Gated + fail-
        soft. NEVER raises. NO hardcoded IP, NO 0.0.0.0/0."""
        if not _ephemeral_fw_enabled():
            return
        try:
            from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
                get_compute_rest, resolve_local_public_ip,
            )
            ip = await resolve_local_public_ip()
            if not ip:
                logger.warning(
                    "[FailoverLifecycle] ephemeral perimeter: public IP unresolved "
                    "-- skipping (racer external path may be firewall-blocked)")
                return
            name = _ephemeral_fw_name()
            ok, detail = await get_compute_rest().create_firewall_rule(
                name=name, source_ip=ip, port=_failover_port(),
            )
            if ok:
                self._ephemeral_fw_rule = name
                logger.warning(
                    "[FailoverLifecycle] ephemeral micro-perimeter OPEN: %s "
                    "src=%s/32 tcp:%s (%s) -- bound to node lifecycle",
                    name, ip, _failover_port(), detail,
                )
            else:
                logger.warning(
                    "[FailoverLifecycle] ephemeral perimeter create failed: %s", detail)
        except Exception as exc:  # noqa: BLE001 -- never block awaken
            logger.warning("[FailoverLifecycle] ephemeral perimeter fail-soft err=%r", exc)

    async def _close_ephemeral_perimeter(self) -> None:
        """Delete the ephemeral firewall rule (the IaC teardown half). Idempotent
        + fail-soft. Clears the bound name so a re-awaken re-opens cleanly."""
        name = self._ephemeral_fw_rule
        if not name:
            return
        try:
            from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
                get_compute_rest,
            )
            ok, detail = await get_compute_rest().delete_firewall_rule(name)
            logger.warning(
                "[FailoverLifecycle] ephemeral micro-perimeter CLOSED: %s (%s)",
                name, detail,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FailoverLifecycle] ephemeral perimeter close fail-soft err=%r", exc)
        finally:
            self._ephemeral_fw_rule = None

    def _build_startup_script(self, *, require_gpu: bool = False) -> str:
        from backend.core.ouroboros.governance.failover_deadman import (  # noqa: PLC0415
            build_deadman_startup_script, build_inference_bind_block,
        )
        port = _failover_port()
        script = build_deadman_startup_script(port=port)
        # Dynamic cloud-init: force the inference daemon to bind 0.0.0.0:<port>
        # so the hybrid orchestrator can reach it through the /32 firewall. Gated
        # (default OFF -> byte-identical dead-man-only legacy). Injected right
        # after the dead-man shebang/HOME preamble so it runs early on boot.
        # require_gpu adds a runtime nvidia-smi hardware gate (quality 32B tier --
        # the image is baked on CPU, so the GPU is validated HERE at runtime).
        if _enabled("JARVIS_FAILOVER_INFERENCE_BIND_ENABLED", "false"):
            bind = build_inference_bind_block(port=port, require_gpu=require_gpu)
            lines = script.split("\n", 1)
            if len(lines) == 2 and lines[0].startswith("#!"):
                script = lines[0] + "\n" + bind + "\n" + lines[1]
            else:
                script = bind + "\n" + script
        return script

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
                # PARALLEL teardown (fail-soft): node delete + ephemeral firewall
                # close together -- zero orphan nodes AND zero orphan firewall holes.
                try:
                    await asyncio.gather(
                        self._maybe_await(self._vm_delete_fn),
                        self._close_ephemeral_perimeter(),
                        return_exceptions=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[FailoverLifecycle] AWAKENING timeout teardown fail-soft "
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

        # Observed ensure-ready gate via the Reachability Racer: probe BOTH the
        # external natIP and the internal IP/hostname concurrently and bind
        # whichever answers a healthy 200 FIRST. Dynamic topology resolution --
        # no IS_LOCAL flag, no hardcoded host swap. Only -> SERVING once a
        # candidate answers; otherwise keep waiting (next tick re-races).
        try:
            winner = await self._l7_ready_backoff(
                await self._candidate_endpoints(),
                budget_s=_ready_backoff_budget_s(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] reachability race fail-soft err=%r", exc)
            winner = None
        if not winner:
            return  # keep waiting; next tick re-probes (fail-soft).
        # Bind the winning reachable endpoint -- this is what gets published.
        self._endpoint = winner

        # VRAM pre-warm gate (Phase 3b+): after the node transport is up but
        # BEFORE transitioning to SERVING, fire a lightweight dummy generation
        # to force model weights into VRAM. The awaited completion IS the
        # readiness signal -- no arbitrary sleep. Fail-soft: if warmup times
        # out or errors, log a warning and proceed to SERVING anyway (the outer
        # AWAKENING deadline is still the hard bound; we never deadlock here).
        if _warmup_enabled():
            warmup_ok = False
            try:
                warmup_fn = self._warmup_fn or self._build_default_warmup_fn()
                warmup_ok = bool(await self._maybe_await(warmup_fn))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[FailoverLifecycle] warmup raised fail-soft err=%r "
                    "-- proceeding to SERVING (first op may be cold)", exc,
                )
            if not warmup_ok:
                logger.warning(
                    "[FailoverLifecycle] warmup did not confirm within %.0fs "
                    "-- proceeding to SERVING (first op may be cold)",
                    _warmup_timeout_s(),
                )

        # Gap 3a -- WIRE the awakened node's reachable endpoint to PrimeClient
        # BEFORE flipping to SERVING, so the moment the FSM advertises
        # is_jprime_serving()/jprime_endpoint(), PrimeProvider already points at
        # the live node. Fail-soft ABSOLUTE: a publish error never blocks (or
        # reverts) the SERVING transition (the op is never lost -- PrimeProvider
        # keeps its configured target on a publish miss).
        try:
            await self._maybe_await(self._publish_endpoint)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[FailoverLifecycle] endpoint publish fail-soft err=%r "
                "-- SERVING proceeds (PrimeProvider keeps configured target)", exc,
            )

        self._awakening_started_at = None
        self._state = FailoverState.SERVING
        self._serving_started_at = now
        self._last_probe_at = None
        self._recovered_streak = 0
        # Fleet Registry: publish the CPU (survival) node's endpoint so the
        # per-op router can resolve it independently of the elastic GPU node.
        try:
            from backend.core.ouroboros.governance.fleet_registry import (  # noqa: PLC0415
                get_fleet_registry,
            )
            if self._endpoint:
                get_fleet_registry().register("cpu", self._endpoint)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] fleet register(cpu) fail-soft err=%r", exc)
        logger.info("[FailoverLifecycle] SERVING via J-Prime endpoint=%s", self._endpoint)
        # ABSOLUTE HANDOFF PROOF: record the exact winning endpoint the GENERATE
        # queue is now routed to (the Reachability-Racer winner + the wired
        # JARVIS_PRIME_URL). The Cryo-DLQ drain below replays the sealed ops
        # through this endpoint; their per-op generation results are the
        # downstream cryptographic confirmation that J-Prime processed them.
        try:
            self._emit_flare(
                trigger="serving_handoff", route=self._route, now=now,
            )
            logger.warning(
                "[FailoverHandoff] queue ROUTED to J-Prime winner endpoint=%s "
                "prime_url=%s -- draining Cryo-DLQ ops to the live cloud node",
                self._endpoint, os.environ.get("JARVIS_PRIME_URL", "<unset>"),
            )
        except Exception as exc:  # noqa: BLE001 -- proof telemetry never blocks
            logger.debug("[FailoverLifecycle] handoff proof fail-soft err=%r", exc)

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
        # #2 -- deep-probe recovery: the background DW heartbeat reporting N
        # consecutive fast-healthy probes is an INDEPENDENT recovery signal
        # (data-plane, faithful) that also satisfies the recovery gate.
        deep_ok = self._deep_probe_recovered()

        if (hyst_ok or deep_ok) and uptime_ok:
            logger.info(
                "[FailoverLifecycle] HANDBACK gate passed: gradient_streak=%d (>=%d) "
                "deep_probe_recovered=%s uptime=%.1fs (>=%.1fs)",
                self._recovered_streak, _hysteresis_cycles(), deep_ok,
                self._jprime_uptime(now=now), _min_uptime_s(),
            )
            self._state = FailoverState.HANDBACK
            await self._tick_handback(now=now)

    def _deep_probe_recovered(self) -> bool:
        """True iff the DW heartbeat reports DW recovered (N consecutive
        fast-healthy deep probes). Injectable-free (reads the singleton);
        fail-soft -> False (stay SERVING; never a premature handback)."""
        try:
            from backend.core.ouroboros.governance.provider_heartbeat import (  # noqa: PLC0415
                get_dw_heartbeat,
            )
            return bool(get_dw_heartbeat().dw_recovered(
                min_streak=_recovery_streak_n(),
                max_latency_s=_recovery_max_latency_s(),
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] deep-probe recovery fail-soft err=%r", exc)
            return False

    def _probe_interval(self, *, now: float) -> float:
        # CR5 -- Header-aware DW-recovery sleep. Default-OFF: when the master flag
        # is unset this whole branch is skipped and the method is byte-identical
        # to the legacy forecast-driven jitter backoff below. When ARMED and we
        # awakened on a rate-limit (429) carrying the provider's own
        # Retry-After/x-ratelimit-reset deadline, suspend the SERVING probe until
        # that exact wall-clock reset instead of blind polling. The wait then
        # falls through to the SAME semantic deep probe (_deep_probe_recovered),
        # which still gates handback on real generation success.
        if (
            header_aware_recovery_enabled()
            and self._awaken_reason == AWAKEN_REASON_RATE_LIMIT
            and self._rate_limit_reset_ts is not None
        ):
            remaining = self._rate_limit_reset_ts - time.time()
            if remaining > 0:
                # Header-aware async sleep: suspend the probe until the provider's
                # own reset deadline -- zero blind polling. (asyncio.sleep happens
                # via the FSM tick gate; we just return the exact interval.)
                return max(0.0, remaining)
            self._rate_limit_reset_ts = None  # deadline passed -> resume normal probing
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
        #    is_jprime_serving()/jprime_endpoint() stop pointing at the node and
        #    ALL NEW ops route to DW instantly (T4 re-routes to DW).
        self._endpoint = None
        # Fleet teardown: unpublish the CPU node + force-reap any elastic GPU node
        # (both must vacate on handback so nothing keeps billing or routing).
        try:
            from backend.core.ouroboros.governance.fleet_registry import (  # noqa: PLC0415
                get_fleet_registry,
            )
            get_fleet_registry().unregister("cpu")
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] fleet unregister(cpu) fail-soft err=%r", exc)
        try:
            if self._gpu_lane is not None:
                await self._gpu_lane.drain_and_reap()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] gpu lane reap fail-soft err=%r", exc)

        # 2.5 ZERO-DROP DRAIN: await any IN-FLIGHT J-Prime ops to finish before
        #     teardown so none is severed mid-generation. Bounded by a budget
        #     (never deadlock; the Dead-Man's Switch is the cost backstop).
        await self._drain_inflight_jprime()

        # 3. Delete-to-snapshot + PARALLEL ephemeral firewall close (zero orphan
        #    nodes AND zero orphan firewall holes on handback).
        try:
            results = await asyncio.gather(
                self._maybe_await(self._vm_delete_fn),
                self._close_ephemeral_perimeter(),
                return_exceptions=True,
            )
            if results and results[0] is False:
                logger.warning(
                    "[FailoverLifecycle] delete returned falsy -- Dead-Man's Switch "
                    "remains the cost backstop"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[FailoverLifecycle] handback teardown raised -- Dead-Man's Switch "
                "backstop err=%r", exc,
            )

        # 4. DORMANT + arm anti-thrash cooldown.
        self._state = FailoverState.DORMANT
        self._awakening_started_at = None
        self._serving_started_at = None
        self._last_probe_at = None
        self._recovered_streak = 0
        self._probe_trajectory = []
        self._outage_started_at = None
        self._last_handback_at = now  # arm the anti-thrash cooldown anchor
        logger.info("[FailoverLifecycle] DORMANT (delete-to-snapshot complete)")

    def _inflight_count(self) -> int:
        """Count of in-flight J-Prime ops. An explicit injected ``in_flight_fn``
        wins; otherwise lazily resolves the LIVE generation count from the
        providers module (``get_jprime_inflight_count`` -- the PrimeProvider
        increments it for the duration of each J-Prime generation). Symmetric
        with how ``_is_degrading`` resolves the heartbeat. NEVER raises -> 0
        (fail-open to teardown; the Dead-Man's Switch backstops a wrong 0)."""
        fn = self._in_flight_fn
        if fn is None:
            try:
                from backend.core.ouroboros.governance.providers import (  # noqa: PLC0415
                    get_jprime_inflight_count,
                )
                fn = get_jprime_inflight_count
            except Exception as exc:  # noqa: BLE001
                logger.debug("[FailoverLifecycle] inflight resolve fail-soft err=%r", exc)
                return 0
        try:
            return max(0, int(fn()))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FailoverLifecycle] in_flight_fn fail-soft err=%r", exc)
            return 0

    async def _drain_inflight_jprime(self) -> None:
        """Await in-flight J-Prime ops to drain to 0 before teardown (zero-drop).
        Bounded by ``_handback_drain_budget_s`` so the FSM never deadlocks; on
        budget exhaustion we proceed (the node Dead-Man's Switch is the backstop).
        Fail-soft throughout."""
        try:
            n = self._inflight_count()
            if n <= 0:
                return
            logger.info(
                "[FailoverLifecycle] HANDBACK zero-drop: awaiting %d in-flight "
                "J-Prime op(s) to finish before teardown", n,
            )
            loop = asyncio.get_event_loop()
            deadline = loop.time() + _handback_drain_budget_s()
            poll = _handback_drain_poll_s()
            while self._inflight_count() > 0:
                if loop.time() >= deadline:
                    logger.warning(
                        "[FailoverLifecycle] HANDBACK drain budget exhausted with "
                        "%d op(s) still in flight -- proceeding to teardown "
                        "(Dead-Man's Switch backstop)", self._inflight_count(),
                    )
                    return
                await asyncio.sleep(poll)
            logger.info("[FailoverLifecycle] HANDBACK zero-drop: J-Prime queue drained to 0")
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 -- never block teardown
            logger.debug("[FailoverLifecycle] drain fail-soft err=%r", exc)

    # ------------------------------------------------------------------
    # Violent Ephemeral Teardown (Task CR4)
    # ------------------------------------------------------------------

    async def force_teardown(self, *, reason: str = "a1_terminal") -> None:
        """Deterministically reap the J-Prime (GPU) node the instant the A1 DAG
        hits a terminal state -- zero idle GPU while waiting for human review.

        Reuses the proven parallel-teardown idiom (GPU node + GPU /32 firewall,
        then node + ephemeral /32 firewall vacate together via ``asyncio.gather``)
        so nothing keeps billing or routing. Idempotent + fail-soft: a no-op when
        already DORMANT; NEVER raises. After reaping, drops to DORMANT and arms the
        same anti-thrash cooldown anchor the HANDBACK path uses.

        The body is wrapped in ``self._lock`` so that a concurrent ``tick()``
        mid-transition (e.g. _do_awaken) cannot race the state + endpoint
        mutations here -- matching every other FSM transition."""
        async with self._lock:
            if self._state == FailoverState.DORMANT:
                return  # nothing to reap
            logger.warning(
                "[FailoverFlare] VIOLENT TEARDOWN reason=%s state=%s -- reaping GPU node now",
                reason, self._state.name,
            )
            # 1. Reap the elastic GPU node + its /32 firewall (CPU node survives until
            #    the node-delete below). Fail-soft -- never block the rest of teardown.
            try:
                await self._reap_gpu_node()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[FailoverFlare] violent teardown gpu-reap error (continuing): %s", exc)
            # 2. Delete-to-snapshot the node + close the ephemeral /32 perimeter -- the
            #    SAME guaranteed-parallel gather the FSM teardowns use (zero orphan node
            #    AND zero orphan firewall hole; the lock-race fix).
            try:
                await asyncio.gather(
                    self._maybe_await(self._vm_delete_fn),
                    self._close_ephemeral_perimeter(),
                    return_exceptions=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[FailoverFlare] violent teardown node/fw error (continuing): %s", exc)
            # 3. DORMANT + arm the anti-thrash cooldown anchor + drop the endpoint so
            #    is_jprime_serving()/jprime_endpoint() stop pointing at the dead node.
            self._state = FailoverState.DORMANT
            self._last_handback_at = self._clock_fn()  # arm anti-thrash cooldown
            self._endpoint = None
            logger.info("[FailoverFlare] VIOLENT TEARDOWN complete -- DORMANT, cooldown armed")

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
    "budget_awaken_enabled",
    "violent_teardown_enabled",
    "header_aware_recovery_enabled",
    "AWAKEN_REASON_DATA_PLANE",
    "AWAKEN_REASON_BUDGET",
    "AWAKEN_REASON_RATE_LIMIT",
]

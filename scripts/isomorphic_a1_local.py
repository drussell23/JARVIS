"""isomorphic_a1_local.py -- Full-chain local A1 E2E driver (Task 6).

Composes the Isomorphic Local Sandbox Tasks 1-5 into a single runnable proof:

  T1  IsomorphicEnv             -- path/env/policy parity with the GCP soak node
  T2  repo_root injection fix   -- already in GovernedLoopConfig (no new code)
  T3  SyntheticAdversary        -- deterministic provider chaos via env-URL swap
  T4  failover trigger fix      -- already in candidate_generator (no new code)
  T5  capture_failure_telemetry -- fail-soft FSM/memory/causal dump on any failure

Isomorphism across the process boundary (final-review fix)
-----------------------------------------------------------
The driver imposes isomorphism on the launched organism via env-propagated policy
and disjoint cwd (process mode).  Two mechanisms work together:

1. ``IsomorphicEnv`` sets ``JARVIS_SANDBOX_PREFIXES`` in ``os.environ`` before the
   soak subprocess is spawned.  ``compose_env()`` copies ``os.environ``, so the
   child process inherits the restricted node sandbox-prefix allowlist.
   ``test_runner._effective_sandbox_prefixes()`` reads this env var at call-time,
   so the child's sandbox gate sees the node policy — no in-process monkeypatch
   needed across the process boundary.

2. ``_launch_iso_soak()`` temporarily patches ``subprocess.Popen`` to force the
   organism subprocess cwd to the ``IsomorphicEnv`` disjoint path (``<tmpdir>/app``)
   instead of ``repo_root``.  Code that uses ``os.getcwd()`` as a proxy for the
   repo root now fails in the child exactly as it does on the live GCP node.
   ``JARVIS_REPO_PATH`` in the env tells the child the true repo location.

Container mode achieves full path-literal parity via a genuine bind-mount at
``/opt/trinity/jarvis``; process mode is fast (M1-native, zero Docker).

Failover safety pin
-------------------
By default the driver pins ``JARVIS_FAILOVER_LIFECYCLE_ENABLED=false`` in the
child env to prevent a local fidelity run from triggering a real GCE awaken
attempt (the any-route window has no time-decay, so a partial outage window from
a previous run can accumulate).  Pass ``--enable-failover`` to opt back in.

Run-#12 fix (post-boot chaos injection)
---------------------------------------
OLD: inject -> boot soak -> detect  [TestWatcher ran full pytest tests/]
NEW: boot soak -> [READY] -> inject -> touch(chaos_file) -> detect [scoped pytest]

The pre-soak injection was why chaos was never detected: the TestWatcher was cold
(not yet subscribed to fs.changed.*) when the mutation landed. By injecting AFTER
boot and then touching the mutated file to fire fs.changed.modified, the
TestFailureSensor picks up exactly that file and runs the scoped pytest target
(e.g. tests/core/test_foo.py) instead of the full tests/ suite.

Run-#13 fix (intervention-lock lineage scoping)
-----------------------------------------------
Already complete in a1_graduation_auditor.py -- CONFIRMED PRE-EXISTING.
An unrelated APPROVAL_REQUIRED op (e.g. OpportunityMiner hitting the Immutable
Orange guard) does NOT trip the Absolute Intervention-Lock; only a human-gate
on an op in the chaos-repair causal subtree does.  Verified by the new test
suite in tests/integration/test_isomorphic_a1_e2e.py.

Usage::

    python3 scripts/isomorphic_a1_local.py --stub-soak              # wiring proof
    python3 scripts/isomorphic_a1_local.py --stub-soak --mode container
    python3 scripts/isomorphic_a1_local.py                           # live soak
    python3 scripts/isomorphic_a1_local.py --enable-failover         # opt in to real GCE
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Paths -- no hardcoding; always derived from this file's location
# ---------------------------------------------------------------------------
_SCRIPTS_DIR: str = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT: str = os.path.dirname(_SCRIPTS_DIR)

_HARNESS_SCRIPT: str = os.path.join(_SCRIPTS_DIR, "a1_live_fire_chaos_harness.py")
_AUDITOR_SCRIPT: str = os.path.join(_SCRIPTS_DIR, "a1_graduation_auditor.py")
_ADVERSARY_SCRIPT: str = os.path.join(_SCRIPTS_DIR, "synthetic_adversary.py")

# Marker emitted by TestWatcher when it has successfully subscribed to
# fs.changed.* on the TrinityEventBus.  Used by _await_soak_boot().
_TESTWATCHER_READY_MARKER: str = "[TestWatcher] READY subscribed=fs.changed.*"


# ---------------------------------------------------------------------------
# Lazy module loaders (same pattern as a1_live_fire_chaos_harness.py)
# ---------------------------------------------------------------------------

def _load_module(name: str, path: str) -> Any:
    """Load a script-module by path; return the cached version if already loaded.

    Uses a cache-first strategy so every caller (driver + tests) gets the SAME
    object -- essential for ``patch.object`` to work across call sites.
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise ImportError("Cannot load %s from %s" % (name, path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # store BEFORE exec to handle circular refs
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _harness() -> Any:
    """Return the a1_live_fire_chaos_harness module (lazy-cached)."""
    return sys.modules.get("a1_live_fire_chaos_harness") or _load_module(
        "a1_live_fire_chaos_harness", _HARNESS_SCRIPT
    )


def _auditor() -> Any:
    """Return the a1_graduation_auditor module (lazy-cached)."""
    return sys.modules.get("a1_graduation_auditor") or _load_module(
        "a1_graduation_auditor", _AUDITOR_SCRIPT
    )


def _adversary_mod() -> Any:
    """Return the synthetic_adversary module (lazy-cached)."""
    return sys.modules.get("synthetic_adversary") or _load_module(
        "synthetic_adversary", _ADVERSARY_SCRIPT
    )


def _ensure_backend_on_path() -> None:
    """Add repo root and backend dir to sys.path so absolute backend imports work
    regardless of the process cwd (IsomorphicEnv changes cwd to <tmpdir>/app).

    Order matters: repo root must be inserted at position 0 FIRST so the top-level
    tests/ package takes precedence over backend/tests/ (which has no adversarial/
    sub-package).  backend/ is appended to the END so it never shadows tests/.
    """
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    _backend = os.path.join(_REPO_ROOT, "backend")
    if _backend not in sys.path:
        sys.path.append(_backend)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print("[IsoA1] %s" % (msg,), flush=True)


def _truthy(val: Optional[str]) -> bool:
    return str(val or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Signal handler -- chaos-revert-always on SIGINT / SIGTERM
# ---------------------------------------------------------------------------

_ACTIVE_CHAOS: List[Any] = []

# Active soak runners (the organism subprocess + its process group). Reaped on
# finally + atexit + signal -- whichever fires first -- via a full process-group
# SIGTERM->SIGKILL cascade, so the multiprocessing worker pool NEVER orphans
# (mirrors the _ACTIVE_FAILOVER_RESOURCES reaper pattern).
_ACTIVE_SOAK_RUNNERS: List[Any] = []


def _reap_soak_runners() -> None:
    """Stop every active soak runner via its process-group teardown. Idempotent +
    NEVER raises -- safe to call from a ``finally``, an ``atexit`` hook, AND a
    signal handler (whichever fires first; the rest are no-ops once drained)."""
    if not _ACTIVE_SOAK_RUNNERS:
        return
    runners = list(_ACTIVE_SOAK_RUNNERS)
    _ACTIVE_SOAK_RUNNERS.clear()
    for runner in runners:
        try:
            runner.stop()
        except Exception:  # noqa: BLE001 -- teardown must never block exit
            pass


# ---------------------------------------------------------------------------
# Hybrid Execution Mesh (Task HM-A) -- failover-resource registry + IRONCLAD
# teardown.  The LOCAL driver owns the GCP failover node's networking (an
# ephemeral, zero-trust /32 INGRESS firewall for THIS host's public IP) AND its
# teardown, so a real GPU node can NEVER be orphaned: every awakened node + its
# firewall is reaped on finally + atexit + signal -- whichever fires first.
#
# Each entry mirrors the _ACTIVE_CHAOS pattern:
#   {"node": <name>, "zone": <zone>, "project": <proj>, "fw_rule": <fw|None>}
# ---------------------------------------------------------------------------

_ACTIVE_FAILOVER_RESOURCES: List[Dict[str, Optional[str]]] = []

# Defaults match the failover lifecycle's node/firewall naming; overridable via
# env so the driver and the organism agree on the same resource names.
_FAILOVER_FW_RULE_DEFAULT: str = "jarvis-ephemeral-failover-allow"
_FAILOVER_NODE_DEFAULT: str = "jarvis-prime-failover"
_FAILOVER_INFERENCE_PORT: int = 11434

# Hybrid Execution Mesh (Task HM-B) -- L7 semantic-readiness poller defaults. The
# audit must SUSPEND until the awakened 32B node returns HTTP 200 on /api/tags
# (proving the ~20GB model is loaded into L4 VRAM) -- NO hardcoded sleeps:
# exponential backoff (capped) bounded by a budget. All env-tunable so the soak
# operator and the tests can shrink them.  Read at CALL time (not module-load) so
# monkeypatch.setenv in tests takes effect.
_READY_PROBE_BASE_S: float = 3.0          # env JARVIS_HYBRID_MESH_READY_BASE_S
_READY_PROBE_CAP_S: float = 30.0          # env JARVIS_HYBRID_MESH_READY_CAP_S
_READY_PROBE_TIMEOUT_S: float = 5.0       # env JARVIS_HYBRID_MESH_READY_PROBE_TIMEOUT_S
_READY_BUDGET_DEFAULT_S: float = 900.0    # env JARVIS_HYBRID_MESH_READY_BUDGET_S


def _reap_failover_resources() -> None:
    """Violently reap every awakened failover node + its ephemeral /32 firewall.

    Idempotent (clears the registry so a second call is a no-op) + fail-soft
    (NEVER raises) -- safe to call from a ``finally``, an ``atexit`` hook, AND a
    signal handler.  The node's GCP REST delete is the process's last breath.

    When invoked from inside a live event loop (e.g. a signal handler that
    interrupted ``asyncio.run(driver.run())``), ``asyncio.run`` would raise, so
    the reap is dispatched on a dedicated thread with its own loop and joined --
    the node delete still completes even on Ctrl+C mid-soak.
    """
    if not _ACTIVE_FAILOVER_RESOURCES:
        return
    resources = list(_ACTIVE_FAILOVER_RESOURCES)
    _ACTIVE_FAILOVER_RESOURCES.clear()

    async def _del_all() -> None:
        from backend.core.ouroboros.governance.gcp_compute_rest import (
            get_compute_rest,
        )

        from backend.core.ouroboros.governance.zone_fallback import (
            zone_fallback_chain,
        )

        client = get_compute_rest()
        # The multi-zonal awaken can land the node in ANY fallback zone (a Spot
        # stockout in us-central1-a -> the node is created in -b or -c). The reap
        # must therefore attempt the delete in EVERY candidate zone, not just
        # GCP_ZONE -- otherwise a node in -c is orphaned (the exact cost-leak the
        # 4th live ignition hit). delete_instance is 404-idempotent, so deleting
        # in the zones that DON'T hold the node is a clean no-op.
        _zones = zone_fallback_chain(os.environ.get("GCP_ZONE"))
        tasks = []
        for r in resources:
            if r.get("node"):
                for _z in _zones:
                    tasks.append(client.delete_instance(r["node"], zone=_z))
            if r.get("fw_rule"):
                tasks.append(client.delete_firewall_rule(r["fw_rule"]))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _run_blocking() -> None:
        asyncio.run(_del_all())

    try:
        try:
            loop = asyncio.get_running_loop()
            # running-loop -> daemon-thread branch
            import threading
            t = threading.Thread(
                target=_run_blocking, name="failover-reap", daemon=True)
            t.start()
            t.join(timeout=float(os.environ.get("JARVIS_FAILOVER_REST_TIMEOUT_S", "30")) + 5.0)
        except RuntimeError:
            # no-loop -> asyncio.run branch
            _run_blocking()
        _log("[HybridMesh] reaped %d failover resource(s) -- "
             "node(s)+firewall(s) deleted" % len(resources))
    except Exception as exc:  # noqa: BLE001 -- teardown must NEVER raise
        _log("[HybridMesh] reap error (continuing): %r" % (exc,))


def _register_failover_resource(
    *, node: Optional[str], fw_rule: Optional[str],
) -> None:
    """Register a failover node + its ephemeral firewall for teardown UP FRONT
    (before the node even exists) so a crash mid-awaken still reaps whatever got
    created.  Zone/project are recorded for the audit trail; the GCP REST client
    resolves them lazily from metadata/env at delete time."""
    _ACTIVE_FAILOVER_RESOURCES.append({
        "node": node,
        "zone": os.environ.get("GCP_ZONE"),
        "project": (os.environ.get("GCP_PROJECT_ID")
                    or os.environ.get("GCP_PROJECT")),
        "fw_rule": fw_rule,
    })


async def _open_failover_firewall(fw_name: str) -> Optional[str]:
    """Open the driver-owned ephemeral /32 INGRESS firewall for THIS host's
    public IP -> the failover node's inference port.  Reuses the existing GCP
    primitives (``resolve_local_public_ip`` + ``create_firewall_rule``) -- NO
    ``0.0.0.0/0``, ever.  Returns ``fw_name`` on success, ``None`` on skip/fail;
    the teardown stays armed regardless (the node was registered before this call)."""
    try:
        from backend.core.ouroboros.governance.gcp_compute_rest import (
            get_compute_rest,
            resolve_local_public_ip,
        )

        ip = await resolve_local_public_ip()
        if not ip:
            _log("[HybridMesh] WARN could not resolve local public IP -- "
                 "firewall NOT opened; node may be unreachable")
            return None
        ok, detail = await get_compute_rest().create_firewall_rule(
            name=fw_name, source_ip=ip, port=_FAILOVER_INFERENCE_PORT)
        _log("[HybridMesh] ephemeral /32 firewall %s for %s/32:%d -> %s (%s)" % (
            fw_name, ip, _FAILOVER_INFERENCE_PORT,
            "OPEN" if ok else "FAILED", detail))
        return fw_name if ok else None
    except Exception as exc:  # noqa: BLE001
        _log("[HybridMesh] firewall open error "
             "(continuing -- teardown still armed): %r" % (exc,))
        return None


def _arm_synthetic_roadmap(env: Dict[str, str], run_dir: str) -> str:
    """Deterministic Synthetic Roadmap Generator -- emit the A1 `emit source=roadmap`
    provenance hop with a GENUINE signed payload (no hardcoded fake trace, no
    REQUIRE_SIGNATURE bypass).

    Builds a strategic GOAL and signs it with the PRODUCTION primitives
    (``strategy_signer.sign_roadmap_doc`` -> ``roadmap_reader._build_signing_payload``
    + ``compute_signature``: canonical compact sorted-key JSON, HMAC-SHA256) -- the
    exact bytes the reader re-derives to verify. Writes it to an absolute path the
    organism subprocess can read (same accessibility contract as JARVIS_TRINITY_ROOT)
    and arms the reader env. compose_env already sets JARVIS_ROADMAP_ORCHESTRATOR_ENABLED
    + JARVIS_A1_TRACE_ENABLED; the missing master gate was JARVIS_ROADMAP_READER_ENABLED.
    The organism's _roadmap_ignition_daemon then ingests this goal -> the A1Trace emit
    hop fires. Returns the roadmap file path.

    The HMAC secret is a real per-run token (``generate_secret``, mirroring the
    per-deployment GCE metadata secret) -- overridable via
    JARVIS_A1_SYNTHETIC_ROADMAP_SECRET for byte-reproducible runs; never a committed
    literal. The driver signs and injects the SAME secret into the child env so the
    reader's verification (REQUIRE_SIGNATURE on) succeeds."""
    import yaml as _yaml  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415
    from backend.core.ouroboros.governance.strategy_signer import (  # noqa: PLC0415
        generate_secret,
        sign_roadmap_doc,
    )

    secret = (os.environ.get("JARVIS_A1_SYNTHETIC_ROADMAP_SECRET", "") or "").strip()
    if not secret:
        secret = generate_secret()

    doc = {
        "version": 1,
        "operator_id": "a1-soak@jarvis.local",
        "signed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "goals": [
            {
                "id": "GOAL-001",
                "title": "A1 self-audit: confirm the autonomous dispatch chain is live",
                "description": (
                    "Synthetic strategic goal for the A1 soak -- exercises the full "
                    "emit->ingest->dequeue->submit->accept provenance chain end to end."
                ),
                "priority": "high",
                "target_files": ["backend/core/ouroboros/governance/a1_trace.py"],
                "success_criteria": (
                    "A complete 5-hop A1Trace chain is observed for this roadmap goal."
                ),
            }
        ],
    }
    signed = sign_roadmap_doc(doc, secret)

    rm_dir = os.path.join(run_dir, ".jarvis")
    os.makedirs(rm_dir, exist_ok=True)
    path = os.path.join(rm_dir, "roadmap.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        _yaml.safe_dump(signed, fh, sort_keys=False)

    env["JARVIS_ROADMAP_READER_ENABLED"] = "true"
    env["JARVIS_ROADMAP_READER_REQUIRE_SIGNATURE"] = "true"
    env["JARVIS_ROADMAP_READER_HMAC_SECRET"] = secret
    env["JARVIS_ROADMAP_READER_PATH"] = path
    return path


async def _arm_failover_mesh(env: Dict[str, str]) -> None:
    """Arm the Hybrid Execution Mesh on the soak env, register the failover node
    for teardown UP FRONT, then open the driver-owned /32 firewall.

    - external-natIP routing so the Mac can reach the node's inference server;
    - the node binds ``0.0.0.0`` (reachable from off-box);
    - the DRIVER owns the firewall (NOT the organism -- we do NOT arm
      ``JARVIS_FAILOVER_EPHEMERAL_FW_ENABLED``), so it is reaped on any exit.

    Registration happens BEFORE the firewall open so a crash during firewall
    setup still reaps whatever got created.
    """
    env["JARVIS_FAILOVER_HYBRID_MESH"] = "true"             # external-natIP route
    env["JARVIS_FAILOVER_INFERENCE_BIND_ENABLED"] = "true"  # node binds 0.0.0.0
    # Teach the 32B the Iron Gate's exploration-first contract (>=2 read_file/
    # search_code before any patch) so it does not emit zero-shot diffs that the
    # gate rejects -> GENERATE_RETRY. Injected into the Prime-path system prompt.
    env.setdefault("JARVIS_A1_EXPLORATION_PROMPT_ENABLED", "true")
    # Heavy-tier local-inference timeouts are now DERIVED by the Adaptive EWMA
    # Profiler, not a manual base timer: the Context-Aware Dynamic Seed scales the
    # 30s base by JARVIS_JPRIME_HEAVY_COLDSTART_MULT x (num_ctx/baseline) (~244s for
    # a 16k window on the 32B), asymmetric penalty injection escalates the EWMA on a
    # timeout so the profiler is never starved, and the Absolute Global Circuit
    # Breaker (default 20min) kills a genuinely wedged model. We therefore no longer
    # hardcode SEED_MS/TIMEOUT_MS here -- only the absolute breaker safety ceiling.
    env.setdefault("JARVIS_LOCAL_INFERENCE_ABSOLUTE_CEILING_MS", "1200000")  # 20min hard kill
    # Cross-Region Capacity Matrix: do NOT pin a single region -- a whole-region L4
    # stockout must fall to a fallback region. Leave JARVIS_GCP_ZONE_FALLBACK unset
    # so zone_fallback._DEFAULT_ZONES (the region-ordered L4 matrix) applies; an
    # operator may still override with an explicit cross-region list.
    # L4 Spot is scarce -> let a Spot stockout fall through to on-demand in
    # the same quota'd zone (bounded $; the run is short + violently reaped).
    env.setdefault("JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT", "true")
    fw_name = os.environ.get(
        "JARVIS_FAILOVER_FW_RULE_NAME", _FAILOVER_FW_RULE_DEFAULT)
    node_name = os.environ.get(
        "JARVIS_FAILOVER_NODE_NAME", _FAILOVER_NODE_DEFAULT)
    # Register the node for teardown UP FRONT (orphan safety) with fw_rule=None;
    # fw_rule is updated to the opened name only if the firewall was actually
    # created -- so a reap never deletes a rule that was never opened.
    _register_failover_resource(node=node_name, fw_rule=None)
    opened_fw = await _open_failover_firewall(fw_name)
    if _ACTIVE_FAILOVER_RESOURCES:
        _ACTIVE_FAILOVER_RESOURCES[-1]["fw_rule"] = opened_fw


# ---------------------------------------------------------------------------
# Hybrid Execution Mesh (Task HM-B) -- L7 semantic-readiness poller. The A1
# audit must SUSPEND until the awakened 32B node's inference server returns HTTP
# 200 on /api/tags (the ~20GB model is loaded into L4 VRAM). Without this gate
# the audit fires FAILED before the node can serve -- the exact failure of the
# last live run.  No hardcoded sleeps: exponential backoff (capped) + a budget.
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    """Read a float env var with a fail-soft fallback (NEVER raises)."""
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _a1_audit_ceiling_s(debug_log: "Optional[str]" = None) -> float:
    """Dynamic Global Audit Ceiling (no hardcoded window). The auditor early-exits
    the moment the verdict is proven; this is the SAFETY ceiling.

    DYNAMIC path: derive it from OBSERVED reality -- the largest per-call inference
    budget the profiler actually used (the escalated EWMA seed, logged as
    ``budget=Xms``) multiplied by the expected max agentic rounds
    (``JARVIS_A1_MAX_AGENTIC_ROUNDS``, default 5): a heavy op runs a multi-turn Venom
    loop (explore -> read -> patch -> validate), so the run needs
    ``per_call x rounds`` of wall-clock. This adapts to the 32B's real (escalating)
    latency instead of a flat guess.

    FALLBACK (no budget observed yet): the tier-aware base scaled by
    ``JARVIS_JPRIME_HEAVY_COLDSTART_MULT``. Fail-soft -- NEVER raises."""
    base = _env_float("JARVIS_A1_AUDIT_BASE_S", 300.0)
    max_rounds = max(1.0, _env_float("JARVIS_A1_MAX_AGENTIC_ROUNDS", 5.0))
    # --- DYNAMIC: largest observed per-call budget x max agentic rounds ---
    per_call_ms = 0.0
    if debug_log:
        try:
            import re  # noqa: PLC0415
            with open(debug_log, "r", encoding="utf-8", errors="ignore") as _fh:
                for _m in re.finditer(r"budget=(\d+)ms", _fh.read()):
                    per_call_ms = max(per_call_ms, float(_m.group(1)))
        except Exception:  # noqa: BLE001
            per_call_ms = 0.0
    if per_call_ms > 0.0:
        return max(base, (per_call_ms / 1000.0) * max_rounds)
    # --- FALLBACK: tier-aware heavy-scaled base ---
    try:
        from backend.core.ouroboros.governance.failover_tier import (  # noqa: PLC0415
            resolve_tier,
        )
        from backend.core.ouroboros.governance.failover_lifecycle import (  # noqa: PLC0415
            _heavy_coldstart_mult,
            _tier_is_heavy,
        )
        tier = resolve_tier(
            urgency=os.environ.get("JARVIS_FAILOVER_AWAKEN_URGENCY", ""),
            complexity=os.environ.get("JARVIS_FAILOVER_AWAKEN_COMPLEXITY", ""),
        )
        if _tier_is_heavy(tier):
            return base * _heavy_coldstart_mult()
    except Exception:  # noqa: BLE001
        pass
    return base


# Soak-child wall budget: read at module load for logging; the helper re-reads
# at call time so monkeypatch.setenv in tests takes effect.
_ready_budget = _env_float("JARVIS_HYBRID_MESH_READY_BUDGET_S", 900.0)
_failover_wall = _ready_budget + 600.0  # ~90s boot + audit + slack; READY_BUDGET_S + 600 < harness --max-wall-seconds (2400)


def _expected_agentic_cycle_s() -> float:
    """Arm-time derivation of ONE full multi-round agentic cycle on the heavy
    tier (Slice-47-safe: computed BEFORE launch, immutable once armed -- the
    running wall stays blind to application state).

    cycle = rounds x (cold-round seed x heavy-mult x ctx-factor) -- the SAME
    physics the LatencyProfiler's ``_cold_seed_ms`` uses, so the wall and the
    per-round budgets are derived from one model. ``JARVIS_HYBRID_MESH_
    EXPECTED_NUM_CTX`` (default 16384) is the arm-time expectation of the
    negotiated window (the runtime Negotiator measures the real one). Floored
    at the legacy 600s margin; falls back to 600 on any error."""
    try:
        # ONE physics model: delegate to the shared formula (the same one the
        # BudgetPlan hint / Time-Dilated Deadline / sovereign GENERATE floor
        # consume) -- floored at the legacy 600s margin.
        from backend.core.ouroboros.governance.local_inference_director import (  # noqa: PLC0415
            expected_agentic_cycle_s,
        )
        return max(600.0, float(expected_agentic_cycle_s()))
    except Exception:  # noqa: BLE001 -- arm-time sizing must never block ignition
        return 600.0


def _failover_soak_wall(enable_failover: bool) -> int:
    """Return the soak-child wall-clock budget in seconds.

    enable_failover=False -> 300 (byte-identical default).
    enable_failover=True  -> READY_BUDGET_S + _expected_agentic_cycle_s() --
    the margin is DERIVED from round physics at arm time (was a static +600
    that the 32B's multi-round loops outlived every run). Keep the result
    below the harness --max-wall-seconds.
    """
    if not enable_failover:
        return 300
    budget = _env_float("JARVIS_HYBRID_MESH_READY_BUDGET_S", 900.0)
    return int(budget + _expected_agentic_cycle_s())


def _probe_api_tags(url: str) -> int:
    """Blocking HTTP GET of the inference server's ``/api/tags`` -> status code.

    Factored out as a TINY testable seam (tests monkeypatch THIS, not the real
    network).  Mirrors the failover ``_default_node_ready_fn`` urllib style.
    Returns the HTTP status (200 == 32B served; a real non-2xx like 503 while the
    model still loads is returned verbatim so the poller treats it as 'not ready
    yet').  Transport errors (connection refused, timeout, DNS) PROPAGATE -- the
    caller catches them as 'not ready' and keeps backing off.
    """
    import urllib.error
    import urllib.request

    timeout = _env_float(
        "JARVIS_HYBRID_MESH_READY_PROBE_TIMEOUT_S", _READY_PROBE_TIMEOUT_S)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return int(getattr(resp, "status", None) or resp.getcode() or 0)
    except urllib.error.HTTPError as he:
        # A genuine HTTP status (e.g. 503 while VRAM still loading) -> not a
        # transport error; return the code so the caller backs off, not aborts.
        return int(getattr(he, "code", 0) or 0)


_CAPACITY_EXHAUSTED_MARKER = "HARDWARE_CAPACITY_EXHAUSTED"


def _hardware_capacity_exhausted(debug_log: Optional[str]) -> bool:
    """True iff the organism logged a global L4 capacity wall (the cross-region
    matrix stocked out with no node). Lets the L7 gate + audit fast-fail instead
    of waiting out their full budgets. Fail-soft -> False when unreadable."""
    if not debug_log:
        return False
    try:
        if not os.path.isfile(debug_log):
            return False
        with open(debug_log, "r", encoding="utf-8", errors="ignore") as fh:
            return _CAPACITY_EXHAUSTED_MARKER in fh.read()
    except Exception:  # noqa: BLE001
        return False


async def _await_jprime_serving(
    node_name: str, *, budget_s: float, port: int = 11434,
    debug_log: Optional[str] = None,
) -> bool:
    """Suspend until the awakened GCP node's inference server returns HTTP 200 on
    ``/api/tags`` (the 32B is loaded into VRAM). Exponential backoff (cap'd),
    bounded by ``budget_s``. Returns True on first 200, False if the budget
    elapses. Fail-soft: transport errors are just 'not ready yet' -> keep backing
    off.  NEVER raises -- worst case returns False and the audit proceeds (and the
    ironclad teardown reaps the node).
    """
    base = _env_float("JARVIS_HYBRID_MESH_READY_BASE_S", _READY_PROBE_BASE_S)
    cap = _env_float("JARVIS_HYBRID_MESH_READY_CAP_S", _READY_PROBE_CAP_S)
    probe_to = _env_float(
        "JARVIS_HYBRID_MESH_READY_PROBE_TIMEOUT_S", _READY_PROBE_TIMEOUT_S)

    delay = base
    start = time.monotonic()
    deadline = start + budget_s
    no_ip_logged = False

    try:
        from backend.core.ouroboros.governance.gcp_compute_rest import (
            get_compute_rest,
        )
    except Exception as exc:  # noqa: BLE001 -- import must never crash the run
        _log("[HybridMesh] WARN readiness poller cannot import gcp_compute_rest "
             "(%r) -- skipping gate" % (exc,))
        return False

    while time.monotonic() < deadline:
        # Fast-Fail short-circuit: if the organism hit a global L4 capacity wall
        # (cross-region matrix exhausted), stop waiting out the L7 budget NOW.
        if _hardware_capacity_exhausted(debug_log):
            _log("[HybridMesh] HardwareCapacityExhausted -> L7 gate fast-fail "
                 "(cross-region L4 stockout; not waiting out %.0fs)" % budget_s)
            return False
        external: Optional[str] = None
        try:
            _internal, external = await get_compute_rest().get_node_endpoints(
                node_name)
        except Exception as exc:  # noqa: BLE001 -- treat as 'not ready yet'
            _log("[HybridMesh] endpoint resolve fail-soft (%r) -- backing off"
                 % (exc,))
            external = None

        if not external:
            if not no_ip_logged:
                _log("[HybridMesh] node %s not RUNNING yet (no external IP) -- "
                     "backing off" % node_name)
                no_ip_logged = True
        else:
            url = "http://%s:%d/api/tags" % (external, port)
            status = 0
            try:
                loop = asyncio.get_running_loop()
                status = await asyncio.wait_for(
                    loop.run_in_executor(None, _probe_api_tags, url),
                    timeout=probe_to + 1.0,
                )
            except Exception:  # noqa: BLE001 -- timeout/transport -> not ready
                status = 0
            if status == 200:
                elapsed = time.monotonic() - start
                _log("[HybridMesh] J-Prime SERVING: 32B ready at %s after %.0fs"
                     % (url, elapsed))
                return True

        # Not ready -> exponential backoff, clamped to the remaining budget.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            await asyncio.sleep(min(delay, remaining))
        except asyncio.CancelledError:
            return False
        delay = min(delay * 2.0, cap)

    _log("[HybridMesh] WARN J-Prime NOT ready after %.0fs -- proceeding to audit "
         "(will likely FAIL; teardown will reap)" % budget_s)
    return False


def _install_revert_signal_handlers() -> None:
    """Install SIGINT/SIGTERM handlers that revert any active chaos before exit.
    The repo must NEVER be left broken on any exit path."""
    def _handler(signum: int, _frame: Any) -> None:
        _log("signal %d received -- reaping failover + reverting chaos before "
             "exit" % (signum,))
        # The node REST-delete is the last breath even on Ctrl+C -- reap FIRST.
        try:
            _reap_failover_resources()
        except Exception:  # noqa: BLE001 -- teardown must never block exit
            pass
        # Process-group teardown of the soak organism + its worker pool -- zero
        # orphaned multiprocessing workers on Ctrl+C / SIGTERM / crash.
        try:
            _reap_soak_runners()
        except Exception:  # noqa: BLE001
            pass
        for chaos in list(_ACTIVE_CHAOS):
            try:
                chaos.revert()
            except Exception:  # noqa: BLE001
                pass
        try:
            signal.signal(signum, signal.SIG_DFL)
        except Exception:  # noqa: BLE001
            pass
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass  # not in main thread (e.g. pytest workers) -- skip silently


# ---------------------------------------------------------------------------
# Run-#12 helpers: touch chaos files + derive scoped test targets
# ---------------------------------------------------------------------------

def _touch_chaos_files(chaos_files: List[str], repo_root: str) -> List[str]:
    """Touch each chaos target file to update its mtime (run-#12 fix).

    In a live O+V soak the FileSystemEventBridge watches the filesystem; a
    mtime change fires ``fs.changed.modified`` on the TrinityEventBus, which
    wakes the TestFailureSensor's dynamic subscription and triggers a SCOPED
    pytest run (just the affected test file) instead of the full ``tests/``
    suite.

    In a stub/dry-run soak the touch is still performed: it proves the
    sequencing logic is correct and leaves an auditable mtime trail.

    Returns the list of absolute paths that were successfully touched.
    """
    touched: List[str] = []
    for cf in chaos_files:
        abs_cf = cf if os.path.isabs(cf) else os.path.join(repo_root, cf)
        if not os.path.exists(abs_cf):
            _log("touch skip (not found): %s" % abs_cf)
            continue
        try:
            Path(abs_cf).touch()
            touched.append(abs_cf)
            _log("run-#12 fix: touched %s (fires fs.changed.modified)" % abs_cf)
        except OSError as exc:
            _log("touch warning %s: %r" % (abs_cf, exc))
    return touched


def _derive_scoped_test_targets(chaos_files: List[str], repo_root: str) -> List[str]:
    """Heuristic derivation of scoped pytest targets from chaos source files.

    The authoritative implementation is ``TestFailureSensor._resolve_scoped_targets``
    (async, requires a live sensor context).  This local approximation proves the
    "scoped, not full-suite" invariant without booting the organism.

    Returns a sorted, de-duped list of matching test file paths.  An EMPTY list
    means no scoped targets were found locally -- this is NOT a fallback to
    ``tests/``.  The driver NEVER expands to the full test suite.
    """
    tests_root = Path(repo_root) / "tests"
    targets: List[str] = []
    for cf in chaos_files:
        stem = Path(cf).stem
        # Pattern 1: test_<stem>.py anywhere under tests/
        for tf in tests_root.rglob("test_%s.py" % stem):
            targets.append(str(tf))
        # Pattern 2: <stem>_test.py anywhere under tests/
        for tf in tests_root.rglob("%s_test.py" % stem):
            targets.append(str(tf))
    return sorted(set(targets))


def _await_soak_boot(
    proc: Any,
    debug_log: str,
    timeout_s: float = 60.0,
) -> bool:
    """Poll the soak's debug.log for the TestWatcher READY marker.

    Returns True when the marker is found within *timeout_s*, False on timeout
    or premature process exit.  Stub-soak callers pass ``proc=None`` and this
    returns immediately (no real boot to await).
    """
    if proc is None:
        return True  # stub soak -- no real O+V boot
    deadline = time.monotonic() + timeout_s
    seen_lines: int = 0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _log("soak exited prematurely (rc=%d)" % proc.poll())
            return False
        try:
            with open(debug_log, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    seen_lines += 1
                    if _TESTWATCHER_READY_MARKER in line:
                        _log("soak boot READY (%d lines scanned)" % seen_lines)
                        return True
        except OSError:
            pass
        time.sleep(0.5)
    _log("soak boot TIMEOUT after %.0fs (%d lines scanned)" % (timeout_s, seen_lines))
    return False  # timeout -- proceed anyway; sensor may still start


# ---------------------------------------------------------------------------
# Adversary fault scheduling
# ---------------------------------------------------------------------------

def _schedule_adversary_fault(
    adversary: Any, adv_mod: Any, fault: str
) -> None:
    """Schedule a deterministic DW provider fault via the SyntheticAdversary.

    *fault* is one of: ``http5xx`` | ``transport`` | ``timeout`` | ``parse_error``.
    Silently skips if FailureSource cannot be imported (dev boxes lacking topology deps).
    """
    if not fault or fault == "none":
        return
    fault_map: Dict[str, str] = {
        "http5xx": "live_http_5xx",
        "transport": "live_transport",
        "timeout": "live_stream_stall",
        "parse_error": "live_parse_error",
        "http429": "live_http_429",
    }
    fault_value = fault_map.get(fault, fault)
    try:
        # FailureSource lives in topology_sentinel; the adversary module
        # already re-exports it (or sets it to None on import failure).
        fs_cls = getattr(adv_mod, "FailureSource", None)
        if fs_cls is None:
            _log("adversary fault skipped (FailureSource unavailable): %s" % fault)
            return
        # Look up by value (the string stored in the enum).
        matching = [e for e in fs_cls if e.value == fault_value]
        if not matching:
            _log("adversary fault %r not in FailureSource enum -- skipping" % fault_value)
            return
        adversary.schedule(
            route="doubleword",
            endpoint="/chat/completions",
            fault=matching[0],
            count=None,
        )
        _log("adversary fault scheduled: %s" % matching[0])
    except Exception as exc:  # noqa: BLE001
        _log("adversary fault schedule warning: %r" % (exc,))


# ---------------------------------------------------------------------------
# Subprocess iso-cwd threading
# ---------------------------------------------------------------------------

def _launch_iso_soak(
    soak_runner: Any,
    env: Dict[str, str],
    run_dir: str,
    iso_cwd: str,
) -> Any:
    """Launch the O+V soak subprocess with the IsomorphicEnv disjoint cwd.

    ``SoakRunner.launch`` uses ``self.repo_root`` as the subprocess cwd.
    We need the child to run with the IsomorphicEnv disjoint path so that
    code using ``os.getcwd()`` as a repo-root proxy fails — exactly as it
    does on the GCP node.  Session discovery still uses the real repo_root
    (``SoakRunner._sessions_root()`` is unaffected because ``repo_root`` is
    not mutated).

    Concurrency note: ``subprocess.Popen`` is patched for the duration of
    this call only (thread-local concern; callers must not call this from
    multiple threads simultaneously).
    """
    import subprocess as _sp

    _orig_popen = _sp.Popen

    def _iso_popen(argv: Any, **kwargs: Any) -> Any:
        # Force the disjoint cwd regardless of what SoakRunner passes.
        kwargs["cwd"] = iso_cwd
        return _orig_popen(argv, **kwargs)

    _sp.Popen = _iso_popen  # type: ignore[assignment]
    try:
        return soak_runner.launch(env, run_dir)
    finally:
        _sp.Popen = _orig_popen


# ---------------------------------------------------------------------------
# IsomorphicA1Driver -- the full-chain local E2E driver
# ---------------------------------------------------------------------------

class IsomorphicA1Driver:
    """Full-chain local A1 E2E driver under IsomorphicEnv + SyntheticAdversary.

    Key differences from ``HarnessRun.execute()``:

    1. **Runs inside IsomorphicEnv** (T1): forces live ``/opt/trinity/jarvis``
       path + cwd mismatch + restricted sandbox prefix policy.
    2. **SyntheticAdversary** (T3): env-URL swap replaces real DW/Prime URLs
       with a localhost proxy that serves deterministic failure responses.
    3. **Post-boot chaos injection** (run-#12 fix): the soak is BOOTED first;
       only after the TestWatcher logs its READY marker does the driver inject
       chaos and touch the mutated file to fire ``fs.changed.modified``.
    4. **capture_failure_telemetry** (T5): called on any non-proven verdict.
    """

    def __init__(
        self,
        *,
        repo_root: Optional[str] = None,
        mode: str = "process",
        seed: int = 0,
        stub_soak: bool = True,
        strict: bool = True,
        sse_base: str = "http://127.0.0.1:7778",
        run_root: Optional[str] = None,
        adversary_fault: Optional[str] = None,
        verbose: bool = False,
        enable_failover: bool = False,
        # Injection seam for tests: a zero-arg callable that returns an adversary
        # instance.  None -> use the real SyntheticAdversary from adversary_mod.
        _adversary_factory: Optional[Any] = None,
    ) -> None:
        self.repo_root: str = repo_root or _REPO_ROOT
        self.mode: str = mode
        self.seed: int = seed
        self.stub_soak: bool = stub_soak
        self.strict: bool = strict
        self.sse_base: str = sse_base
        self.run_root: str = run_root or os.path.join(os.getcwd(), "a1_iso_runs")
        self.adversary_fault: Optional[str] = adversary_fault
        self.verbose: bool = verbose
        self.enable_failover: bool = enable_failover
        self._adversary_factory: Optional[Any] = _adversary_factory

    async def run(self) -> int:
        """Execute the full chain.  Returns 0 iff A1_DISPATCH_PROVEN."""
        _ensure_backend_on_path()

        harness_mod = _harness()
        auditor_mod = _auditor()
        adv_mod = _adversary_mod()

        # T1: IsomorphicEnv + T5: capture_failure_telemetry (imported inside
        # run() so the lazy sys.path extension is in effect before import).
        from backend.core.ouroboros.battle_test.isomorphic_env import IsomorphicEnv
        from backend.core.ouroboros.battle_test.failure_telemetry import (
            capture_failure_telemetry,
        )

        run_id = time.strftime("iso-a1-%Y%m%d-%H%M%S")
        run_dir = os.path.join(self.run_root, run_id)
        os.makedirs(run_dir, exist_ok=True)

        _log("run_id=%s mode=%s stub_soak=%s seed=%d" % (
            run_id, self.mode, self.stub_soak, self.seed))

        # T3: SyntheticAdversary -- start BEFORE IsomorphicEnv so the server
        # binds on the host-network port that the soak env will point at.
        if self._adversary_factory is not None:
            adversary = self._adversary_factory()
        else:
            adversary = adv_mod.SyntheticAdversary()
        # Belt-and-suspenders zero-shot propagation: synthetic_adversary reads
        # JARVIS_ADVERSARY_SIMULATE_ZERO_SHOT at module-load time into the
        # module-level _ZERO_SHOT_ENV_DEFAULT constant.  When the module was
        # already cached in sys.modules before the env var was set (e.g. in
        # tests or when the caller imports isomorphic_a1_local early), the
        # cached constant is stale.  Explicitly call set_simulate_zero_shot()
        # here so the runtime flag always reflects the current env, regardless
        # of import order.
        _zs_raw = os.environ.get("JARVIS_ADVERSARY_SIMULATE_ZERO_SHOT", "")
        if _zs_raw.lower() in ("1", "true", "yes"):
            adversary.set_simulate_zero_shot(True)
        if self.adversary_fault:
            _schedule_adversary_fault(adversary, adv_mod, self.adversary_fault)
        adversary_urls: Dict[str, str] = await adversary.start()
        _log("adversary started: dw=%s prime=%s" % (
            adversary_urls.get("doubleword", "?"), adversary_urls.get("prime", "?")))

        verdict: Dict[str, Any] = {"proven": False, "failure_locus": "not_run"}
        injected: bool = False
        chaos: Any = None

        try:
            # T1: enter the isomorphic process/container environment.
            with IsomorphicEnv(Path(self.repo_root), mode=self.mode) as env_ctx:
                _log("IsomorphicEnv: root=%s cwd=%s" % (env_ctx.root, os.getcwd()))

                # Capture the disjoint cwd now (IsomorphicEnv chdir'd us here).
                # Used later to run the organism subprocess under the same path.
                iso_cwd: str = os.getcwd()

                # Compose env: node vars (from IsomorphicEnv via os.environ, which
                # now includes JARVIS_SANDBOX_PREFIXES) + cognitive flags ON (from
                # CADENCE_POLICY) + adversary overrides.
                env: Dict[str, str] = harness_mod.compose_env()
                env.update(adversary.env_overrides())

                # Safety pin: prevent a local fidelity run from triggering a real
                # GCE awaken attempt.  The any-route outage window has no time-decay,
                # so accumulated failures from a previous partial run can trigger the
                # FSM.  Pass --enable-failover to opt back in.
                # (Minor-5: no time-decay on the any-route window by design.)
                if not self.enable_failover:
                    env["JARVIS_FAILOVER_LIFECYCLE_ENABLED"] = "false"
                else:
                    # Live-failover A1 run: compose_env()'s apply_manifest() pins
                    # failover_lifecycle=False, so merely SKIPPING the false-pin above
                    # is not enough -- the manifest already wrote "false" into env and
                    # would keep the failover loop from ever starting. Override it here
                    # (AFTER compose_env, exactly as the Iron Triad block does below) by
                    # propagating the ignition harness's / operator's JARVIS_FAILOVER_*
                    # and GCP_* env verbatim -- NO hardcoded tier/flag values; the
                    # ignite_a1_soak.py --live-failover arming is the single source.
                    for _k, _v in os.environ.items():
                        if (
                            _k.startswith("JARVIS_FAILOVER_")
                            or _k.startswith("GCP_")
                            or _k in ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT")
                        ):
                            env[_k] = _v
                    # The failover mesh is inert unless the lifecycle loop runs; ensure
                    # it is ON even if the harness only passed --enable-failover.
                    env["JARVIS_FAILOVER_LIFECYCLE_ENABLED"] = "true"

                    # Hybrid Execution Mesh (Task HM-A): arm external-IP routing +
                    # bind, and have the DRIVER (this local orchestrator) own the
                    # ephemeral /32 firewall for its OWN public IP -- zero-trust,
                    # no 0.0.0.0/0, reaped on any exit (finally+atexit+signal). The
                    # node is registered for teardown UP FRONT, before it exists.
                    # run() is already async -> await directly (NEVER asyncio.run
                    # inside a live loop; only _reap_failover_resources uses run()).
                    await _arm_failover_mesh(env)

                    # Autonomous FSM Suspend/Resume: pin a STABLE absolute checkpoint
                    # dir into the ORGANISM env (compose_env starts from a manifest,
                    # so JARVIS_CHECKPOINT_* is NOT inherited unless set here). The
                    # organism runs in an ephemeral isomorphic cwd, so a CWD-relative
                    # default would never survive to the next window. This shared dir
                    # (+ its persisted HMAC key) lets window-1 suspend -> window-2
                    # resume across ignitions.
                    env["JARVIS_CHECKPOINT_DIR"] = str(
                        Path(self.repo_root) / ".ouroboros" / "checkpoints"
                    )
                    env.setdefault("JARVIS_FSM_CHECKPOINT_ENABLED", "true")
                    env.setdefault("JARVIS_FSM_RESUME_ENABLED", "true")
                    # capture_inflight() reads the in-flight registry to know WHICH
                    # ops to checkpoint -- but register_op_safely no-ops when the
                    # registry master flag is off (default). Arm it so in-flight ops
                    # are tracked and thus captured on suspend.
                    env.setdefault("JARVIS_IN_FLIGHT_REGISTRY_ENABLED", "true")
                    # Activity-Gated Audit Deferral: the organism mirrors its
                    # stream heartbeat to this file on every streamed token; the
                    # in-process auditor's default_activity_probe reads it
                    # (cross-process, no new IPC) so the verdict defers while the
                    # 32B is genuinely mid-thought. Armed in BOTH the organism
                    # env (writer) and the driver process env (reader).
                    _hb_file = str(
                        Path(self.repo_root) / ".ouroboros" / "stream_heartbeat.epoch"
                    )
                    env["JARVIS_STREAM_HEARTBEAT_FILE"] = _hb_file
                    os.environ["JARVIS_STREAM_HEARTBEAT_FILE"] = _hb_file
                    # Scenario-premise pin: this soak's premise is a DEAD DW
                    # (adversary chaos-kills generation) but the adversary's
                    # PROBE path is healthy by design -- without this pin the
                    # FSM hands the sovereign node back every ~6min of uptime
                    # and deletes it under committed 32B ops
                    # (bt-iso-1782957492). Driver teardown + Dead-Man's Switch
                    # remain the cost backstops.
                    env.setdefault("JARVIS_FAILOVER_HANDBACK_ENABLED", "false")
                    # Arm-time adaptive audit patience: the deferral absolute
                    # ceiling is DERIVED from the same round physics as the
                    # soak wall (one model, no magic numbers) -- the verdict
                    # can outwait one full multi-round agentic cycle.
                    # Lane count: Dynamic Fleet Registry Service Discovery
                    # (governed_loop_service._fleet_lane_sync) -- the pool
                    # tracks serving sovereign endpoints live (one GPU = one
                    # lane). The former hard-assign is deprecated; the
                    # Immutability Lock also defeats the manifest's static 6.
                    _cycle_s = str(int(_expected_agentic_cycle_s()))
                    env.setdefault("JARVIS_A1_AUDIT_DEFER_ABSOLUTE_S", _cycle_s)
                    os.environ.setdefault("JARVIS_A1_AUDIT_DEFER_ABSOLUTE_S", _cycle_s)

                # ---- Iron Triad: arm the three gates + enforcer for the A1 soak ----
                # (all default OFF in prod; this driver IS the A1 ignition harness).
                env["JARVIS_RUNTIME_SANDBOX_ENABLED"] = "true"   # L4 container backend
                env["JARVIS_A1_SANDBOX_LOCK_ENABLED"] = "true"   # Gate 1
                env["JARVIS_A1_BLAST_RADIUS_ENABLED"] = "true"   # Gate 2
                env["JARVIS_A1_PR_LINTER_ENABLED"] = "true"      # Gate 3
                env["JARVIS_A1_TOKEN_ENFORCER_ENABLED"] = "true" # enforcer (PR needs token chain)
                # File-isolation OFF -> autonomous writes land in repo_root ->
                # durable commit (written=True) -> fixes the fsm_classify_to_applied
                # blocker. Mirrors the failover_lifecycle pin (stale a1-disable-file-
                # isolation branch folded here as 2 env vars).
                env["JARVIS_FILE_ISOLATION_ENABLED"] = "false"
                env["JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED"] = "false"

                # ---- Virtualized writable Trinity root (Blocker #4 structural fix) ----
                # The isomorphic env makes the organism believe it lives at the
                # literal /opt/trinity/jarvis (the production path) -- but that base
                # is not writable off the GCE node (no admin on the dev host). Inject
                # a writable, per-run state root so JARVIS_TRINITY_ROOT-aware storage
                # (intake WAL/lock, ...) lands somewhere unprivileged. The code stays
                # byte-identical in production: the env var is unset on the real node,
                # where storage falls back to project_root exactly as before.
                _trinity_root = os.path.join(run_dir, "trinity_root")
                os.makedirs(_trinity_root, exist_ok=True)
                env["JARVIS_TRINITY_ROOT"] = _trinity_root

                # ---- Deterministic Synthetic Roadmap (A1 emit-hop provenance) ----
                # compose_env arms the orchestrator + A1Trace flags but NOT the
                # roadmap READER, and no signed roadmap exists -> the strategic-GOAL
                # `emit source=roadmap` hop never fires and the A1 audit fails on a
                # missing chain. Generate a GENUINE signed GOAL (production crypto)
                # so _roadmap_ignition_daemon emits it. Skipped for stub wiring runs.
                if not self.stub_soak:
                    try:
                        _rm_path = _arm_synthetic_roadmap(env, run_dir)
                        _log("[A1] synthetic signed roadmap armed -> %s "
                             "(reader ENABLED, REQUIRE_SIGNATURE on, real HMAC)" % _rm_path)
                    except Exception as _exc:  # noqa: BLE001 -- never block the soak
                        _log("[A1] WARN synthetic roadmap arming failed "
                             "(emit hop will be absent): %r" % (_exc,))

                _log("env composed: %d keys total, adversary overrides applied, "
                     "failover=%s" % (len(env), "enabled" if self.enable_failover
                                      else "pinned-off"))

                chaos = harness_mod.ChaosController(
                    repo_root=self.repo_root,
                    test_timeout_s=60.0,
                )
                _ACTIVE_CHAOS.append(chaos)

                try:
                    # ── a. PREFLIGHT ─────────────────────────────────────────
                    _log("STEP preflight: chaos status")
                    st = chaos.status()
                    if st.get("active"):
                        _log("ABORT: active chaos manifest already exists "
                             "(run --revert first)")
                        verdict = {"proven": False,
                                   "failure_locus": "preflight:active_manifest"}
                        return 1

                    # ── b. BOOT SOAK FIRST (run-#12 fix) ─────────────────────
                    #
                    # This is the critical ordering change: the O+V organism is
                    # booted BEFORE chaos is injected.  The TestFailureSensor
                    # subscribes to fs.changed.* during boot; only then does the
                    # injection + touch trigger a scoped pytest run (not the full
                    # tests/ suite).
                    verdict_out = os.path.join(run_dir, "a1_verdict.json")
                    soak_proc: Any = None
                    debug_log: str = ""

                    if self.stub_soak:
                        _log("STEP soak: STUB -- post-boot chaos sequencing (run-#12)")
                        debug_log = os.path.join(run_dir, "stub_debug.log")
                        harness_mod.write_stub_soak_log(
                            debug_log, goal_id="GOAL-ISO-A1")
                        soak_proc = None
                    else:
                        _log("STEP soak: launching production O+V (pre-inject boot) "
                             "iso_cwd=%s" % iso_cwd)
                        if self.enable_failover:
                            _log("[HybridMesh] soak-child wall extended to %ds "
                                 "(32B cold-start: readiness %ds + margin)" % (
                                     _failover_soak_wall(True),
                                     int(_env_float(
                                         "JARVIS_HYBRID_MESH_READY_BUDGET_S", 900.0))))
                        soak_runner = harness_mod.SoakRunner(
                            repo_root=self.repo_root,
                            cost_cap=0.0,
                            wall_seconds=_failover_soak_wall(self.enable_failover),
                        )
                        # Register for process-group teardown (finally+atexit+signal)
                        # BEFORE launch so a crash mid-launch still reaps the group.
                        _ACTIVE_SOAK_RUNNERS.append(soak_runner)
                        # Thread IsomorphicEnv: launch child with disjoint cwd so
                        # os.getcwd()-as-repo-root bugs surface in the real chain.
                        # The env (composed above) carries JARVIS_SANDBOX_PREFIXES +
                        # JARVIS_REPO_PATH so the child can locate the real repo.
                        handle = _launch_iso_soak(soak_runner, env, run_dir, iso_cwd)
                        debug_log = handle.debug_log
                        soak_proc = handle.proc
                        _log("STEP await boot READY (TestWatcher fs.changed.* sub)")
                        _await_soak_boot(soak_proc, debug_log, timeout_s=90.0)

                    _log("STEP soak boot OK: debug_log=%s" % debug_log)

                    # ── c. INJECT CHAOS POST-BOOT (run-#12 fix) ───────────────
                    _log("STEP inject POST-BOOT: seed=%d" % self.seed)
                    red = chaos.inject(self.seed)
                    injected = True

                    if not red and not self.stub_soak:
                        _log("ABORT: test did not go RED post-injection")
                        verdict = {"proven": False, "failure_locus": "inject:not_red"}
                        return 1
                    _log("STEP inject OK (red=%s stub=%s)" % (red, self.stub_soak))

                    # ── d. TOUCH chaos files → fire fs.changed.* (run-#12) ────
                    manifest_path = os.path.join(
                        self.repo_root, ".jarvis", "chaos_manifest.json")
                    chaos_files = auditor_mod.load_chaos_target_files(manifest_path)
                    if chaos_files:
                        touched = _touch_chaos_files(chaos_files, self.repo_root)
                        scoped = _derive_scoped_test_targets(
                            chaos_files, self.repo_root)
                        _log("run-#12: %d file(s) touched; scoped pytest: %s"
                             % (len(touched),
                                scoped[0] if scoped else "<none found locally>"))
                    else:
                        _log("run-#12: no chaos files in manifest (stub mode?)")

                    # ── d.5 L7 SEMANTIC READINESS GATE (Task HM-B) ───────────
                    # When failover is armed the audit MUST suspend until the
                    # awakened 32B node returns HTTP 200 on /api/tags (model loaded
                    # into VRAM) -- otherwise the audit fires FAILED before the node
                    # can serve (the exact failure of the last live run). Default
                    # path (no --enable-failover): skipped entirely -> byte-identical.
                    if self.enable_failover:
                        _node = os.environ.get(
                            "JARVIS_FAILOVER_NODE_NAME", _FAILOVER_NODE_DEFAULT)
                        _budget = _env_float(
                            "JARVIS_HYBRID_MESH_READY_BUDGET_S",
                            _READY_BUDGET_DEFAULT_S)
                        _log("[HybridMesh] L7 readiness gate: awaiting 32B SERVING "
                             "(budget=%.0fs) before audit ..." % _budget)
                        _served = await _await_jprime_serving(
                            _node, budget_s=_budget, debug_log=debug_log)
                        _log("[HybridMesh] L7 readiness gate -> %s"
                             % ("SERVING" if _served else "TIMEOUT"))

                    # ── Fast-Fail short-circuit: a global L4 capacity wall means the
                    # cognitive loop can NEVER reach APPLIED this run. Skip the audit
                    # ceiling entirely, emit a capacity verdict, and flow to teardown
                    # -- zero wasted wall-clock (no 480s audit on a foregone result).
                    _capacity_wall = (
                        self.enable_failover and _hardware_capacity_exhausted(debug_log)
                    )
                    if _capacity_wall:
                        _log("[HybridMesh] HardwareCapacityExhausted -> short-circuit "
                             "A1 audit (global L4 stockout; NOT a cognitive failure)")
                        verdict = {
                            "proven": False,
                            "failure_locus": "hardware_capacity_exhausted:no_l4_global",
                            "capacity_exhausted": True,
                        }
                        proven = False
                        _log("STEP audit VERDICT: SKIPPED (hardware_capacity_exhausted)")
                    else:
                        # ── e. LAUNCH AUDITOR ────────────────────────────────
                        _log("STEP audit: sse=%s log=%s" % (self.sse_base, debug_log))
                        if self.stub_soak:
                            aud_runner = harness_mod.StubAuditorRunner(
                                strict=self.strict, goal_id="GOAL-ISO-A1")
                        else:
                            aud_runner = harness_mod.AuditorRunner(strict=self.strict)

                        verdict = aud_runner.watch(
                            base=self.sse_base,
                            log_file=debug_log,
                            timeout_s=_a1_audit_ceiling_s(debug_log=debug_log),
                            verdict_out=verdict_out,
                        )

                        proven = bool(verdict.get("proven"))
                        _log("STEP audit VERDICT: %s"
                             % ("A1_DISPATCH_PROVEN" if proven else "FAILED"))

                    # ── f. FAILURE PATH: T5 telemetry + local autopsy ────────
                    if not proven:
                        _log("STEP telemetry: capturing failure artifacts (T5)")
                        try:
                            capture_failure_telemetry(
                                output_dir=Path(run_dir) / "telemetry",
                                reason="a1_iso_not_proven:%s"
                                % verdict.get("failure_locus", ""),
                            )
                        except Exception as exc:  # noqa: BLE001
                            _log("telemetry warning: %r" % (exc,))
                        try:
                            harness_mod.local_autopsy(
                                run_id=run_id,
                                autopsy_root=os.path.join(
                                    self.run_root, "autopsy"),
                                debug_log=debug_log,
                                verdict=verdict,
                                chaos_manifest=manifest_path,
                            )
                        except Exception as exc:  # noqa: BLE001
                            _log("autopsy warning: %r" % (exc,))

                    _log("run complete: %s -> %s"
                         % (run_id, "PROVEN" if proven else "FAILED"))
                    return 0 if proven else 1

                except Exception as exc:  # noqa: BLE001
                    _log("orchestration error: %r" % (exc,))
                    verdict = {
                        "proven": False,
                        "failure_locus": "orchestration_error:%s" % type(exc).__name__,
                    }
                    try:
                        capture_failure_telemetry(
                            output_dir=Path(run_dir) / "telemetry",
                            reason="orchestration_error:%s" % type(exc).__name__,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return 1

                finally:
                    # CHAOS-REVERT-ALWAYS: repo must never be left broken.
                    if injected and chaos is not None:
                        _log("STEP revert: restoring chaos (always)")
                        try:
                            chaos.revert()
                        except Exception as exc:  # noqa: BLE001
                            _log("revert warning: %r" % (exc,))
                    if chaos in _ACTIVE_CHAOS:
                        _ACTIVE_CHAOS.remove(chaos)

        finally:
            try:
                await adversary.stop()
                _log("adversary stopped")
            except Exception as exc:  # noqa: BLE001
                _log("adversary stop warning: %r" % (exc,))
            # Process-group teardown of the soak organism + its worker pool, so no
            # multiprocessing worker ever orphans (the PPID->1 OOM leak). Runs on
            # completion/failure/cancel; no-op once drained by signal/atexit.
            _reap_soak_runners()
            # IRONCLAD teardown (Task HM-A): reap any awakened failover node + its
            # firewall AFTER the soak completes/fails/cancels. No-op (registry
            # empty) when failover was never armed -> default path byte-identical.
            _reap_failover_resources()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="isomorphic_a1_local.py",
        description=(
            "Isomorphic A1 Local E2E driver (Task 6). "
            "Full O+V A1 chain under GCP-node-identical conditions for $0. "
            "Post-boot chaos injection fires fs.changed dynamically (run-#12 fix). "
            "Lineage-scoped intervention lock (run-#13 fix) is in the auditor."
        ),
    )
    p.add_argument(
        "--mode", choices=["process", "container"], default="process",
        help="Isomorphic env mode: process (symlink + env-patch, default) or "
             "container (docker run --network none).",
    )
    p.add_argument(
        "--stub-soak", action="store_true",
        help="Stub soak: write a synthetic debug.log (no real O+V process, $0). "
             "Proves the wiring without a live soak.",
    )
    p.add_argument(
        "--seed", type=int,
        default=int(os.environ.get("JARVIS_A1_CHAOS_SEED", "0")),
        help="Chaos injector seed for deterministic target selection.",
    )
    p.add_argument(
        "--base",
        default=os.environ.get("JARVIS_A1_SSE_BASE", "http://127.0.0.1:7778"),
        help="SSE observability base URL.",
    )
    p.add_argument(
        "--strict", action="store_true", default=True,
        help="Strict auditor mode (UNVERIFIABLE -> FAIL; default).",
    )
    p.add_argument(
        "--lenient", action="store_false", dest="strict",
        help="Lenient auditor mode (UNVERIFIABLE -> WARN).",
    )
    p.add_argument("--run-root", default=None,
                   help="Root directory for run artifacts (default: ./a1_iso_runs).")
    p.add_argument(
        "--adversary-fault",
        default=os.environ.get("JARVIS_ISO_ADVERSARY_FAULT", "none"),
        choices=["none", "http5xx", "transport", "timeout", "parse_error", "http429"],
        help="Deterministic fault to inject into the DW provider via the "
             "SyntheticAdversary (default: none = transparent passthrough).",
    )
    p.add_argument("--verbose", action="store_true", help="Verbose output.")
    p.add_argument(
        "--enable-failover", action="store_true", default=False,
        help="Opt in to real GCE failover awaken during local fidelity run "
             "(default: JARVIS_FAILOVER_LIFECYCLE_ENABLED=false is pinned in "
             "child env to prevent accidental GCE spend).",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _install_revert_signal_handlers()
    if args.enable_failover:
        # IRONCLAD teardown (Task HM-A): a process-exit safety net in addition to
        # run()'s finally + the signal handlers. Idempotent -> at most one real
        # reap. Only registered when failover is opted in, so the default path is
        # byte-identical (no atexit hook).
        atexit.register(_reap_failover_resources)
        atexit.register(_reap_soak_runners)
    driver = IsomorphicA1Driver(
        mode=args.mode,
        seed=args.seed,
        stub_soak=args.stub_soak,
        strict=args.strict,
        sse_base=args.base,
        run_root=args.run_root,
        adversary_fault=(
            args.adversary_fault if args.adversary_fault != "none" else None
        ),
        verbose=args.verbose,
        enable_failover=args.enable_failover,
    )
    return asyncio.run(driver.run())


if __name__ == "__main__":
    sys.exit(main())

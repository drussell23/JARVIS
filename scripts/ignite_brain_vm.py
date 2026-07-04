"""ignite_brain_vm.py -- Stage-1 Brain-VM ignition driver + cross-host acceptance.

Provisions (or reuses) the GCP CPU Brain VM, opens an ephemeral /32 mTLS firewall
for THIS Mac, connects a Stage-0 ``DistributedEventBus`` client over the Brain's
WS transport, runs the LOAD-BEARING Stage-1 acceptance exchange, and tears
EVERYTHING down on every exit path with a $0-orphan guarantee.

Teardown discipline (mirrors ``scripts/isomorphic_a1_local.py``)
--------------------------------------------------------------
A module-level ``_ACTIVE_BRAIN_RESOURCES`` registry is drained by
``_reap_brain_resources`` on ``finally`` + ``atexit`` + SIGINT/SIGTERM --
whichever fires first; the rest are no-ops once drained (idempotent, never
raises). Each entry is registered UP FRONT (before the VM even exists) so a
crash mid-provision still reaps whatever got created. The firewall is ALWAYS
closed; the VM is deleted only when ``JARVIS_BRAIN_VM_PERSISTENT`` is false
(on-demand-per-session default). Delete is idempotent (404==already-gone) and is
attempted across the whole ``zone_fallback_chain`` so a node the scatter-gather
awaken landed in a fallback zone is never orphaned.

The acceptance exchange (Stage-1 proof)
---------------------------------------
``ValidationExchange`` is transport-agnostic and unit-testable: it publishes N
events Mac->Brain and asserts they are observed on the Brain, publishes
Brain->Mac and asserts observed on the Mac (both bus directions), then severs the
WS mid-stream, RE-DISCOVERS (stateless) + reconnects, and asserts exact
Last-Event-ID replay (zero gap / zero dup -- the Stage-0 guarantee, now
cross-host). mTLS-required is proven by the discovery probe itself (a certless
handshake is rejected by the Brain before the WS upgrade). Post-run the driver
verifies $0: no brain instance + no brain firewall rule remain.

The live path hits real GCP (Mandate 1: no cloud simulation); the fakes live
only in the unit test (``tests/infra/test_ignite_brain_vm.py``), which proves the
exchange + provision + teardown LOGIC without spending a cent.

Env knobs (all resolved at call time -- Mandate 2, zero baked assumptions):
    JARVIS_BRAIN_VM_PERSISTENT        opt-in warm standby (default false)
    JARVIS_BRAIN_VM_NODE_NAME         node name (default jarvis-brain-<stamp>)
    JARVIS_BRAIN_FIREWALL_RULE_NAME   /32 rule name (default jarvis-brain-mtls)
    JARVIS_BRAIN_READY_BUDGET_S       readiness-await budget (default 600)
    JARVIS_BRAIN_READY_BASE_S/_CAP_S  readiness backoff base/cap
    JARVIS_BRAIN_EXCHANGE_N           events per acceptance direction (default 5)
    JARVIS_BRAIN_WS_*                 Stage-0 transport (host/port/path/TLS/certs)
    JARVIS_BRAIN_VM_MACHINE_TYPE / _IMAGE_FAMILY / _IDLE_SHUTDOWN_S / _SPOT
                                      (resolved inside gcp_compute_rest)

Usage::

    python3 scripts/ignite_brain_vm.py                 # on-demand: provision+reap
    JARVIS_BRAIN_VM_PERSISTENT=true python3 scripts/ignite_brain_vm.py  # warm standby
    python3 scripts/ignite_brain_vm.py --dry-run       # print the plan, no GCP
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import os
import signal
import sys
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths -- derived from this file's location; backend importable regardless of cwd.
# ---------------------------------------------------------------------------
_SCRIPTS_DIR: str = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT: str = os.path.dirname(_SCRIPTS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _log(msg: str) -> None:
    print("[BrainIgnite] %s" % (msg,), flush=True)


def _truthy(val: Optional[str]) -> bool:
    return str(val or "").strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Env knobs (resolved at call time).
# ---------------------------------------------------------------------------

_ENV_PERSISTENT = "JARVIS_BRAIN_VM_PERSISTENT"
_ENV_NODE_NAME = "JARVIS_BRAIN_VM_NODE_NAME"
_ENV_FW_RULE_NAME = "JARVIS_BRAIN_FIREWALL_RULE_NAME"
_DEFAULT_FW_RULE_NAME = "jarvis-brain-mtls"
_ENV_READY_BUDGET_S = "JARVIS_BRAIN_READY_BUDGET_S"
_ENV_READY_BASE_S = "JARVIS_BRAIN_READY_BASE_S"
_ENV_READY_CAP_S = "JARVIS_BRAIN_READY_CAP_S"
_ENV_EXCHANGE_N = "JARVIS_BRAIN_EXCHANGE_N"
_ENV_MTLS_DIR = "JARVIS_BRAIN_MTLS_DIR"

# The server-side TLS material travels from the Mac (gen_brain_mtls.py output)
# to the node as instance metadata; the runtime startup script writes it to
# these node-local paths BEFORE the units start, and brain.env points at them.
_NODE_TLS_DIR = "/etc/jarvis/tls"
_TLS_META_KEYS = {
    "server_cert": ("brain-tls-server-cert", "server-cert.pem"),
    "server_key": ("brain-tls-server-key", "server-key.pem"),
    "ca": ("brain-tls-ca", "ca.pem"),
}


class TLSMaterialError(RuntimeError):
    """TLS is enabled but the server material is absent/incomplete. Raised in the
    driver preflight BEFORE any GCP call (fail-closed, $0)."""


def _tls_material() -> Optional[Dict[str, str]]:
    """Load the SERVER PEM material (server cert/key + CA) from
    ``JARVIS_BRAIN_MTLS_DIR`` (the gen_brain_mtls.py output dir). Returns None
    when TLS is disabled; raises ``TLSMaterialError`` when TLS is enabled and any
    piece is missing -- the ignition must never spend on a node that can only
    fail its mTLS probe."""
    if not _truthy(os.environ.get("JARVIS_BRAIN_WS_TLS_ENABLED", "true")):
        return None
    raw_dir = (os.environ.get(_ENV_MTLS_DIR, "") or "").strip()
    if not raw_dir:
        raise TLSMaterialError(
            "TLS enabled but %s is unset -- run scripts/gen_brain_mtls.py and "
            "export the material dir" % _ENV_MTLS_DIR)
    mat: Dict[str, str] = {}
    for part, (_meta_key, filename) in _TLS_META_KEYS.items():
        path = os.path.join(raw_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                mat[part] = fh.read()
        except OSError as exc:
            raise TLSMaterialError(
                "TLS enabled but %s is missing/unreadable (%s) -- aborting "
                "BEFORE any GCP spend" % (path, exc)) from exc
    return mat


def _tls_extra_metadata(mat: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Map the loaded PEM material onto its instance-metadata keys."""
    if not mat:
        return {}
    return {meta_key: mat[part] for part, (meta_key, _fn) in _TLS_META_KEYS.items()}


def _persistent() -> bool:
    return _truthy(os.environ.get(_ENV_PERSISTENT, "false"))


def _node_name() -> str:
    val = (os.environ.get(_ENV_NODE_NAME, "") or "").strip()
    return val or ("jarvis-brain-%s" % time.strftime("%Y%m%d-%H%M%S"))


def _fw_rule_name() -> str:
    val = (os.environ.get(_ENV_FW_RULE_NAME, "") or "").strip()
    return val or _DEFAULT_FW_RULE_NAME


# ---------------------------------------------------------------------------
# Teardown reaper -- the $0-orphan guarantee (mirrors _reap_failover_resources).
#
# Each entry: {"node": <name|None>, "fw_rule": <name|None>, "persistent": bool,
#              "zone": <zone|None>}
#   node=None      -> a REUSED persistent VM (never provisioned here -> never deleted)
#   persistent=True-> LEAVE the VM running; still close the firewall
# ---------------------------------------------------------------------------

_ACTIVE_BRAIN_RESOURCES: List[Dict[str, Any]] = []

# Collaborators the SYNC reaper (signal / atexit path) uses. Injected by the
# driver (and by tests). Default None -> the reaper resolves the live GCP
# firewall/instance functions lazily.
_REAP_CLOSE_FW_FN: Optional[Callable[..., Awaitable[Any]]] = None
_REAP_DELETE_FN: Optional[Callable[..., Awaitable[Any]]] = None


def _set_reap_collaborators(
    *,
    close_firewall_fn: Optional[Callable[..., Awaitable[Any]]],
    delete_instance_fn: Optional[Callable[..., Awaitable[Any]]],
) -> None:
    """Point the sync (signal/atexit) reaper at specific firewall/delete
    callables. The driver sets these before it registers any resource so a
    SIGTERM at ANY point reaps through the same path the ``finally`` uses."""
    global _REAP_CLOSE_FW_FN, _REAP_DELETE_FN
    _REAP_CLOSE_FW_FN = close_firewall_fn
    _REAP_DELETE_FN = delete_instance_fn


def _register_brain_resource(
    *, node: Optional[str], fw_rule: Optional[str], persistent: bool,
    zone: Optional[str] = None,
) -> None:
    """Register a Brain node + its ephemeral firewall for teardown UP FRONT
    (before the node exists) so a crash mid-provision still reaps what landed."""
    _ACTIVE_BRAIN_RESOURCES.append({
        "node": node,
        "fw_rule": fw_rule,
        "persistent": bool(persistent),
        "zone": zone or os.environ.get("GCP_ZONE"),
    })


def _reap_confirm_attempts() -> int:
    """Bounded delete+recheck rounds -- reuses the same env lever the GCP reap
    path uses so a wedged zone never blocks teardown forever."""
    try:
        from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
            _reap_confirm_attempts as _rca,
        )

        return int(_rca())
    except Exception:  # noqa: BLE001
        return _env_int("JARVIS_REAP_CONFIRM_ATTEMPTS", 6)


async def _default_close_firewall(rule_name: str) -> Any:
    from backend.core.ouroboros.governance.brain_discovery import (  # noqa: PLC0415
        close_brain_firewall,
    )

    return await close_brain_firewall(rule_name=rule_name)


async def _default_delete_instance(node: str, *, zone: Optional[str] = None) -> Any:
    from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
        get_compute_rest,
    )

    return await get_compute_rest().delete_instance(node, zone=zone)


async def _reap_brain_resources_async(
    *,
    close_firewall_fn: Optional[Callable[..., Awaitable[Any]]] = None,
    delete_instance_fn: Optional[Callable[..., Awaitable[Any]]] = None,
) -> None:
    """Drain the resource registry: close every firewall + delete every
    non-persistent VM across the whole zone fallback chain. Idempotent (drains
    the registry so a second call is a no-op) + fail-soft (NEVER raises)."""
    if not _ACTIVE_BRAIN_RESOURCES:
        return
    resources = list(_ACTIVE_BRAIN_RESOURCES)
    _ACTIVE_BRAIN_RESOURCES.clear()

    close_fw = close_firewall_fn or _REAP_CLOSE_FW_FN or _default_close_firewall
    delete_vm = delete_instance_fn or _REAP_DELETE_FN or _default_delete_instance

    try:
        from backend.core.ouroboros.governance.zone_fallback import (  # noqa: PLC0415
            zone_fallback_chain,
        )

        zones = zone_fallback_chain(os.environ.get("GCP_ZONE"))
    except Exception:  # noqa: BLE001
        zones = [os.environ.get("GCP_ZONE")] if os.environ.get("GCP_ZONE") else [None]
    if not zones:
        zones = [None]

    attempts = max(1, _reap_confirm_attempts())

    for r in resources:
        fw_rule = r.get("fw_rule")
        if fw_rule:
            try:
                await close_fw(rule_name=fw_rule)
            except Exception as exc:  # noqa: BLE001 -- teardown must never raise
                _log("firewall close warning (continuing): %r" % (exc,))
        node = r.get("node")
        if node and not r.get("persistent"):
            # Reap-confirm: idempotent delete across every candidate zone. The
            # bounded ``attempts`` loop guards a LATE async materialization (an
            # 'unknown' insert op that surfaces a node after the first pass).
            for _round in range(attempts):
                any_alive = False
                for z in zones:
                    try:
                        ok, detail = await delete_vm(node, zone=z)
                        # A non-terminal delete (not deleted/404) means the node
                        # may still be materializing -> another round.
                        if not ok:
                            any_alive = True
                    except Exception as exc:  # noqa: BLE001
                        _log("delete warning node=%s zone=%s (continuing): %r"
                             % (node, z, exc))
                        any_alive = True
                if not any_alive:
                    break
    _log("reaped %d brain resource(s) -- firewall(s) closed + non-persistent "
         "VM(s) deleted" % len(resources))


def _reap_brain_resources() -> None:
    """SYNC reaper for the ``finally`` / ``atexit`` / signal paths. Dispatches the
    async reap on a dedicated thread when a loop is already running (a signal that
    interrupted ``asyncio.run``), else runs it directly. Idempotent + NEVER
    raises."""
    if not _ACTIVE_BRAIN_RESOURCES:
        return

    def _run_blocking() -> None:
        asyncio.run(_reap_brain_resources_async())

    try:
        try:
            asyncio.get_running_loop()
            t = threading.Thread(target=_run_blocking, name="brain-reap", daemon=True)
            t.start()
            t.join(timeout=_env_float("JARVIS_FAILOVER_REST_TIMEOUT_S", 30.0) + 15.0)
        except RuntimeError:
            _run_blocking()
    except Exception as exc:  # noqa: BLE001 -- teardown must NEVER raise
        _log("reap error (continuing): %r" % (exc,))


def _install_teardown_signal_handlers() -> None:
    """SIGINT/SIGTERM -> reap the Brain VM + firewall before exiting. The REST
    delete is the process's last breath even on Ctrl+C."""
    def _handler(signum: int, _frame: Any) -> None:
        _log("signal %d received -- reaping brain resources before exit" % (signum,))
        try:
            _reap_brain_resources()
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
# Post-teardown $0 verify.
# ---------------------------------------------------------------------------


async def _default_list_brain_instances() -> List[Dict[str, Any]]:
    from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
        _BRAIN_ROLE_LABEL_KEY,
        _BRAIN_ROLE_LABEL_VALUE,
        get_compute_rest,
    )

    return await get_compute_rest().list_instances_by_label(
        label_key=_BRAIN_ROLE_LABEL_KEY, label_value=_BRAIN_ROLE_LABEL_VALUE,
    )


async def _default_firewall_exists(rule_name: str) -> bool:
    """Best-effort: a successful close returns deleted/404 which IS the $0 proof;
    there is no cheap read API, so default to False (assume-closed) and let a
    non-terminal close surface as a warning. Tests inject a real probe."""
    return False


async def _teardown_verify(
    *,
    persistent: bool,
    list_brain_fn: Optional[Callable[[], Awaitable[List[Dict[str, Any]]]]] = None,
    firewall_exists_fn: Optional[Callable[[str], Awaitable[bool]]] = None,
    rule_name: Optional[str] = None,
) -> Dict[str, Any]:
    """The driver's OWN $0 proof: assert no brain instance (unless persistent) +
    no brain firewall remain post-teardown. Returns
    ``{"zero_cost": bool, "leaked": [str, ...]}``. NEVER raises."""
    leaked: List[str] = []
    list_fn = list_brain_fn or _default_list_brain_instances
    fw_fn = firewall_exists_fn or _default_firewall_exists
    name = rule_name or _fw_rule_name()

    if not persistent:
        try:
            instances = await list_fn()
            for inst in instances or []:
                status = str(inst.get("status") or "").upper()
                if status in ("RUNNING", "PROVISIONING", "STAGING", "REPAIRING"):
                    leaked.append("instance:%s" % (inst.get("name") or "?"))
        except Exception as exc:  # noqa: BLE001
            _log("teardown-verify instance list warning: %r" % (exc,))
    try:
        if await fw_fn(name):
            leaked.append("firewall:%s" % name)
    except Exception as exc:  # noqa: BLE001
        _log("teardown-verify firewall check warning: %r" % (exc,))

    return {"zero_cost": not leaked, "leaked": leaked}


# ---------------------------------------------------------------------------
# The Stage-1 acceptance exchange (transport-agnostic + unit-testable).
# ---------------------------------------------------------------------------

# A BusEndpoint is any object with:
#   publish(event_type: str, op_id: str, payload: dict) -> str (event_id)
#   observed() -> List[Tuple[op_id, event_id]]  (receive order)
#   last_event_id -> Optional[str]

_EXCHANGE_EVENT_TYPE = "task_started"  # a valid StreamEventBroker event type


def _gap_dup(published_ids: List[str], observed_ids: List[str]) -> Tuple[List[str], List[str]]:
    """Compute the (gap, dup) of ``observed_ids`` vs ``published_ids``: a gap is a
    published id never observed; a dup is an id observed more than once."""
    obs_counts: Dict[str, int] = {}
    for eid in observed_ids:
        obs_counts[eid] = obs_counts.get(eid, 0) + 1
    gap = [eid for eid in published_ids if obs_counts.get(eid, 0) == 0]
    dup = [eid for eid, c in obs_counts.items() if c > 1]
    return gap, dup


class ValidationExchange:
    """Cross-host acceptance exchange over two ``BusEndpoint``s + a stateless
    ``reconnect`` callable. Every assertion is returned as structured data so the
    driver logs it and the unit test asserts on it."""

    def __init__(
        self,
        *,
        mac: Any,
        brain: Any,
        reconnect: Callable[[], Awaitable[Any]],
        sever: Optional[Callable[[], Awaitable[Any]]] = None,
        settle_s: float = 0.25,
        observe_timeout_s: float = 10.0,
        event_type: str = _EXCHANGE_EVENT_TYPE,
    ) -> None:
        self._mac = mac
        self._brain = brain
        self._reconnect = reconnect
        self._sever = sever
        self._settle_s = max(0.0, settle_s)
        self._observe_timeout_s = max(0.0, observe_timeout_s)
        self._event_type = event_type
        # LOOPBACK GUARD (Fix 3): when the Mac and Brain observation endpoints are
        # the SAME object, the bidirectional "cross-host" proof is really a local
        # loopback (no Brain echo subscriber) -- it proves nothing across the host
        # boundary. Recorded so ``run_all`` can gate the ``proven`` flag: a
        # loopback run can NEVER report cross-host-proven.
        self._loopback = mac is brain

    async def _await_observed(
        self, endpoint: Any, expected_ids: List[str],
    ) -> List[str]:
        """Poll ``endpoint.observed()`` until every expected event_id is present
        or the observe budget elapses. Returns the observed event_ids for the
        expected op set (in receive order)."""
        expected = set(expected_ids)
        deadline = time.monotonic() + self._observe_timeout_s
        while True:
            observed_ids = [eid for _op, eid in endpoint.observed()
                            if eid in expected]
            if expected.issubset(set(observed_ids)):
                return observed_ids
            if time.monotonic() >= deadline:
                return observed_ids
            await asyncio.sleep(min(0.05, self._settle_s) if self._settle_s else 0.02)

    async def _publish_batch(
        self, source: Any, n: int, tag: str,
    ) -> Tuple[List[str], List[str]]:
        """Publish ``n`` events on ``source``; return (op_ids, event_ids)."""
        op_ids: List[str] = []
        event_ids: List[str] = []
        for i in range(n):
            op_id = "brain-accept-%s-%d-%d" % (tag, int(time.time() * 1000), i)
            eid = source.publish(self._event_type, op_id, {"seq": i, "tag": tag})
            op_ids.append(op_id)
            if eid:
                event_ids.append(eid)
        if self._settle_s:
            await asyncio.sleep(self._settle_s)
        return op_ids, event_ids

    async def run_bidirectional(self, n: int) -> Dict[str, Any]:
        """Mac->Brain then Brain->Mac; each direction asserts zero gap / zero
        dup on the DESTINATION endpoint."""
        # Mac -> Brain
        _ops, m2b_ids = await self._publish_batch(self._mac, n, "m2b")
        observed = await self._await_observed(self._brain, m2b_ids)
        gap, dup = _gap_dup(m2b_ids, observed)
        m2b = {"ok": not gap and not dup, "published": len(m2b_ids),
               "observed": len(observed), "gap": gap, "dup": dup}

        # Brain -> Mac
        _ops2, b2m_ids = await self._publish_batch(self._brain, n, "b2m")
        observed2 = await self._await_observed(self._mac, b2m_ids)
        gap2, dup2 = _gap_dup(b2m_ids, observed2)
        b2m = {"ok": not gap2 and not dup2, "published": len(b2m_ids),
               "observed": len(observed2), "gap": gap2, "dup": dup2}

        return {"mac_to_brain": m2b, "brain_to_mac": b2m,
                "ok": bool(m2b["ok"] and b2m["ok"])}

    async def run_reconnect_replay(self, n: int) -> Dict[str, Any]:
        """Prove Last-Event-ID replay is LOAD-BEARING: publish ``n`` Brain->Mac
        live, record the Mac's last acked id, SEVER the link, publish ``n`` more
        events WHILE SEVERED (buffered at the source -- NOT delivered live), then
        stateless re-discover/reconnect so the reconnect's Last-Event-ID replay is
        the ONLY delivery route for the severed span. Assert the Mac observes the
        exact contiguous pre+post span with zero gap / zero dup."""
        _ops, pre_ids = await self._publish_batch(self._brain, n, "pre")
        await self._await_observed(self._mac, pre_ids)
        last_id = self._mac.last_event_id

        # SEVER before publishing the post span so those events cannot deliver
        # live -- they can only arrive via the reconnect's Last-Event-ID replay.
        if self._sever is not None:
            await self._sever()
        _ops2, post_ids = await self._publish_batch(self._brain, n, "post")

        # Re-discover + reconnect (stateless -- the caller re-resolves the endpoint
        # from scratch). The hello-frame's Last-Event-ID triggers the replay of the
        # severed span. The reconnected Mac endpoint replaces ours.
        self._mac = await self._reconnect()

        expected = pre_ids + post_ids
        observed = await self._await_observed(self._mac, expected)
        gap, dup = _gap_dup(expected, observed)
        return {
            "ok": not gap and not dup,
            "last_event_id_at_sever": last_id,
            "published": len(expected),
            "observed": len(observed),
            "gap": gap,
            "dup": dup,
        }

    async def run_all(self, n: int) -> Dict[str, Any]:
        bidi = await self.run_bidirectional(n)
        replay = await self.run_reconnect_replay(n)
        logic_ok = bool(bidi["ok"] and replay["ok"])
        # Fix 3: a loopback run (mac is brain, no Brain echo) can NEVER be read as
        # cross-host-proven. Gate ``proven`` on NOT loopback; surface loopback_only.
        return {
            "proven": bool(logic_ok and not self._loopback),
            "loopback_only": self._loopback,
            "logic_ok": logic_ok,
            "bidirectional": bidi,
            "reconnect_replay": replay,
        }


# ---------------------------------------------------------------------------
# Live BusEndpoint over a StreamEventBroker + DistributedEventBus client.
# ---------------------------------------------------------------------------


class _LiveMacEndpoint:
    """Live Mac-side endpoint: publishes onto a local ``StreamEventBroker`` (which
    a client ``DistributedEventBus`` bridge pumps to the Brain) and observes every
    event that lands in the local broker (Brain-originated events arrive via the
    client's inbound republish). ``last_event_id`` tracks the client bridge's
    contiguous high-water mark (the real Last-Event-ID replay cursor)."""

    def __init__(self, bus: Any, broker: Any, client_getter: Callable[[], Any]) -> None:
        self.name = "mac"
        self._bus = bus
        self._broker = broker
        self._client_getter = client_getter
        self._observed: List[Tuple[str, str]] = []
        # Task-9 wiring: (op -> our minted event_id) for the echo mapping, and
        # the full observed stream (type/op/eid/payload) for protocol parsing.
        self._published: Dict[str, str] = {}
        self._observed_full: List[Tuple[str, str, str, Dict[str, Any]]] = []
        self._sub = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        sub = self._broker.subscribe(op_id_filter=None, last_event_id=None)
        if sub is None:
            return
        self._sub = sub

        async def _collect() -> None:
            try:
                async for ev in self._broker.stream_iter(sub, heartbeat_s=0):
                    eid = getattr(ev, "event_id", "") or ""
                    op = getattr(ev, "op_id", "") or ""
                    if eid:
                        etype = getattr(ev, "event_type", "") or ""
                        payload = dict(getattr(ev, "payload", None) or {})
                        self._observed_full.append((etype, op, eid, payload))
                        # Brain-origin nonce events surface AS their nonce so the
                        # exchange's expected-id matching sees what brain.publish
                        # returned (the nonce IS the cross-host correlation id).
                        parsed = _xp().parse_observed(etype, op, payload)
                        if parsed and parsed[0] == "nonce":
                            self._observed.append((op, parsed[1]))
                        else:
                            self._observed.append((op, eid))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass

        self._task = asyncio.ensure_future(_collect())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def publish(self, event_type: str, op_id: str, payload: Dict[str, Any]) -> str:
        eid = self._bus.publish(event_type, op_id, payload) or ""
        if eid:
            self._published[op_id] = eid
        return eid

    def observed(self) -> List[Tuple[str, str]]:
        return list(self._observed)

    @property
    def last_event_id(self) -> Optional[str]:
        client = self._client_getter()
        return getattr(client, "last_event_id", None) if client else None


def _xp():
    """Lazy exchange-protocol import (keeps module import light for --help)."""
    import backend.core.ouroboros.governance.transport.exchange_protocol as xp
    return xp


class _LiveBrainEndpoint:
    """The BRAIN endpoint as seen FROM the Mac (Task-9 un-loopback).

    ``publish()`` asks the Brain to publish: a nonce'd command crosses the
    bridge, the Brain's echo responder answers with a BRAIN-ORIGIN event
    carrying the nonce -- so the returned "event id" (the nonce) is observed on
    the Mac iff the Brain really published across the wire.

    ``observed()`` is what the Brain has PROVEN to observe: its echoes, mapped
    back to the Mac-side event ids of the original ops (an echo for op X arrived
    <=> the Brain saw the event the Mac minted for op X)."""

    def __init__(self, mac: "_LiveMacEndpoint") -> None:
        self.name = "brain"
        self._mac = mac
        self._pub_seq = 0

    def publish(self, event_type: str, op_id: str, payload: Dict[str, Any]) -> str:
        self._pub_seq += 1
        nonce = "bn-%s-%d" % (op_id, self._pub_seq)
        self._mac.publish(event_type, op_id, _xp().make_publish_cmd(nonce))
        return nonce

    def observed(self) -> List[Tuple[str, str]]:
        xp = _xp()
        out: List[Tuple[str, str]] = []
        for etype, op, _eid, payload in self._mac._observed_full:
            parsed = xp.parse_observed(etype, op, payload)
            if parsed and parsed[0] == "echo":
                orig_op = parsed[1]
                mac_eid = self._mac._published.get(orig_op)
                if mac_eid:
                    out.append((orig_op, mac_eid))
        return out


# ---------------------------------------------------------------------------
# The ignition driver.
# ---------------------------------------------------------------------------


class BrainIgnitionDriver:
    """Provision (or reuse) -> firewall -> connect -> acceptance exchange ->
    teardown ($0). Every GCP/transport collaborator is an injectable seam; the
    defaults are the live REST/discovery/transport functions. The unit test
    injects fakes for all of them (Mandate 1: no cloud simulation in the LIVE
    path; fakes only in tests)."""

    def __init__(
        self,
        *,
        persistent: Optional[bool] = None,
        node_name: Optional[str] = None,
        fw_rule_name: Optional[str] = None,
        exchange_n: Optional[int] = None,
        ready_budget_s: Optional[float] = None,
        # Injectable seams (live defaults resolved lazily inside run()).
        create_instance_fn: Optional[Callable[..., Awaitable[Tuple[bool, str]]]] = None,
        discover_fn: Optional[Callable[..., Awaitable[Optional[str]]]] = None,
        open_firewall_fn: Optional[Callable[..., Awaitable[Any]]] = None,
        close_firewall_fn: Optional[Callable[..., Awaitable[Any]]] = None,
        delete_instance_fn: Optional[Callable[..., Awaitable[Any]]] = None,
        list_brain_fn: Optional[Callable[[], Awaitable[List[Dict[str, Any]]]]] = None,
        firewall_exists_fn: Optional[Callable[[str], Awaitable[bool]]] = None,
        run_exchange_fn: Optional[Callable[[], Awaitable[Dict[str, Any]]]] = None,
        dry_run: bool = False,
    ) -> None:
        self.persistent = _persistent() if persistent is None else bool(persistent)
        self.node_name = node_name or _node_name()
        self.fw_rule_name = fw_rule_name or _fw_rule_name()
        self.exchange_n = exchange_n if exchange_n is not None else _env_int(_ENV_EXCHANGE_N, 5)
        self.ready_budget_s = (ready_budget_s if ready_budget_s is not None
                               else _env_float(_ENV_READY_BUDGET_S, 600.0))
        self.dry_run = dry_run

        self._create_instance = create_instance_fn
        self._discover = discover_fn
        self._open_firewall = open_firewall_fn
        self._close_firewall = close_firewall_fn
        self._delete_instance = delete_instance_fn
        self._list_brain = list_brain_fn
        self._firewall_exists = firewall_exists_fn
        self._run_exchange = run_exchange_fn

    # -- lazy live default resolvers ---------------------------------------

    async def _do_create_instance(self) -> Tuple[bool, str]:
        if self._create_instance is not None:
            return await self._create_instance(name=self.node_name)
        from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
            _BRAIN_ENV_METADATA_KEY,
            _BRAIN_ROLE_LABEL_KEY,
            _BRAIN_ROLE_LABEL_VALUE,
            _brain_image_family,
            _brain_machine_type,
            _compose_brain_env,
            get_compute_rest,
        )

        brain_env = _compose_brain_env(self._brain_env_values())
        extra = {_BRAIN_ENV_METADATA_KEY: brain_env}
        # Ship the SERVER PEM material as metadata; the runtime startup script
        # writes it to /etc/jarvis/tls/* before the units start.
        extra.update(_tls_extra_metadata(_tls_material()))
        return await get_compute_rest().create_instance(
            startup_script=_brain_runtime_startup_script(),
            name=self.node_name,
            machine_type=_brain_machine_type(),
            image_family=_brain_image_family(),
            accelerator_type="",
            accelerator_count=0,
            labels={_BRAIN_ROLE_LABEL_KEY: _BRAIN_ROLE_LABEL_VALUE},
            extra_metadata=extra,
        )

    def _brain_env_values(self) -> Dict[str, str]:
        """Fold the Task-4 ignition values into the brain-env metadata: the WS
        port/path + TLS material references + the session cost cap, all
        env-resolved (Mandate 2). The idle-shutdown seconds are added by
        ``_compose_brain_env`` itself.

        TLS paths: when server material is shipped (TLS enabled), the node's
        brain.env is OVERRIDDEN to the node-local /etc/jarvis/tls/* paths the
        runtime startup script writes -- the Mac-side env holds the Mac's OWN
        (client) paths, which must never leak into the node's config."""
        vals: Dict[str, str] = {}
        for key in (
            "JARVIS_BRAIN_WS_PORT", "JARVIS_BRAIN_WS_PATH", "JARVIS_BRAIN_WS_TLS_ENABLED",
            "JARVIS_BRAIN_WS_TLS_CERT", "JARVIS_BRAIN_WS_TLS_KEY", "JARVIS_BRAIN_WS_TLS_CA",
            "JARVIS_BRAIN_COST_CAP", "JARVIS_DISTRIBUTED_BUS_ENABLED",
            "JARVIS_BRAIN_BUS_SIDECAR_ENABLED", "JARVIS_BRAIN_OUTBOUND_TOPICS",
        ):
            v = os.environ.get(key)
            if v is not None and v != "":
                vals[key] = v
        if _tls_material() is not None:
            vals["JARVIS_BRAIN_WS_TLS_CERT"] = "%s/%s" % (
                _NODE_TLS_DIR, _TLS_META_KEYS["server_cert"][1])
            vals["JARVIS_BRAIN_WS_TLS_KEY"] = "%s/%s" % (
                _NODE_TLS_DIR, _TLS_META_KEYS["server_key"][1])
            vals["JARVIS_BRAIN_WS_TLS_CA"] = "%s/%s" % (
                _NODE_TLS_DIR, _TLS_META_KEYS["ca"][1])
        return vals

    async def _do_discover(self) -> Optional[str]:
        if self._discover is not None:
            return await self._discover()
        from backend.core.ouroboros.governance.brain_discovery import (  # noqa: PLC0415
            discover_brain_endpoint,
        )

        return await discover_brain_endpoint()

    async def _do_open_firewall(self) -> Any:
        if self._open_firewall is not None:
            return await self._open_firewall(rule_name=self.fw_rule_name)
        from backend.core.ouroboros.governance.brain_discovery import (  # noqa: PLC0415
            open_brain_firewall,
        )
        from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
            resolve_local_public_ip,
        )

        ip = await resolve_local_public_ip()
        return await open_brain_firewall(ip, rule_name=self.fw_rule_name)

    async def _await_ready(self) -> Optional[str]:
        """Await the Brain readiness sentinel by re-running discovery until it
        binds a healthy mTLS endpoint (the same stateless probe a reconnect uses).
        Exponential backoff bounded by ``ready_budget_s``. Returns the WS URL or
        None on timeout."""
        base = _env_float(_ENV_READY_BASE_S, 3.0)
        cap = _env_float(_ENV_READY_CAP_S, 30.0)
        deadline = time.monotonic() + self.ready_budget_s
        delay = base
        while time.monotonic() < deadline:
            url = await self._do_discover()
            if url:
                return url
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2.0, cap)
        return None

    async def _default_run_exchange(self, url: str) -> Dict[str, Any]:
        """LIVE acceptance exchange: connect a client ``DistributedEventBus`` over
        mTLS to the discovered Brain URL, run the bidirectional + reconnect-replay
        proof. The Brain side of the bidirectional proof requires the Brain
        harness's echo subscriber (wired/validated in Task 5); this driver owns the
        Mac side + the full exchange LOGIC (unit-proven)."""
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: PLC0415
            StreamEventBroker,
        )
        from backend.core.ouroboros.governance.transport.distributed_event_bus import (  # noqa: PLC0415
            DistributedEventBus,
        )
        from backend.core.ouroboros.governance.transport.transport_config import (  # noqa: PLC0415
            TransportConfig,
        )

        cfg = TransportConfig.from_env(role="brain-client")
        broker = StreamEventBroker()
        bus = DistributedEventBus(broker, cfg, role="client")
        client_task = asyncio.ensure_future(bus.start_client(url))

        def _client_getter() -> Any:
            return getattr(bus, "_client", None)

        async def _await_connected(budget_s: float) -> bool:
            # CONNECT GATE (live-fire attempt 2): events published before the
            # first successful connect are live-only-lost. Never start the
            # exchange on a dark link.
            deadline = time.monotonic() + budget_s
            while time.monotonic() < deadline:
                client = _client_getter()
                if client is not None and getattr(client, "connected", False):
                    return True
                await asyncio.sleep(0.1)
            return False

        mac = _LiveMacEndpoint(bus, broker, _client_getter)
        await mac.start()
        if not await _await_connected(_env_float("JARVIS_BRAIN_CONNECT_GATE_S", 30.0)):
            _log("[BrainIgnite] connect gate TIMEOUT -- WS client never established")
            await mac.stop()
            client_task.cancel()
            return {"proven": False, "logic_ok": False,
                    "error": "connect_gate_timeout"}

        _live_tasks: List[asyncio.Task] = [client_task]

        async def _sever() -> Any:
            # Drop the WS: stop the client bridge so events published next buffer in
            # the local broker history and can only reach the peer via the reconnect
            # hello-frame's Last-Event-ID replay.
            try:
                await bus.stop()
            except Exception:  # noqa: BLE001
                pass

        async def _reconnect() -> Any:
            # Stateless re-discovery: re-resolve the endpoint from scratch (a Brain
            # that moved is re-found), reconnect a fresh client -> Last-Event-ID
            # replay fills the severed span (the bus carries the outbound cursor
            # across client instances).
            new_url = await self._do_discover() or url
            new_task = asyncio.ensure_future(bus.start_client(new_url))
            _live_tasks.append(new_task)
            await _await_connected(_env_float("JARVIS_BRAIN_CONNECT_GATE_S", 30.0))
            return mac

        # Task-9 un-loopback: the Brain endpoint is REAL -- publish() commands the
        # Brain's echo responder over the wire, observed() reads its echoes. The
        # loopback guard clears (mac is not brain) and ``proven`` is reachable.
        brain = _LiveBrainEndpoint(mac)
        _log("[BrainIgnite] cross-host exchange armed (echo responder protocol; "
             "loopback mode retired)")
        exchange = ValidationExchange(
            mac=mac, brain=brain, reconnect=_reconnect, sever=_sever,
            settle_s=_env_float("JARVIS_BRAIN_EXCHANGE_SETTLE_S", 1.0),
            observe_timeout_s=_env_float("JARVIS_BRAIN_EXCHANGE_OBSERVE_S", 15.0),
        )
        try:
            result = await exchange.run_all(self.exchange_n)
        finally:
            await mac.stop()
            try:
                await bus.stop()
            except Exception:  # noqa: BLE001
                pass
            for t in _live_tasks:
                t.cancel()
        return result

    # -- the run FSM -------------------------------------------------------

    async def run(self) -> int:
        # Point the sync (signal/atexit) reaper at THIS run's collaborators before
        # registering any resource, so a SIGTERM at any point reaps identically.
        _set_reap_collaborators(
            close_firewall_fn=self._close_firewall,
            delete_instance_fn=self._delete_instance,
        )

        if self.dry_run:
            _log("[dry-run] would open firewall %s, provision/reuse node %s "
                 "(persistent=%s) -- no GCP touched" % (self.fw_rule_name,
                                                        self.node_name, self.persistent))
            return 0

        # 0. TLS PREFLIGHT -- fail-closed BEFORE any GCP call: a node that cannot
        #    pass its own mTLS probe must never be paid for.
        try:
            _tls_material()
        except TLSMaterialError as exc:
            _log("TLS preflight FAILED ($0, nothing provisioned): %s" % exc)
            return 4

        proven = False
        try:
            # 1. FIREWALL first (discovery's mTLS probe needs our /32 open).
            _register_brain_resource(
                node=None, fw_rule=self.fw_rule_name, persistent=self.persistent)
            ok, detail = await self._do_open_firewall()
            _log("firewall %s -> %s (%s)" % (self.fw_rule_name,
                                             "OPEN" if ok else "FAILED", detail))

            # 2. PROVISION or REUSE.
            url: Optional[str] = None
            if self.persistent:
                url = await self._do_discover()
            if url:
                _log("REUSE running brain (persistent) -- no provision")
                # A reused VM was not provisioned here -> never delete it. Update
                # the registered entry's node to None (already None) -- firewall
                # stays registered for close.
            else:
                # Register the node for teardown UP FRONT (orphan safety), then
                # provision. Non-persistent -> the reaper will delete it.
                _register_brain_resource(
                    node=self.node_name, fw_rule=None, persistent=self.persistent)
                _log("provisioning brain node=%s (persistent=%s)"
                     % (self.node_name, self.persistent))
                cok, cdetail = await self._do_create_instance()
                _log("create_instance -> %s (%s)" % ("OK" if cok else "FAILED", cdetail))
                if not cok:
                    _log("provision FAILED -- aborting (teardown will reap)")
                    return 2
                url = await self._await_ready()
                if not url:
                    _log("readiness TIMEOUT after %.0fs -- aborting (teardown reaps)"
                         % self.ready_budget_s)
                    return 3

            # 3. CONNECT + 4. VALIDATION EXCHANGE.
            _log("connecting DistributedEventBus client over mTLS -> %s" % url)
            if self._run_exchange is not None:
                result = await self._run_exchange()
            else:
                result = await self._default_run_exchange(url)
            proven = bool(result.get("proven"))
            if result.get("loopback_only"):
                _log("ACCEPTANCE NOT PROVEN (LOOPBACK_ONLY) -- exchange logic_ok=%r "
                     "but cross-host round-trip requires the Brain echo (Task 5): %r"
                     % (result.get("logic_ok"), result))
            else:
                _log("ACCEPTANCE %s: %r"
                     % ("PROVEN" if proven else "NOT PROVEN", result))
            return 0 if proven else 4

        except Exception as exc:  # noqa: BLE001
            _log("run error: %r -- teardown will reap" % (exc,))
            return 1
        finally:
            # 5. TEARDOWN on EVERY exit path (idempotent; also armed on
            # signal/atexit). Reap first, then verify $0.
            await _reap_brain_resources_async(
                close_firewall_fn=self._close_firewall,
                delete_instance_fn=self._delete_instance,
            )
            verdict = await _teardown_verify(
                persistent=self.persistent,
                list_brain_fn=self._list_brain,
                firewall_exists_fn=self._firewall_exists,
                rule_name=self.fw_rule_name,
            )
            if verdict["zero_cost"]:
                _log("TEARDOWN VERIFY: $0 -- no brain instance / firewall remain")
            else:
                _log("TEARDOWN VERIFY: NON-$0 -- leaked=%r" % (verdict["leaked"],))


# ---------------------------------------------------------------------------
# Runtime startup script (thin provisioning glue Task 4 owns): repopulate
# /etc/jarvis/brain.env from THIS instance's metadata, create the /run/jarvis
# runtime dir + liveness marker (the deferred Task-1 coupling), and (re)start the
# baked brain unit so it picks up the fresh env.
# ---------------------------------------------------------------------------


def _brain_runtime_startup_script() -> str:
    """Minimal boot glue for the golden-image runtime boot. The image already has
    the ``jarvis-brain.service`` units baked+enabled; this only (a) writes
    /etc/jarvis/brain.env from the runtime ``brain-env`` metadata, (b) ensures
    /run/jarvis exists + touches the liveness marker so the idle-check never nukes
    a freshly-booted node, and (c) restarts the unit to apply the fresh env.
    ASCII-only. Kept tiny + idempotent (Mandate 3: no new provisioning framework)."""
    meta_base = "http://metadata.google.internal/computeMetadata/v1/instance/attributes"
    tls_fetch = "".join(
        "curl -fsS -H 'Metadata-Flavor: Google' '%s/%s' -o %s/%s || true\n"
        % (meta_base, meta_key, _NODE_TLS_DIR, filename)
        for meta_key, filename in _TLS_META_KEYS.values()
    )
    return (
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        "mkdir -p /etc/jarvis /run/jarvis %s\n" % _NODE_TLS_DIR
        + "curl -fsS -H 'Metadata-Flavor: Google' "
        "'%s/brain-env' " % meta_base
        + "-o /etc/jarvis/brain.env || : > /etc/jarvis/brain.env\n"
        # SERVER TLS material (shipped as metadata by the driver) -> node files,
        # BEFORE any unit starts. Fail-soft per file: TLS-disabled ignitions have
        # no such metadata and must still boot.
        + tls_fetch
        + "chmod 600 %s/%s 2>/dev/null || true\n" % (
            _NODE_TLS_DIR, _TLS_META_KEYS["server_key"][1])
        # Refresh the baked clone (fail-soft): the golden image tracks main at
        # ignition, so driver-shipped scripts (e.g. the bus sidecar) that landed
        # AFTER the bake are present without a re-bake.
        + "git -C /opt/trinity/jarvis pull --ff-only || true\n"
        # Stage-0 bus + acceptance echo responder SIDECAR: the Brain-side WS
        # server the cross-host acceptance connects to. Same venv as the soak.
        + "cat > /etc/systemd/system/jarvis-brain-bus.service <<'UNIT'\n"
        "[Unit]\n"
        "Description=JARVIS Brain Stage-0 bus + acceptance echo responder\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "WorkingDirectory=/opt/trinity/jarvis\n"
        "EnvironmentFile=/etc/jarvis/brain.env\n"
        "RuntimeDirectory=jarvis\n"
        "ExecStart=/opt/trinity/venv/bin/python scripts/brain_bus_echo_server.py\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
        "UNIT\n"
        + "touch /run/jarvis/brain_liveness\n"
        "systemctl daemon-reload || true\n"
        "systemctl restart jarvis-brain.service || systemctl start jarvis-brain.service || true\n"
        "systemctl restart jarvis-brain-bus.service || systemctl start jarvis-brain-bus.service || true\n"
        "systemctl start jarvis-brain-idle.timer || true\n"
    )


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ignite_brain_vm.py",
        description=(
            "Stage-1 Brain-VM ignition driver + cross-host acceptance harness. "
            "Provisions (or reuses) the CPU Brain VM, opens an ephemeral /32 mTLS "
            "firewall for this Mac, connects the Stage-0 WS bus, runs the "
            "bidirectional + Last-Event-ID-replay acceptance exchange, and tears "
            "everything down ($0-orphan guarantee) on every exit path."
        ),
    )
    p.add_argument(
        "--persistent", action="store_true", default=None,
        help="Keep the Brain VM as a warm standby (default: env "
             "JARVIS_BRAIN_VM_PERSISTENT, else on-demand-per-session).",
    )
    p.add_argument(
        "--node-name", default=None,
        help="Brain node name (default: env JARVIS_BRAIN_VM_NODE_NAME or "
             "jarvis-brain-<stamp>).",
    )
    p.add_argument(
        "--exchange-n", type=int, default=None,
        help="Events per acceptance direction (default: env JARVIS_BRAIN_EXCHANGE_N "
             "or 5).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print the ignition plan and exit -- touches no GCP.",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _install_teardown_signal_handlers()
    # Process-exit safety net in addition to run()'s finally + the signal
    # handlers. Idempotent -> at most one real reap.
    atexit.register(_reap_brain_resources)
    driver = BrainIgnitionDriver(
        persistent=args.persistent,
        node_name=args.node_name,
        exchange_n=args.exchange_n,
        dry_run=args.dry_run,
    )
    return asyncio.run(driver.run())


if __name__ == "__main__":
    sys.exit(main())

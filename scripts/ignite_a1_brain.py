"""ignite_a1_brain.py -- remote A1 dispatch on the GCP Brain VM.

================================================================================
Provisions the GCP Brain VM (Stage-1 CPU golden image), runs
``scripts/isomorphic_a1_local.py`` ON THE NODE (co-located: parent driver +
child soak share the SAME host so ``wall_deadline.json`` IPC stays same-host --
the historical cross-host coordination bug this deliberately avoids), monitors
the dispatch to ACTUAL completion, retrieves the authoritative A1 verdict, and
guarantees $0 teardown across a 5-layer defense-in-depth (see below).

Mandate 3 (DRY -- reuse, no new SSH client): this script does NOT reimplement
SSH/scp. It loads ``scripts/sovereign_iac_hypervisor.py`` via
``importlib.util.spec_from_file_location`` (a script, not a package -- same
pattern ``scripts/a1_live_fire_chaos_harness.py:730-739`` uses for exactly this
reason) and reuses:

  * ``hyper._ssh_cmd(args, node, remote) -> List[str]``  -- the IAP SSH argv builder.
  * ``hyper._run(cmd, timeout_s=...) -> (rc, out)``       -- the ONE subprocess boundary.
  * ``hyper._delete_node_cmd(args, node) -> List[str]``   -- teardown argv (preview only;
                                                              the REAL teardown goes
                                                              through the REST client).
  * ``a1_live_fire_chaos_harness.IapBlackBoxTransport``   -- the checksum-gated
    Black-Box pull (``bundle_on_node`` + ``pull_archive``), loaded the SAME way.

Provisioning composes ``backend.core.ouroboros.governance.brain_lifecycle.provision_brain``
directly (NOT ``ignite_brain_vm.BrainIgnitionDriver.run()``, which runs a full
acceptance+teardown cycle we don't want here -- we HOLD the node for the A1 run).

Piece B (declarative outbound allowlist, applied as CONFIG only -- no code
change): the remote command declares
``JARVIS_BRAIN_OUTBOUND_TOPICS=actuation.*,telemetry.posture.*,autonomy.*`` so
autonomy-governor op-lifecycle events (``EventEmitter._bridge_to_spine`` ->
``autonomy.{key}``) cross the Stage-3 mTLS bus. Transport stays payload-agnostic
-- this is just the existing declarative env seam
(``organism_bus_host.py::resolve_outbound_topics``), not a routing special-case.

Piece D (node-side absolute self-destruct) is armed by setting
``JARVIS_BRAIN_ABSOLUTE_LIFETIME_S`` in the provisioning env before calling
``provision_brain`` -- ``brain_lifecycle._brain_runtime_startup_script`` arms an
unconditional ``nohup bash -c 'sleep N; shutdown -h now' &`` dead-man
independent of this Mac, the brain process, and the idle-liveness marker.

Teardown defense-in-depth (mandate 4, $0 even on total Mac network loss)
-------------------------------------------------------------------------
  1. This script's own explicit ``get_compute_rest().delete_instance()`` on
     clean finish (fastest).
  2. An up-front-registered atexit/SIGINT/SIGTERM reaper (drains
     ``_ACTIVE_IGNITIONS``) -- bypassed only by SIGKILL.
  3. The node-side absolute self-destruct (Piece D) -- survives Mac SIGKILL /
     total network partition.
  4. ``scripts/gcp_cleanup.py`` (Piece A, already extended to match
     ``jarvis-role=brain``) -- Mac-side sweep once network returns.
  5. The existing idle-shutdown timer (clean-idle backstop).

``--dry-run`` prints the FULL plan (provision params, remote command, SSH argv,
Black-Box pull argv preview, teardown argv preview, absolute-lifetime) and
returns 0 with ZERO network/subprocess side effects -- no provision, no SSH, no
GCP REST call of any kind. It is also fully synchronous (no ``asyncio.run`` is
ever invoked on the dry-run path), so no coroutine can accidentally touch the
network even by mistake.

Design: ``from __future__ import annotations``, Python 3.9+ (``asyncio.wait_for``,
never ``asyncio.timeout``), ASCII-only, every tunable env-driven with a sane
default, every network/subprocess call fail-soft + logged (never a bare silent
except).
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import importlib.util
import json
import os
import re
import shlex
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Paths -- derived from this file's location; backend importable regardless of cwd.
# ---------------------------------------------------------------------------
_SCRIPTS_DIR: str = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT: str = os.path.dirname(_SCRIPTS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_HYPERVISOR_SCRIPT = os.path.join(_SCRIPTS_DIR, "sovereign_iac_hypervisor.py")
_CHAOS_HARNESS_SCRIPT = os.path.join(_SCRIPTS_DIR, "a1_live_fire_chaos_harness.py")

# The remote jarvis repo root on the node (matches the golden image / IaC sync target).
_REMOTE_REPO_ROOT = os.environ.get("JARVIS_A1_BRAIN_REMOTE_REPO", "/opt/trinity/jarvis")

# Piece C / B config baked into the remote command (facts-file authoritative values).
_DEFAULT_CHAOS_TARGET_DIRS = "backend/core/ouroboros/a1_ignition_vector"
_DEFAULT_OUTBOUND_TOPICS = "actuation.*,telemetry.posture.*,autonomy.*"

# The node-side Black Box bundler's default staging dir (matches a1_black_box.py /
# a1_live_fire_chaos_harness.py's JARVIS_A1_BLACKBOX_NODE_OUT default).
_BLACK_BOX_NODE_OUT = os.environ.get("JARVIS_A1_BLACKBOX_NODE_OUT", "/tmp/a1_black_box")


def _log(msg: str) -> None:
    print("[IgniteA1Brain] %s" % (msg,), flush=True)


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
# Module loader -- EXACTLY the pattern a1_live_fire_chaos_harness.py:730-739
# uses for IapBlackBoxTransport._hypervisor(): the hypervisor + chaos harness
# are SCRIPTS (not packages), so a normal `import` can't reach them by name;
# spec_from_file_location loads them by path instead. No reimplementation of
# any SSH/scp logic -- only the loader boilerplate is repeated.
# ---------------------------------------------------------------------------
def _load_module(name: str, path: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


def _default_hypervisor_loader() -> Any:
    return _load_module("sovereign_iac_hypervisor", _HYPERVISOR_SCRIPT)


def _default_blackbox_transport_factory(*, node: str, hypervisor_args: Any) -> Any:
    harness_mod = _load_module("a1_live_fire_chaos_harness", _CHAOS_HARNESS_SCRIPT)
    return harness_mod.IapBlackBoxTransport(node=node, hypervisor_args=hypervisor_args)


def _default_compute_rest_factory() -> Any:
    from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
        get_compute_rest,
    )

    return get_compute_rest()


async def _default_provision_fn(*, node_name: str) -> Tuple[bool, str]:
    from backend.core.ouroboros.governance.brain_lifecycle import (  # noqa: PLC0415
        provision_brain,
    )

    return await provision_brain(node_name=node_name)


# ---------------------------------------------------------------------------
# Preflight-only (dry-run-safe, PURE) project/zone planning defaults. These are
# NEVER used to actually drive provisioning/SSH in a real run -- the real run
# resolves project via a REST call (resolve_project) and zone via a
# post-create label query (resolve_actual_zone), per the facts file. These
# helpers only feed the informational --dry-run plan print with zero network.
# ---------------------------------------------------------------------------
def _resolve_project_for_plan() -> str:
    v = (
        os.environ.get("GCP_PROJECT_ID", "") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    ).strip()
    return v or (
        "<unresolved: set GCP_PROJECT_ID/GOOGLE_CLOUD_PROJECT -- resolved via "
        "get_compute_rest().project() at provision time>"
    )


def _resolve_zone_for_plan() -> str:
    v = (os.environ.get("GCP_ZONE", "") or "").strip()
    if v:
        return v
    try:
        from backend.core.ouroboros.governance.zone_fallback import (  # noqa: PLC0415
            zone_fallback_chain,
        )

        chain = zone_fallback_chain(None)
        return chain[0] if chain else "<unresolved>"
    except Exception:  # noqa: BLE001 -- planning helper must never raise
        return "<unresolved>"


def _default_node_name() -> str:
    v = (os.environ.get("JARVIS_A1_BRAIN_VM_NODE_NAME", "") or "").strip()
    return v or ("a1-brain-%s" % time.strftime("%Y%m%d-%H%M%S"))


def _default_remote_run_dir(node_name: str) -> str:
    v = (os.environ.get("JARVIS_A1_BRAIN_REMOTE_RUN_DIR", "") or "").strip()
    if v:
        return v
    return "%s/a1_iso_runs/%s" % (_REMOTE_REPO_ROOT, node_name)


# ---------------------------------------------------------------------------
# Remote command composition (Piece B config baked in; Piece C dispatch).
# ---------------------------------------------------------------------------
def _compose_remote_command(*, remote_run_dir: str, seed: int, verbose: bool) -> str:
    """The actual isomorphic_a1_local.py invocation that runs ON the node.
    Co-located (parent driver's SSH session + the soak child it launches share
    the same host) so wall_deadline.json IPC stays same-host -- unchanged from
    the local isomorphic-fidelity design."""
    verbose_flag = " --verbose" if verbose else ""
    return (
        "cd %s && "
        "JARVIS_CHAOS_TARGET_DIRS=%s "
        "JARVIS_BRAIN_OUTBOUND_TOPICS=%s "
        "OUROBOROS_BATTLE_HEADLESS=1 "
        "python3 scripts/isomorphic_a1_local.py --mode process --run-root %s "
        "--seed %d%s"
        % (
            _REMOTE_REPO_ROOT,
            _DEFAULT_CHAOS_TARGET_DIRS,
            _DEFAULT_OUTBOUND_TOPICS,
            remote_run_dir,
            seed,
            verbose_flag,
        )
    )


def _compose_dispatch_wrapper(remote_cmd: str, remote_run_dir: str) -> str:
    """Wrap the remote command in a detached (nohup + disown) shell so the SSH
    session returns immediately while the soak keeps running on the node. A
    done-file (with the child's exit code) is the completion signal `monitor()`
    polls for -- so verdict retrieval binds to ACTUAL completion, not a guessed
    ceiling."""
    done_file = remote_run_dir.rstrip("/") + "/ignite_dispatch.done"
    log_file = remote_run_dir.rstrip("/") + "/ignite_dispatch.log"
    inner = "%s; echo DONE_RC=$? > %s" % (remote_cmd, shlex.quote(done_file))
    return (
        "mkdir -p %s && nohup bash -c %s > %s 2>&1 < /dev/null & disown; "
        "echo DISPATCH_STARTED"
        % (shlex.quote(remote_run_dir), shlex.quote(inner), shlex.quote(log_file))
    )


# ---------------------------------------------------------------------------
# Up-front teardown registry -- mirrors ignite_brain_vm.py's
# _ACTIVE_BRAIN_RESOURCES / _reap_brain_resources pattern (idempotent, NEVER
# raises, drained by atexit + SIGINT/SIGTERM + the driver's own `finally`).
# Kept self-contained (not sharing ignite_brain_vm's global) so this script has
# no cross-module global-state coupling with a script another agent owns.
# ---------------------------------------------------------------------------
_ACTIVE_IGNITIONS: List[Dict[str, Any]] = []
_SIGNAL_HANDLERS_INSTALLED = False


def _register_ignition(
    *, node: str, zone: Optional[str], compute_rest_factory: Callable[[], Any]
) -> None:
    """Register a Brain node for teardown UP FRONT (before/at create) so a
    crash mid-provision still reaps it."""
    _ACTIVE_IGNITIONS.append(
        {"node": node, "zone": zone, "compute_rest_factory": compute_rest_factory}
    )


async def _reap_ignitions_async() -> None:
    """Drain the registry: delete every registered node. Idempotent (the
    registry is cleared before the loop runs, so re-entrant/second calls are a
    no-op) + fail-soft (NEVER raises -- teardown must always complete)."""
    if not _ACTIVE_IGNITIONS:
        return
    entries = list(_ACTIVE_IGNITIONS)
    _ACTIVE_IGNITIONS.clear()
    for entry in entries:
        node = entry.get("node")
        zone = entry.get("zone")
        factory = entry.get("compute_rest_factory") or _default_compute_rest_factory
        if not node:
            continue
        try:
            rest = factory()
            ok, detail = await rest.delete_instance(node, zone=zone)
            _log("[Reap] delete_instance node=%s zone=%s -> ok=%s detail=%s"
                 % (node, zone, ok, detail))
        except Exception as exc:  # noqa: BLE001 -- reap must NEVER raise
            _log("[Reap] warning node=%s: %r" % (node, exc))


def _reap_ignitions() -> None:
    """SYNC reaper for the atexit/signal paths. Dispatches the async reap on a
    dedicated thread when a loop is already running, else runs it directly.
    Idempotent + NEVER raises."""
    if not _ACTIVE_IGNITIONS:
        return
    import threading  # noqa: PLC0415 -- only needed on this rare signal path

    def _run_blocking() -> None:
        asyncio.run(_reap_ignitions_async())

    try:
        try:
            asyncio.get_running_loop()
            t = threading.Thread(target=_run_blocking, name="a1-brain-reap", daemon=True)
            t.start()
            t.join(timeout=_env_float("JARVIS_A1_BRAIN_REAP_JOIN_TIMEOUT_S", 45.0))
        except RuntimeError:
            _run_blocking()
    except Exception as exc:  # noqa: BLE001 -- teardown must NEVER raise
        _log("[Reap] error (continuing): %r" % (exc,))


def _install_signal_handlers() -> None:
    """SIGINT/SIGTERM -> reap before exiting; atexit -> reap as the last-resort
    net. Installed once (idempotent). Only SIGKILL bypasses this layer (Piece D
    + gcp_cleanup.py are the backstops for that case)."""
    global _SIGNAL_HANDLERS_INSTALLED
    if _SIGNAL_HANDLERS_INSTALLED:
        return

    def _handler(signum: int, _frame: Any) -> None:
        _log("signal %d received -- reaping ignition resources before exit" % (signum,))
        try:
            _reap_ignitions()
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

    atexit.register(_reap_ignitions)
    _SIGNAL_HANDLERS_INSTALLED = True


# ---------------------------------------------------------------------------
# The plan -- what --dry-run prints, and what the real run executes.
# ---------------------------------------------------------------------------
@dataclass
class IgnitionPlan:
    node_name: str
    project: str
    zone: str
    lifetime_s: int
    seed: int
    remote_run_dir: str
    remote_cmd: str
    dispatch_wrapper_cmd: str
    ssh_argv: List[str] = field(default_factory=list)
    pull_argv: List[str] = field(default_factory=list)
    teardown_argv: List[str] = field(default_factory=list)
    provision_env: Dict[str, str] = field(default_factory=dict)


class A1BrainIgnition:
    """Async orchestrator: preflight -> provision (HOLD) -> verify HEAD ->
    dispatch -> monitor -> retrieve verdict (authoritative) -> teardown ($0).

    Every collaborator is constructor-injectable so tests never touch real
    gcloud/GCP: `hypervisor_loader` / `blackbox_transport_factory` /
    `compute_rest_factory` / `provision_fn` / `runner`.
    """

    def __init__(
        self,
        *,
        node_name: Optional[str] = None,
        project: Optional[str] = None,
        zone: Optional[str] = None,
        lifetime_s: Optional[int] = None,
        seed: int = 0,
        remote_run_dir: Optional[str] = None,
        local_out_root: Optional[str] = None,
        verbose: bool = True,
        dry_run: bool = False,
        hypervisor_loader: Optional[Callable[[], Any]] = None,
        blackbox_transport_factory: Optional[Callable[..., Any]] = None,
        compute_rest_factory: Optional[Callable[[], Any]] = None,
        provision_fn: Optional[Callable[..., Any]] = None,
        runner: Optional[Callable[[Sequence[str], float], Tuple[int, str]]] = None,
        monitor_poll_s: Optional[float] = None,
        monitor_max_s: Optional[float] = None,
    ) -> None:
        self.node_name = node_name or _default_node_name()
        self.project = (project or "").strip()
        self.zone = (zone or "").strip()
        self.lifetime_s = int(
            lifetime_s if lifetime_s is not None
            else _env_int("JARVIS_A1_BRAIN_LIFETIME_S", 1800)
        )
        self.seed = int(seed)
        self.remote_run_dir = remote_run_dir or _default_remote_run_dir(self.node_name)
        self._local_out_root_val = local_out_root or os.path.join(
            os.getcwd(), "a1_brain_runs")
        self.verbose = bool(verbose)
        self.dry_run = bool(dry_run)

        self._hypervisor_loader = hypervisor_loader or _default_hypervisor_loader
        self._blackbox_transport_factory = (
            blackbox_transport_factory or _default_blackbox_transport_factory)
        self._compute_rest_factory = compute_rest_factory or _default_compute_rest_factory
        self._provision_fn = provision_fn or _default_provision_fn
        self._runner = runner

        self._monitor_poll_s = float(
            monitor_poll_s if monitor_poll_s is not None
            else _env_float("JARVIS_A1_BRAIN_MONITOR_POLL_S", 20.0))
        self._monitor_max_s = float(
            monitor_max_s if monitor_max_s is not None
            else _env_float("JARVIS_A1_BRAIN_MONITOR_MAX_S",
                             max(60.0, self.lifetime_s - 120.0)))

        self._hyper_mod: Optional[Any] = None

    # -- collaborator plumbing ------------------------------------------------

    def _hyper(self) -> Any:
        if self._hyper_mod is None:
            self._hyper_mod = self._hypervisor_loader()
        return self._hyper_mod

    def _ns(self) -> argparse.Namespace:
        return argparse.Namespace(project=self.project, zone=self.zone)

    def _local_out_root(self) -> str:
        return self._local_out_root_val

    def _done_file(self) -> str:
        return self.remote_run_dir.rstrip("/") + "/ignite_dispatch.done"

    def _log_file(self) -> str:
        return self.remote_run_dir.rstrip("/") + "/ignite_dispatch.log"

    def _ssh_exec(self, remote: str, *, timeout_s: float) -> Tuple[int, str]:
        """Fail-soft SSH exec via the REUSED hypervisor._ssh_cmd + _run (or an
        injected `runner`). Never raises."""
        hyper = self._hyper()
        argv = hyper._ssh_cmd(self._ns(), self.node_name, remote)
        run = self._runner or (lambda a, t: hyper._run(a, timeout_s=t))
        try:
            return run(argv, timeout_s)
        except Exception as exc:  # noqa: BLE001 -- ssh exec must never raise
            _log("SSH exec error (continuing, fail-soft): %r" % (exc,))
            return 1, "[ssh exec failed: %r]" % (exc,)

    async def _ssh_exec_async(self, remote: str, *, timeout_s: float) -> Tuple[int, str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._ssh_exec(remote, timeout_s=timeout_s))

    # -- plan (pure, dry-run-safe) --------------------------------------------

    def build_plan(self) -> IgnitionPlan:
        project = self.project or _resolve_project_for_plan()
        zone = self.zone or _resolve_zone_for_plan()
        remote_cmd = _compose_remote_command(
            remote_run_dir=self.remote_run_dir, seed=self.seed, verbose=self.verbose)
        dispatch_wrapper = _compose_dispatch_wrapper(remote_cmd, self.remote_run_dir)
        ns = argparse.Namespace(project=project, zone=zone)

        ssh_argv: List[str] = []
        teardown_argv: List[str] = []
        try:
            hyper = self._hyper()
            ssh_argv = hyper._ssh_cmd(ns, self.node_name, dispatch_wrapper)
            teardown_argv = hyper._delete_node_cmd(ns, self.node_name)
        except Exception as exc:  # noqa: BLE001 -- planning must never raise
            _log("WARN plan preview: hypervisor argv builders unavailable: %r" % (exc,))

        pull_argv: List[str] = []
        try:
            transport = self._blackbox_transport_factory(
                node=self.node_name, hypervisor_args=ns)
            placeholder_archive = "%s/black_box_<run_id>.tar.gz" % (_BLACK_BOX_NODE_OUT,)
            pull_argv = transport._scp_pull_cmd(placeholder_archive, self._local_out_root())
        except Exception as exc:  # noqa: BLE001 -- preview only, never fatal
            _log("WARN plan preview: black-box pull argv unavailable: %r" % (exc,))

        provision_env = {
            "JARVIS_BRAIN_VM_PERSISTENT": "true",
            "JARVIS_BRAIN_ABSOLUTE_LIFETIME_S": str(self.lifetime_s),
        }
        return IgnitionPlan(
            node_name=self.node_name,
            project=project,
            zone=zone,
            lifetime_s=self.lifetime_s,
            seed=self.seed,
            remote_run_dir=self.remote_run_dir,
            remote_cmd=remote_cmd,
            dispatch_wrapper_cmd=dispatch_wrapper,
            ssh_argv=ssh_argv,
            pull_argv=pull_argv,
            teardown_argv=teardown_argv,
            provision_env=provision_env,
        )

    def print_plan(self, plan: IgnitionPlan) -> None:
        def _argv(a: Sequence[str]) -> str:
            return " ".join(shlex.quote(c) for c in a) if a else "<unavailable>"

        print("=" * 78)
        print("IGNITE A1 BRAIN -- PLAN (dry-run, zero side effects)")
        print("=" * 78)
        print("  node_name         : %s" % plan.node_name)
        print("  project           : %s" % plan.project)
        print("  zone (pre-create) : %s  (actual zone resolved post-create via "
              "list_instances_by_label)" % plan.zone)
        print("  lifetime_s        : %d  (-> JARVIS_BRAIN_ABSOLUTE_LIFETIME_S, "
              "Piece D self-destruct)" % plan.lifetime_s)
        print("  seed              : %d" % plan.seed)
        print("  remote_run_dir    : %s" % plan.remote_run_dir)
        print("-" * 78)
        print("PROVISION ENV OVERRIDES (applied around brain_lifecycle.provision_brain):")
        for k, v in plan.provision_env.items():
            print("  %s=%s" % (k, v))
        print("-" * 78)
        print("REMOTE COMMAND (isomorphic_a1_local.py, co-located ON the node):")
        print("  " + plan.remote_cmd)
        print("-" * 78)
        print("DISPATCH WRAPPER (detached nohup; monitor() polls the done-file):")
        print("  " + plan.dispatch_wrapper_cmd)
        print("-" * 78)
        print("SSH ARGV (reused sovereign_iac_hypervisor._ssh_cmd):")
        print("  " + _argv(plan.ssh_argv))
        print("-" * 78)
        print("BLACK-BOX PULL ARGV preview (reused IapBlackBoxTransport._scp_pull_cmd):")
        print("  " + _argv(plan.pull_argv))
        print("-" * 78)
        print("TEARDOWN ARGV preview (reused sovereign_iac_hypervisor._delete_node_cmd):")
        print("  " + _argv(plan.teardown_argv))
        print("  (the REAL teardown executes get_compute_rest().delete_instance("
              "node, zone=...) directly -- this argv is informational only)")
        print("=" * 78)
        print("DRY RUN: zero network / subprocess side effects. Exiting 0.")

    # -- phase 2: provision (HOLD) ---------------------------------------------

    async def provision(self) -> Tuple[bool, str]:
        _install_signal_handlers()
        # Register UP FRONT (before create) so a crash mid-provision still reaps.
        _register_ignition(
            node=self.node_name, zone=self.zone or None,
            compute_rest_factory=self._compute_rest_factory)

        prev_persistent = os.environ.get("JARVIS_BRAIN_VM_PERSISTENT")
        prev_lifetime = os.environ.get("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S")
        os.environ["JARVIS_BRAIN_VM_PERSISTENT"] = "true"
        os.environ["JARVIS_BRAIN_ABSOLUTE_LIFETIME_S"] = str(self.lifetime_s)
        try:
            try:
                ok, detail = await self._provision_fn(node_name=self.node_name)
            except Exception as exc:  # noqa: BLE001 -- provision must surface, never crash
                _log("ERROR provision_brain raised: %r" % (exc,))
                return False, "PROVISION_EXCEPTION:%r" % (exc,)
        finally:
            if prev_persistent is None:
                os.environ.pop("JARVIS_BRAIN_VM_PERSISTENT", None)
            else:
                os.environ["JARVIS_BRAIN_VM_PERSISTENT"] = prev_persistent
            if prev_lifetime is None:
                os.environ.pop("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S", None)
            else:
                os.environ["JARVIS_BRAIN_ABSOLUTE_LIFETIME_S"] = prev_lifetime

        _log("provision -> ok=%s detail=%s" % (ok, detail))
        return ok, detail

    async def resolve_project(self) -> Optional[str]:
        if self.project:
            return self.project
        try:
            rest = self._compute_rest_factory()
            proj = await rest.project()
        except Exception as exc:  # noqa: BLE001
            _log("WARN project resolution error (continuing, fail-soft): %r" % (exc,))
            proj = None
        if proj:
            self.project = proj
            _log("resolved project=%s" % proj)
            return proj
        _log("ERROR could not resolve GCP project (set GCP_PROJECT_ID / "
             "GOOGLE_CLOUD_PROJECT, or configure ADC/SA) -- SSH/teardown calls "
             "downstream will be missing --project")
        return None

    async def resolve_actual_zone(self) -> Optional[str]:
        try:
            from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
                _BRAIN_ROLE_LABEL_KEY,
                _BRAIN_ROLE_LABEL_VALUE,
            )

            rest = self._compute_rest_factory()
            instances = await rest.list_instances_by_label(
                label_key=_BRAIN_ROLE_LABEL_KEY, label_value=_BRAIN_ROLE_LABEL_VALUE)
        except Exception as exc:  # noqa: BLE001
            _log("WARN zone resolution error (continuing, fail-soft): %r" % (exc,))
            return None
        for inst in instances or []:
            if inst.get("name") == self.node_name:
                raw_zone = str(inst.get("zone") or "")
                zone = raw_zone.rsplit("/", 1)[-1] if raw_zone else None
                if zone:
                    self.zone = zone
                    for entry in _ACTIVE_IGNITIONS:
                        if entry.get("node") == self.node_name:
                            entry["zone"] = zone
                    _log("resolved actual zone=%s for node=%s" % (zone, self.node_name))
                    return zone
        _log("WARN could not find node=%s via list_instances_by_label -- zone "
             "remains unresolved" % (self.node_name,))
        return None

    # -- phase 3: verify HEAD ---------------------------------------------------

    async def verify_head(self) -> Optional[str]:
        remote = "git -C %s rev-parse HEAD" % (_REMOTE_REPO_ROOT,)
        rc, out = await self._ssh_exec_async(
            remote, timeout_s=_env_float("JARVIS_A1_BRAIN_SSH_TIMEOUT_S", 60.0))
        text = (out or "").strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        sha = lines[-1] if lines else ""
        if rc == 0 and re.fullmatch(r"[0-9a-f]{40}", sha or ""):
            _log("remote HEAD verified: %s" % (sha,))
            return sha
        _log("WARN could not verify remote HEAD (rc=%d out=%r) -- proceeding "
             "(node-boot git-pull is best-effort; surfaced, not silently ignored)"
             % (rc, text[-200:] if text else ""))
        return None

    # -- phase 4: dispatch -------------------------------------------------------

    async def dispatch(self) -> bool:
        remote = _compose_remote_command(
            remote_run_dir=self.remote_run_dir, seed=self.seed, verbose=self.verbose)
        wrapper = _compose_dispatch_wrapper(remote, self.remote_run_dir)
        rc, out = await self._ssh_exec_async(
            wrapper, timeout_s=_env_float("JARVIS_A1_BRAIN_DISPATCH_TIMEOUT_S", 60.0))
        ok = rc == 0 and "DISPATCH_STARTED" in (out or "")
        _log("dispatch -> rc=%d ok=%s out=%r" % (rc, ok, (out or "")[-300:]))
        return ok

    # -- phase 5: monitor (bounded, fail-soft) -----------------------------------

    async def monitor(self) -> Dict[str, Any]:
        deadline = time.monotonic() + self._monitor_max_s
        tail_lines = _env_int("JARVIS_A1_BRAIN_MONITOR_TAIL_LINES", 40)
        last_tail = ""
        remote = (
            "if [ -f %s ]; then echo __IGNITE_DONE__; cat %s; else "
            "tail -n %d %s 2>/dev/null || true; fi"
            % (shlex.quote(self._done_file()), shlex.quote(self._done_file()),
               tail_lines, shlex.quote(self._log_file()))
        )
        while time.monotonic() < deadline:
            rc, out = await self._ssh_exec_async(
                remote, timeout_s=_env_float("JARVIS_A1_BRAIN_MONITOR_SSH_TIMEOUT_S", 30.0))
            if out:
                last_tail = out
                if self.verbose:
                    _log("[monitor] %s" % (out.strip()[-2000:],))
            if rc == 0 and "__IGNITE_DONE__" in (out or ""):
                _log("[monitor] remote dispatch COMPLETE")
                return {"completed": True, "tail": last_tail}
            await asyncio.sleep(self._monitor_poll_s)
        _log("[monitor] WARN monitor budget exhausted (%ds) without a done-file "
             "-- proceeding to verdict retrieval anyway (fail-soft; the verdict "
             "pull below is what's actually authoritative)" % (self._monitor_max_s,))
        return {"completed": False, "tail": last_tail}

    # -- phase 6: retrieve verdict (authoritative) --------------------------------

    def _discover_verdict_path(self) -> Optional[str]:
        remote = (
            "find %s -maxdepth 2 -name a1_verdict.json 2>/dev/null | sort | tail -n1"
            % (shlex.quote(self.remote_run_dir),)
        )
        rc, out = self._ssh_exec(remote, timeout_s=30.0)
        if rc != 0 or not out:
            return None
        lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
        return lines[-1] if lines else None

    def _fetch_verdict_json(self, path: str) -> Optional[Dict[str, Any]]:
        rc, out = self._ssh_exec("cat %s" % (shlex.quote(path),), timeout_s=30.0)
        if rc != 0 or not out:
            return None
        text = out.strip()
        start = text.find("{")
        if start == -1:
            return None
        try:
            return json.loads(text[start:])
        except Exception as exc:  # noqa: BLE001 -- malformed verdict must not crash
            _log("WARN verdict JSON parse error: %r" % (exc,))
            return None

    async def retrieve_verdict(self) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        verdict_path = await loop.run_in_executor(None, self._discover_verdict_path)
        verdict: Optional[Dict[str, Any]] = None
        if verdict_path:
            verdict = await loop.run_in_executor(
                None, self._fetch_verdict_json, verdict_path)
        else:
            _log("WARN could not discover a1_verdict.json under %s"
                 % (self.remote_run_dir,))

        # Checksum-gated Black Box pull -- supplementary diagnostic bundle
        # (debug.log + session context), reused as-is (no reimplementation).
        blackbox: Dict[str, Any] = {"pulled": False}
        try:
            transport = self._blackbox_transport_factory(
                node=self.node_name, hypervisor_args=self._ns())
            run_id = "a1-brain-%s" % (self.node_name,)
            local_dir = os.path.join(self._local_out_root(), self.node_name)
            bundle = await loop.run_in_executor(
                None,
                lambda: transport.bundle_on_node(run_id=run_id, out_dir=_BLACK_BOX_NODE_OUT),
            )
            if bundle:
                local_sha = await loop.run_in_executor(
                    None,
                    lambda: transport.pull_archive(
                        node_archive=bundle["archive"],
                        node_sha_path=bundle["sha_path"],
                        local_dir=local_dir,
                    ),
                )
                if local_sha and local_sha == bundle.get("sha256"):
                    blackbox = {"pulled": True, "local_dir": local_dir, "sha256": local_sha}
                    _log("[BlackBox] confirmed sha256=%s -> %s" % (local_sha[:16], local_dir))
                else:
                    _log("[BlackBox] WARN pull/checksum unresolved (node_sha=%s "
                         "local_sha=%s) -- diagnostic bundle NOT confirmed (the "
                         "direct SSH verdict fetch above is unaffected)"
                         % (bundle.get("sha256"), local_sha))
            else:
                _log("[BlackBox] WARN node-side bundle failed -- no diagnostic archive")
        except Exception as exc:  # noqa: BLE001 -- Black Box is supplementary, never fatal
            _log("[BlackBox] WARN bundling/pull error (continuing, fail-soft): %r" % (exc,))

        if verdict is not None:
            criteria = verdict.get("criteria", {}) or {}
            _log("A1 VERDICT proven=%s" % (verdict.get("proven"),))
            for name, ok in criteria.items():
                _log("  criterion %-32s %s" % (name, "PASS" if ok else "FAIL"))
        else:
            _log("A1 VERDICT: UNAVAILABLE (could not retrieve a1_verdict.json "
                 "from the node)")

        return {"verdict": verdict, "verdict_path": verdict_path, "blackbox": blackbox}

    # -- phase 7: teardown ($0) ---------------------------------------------------

    async def teardown(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"deleted": False, "detail": "", "zero_cost": False}
        try:
            rest = self._compute_rest_factory()
            ok, detail = await rest.delete_instance(self.node_name, zone=self.zone or None)
            result["deleted"] = ok
            result["detail"] = detail
            _log("[Teardown] delete_instance node=%s zone=%s -> ok=%s detail=%s"
                 % (self.node_name, self.zone, ok, detail))
        except Exception as exc:  # noqa: BLE001 -- teardown must never raise
            _log("[Teardown] WARN delete_instance error (continuing to reaper): %r"
                 % (exc,))

        # Defense-in-depth layer 2: drain the up-front reaper registry. Run off
        # the event loop -- the sync reaper may block on a thread-join.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _reap_ignitions)

        try:
            from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
                _BRAIN_ROLE_LABEL_KEY,
                _BRAIN_ROLE_LABEL_VALUE,
            )

            rest2 = self._compute_rest_factory()
            instances = await rest2.list_instances_by_label(
                label_key=_BRAIN_ROLE_LABEL_KEY, label_value=_BRAIN_ROLE_LABEL_VALUE)
            leaked = [i for i in (instances or []) if i.get("name") == self.node_name]
            result["zero_cost"] = not leaked
            _log("[Teardown] $0 verify: %s"
                 % ("CONFIRMED (no matching instance)" if not leaked
                    else "LEAK detected: %r" % (leaked,)))
        except Exception as exc:  # noqa: BLE001
            _log("[Teardown] WARN $0-verify error (continuing): %r" % (exc,))
        return result

    # -- top-level run -------------------------------------------------------

    async def run(self) -> int:
        if self.dry_run:
            self.print_plan(self.build_plan())
            return 0

        await self.resolve_project()
        try:
            ok, detail = await self.provision()
            if not ok:
                _log("ABORT: provision failed (%s)" % (detail,))
                return 1
            await self.resolve_actual_zone()
            await self.verify_head()
            dispatched = await self.dispatch()
            if not dispatched:
                _log("ABORT: dispatch did not confirm DISPATCH_STARTED")
                return 1
            await self.monitor()
            result = await self.retrieve_verdict()
            verdict = result.get("verdict") or {}
            return 0 if verdict.get("proven") else 1
        finally:
            await self.teardown()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ignite_a1_brain.py",
        description=(
            "Remote A1 dispatch on the GCP Brain VM -- provision (HOLD), run "
            "isomorphic_a1_local.py co-located on the node, monitor to actual "
            "completion, retrieve the authoritative A1 verdict, and guarantee "
            "$0 teardown. Reuses sovereign_iac_hypervisor's SSH primitives + "
            "a1_live_fire_chaos_harness's IapBlackBoxTransport + "
            "brain_lifecycle.provision_brain -- no new SSH client."
        ),
    )
    p.add_argument("--dry-run", action="store_true",
                    help="Print the full plan and exit 0. ZERO network/subprocess "
                         "side effects -- no provision, no SSH, no GCP REST call.")
    p.add_argument("--node-name", default=None,
                    help="Brain VM node name (default: env JARVIS_A1_BRAIN_VM_NODE_NAME "
                         "or a1-brain-<timestamp>).")
    p.add_argument("--seed", type=int,
                    default=int(os.environ.get("JARVIS_A1_CHAOS_SEED", "0")),
                    help="Chaos injector seed passed to isomorphic_a1_local.py.")
    p.add_argument("--lifetime-s", type=int, default=None,
                    help="Absolute node lifetime in seconds (default: env "
                         "JARVIS_A1_BRAIN_LIFETIME_S or 1800). Arms the node-side "
                         "unconditional self-destruct (Piece D).")
    p.add_argument("--remote-run-dir", default=None,
                    help="Run-root on the node passed to isomorphic_a1_local.py's "
                         "--run-root (default: env JARVIS_A1_BRAIN_REMOTE_RUN_DIR or "
                         "<repo>/a1_iso_runs/<node-name>).")
    p.add_argument("--out-dir", default=None,
                    help="Local directory the Black Box archive + verdict artifacts "
                         "are pulled into (default: ./a1_brain_runs).")
    p.add_argument("--verbose", action="store_true", default=True,
                    help="Verbose monitor tail logging (default: on).")
    p.add_argument("--quiet", action="store_false", dest="verbose",
                    help="Suppress verbose monitor tail logging.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    ignition = A1BrainIgnition(
        node_name=args.node_name,
        seed=args.seed,
        lifetime_s=args.lifetime_s,
        remote_run_dir=args.remote_run_dir,
        local_out_root=args.out_dir,
        verbose=args.verbose,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        # Fully synchronous path -- asyncio.run() is never invoked, so no
        # coroutine can accidentally touch the network on a --dry-run call.
        ignition.print_plan(ignition.build_plan())
        return 0
    return asyncio.run(ignition.run())


if __name__ == "__main__":
    sys.exit(main())

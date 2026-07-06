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

Mandate 3 (verdict retrieval is 100% via IapBlackBoxTransport): ``a1_verdict.json``
is now bundled INTO the node-side Black Box archive by ``a1_black_box.py`` (it is
part of the audit trail). ``retrieve_verdict()`` is AUTHORITATIVE via the
checksum-verified pulled tarball: it EXTRACTS ``a1_verdict.json`` from the local
archive only after ``pull_archive()`` confirms the local sha256 matches the
node-emitted sha256 -- no separate SSH ``find``/``cat`` is used for the
authoritative path. A direct-SSH fallback (``_discover_verdict_path`` /
``_fetch_verdict_json``) is retained ONLY as an explicit, clearly-logged
FAIL-SOFT path for when the Black-Box bundle/pull fails entirely -- it is
gated behind ``JARVIS_A1_BRAIN_VERDICT_SSH_FALLBACK_ENABLED`` (default
``true``) and every result carries ``verdict_source`` (``"blackbox"`` /
``"ssh_fallback"`` / ``"none"``) so a fallback read is never mistaken for the
authoritative one.

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

Coordinated soak-wall <-> node-lifetime budget (review fix): ``N`` above
(``lifetime_s``) is DERIVED from ``soak_wall_s`` (also baked into the remote
command as ``OUROBOROS_BATTLE_MAX_WALL_SECONDS``) plus env-tunable boot/
verdict-pull margins -- see ``_resolve_lifetime_s`` -- instead of being an
independent knob that could silently disagree with the on-node soak's own
wall. Ordering invariant: ``soak_wall_s < monitor_stop < lifetime_s``.

Teardown defense-in-depth (mandate 4, $0 even on total Mac network loss)
-------------------------------------------------------------------------
  1. This script's own explicit ``get_compute_rest().delete_instance()`` sweep
     ACROSS THE FULL CANDIDATE ZONE CHAIN (``_candidate_teardown_zones`` --
     mirrors ``zone_fallback_chain``) on clean finish (fastest). Live-fire fix:
     scatter-gather can land the winner in ANY zone in the chain, so this no
     longer trusts a single (possibly stale/wrong) resolved zone.
  2. An up-front-registered atexit/SIGINT/SIGTERM reaper (drains
     ``_ACTIVE_IGNITIONS``, itself now an all-zone sweep for the SAME reason)
     -- bypassed only by SIGKILL.
  3. A post-sweep ``list_instances_by_label`` $0-verify: if the node is STILL
     found (a zone outside the matrix, or an outright REST failure), re-delete
     it at the zone the verify list actually discovered.
  4. The node-side absolute self-destruct (Piece D) -- survives Mac SIGKILL /
     total network partition.
  5. ``scripts/gcp_cleanup.py`` (Piece A, already extended to match
     ``jarvis-role=brain``) -- Mac-side sweep once network returns.
  6. The existing idle-shutdown timer (clean-idle backstop).

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
import base64
import importlib.util
import json
import os
import re
import shlex
import signal
import sys
import tarfile
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
# The organism's deps live in the golden-image venv (PEP-668; the same
# interpreter jarvis-brain.service uses), NOT system python3 -- bare `python3`
# on the node has none of the org deps and dies with ModuleNotFoundError before
# a session is ever created (live-fire bt a1-brain-20260705-173210: soak exited
# RC=1, session_dir '<not discovered>'). Env-tunable.
_REMOTE_PYTHON = os.environ.get(
    "JARVIS_A1_BRAIN_PYTHON", "/opt/trinity/venv/bin/python3")

# Piece C / B config baked into the remote command (facts-file authoritative values).
_DEFAULT_CHAOS_TARGET_DIRS = "backend/core/ouroboros/a1_ignition_vector"
_DEFAULT_OUTBOUND_TOPICS = "actuation.*,telemetry.posture.*,autonomy.*"

# The node-side Black Box bundler's default staging dir (matches a1_black_box.py /
# a1_live_fire_chaos_harness.py's JARVIS_A1_BLACKBOX_NODE_OUT default).
_BLACK_BOX_NODE_OUT = os.environ.get("JARVIS_A1_BLACKBOX_NODE_OUT", "/tmp/a1_black_box")

# The stable in-archive arcname a1_black_box.py bundles a1_verdict.json at.
# Mandate 3: the AUTHORITATIVE verdict retrieval path parses this member out
# of the checksum-verified pulled tarball -- MUST match
# a1_black_box.py::_A1_VERDICT_ARCNAME.
_A1_VERDICT_ARCNAME = "a1_verdict.json"

# Fail-soft SSH-fallback gate: the AUTHORITATIVE path is IapBlackBoxTransport
# (checksum-gated bundle+pull) ONLY. When that channel produces no verdict at
# all (bundle/pull/extract all failed), a direct SSH find+cat is used as a
# clearly-logged, NON-AUTHORITATIVE fallback so a Black-Box bundling failure
# doesn't lose all verdict visibility. Default-on (fail-soft over silence);
# set to false to make IapBlackBoxTransport the ONLY verdict source, full stop.
_ENV_VERDICT_SSH_FALLBACK_ENABLED = "JARVIS_A1_BRAIN_VERDICT_SSH_FALLBACK_ENABLED"


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
# Coordinated soak-wall <-> node-lifetime budget (review fix, same class as
# the "wall_deadline single-source-of-truth" lesson).
#
# Bug: the node self-destruct (Piece D, `sleep lifetime_s; shutdown -h now`)
# starts at BOOT, but nothing told the on-node soak what ITS wall was -- it
# fell back to isomorphic_a1_local.py's own legacy default, which could
# disagree with `lifetime_s`. A long/graduation run could get $0-killed
# mid-verdict, or a short `lifetime_s` could leave almost no time for
# `retrieve_verdict()` after the soak's own wall fired.
#
# Fix: ONE coordinated budget, derived (not independent):
#   lifetime_s = soak_wall_s + boot_offset_s + verdict_pull_margin_s
# and the on-node soak's OWN wall (OUROBOROS_BATTLE_MAX_WALL_SECONDS) is set
# to the SAME soak_wall_s baked into the remote command, so the node and this
# driver agree. Ordering invariant (documented + asserted in tests):
#   soak_wall_s < monitor_stop (monitor_max_s) < lifetime_s
# i.e. the soak's own hard wall fires first; this driver's monitor loop stops
# well before the node's self-destruct so retrieve_verdict() has real
# headroom (>= verdict_pull_margin_s); Piece D's dead-man is the final,
# unconditional backstop if everything else wedges.
# ---------------------------------------------------------------------------
_ENV_SOAK_WALL_S = "JARVIS_A1_BRAIN_SOAK_WALL_S"
_DEFAULT_SOAK_WALL_S = 1800  # matches the CLAUDE.md A1 happy-path (~850-1300s)
_ENV_BOOT_OFFSET_S = "JARVIS_A1_BRAIN_BOOT_OFFSET_S"
_DEFAULT_BOOT_OFFSET_S = 180  # observed ~168s cold boot, rounded up w/ margin
_ENV_VERDICT_PULL_MARGIN_S = "JARVIS_A1_BRAIN_VERDICT_PULL_MARGIN_S"
_DEFAULT_VERDICT_PULL_MARGIN_S = 180  # headroom for the checksum-gated pull


def _resolve_soak_wall_s(explicit: Optional[int]) -> int:
    if explicit is not None:
        return int(explicit)
    return _env_int(_ENV_SOAK_WALL_S, _DEFAULT_SOAK_WALL_S)


def _resolve_lifetime_s(
    *, explicit: Optional[int], soak_wall_s: int, boot_offset_s: int,
    verdict_pull_margin_s: int,
) -> int:
    """DERIVE the node's absolute lifetime from the coordinated soak wall
    (+ margins) so the two budgets can never drift apart. `explicit` (an
    operator-supplied `--lifetime-s` or `JARVIS_A1_BRAIN_LIFETIME_S`) is
    still honored as an escape hatch -- but the default path is always
    derived, never independent.

    Re-review fix (safety-critical): an explicit lifetime BELOW the
    coordination floor (`soak_wall_s + boot_offset_s +
    verdict_pull_margin_s`) used to be honored VERBATIM -- e.g.
    `--soak-wall-s 2400 --lifetime-s 1800` armed the node's Piece D
    self-destruct at 1800s while the on-node soak's own wall
    (`OUROBOROS_BATTLE_MAX_WALL_SECONDS=soak_wall_s`) didn't fire until
    2400s, so the node could be $0-killed BEFORE the soak's own wall even
    fires, mid-verdict. An explicit-but-too-short lifetime is now CLAMPED
    UP to the floor -- never silently honored (that hides the danger),
    never rejected/crashed (the run should still proceed safely) -- with a
    LOUD warning so the operator knows their value was overridden. This
    keeps the ordering invariant `soak_wall_s < monitor_stop < lifetime_s`
    true after resolution in every case. An explicit lifetime >= the floor
    is honored as-is (an operator may deliberately extend it further)."""
    floor = int(soak_wall_s) + int(boot_offset_s) + int(verdict_pull_margin_s)

    def _clamp_to_floor(value: int, *, source: str) -> int:
        if value >= floor:
            return value
        _log(
            "WARN %s=%ds is BELOW the coordination floor=%ds "
            "(soak_wall_s=%ds + boot_offset_s=%ds + verdict_pull_margin_s=%ds) "
            "-- honoring it verbatim would let the node self-destruct BEFORE "
            "the soak's own wall fires, killing the run mid-verdict. "
            "CLAMPING lifetime_s UP to %ds to keep "
            "soak_wall_s < monitor_stop < lifetime_s. Pass --lifetime-s >= "
            "%ds (or a smaller --soak-wall-s) to silence this."
            % (source, value, floor, soak_wall_s, boot_offset_s,
               verdict_pull_margin_s, floor, floor)
        )
        return floor

    if explicit is not None:
        return _clamp_to_floor(int(explicit), source="--lifetime-s")
    env_raw = (os.environ.get("JARVIS_A1_BRAIN_LIFETIME_S", "") or "").strip()
    if env_raw:
        try:
            return _clamp_to_floor(
                int(float(env_raw)), source="JARVIS_A1_BRAIN_LIFETIME_S"
            )
        except (TypeError, ValueError):
            pass
    return floor


# ---------------------------------------------------------------------------
# Remote command composition (Piece B config baked in; Piece C dispatch).
# ---------------------------------------------------------------------------
def _compose_remote_command(
    *, remote_run_dir: str, seed: int, verbose: bool, soak_wall_s: int,
) -> str:
    """The actual isomorphic_a1_local.py invocation that runs ON the node.
    Co-located (parent driver's SSH session + the soak child it launches share
    the same host) so wall_deadline.json IPC stays same-host -- unchanged from
    the local isomorphic-fidelity design.

    `OUROBOROS_BATTLE_MAX_WALL_SECONDS=soak_wall_s` is the coordinated soak
    wall (review fix): the SAME value this driver derives `lifetime_s` from,
    so the node-side soak's hard wall and the node-side self-destruct
    (Piece D) never disagree."""
    verbose_flag = " --verbose" if verbose else ""
    return (
        "cd %s && "
        "JARVIS_CHAOS_TARGET_DIRS=%s "
        "JARVIS_BRAIN_OUTBOUND_TOPICS=%s "
        "OUROBOROS_BATTLE_HEADLESS=1 "
        "OUROBOROS_BATTLE_MAX_WALL_SECONDS=%d "
        "%s scripts/isomorphic_a1_local.py --mode process --run-root %s "
        "--seed %d%s"
        % (
            _REMOTE_REPO_ROOT,
            _DEFAULT_CHAOS_TARGET_DIRS,
            _DEFAULT_OUTBOUND_TOPICS,
            int(soak_wall_s),
            _REMOTE_PYTHON,
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


def _compose_verify_head_command() -> str:
    """The remote HEAD-verification script. `/opt/trinity/jarvis` is ROOT-owned
    (golden-image bake; the boot-time git-pull runs as root), so the SSH
    OS-Login user hits `fatal: detected dubious ownership` on a bare
    `git rev-parse` -- the `safe.directory` exception is required BEFORE the
    rev-parse, and (since we run the whole thing root-wrapped via
    `_wrap_as_root`) both lines execute as root, matching the tree's owner."""
    quoted_root = shlex.quote(_REMOTE_REPO_ROOT)
    return (
        "git config --global --add safe.directory %s >/dev/null 2>&1; "
        "git -C %s rev-parse HEAD" % (quoted_root, quoted_root)
    )


# ---------------------------------------------------------------------------
# Bug 3 (live-fire): root-wrapped remote exec. `/opt/trinity/jarvis` is
# ROOT-owned (golden-image bake) -- the SSH OS-Login user can neither
# `git rev-parse` (dubious ownership) nor `mkdir`/write under it directly. GCE
# OS Login admins get passwordless sudo, so every node-side command that
# touches that tree runs as root via a SINGLE base64 | sudo bash pipe instead
# of nesting `sudo bash -c '...'` around already-quoted payloads (the nested
# quoting is what makes the complex nohup dispatch wrapper unmanageable) --
# one clean layer of quoting, the whole script runs as root (so any
# background/disowned child it forks inherits root too, which is what lets
# the detached soak keep writing under the root-owned tree after the SSH
# session that dispatched it closes).
# ---------------------------------------------------------------------------
def _wrap_as_root(remote_cmd: str) -> str:
    """Root-wrap `remote_cmd` for a single clean layer of quoting: base64-encode
    the whole script locally, and have the remote shell decode + execute it
    under `sudo bash`. NEVER raises (base64-encoding a str is infallible)."""
    encoded = base64.b64encode(remote_cmd.encode("utf-8")).decode("ascii")
    return "printf %%s %s | base64 -d | sudo bash" % (shlex.quote(encoded),)


# ---------------------------------------------------------------------------
# Bug 1 (live-fire, MONEY-CRITICAL): the full cross-region candidate zone
# chain teardown must sweep. Scatter-gather provisioning can land the WINNER
# in any zone in this chain (`gcp_compute_rest.create_instance` races
# `zone_fallback_chain(...)` waves) -- a leak happens when teardown only
# tries the ONE zone a (possibly-wrong) resolution picked. Mirrors
# ignite_brain_vm.py's `_reap_brain_resources_async` reap-across-chain
# pattern: EVERY exit path sweeps EVERY candidate zone, unconditionally.
# ---------------------------------------------------------------------------
def _candidate_teardown_zones(preferred: Optional[str] = None) -> List[str]:
    """The full ordered zone chain teardown must attempt `delete_instance` in.
    `preferred` (the pre-create/operator zone) leads the chain but the WHOLE
    matrix (all regions) is always included -- so a scatter-gather winner
    landing in ANY candidate zone still gets swept. Falls back to
    `GCP_ZONE` env (the same preferred `create_instance` itself races from)
    when `preferred` is not given. Fail-soft: any import/lookup error
    degrades to a single-zone list (never raises, never blocks teardown)."""
    pref = (preferred or os.environ.get("GCP_ZONE", "") or "").strip() or None
    try:
        from backend.core.ouroboros.governance.zone_fallback import (  # noqa: PLC0415
            zone_fallback_chain,
        )

        chain = zone_fallback_chain(pref)
        if chain:
            return chain
    except Exception as exc:  # noqa: BLE001 -- must never block teardown
        _log("WARN zone_fallback_chain unavailable (%r) -- degrading to a "
             "single-zone teardown sweep" % (exc,))
    return [pref] if pref else [None]


# ---------------------------------------------------------------------------
# Bug 2 (live-fire): resolve_actual_zone must pick the RUNNING winner, not a
# being-deleted loser. Under scatter-gather, multiple same-named instances
# can briefly coexist (winner RUNNING in one zone, losers STOPPING/
# TERMINATED elsewhere mid-reap) -- a naive first-match pick can grab a loser.
# ---------------------------------------------------------------------------
_ZONE_STATUS_RANK: Dict[str, int] = {
    "RUNNING": 0,
    "PROVISIONING": 1,
    "STAGING": 1,
    "REPAIRING": 2,
    "SUSPENDING": 3,
    "SUSPENDED": 3,
    "STOPPING": 4,
    "TERMINATED": 5,
}


def _status_rank(status: Optional[str]) -> int:
    """Lower is better (more likely the live winner). Unknown/missing status
    ranks in the middle -- neither preferred over a confirmed RUNNING match
    nor penalized below a confirmed STOPPING/TERMINATED one. NEVER raises."""
    return _ZONE_STATUS_RANK.get(str(status or "").strip().upper(), 2)


# Parses the AUTHORITATIVE winner zone out of provision_brain's detail string.
# gcp_compute_rest.create_instance's scatter-gather winner reports
# "created:zone=<z>:mode=<m>" (see GCPComputeRest._insert_in_zone) -- this is
# the real create-time zone, straight from the REST call that materialized
# the node, and is preferred over re-listing (which can observe a transient
# multi-instance race).
_PROVISION_ZONE_RE = re.compile(r"zone=([a-zA-Z0-9-]+)")


def _parse_provision_zone(detail: str) -> Optional[str]:
    """Parse the winner zone out of provision()'s `(ok, detail)` detail
    string. Fail-soft -- returns None on any non-match, NEVER raises."""
    if not detail:
        return None
    m = _PROVISION_ZONE_RE.search(str(detail))
    return m.group(1) if m else None


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
    *, node: str, zones: Sequence[Optional[str]], compute_rest_factory: Callable[[], Any]
) -> None:
    """Register a Brain node for teardown UP FRONT (before/at create) so a
    crash mid-provision still reaps it. `zones` is the FULL candidate zone
    chain (Bug 1) -- NOT a single (possibly-wrong) resolved zone -- so the
    reaper always sweeps every zone the scatter-gather winner could have
    landed in, regardless of which zone later resolution picks."""
    _ACTIVE_IGNITIONS.append(
        {
            "node": node,
            "zones": list(zones) if zones else [None],
            "confirmed_zone": None,  # informational only; never gates teardown
            "compute_rest_factory": compute_rest_factory,
        }
    )


async def _reap_ignitions_async() -> None:
    """Drain the registry: for every registered node, attempt
    `delete_instance` across EVERY zone in its candidate chain (Bug 1) --
    unconditionally, not stopping at the first success, since idempotent
    404-is-success semantics mean sweeping the whole chain is cheap and
    guarantees the node is gone wherever it actually landed. Idempotent (the
    registry is cleared before the loop runs, so re-entrant/second calls are a
    no-op) + fail-soft (NEVER raises -- teardown must always complete)."""
    if not _ACTIVE_IGNITIONS:
        return
    entries = list(_ACTIVE_IGNITIONS)
    _ACTIVE_IGNITIONS.clear()
    for entry in entries:
        node = entry.get("node")
        zones = entry.get("zones") or [entry.get("zone")]  # tolerate legacy shape
        factory = entry.get("compute_rest_factory") or _default_compute_rest_factory
        if not node:
            continue
        try:
            rest = factory()
        except Exception as exc:  # noqa: BLE001 -- reap must NEVER raise
            _log("[Reap] factory error node=%s: %r" % (node, exc))
            continue
        for zone in zones:
            try:
                ok, detail = await rest.delete_instance(node, zone=zone)
                _log("[Reap] delete_instance node=%s zone=%s -> ok=%s detail=%s"
                     % (node, zone, ok, detail))
            except Exception as exc:  # noqa: BLE001 -- reap must NEVER raise
                _log("[Reap] warning node=%s zone=%s: %r" % (node, zone, exc))


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
    soak_wall_s: int
    seed: int
    remote_run_dir: str
    remote_cmd: str
    dispatch_wrapper_cmd: str
    dispatch_wrapper_root_cmd: str = ""
    verify_head_cmd: str = ""
    teardown_zones: List[str] = field(default_factory=list)
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
        soak_wall_s: Optional[int] = None,
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
        # Bug 2: the AUTHORITATIVE winner zone parsed from provision()'s
        # "created:zone=<z>:mode=<m>" detail string -- set by provision(),
        # preferred by resolve_actual_zone() over re-listing (avoids a
        # scatter-gather race where a same-named LOSER is briefly visible).
        self._provision_zone: Optional[str] = None
        # Bug 1: the full candidate zone chain teardown sweeps -- set by
        # provision() (up front, before create); computed fresh from
        # self.zone if teardown() is ever reached without provision() having
        # run (e.g. a direct test call).
        self._teardown_zones: List[str] = []

        # Coordinated soak-wall <-> node-lifetime budget (review fix): the
        # on-node soak wall and the node's absolute self-destruct lifetime
        # are derived from ONE source instead of drifting independently.
        # Ordering invariant: soak_wall_s < monitor_stop < lifetime_s.
        self.boot_offset_s = int(_env_int(_ENV_BOOT_OFFSET_S, _DEFAULT_BOOT_OFFSET_S))
        self.verdict_pull_margin_s = int(
            _env_int(_ENV_VERDICT_PULL_MARGIN_S, _DEFAULT_VERDICT_PULL_MARGIN_S))
        self.soak_wall_s = _resolve_soak_wall_s(soak_wall_s)
        self.lifetime_s = _resolve_lifetime_s(
            explicit=lifetime_s,
            soak_wall_s=self.soak_wall_s,
            boot_offset_s=self.boot_offset_s,
            verdict_pull_margin_s=self.verdict_pull_margin_s,
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
        # Default leaves >= verdict_pull_margin_s of headroom before
        # lifetime_s (the node's self-destruct) so retrieve_verdict() -- the
        # checksum-gated Black Box pull + a1_verdict.json fetch -- runs
        # BEFORE the node halts, not after.
        self._monitor_max_s = float(
            monitor_max_s if monitor_max_s is not None
            else _env_float("JARVIS_A1_BRAIN_MONITOR_MAX_S",
                             max(60.0, self.lifetime_s - self.verdict_pull_margin_s)))

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

    async def _await_ssh_ready(self) -> bool:
        """Poll SSH-over-IAP until the freshly-provisioned node ANSWERS, before
        verify_head/dispatch. A new node needs ~30-90s to reach RUNNING + boot
        sshd + establish the IAP tunnel; the pre-readiness-gate driver SSHed
        immediately after provision and got rc=255 (ssh exit 255 = tunnel/host
        not ready). Bounded + backoff, off-loop, fail-soft. Budget env
        ``JARVIS_A1_BRAIN_SSH_READY_BUDGET_S`` (default 300, floor 60). Returns
        True once a trivial `echo` round-trips; False if the budget elapses."""
        budget_s = max(60.0, _env_float("JARVIS_A1_BRAIN_SSH_READY_BUDGET_S", 300.0))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget_s
        attempt = 0
        delay = 5.0
        while loop.time() < deadline:
            attempt += 1
            rc, out = await self._ssh_exec_async(
                "echo A1_SSH_READY", timeout_s=30.0)
            if rc == 0 and "A1_SSH_READY" in (out or ""):
                _log("[IgniteA1Brain] SSH-over-IAP ready after %d attempt(s)"
                     % (attempt,))
                return True
            _log("[IgniteA1Brain] SSH not ready yet (attempt %d rc=%d) -- "
                 "retry in %.0fs" % (attempt, rc, delay))
            await asyncio.sleep(delay)
            delay = min(30.0, delay * 1.5)
        _log("[IgniteA1Brain] WARN SSH-over-IAP not ready within %.0fs budget"
             % (budget_s,))
        return False

    # -- plan (pure, dry-run-safe) --------------------------------------------

    def build_plan(self) -> IgnitionPlan:
        project = self.project or _resolve_project_for_plan()
        zone = self.zone or _resolve_zone_for_plan()
        remote_cmd = _compose_remote_command(
            remote_run_dir=self.remote_run_dir, seed=self.seed, verbose=self.verbose,
            soak_wall_s=self.soak_wall_s)
        dispatch_wrapper = _compose_dispatch_wrapper(remote_cmd, self.remote_run_dir)
        # Bug 3: the command actually sent over SSH is root-wrapped (single
        # base64 | sudo bash pipe) -- /opt/trinity/jarvis is root-owned.
        dispatch_wrapper_root = _wrap_as_root(dispatch_wrapper)
        verify_head_cmd = _wrap_as_root(_compose_verify_head_command())
        ns = argparse.Namespace(project=project, zone=zone)

        ssh_argv: List[str] = []
        teardown_argv: List[str] = []
        try:
            hyper = self._hyper()
            ssh_argv = hyper._ssh_cmd(ns, self.node_name, dispatch_wrapper_root)
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
        # Bug 1: the FULL candidate zone chain teardown will sweep -- shown so
        # --dry-run makes the $0 guarantee legible (not just the pre-create
        # zone, which scatter-gather may not even land in).
        teardown_zones = _candidate_teardown_zones(zone or None)
        return IgnitionPlan(
            node_name=self.node_name,
            project=project,
            zone=zone,
            lifetime_s=self.lifetime_s,
            soak_wall_s=self.soak_wall_s,
            seed=self.seed,
            remote_run_dir=self.remote_run_dir,
            remote_cmd=remote_cmd,
            dispatch_wrapper_cmd=dispatch_wrapper,
            dispatch_wrapper_root_cmd=dispatch_wrapper_root,
            verify_head_cmd=verify_head_cmd,
            teardown_zones=teardown_zones,
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
        print("  zone (pre-create) : %s  (actual zone resolved post-create -- "
              "PREFERS the provision-reported winner zone, falls back to a "
              "RUNNING-preferring list_instances_by_label match)" % plan.zone)
        print("  soak_wall_s       : %d  (-> OUROBOROS_BATTLE_MAX_WALL_SECONDS, "
              "on-node soak hard wall)" % plan.soak_wall_s)
        print("  lifetime_s        : %d  (-> JARVIS_BRAIN_ABSOLUTE_LIFETIME_S, "
              "Piece D self-destruct; DERIVED = soak_wall_s + boot_offset_s + "
              "verdict_pull_margin_s -- ordering invariant: soak_wall_s < "
              "monitor_stop < lifetime_s)" % plan.lifetime_s)
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
        print("VERIFY-HEAD COMMAND (root-wrapped: safe.directory + git rev-parse AS ROOT):")
        print("  " + plan.verify_head_cmd)
        print("-" * 78)
        print("DISPATCH WRAPPER, raw (detached nohup; monitor() polls the done-file):")
        print("  " + plan.dispatch_wrapper_cmd)
        print("DISPATCH WRAPPER, root-wrapped (what is ACTUALLY sent over SSH --"
              " /opt/trinity/jarvis is root-owned):")
        print("  " + plan.dispatch_wrapper_root_cmd)
        print("-" * 78)
        print("SSH ARGV (reused sovereign_iac_hypervisor._ssh_cmd; --command carries "
              "the root-wrapped dispatch wrapper above):")
        print("  " + _argv(plan.ssh_argv))
        print("-" * 78)
        print("BLACK-BOX PULL ARGV preview (reused IapBlackBoxTransport._scp_pull_cmd):")
        print("  " + _argv(plan.pull_argv))
        print("-" * 78)
        print("TEARDOWN ARGV preview (reused sovereign_iac_hypervisor._delete_node_cmd):")
        print("  " + _argv(plan.teardown_argv))
        print("  (the REAL teardown executes get_compute_rest().delete_instance("
              "node, zone=...) directly -- this argv is informational only)")
        print("-" * 78)
        print("TEARDOWN ALL-ZONE SWEEP (Bug 1 fix -- every exit path, incl. the "
              "SIGINT/SIGTERM reaper, deletes the node NAME across EVERY zone "
              "below, idempotent + fail-soft, so the $0 guarantee does not "
              "depend on which zone got resolved):")
        print(("  " + ", ".join(plan.teardown_zones)) if plan.teardown_zones else "  <none>")
        print("=" * 78)
        print("DRY RUN: zero network / subprocess side effects. Exiting 0.")

    # -- phase 2: provision (HOLD) ---------------------------------------------

    async def provision(self) -> Tuple[bool, str]:
        _install_signal_handlers()
        # Bug 1: register UP FRONT (before create) with the FULL candidate
        # zone chain -- NOT a single (possibly pre-create, possibly wrong)
        # zone -- so a crash mid-provision (or a later SIGINT/SIGTERM) still
        # reaps the node wherever the scatter-gather winner actually landed.
        self._teardown_zones = _candidate_teardown_zones(self.zone or None)
        _register_ignition(
            node=self.node_name, zones=self._teardown_zones,
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

        # Bug 2: capture the AUTHORITATIVE winner zone straight out of the
        # create-time detail string ("created:zone=<z>:mode=<m>") -- this is
        # the real create-time zone, preferred by resolve_actual_zone() over
        # a post-hoc list_instances_by_label query that can observe a
        # transient scatter-gather multi-instance race.
        self._provision_zone = _parse_provision_zone(detail) if ok else None

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

    def _mark_confirmed_zone(self, zone: str) -> None:
        """Record the confirmed SSH-target zone for logging/introspection
        ONLY -- teardown no longer depends on this being right (Bug 1 sweeps
        the whole candidate chain unconditionally); this just tells the
        reaper log which zone to expect the node in."""
        self.zone = zone
        for entry in _ACTIVE_IGNITIONS:
            if entry.get("node") == self.node_name:
                entry["confirmed_zone"] = zone

    async def resolve_actual_zone(self) -> Optional[str]:
        """Bug 2: prefer the AUTHORITATIVE zone provision() parsed straight
        out of the create-time detail string over re-listing (the real
        create-time zone the REST call materialized the node in). Only when
        that isn't available does this fall back to list_instances_by_label
        -- and even then, prefer a RUNNING/PROVISIONING/STAGING match over a
        STOPPING/TERMINATED one, since scatter-gather can leave a same-named
        LOSER briefly visible mid-reap in a different zone (the exact live-fire
        bug: a STOPPING loser in zone A got picked over the RUNNING winner in
        zone B). This zone feeds the SSH target (verify_head/dispatch/
        monitor) -- teardown's $0 guarantee no longer depends on it (Bug 1)."""
        if self._provision_zone:
            self._mark_confirmed_zone(self._provision_zone)
            _log("resolved actual zone=%s for node=%s (source=provision_detail)"
                 % (self._provision_zone, self.node_name))
            return self._provision_zone

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

        matches = [inst for inst in (instances or []) if inst.get("name") == self.node_name]
        if not matches:
            _log("WARN could not find node=%s via list_instances_by_label -- zone "
                 "remains unresolved" % (self.node_name,))
            return None

        # RUNNING-preferring pick: sort is STABLE, so ties keep the original
        # (API) order -- only a strictly better status jumps the queue.
        best = sorted(matches, key=lambda inst: _status_rank(inst.get("status")))[0]
        raw_zone = str(best.get("zone") or "")
        zone = raw_zone.rsplit("/", 1)[-1] if raw_zone else None
        if zone:
            self._mark_confirmed_zone(zone)
            _log("resolved actual zone=%s status=%s for node=%s "
                 "(source=list_running_preferred, %d match(es))"
                 % (zone, best.get("status"), self.node_name, len(matches)))
            return zone
        _log("WARN matched instance(s) for node=%s but zone field was empty"
             % (self.node_name,))
        return None

    # -- phase 3: verify HEAD ---------------------------------------------------

    async def verify_head(self) -> Optional[str]:
        # Bug 3: /opt/trinity/jarvis is ROOT-owned (golden-image bake; the
        # boot-time git-pull runs as root) -- a bare `git rev-parse` as the
        # SSH OS-Login user fails with "dubious ownership". Root-wrap the
        # whole thing (safe.directory exception + rev-parse) via a single
        # base64 | sudo bash pipe.
        remote = _wrap_as_root(_compose_verify_head_command())
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
            remote_run_dir=self.remote_run_dir, seed=self.seed, verbose=self.verbose,
            soak_wall_s=self.soak_wall_s)
        wrapper = _compose_dispatch_wrapper(remote, self.remote_run_dir)
        # Bug 3: the mkdir + nohup + isomorphic_a1_local.py invocation all
        # write under the root-owned /opt/trinity/jarvis tree -- root-wrap the
        # WHOLE detached wrapper so the disowned child it forks inherits root
        # too (it keeps writing after this SSH session closes).
        root_wrapper = _wrap_as_root(wrapper)
        rc, out = await self._ssh_exec_async(
            root_wrapper, timeout_s=_env_float("JARVIS_A1_BRAIN_DISPATCH_TIMEOUT_S", 60.0))
        ok = rc == 0 and "DISPATCH_STARTED" in (out or "")
        _log("dispatch -> rc=%d ok=%s out=%r" % (rc, ok, (out or "")[-300:]))
        return ok

    # -- phase 5: monitor (bounded, fail-soft) -----------------------------------

    async def monitor(self) -> Dict[str, Any]:
        deadline = time.monotonic() + self._monitor_max_s
        tail_lines = _env_int("JARVIS_A1_BRAIN_MONITOR_TAIL_LINES", 40)
        last_tail = ""
        inner = (
            "if [ -f %s ]; then echo __IGNITE_DONE__; cat %s; else "
            "tail -n %d %s 2>/dev/null || true; fi"
            % (shlex.quote(self._done_file()), shlex.quote(self._done_file()),
               tail_lines, shlex.quote(self._log_file()))
        )
        # Bug 3: the done-file/log-file live under the root-owned run dir --
        # the SSH user may not have read access, so poll AS ROOT too.
        remote = _wrap_as_root(inner)
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
        # Bug 3: the verdict lives under the root-owned run dir -- `find` as
        # the plain SSH user can hit permission-denied on root-owned
        # subdirectories, so this runs AS ROOT too.
        inner = (
            "find %s -maxdepth 2 -name a1_verdict.json 2>/dev/null | sort | tail -n1"
            % (shlex.quote(self.remote_run_dir),)
        )
        remote = _wrap_as_root(inner)
        rc, out = self._ssh_exec(remote, timeout_s=30.0)
        if rc != 0 or not out:
            return None
        lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
        return lines[-1] if lines else None

    def _fetch_verdict_json(self, path: str) -> Optional[Dict[str, Any]]:
        """NON-AUTHORITATIVE fallback fetch: direct SSH ``cat`` (root-wrapped
        -- Bug 3, the verdict file is root-owned) of the verdict path
        discovered by ``_discover_verdict_path``. Only reached when the
        checksum-gated Black-Box channel produced no verdict at all."""
        remote = _wrap_as_root("cat %s" % (shlex.quote(path),))
        rc, out = self._ssh_exec(remote, timeout_s=30.0)
        if rc != 0 or not out:
            return None
        text = out.strip()
        start = text.find("{")
        if start == -1:
            return None
        try:
            return json.loads(text[start:])
        except Exception as exc:  # noqa: BLE001 -- malformed verdict must not crash
            _log("[VerdictFallback] WARN verdict JSON parse error: %r" % (exc,))
            return None

    def _extract_verdict_from_archive(self, archive_path: str) -> Optional[Dict[str, Any]]:
        """AUTHORITATIVE extraction (mandate 3): parse ``a1_verdict.json`` out
        of the LOCAL, checksum-verified Black-Box tarball. Fail-soft -- any
        error (missing member, bad tar, malformed JSON) returns None and is
        logged; it never raises (a corrupt/absent verdict member must not
        crash the driver -- the caller falls back to SSH if fully absent)."""
        try:
            with tarfile.open(archive_path, "r:*") as tf:
                try:
                    member = tf.getmember(_A1_VERDICT_ARCNAME)
                except KeyError:
                    _log("[BlackBox] a1_verdict.json NOT present in the pulled archive "
                         "(the node-side bundler noted it absent -- see the archive's "
                         "own MANIFEST.txt)")
                    return None
                fh = tf.extractfile(member)
                if fh is None:
                    return None
                text = fh.read().decode("utf-8", errors="ignore").strip()
        except Exception as exc:  # noqa: BLE001 -- archive extraction must never crash
            _log("[BlackBox] WARN archive extraction error: %r" % (exc,))
            return None
        start = text.find("{")
        if start == -1:
            return None
        try:
            return json.loads(text[start:])
        except Exception as exc:  # noqa: BLE001 -- malformed verdict must not crash
            _log("[BlackBox] WARN verdict JSON parse error: %r" % (exc,))
            return None

    async def retrieve_verdict(self) -> Dict[str, Any]:
        """Mandate 3: verdict retrieval is 100% via ``IapBlackBoxTransport``.

        AUTHORITATIVE path: ``bundle_on_node`` (node-side ``a1_black_box.py``
        now bundles ``a1_verdict.json`` INTO the archive) -> ``pull_archive``
        (checksum-gated scp pull, local sha256 recompute) -> on a CONFIRMED
        match, extract + parse ``a1_verdict.json`` straight out of the local
        tarball. No SSH ``find``/``cat`` is used on this path.

        FAIL-SOFT fallback: only when the Black-Box channel produces NO
        verdict at all (bundle failed, pull/checksum failed, or the archive
        didn't contain the member), a direct SSH ``find``+``cat`` is used --
        clearly logged as non-authoritative -- gated by
        ``JARVIS_A1_BRAIN_VERDICT_SSH_FALLBACK_ENABLED`` (default true)."""
        loop = asyncio.get_running_loop()
        verdict: Optional[Dict[str, Any]] = None
        verdict_source = "none"
        verdict_path: Optional[str] = None

        # -- AUTHORITATIVE: checksum-gated Black Box bundle + pull, then
        # extract a1_verdict.json from the LOCAL verified tarball. --
        blackbox: Dict[str, Any] = {"pulled": False}
        local_archive: Optional[str] = None
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
                    local_archive = os.path.join(
                        local_dir, os.path.basename(bundle["archive"]))
                    blackbox = {
                        "pulled": True, "local_dir": local_dir, "sha256": local_sha,
                        "archive": local_archive,
                    }
                    _log("[BlackBox] confirmed sha256=%s -> %s" % (local_sha[:16], local_dir))
                else:
                    _log("[BlackBox] WARN pull/checksum unresolved (node_sha=%s "
                         "local_sha=%s) -- diagnostic bundle NOT confirmed"
                         % (bundle.get("sha256"), local_sha))
            else:
                _log("[BlackBox] WARN node-side bundle failed -- no diagnostic archive")
        except Exception as exc:  # noqa: BLE001 -- bundling/pull must never crash the driver
            _log("[BlackBox] WARN bundling/pull error: %r" % (exc,))

        if local_archive:
            verdict = await loop.run_in_executor(
                None, self._extract_verdict_from_archive, local_archive)
            if verdict is not None:
                verdict_source = "blackbox"
                verdict_path = "%s!%s" % (local_archive, _A1_VERDICT_ARCNAME)
                _log("[BlackBox] A1 verdict parsed from the checksum-verified "
                     "archive (AUTHORITATIVE)")
            else:
                _log("[BlackBox] WARN checksum-verified archive did not yield a "
                     "parseable a1_verdict.json")

        # -- FAIL-SOFT, NON-AUTHORITATIVE fallback: only when the Black-Box
        # channel produced NO verdict at all. --
        if verdict is None:
            if _truthy(os.environ.get(_ENV_VERDICT_SSH_FALLBACK_ENABLED, "true")):
                _log("[VerdictFallback] Black-Box channel produced no verdict -- "
                     "falling back to a direct SSH find+cat. This is a "
                     "NON-AUTHORITATIVE fallback (Black-Box channel unavailable); "
                     "the mandate-3 authoritative path is IapBlackBoxTransport-only.")
                verdict_path = await loop.run_in_executor(None, self._discover_verdict_path)
                if verdict_path:
                    verdict = await loop.run_in_executor(
                        None, self._fetch_verdict_json, verdict_path)
                    if verdict is not None:
                        verdict_source = "ssh_fallback"
                else:
                    _log("[VerdictFallback] WARN could not discover a1_verdict.json "
                         "under %s" % (self.remote_run_dir,))
            else:
                _log("[VerdictFallback] SSH fallback disabled (%s=false) -- verdict "
                     "remains UNAVAILABLE rather than falling back to a "
                     "non-authoritative source" % (_ENV_VERDICT_SSH_FALLBACK_ENABLED,))

        if verdict is not None:
            criteria = verdict.get("criteria", {}) or {}
            _log("A1 VERDICT proven=%s (verdict_source=%s)"
                 % (verdict.get("proven"), verdict_source))
            for name, ok in criteria.items():
                _log("  criterion %-32s %s" % (name, "PASS" if ok else "FAIL"))
        else:
            _log("A1 VERDICT: UNAVAILABLE (verdict_source=%s)" % (verdict_source,))
            # Remote-failure visibility: the node-side dispatch log carries the
            # soak's stdout/stderr (e.g. an early ModuleNotFoundError before any
            # session exists). SSH is root-wrapped, so it can read the
            # root-owned log. Bounded tail, fail-soft -- essential for
            # diagnosing a remote soak that died before producing a verdict.
            try:
                rc, tail = await self._ssh_exec_async(
                    "tail -n 60 %s 2>/dev/null || true"
                    % (shlex.quote(self._log_file()),),
                    timeout_s=30.0)
                if tail and tail.strip():
                    _log("[IgniteA1Brain] --- node ignite_dispatch.log (tail) ---")
                    for ln in tail.strip().splitlines()[-60:]:
                        _log("[node] %s" % (ln,))
                    _log("[IgniteA1Brain] --- end dispatch log ---")
            except Exception as exc:  # noqa: BLE001 -- diagnostic, never fatal
                _log("[IgniteA1Brain] WARN could not pull dispatch log: %r" % (exc,))

        return {
            "verdict": verdict,
            "verdict_path": verdict_path,
            "verdict_source": verdict_source,
            "blackbox": blackbox,
        }

    # -- phase 7: teardown ($0) ---------------------------------------------------

    async def teardown(self) -> Dict[str, Any]:
        """Defense-in-depth layer 1: an explicit, zone-agnostic sweep. Bug 1
        (MONEY-CRITICAL, live-fire): scatter-gather can land the winner in ANY
        zone in the candidate chain, and whichever zone got resolved for SSH
        purposes (``self.zone``) is NOT trusted here -- this deletes the node
        NAME across EVERY zone in the chain, unconditionally, idempotent
        (404/not-found counts as success at the REST layer) + fail-soft (a
        single zone's error never aborts the sweep)."""
        result: Dict[str, Any] = {"deleted": False, "detail": "", "zero_cost": False}
        zones = self._teardown_zones or _candidate_teardown_zones(self.zone or None)
        try:
            rest = self._compute_rest_factory()
            any_ok = False
            details: List[str] = []
            for zone in zones:
                try:
                    ok, detail = await rest.delete_instance(self.node_name, zone=zone)
                except Exception as exc:  # noqa: BLE001 -- one zone's error never aborts the sweep
                    ok, detail = False, "EXC:%r" % (exc,)
                    _log("[Teardown] WARN delete_instance error zone=%s (continuing "
                         "sweep): %r" % (zone, exc))
                any_ok = any_ok or ok
                details.append("zone=%s:ok=%s:%s" % (zone, ok, detail))
                _log("[Teardown] delete_instance node=%s zone=%s -> ok=%s detail=%s"
                     % (self.node_name, zone, ok, detail))
            result["deleted"] = any_ok
            result["detail"] = "; ".join(details)
        except Exception as exc:  # noqa: BLE001 -- teardown must never raise
            _log("[Teardown] WARN all-zone sweep error (continuing to reaper): %r"
                 % (exc,))

        # Defense-in-depth layer 2: drain the up-front reaper registry (itself
        # now an all-zone sweep, Bug 1 -- see _reap_ignitions_async). Run off
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

            if leaked:
                # The explicit delete above targeted `self.zone`, which may be
                # STALE or never resolved (e.g. resolve_actual_zone() failed,
                # or the node otherwise moved zones) -- this all-zone
                # label-based list is authoritative post-facto. RE-ISSUE the
                # delete with the zone discovered HERE instead of just
                # logging LEAK. Fail-soft: a second miss still just logs LEAK
                # (gcp_cleanup.py's sweep + Piece D remain the deeper
                # backstops).
                still_leaked = []
                for inst in leaked:
                    raw_zone = str(inst.get("zone") or "")
                    discovered_zone = raw_zone.rsplit("/", 1)[-1] if raw_zone else None
                    if not discovered_zone:
                        still_leaked.append(inst)
                        continue
                    try:
                        ok2, detail2 = await rest2.delete_instance(
                            self.node_name, zone=discovered_zone)
                        _log("[Teardown] zone-mismatch re-delete node=%s zone=%s "
                             "-> ok=%s detail=%s"
                             % (self.node_name, discovered_zone, ok2, detail2))
                        if not ok2:
                            still_leaked.append(inst)
                    except Exception as exc:  # noqa: BLE001 -- fail-soft retry
                        _log("[Teardown] WARN zone-mismatch re-delete error: %r" % (exc,))
                        still_leaked.append(inst)
                leaked = still_leaked

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
            if not await self._await_ssh_ready():
                _log("ABORT: node SSH-over-IAP not reachable within budget "
                     "(teardown will $0 the node)")
                return 1
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
    p.add_argument("--soak-wall-s", type=int, default=None,
                    help="On-node soak hard wall-clock cap in seconds, set as "
                         "OUROBOROS_BATTLE_MAX_WALL_SECONDS on the remote command "
                         "(default: env JARVIS_A1_BRAIN_SOAK_WALL_S or 1800). "
                         "--lifetime-s is DERIVED from this (+ boot/verdict-pull "
                         "margins) unless --lifetime-s/JARVIS_A1_BRAIN_LIFETIME_S "
                         "is set explicitly -- bumping this grows lifetime_s too, "
                         "so the two budgets never drift apart.")
    p.add_argument("--lifetime-s", type=int, default=None,
                    help="Absolute node lifetime in seconds (default: DERIVED as "
                         "soak_wall_s + boot_offset_s + verdict_pull_margin_s; "
                         "env JARVIS_A1_BRAIN_LIFETIME_S or an explicit value here "
                         "overrides the derivation). Arms the node-side "
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
        soak_wall_s=args.soak_wall_s,
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

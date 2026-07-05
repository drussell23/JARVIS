"""brain_lifecycle.py -- Stage-4 Task 1: the reusable Brain provisioning core
+ the hierarchical-ownership substrate (lineage labels + Resource Manifest).

Extraction
----------
The create-instance composition that lived inline in the Stage-1 CLI driver
(``scripts/ignite_brain_vm.py``) -- brain-env compose, TLS metadata delivery,
the runtime startup script, machine/image resolution -- now lives here as
``provision_brain``. The driver consumes this function (its injected-seam
behavior unchanged; the live default delegates). BYTE-EQUIVALENT: the driver's
existing regression suites (tests/infra/test_ignite_brain_vm.py,
test_brain_tls_delivery.py) pin the behavior unmodified; the driver re-exports
the helpers extracted here so its module surface stays back-compatible.

Hierarchical ownership (the Stage-4 foundation)
-----------------------------------------------
Live incident that motivates this: an orphaned ``jarvis-prime-failover`` node
(spawned by a Brain whose controller died) was found by AD-HOC listing. That
class must be structurally impossible: every cloud resource is OWNED AT BIRTH
(lineage labels stamped on create) and recorded in an append-only Resource
Manifest, so teardown is a MANIFEST WALK (children first), never a discovery
grep. The label-family query exists ONLY as a drift DETECTOR -- it is never
the deletion source (see ``teardown_family``).

Components
----------
* Label constants: ``LABEL_OWNER`` / ``LABEL_PARENT`` / ``LABEL_GEN`` +
  ``sanitize_label_value`` (GCP label charset: lowercase [a-z0-9_-], <=63).
* ``ResourceManifest``: append-only JSONL at ``JARVIS_RESOURCE_MANIFEST_PATH``
  (default ``<repo>/.jarvis/manifests/resource_manifest.jsonl``). Reuses the
  intake-WAL write primitive (flock cross-process append) -- imported, never
  copied. Tolerant replay (corrupt lines skipped, mirroring WAL semantics).
* ``teardown_family``: children-first manifest walk, fail-soft per item,
  ``record_delete`` on success, optional label-family drift detector.
* ``provision_brain``: the extracted Stage-1 Brain-VM create composition, with
  additive ``labels`` / ``extra_env`` seams for Stage-4 lineage stamping.

Env knobs (all resolved at call time -- zero baked assumptions):
    JARVIS_RESOURCE_MANIFEST_PATH   manifest JSONL path (default repo-local)
    JARVIS_KEEPER_ID                keeper identity stamped on create records
    JARVIS_BRAIN_MTLS_DIR           server TLS material dir (gen_brain_mtls.py)
    JARVIS_BRAIN_WS_* / JARVIS_BRAIN_VM_*  (resolved here + in gcp_compute_rest)
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _truthy(val: Optional[str]) -> bool:
    return str(val or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Lineage labels -- ownership stamped at BIRTH.
# ---------------------------------------------------------------------------

LABEL_OWNER = "jarvis-owner"
LABEL_PARENT = "jarvis-parent"
LABEL_GEN = "jarvis-brain-gen"

_LABEL_SAFE_RE = re.compile(r"[^a-z0-9_-]")
_LABEL_MAX_LEN = 63


def sanitize_label_value(value: Any) -> str:
    """Coerce an arbitrary value into a GCP-label-safe value: lowercase,
    charset [a-z0-9_-], max 63 chars. Unsafe codepoints become ``-``.
    NEVER raises."""
    try:
        raw = "" if value is None else str(value)
    except Exception:  # noqa: BLE001
        return ""
    safe = _LABEL_SAFE_RE.sub("-", raw.strip().lower())
    return safe[:_LABEL_MAX_LEN]


# ---------------------------------------------------------------------------
# TLS material delivery (extracted VERBATIM from scripts/ignite_brain_vm.py;
# the driver re-exports these names -- its test suites pin the behavior).
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Brain-env composition (extracted from BrainIgnitionDriver._brain_env_values;
# the driver method now delegates here -- byte-equivalent when extra_env=None).
# ---------------------------------------------------------------------------


def brain_env_values(extra_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Fold the Task-4 ignition values into the brain-env metadata: the WS
    port/path + TLS material references + the session cost cap, all
    env-resolved (Mandate 2). The idle-shutdown seconds are added by
    ``_compose_brain_env`` itself.

    TLS paths: when server material is shipped (TLS enabled), the node's
    brain.env is OVERRIDDEN to the node-local /etc/jarvis/tls/* paths the
    runtime startup script writes -- the Mac-side env holds the Mac's OWN
    (client) paths, which must never leak into the node's config.

    ``extra_env`` (Stage-4, additive): caller-supplied values folded LAST so a
    keeper can ship lineage env (e.g. JARVIS_BRAIN_PARENT_NODE /
    JARVIS_BRAIN_GENERATION) onto the node. ``extra_env=None`` is
    byte-identical to the pre-extraction composition."""
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
    # Opt-in provider-key fold (live-fire 2026-07-04 Stage-2 acceptance: the
    # soak crash-looped 54x with "No API keys set" because these were never
    # shipped). Keys travel as instance metadata (project-scoped
    # visibility) -- the established J-Prime pattern per the relocation
    # design's open risk #4. Opt-in so default ignitions never ship
    # secrets.
    if _truthy(os.environ.get("JARVIS_BRAIN_SHIP_PROVIDER_KEYS")):
        for key in ("DOUBLEWORD_API_KEY", "ANTHROPIC_API_KEY"):
            v = os.environ.get(key)
            if v is not None and v != "":
                vals[key] = v
    if extra_env:
        for k, v in extra_env.items():
            if v is not None and v != "":
                vals[str(k)] = str(v)
    return vals


# ---------------------------------------------------------------------------
# Runtime startup script (extracted VERBATIM from scripts/ignite_brain_vm.py:
# thin provisioning glue -- repopulate /etc/jarvis/brain.env from THIS
# instance's metadata, create the /run/jarvis runtime dir + liveness marker,
# and (re)start the baked brain unit so it picks up the fresh env).
# ---------------------------------------------------------------------------

# Stage-4 Mandate 4 ($0 even on total Mac network loss): an UNCONDITIONAL,
# node-side absolute self-destruct, independent of the idle-liveness marker,
# the brain process, and the control plane. Armed ONLY when this env is a
# positive int -- unset/blank/non-numeric/<=0 all disarm it (fail-soft,
# byte-identical-legacy startup script).
_ENV_ABSOLUTE_LIFETIME_S = "JARVIS_BRAIN_ABSOLUTE_LIFETIME_S"


def _resolve_absolute_lifetime_s() -> int:
    """Resolve the absolute self-destruct lifetime (seconds). Fail-soft: any
    unset/blank/non-numeric/non-positive value returns 0 (disarmed) -- this
    seam must never raise and never block a boot."""
    raw = (os.environ.get(_ENV_ABSOLUTE_LIFETIME_S, "") or "").strip()
    if not raw:
        return 0
    try:
        val = int(float(raw))
    except (TypeError, ValueError):
        return 0
    return val if val > 0 else 0


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
    lifetime_s = _resolve_absolute_lifetime_s()
    selfdestruct = (
        (
            "# Absolute self-destruct dead-man (A1 $0 mandate): unconditional,\n"
            "# hang-proof, independent of the idle marker / brain process / control\n"
            "# plane. Survives a Mac SIGKILL or total network partition.\n"
            "nohup bash -c 'sleep %d; shutdown -h now' >/dev/null 2>&1 &\n"
            % lifetime_s
        )
        if lifetime_s > 0
        else ""
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
        + selfdestruct
    )


# ---------------------------------------------------------------------------
# provision_brain -- the extracted create-instance composition.
# ---------------------------------------------------------------------------


async def provision_brain(
    *,
    node_name: str,
    labels: Optional[Dict[str, str]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    create_instance_fn: Optional[Callable[..., Awaitable[Tuple[bool, str]]]] = None,
) -> Tuple[bool, str]:
    """Provision the Stage-1 CPU Brain VM: compose brain-env metadata + TLS
    material delivery + the runtime startup script + env-resolved machine/image,
    then issue the create. EXTRACTED from the ignition driver's live
    ``_do_create_instance`` default -- byte-equivalent create_instance kwargs
    when ``labels``/``extra_env`` are None.

    Stage-4 additive seams:
      * ``labels``: extra lineage labels (values sanitized to the GCP charset)
        merged OVER the base ``{jarvis-role: brain}`` discovery label.
      * ``extra_env``: extra brain-env values folded into the node's
        /etc/jarvis/brain.env (e.g. parent/generation lineage env).
      * ``create_instance_fn``: injectable seam for tests; the live default is
        ``get_compute_rest().create_instance`` (real GCP REST -- Mandate 1: no
        cloud simulation on the live path).

    Raises ``TLSMaterialError`` when TLS is enabled and the server material is
    incomplete (fail-closed, $0 -- never spend on a node that cannot pass its
    own mTLS probe)."""
    from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
        _BRAIN_ENV_METADATA_KEY,
        _BRAIN_ROLE_LABEL_KEY,
        _BRAIN_ROLE_LABEL_VALUE,
        _brain_image_family,
        _brain_machine_type,
        _compose_brain_env,
        get_compute_rest,
    )

    brain_env = _compose_brain_env(brain_env_values(extra_env))
    extra = {_BRAIN_ENV_METADATA_KEY: brain_env}
    # Ship the SERVER PEM material as metadata; the runtime startup script
    # writes it to /etc/jarvis/tls/* before the units start.
    extra.update(_tls_extra_metadata(_tls_material()))

    merged_labels: Dict[str, str] = {_BRAIN_ROLE_LABEL_KEY: _BRAIN_ROLE_LABEL_VALUE}
    for k, v in (labels or {}).items():
        merged_labels[str(k)] = sanitize_label_value(v)

    create = create_instance_fn or get_compute_rest().create_instance
    return await create(
        startup_script=_brain_runtime_startup_script(),
        name=node_name,
        machine_type=_brain_machine_type(),
        image_family=_brain_image_family(),
        accelerator_type="",
        accelerator_count=0,
        labels=merged_labels,
        extra_metadata=extra,
    )


# ---------------------------------------------------------------------------
# ResourceManifest -- append-only ownership ledger (JSONL).
# ---------------------------------------------------------------------------

_ENV_MANIFEST_PATH = "JARVIS_RESOURCE_MANIFEST_PATH"


def _default_manifest_path() -> Path:
    # backend/core/ouroboros/governance/brain_lifecycle.py -> repo root is 4 up.
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / ".jarvis" / "manifests" / "resource_manifest.jsonl"


def _keeper_id() -> str:
    val = (os.environ.get("JARVIS_KEEPER_ID", "") or "").strip()
    if val:
        return val
    try:
        return socket.gethostname()
    except Exception:  # noqa: BLE001
        return "unknown-keeper"


class ResourceManifest:
    """Append-only JSONL ledger of every cloud resource this keeper family has
    created or deleted. The manifest IS the teardown walk (never a discovery
    grep): ``live_family`` replays the file, applies delete tombstones, and
    returns the surviving family CHILDREN FIRST so teardown deletes leaves
    before their parents.

    Writes reuse the intake-WAL flock append substrate (imported via the WAL
    class -- private-but-stable, same precedent as bake_brain_golden_image's
    helper reuse; the flock logic is NEVER copied). Replay mirrors the WAL's
    corrupt-line tolerance: a torn/garbled line is skipped with a warning,
    never a crash."""

    def __init__(self, path: Optional[Any] = None) -> None:
        if path is not None:
            resolved = Path(path)
        else:
            raw = (os.environ.get(_ENV_MANIFEST_PATH, "") or "").strip()
            resolved = Path(raw) if raw else _default_manifest_path()
        self._path = resolved
        # REUSE the intake-WAL primitives: WAL.__init__ mkdirs parents;
        # WAL._write_line is the flock cross-process append (never raises).
        from backend.core.ouroboros.governance.intake.wal import WAL  # noqa: PLC0415

        self._wal = WAL(self._path)

    @property
    def path(self) -> Path:
        return self._path

    # -- append surface -----------------------------------------------------

    def record_create(
        self,
        *,
        kind: str,
        name: str,
        zone: Optional[str] = None,
        labels: Optional[Dict[str, str]] = None,
        parent: Optional[str] = None,
        gen: Optional[int] = None,
        keeper_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append a create record. Called AT BIRTH (right when the resource is
        requested) so a controller death mid-provision still leaves the child on
        the manifest for the next teardown walk."""
        record: Dict[str, Any] = {
            "op": "create",
            "kind": str(kind),
            "name": str(name),
            "zone": zone,
            "labels": dict(labels or {}),
            "parent": parent,
            "gen": gen,
            "keeper_id": keeper_id or _keeper_id(),
            "ts_utc": datetime.now(timezone.utc).isoformat(),
        }
        self._wal._write_line(record)  # noqa: SLF001 -- private-but-stable WAL substrate
        return record

    def record_delete(self, name: str) -> Dict[str, Any]:
        """Append a delete tombstone for ``name``."""
        record: Dict[str, Any] = {
            "op": "delete",
            "name": str(name),
            "keeper_id": _keeper_id(),
            "ts_utc": datetime.now(timezone.utc).isoformat(),
        }
        self._wal._write_line(record)  # noqa: SLF001 -- private-but-stable WAL substrate
        return record

    # -- replay surface -----------------------------------------------------

    def _replay_records(self) -> List[Dict[str, Any]]:
        """Tolerant full-file replay: every parseable record, in append order.
        Corrupt lines are skipped with a warning (WAL semantics)."""
        records: List[Dict[str, Any]] = []
        if not self._path.exists():
            return records
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning(
                            "[ResourceManifest] corrupt line %d in %s -- skipping",
                            line_no, self._path,
                        )
                        continue
                    if isinstance(rec, dict):
                        records.append(rec)
        except OSError as exc:
            logger.warning("[ResourceManifest] read failed (%r) -- empty replay", exc)
        return records

    def _live_records(self) -> Dict[str, Dict[str, Any]]:
        """Replay -> live set: creates keyed by name, delete tombstones applied.
        A re-create after a delete resurrects the name (append order wins)."""
        live: Dict[str, Dict[str, Any]] = {}
        for rec in self._replay_records():
            name = rec.get("name")
            if not name:
                continue
            op = rec.get("op")
            if op == "create":
                live.pop(name, None)  # re-insert to refresh append ordering
                live[name] = rec
            elif op == "delete":
                live.pop(name, None)
        return live

    def live_family(self, root_name: str) -> List[Dict[str, Any]]:
        """Return the LIVE records whose parent-chain reaches ``root_name``,
        ordered CHILDREN FIRST (deepest descendants first, the root record --
        when present -- last) so a teardown walk deletes leaves before the
        parents that own them."""
        live = self._live_records()
        children: Dict[str, List[Dict[str, Any]]] = {}
        for rec in live.values():
            parent = rec.get("parent")
            if parent:
                children.setdefault(str(parent), []).append(rec)

        # BFS by depth from the root; cycle-guarded via the seen set.
        levels: List[List[Dict[str, Any]]] = []
        seen = {str(root_name)}
        frontier = [str(root_name)]
        while frontier:
            level: List[Dict[str, Any]] = []
            nxt: List[str] = []
            for parent_name in frontier:
                for rec in children.get(parent_name, []):
                    name = str(rec.get("name"))
                    if name in seen:
                        continue
                    seen.add(name)
                    level.append(rec)
                    nxt.append(name)
            if level:
                levels.append(level)
            frontier = nxt

        ordered: List[Dict[str, Any]] = []
        for level in reversed(levels):
            ordered.extend(level)
        root_rec = live.get(str(root_name))
        if root_rec is not None:
            ordered.append(root_rec)
        return ordered

    def max_gen(self) -> int:
        """Highest ``gen`` across ALL create records ever appended (delete
        tombstones do NOT regress the counter -- a generation number is never
        reused even after its node is gone)."""
        best = 0
        for rec in self._replay_records():
            if rec.get("op") != "create":
                continue
            try:
                gen = int(rec.get("gen"))
            except (TypeError, ValueError):
                continue
            best = max(best, gen)
        return best


# ---------------------------------------------------------------------------
# teardown_family -- the manifest IS the walk; the label query is ONLY a
# drift detector.
# ---------------------------------------------------------------------------


def _delete_ok(result: Any) -> Tuple[bool, str]:
    """Normalize a delete callable's result to (ok, detail). Accepts the
    codebase's ``(ok, detail)`` tuple convention or a bare truthy."""
    if isinstance(result, tuple):
        ok = bool(result[0])
        detail = str(result[1]) if len(result) > 1 else ""
        return ok, detail
    return bool(result), str(result)


async def teardown_family(
    root_name: str,
    *,
    manifest: ResourceManifest,
    delete_instance_fn: Callable[..., Awaitable[Any]],
    delete_firewall_fn: Optional[Callable[..., Awaitable[Any]]] = None,
    label_query_fn: Optional[Callable[..., Awaitable[Any]]] = None,
    owner_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Tear down every live resource owned (transitively) by ``root_name``.

    # INVARIANT: manifest-walk-only teardown
    The deletion source is EXCLUSIVELY ``manifest.live_family`` (children
    first). No list/aggregatedList discovery ever feeds the delete loop -- the
    optional ``label_query_fn`` runs AFTER the walk purely as a drift DETECTOR:
    any resource carrying our ``LABEL_OWNER`` family label that the manifest
    did not know is LOUD-warned and returned as ``drift`` (a structural
    invariant violation: something was born unrecorded), never silently
    deleted.

    ``owner_id`` (Stage-4 Task-4 re-review, IMPORTANT-4): the value the drift
    detector queries the ``LABEL_OWNER`` label for. The FAMILY WALK still keys
    on ``root_name`` (the parent-chain root), but the keeper stamps the
    ``LABEL_OWNER`` label with its KEEPER ID -- which is NOT the family-walk
    root (a child's ``parent`` is its node/keeper). Without this the drift
    query for a keeper-owned family used ``root_name`` and matched nothing
    (inert detector). ``None`` -> the value defaults to ``root_name``
    (byte-identical legacy behavior for callers that own by root name).

    Fail-soft per item: one failing delete is recorded in ``failed`` and the
    walk continues (a wedged child must not orphan its siblings). Successful
    deletes append a ``record_delete`` tombstone so the manifest converges.

    Callable contracts:
      * ``delete_instance_fn(name, zone=...)`` -> (ok, detail) or truthy
      * ``delete_firewall_fn(rule_name=...)``  -> (ok, detail) or truthy
      * ``label_query_fn(label_key=..., label_value=...)`` -> list of dicts
        with at least ``name`` (e.g. ``list_instances_by_label``)
    """
    family = manifest.live_family(root_name)
    deleted: List[str] = []
    failed: List[Dict[str, str]] = []

    for rec in family:
        name = str(rec.get("name"))
        kind = str(rec.get("kind") or "instance")
        zone = rec.get("zone")
        try:
            if kind == "firewall":
                if delete_firewall_fn is None:
                    failed.append({
                        "name": name, "kind": kind,
                        "error": "no_delete_firewall_fn",
                    })
                    continue
                ok, detail = _delete_ok(await delete_firewall_fn(rule_name=name))
            else:
                ok, detail = _delete_ok(await delete_instance_fn(name, zone=zone))
            if ok:
                manifest.record_delete(name)
                deleted.append(name)
            else:
                failed.append({"name": name, "kind": kind, "error": detail})
        except Exception as exc:  # noqa: BLE001 -- teardown is fail-soft per item
            logger.warning(
                "[BrainLifecycle] teardown delete failed name=%s kind=%s "
                "(continuing): %r", name, kind, exc,
            )
            failed.append({"name": name, "kind": kind, "error": repr(exc)})

    # Drift DETECTOR (never a deletion source): compare the label family
    # against everything the manifest has EVER known (creates, live or
    # tombstoned) -- an unknown member means a resource was born unrecorded.
    drift: List[Dict[str, Any]] = []
    if label_query_fn is not None:
        known = {
            str(rec.get("name"))
            for rec in manifest._replay_records()  # noqa: SLF001 -- own module
            if rec.get("op") == "create" and rec.get("name")
        }
        drift_owner = sanitize_label_value(owner_id if owner_id else root_name)
        try:
            found = await label_query_fn(
                label_key=LABEL_OWNER,
                label_value=drift_owner,
            )
            for item in found or []:
                item_name = str((item or {}).get("name") or "")
                if item_name and item_name not in known:
                    drift.append(dict(item))
                    logger.warning(
                        "[BrainLifecycle] OWNERSHIP DRIFT: resource %r carries "
                        "%s=%s but the manifest never recorded it -- a resource "
                        "was born OUTSIDE the ownership substrate (structural "
                        "invariant violation; NOT auto-deleted)",
                        item_name, LABEL_OWNER, drift_owner,
                    )
        except Exception as exc:  # noqa: BLE001 -- detector is advisory
            logger.warning(
                "[BrainLifecycle] drift detector failed (advisory only): %r", exc,
            )

    return {
        "root": str(root_name),
        "family_size": len(family),
        "deleted": deleted,
        "failed": failed,
        "drift": drift,
    }

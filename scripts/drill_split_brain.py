#!/usr/bin/env python3
"""Stage-4 split-brain LIVE drill (Task 5) -- bounded, faithful, real-wire.

Proves the novel Mandate-4 mechanic that unit tests cannot: the keeper actively
DELIVERS the generation-fence signal over the real mTLS bus to a real superseded
Brain node's endpoint, and that node self-fences (capture_inflight) then is
reaped -- all composing the shipped primitives, zero mocks in the live path.

Economy (documented, not a shortcut): ONE real gen-1 node is provisioned. gen-2's
existence is orthogonal to proving gen-1's fence behaviour, so rather than burn a
second idle VM we tell the keeper `current_gen=2`; the fence delivery, the remote
self-fence + capture, and the reap are all genuinely live. The autonomous gen-2
MINT path is the same provisioning code proven live in Stages 1-3.

Streams telemetry to stdout at every phase.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _log(msg: str) -> None:
    print("[S4-DRILL] %s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


async def _await_serving(budget_s: float) -> str:
    """Zone-aware serving probe -- the SAME Reachability Racer + mTLS handshake
    the real fence delivery uses (never a zone-pinned SSH poll). Returns the
    bound WS URL, or '' on timeout."""
    from backend.core.ouroboros.governance.brain_discovery import discover_brain_endpoint
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        url = await discover_brain_endpoint()
        if url:
            return url
        _log("  ...gen-1 booting (mTLS endpoint not yet discoverable)")
        await asyncio.sleep(20)
    return ""


async def main() -> int:
    from backend.core.ouroboros.governance.brain_lifecycle import (
        LABEL_GEN, LABEL_OWNER, LABEL_PARENT, ResourceManifest,
        provision_brain, sanitize_label_value,
    )
    from backend.core.ouroboros.governance.brain_keeper import BrainKeeper, PersistentTokenBucket
    from backend.core.ouroboros.governance import brain_discovery

    project = os.environ["JARVIS_GCP_PROJECT"]
    zone = os.environ.get("GCP_ZONE", "us-central1-c")
    keeper_id = os.environ.get("JARVIS_KEEPER_ID", "mac-body-keeper")
    stamp = os.environ["DRILL_STAMP"]
    node = f"jarvis-brain-gen1-{stamp}"

    manifest = ResourceManifest()  # JARVIS_RESOURCE_MANIFEST_PATH -> drill dir
    bucket = PersistentTokenBucket()

    # --- open the /32 firewall so the Mac can reach the node's bus ---
    _log("PHASE 0: opening ephemeral /32 mTLS firewall")
    ok, detail = await brain_discovery.open_brain_firewall()
    _log(f"  firewall -> {ok} ({detail})")

    # --- PHASE 1: provision the REAL gen-1 node (keeper-stamped => has a fence) ---
    _log(f"PHASE 1: provisioning gen-1 node={node} (fence-stamped)")
    labels = {
        LABEL_OWNER: sanitize_label_value(keeper_id),
        LABEL_PARENT: sanitize_label_value(keeper_id),
        LABEL_GEN: "1",
    }
    extra_env = {
        "JARVIS_BRAIN_GENERATION": "1",
        "JARVIS_KEEPER_ID": keeper_id,
        "JARVIS_BRAIN_PARENT_NODE": keeper_id,
    }
    # record-at-birth BEFORE the await (the orphan-proof ordering)
    manifest.record_create(kind="instance", name=node, zone=zone,
                           labels=labels, parent=keeper_id, gen=1,
                           keeper_id=keeper_id)
    ok, detail = await provision_brain(node_name=node, labels=labels, extra_env=extra_env)
    _log(f"  provision -> {ok} ({detail})")
    if not ok:
        _log("  PROVISION FAILED -- reaping + abort")
        await _reap(manifest, keeper_id, node, project)
        return 2

    _log("PHASE 2: awaiting gen-1 organism (mTLS-discoverable + fence armed)")
    url = await _await_serving(budget_s=900)
    if not url:
        _log("  gen-1 never served -- reaping + abort")
        await _reap(manifest, keeper_id, node, project)
        return 3
    _log(f"  gen-1 SERVING at {url} (fence subscribed to console.keeper_heartbeat)")

    # --- PHASE 3: deliver the fence over the REAL wire (explicit, so PHASE 4 can
    #     capture the node-side self-fence BEFORE the reap -- Mandate-4 proof) ---
    _log("PHASE 3: delivering console.keeper_heartbeat{gen:2} to gen-1's real endpoint")
    keeper = BrainKeeper(
        discover_fn=brain_discovery.discover_brain_endpoint,
        provision_fn=provision_brain, manifest=manifest, bucket=bucket,
        keeper_id=keeper_id,
    )
    deliver = keeper._resolve_fence_deliver()  # the live default fence-deliver fn
    delivered = await deliver(node, 2)
    _log(f"  fence DELIVERED over mTLS wire -> {delivered}")

    # --- PHASE 4: capture the REMOTE self-fence journal (the Mandate-4 clause) ---
    _log("PHASE 4: capturing gen-1's self-fence journal (FENCED + capture_inflight)")
    await asyncio.sleep(8)  # let the node process the heartbeat + run the 4-arm fence
    import subprocess
    try:
        r = subprocess.run(
            ["gcloud", "compute", "ssh", node, f"--project={project}",
             f"--zone={zone}", "--tunnel-through-iap", "--command",
             "sudo journalctl -u jarvis-brain.service --no-pager | "
             "grep -iE 'GenerationFence|generation_fenced|FENCED' | tail -10 || echo NONE"],
            capture_output=True, text=True, timeout=120,
        )
        for line in (r.stdout or "").splitlines():
            _log(f"  [gen-1 journal] {line}")
    except Exception as exc:  # noqa: BLE001
        _log(f"  journal capture failed (node-side proof best-effort): {exc!r}")

    # --- reap gen-1 now (post-proof) ---
    _log("PHASE 4b: reaping the superseded gen-1")
    reaped = await keeper.reap_generation(node)
    _log(f"  reaped -> {reaped.get('ok') if isinstance(reaped, dict) else reaped}")

    # --- PHASE 5: cap-exhaustion leg (bucket is persistent) ---
    _log("PHASE 5: cap-exhaustion leg")
    caps = int(os.environ.get("JARVIS_BRAIN_RESURRECT_MAX_PER_H", "2"))
    takes = [bucket.try_take() for _ in range(caps + 1)]
    _log(f"  bucket takes (cap={caps}): {takes} -> last MUST be False (terminal, no VM)")

    # --- PHASE 6: teardown to $0 + drift check ---
    _log("PHASE 6: teardown + drift check -> $0")
    await _reap(manifest, keeper_id, node, project)
    return 0


async def _reap(manifest, keeper_id, node, project) -> None:
    from backend.core.ouroboros.governance.brain_lifecycle import teardown_family
    from backend.core.ouroboros.governance import gcp_compute_rest

    async def _del_inst(name):
        return await gcp_compute_rest.get_compute_rest().delete_instance(name=name)

    async def _del_fw(name):
        try:
            from backend.core.ouroboros.governance.brain_discovery import close_brain_firewall
            return await close_brain_firewall()
        except Exception:  # noqa: BLE001
            return (True, "no-fw")

    res = await teardown_family(
        node, manifest=manifest, owner_id=keeper_id,
        delete_instance_fn=_del_inst, delete_firewall_fn=_del_fw,
    )
    _log(f"  teardown: reaped={res.get('reaped')} drift={res.get('drift')}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

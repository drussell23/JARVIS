"""Gap 3c confirmation -- the dead-man startup-script generator + self-delete.

The failover FSM passes a ``startup_script`` to the awakened VM; the generator
(``failover_deadman.build_deadman_startup_script``) emits an UNBREAKABLE
node-side watchdog that self-deletes the VM via the metadata-SA-token Compute
REST API once the Ollama endpoint has been idle past a TTL (env-tuned).

This file is a Mesh-scoped confirmation that the generator exists, is wired into
the lifecycle, is env-tuned, and emits the self-delete REST DELETE -- it does
NOT duplicate the deadman's own unit suite.
"""
from __future__ import annotations

import backend.core.ouroboros.governance.failover_deadman as dm
import backend.core.ouroboros.governance.failover_lifecycle as fl


def test_deadman_script_generated_and_self_deletes():
    script = dm.build_deadman_startup_script(port=11434)
    # The watchdog self-deletes via the GCE Compute REST DELETE (decoupled,
    # unbreakable -- no gcloud on the node; metadata SA token + curl only).
    assert "jprime-deadman" in script
    assert "-X DELETE" in script
    assert "compute.googleapis.com" in script
    assert "Metadata-Flavor: Google" in script
    # Idle-driven: only self-deletes once idle > IDLE_TIMEOUT_S and uptime
    # past boot grace.
    assert "IDLE_TIMEOUT_S" in script
    assert "BOOT_GRACE_S" in script


def test_deadman_ttl_is_env_tuned(monkeypatch):
    monkeypatch.setenv("JARVIS_DEADMAN_IDLE_TIMEOUT_S", "777")
    script = dm.build_deadman_startup_script(port=11434)
    assert "777" in script


def test_lifecycle_wires_deadman_into_startup_script():
    """The FSM's _build_startup_script delegates to the deadman generator -- so
    the awakened node always carries the cost backstop."""
    ctrl = fl.FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: True,
        vm_delete_fn=lambda: True,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: True,
        clock_fn=lambda: 0.0,
    )
    script = ctrl._build_startup_script()
    assert "jprime-deadman" in script
    assert "-X DELETE" in script


def test_deadman_script_is_ascii():
    script = dm.build_deadman_startup_script(port=11434)
    # ASCII-only invariant (the bash script must be 7-bit clean).
    script.encode("ascii")

"""Config-driven port resolution + dynamic cloud-init inference-bind injection.

Final-soak last mile (bt-2026-06-29-071731): the node was reachable (firewall +
racer worked at L3) but :11434 RST'd -- the golden image's inference daemon
either wasn't up or bound 127.0.0.1, and the failover port was hardcoded 11434
while .env.gcp serves JARVIS_PRIME_PORT=8000.

Fixes:
  1. ``_failover_port`` is config-driven (JARVIS_JPRIME_FAILOVER_PORT override >
     JARVIS_PRIME_PORT > legacy default). One resolver feeds the firewall, the
     racer, and the endpoint publisher -- change the config, the whole mesh adapts.
  2. The awaken startup-script injects a cloud-init block that forces the
     inference daemon to bind 0.0.0.0:<resolved-port> + restarts it.
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.failover_lifecycle as fl
from backend.core.ouroboros.governance import failover_deadman as fd


# ---------------------------------------------------------------------------
# Config-driven port resolution
# ---------------------------------------------------------------------------

def test_port_defaults_to_legacy(monkeypatch):
    monkeypatch.delenv("JARVIS_JPRIME_FAILOVER_PORT", raising=False)
    monkeypatch.delenv("JARVIS_PRIME_PORT", raising=False)
    assert fl._failover_port() == 11434


def test_port_reads_jarvis_prime_port(monkeypatch):
    monkeypatch.delenv("JARVIS_JPRIME_FAILOVER_PORT", raising=False)
    monkeypatch.setenv("JARVIS_PRIME_PORT", "8000")
    assert fl._failover_port() == 8000  # the whole mesh adapts to 8000


def test_explicit_failover_port_override_wins(monkeypatch):
    monkeypatch.setenv("JARVIS_PRIME_PORT", "8000")
    monkeypatch.setenv("JARVIS_JPRIME_FAILOVER_PORT", "9999")
    assert fl._failover_port() == 9999  # explicit failover pin beats the general


def test_port_feeds_endpoint_builder(monkeypatch):
    """The resolved port flows into the racer's candidate endpoints (no hardcode)."""
    monkeypatch.setenv("JARVIS_PRIME_PORT", "8000")
    ctrl = fl.FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: True, vm_delete_fn=lambda: True,
        node_ready_fn=lambda e: True, clock_fn=lambda: 1.0,
    )
    assert ctrl._build_endpoint().endswith(":8000")


# ---------------------------------------------------------------------------
# Cloud-init inference-bind injection into the awaken startup-script
# ---------------------------------------------------------------------------

def test_startup_script_injects_bind_block(monkeypatch):
    monkeypatch.setenv("JARVIS_PRIME_PORT", "8000")
    monkeypatch.setenv("JARVIS_FAILOVER_INFERENCE_BIND_ENABLED", "true")
    ctrl = fl.FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: True, vm_delete_fn=lambda: True,
        node_ready_fn=lambda e: True, clock_fn=lambda: 1.0,
    )
    script = ctrl._build_startup_script()
    # Forces 0.0.0.0 bind on the RESOLVED port (not 127.0.0.1, not hardcoded 11434).
    assert "0.0.0.0:8000" in script
    assert "OLLAMA_HOST" in script
    assert "systemctl" in script and "restart" in script
    assert "127.0.0.1" not in script.split("OLLAMA_HOST", 1)[1][:80]


def test_bind_block_gate_off_legacy(monkeypatch):
    """Gate OFF -> only the dead-man script (byte-identical legacy)."""
    monkeypatch.setenv("JARVIS_FAILOVER_INFERENCE_BIND_ENABLED", "false")
    ctrl = fl.FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: True, vm_delete_fn=lambda: True,
        node_ready_fn=lambda e: True, clock_fn=lambda: 1.0,
    )
    script = ctrl._build_startup_script()
    assert "OLLAMA_HOST=0.0.0.0" not in script


def test_bind_block_is_valid_bash_snippet(monkeypatch):
    monkeypatch.setenv("JARVIS_PRIME_PORT", "8000")
    block = fd.build_inference_bind_block(port=8000)
    assert block.startswith("#") or "OLLAMA_HOST" in block
    assert "0.0.0.0:8000" in block
    assert "daemon-reload" in block or "systemctl" in block


def test_inference_bind_gpu_hardware_gate():
    """Quality (require_gpu) node: nvidia-smi gate refuses to serve on GPU-absent
    so the Reachability Racer never gets 200 -> orchestrator fail-soft."""
    from backend.core.ouroboros.governance.failover_deadman import build_inference_bind_block
    gpu = build_inference_bind_block(port=11434, require_gpu=True)
    assert "nvidia-smi" in gpu
    assert "REFUSING to serve" in gpu
    assert "systemctl stop ollama" in gpu          # crashes the service if no GPU
    assert "OLLAMA_HOST=0.0.0.0:11434" in gpu       # binds only inside the GPU-ok branch
    assert "if nvidia-smi" in gpu                    # gated


def test_inference_bind_cpu_survival_node_ungated():
    """Survival 7B CPU node (require_gpu=False default): NO nvidia-smi gate, binds
    normally (it has no GPU; a gate would break it)."""
    from backend.core.ouroboros.governance.failover_deadman import build_inference_bind_block
    cpu = build_inference_bind_block(port=11434)
    assert "nvidia-smi" not in cpu
    assert "OLLAMA_HOST=0.0.0.0:11434" in cpu        # binds unconditionally

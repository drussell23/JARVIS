# -*- coding: utf-8 -*-
"""TLS material delivery: driver preflight + metadata + node files (Task-5 wiring).

Live-fire pre-flight finding (2026-07-04): the driver folded Mac-local
``JARVIS_BRAIN_WS_TLS_*`` PATHS into brain.env, but the SSL builders take file
paths and nothing ever shipped the PEMs onto the node -- and the Mac's CLIENT
cert paths would have masqueraded as the node's SERVER cert. This spine proves:

  (a) fail-closed preflight: TLS enabled + material missing -> abort BEFORE any
      GCP call ($0);
  (b) the server PEMs + CA travel via instance metadata keys;
  (c) brain.env TLS paths are OVERRIDDEN to the node-local /etc/jarvis/tls/*;
  (d) the runtime startup script writes those files (key 0600) BEFORE the unit
      (re)start.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ignite_brain_vm.py"


def _load():
    spec = importlib.util.spec_from_file_location("ignite_brain_vm", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("ignite_brain_vm", mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ign():
    return _load()


@pytest.fixture()
def mtls_dir(tmp_path, monkeypatch):
    d = tmp_path / "mtls"
    d.mkdir()
    (d / "server-cert.pem").write_text("PEM-SERVER-CERT")
    (d / "server-key.pem").write_text("PEM-SERVER-KEY")
    (d / "ca.pem").write_text("PEM-CA")
    monkeypatch.setenv("JARVIS_BRAIN_MTLS_DIR", str(d))
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "true")
    return d


# --------------------------------------------------------------------------- #
# (a) fail-closed preflight.
# --------------------------------------------------------------------------- #
def test_tls_material_loads_from_mtls_dir(ign, mtls_dir):
    mat = ign._tls_material()
    assert mat == {
        "server_cert": "PEM-SERVER-CERT",
        "server_key": "PEM-SERVER-KEY",
        "ca": "PEM-CA",
    }


def test_tls_material_missing_raises_before_any_gcp(ign, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("JARVIS_BRAIN_MTLS_DIR", str(empty))
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "true")
    with pytest.raises(ign.TLSMaterialError):
        ign._tls_material()


def test_tls_material_none_when_tls_disabled(ign, monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "false")
    monkeypatch.delenv("JARVIS_BRAIN_MTLS_DIR", raising=False)
    assert ign._tls_material() is None


def test_tls_material_unset_dir_raises_when_tls_enabled(ign, monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "true")
    monkeypatch.delenv("JARVIS_BRAIN_MTLS_DIR", raising=False)
    with pytest.raises(ign.TLSMaterialError):
        ign._tls_material()


# --------------------------------------------------------------------------- #
# (b)+(c) metadata keys + brain.env node-path override.
# --------------------------------------------------------------------------- #
def test_tls_metadata_keys_carry_pem_content(ign, mtls_dir):
    meta = ign._tls_extra_metadata(ign._tls_material())
    assert meta["brain-tls-server-cert"] == "PEM-SERVER-CERT"
    assert meta["brain-tls-server-key"] == "PEM-SERVER-KEY"
    assert meta["brain-tls-ca"] == "PEM-CA"


def test_brain_env_values_override_tls_paths_to_node_local(ign, mtls_dir, monkeypatch):
    # Mac-side env points at MAC paths (the client material) -- the node must
    # NEVER see them.
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_CERT", "/Users/mac/client-cert.pem")
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_KEY", "/Users/mac/client-key.pem")
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_CA", "/Users/mac/ca.pem")
    driver = ign.BrainIgnitionDriver(dry_run=True)
    vals = driver._brain_env_values()
    assert vals["JARVIS_BRAIN_WS_TLS_CERT"] == "/etc/jarvis/tls/server-cert.pem"
    assert vals["JARVIS_BRAIN_WS_TLS_KEY"] == "/etc/jarvis/tls/server-key.pem"
    assert vals["JARVIS_BRAIN_WS_TLS_CA"] == "/etc/jarvis/tls/ca.pem"
    for v in vals.values():
        assert "/Users/" not in v, "Mac-local paths must never reach brain.env"


def test_brain_env_values_no_override_when_tls_disabled(ign, monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "false")
    monkeypatch.delenv("JARVIS_BRAIN_MTLS_DIR", raising=False)
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_CERT", "/tmp/x.pem")
    driver = ign.BrainIgnitionDriver(dry_run=True)
    vals = driver._brain_env_values()
    # Legacy fold-through when no material is shipped.
    assert vals.get("JARVIS_BRAIN_WS_TLS_CERT") == "/tmp/x.pem"


# --------------------------------------------------------------------------- #
# (d) runtime startup script writes the TLS files before the unit (re)start.
# --------------------------------------------------------------------------- #
def test_runtime_startup_script_writes_tls_files_before_restart(ign):
    script = ign._brain_runtime_startup_script()
    for key, path in (
        ("brain-tls-server-cert", "/etc/jarvis/tls/server-cert.pem"),
        ("brain-tls-server-key", "/etc/jarvis/tls/server-key.pem"),
        ("brain-tls-ca", "/etc/jarvis/tls/ca.pem"),
    ):
        assert key in script, f"metadata key {key} must be fetched"
        assert path in script, f"node file {path} must be written"
        assert script.index(path) < script.index("systemctl restart jarvis-brain"), (
            "TLS files must land before the unit restart")
    assert "chmod 600 /etc/jarvis/tls/server-key.pem" in script
    assert script.startswith("#!")
    script.encode("ascii")


# --------------------------------------------------------------------------- #
# (e) runtime startup script refreshes the baked clone (fail-soft) and starts
#     the Stage-0 bus + echo-responder SIDECAR -- the Brain-side server that
#     makes the cross-host acceptance real (wired-but-inert finding #6: the
#     transport package previously had no boot path on the node).
# --------------------------------------------------------------------------- #
def test_runtime_startup_script_pulls_repo_before_units(ign):
    script = ign._brain_runtime_startup_script()
    assert "git -C /opt/trinity/jarvis pull --ff-only || true" in script, (
        "the baked clone must track main at ignition (fail-soft)")
    assert script.index("git -C /opt/trinity/jarvis pull") < script.index(
        "systemctl restart jarvis-brain"), "pull must precede unit (re)starts"


def test_runtime_startup_script_writes_and_starts_bus_sidecar(ign):
    script = ign._brain_runtime_startup_script()
    assert "jarvis-brain-bus.service" in script
    assert ("ExecStart=/opt/trinity/venv/bin/python scripts/brain_bus_echo_server.py"
            in script), "sidecar must run under the SAME venv as the soak"
    assert "EnvironmentFile=/etc/jarvis/brain.env" in script
    assert "WorkingDirectory=/opt/trinity/jarvis" in script
    # TLS files must land before the sidecar starts (it builds the server ctx).
    assert script.index("/etc/jarvis/tls/ca.pem") < script.index(
        "systemctl restart jarvis-brain-bus.service")
    script.encode("ascii")

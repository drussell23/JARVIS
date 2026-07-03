# tests/governance/transport/test_transport_security.py
from __future__ import annotations

import ssl

import pytest

from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport import transport_security as ts


def _cfg(**over):
    base = dict(
        host="127.0.0.1", port=0, path="/ws/trinity-bus", heartbeat_s=15.0,
        reconnect_base_s=0.5, reconnect_max_s=30.0, reconnect_jitter=0.3,
        queue_maxsize=256, history_maxlen=1024, degrade_after_missed_hb=2,
        tls_enabled=True, tls_cert=None, tls_key=None, tls_ca=None,
        tls_ephemeral=True, source_id="test",
    )
    base.update(over)
    return TransportConfig(**base)


def test_tls_disabled_returns_none():
    cfg = _cfg(tls_enabled=False, tls_ephemeral=False)
    assert ts.build_server_ssl_context(cfg) is None
    assert ts.build_client_ssl_context(cfg) is None


def test_ephemeral_material_builds_a_real_server_context():
    cfg = _cfg()
    ctx = ts.build_server_ssl_context(cfg)
    assert isinstance(ctx, ssl.SSLContext)
    # mTLS: server demands a client cert.
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_no_hardcoded_material_paths_in_module_source():
    import inspect
    src = inspect.getsource(ts)
    # No absolute cert/key paths baked in.
    assert "/etc/ssl" not in src
    assert "-----BEGIN" not in src  # no inlined PEM

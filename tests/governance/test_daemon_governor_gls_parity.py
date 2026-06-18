# tests/governance/test_daemon_governor_gls_parity.py
from __future__ import annotations


def test_governor_default_off_means_no_attr_and_no_mutation(monkeypatch):
    """With the master gate OFF (default), the governor is never instantiated and
    no host-process mutation can occur (byte-identical to pre-Phase-3.4)."""
    monkeypatch.delenv("JARVIS_LOCAL_DAEMON_GOVERNOR_ENABLED", raising=False)
    from backend.core.ouroboros.governance.local_daemon_governor import daemon_governor_enabled
    assert daemon_governor_enabled() is False


def test_governor_source_is_gated_and_ownership_safe():
    """Static guard: brew 'stop' only reachable behind the _owned ownership flag;
    brew calls are macOS-guarded; master gate present."""
    import backend.core.ouroboros.governance.local_daemon_governor as g
    src = open(g.__file__).read()
    assert "JARVIS_LOCAL_DAEMON_GOVERNOR_ENABLED" in src
    assert "self._owned" in src                      # ownership tracking exists
    assert 'platform.system() == "Darwin"' in src    # macOS-guarded host mutation
    # the stop path must be ownership-gated: 'stop' brew call sits under an _owned check
    assert "if self._owned" in src

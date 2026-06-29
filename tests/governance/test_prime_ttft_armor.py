"""TTFT Armor -- adaptive, decoupled timeouts for the J-Prime tier.

Final-soak (bt-2026-06-29-074457): J-Prime served, but a cold 7B-on-CPU first
token takes >90s. The aiohttp client's ``total`` timeout (180s) would SEVER the
TCP connection mid-crunch regardless of the read timeout. The armor decouples a
SHORT connect timeout (5s -- fail fast if the node is unreachable) from a MASSIVE
read timeout (300s -- absorb the cold TTFT) and removes the hard ``total`` cap
that defeats the decoupling. Config-driven (env), no hardcoding.
"""
from __future__ import annotations

import pytest

import backend.core.prime_client as pc


def test_connect_timeout_is_short(monkeypatch):
    monkeypatch.delenv("PRIME_CONNECT_TIMEOUT", raising=False)
    assert pc.PrimeClientConfig().connect_timeout == 5.0  # fail fast on unreachable


def test_read_timeout_is_armored(monkeypatch):
    """Massive read timeout absorbs the cold CPU TTFT (was 120 -> 300)."""
    monkeypatch.delenv("PRIME_READ_TIMEOUT", raising=False)
    assert pc.PrimeClientConfig().read_timeout >= 300.0


def test_total_timeout_no_hard_cap_by_default(monkeypatch):
    """The hard total cap that severed the crunch is gone (0 == no cap)."""
    monkeypatch.delenv("PRIME_TOTAL_TIMEOUT", raising=False)
    assert pc.PrimeClientConfig().total_timeout == 0.0


def test_timeouts_env_overridable(monkeypatch):
    monkeypatch.setenv("PRIME_READ_TIMEOUT", "600")
    monkeypatch.setenv("PRIME_CONNECT_TIMEOUT", "3")
    cfg = pc.PrimeClientConfig()
    assert cfg.read_timeout == 600.0 and cfg.connect_timeout == 3.0


def test_aiohttp_timeout_decouples_and_drops_total(monkeypatch):
    """The resolved aiohttp timeout: connect=5, sock_read>=300, total=None (the
    decoupling is preserved -- a long crunch is never severed by a total cap)."""
    monkeypatch.delenv("PRIME_TOTAL_TIMEOUT", raising=False)
    monkeypatch.delenv("PRIME_READ_TIMEOUT", raising=False)
    monkeypatch.delenv("PRIME_CONNECT_TIMEOUT", raising=False)
    cfg = pc.PrimeClientConfig()
    kw = pc.resolve_aiohttp_timeout_kwargs(cfg)
    assert kw["connect"] == 5.0
    assert kw["sock_read"] >= 300.0
    assert kw["total"] is None  # NO hard total cap -> never severs the crunch


def test_aiohttp_timeout_honors_explicit_total(monkeypatch):
    """An explicit positive total is honored (operator can re-impose a cap)."""
    monkeypatch.setenv("PRIME_TOTAL_TIMEOUT", "900")
    cfg = pc.PrimeClientConfig()
    kw = pc.resolve_aiohttp_timeout_kwargs(cfg)
    assert kw["total"] == 900.0

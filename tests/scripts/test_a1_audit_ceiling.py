"""Tier-aware audit ceiling (Part C of the final A1 audit fix).

The audit window was a hardcoded 120s -- too short for a 32B/L4 op to reach
terminal APPLIED. The driver now derives the ceiling from the resolved failover
tier, reusing the SAME _heavy_coldstart_mult logic as the cold-start timeouts
(JARVIS_JPRIME_HEAVY_COLDSTART_MULT). No hardcoded window.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_REPO_ROOT = str((Path(__file__).parent.parent.parent).resolve())
_SCRIPTS_DIR = str((Path(__file__).parent.parent.parent / "scripts").resolve())
for _p in (_REPO_ROOT, _SCRIPTS_DIR, os.path.join(_REPO_ROOT, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_script(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS_DIR, name + ".py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _arm_heavy_tier(monkeypatch):
    # Quality tier ON -> resolve_tier returns the 32B/L4 GPU spec (heavy).
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_AWAKEN_URGENCY", "immediate")
    monkeypatch.setenv("JARVIS_FAILOVER_AWAKEN_COMPLEXITY", "complex")


def test_ceiling_scales_for_heavy_tier(monkeypatch):
    drv = _load_script("isomorphic_a1_local")
    monkeypatch.setenv("JARVIS_A1_AUDIT_BASE_S", "120")
    monkeypatch.setenv("JARVIS_JPRIME_HEAVY_COLDSTART_MULT", "4.0")
    _arm_heavy_tier(monkeypatch)

    assert drv._a1_audit_ceiling_s() == 480.0  # 120 * 4.0 (heavy)


def test_ceiling_base_when_not_heavy(monkeypatch):
    drv = _load_script("isomorphic_a1_local")
    monkeypatch.setenv("JARVIS_A1_AUDIT_BASE_S", "120")
    monkeypatch.setenv("JARVIS_JPRIME_HEAVY_COLDSTART_MULT", "4.0")
    # Quality tier OFF -> survival 7B/CPU tier -> NOT heavy -> base unchanged.
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "false")

    assert drv._a1_audit_ceiling_s() == 120.0


def test_ceiling_respects_base_override(monkeypatch):
    drv = _load_script("isomorphic_a1_local")
    monkeypatch.setenv("JARVIS_A1_AUDIT_BASE_S", "90")
    monkeypatch.setenv("JARVIS_JPRIME_HEAVY_COLDSTART_MULT", "3.0")
    _arm_heavy_tier(monkeypatch)

    assert drv._a1_audit_ceiling_s() == 270.0  # 90 * 3.0

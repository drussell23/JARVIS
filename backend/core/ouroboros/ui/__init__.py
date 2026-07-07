"""backend.core.ouroboros.ui -- presentation layer primitives.

This package is a **dependency-free leaf**: it imports only stdlib + Rich and
NEVER imports from ``governance/`` or ``battle_test/``. That inversion lets
every higher layer (the ``ov`` entry script, Ouroboros governance modules, and
Venom tool rendering) import the theme engine *upward* from one place, with no
circular-import risk.

See docs/superpowers/specs/2026-07-06-ov-theme-and-boot-cockpit-design.md §3.1.
"""
from __future__ import annotations

"""A1 Ignition Vector -- a deliberately ISOLATED pure-leaf target package.

Purpose
-------
This package exists solely as the surgical **isolated test vector** for the A1
autonomy-gate ignition (GCP Brain drill, 2026-07-05). It is imported by NOTHING
in the production graph (blast radius ~1) and is fully covered by its own test
(coverage 1.0), so a synthetic failure injected here clears the
``OperationAdvisor`` gate **legitimately** -- without lowering any governance
threshold -- letting the organism drive a full CLASSIFY->...->APPLIED cycle on
a change that is genuinely safe to auto-apply.

Why this is not a shortcut
--------------------------
The Advisor blocks auto-apply when ``blast_radius >= 20 and coverage == 0`` (or
``blast_radius >= 10`` under memory pressure). Real production files the chaos
injector otherwise selects (e.g. ``repl_input_polish.py``) are widely imported
(blast radius 50) and trip that gate -- correctly. This package is the opposite
by construction: a leaf nothing depends on, with 100% test coverage. The gate
still runs at full strictness; the target simply satisfies it honestly.

The chaos injector is scoped to this package via
``JARVIS_CHAOS_TARGET_DIRS=backend/core/ouroboros/a1_ignition_vector`` so it
selects a pure-leaf function HERE rather than anywhere else in the tree.
"""
from __future__ import annotations

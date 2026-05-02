"""Serpent Flow — Ouroboros Flowing CLI with Organism Personality.

Layout Architecture (post UI Slice 3, 2026-04-30):

  Zone 0: Boot Banner — printed once at startup, scrolls away inline
  Zone 1: Event Stream — op-scoped blocks with box-drawing borders
  Zone 2: REPL Input — prompt_toolkit.prompt_async, no fixed positioning

  (Zone 3 — persistent bottom_toolbar — retired in UI Slice 3.
  State is surfaced on-demand via /status /cost /posture REPL
  commands and via inline op-completion receipt lines. No fixed
  terminal regions; matches Claude Code's flowing UX.)

Op blocks use box-drawing characters for visual hierarchy::

  ┌ a7f3 ── TestFailure ──────────────────────────
  │  🔬 sensed    test_voice_pipeline
  │  🧬 synth     via DW-397B
  │  ┌─ 📄 read_file ────────────────────────────
  │  │  backend/voice/pipeline.py  38 lines  42ms
  │  └────────────────────────────────────────────
  │  ✨ evolved   1 file changed │ ⏱ 22.3s
  └ a7f3 ── 🐍 ✅ 1  💀 0 │ 💰 $0.003 ──────────

Manifesto §7: Absolute Observability — the inner workings of the
symbiote must be entirely visible.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.spinner import SPINNERS
from rich.status import Status
from rich.syntax import Syntax

from backend.core.ouroboros.governance.inline_prompt_gate_renderer import (
    attach_phase_boundary_renderer,
)

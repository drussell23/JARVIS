"""The per-route generation window — ONE table, three readers.

WHY THIS MODULE EXISTS
----------------------
The route→timeout table was written twice, byte-identical, in
``orchestrator.py`` and ``phase_runners/generate_runner.py`` (the second
carrying a comment calling itself a "parity twin" of the first). Two copies of
a calibration is a latent drift: whichever one a reader finds is the one they
believe, and only one of them is on the shipping path
(``JARVIS_PHASE_RUNNER_GATE_EXTRACTED``).

The throughput governor needs the same numbers to size lanes against the
window an op will actually be given. Adding a third copy would make the drift
certain, so the table moves here and the existing sites read it.

WHAT IS AND IS NOT HERE
-----------------------
The BASE, env-tunable window only. The value-band scaling (Slice 15 T4) and
telemetry-driven synthesis (Slice 231) stay at their call sites: they are
multipliers applied to this table, not part of it, and they need op context
this module deliberately does not take.

Defaults preserve the 2026-04-12 calibration. Python 3.9+, stdlib only.
"""
from __future__ import annotations

import os
from typing import Dict

#: Route -> (env var, default seconds). The defaults are the 2026-04-12
#: calibration and are the ONLY place they are written.
_ROUTE_ENV: Dict[str, "tuple"] = {
    "immediate": ("JARVIS_GEN_TIMEOUT_IMMEDIATE_S", "120"),
    "standard": ("JARVIS_GEN_TIMEOUT_STANDARD_S", "220"),
    "complex": ("JARVIS_GEN_TIMEOUT_COMPLEX_S", "240"),
    "background": ("JARVIS_GEN_TIMEOUT_BACKGROUND_S", "180"),
    "speculative": ("JARVIS_GEN_TIMEOUT_SPECULATIVE_S", "180"),
}

#: The route a pool-level (pre-dequeue) consumer sizes against when it does not
#: yet hold an op. BackgroundAgentPool predominantly serves this route, and it
#: is the widest of the two background-class windows, so sizing against it is
#: the least-throttling honest choice.
DEFAULT_POOL_ROUTE = "background"


def route_generation_budgets() -> Dict[str, float]:
    """The full route→seconds table, re-read from the environment each call.

    Re-read (not cached) because the harnesses tune these vars at runtime; the
    cost is five ``os.environ`` lookups. A malformed value falls back to its
    own default rather than taking the table down. NEVER raises.
    """
    out: Dict[str, float] = {}
    for route, (env, default) in _ROUTE_ENV.items():
        try:
            out[route] = float(os.environ.get(env, default))
        except (TypeError, ValueError):
            out[route] = float(default)
    return out


def route_generation_budget_s(route: str, default: float = 0.0) -> float:
    """Window for *route*, or *default* when the route is unknown.

    An unknown route returns *default* rather than guessing a window: callers
    already hold a meaningful fallback (``config.generation_timeout_s``) and
    inventing one here would silently overrule it. NEVER raises.
    """
    try:
        key = str(route or "").strip().lower()
    except Exception:  # noqa: BLE001
        return default
    return route_generation_budgets().get(key, default)

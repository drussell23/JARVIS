"""Aegis FlagRegistry seeds + typed env-read helpers.

Single seam to read Aegis-specific env vars. The daemon boot path
imports from here; never reads ``os.environ`` for an Aegis flag
directly. This lets the FlagRegistry record usage + surface typos.

Per binding correction "fail-closed defaults": every cap defaults to
0.0 USD (= nothing admitted). Operator must set caps explicitly to
allow any spend at all. This is the §43.6.1 safety-gate polarity.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Tuple

from backend.core.ouroboros.governance.flag_registry import (
    Category,
    FlagRegistry,
    FlagSpec,
    FlagType,
    get_default_registry,
)

from backend.core.ouroboros.aegis.budget_state_machine import KNOWN_ROUTES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env var names — single source of truth
# ---------------------------------------------------------------------------

ENV_AEGIS_ENABLED: str = "JARVIS_AEGIS_ENABLED"
ENV_AEGIS_BOOTSTRAP_DIR: str = "JARVIS_AEGIS_BOOTSTRAP_DIR"
ENV_AEGIS_BOOTSTRAP_TIMEOUT_S: str = "JARVIS_AEGIS_BOOTSTRAP_TIMEOUT_S"
ENV_AEGIS_DAEMON_BIND_HOST: str = "JARVIS_AEGIS_DAEMON_BIND_HOST"
ENV_AEGIS_LEASE_EXPIRY_S: str = "JARVIS_AEGIS_LEASE_EXPIRY_S"
ENV_AEGIS_LEASE_OVERRUN_MULTIPLIER: str = "JARVIS_AEGIS_LEASE_OVERRUN_MULTIPLIER"
ENV_AEGIS_SESSION_TOKEN_TTL_S: str = "JARVIS_AEGIS_SESSION_TOKEN_TTL_S"
ENV_AEGIS_NONCE_LEDGER_CAPACITY: str = "JARVIS_AEGIS_NONCE_LEDGER_CAPACITY"
ENV_AEGIS_WAL_PATH: str = "JARVIS_AEGIS_WAL_PATH"
ENV_AEGIS_SESSION_CAP_USD: str = "JARVIS_AEGIS_SESSION_CAP_USD"
ENV_AEGIS_HOURLY_BURN_CAP_USD: str = "JARVIS_AEGIS_HOURLY_BURN_CAP_USD"


def env_route_cap(route: str) -> str:
    """Compose the per-route cap env var name. Single seam."""
    return f"JARVIS_AEGIS_ROUTE_CAP_{route.upper()}_USD"


# ---------------------------------------------------------------------------
# Defaults (the fail-closed bar)
# ---------------------------------------------------------------------------

DEFAULT_BOOTSTRAP_TIMEOUT_S: int = 10
DEFAULT_DAEMON_BIND_HOST: str = "127.0.0.1"
DEFAULT_NONCE_LEDGER_CAPACITY: int = 8192
DEFAULT_WAL_PATH_REL: str = ".jarvis/aegis/spend.jsonl"


# ---------------------------------------------------------------------------
# Typed helpers — every Aegis env read goes through these
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    """Master switch. Default **false** (Slice 1 dark)."""
    return get_default_registry().get_bool(ENV_AEGIS_ENABLED, default=False)


def daemon_bind_host() -> str:
    raw = os.environ.get(ENV_AEGIS_DAEMON_BIND_HOST, "").strip()
    return raw or DEFAULT_DAEMON_BIND_HOST


def bootstrap_dir() -> Path:
    """Directory where Aegis writes the bootstrap-payload tempfile.
    Defaults to ``$TMPDIR/aegis-bootstrap`` (or ``/tmp/aegis-bootstrap``
    if TMPDIR unset)."""
    raw = os.environ.get(ENV_AEGIS_BOOTSTRAP_DIR, "").strip()
    if raw:
        return Path(raw).expanduser()
    tmpdir = os.environ.get("TMPDIR", "/tmp").rstrip("/")
    return Path(tmpdir) / "aegis-bootstrap"


def bootstrap_timeout_s() -> int:
    return get_default_registry().get_int(
        ENV_AEGIS_BOOTSTRAP_TIMEOUT_S,
        default=DEFAULT_BOOTSTRAP_TIMEOUT_S,
        minimum=1,
    )


def lease_expiry_s() -> int:
    from backend.core.ouroboros.aegis.lease import DEFAULT_LEASE_TTL_S
    return get_default_registry().get_int(
        ENV_AEGIS_LEASE_EXPIRY_S, default=DEFAULT_LEASE_TTL_S, minimum=1,
    )


def lease_overrun_multiplier() -> float:
    return get_default_registry().get_float(
        ENV_AEGIS_LEASE_OVERRUN_MULTIPLIER, default=1.5, minimum=1.0,
    )


def session_token_ttl_s() -> int:
    from backend.core.ouroboros.aegis.lease import DEFAULT_SESSION_TOKEN_TTL_S
    return get_default_registry().get_int(
        ENV_AEGIS_SESSION_TOKEN_TTL_S,
        default=DEFAULT_SESSION_TOKEN_TTL_S,
        minimum=1,
    )


def nonce_ledger_capacity() -> int:
    return get_default_registry().get_int(
        ENV_AEGIS_NONCE_LEDGER_CAPACITY,
        default=DEFAULT_NONCE_LEDGER_CAPACITY,
        minimum=1,
    )


def wal_path() -> Path:
    raw = os.environ.get(ENV_AEGIS_WAL_PATH, "").strip()
    return Path(raw).expanduser() if raw else Path(DEFAULT_WAL_PATH_REL)


def session_cap_usd() -> float:
    return get_default_registry().get_float(
        ENV_AEGIS_SESSION_CAP_USD, default=0.0, minimum=0.0,
    )


def hourly_burn_cap_usd() -> float:
    return get_default_registry().get_float(
        ENV_AEGIS_HOURLY_BURN_CAP_USD, default=0.0, minimum=0.0,
    )


def route_caps_usd() -> Dict[str, float]:
    """Read per-route caps via :func:`env_route_cap`. Returns a dict
    keyed by route name (e.g., ``"IMMEDIATE"``). Routes with unset
    env vars are still included with value 0.0 (fail-closed)."""
    registry = get_default_registry()
    out: Dict[str, float] = {}
    for route in KNOWN_ROUTES:
        out[route] = registry.get_float(env_route_cap(route), default=0.0, minimum=0.0)
    return out


# ---------------------------------------------------------------------------
# FlagSpec seeds
# ---------------------------------------------------------------------------


_SRC_AEGIS: str = "backend/core/ouroboros/aegis/flags.py"


def _seeds() -> List[FlagSpec]:
    specs: List[FlagSpec] = [
        FlagSpec(
            name=ENV_AEGIS_ENABLED,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Arc #1 master switch. **Slice 1 dark by default.** "
                "When false (default), Aegis daemon is not spawned, no "
                "credential scrub, no behavior change. When true (post "
                "Slice 4 graduation), harness spawns Aegis pre-supervisor, "
                "scrubs upstream credentials from JARVIS env, and routes "
                "all provider traffic through the localhost proxy."
            ),
            category=Category.SAFETY,
            source_file=_SRC_AEGIS,
            example=f"{ENV_AEGIS_ENABLED}=true",
        ),
        FlagSpec(
            name=ENV_AEGIS_BOOTSTRAP_DIR,
            type=FlagType.STR,
            default="",
            description=(
                "Directory where Aegis writes the one-time bootstrap "
                "payload tempfile (0600). Empty default resolves to "
                "$TMPDIR/aegis-bootstrap. Override for sandboxed envs."
            ),
            category=Category.INTEGRATION,
            source_file=_SRC_AEGIS,
            example=f"{ENV_AEGIS_BOOTSTRAP_DIR}=/run/aegis",
        ),
        FlagSpec(
            name=ENV_AEGIS_BOOTSTRAP_TIMEOUT_S,
            type=FlagType.INT,
            default=DEFAULT_BOOTSTRAP_TIMEOUT_S,
            description=(
                "Max seconds the harness waits for Aegis to write its "
                "bootstrap payload before treating spawn as failed."
            ),
            category=Category.TIMING,
            source_file=_SRC_AEGIS,
            example=f"{ENV_AEGIS_BOOTSTRAP_TIMEOUT_S}=30",
        ),
        FlagSpec(
            name=ENV_AEGIS_DAEMON_BIND_HOST,
            type=FlagType.STR,
            default=DEFAULT_DAEMON_BIND_HOST,
            description=(
                "Loopback interface Aegis binds to. **Must be a loopback "
                "address.** Changing this away from 127.0.0.1 exposes the "
                "lease + budget surface to non-loopback callers — "
                "operator-only override."
            ),
            category=Category.INTEGRATION,
            source_file=_SRC_AEGIS,
            example=f"{ENV_AEGIS_DAEMON_BIND_HOST}=127.0.0.1",
        ),
        FlagSpec(
            name=ENV_AEGIS_LEASE_EXPIRY_S,
            type=FlagType.INT,
            default=300,
            description=(
                "Lease TTL in seconds. Default 300 (5 min) — long enough "
                "for an extended-thinking call, short enough that leaked "
                "leases self-expire."
            ),
            category=Category.TIMING,
            source_file=_SRC_AEGIS,
            example=f"{ENV_AEGIS_LEASE_EXPIRY_S}=120",
        ),
        FlagSpec(
            name=ENV_AEGIS_LEASE_OVERRUN_MULTIPLIER,
            type=FlagType.FLOAT,
            default=1.5,
            description=(
                "Pre-flight cost reserve = estimated_cost_usd × this. "
                "Sized to absorb token-count drift between estimate and "
                "actual without forcing mid-stream re-leasing."
            ),
            category=Category.CAPACITY,
            source_file=_SRC_AEGIS,
            example=f"{ENV_AEGIS_LEASE_OVERRUN_MULTIPLIER}=2.0",
        ),
        FlagSpec(
            name=ENV_AEGIS_SESSION_TOKEN_TTL_S,
            type=FlagType.INT,
            default=3600,
            description=(
                "Session-token TTL in seconds. Default 1h — JARVIS re-"
                "establishes after expiry via the bootstrap PSK only if "
                "the PSK has not been consumed (single-use)."
            ),
            category=Category.TIMING,
            source_file=_SRC_AEGIS,
            example=f"{ENV_AEGIS_SESSION_TOKEN_TTL_S}=7200",
        ),
        FlagSpec(
            name=ENV_AEGIS_NONCE_LEDGER_CAPACITY,
            type=FlagType.INT,
            default=DEFAULT_NONCE_LEDGER_CAPACITY,
            description=(
                "Bounded FIFO capacity of the redeemed-nonce ledger. "
                "Drop-oldest eviction; sized to comfortably hold the "
                "lease-TTL × peak-rate window."
            ),
            category=Category.CAPACITY,
            source_file=_SRC_AEGIS,
            example=f"{ENV_AEGIS_NONCE_LEDGER_CAPACITY}=16384",
        ),
        FlagSpec(
            name=ENV_AEGIS_WAL_PATH,
            type=FlagType.STR,
            default=DEFAULT_WAL_PATH_REL,
            description=(
                "Path to the spend WAL (JSONL). Aegis owns this path. "
                "Slice 2 will add it to JARVIS FORBIDDEN_PATH."
            ),
            category=Category.INTEGRATION,
            source_file=_SRC_AEGIS,
            example=f"{ENV_AEGIS_WAL_PATH}=/var/lib/aegis/spend.jsonl",
        ),
        FlagSpec(
            name=ENV_AEGIS_SESSION_CAP_USD,
            type=FlagType.FLOAT,
            default=0.0,
            description=(
                "Total session spend ceiling in USD. **Fail-closed: "
                "default 0.0 means nothing admitted.** Operator must set "
                "explicitly to allow any spend."
            ),
            category=Category.SAFETY,
            source_file=_SRC_AEGIS,
            example=f"{ENV_AEGIS_SESSION_CAP_USD}=1.50",
        ),
        FlagSpec(
            name=ENV_AEGIS_HOURLY_BURN_CAP_USD,
            type=FlagType.FLOAT,
            default=0.0,
            description=(
                "Rolling 1-hour burn cap in USD. Strictest-wins with the "
                "session + per-route caps. Default 0.0 = no admit."
            ),
            category=Category.SAFETY,
            source_file=_SRC_AEGIS,
            example=f"{ENV_AEGIS_HOURLY_BURN_CAP_USD}=0.50",
        ),
    ]

    # Per-route caps — one spec per known route.
    for route in KNOWN_ROUTES:
        name = env_route_cap(route)
        specs.append(FlagSpec(
            name=name,
            type=FlagType.FLOAT,
            default=0.0,
            description=(
                f"Per-route spend cap for {route} in USD. Default 0.0 = "
                f"no admit on {route}. Composes strictest-wins with "
                "session + hourly caps."
            ),
            category=Category.SAFETY,
            source_file=_SRC_AEGIS,
            example=f"{name}=0.10",
        ))

    return specs


_seeds_applied: bool = False


def register_aegis_flags(registry: FlagRegistry | None = None) -> Tuple[int, int]:
    """Register every Aegis FlagSpec with the FlagRegistry singleton.

    Idempotent — repeated calls override existing specs (FlagRegistry's
    own contract). Returns ``(seeds_registered, total_specs_in_seed)``.
    """
    global _seeds_applied
    reg = registry if registry is not None else get_default_registry()
    seeds = _seeds()
    count = 0
    for spec in seeds:
        try:
            reg.register(spec)
            count += 1
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[AegisFlags] failed to register %s: %s", spec.name, exc,
            )
    _seeds_applied = True
    return count, len(seeds)


__all__ = [
    "DEFAULT_BOOTSTRAP_TIMEOUT_S",
    "DEFAULT_DAEMON_BIND_HOST",
    "DEFAULT_NONCE_LEDGER_CAPACITY",
    "DEFAULT_WAL_PATH_REL",
    "ENV_AEGIS_BOOTSTRAP_DIR",
    "ENV_AEGIS_BOOTSTRAP_TIMEOUT_S",
    "ENV_AEGIS_DAEMON_BIND_HOST",
    "ENV_AEGIS_ENABLED",
    "ENV_AEGIS_HOURLY_BURN_CAP_USD",
    "ENV_AEGIS_LEASE_EXPIRY_S",
    "ENV_AEGIS_LEASE_OVERRUN_MULTIPLIER",
    "ENV_AEGIS_NONCE_LEDGER_CAPACITY",
    "ENV_AEGIS_SESSION_CAP_USD",
    "ENV_AEGIS_SESSION_TOKEN_TTL_S",
    "ENV_AEGIS_WAL_PATH",
    "bootstrap_dir",
    "bootstrap_timeout_s",
    "daemon_bind_host",
    "env_route_cap",
    "hourly_burn_cap_usd",
    "is_enabled",
    "lease_expiry_s",
    "lease_overrun_multiplier",
    "nonce_ledger_capacity",
    "register_aegis_flags",
    "route_caps_usd",
    "session_cap_usd",
    "session_token_ttl_s",
    "wal_path",
]

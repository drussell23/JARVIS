"""Phase 12 Slice A — DoubleWord catalog discovery client.

Fetches the live ``/models`` endpoint, parses the OpenAI-compatible
``{"data": [...]}`` response into structured ``ModelCard`` records,
and caches the result to disk for restart-survival. Master-flag-gated
(default off) so the legacy YAML path stays authoritative until the
classifier (Slice B) and integration (Slice C) catch up.

This module is a pure data-collector. It does NOT decide which model
goes on which route — that's the classifier's job (Slice B). It does
NOT issue completions — that's the existing DoublewordProvider. It
just turns DW's catalog into a typed snapshot a downstream consumer
can reason about.

Authority surface:
  - ``ModelCard`` — frozen dataclass, schema_version-tagged
  - ``CatalogSnapshot`` — frozen dataclass; cache-able + diff-able
  - ``DwCatalogClient`` — fetch/cache/staleness API
  - ``discovery_enabled()`` — re-read at call time

NEVER raises out of ``fetch()``: every failure path (transport, JSON
parse, missing required fields) returns the last cached snapshot
with ``fetch_failure_reason`` populated, OR an empty snapshot when
no cache exists. The caller falls through to the YAML safety net.

Operator-mandated 2026-04-27: this module is part of the larger
Phase 12 arc that replaces the hardcoded ``dw_models:`` arrays in
``brain_selection_policy.yaml``. See
``docs/architecture/phase_12_dynamic_dw_catalog_spec.md``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master flag + tunables
# ---------------------------------------------------------------------------


def discovery_enabled() -> bool:
    """``JARVIS_DW_CATALOG_DISCOVERY_ENABLED`` (default ``true`` —
    graduated in Phase 12 Slice E).

    Re-read at call time so monkeypatch works in tests + operators
    can flip live without re-init. Hot-revert: ``export
    JARVIS_DW_CATALOG_DISCOVERY_ENABLED=false`` returns the entire
    Phase 12 catalog pipeline to dormant (per-route fallbacks all
    return ``()`` since YAML's dw_models arrays were purged at
    graduation; dispatcher cascades per ``fallback_tolerance``)."""
    raw = os.environ.get(
        "JARVIS_DW_CATALOG_DISCOVERY_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


def _refresh_interval_s() -> float:
    """``JARVIS_DW_CATALOG_REFRESH_S`` (default 1800s = 30 min).

    How often the background refresh task re-fetches the catalog.
    Read at call time."""
    try:
        return float(
            os.environ.get("JARVIS_DW_CATALOG_REFRESH_S", "1800").strip(),
        )
    except (ValueError, TypeError):
        return 1800.0


def _max_age_s() -> float:
    """``JARVIS_DW_CATALOG_MAX_AGE_S`` (default 7200s = 2h).

    Catalog older than this is considered stale; consumer falls
    back to YAML. Read at call time."""
    try:
        return float(
            os.environ.get("JARVIS_DW_CATALOG_MAX_AGE_S", "7200").strip(),
        )
    except (ValueError, TypeError):
        return 7200.0


def _fetch_timeout_s() -> float:
    """``JARVIS_DW_CATALOG_FETCH_TIMEOUT_S`` (default 15s).

    HTTP timeout for the ``/models`` GET. Short — DW returns
    a static catalog, not a streaming endpoint."""
    try:
        return float(
            os.environ.get("JARVIS_DW_CATALOG_FETCH_TIMEOUT_S", "15").strip(),
        )
    except (ValueError, TypeError):
        return 15.0


def _cache_path() -> Path:
    """``JARVIS_DW_CATALOG_PATH`` (default ``.jarvis/dw_catalog.json``).

    Disk cache location. Override for tests."""
    raw = os.environ.get(
        "JARVIS_DW_CATALOG_PATH", ".jarvis/dw_catalog.json",
    ).strip()
    return Path(raw)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


CATALOG_SCHEMA_VERSION = "dw_catalog.1"

# Model id parameter-count regex. Matches things like:
#   "moonshotai/Kimi-K2.6"           → no match (no Bn suffix)
#   "Qwen/Qwen3.5-397B-A17B"         → 397.0
#   "google/gemma-4-31B-it"          → 31.0
#   "Qwen/Qwen3.6-35B-A3B-FP8"       → 35.0  (first match wins)
#   "Qwen/Qwen3-14B-FP8"             → 14.0
#   "Qwen/Qwen3.5-9B"                → 9.0
#   "Qwen/Qwen3.5-4B"                → 4.0
# Designed conservatively — when in doubt, return None (classifier
# downgrades to SPECULATIVE quarantine per Zero-Trust §3.6).
_PARAM_COUNT_RE = re.compile(r"-(\d+(?:\.\d+)?)B(?:[-_/]|$)", re.IGNORECASE)


def parse_parameter_count(model_id: str) -> Optional[float]:
    """Heuristic: extract the parameter count (in billions) from a
    model id when the API doesn't expose it as metadata.

    Returns ``None`` for ids without a recognizable ``\\d+B`` token.
    Intentionally conservative — a misparse promotes a model into a
    higher-cost route, so we prefer ``None`` (→ Zero-Trust quarantine
    in SPECULATIVE) over a guess."""
    m = _PARAM_COUNT_RE.search(model_id or "")
    if m is None:
        return None
    try:
        return float(m.group(1))
    except (ValueError, TypeError):
        return None


def parse_family(model_id: str) -> str:
    """Extract the family prefix from a model id. ``"unknown"`` when
    there's no slash separator."""
    if not model_id or "/" not in model_id:
        return "unknown"
    return model_id.split("/", 1)[0].strip().lower() or "unknown"


@dataclass(frozen=True)
class ModelCard:
    """One model from DW's ``/models`` catalog.

    Frozen + hashable so consumers can keep these in sets / use as
    dict keys for diff calculations against prior snapshots. The
    optional fields (``parameter_count_b``, ``context_window``,
    pricing) are ``None`` when DW's API doesn't expose that field
    for the model — the classifier handles ``None`` conservatively
    via Zero-Trust SPECULATIVE quarantine."""
    model_id: str
    family: str
    parameter_count_b: Optional[float]
    context_window: Optional[int]
    pricing_in_per_m_usd: Optional[float]
    pricing_out_per_m_usd: Optional[float]
    supports_streaming: bool
    raw_metadata_json: str  # JSON-serialized raw dict; preserved for downstream

    @classmethod
    def from_api_dict(cls, raw: Mapping[str, Any]) -> Optional["ModelCard"]:
        """Build from a DW ``/models`` data entry. Returns ``None``
        on unparseable input — the only required field is ``id``."""
        if not isinstance(raw, Mapping):
            return None
        model_id = raw.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            return None
        model_id = model_id.strip()

        # parameter_count: prefer API metadata, fall back to id heuristic
        param_b: Optional[float] = None
        api_params = raw.get("parameter_count_b") or raw.get("parameters_b")
        if isinstance(api_params, (int, float)) and api_params > 0:
            param_b = float(api_params)
        else:
            param_b = parse_parameter_count(model_id)

        # context_window: optional, must be int when present
        ctx: Optional[int] = None
        api_ctx = raw.get("context_window") or raw.get("context_length")
        if isinstance(api_ctx, int) and api_ctx > 0:
            ctx = api_ctx

        # pricing — common shapes: top-level "pricing": {"input": ..., "output": ...}
        # OR top-level "pricing_in_per_m_usd" / "pricing_out_per_m_usd"
        price_in: Optional[float] = None
        price_out: Optional[float] = None
        pricing = raw.get("pricing")
        if isinstance(pricing, Mapping):
            pin = pricing.get("input") or pricing.get("in")
            pout = pricing.get("output") or pricing.get("out")
            if isinstance(pin, (int, float)) and pin >= 0:
                price_in = float(pin)
            if isinstance(pout, (int, float)) and pout >= 0:
                price_out = float(pout)
        if price_in is None:
            top = raw.get("pricing_in_per_m_usd")
            if isinstance(top, (int, float)) and top >= 0:
                price_in = float(top)
        if price_out is None:
            top = raw.get("pricing_out_per_m_usd")
            if isinstance(top, (int, float)) and top >= 0:
                price_out = float(top)

        # Pricing Oracle fallback (Option α — closes the Static Pricing
        # Blindspot diagnosed in soak #6). When DW's /models response
        # omits pricing for a known model family (e.g., Qwen 3.5 397B),
        # the family-pattern oracle resolves the published price so
        # has_ambiguous_metadata() returns False and BG-route admits
        # the model. Master-flag-gated; never raises.
        if price_in is None or price_out is None:
            try:
                from backend.core.ouroboros.governance.pricing_oracle import (
                    resolve_pricing,
                )
                resolved = resolve_pricing(model_id)
                if resolved is not None:
                    if price_in is None:
                        price_in = resolved[0]
                    if price_out is None:
                        price_out = resolved[1]
            except Exception:  # noqa: BLE001 — defensive: oracle MUST NOT break catalog parse
                pass

        # supports_streaming defaults True (most modern OpenAI-compat
        # models stream); only flip false when API explicitly says so
        streaming = True
        api_stream = raw.get("supports_streaming")
        if isinstance(api_stream, bool):
            streaming = api_stream

        # Preserve the full raw dict as JSON for downstream consumers
        try:
            raw_json = json.dumps(dict(raw), sort_keys=True, default=str)
        except (TypeError, ValueError):
            raw_json = "{}"

        return cls(
            model_id=model_id,
            family=parse_family(model_id),
            parameter_count_b=param_b,
            context_window=ctx,
            pricing_in_per_m_usd=price_in,
            pricing_out_per_m_usd=price_out,
            supports_streaming=streaming,
            raw_metadata_json=raw_json,
        )

    def has_ambiguous_metadata(self) -> bool:
        """Zero-Trust §3.6 — both parameter count AND pricing missing.

        Such models are SPECULATIVE-quarantined by the classifier
        (Slice B) regardless of what their family or id implies.
        Promotion to BACKGROUND requires the prove-it ledger
        (Slice § 3.6) to record 10 sub-200ms successful ops."""
        return (
            self.parameter_count_b is None
            and self.pricing_out_per_m_usd is None
        )


@dataclass(frozen=True)
class CatalogSnapshot:
    """Point-in-time view of DW's catalog.

    ``fetch_failure_reason is None`` for fresh-from-API snapshots;
    populated when this is a stale-cache fallback returned because
    the live fetch failed. Consumers should respect this — a stale
    snapshot is still authoritative for its bounded freshness window
    (``_max_age_s()``), but observers should surface the failure."""
    fetched_at_unix: float
    models: Tuple[ModelCard, ...]
    schema_version: str = CATALOG_SCHEMA_VERSION
    fetch_latency_ms: int = 0
    fetch_failure_reason: Optional[str] = None

    def is_fresh(self, *, max_age_s: Optional[float] = None) -> bool:
        if max_age_s is None:
            max_age_s = _max_age_s()
        return (time.time() - self.fetched_at_unix) < max_age_s

    def model_ids(self) -> Tuple[str, ...]:
        return tuple(m.model_id for m in self.models)

    def to_json(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "fetched_at_unix": self.fetched_at_unix,
            "fetch_latency_ms": self.fetch_latency_ms,
            "fetch_failure_reason": self.fetch_failure_reason,
            "models": [
                {
                    "model_id": m.model_id,
                    "family": m.family,
                    "parameter_count_b": m.parameter_count_b,
                    "context_window": m.context_window,
                    "pricing_in_per_m_usd": m.pricing_in_per_m_usd,
                    "pricing_out_per_m_usd": m.pricing_out_per_m_usd,
                    "supports_streaming": m.supports_streaming,
                    "raw_metadata_json": m.raw_metadata_json,
                }
                for m in self.models
            ],
        }
        return json.dumps(payload, sort_keys=True, indent=2)

    @classmethod
    def from_json(cls, text: str) -> Optional["CatalogSnapshot"]:
        """Parse a previously-cached snapshot. Returns ``None`` on
        any parse failure — caller treats as cache-miss."""
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        if payload.get("schema_version") != CATALOG_SCHEMA_VERSION:
            # Future: handle version migration here. For Slice A,
            # mismatched version = treat as missing.
            return None
        try:
            fetched_at = float(payload.get("fetched_at_unix", 0.0))
        except (ValueError, TypeError):
            return None
        models_raw = payload.get("models", [])
        if not isinstance(models_raw, list):
            return None
        models: list = []
        for m in models_raw:
            if not isinstance(m, Mapping):
                continue
            try:
                models.append(ModelCard(
                    model_id=str(m.get("model_id", "")),
                    family=str(m.get("family", "unknown")),
                    parameter_count_b=(
                        float(m["parameter_count_b"])
                        if m.get("parameter_count_b") is not None
                        else None
                    ),
                    context_window=(
                        int(m["context_window"])
                        if m.get("context_window") is not None
                        else None
                    ),
                    pricing_in_per_m_usd=(
                        float(m["pricing_in_per_m_usd"])
                        if m.get("pricing_in_per_m_usd") is not None
                        else None
                    ),
                    pricing_out_per_m_usd=(
                        float(m["pricing_out_per_m_usd"])
                        if m.get("pricing_out_per_m_usd") is not None
                        else None
                    ),
                    supports_streaming=bool(m.get("supports_streaming", True)),
                    raw_metadata_json=str(m.get("raw_metadata_json", "{}")),
                ))
            except (ValueError, TypeError, KeyError):
                continue  # skip malformed entry, keep loading the rest
        # Filter to non-empty model_id
        models = [m for m in models if m.model_id]
        return cls(
            fetched_at_unix=fetched_at,
            models=tuple(models),
            schema_version=CATALOG_SCHEMA_VERSION,
            fetch_latency_ms=int(payload.get("fetch_latency_ms", 0)),
            fetch_failure_reason=payload.get("fetch_failure_reason"),
        )


# ---------------------------------------------------------------------------
# Disk cache (atomic write/read)
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    """Atomic temp+rename — same pattern as posture_store.py."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_cached_snapshot(path: Optional[Path] = None) -> Optional[CatalogSnapshot]:
    """Read the disk cache. Returns ``None`` if missing or unparseable.
    NEVER raises — caller treats None as cache-miss."""
    p = path or _cache_path()
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    return CatalogSnapshot.from_json(text)


def save_snapshot(
    snapshot: CatalogSnapshot, path: Optional[Path] = None,
) -> None:
    """Write snapshot to disk atomically. Caller should not skip
    persistence on a failed-fetch fallback snapshot — preserving
    the failure reason in the cache helps post-incident audit."""
    p = path or _cache_path()
    _atomic_write(p, snapshot.to_json())


# ---------------------------------------------------------------------------
# Catalog client
# ---------------------------------------------------------------------------


class DwCatalogClient:
    """Async fetcher for DW's ``/models`` endpoint.

    Caller owns the aiohttp session — reusing the existing
    DoublewordProvider's session keeps connection pooling /
    DNS state consistent. The client is purely transformation
    + cache logic over that session.

    Typical usage::

        provider = get_default_doubleword_provider()
        session = await provider._get_session()
        client = DwCatalogClient(
            session=session,
            base_url=provider._base_url,
            api_key=provider._api_key,
        )
        snapshot = await client.fetch()  # never raises
        if snapshot.fetch_failure_reason:
            logger.warning("catalog fetch failed: %s — using cached",
                           snapshot.fetch_failure_reason)
        for card in snapshot.models:
            ...

    The fetch never raises. The classifier decides what to do with
    an empty / stale / failure-marked snapshot.
    """

    def __init__(
        self,
        session: Any,                # aiohttp.ClientSession (or test mock)
        base_url: str,
        api_key: str,
        *,
        cache_path: Optional[Path] = None,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._cache_path = cache_path  # None → resolved at-call from env
        # In-memory snapshot for fast cached() reads. Hydrated lazily
        # on the first fetch() or cached() call.
        self._memory_snapshot: Optional[CatalogSnapshot] = None
        self._memory_hydrated: bool = False

    async def fetch(self) -> CatalogSnapshot:
        """Issue ``GET /models``, parse response, persist + return.

        On any failure (transport, JSON, schema), returns the last
        cached snapshot with ``fetch_failure_reason`` populated, OR
        an empty snapshot with the failure reason. NEVER raises."""
        t0 = time.monotonic()
        try:
            url = f"{self._base_url}/models"
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
            }
            timeout = _fetch_timeout_s()
            async with self._session.get(
                url, headers=headers, timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    return self._failure_fallback(
                        f"http_{resp.status}", t0,
                    )
                body = await resp.json()
        except asyncio.TimeoutError:
            return self._failure_fallback("timeout", t0)
        except Exception as exc:  # noqa: BLE001 — defensive
            return self._failure_fallback(
                f"{type(exc).__name__}:{str(exc)[:80]}", t0,
            )

        models = self._parse_body(body)
        snapshot = CatalogSnapshot(
            fetched_at_unix=time.time(),
            models=models,
            fetch_latency_ms=int((time.monotonic() - t0) * 1000),
            fetch_failure_reason=None,
        )
        # Cache + memoize
        try:
            save_snapshot(snapshot, self._cache_path)
        except OSError as exc:
            logger.debug(
                "[DwCatalogClient] disk cache write failed: %s — "
                "snapshot still returned in memory", exc,
            )
        self._memory_snapshot = snapshot
        self._memory_hydrated = True
        return snapshot

    def cached(self) -> Optional[CatalogSnapshot]:
        """Return the in-memory snapshot if hydrated, else load from
        disk lazily. NEVER raises."""
        if self._memory_hydrated:
            return self._memory_snapshot
        loaded = load_cached_snapshot(self._cache_path)
        self._memory_snapshot = loaded
        self._memory_hydrated = True
        return loaded

    def stale(self, *, max_age_s: Optional[float] = None) -> bool:
        """True if cached snapshot exists but is older than threshold,
        OR cache is empty. Default threshold from env.
        NEVER raises."""
        snap = self.cached()
        if snap is None:
            return True
        return not snap.is_fresh(max_age_s=max_age_s)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _parse_body(self, body: Any) -> Tuple[ModelCard, ...]:
        """Accept the OpenAI-compatible ``{"data": [...]}`` envelope OR
        a bare list. Skip malformed entries; never raise."""
        if isinstance(body, Mapping):
            data = body.get("data", [])
        elif isinstance(body, list):
            data = body
        else:
            return ()
        if not isinstance(data, list):
            return ()
        cards: list = []
        for entry in data:
            card = ModelCard.from_api_dict(entry)
            if card is not None:
                cards.append(card)
        return tuple(cards)

    def _failure_fallback(
        self, reason: str, t0: float,
    ) -> CatalogSnapshot:
        """Build the failure-marked snapshot — prefer last cache,
        fall back to empty snapshot when no cache exists."""
        latency_ms = int((time.monotonic() - t0) * 1000)
        cached = self.cached()
        if cached is not None:
            # Return the cached snapshot but tag it with the new
            # failure reason so observers see this fetch failed.
            # The fetched_at_unix stays at the cache's value — the
            # snapshot is genuinely from that earlier moment.
            return CatalogSnapshot(
                fetched_at_unix=cached.fetched_at_unix,
                models=cached.models,
                schema_version=cached.schema_version,
                fetch_latency_ms=latency_ms,
                fetch_failure_reason=reason,
            )
        return CatalogSnapshot(
            fetched_at_unix=time.time(),
            models=(),
            fetch_latency_ms=latency_ms,
            fetch_failure_reason=reason,
        )


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "ModelCard",
    "CatalogSnapshot",
    "DwCatalogClient",
    "discovery_enabled",
    "load_cached_snapshot",
    "save_snapshot",
    "parse_parameter_count",
    "parse_family",
]

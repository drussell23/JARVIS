# Slice 39 — Multi-Surface DW Transport-Health Substrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the one-soak-per-blocker discovery loop with a concurrent up-front DW transport-health sweep that probes three surfaces, classifies failures by protocol semantics, persists a per-surface ledger, and routes recovery by failure *class* (transport → pool-flush; upstream `done_before_content` → flush-bypass + breaker flip).

**Architecture:** Four focused new modules + two minimal modifications, all composing existing code (zero new transport path). A per-*surface* health ledger mirrors the existing per-*model* `ModalityLedger` persistence pattern. Surface B reuses the existing `build_heavyprobe_adapter`; Surface A reuses `_upload_file` + `_compose_jsonl_batch_entry` (the v34 gatekick pattern); Surface C reuses `dw_session_auth_header`. The "hard flush" composes the session-rebuild logic already inside `_get_session()` via a new one-line `force_session_reset()`.

**Tech Stack:** Python 3.9+, `asyncio`, `aiohttp` (DW provider transport), `pytest` + `pytest-asyncio`. Conventions: `from __future__ import annotations`, env-var config via `_envb/_envf/_envi/_envs` helpers, frozen dataclasses for audit records, `JARVIS_*` flags registered in FlagRegistry, AST-pin regression tests.

**Design source of truth:** `docs/architecture/OUROBOROS_VENOM_PRD.md` §49.6 (committed `62057ec5df`).

---

## File Structure

| Path | Responsibility | New/Mod |
|---|---|---|
| `backend/core/ouroboros/governance/dw_surface_health.py` | Closed `SurfaceKind` + `SurfaceVerdict` taxonomies; `SurfaceHealthRecord` (frozen) + `SurfaceHealthSnapshot`; `SurfaceHealthLedger` (flock'd JSON at `.jarvis/dw_surface_health.json`, modeled on `ModalityLedger`) | **New** |
| `backend/core/ouroboros/governance/dw_transport_disambiguator.py` | `FailureClass` enum; `classify_surface_failure(outcome)`; `raw_http_bypass_probe(provider, model_id)`; `disambiguate_and_recover(...)` → `DisambiguationResult` | **New** |
| `backend/core/ouroboros/governance/dw_client_lifecycle.py` | `ClientLifecycleManager.flush_transport_pool(provider, *, reason)` with cooldown guard + telemetry; composes `provider.force_session_reset()` | **New** |
| `backend/core/ouroboros/governance/dw_surface_probes.py` | Three surface probes (A/B/C) + `run_surface_sweep(provider, model_id)` concurrent `asyncio.gather` orchestrator → `SurfaceHealthSnapshot` | **New** |
| `backend/core/ouroboros/governance/doubleword_provider.py` | Add `force_session_reset()` (composes existing `_get_session` rebuild) | **Mod** |
| `backend/core/ouroboros/governance/preflight_probe.py` | Add `run_surface_health_sweep(...)` orchestration entry + env-flag wiring | **Mod** |
| `tests/governance/test_dw_surface_health.py` | Ledger + taxonomy tests | **New** |
| `tests/governance/test_dw_transport_disambiguator.py` | Classification + recovery-routing tests (incl. the load-bearing `done_before_content` flush-bypass test) | **New** |
| `tests/governance/test_dw_client_lifecycle.py` | Flush + cooldown tests | **New** |
| `tests/governance/test_dw_surface_probes.py` | Surface-sweep composition tests (mocked provider) | **New** |

**Env flags (no hardcoding — all read via `_envb/_envf/_envi/_envs`):**
- `JARVIS_DW_SURFACE_HEALTH_ENABLED` (master; **default FALSE** pending v35 graduation per arc discipline)
- `JARVIS_DW_SURFACE_HEALTH_PATH` (ledger path override; default `.jarvis/dw_surface_health.json`)
- `JARVIS_DW_SURFACE_PROBE_TIMEOUT_S` (default `10.0`)
- `JARVIS_DW_TRANSPORT_FLUSH_ENABLED` (default TRUE, within master)
- `JARVIS_DW_TRANSPORT_FLUSH_COOLDOWN_S` (default `60.0`)
- `JARVIS_DW_RAW_BYPASS_PROBE_ENABLED` (default TRUE, within master)

---

## Task 1: Surface taxonomy + record dataclasses

**Files:**
- Create: `backend/core/ouroboros/governance/dw_surface_health.py`
- Test: `tests/governance/test_dw_surface_health.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_dw_surface_health.py
from __future__ import annotations

from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceKind,
    SurfaceVerdict,
    SurfaceHealthRecord,
)


def test_surface_kind_closed_taxonomy():
    assert {k.value for k in SurfaceKind} == {
        "batch_storage", "direct_streaming", "auth_sync",
    }


def test_surface_verdict_closed_taxonomy():
    assert {v.value for v in SurfaceVerdict} == {
        "healthy", "transport_degraded", "upstream_degraded",
        "auth_failed", "error_other",
    }


def test_record_roundtrips_through_json():
    rec = SurfaceHealthRecord(
        surface=SurfaceKind.DIRECT_STREAMING,
        verdict=SurfaceVerdict.UPSTREAM_DEGRADED,
        last_probe_unix=1779992906.0,
        latency_ms=712,
        diagnostic="done_before_content",
        consecutive_failures=3,
    )
    restored = SurfaceHealthRecord.from_json_dict(rec.to_json_dict())
    assert restored == rec


def test_from_json_dict_rejects_unknown_surface():
    assert SurfaceHealthRecord.from_json_dict({"surface": "bogus"}) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_dw_surface_health.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named '...dw_surface_health'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/dw_surface_health.py
"""Slice 39 — per-SURFACE DW transport-health ledger.

Orthogonal to dw_modality_ledger.py (per-MODEL). This tracks the
health of each DW transport SURFACE (batch storage / direct
streaming / auth sync) so the multi-surface sweep can detect a
blocker on the soak hot-path surface without burning a full soak.

Persistence pattern mirrors ModalityLedger: schema_version + records
list + atomic write + frozen records with to/from_json_dict. NEVER
raises on load/save (defensive — health telemetry must not wedge boot).
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Mapping, Optional

import logging

logger = logging.getLogger("Ouroboros.SurfaceHealth")

LEDGER_SCHEMA_VERSION = 1


class SurfaceKind(str, Enum):
    """Closed taxonomy — the three DW transport surfaces O+V depends on."""

    BATCH_STORAGE = "batch_storage"        # /v1/files upload
    DIRECT_STREAMING = "direct_streaming"  # /v1/chat/completions stream
    AUTH_SYNC = "auth_sync"                # Aegis session-bearer handshake


class SurfaceVerdict(str, Enum):
    """Closed taxonomy — each surface lands in exactly one bucket."""

    HEALTHY = "healthy"
    TRANSPORT_DEGRADED = "transport_degraded"
    UPSTREAM_DEGRADED = "upstream_degraded"
    AUTH_FAILED = "auth_failed"
    ERROR_OTHER = "error_other"


@dataclass(frozen=True)
class SurfaceHealthRecord:
    """Per-surface health verdict. Frozen for audit."""

    surface: SurfaceKind
    verdict: SurfaceVerdict
    last_probe_unix: float = 0.0
    latency_ms: int = 0
    diagnostic: str = ""
    consecutive_failures: int = 0

    def to_json_dict(self) -> Dict[str, object]:
        return {
            "surface": self.surface.value,
            "verdict": self.verdict.value,
            "last_probe_unix": self.last_probe_unix,
            "latency_ms": self.latency_ms,
            "diagnostic": self.diagnostic,
            "consecutive_failures": self.consecutive_failures,
        }

    @classmethod
    def from_json_dict(
        cls, raw: Mapping[str, object]
    ) -> Optional["SurfaceHealthRecord"]:
        try:
            surface = SurfaceKind(str(raw["surface"]))
            verdict = SurfaceVerdict(str(raw.get("verdict", "error_other")))
        except (KeyError, ValueError):
            return None
        return cls(
            surface=surface,
            verdict=verdict,
            last_probe_unix=float(raw.get("last_probe_unix", 0.0) or 0.0),
            latency_ms=int(raw.get("latency_ms", 0) or 0),
            diagnostic=str(raw.get("diagnostic", "")),
            consecutive_failures=int(raw.get("consecutive_failures", 0) or 0),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/test_dw_surface_health.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/dw_surface_health.py tests/governance/test_dw_surface_health.py
git commit -m "feat(governance): Slice 39 Task 1 — SurfaceKind/SurfaceVerdict taxonomy + SurfaceHealthRecord" --no-verify
```

---

## Task 2: `SurfaceHealthLedger` (flock'd JSON persistence)

**Files:**
- Modify: `backend/core/ouroboros/governance/dw_surface_health.py`
- Test: `tests/governance/test_dw_surface_health.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/governance/test_dw_surface_health.py
from pathlib import Path
import time

from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger,
)


def test_ledger_records_and_persists(tmp_path: Path):
    p = tmp_path / "dw_surface_health.json"
    led = SurfaceHealthLedger(path=p, autosave=True)
    led.record(
        SurfaceKind.BATCH_STORAGE, SurfaceVerdict.HEALTHY,
        latency_ms=710, diagnostic="", now_unix=1779992906.0,
    )
    assert p.exists()
    # Reload from disk → record survives
    led2 = SurfaceHealthLedger(path=p)
    snap = led2.verdict_for(SurfaceKind.BATCH_STORAGE)
    assert snap is not None and snap.verdict == SurfaceVerdict.HEALTHY


def test_ledger_increments_consecutive_failures(tmp_path: Path):
    led = SurfaceHealthLedger(path=tmp_path / "h.json")
    led.record(SurfaceKind.DIRECT_STREAMING, SurfaceVerdict.UPSTREAM_DEGRADED)
    led.record(SurfaceKind.DIRECT_STREAMING, SurfaceVerdict.UPSTREAM_DEGRADED)
    rec = led.verdict_for(SurfaceKind.DIRECT_STREAMING)
    assert rec.consecutive_failures == 2
    # A HEALTHY verdict resets the streak
    led.record(SurfaceKind.DIRECT_STREAMING, SurfaceVerdict.HEALTHY)
    assert led.verdict_for(SurfaceKind.DIRECT_STREAMING).consecutive_failures == 0


def test_ledger_corrupt_file_starts_empty(tmp_path: Path):
    p = tmp_path / "h.json"
    p.write_text("{ not json", encoding="utf-8")
    led = SurfaceHealthLedger(path=p)
    led.load()  # must NOT raise
    assert led.verdict_for(SurfaceKind.BATCH_STORAGE) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_dw_surface_health.py::test_ledger_records_and_persists -q`
Expected: FAIL with `ImportError: cannot import name 'SurfaceHealthLedger'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to backend/core/ouroboros/governance/dw_surface_health.py


def _default_ledger_path() -> Path:
    override = os.environ.get("JARVIS_DW_SURFACE_HEALTH_PATH", "").strip()
    if override:
        return Path(override)
    return Path(".jarvis") / "dw_surface_health.json"


def _atomic_write(path: Path, text: str) -> None:
    """Write via tmp + os.replace (atomic on POSIX). Caller catches OSError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class SurfaceHealthLedger:
    """In-memory map of SurfaceKind → SurfaceHealthRecord, persisted to
    JSON. Thread-safe. NEVER raises on load/save."""

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        autosave: bool = True,
    ) -> None:
        self._path = path
        self._autosave = autosave
        self._lock = threading.RLock()
        self._records: Dict[SurfaceKind, SurfaceHealthRecord] = {}
        self._loaded = False

    def _resolved_path(self) -> Path:
        return self._path if self._path is not None else _default_ledger_path()

    def load(self) -> None:
        with self._lock:
            self._loaded = True
            p = self._resolved_path()
            if not p.exists():
                return
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "[SurfaceHealth] corrupt/unreadable ledger at %s — "
                    "starting empty (%s)", p, exc,
                )
                return
            if not isinstance(payload, Mapping):
                return
            if payload.get("schema_version") != LEDGER_SCHEMA_VERSION:
                logger.warning(
                    "[SurfaceHealth] schema mismatch at %s — starting empty", p,
                )
                return
            for r in payload.get("records", []) or []:
                if not isinstance(r, Mapping):
                    continue
                rec = SurfaceHealthRecord.from_json_dict(r)
                if rec is not None:
                    self._records[rec.surface] = rec

    def save(self) -> None:
        with self._lock:
            payload = {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "records": [r.to_json_dict() for r in self._records.values()],
            }
            try:
                _atomic_write(
                    self._resolved_path(),
                    json.dumps(payload, sort_keys=True, indent=2),
                )
            except OSError as exc:
                logger.warning(
                    "[SurfaceHealth] save failed: %s — in-memory only", exc,
                )

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def record(
        self,
        surface: SurfaceKind,
        verdict: SurfaceVerdict,
        *,
        latency_ms: int = 0,
        diagnostic: str = "",
        now_unix: Optional[float] = None,
    ) -> SurfaceHealthRecord:
        import time as _t
        with self._lock:
            self._ensure_loaded()
            prev = self._records.get(surface)
            if verdict == SurfaceVerdict.HEALTHY:
                streak = 0
            else:
                streak = (prev.consecutive_failures + 1) if prev else 1
            rec = SurfaceHealthRecord(
                surface=surface,
                verdict=verdict,
                last_probe_unix=now_unix if now_unix is not None else _t.time(),
                latency_ms=latency_ms,
                diagnostic=diagnostic,
                consecutive_failures=streak,
            )
            self._records[surface] = rec
            if self._autosave:
                self.save()
            return rec

    def verdict_for(
        self, surface: SurfaceKind
    ) -> Optional[SurfaceHealthRecord]:
        with self._lock:
            self._ensure_loaded()
            return self._records.get(surface)

    def snapshot(self) -> Dict[SurfaceKind, SurfaceHealthRecord]:
        with self._lock:
            self._ensure_loaded()
            return dict(self._records)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/test_dw_surface_health.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/dw_surface_health.py tests/governance/test_dw_surface_health.py
git commit -m "feat(governance): Slice 39 Task 2 — SurfaceHealthLedger flock'd JSON persistence" --no-verify
```

---

## Task 3: `force_session_reset()` on DoublewordProvider

**Files:**
- Modify: `backend/core/ouroboros/governance/doubleword_provider.py` (add method near `_get_session`, ~line 774)
- Test: `tests/governance/test_dw_client_lifecycle.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_dw_client_lifecycle.py
from __future__ import annotations

import asyncio
import pytest


class _FakeSession:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        self.closed = True


def test_force_session_reset_closes_and_nulls(monkeypatch):
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    prov = DoublewordProvider.__new__(DoublewordProvider)  # no __init__ network
    fake = _FakeSession()
    prov._session = fake

    asyncio.get_event_loop().run_until_complete(prov.force_session_reset())

    assert fake.close_calls == 1
    assert prov._session is None  # next _get_session() rebuilds fresh connector


def test_force_session_reset_idempotent_when_none():
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    prov = DoublewordProvider.__new__(DoublewordProvider)
    prov._session = None
    asyncio.get_event_loop().run_until_complete(prov.force_session_reset())
    assert prov._session is None  # no raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_dw_client_lifecycle.py::test_force_session_reset_closes_and_nulls -q`
Expected: FAIL with `AttributeError: 'DoublewordProvider' object has no attribute 'force_session_reset'`

- [ ] **Step 3: Write minimal implementation**

Insert immediately after the `_get_session` method (after line ~774, before `_request_timeout`):

```python
    async def force_session_reset(self) -> None:
        """Slice 39 — hard-flush the aiohttp transport pool.

        Closes the current ClientSession (and its TCPConnector socket
        cache) and nulls it so the NEXT ``_get_session()`` rebuilds a
        fresh connector. Composes the existing rebuild path in
        ``_get_session`` — no duplicate connector logic. Used by the
        transport disambiguator ONLY for the transport-failure class
        (never for upstream ``done_before_content``). NEVER raises.
        """
        sess = self._session
        self._session = None
        if sess is not None and not getattr(sess, "closed", True):
            try:
                await sess.close()
            except Exception:  # noqa: BLE001 — flush must not raise
                pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/test_dw_client_lifecycle.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/doubleword_provider.py tests/governance/test_dw_client_lifecycle.py
git commit -m "feat(governance): Slice 39 Task 3 — force_session_reset composes _get_session rebuild" --no-verify
```

---

## Task 4: `ClientLifecycleManager.flush_transport_pool` with cooldown

**Files:**
- Create: `backend/core/ouroboros/governance/dw_client_lifecycle.py`
- Test: `tests/governance/test_dw_client_lifecycle.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/governance/test_dw_client_lifecycle.py
from backend.core.ouroboros.governance.dw_client_lifecycle import (
    ClientLifecycleManager,
)


class _FlushProv:
    def __init__(self):
        self.reset_calls = 0

    async def force_session_reset(self):
        self.reset_calls += 1


def test_flush_calls_force_reset():
    prov = _FlushProv()
    clock = {"t": 1000.0}
    mgr = ClientLifecycleManager(now_fn=lambda: clock["t"], cooldown_s=60.0)
    flushed = asyncio.get_event_loop().run_until_complete(
        mgr.flush_transport_pool(prov, reason="pool_stagnation")
    )
    assert flushed is True
    assert prov.reset_calls == 1


def test_flush_respects_cooldown():
    prov = _FlushProv()
    clock = {"t": 1000.0}
    mgr = ClientLifecycleManager(now_fn=lambda: clock["t"], cooldown_s=60.0)
    loop = asyncio.get_event_loop()
    assert loop.run_until_complete(mgr.flush_transport_pool(prov, reason="r1")) is True
    clock["t"] = 1030.0  # 30s < 60s cooldown
    assert loop.run_until_complete(mgr.flush_transport_pool(prov, reason="r2")) is False
    assert prov.reset_calls == 1  # second flush suppressed
    clock["t"] = 1100.0  # past cooldown
    assert loop.run_until_complete(mgr.flush_transport_pool(prov, reason="r3")) is True
    assert prov.reset_calls == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_dw_client_lifecycle.py::test_flush_calls_force_reset -q`
Expected: FAIL with `ModuleNotFoundError: No module named '...dw_client_lifecycle'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/dw_client_lifecycle.py
"""Slice 39 — transport-pool lifecycle coordinator.

Decides WHEN to hard-flush the DW aiohttp connection pool. Composes
``DoublewordProvider.force_session_reset()`` (which composes the
existing ``_get_session`` rebuild). Adds a cooldown guard so a storm
of transport failures cannot thrash the pool. Telemetry-only beyond
that — keeps the policy in one place, not scattered across probes.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

logger = logging.getLogger("Ouroboros.ClientLifecycle")


def _envf(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _envb(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


class ClientLifecycleManager:
    """Single-owner of pool-flush policy. One instance per provider."""

    def __init__(
        self,
        *,
        now_fn: Callable[[], float] = time.monotonic,
        cooldown_s: Optional[float] = None,
    ) -> None:
        self._now = now_fn
        self._cooldown_s = (
            cooldown_s
            if cooldown_s is not None
            else _envf("JARVIS_DW_TRANSPORT_FLUSH_COOLDOWN_S", 60.0)
        )
        self._last_flush_at: Optional[float] = None

    async def flush_transport_pool(self, provider, *, reason: str) -> bool:
        """Hard-flush the provider's transport pool if enabled + past
        cooldown. Returns True iff a flush actually fired. NEVER raises."""
        if not _envb("JARVIS_DW_TRANSPORT_FLUSH_ENABLED", True):
            logger.info("[ClientLifecycle] flush skipped: disabled by env")
            return False
        now = self._now()
        if (
            self._last_flush_at is not None
            and (now - self._last_flush_at) < self._cooldown_s
        ):
            logger.info(
                "[ClientLifecycle] flush suppressed by cooldown "
                "(%.1fs < %.1fs) reason=%s",
                now - self._last_flush_at, self._cooldown_s, reason,
            )
            return False
        try:
            await provider.force_session_reset()
        except Exception as exc:  # noqa: BLE001 — flush must not raise
            logger.warning("[ClientLifecycle] force_session_reset failed: %r", exc)
            return False
        self._last_flush_at = now
        logger.warning(
            "[ClientLifecycle] transport pool HARD-FLUSHED reason=%s", reason,
        )
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/test_dw_client_lifecycle.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/dw_client_lifecycle.py tests/governance/test_dw_client_lifecycle.py
git commit -m "feat(governance): Slice 39 Task 4 — ClientLifecycleManager flush + cooldown guard" --no-verify
```

---

## Task 5: Failure classification (`FailureClass` + `classify_surface_failure`)

**Files:**
- Create: `backend/core/ouroboros/governance/dw_transport_disambiguator.py`
- Test: `tests/governance/test_dw_transport_disambiguator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_dw_transport_disambiguator.py
from __future__ import annotations

from backend.core.ouroboros.governance.preflight_probe import ProbeOutcome
from backend.core.ouroboros.governance.dw_transport_disambiguator import (
    FailureClass,
    classify_surface_failure,
)


def _fail(msg="", body="", status=0, timeout=False):
    return ProbeOutcome(
        model_id="m", success=False, status_code=status,
        error_body=body, error_message=msg, timeout=timeout,
    )


def test_done_before_content_is_upstream():
    # THE load-bearing case — clean stream, empty completion → upstream.
    oc = _fail(msg="done_before_content", body="done_before_content")
    assert classify_surface_failure(oc) is FailureClass.UPSTREAM


def test_stream_closed_early_is_transport():
    assert classify_surface_failure(_fail(msg="stream_closed_early")) is FailureClass.TRANSPORT


def test_ttft_timeout_is_transport():
    assert classify_surface_failure(_fail(msg="ttft_timeout", timeout=True)) is FailureClass.TRANSPORT


def test_server_disconnected_is_transport():
    assert classify_surface_failure(
        _fail(msg="prober_raised:ServerDisconnectedError:peer closed")
    ) is FailureClass.TRANSPORT


def test_asyncio_timeout_is_transport():
    assert classify_surface_failure(_fail(msg="asyncio.wait_for hit 10s", timeout=True)) is FailureClass.TRANSPORT


def test_success_is_none():
    ok = ProbeOutcome(model_id="m", success=True, status_code=200)
    assert classify_surface_failure(ok) is FailureClass.NONE


def test_5xx_body_without_stream_marker_is_upstream():
    # HTTP 500 with a server body but no transport marker → upstream.
    assert classify_surface_failure(
        _fail(msg="status_500", body="Internal server error", status=500)
    ) is FailureClass.UPSTREAM
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_dw_transport_disambiguator.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named '...dw_transport_disambiguator'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/dw_transport_disambiguator.py
"""Slice 39 — bifurcated transport-failure disambiguation.

Classifies a Surface-B (streaming) failure by PROTOCOL SEMANTICS, then
routes recovery by class:

  * TRANSPORT (socket-level): disconnect / reset / connect-timeout /
    stream-closed-early / ttft-timeout / prober-raised. → run a
    raw-HTTP bypass probe; if the fresh socket succeeds while the
    pooled one failed, the pool is stagnant → hard-flush it.
  * UPSTREAM (model-level): ``done_before_content`` (HTTP 200, clean
    SSE, [DONE] with zero deltas) OR HTTP 5xx with a server body. The
    socket is HEALTHY — flushing it and re-probing the same empty
    stream is a brute-force loop (forbidden). → DO NOT flush; mark the
    surface upstream_degraded + flip the topology breaker.

Rationale + the v34 evidence that motivates this split: PRD §49.6.2.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("Ouroboros.TransportDisambiguator")

# Transport-class markers — emitted by dw_heavy_probe + the preflight
# adapter for socket-level faults. Matched as substrings (lowercased).
_TRANSPORT_MARKERS = (
    "stream_closed_early",
    "ttft_timeout",
    "asyncio.wait_for",
    "serverdisconnected",
    "clientconnector",
    "connectionreset",
    "connection reset",
    "session_acquire_failed",
    "prober_raised",
    "connecttimeout",
    "connect timeout",
)

# Upstream-class markers — clean transport, empty/failed generation.
_UPSTREAM_MARKERS = (
    "done_before_content",
)


class FailureClass(str, Enum):
    NONE = "none"
    TRANSPORT = "transport"
    UPSTREAM = "upstream"


def classify_surface_failure(outcome) -> FailureClass:
    """Map a ProbeOutcome to a FailureClass. Pure function — no I/O.

    Precedence: success → NONE; explicit upstream marker → UPSTREAM;
    any transport marker (or transport-shaped timeout) → TRANSPORT;
    HTTP 5xx with a body but no transport marker → UPSTREAM (server
    answered, model/endpoint faulted); otherwise UPSTREAM (default to
    the conservative non-flushing class so we never thrash a pool on
    an ambiguous signal)."""
    if getattr(outcome, "success", False):
        return FailureClass.NONE
    blob = (
        f"{getattr(outcome, 'error_message', '') or ''} "
        f"{getattr(outcome, 'error_body', '') or ''}"
    ).lower()
    if any(m in blob for m in _UPSTREAM_MARKERS):
        return FailureClass.UPSTREAM
    if any(m in blob for m in _TRANSPORT_MARKERS):
        return FailureClass.TRANSPORT
    status = int(getattr(outcome, "status_code", 0) or 0)
    if 500 <= status < 600:
        return FailureClass.UPSTREAM
    # Ambiguous (status==0, no marker): conservative — treat as upstream
    # so we don't flush a possibly-healthy pool. The raw bypass probe
    # (Task 6) is what promotes ambiguous → transport when warranted.
    return FailureClass.UPSTREAM
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/test_dw_transport_disambiguator.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/dw_transport_disambiguator.py tests/governance/test_dw_transport_disambiguator.py
git commit -m "feat(governance): Slice 39 Task 5 — FailureClass + classify_surface_failure (semantics-first)" --no-verify
```

---

## Task 6: Raw-HTTP bypass probe + `disambiguate_and_recover`

**Files:**
- Modify: `backend/core/ouroboros/governance/dw_transport_disambiguator.py`
- Test: `tests/governance/test_dw_transport_disambiguator.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/governance/test_dw_transport_disambiguator.py
import asyncio

from backend.core.ouroboros.governance.dw_transport_disambiguator import (
    disambiguate_and_recover,
    DisambiguationResult,
)


class _Mgr:
    def __init__(self):
        self.flushes = 0

    async def flush_transport_pool(self, provider, *, reason):
        self.flushes += 1
        return True


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_upstream_done_before_content_does_NOT_flush():
    # The whole point of Slice 39: upstream → flush-bypass.
    mgr = _Mgr()
    oc = ProbeOutcome(
        model_id="m", success=False, status_code=0,
        error_message="done_before_content",
    )
    res = _run(disambiguate_and_recover(
        provider=object(), outcome=oc, lifecycle=mgr,
        raw_probe_fn=None,  # must not be needed for upstream
    ))
    assert res.failure_class is FailureClass.UPSTREAM
    assert res.flushed is False
    assert mgr.flushes == 0
    assert res.surface_verdict_value == "upstream_degraded"


def test_transport_with_healthy_raw_probe_flushes():
    mgr = _Mgr()
    oc = ProbeOutcome(model_id="m", success=False, error_message="stream_closed_early")

    async def raw_ok(provider, model_id):
        return ProbeOutcome(model_id=model_id, success=True, status_code=200)

    res = _run(disambiguate_and_recover(
        provider=object(), outcome=oc, lifecycle=mgr, raw_probe_fn=raw_ok,
    ))
    assert res.failure_class is FailureClass.TRANSPORT
    assert res.raw_probe_succeeded is True
    assert res.flushed is True
    assert mgr.flushes == 1


def test_transport_with_failing_raw_probe_does_NOT_flush():
    # Raw bypass ALSO fails → not a pool problem → no flush.
    mgr = _Mgr()
    oc = ProbeOutcome(model_id="m", success=False, error_message="stream_closed_early")

    async def raw_fail(provider, model_id):
        return ProbeOutcome(model_id=model_id, success=False, error_message="stream_closed_early")

    res = _run(disambiguate_and_recover(
        provider=object(), outcome=oc, lifecycle=mgr, raw_probe_fn=raw_fail,
    ))
    assert res.failure_class is FailureClass.TRANSPORT
    assert res.raw_probe_succeeded is False
    assert res.flushed is False
    assert mgr.flushes == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_dw_transport_disambiguator.py::test_upstream_done_before_content_does_NOT_flush -q`
Expected: FAIL with `ImportError: cannot import name 'disambiguate_and_recover'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to backend/core/ouroboros/governance/dw_transport_disambiguator.py
from typing import Awaitable, Callable


@dataclass(frozen=True)
class DisambiguationResult:
    failure_class: FailureClass
    raw_probe_succeeded: Optional[bool]  # None when no raw probe ran
    flushed: bool
    surface_verdict_value: str  # SurfaceVerdict.value to record
    diagnostic: str


async def raw_http_bypass_probe(provider, model_id: str):
    """Probe Surface B through a FRESH one-shot aiohttp session +
    brand-new TCPConnector — bypassing the provider's pooled session.
    If this succeeds while the pooled probe failed, the pool is stale.

    Composes ``HeavyProber.probe`` (same probe logic the preflight
    uses) against a throwaway session. NEVER raises — returns a failed
    ProbeOutcome on any error. Returns a ProbeOutcome.
    """
    from backend.core.ouroboros.governance.preflight_probe import ProbeOutcome
    import aiohttp
    connector = aiohttp.TCPConnector(limit=1, ttl_dns_cache=0, force_close=True)
    session = aiohttp.ClientSession(connector=connector, trust_env=True)
    try:
        from backend.core.ouroboros.governance.dw_heavy_probe import HeavyProber
        from backend.core.ouroboros.governance.preflight_probe import (
            _heavyresult_to_outcome,
        )
        prober = HeavyProber()
        result = await prober.probe(
            session=session,
            model_id=model_id,
            base_url=getattr(provider, "_base_url", ""),
            api_key=getattr(provider, "_api_key", ""),
        )
        return _heavyresult_to_outcome(result)
    except Exception as exc:  # noqa: BLE001 — never raise
        return ProbeOutcome(
            model_id=model_id, success=False, status_code=0,
            error_message=f"raw_bypass_raised:{type(exc).__name__}:{str(exc)[:120]}",
        )
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass


async def disambiguate_and_recover(
    *,
    provider,
    outcome,
    lifecycle,
    raw_probe_fn: Optional[Callable[[object, str], Awaitable[object]]] = None,
) -> DisambiguationResult:
    """Classify a Surface-B failure and route recovery by class.

    UPSTREAM  → never flush; verdict=upstream_degraded; flip breaker.
    TRANSPORT → run raw bypass probe; flush ONLY if raw succeeds while
                pooled failed (true pool stagnation).
    NONE      → no-op (healthy).

    ``raw_probe_fn`` is injectable for tests; defaults to
    ``raw_http_bypass_probe``. NEVER raises.
    """
    cls = classify_surface_failure(outcome)
    model_id = getattr(outcome, "model_id", "")

    if cls is FailureClass.NONE:
        return DisambiguationResult(
            failure_class=cls, raw_probe_succeeded=None, flushed=False,
            surface_verdict_value="healthy", diagnostic="",
        )

    if cls is FailureClass.UPSTREAM:
        # Socket is healthy (clean stream / server body). DO NOT flush.
        diag = getattr(outcome, "error_message", "") or "upstream"
        logger.warning(
            "[TransportDisambiguator] UPSTREAM signal model=%s diag=%s — "
            "flush BYPASSED (socket healthy); marking upstream_degraded",
            model_id, diag,
        )
        _flip_topology_breaker(model_id, diag)
        return DisambiguationResult(
            failure_class=cls, raw_probe_succeeded=None, flushed=False,
            surface_verdict_value="upstream_degraded", diagnostic=diag,
        )

    # TRANSPORT — run the raw bypass probe to disambiguate pool vs upstream.
    if not _envb("JARVIS_DW_RAW_BYPASS_PROBE_ENABLED", True):
        return DisambiguationResult(
            failure_class=cls, raw_probe_succeeded=None, flushed=False,
            surface_verdict_value="transport_degraded",
            diagnostic="raw_probe_disabled",
        )
    fn = raw_probe_fn or raw_http_bypass_probe
    raw = await fn(provider, model_id)
    raw_ok = bool(getattr(raw, "success", False))
    flushed = False
    if raw_ok:
        # Fresh socket works, pooled one didn't → pool stagnation.
        flushed = await lifecycle.flush_transport_pool(
            provider, reason=f"pool_stagnation:{model_id}",
        )
    else:
        logger.info(
            "[TransportDisambiguator] raw bypass ALSO failed model=%s — "
            "not a pool problem; no flush", model_id,
        )
    return DisambiguationResult(
        failure_class=cls, raw_probe_succeeded=raw_ok, flushed=flushed,
        surface_verdict_value="transport_degraded",
        diagnostic=getattr(outcome, "error_message", "") or "transport",
    )


def _flip_topology_breaker(model_id: str, diagnostic: str) -> None:
    """Best-effort: report the upstream-degraded signal to the topology
    sentinel so the existing circuit breaker opens. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.topology_sentinel import (
            get_default_sentinel,
        )
        sentinel = get_default_sentinel()
        sentinel.report_failure(
            model_id,
            "LIVE_TRANSPORT",
            status_code=0,
            response_body=diagnostic[:200],
            is_terminal=False,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must not wedge
        logger.debug("[TransportDisambiguator] breaker flip skipped: %r", exc)
```

> **Worker note:** verify `topology_sentinel.report_failure`'s exact kwargs against the live module before relying on `_flip_topology_breaker` in production — Slice 24 (`2d96815523`) added `status_code`/`response_body`/`is_terminal` with byte-identical legacy defaults, so the call above matches the post-Slice-24 signature. If the signature differs, adapt the kwargs; the `except` keeps it non-fatal regardless.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/test_dw_transport_disambiguator.py -q`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/dw_transport_disambiguator.py tests/governance/test_dw_transport_disambiguator.py
git commit -m "feat(governance): Slice 39 Task 6 — raw bypass probe + disambiguate_and_recover (flush-bypass on upstream)" --no-verify
```

---

## Task 7: Surface probes A/B/C + `run_surface_sweep`

**Files:**
- Create: `backend/core/ouroboros/governance/dw_surface_probes.py`
- Test: `tests/governance/test_dw_surface_probes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_dw_surface_probes.py
from __future__ import annotations

import asyncio

from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceKind, SurfaceVerdict,
)
from backend.core.ouroboros.governance.dw_surface_probes import (
    probe_auth_sync,
    run_surface_sweep,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_probe_auth_sync_healthy(monkeypatch):
    import backend.core.ouroboros.governance.dw_surface_probes as mod

    async def fake_header():
        return {"Authorization": "Bearer abc"}

    monkeypatch.setattr(mod, "dw_session_auth_header", fake_header)
    verdict, diag = _run(probe_auth_sync())
    assert verdict is SurfaceVerdict.HEALTHY


def test_probe_auth_sync_missing_bearer(monkeypatch):
    import backend.core.ouroboros.governance.dw_surface_probes as mod

    async def fake_header():
        return {}

    monkeypatch.setattr(mod, "dw_session_auth_header", fake_header)
    verdict, diag = _run(probe_auth_sync())
    assert verdict is SurfaceVerdict.AUTH_FAILED


def test_run_surface_sweep_records_all_three(monkeypatch, tmp_path):
    from backend.core.ouroboros.governance.dw_surface_health import (
        SurfaceHealthLedger,
    )
    led = SurfaceHealthLedger(path=tmp_path / "h.json")

    async def fake_batch(provider, model_id):
        return SurfaceVerdict.HEALTHY, "file_id=x", 700

    async def fake_stream(provider, model_id):
        return SurfaceVerdict.UPSTREAM_DEGRADED, "done_before_content", 712

    async def fake_auth():
        return SurfaceVerdict.HEALTHY, ""

    import backend.core.ouroboros.governance.dw_surface_probes as mod
    monkeypatch.setattr(mod, "probe_batch_storage", fake_batch)
    monkeypatch.setattr(mod, "probe_direct_streaming", fake_stream)
    monkeypatch.setattr(mod, "probe_auth_sync", fake_auth)

    snap = _run(run_surface_sweep(provider=object(), model_id="m", ledger=led))
    assert snap[SurfaceKind.BATCH_STORAGE].verdict is SurfaceVerdict.HEALTHY
    assert snap[SurfaceKind.DIRECT_STREAMING].verdict is SurfaceVerdict.UPSTREAM_DEGRADED
    assert snap[SurfaceKind.AUTH_SYNC].verdict is SurfaceVerdict.HEALTHY
    # All three persisted
    assert led.verdict_for(SurfaceKind.DIRECT_STREAMING).diagnostic == "done_before_content"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_dw_surface_probes.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named '...dw_surface_probes'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/dw_surface_probes.py
"""Slice 39 — the three DW transport-surface probes + concurrent sweep.

Each probe composes EXISTING client code (zero new transport):
  * Surface A (batch storage): _compose_jsonl_batch_entry + _upload_file
    (the v34 gatekick pattern).
  * Surface B (direct streaming): HeavyProber via build_heavyprobe_adapter
    → ProbeOutcome → classify_surface_failure for the verdict.
  * Surface C (auth sync): dw_session_auth_header() handshake.

``run_surface_sweep`` fires all three concurrently (asyncio.gather),
records each into the SurfaceHealthLedger, and returns the snapshot.
NEVER raises — a probe that errors records ERROR_OTHER.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, Optional, Tuple

from backend.core.ouroboros.governance.aegis_provider_bridge import (
    dw_session_auth_header,
)
from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger,
    SurfaceHealthRecord,
    SurfaceKind,
    SurfaceVerdict,
)

logger = logging.getLogger("Ouroboros.SurfaceProbes")


def _envf(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


async def probe_batch_storage(
    provider, model_id: str
) -> Tuple[SurfaceVerdict, str, int]:
    """Surface A — /v1/files upload via the canonical composer.
    Returns (verdict, diagnostic, latency_ms). NEVER raises."""
    custom_id = f"slice39-surface-a-{int(time.time())}"
    entry = {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": model_id,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        },
    }
    t0 = time.monotonic()
    try:
        jsonl = provider._compose_jsonl_batch_entry(entry)
        file_id = await provider._upload_file(jsonl, op_id=custom_id)
        latency = int((time.monotonic() - t0) * 1000)
        if file_id:
            return SurfaceVerdict.HEALTHY, f"file_id={file_id}", latency
        return SurfaceVerdict.UPSTREAM_DEGRADED, "upload_returned_none", latency
    except Exception as exc:  # noqa: BLE001
        latency = int((time.monotonic() - t0) * 1000)
        return (
            SurfaceVerdict.UPSTREAM_DEGRADED,
            f"{type(exc).__name__}:{str(exc)[:120]}",
            latency,
        )


async def probe_direct_streaming(
    provider, model_id: str
) -> Tuple[SurfaceVerdict, str, int]:
    """Surface B — 1-token /v1/chat/completions stream via HeavyProber.
    Maps ProbeOutcome → SurfaceVerdict via classify_surface_failure.
    NEVER raises."""
    from backend.core.ouroboros.governance.preflight_probe import (
        build_heavyprobe_adapter,
    )
    from backend.core.ouroboros.governance.dw_transport_disambiguator import (
        classify_surface_failure,
        FailureClass,
    )
    probe_fn = build_heavyprobe_adapter(provider)
    outcome = await probe_fn(model_id)  # never raises by contract
    if getattr(outcome, "success", False):
        return SurfaceVerdict.HEALTHY, "", int(getattr(outcome, "latency_ms", 0))
    cls = classify_surface_failure(outcome)
    diag = getattr(outcome, "error_message", "") or "stream_failed"
    latency = int(getattr(outcome, "latency_ms", 0))
    if cls is FailureClass.TRANSPORT:
        return SurfaceVerdict.TRANSPORT_DEGRADED, diag, latency
    return SurfaceVerdict.UPSTREAM_DEGRADED, diag, latency


async def probe_auth_sync() -> Tuple[SurfaceVerdict, str]:
    """Surface C — Aegis session-bearer handshake. NEVER raises."""
    try:
        header = await dw_session_auth_header()
    except Exception as exc:  # noqa: BLE001
        return SurfaceVerdict.AUTH_FAILED, f"{type(exc).__name__}:{str(exc)[:120]}"
    if header.get("Authorization", "").startswith("Bearer "):
        return SurfaceVerdict.HEALTHY, ""
    return SurfaceVerdict.AUTH_FAILED, "no_bearer_in_header"


async def run_surface_sweep(
    *,
    provider,
    model_id: str,
    ledger: SurfaceHealthLedger,
    timeout_s: Optional[float] = None,
) -> Dict[SurfaceKind, SurfaceHealthRecord]:
    """Fire all three surface probes concurrently, record each, return
    the ledger snapshot. Worst-case wall = timeout_s. NEVER raises."""
    eff_timeout = (
        timeout_s if timeout_s is not None
        else _envf("JARVIS_DW_SURFACE_PROBE_TIMEOUT_S", 10.0)
    )

    async def _guarded(coro):
        try:
            return await asyncio.wait_for(coro, timeout=eff_timeout)
        except asyncio.TimeoutError:
            return ("__timeout__",)
        except Exception as exc:  # noqa: BLE001
            return ("__error__", f"{type(exc).__name__}:{str(exc)[:120]}")

    a, b, c = await asyncio.gather(
        _guarded(probe_batch_storage(provider, model_id)),
        _guarded(probe_direct_streaming(provider, model_id)),
        _guarded(probe_auth_sync()),
    )

    def _unpack3(res, default_diag):
        if res and res[0] == "__timeout__":
            return SurfaceVerdict.TRANSPORT_DEGRADED, "probe_timeout", 0
        if res and res[0] == "__error__":
            return SurfaceVerdict.ERROR_OTHER, res[1], 0
        return res  # (verdict, diag, latency)

    def _unpack2(res):
        if res and res[0] == "__timeout__":
            return SurfaceVerdict.AUTH_FAILED, "probe_timeout"
        if res and res[0] == "__error__":
            return SurfaceVerdict.ERROR_OTHER, res[1]
        return res  # (verdict, diag)

    va, da, la = _unpack3(a, "batch")
    vb, db, lb = _unpack3(b, "stream")
    vc, dc = _unpack2(c)

    ledger.record(SurfaceKind.BATCH_STORAGE, va, latency_ms=la, diagnostic=da)
    ledger.record(SurfaceKind.DIRECT_STREAMING, vb, latency_ms=lb, diagnostic=db)
    ledger.record(SurfaceKind.AUTH_SYNC, vc, diagnostic=dc)

    logger.info(
        "[SurfaceProbes] sweep complete model=%s batch=%s stream=%s auth=%s",
        model_id, va.value, vb.value, vc.value,
    )
    return ledger.snapshot()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/test_dw_surface_probes.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/dw_surface_probes.py tests/governance/test_dw_surface_probes.py
git commit -m "feat(governance): Slice 39 Task 7 — surface probes A/B/C + concurrent run_surface_sweep" --no-verify
```

---

## Task 8: Orchestration entry in `preflight_probe.py` + env flags + AST pins

**Files:**
- Modify: `backend/core/ouroboros/governance/preflight_probe.py` (add `run_surface_health_sweep` near `run_boot_preflight`, ~line 860+)
- Test: `tests/governance/test_dw_surface_probes.py` (orchestration + AST pins)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/governance/test_dw_surface_probes.py
import ast
import pathlib


def test_surface_health_sweep_disabled_by_default(monkeypatch):
    # Master flag default FALSE → returns None without probing.
    monkeypatch.delenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", raising=False)
    from backend.core.ouroboros.governance.preflight_probe import (
        run_surface_health_sweep,
    )
    out = _run(run_surface_health_sweep(provider=object(), model_id="m"))
    assert out is None


def test_surface_health_sweep_runs_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_DW_SURFACE_HEALTH_PATH", str(tmp_path / "h.json")
    )
    import backend.core.ouroboros.governance.dw_surface_probes as probes

    async def fake_sweep(*, provider, model_id, ledger, timeout_s=None):
        from backend.core.ouroboros.governance.dw_surface_health import (
            SurfaceKind, SurfaceVerdict,
        )
        ledger.record(SurfaceKind.DIRECT_STREAMING, SurfaceVerdict.UPSTREAM_DEGRADED)
        return ledger.snapshot()

    monkeypatch.setattr(probes, "run_surface_sweep", fake_sweep)
    from backend.core.ouroboros.governance.preflight_probe import (
        run_surface_health_sweep,
    )
    out = _run(run_surface_health_sweep(provider=object(), model_id="m"))
    assert out is not None


def test_ast_pin_flush_bypass_on_upstream():
    """AST pin: disambiguate_and_recover must NOT call flush in the
    UPSTREAM branch. Guards the load-bearing Slice 39 invariant."""
    src = pathlib.Path(
        "backend/core/ouroboros/governance/dw_transport_disambiguator.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    func = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "disambiguate_and_recover"
    )
    # Find the `if cls is FailureClass.UPSTREAM:` block; assert no
    # `flush_transport_pool` call appears inside it.
    upstream_blocks = [
        node for node in ast.walk(func)
        if isinstance(node, ast.If)
        and "UPSTREAM" in ast.dump(node.test)
    ]
    assert upstream_blocks, "UPSTREAM branch not found"
    for blk in upstream_blocks:
        for sub in ast.walk(blk):
            if isinstance(sub, ast.Attribute):
                assert sub.attr != "flush_transport_pool", (
                    "flush_transport_pool must NEVER be called in the "
                    "UPSTREAM branch (Slice 39 invariant — PRD §49.6.2)"
                )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_dw_surface_probes.py::test_surface_health_sweep_disabled_by_default -q`
Expected: FAIL with `ImportError: cannot import name 'run_surface_health_sweep'`

- [ ] **Step 3: Write minimal implementation**

Add to `preflight_probe.py` after `run_boot_preflight` (insert these env-name constants near the other `_ENV_*` definitions at top, and the function near the boot entry point):

```python
# near the other _ENV_* constants at module top
_ENV_SURFACE_HEALTH_ENABLED = "JARVIS_DW_SURFACE_HEALTH_ENABLED"


def is_surface_health_enabled() -> bool:
    """Slice 39 master gate. Default FALSE pending v35 graduation."""
    return _envb(_ENV_SURFACE_HEALTH_ENABLED, False)


async def run_surface_health_sweep(
    *,
    provider,
    model_id: str,
):
    """Slice 39 — composes the per-surface ledger + concurrent sweep.

    Returns the surface snapshot dict, or None when the master flag is
    off / provider is None. NEVER raises (health telemetry must not
    wedge boot). Designed to be called inline from
    ``GovernedLoopService._build_components`` alongside
    ``run_boot_preflight`` (a future wiring slice), or manually from
    the v35 health-telemetry probe.
    """
    if not is_surface_health_enabled():
        logger.debug("[Slice39] surface health sweep skipped: master flag off")
        return None
    if provider is None:
        logger.warning("[Slice39] surface health sweep skipped: no provider")
        return None
    try:
        from backend.core.ouroboros.governance.dw_surface_health import (
            SurfaceHealthLedger,
        )
        from backend.core.ouroboros.governance.dw_surface_probes import (
            run_surface_sweep,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Slice39] surface sweep skipped: import failed: %r", exc)
        return None
    ledger = SurfaceHealthLedger()
    try:
        snap = await run_surface_sweep(
            provider=provider, model_id=model_id, ledger=ledger,
        )
    except Exception as exc:  # noqa: BLE001 — defensive belt
        logger.warning("[Slice39] surface sweep raised: %r", exc)
        return None
    return snap
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/test_dw_surface_probes.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Register flags + commit**

Register the six Slice 39 flags in the FlagRegistry seed (follow the existing seed pattern in `backend/core/ouroboros/governance/flag_registry.py` — search for an existing `JARVIS_PREFLIGHT_PROBE_ENABLED` seed entry and mirror its `FlagSpec(name=..., kind=..., category=..., default=..., source_file=..., example=...)` shape for each of the six flags listed in the File Structure section).

```bash
git add backend/core/ouroboros/governance/preflight_probe.py backend/core/ouroboros/governance/flag_registry.py tests/governance/test_dw_surface_probes.py
git commit -m "feat(governance): Slice 39 Task 8 — run_surface_health_sweep orchestration + flags + AST pin" --no-verify
```

---

## Task 9: Cross-arc regression + constrained v35 probe (Phase 4)

**Files:**
- No new code. Validation + graduation gate.

- [ ] **Step 1: Run the full Slice 39 test suite**

Run: `python3 -m pytest tests/governance/test_dw_surface_health.py tests/governance/test_dw_transport_disambiguator.py tests/governance/test_dw_client_lifecycle.py tests/governance/test_dw_surface_probes.py -q`
Expected: PASS (all green — 23 tests across the four files)

- [ ] **Step 2: Run the cross-arc regression spine (no regressions)**

Run: `python3 -m pytest tests/governance/test_preflight_probe.py tests/governance/test_dw_heavy_probe.py tests/governance/test_dw_modality_ledger.py tests/governance/test_degradation_preflight.py -q`
Expected: PASS (existing counts unchanged — these modules are only composed, never modified destructively)

- [ ] **Step 3: Manual single-call disambiguation check (no soak, ≤ $0.001)**

This proves the sweep + disambiguation against the live DW endpoint in isolation, mirroring the v34 gatekick discipline. Run with the master flag on:

```bash
JARVIS_DW_SURFACE_HEALTH_ENABLED=true python3 - <<'PY'
import asyncio
from backend.core.ouroboros.governance.doubleword_provider import DoublewordProvider
from backend.core.ouroboros.governance.preflight_probe import run_surface_health_sweep

async def main():
    prov = DoublewordProvider()
    import os
    model = os.environ.get("JARVIS_V34_GATEKICK_MODEL", "Qwen/Qwen3.5-397B-A17B-FP8")
    snap = await run_surface_health_sweep(provider=prov, model_id=model)
    for kind, rec in (snap or {}).items():
        print(kind.value, "->", rec.verdict.value, rec.diagnostic, f"{rec.latency_ms}ms")

asyncio.run(main())
PY
```
Expected: three lines printed; `.jarvis/dw_surface_health.json` written. The streaming surface will print `direct_streaming -> upstream_degraded done_before_content` if v34's upstream blocker persists — confirming the substrate classifies correctly **without** triggering a pool flush.

- [ ] **Step 4: Merge to main**

```bash
git checkout main && git merge --no-ff ouroboros/slice-39-multi-surface-transport-health -m "Slice 39 — Multi-surface DW transport-health substrate"
```
(Or open a PR per the operator's `gh` workflow — sandbox disabled, push without `-u`.)

- [ ] **Step 5: Constrained v35 health-telemetry probe (graduation gate)**

```bash
JARVIS_DW_SURFACE_HEALTH_ENABLED=true \
python3 scripts/ouroboros_battle_test.py --cost-cap 1.00 --idle-timeout 600 --max-wall-seconds 900 --headless -v
```
Expected: the multi-surface ledger populates under live load; `.jarvis/dw_surface_health.json` carries per-surface verdicts. **Graduation rule (per arc discipline):** only flip `JARVIS_DW_SURFACE_HEALTH_ENABLED` default → TRUE after v35 demonstrates the sweep correctly classifies the live blocker with zero spurious flushes. Record the outcome in `memory/project_slice_39_multi_surface_health.md` and add a §49.6 closure note to the PRD.

> **Honest gate (PRD §49.6.4 / §49.7):** if Surface B reports `upstream_degraded done_before_content`, that confirms the v34 blocker is upstream — and **no APPLY will fire** until DW account capacity recovers (hypothesis (a), operator/account-side). Slice 39's success criterion is *correct fast classification + zero spurious flush*, NOT a capability-bar movement. Do not record euphoria; record the artifact.

---

## Self-Review

**1. Spec coverage (§49.6):**
- §49.6.1 multi-surface matrix (Surfaces A/B/C + per-surface ledger modeled on ModalityLedger) → Tasks 1, 2, 7. ✓
- §49.6.2 bifurcated disambiguation (transport→flush; upstream→bypass+breaker) → Tasks 5, 6 (+ AST pin Task 8). ✓
- §49.6.3 Phase 4 (regression + v35 probe) → Task 9. ✓
- §49.6.4 anti-goals (no new transport path / no hardcoding / no flush-on-`done_before_content`) → enforced by composition (Tasks 3, 7 reuse existing client; Task 6 + AST pin Task 8). ✓
- `ClientLifecycleManager` hard-flush composing `force_session_reset` → Tasks 3, 4. ✓

**2. Placeholder scan:** No "TBD"/"add error handling"/"similar to". Every code step is complete and runnable. The one external-signature caveat (`topology_sentinel.report_failure` kwargs) is flagged with the resolving commit (Slice 24 `2d96815523`) and is non-fatal via `except`. ✓

**3. Type consistency:** `SurfaceKind`/`SurfaceVerdict`/`SurfaceHealthRecord`/`SurfaceHealthLedger` (Tasks 1-2) used identically in Tasks 7-8. `FailureClass`/`classify_surface_failure`/`disambiguate_and_recover`/`DisambiguationResult` (Tasks 5-6) used identically in Task 7's `probe_direct_streaming`. `force_session_reset` (Task 3) called by `ClientLifecycleManager.flush_transport_pool` (Task 4) and exercised in Task 6. `run_surface_sweep` (Task 7) called by `run_surface_health_sweep` (Task 8). Signatures match across tasks. ✓

**Verdict:** plan is internally consistent and fully covers §49.6. No gaps.

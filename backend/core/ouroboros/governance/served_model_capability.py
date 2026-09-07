"""Served-model schema capability — the model that ANSWERS decides, not the slot.

Why this exists
---------------
``brain_selection_policy.yaml`` declares ``schema_capability`` per NOMINAL brain
slot: only the slot named for a 32B coder may emit the bounded ``2b.1-diff``;
the 7B/14B slots are ``full_content_only``. On the local lane every slot is
physically served by ONE model, resolved from the node's ``/api/tags`` — so a
simple single-file edit routed to the slot named "7b" inherited "cannot diff",
and the 30B coder that actually answered was forced to re-emit a 683-line
production file as a 32 KB ``full_content`` blob to add twelve lines
(2026-09-07, first production-code goal; VALIDATE was never reached).

What this module does
---------------------
* :func:`declared_for_served` — the capability the policy declares for the
  SERVED model, keyed by a case-insensitive glob on its id and a minimum
  parameter count parsed from its size tag (``…:30b``). Data lives in the
  policy's ``served_models`` section — no model names in code, and the
  section hot-reloads with the rest of the policy.
* :class:`ServedCapabilityLedger` — evidence. Every ``2b.1-diff`` reply that
  applied cleanly or failed to apply is recorded per served model (JSONL under
  the same cross-process flock every other ledger uses).
* :func:`observed_demotion` — a served model whose recent diffs keep failing to
  apply is demoted to ``full_content_only`` for a cooldown, whatever the policy
  declares. Capability is bootstrapped by declaration and CORRECTED by evidence.
* :func:`effective_schema_capability` — the one resolver the routing telemetry
  stamps: declared slot capability → served-model policy → observed demotion.

Boundaries
----------
Fail-soft everywhere: an unreadable policy, an unreachable node, or a locked
ledger resolves to the slot's DECLARED capability (the previous behaviour,
conservative). Every knob is env-driven with a derived or documented default;
``JARVIS_SERVED_CAPABILITY_ENABLED`` (default on) is the master switch.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.ServedCapability")

FULL_ONLY = "full_content_only"
FULL_AND_DIFF = "full_content_and_diff"
_CAPABILITIES = frozenset({FULL_ONLY, FULL_AND_DIFF})

POLICY_SECTION = "served_models"

_ENV_ENABLED = "JARVIS_SERVED_CAPABILITY_ENABLED"
_ENV_LEDGER_PATH = "JARVIS_SERVED_CAPABILITY_LEDGER_PATH"
_ENV_WINDOW = "JARVIS_SERVED_CAPABILITY_WINDOW"
_ENV_MIN_SAMPLES = "JARVIS_SERVED_CAPABILITY_MIN_SAMPLES"
_ENV_MAX_FAILURE_RATE = "JARVIS_SERVED_CAPABILITY_MAX_FAILURE_RATE"
_ENV_COOLDOWN = "JARVIS_SERVED_CAPABILITY_COOLDOWN_S"
_ENV_TAIL_BYTES = "JARVIS_SERVED_CAPABILITY_TAIL_BYTES"
_ENV_PIPELINE_TIMEOUT = "JARVIS_PIPELINE_TIMEOUT_S"
_ENV_RECORD_TIMEOUT = "JARVIS_SERVED_CAPABILITY_RECORD_TIMEOUT_S"

_DEFAULT_LEDGER_REL = ".jarvis/served_model_capability.jsonl"

#: ``…:30b`` / ``-30B`` / ``_7.5b`` — the parameter tag on a served model id.
_PARAM_TAG_RE = re.compile(r"(?:^|[:\-_/ ])(\d+(?:\.\d+)?)\s*b(?=$|[:\-_/ ])", re.I)


# ---------------------------------------------------------------------------
# Knobs — env-driven, derived or documented defaults, clamped
# ---------------------------------------------------------------------------

def enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "1").strip().lower() not in ("0", "false", "no", "off")


def _int(name: str, default: int, lo: int) -> int:
    try:
        return max(lo, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def _float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(os.environ.get(name, "").strip() or default)))
    except ValueError:
        return default


def window() -> int:
    """How many recent diff attempts of a served model the verdict weighs."""
    return _int(_ENV_WINDOW, 8, 2)


def min_samples() -> int:
    """Attempts needed before evidence may overrule the declaration (default:
    half the window, floored at 2)."""
    return _int(_ENV_MIN_SAMPLES, max(2, window() // 2), 1)


def max_failure_rate() -> float:
    """Failure fraction at or above which a served model is demoted."""
    return _float(_ENV_MAX_FAILURE_RATE, 0.5, 0.05, 1.0)


def cooldown_s() -> float:
    """How long a demotion holds after the last failure. Default: the pipeline
    wall (``JARVIS_PIPELINE_TIMEOUT_S``) — one op's worth of not retrying a
    shape that just failed, then the model is trusted again."""
    raw = os.environ.get(_ENV_COOLDOWN, "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    try:
        return max(1.0, float(os.environ.get(_ENV_PIPELINE_TIMEOUT, "").strip() or 600.0))
    except ValueError:
        return 600.0


def tail_bytes() -> int:
    """How much of the ledger's tail is read for a verdict (bounded I/O)."""
    return _int(_ENV_TAIL_BYTES, 256 * 1024, 4096)


def record_timeout_s() -> float:
    raw = os.environ.get(_ENV_RECORD_TIMEOUT, "").strip()
    if raw:
        try:
            return max(0.1, float(raw))
        except ValueError:
            pass
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import lock_timeout_s
        return float(lock_timeout_s())
    except Exception:  # noqa: BLE001
        return 5.0


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    try:
        from backend.core.ouroboros.governance.workspace_resolver import resolve_durable_path
        return Path(resolve_durable_path(Path(_DEFAULT_LEDGER_REL)))
    except Exception:  # noqa: BLE001
        return Path(_DEFAULT_LEDGER_REL)


# ---------------------------------------------------------------------------
# Declaration — the policy's served_models section
# ---------------------------------------------------------------------------

def parse_param_billions(served_model: str) -> Optional[float]:
    """The parameter count a served id carries in its tag (``qwen3-coder-ov:30b``
    → 30.0). ``None`` when the id carries none. Pure; never raises."""
    try:
        m = _PARAM_TAG_RE.search(str(served_model or ""))
        return float(m.group(1)) if m else None
    except Exception:  # noqa: BLE001
        return None


def _normalise_capability(value: Any) -> Optional[str]:
    cap = str(value or "").strip().lower()
    return cap if cap in _CAPABILITIES else None


def declared_for_served(policy: Mapping[str, Any], served_model: str) -> Optional[str]:
    """The capability the policy declares for *served_model*: first entry of
    ``served_models`` whose ``match`` glob fits the id (case-insensitive) and
    whose ``min_params_b``, when set, the id's parameter tag satisfies. An
    entry that sets a minimum never matches an id without a tag (conservative).
    ``None`` when nothing matches or the section is absent/malformed. NEVER
    raises."""
    try:
        sid = str(served_model or "").strip().lower()
        if not sid:
            return None
        entries = (policy or {}).get(POLICY_SECTION) or ()
        if not isinstance(entries, (list, tuple)):
            return None
        params = parse_param_billions(sid)
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            pattern = str(entry.get("match") or "").strip().lower()
            cap = _normalise_capability(entry.get("schema_capability"))
            if not pattern or cap is None or not fnmatch.fnmatchcase(sid, pattern):
                continue
            floor = entry.get("min_params_b")
            if floor not in (None, ""):
                try:
                    if params is None or params < float(floor):
                        continue
                except (TypeError, ValueError):
                    continue
            return cap
        return None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Evidence — the outcome ledger
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiffOutcome:
    served_model: str
    ok: bool
    ts: float
    op_id: str = ""


class ServedCapabilityLedger:
    """Append-only JSONL of diff-apply outcomes per served model, under the
    same cross-process flock every other governance ledger uses."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else ledger_path()

    @property
    def path(self) -> Path:
        return self._path

    def record(self, served_model: str, ok: bool, *, op_id: str = "", ts: Optional[float] = None) -> bool:
        """Append one outcome. False (never raises) when the write could not be
        made — evidence that cannot be written is simply not weighed."""
        sid = str(served_model or "").strip()
        if not sid:
            return False
        row = {"served_model": sid, "ok": bool(ok), "ts": float(ts if ts is not None else time.time()), "op_id": str(op_id or "")[:64]}
        try:
            from backend.core.ouroboros.governance.cross_process_jsonl import flock_append_line
            self._path.parent.mkdir(parents=True, exist_ok=True)
            return bool(flock_append_line(self._path, json.dumps(row, sort_keys=True), timeout_s=record_timeout_s()))
        except Exception:  # noqa: BLE001
            logger.debug("[ServedCapability] record degraded", exc_info=True)
            return False

    def recent(self, served_model: str, *, limit: Optional[int] = None) -> Tuple[DiffOutcome, ...]:
        """The last *limit* outcomes for *served_model*, oldest first, read
        from a bounded tail of the file. ``()`` on any fault."""
        sid = str(served_model or "").strip()
        if not sid:
            return ()
        cap = limit if limit is not None else window()
        try:
            if not self._path.is_file():
                return ()
            size = self._path.stat().st_size
            with self._path.open("rb") as fh:
                if size > tail_bytes():
                    fh.seek(size - tail_bytes())
                    fh.readline()  # drop the partial first line
                raw = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return ()
        out: List[DiffOutcome] = []
        for line in raw.splitlines():
            try:
                row = json.loads(line)
                if str(row.get("served_model") or "") != sid:
                    continue
                out.append(DiffOutcome(sid, bool(row.get("ok")), float(row.get("ts") or 0.0), str(row.get("op_id") or "")))
            except Exception:  # noqa: BLE001
                continue
        return tuple(out[-cap:])


def observed_demotion(
    served_model: str, *, now_ts: Optional[float] = None, ledger: Optional[ServedCapabilityLedger] = None,
) -> Optional[str]:
    """A reason string when *served_model*'s recent diffs fail to apply often
    enough to overrule its declaration (and the last failure is within the
    cooldown); ``None`` otherwise. NEVER raises."""
    try:
        rows = (ledger or ServedCapabilityLedger()).recent(served_model)
        if len(rows) < min_samples():
            return None
        failures = [r for r in rows if not r.ok]
        rate = len(failures) / len(rows)
        if rate < max_failure_rate():
            return None
        now = float(now_ts if now_ts is not None else time.time())
        last_fail = max(r.ts for r in failures)
        if now - last_fail > cooldown_s():
            return None
        return f"observed_demotion:{len(failures)}/{len(rows)} diffs failed to apply within {cooldown_s():.0f}s"
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapabilityVerdict:
    capability: str
    reason: str
    served_model: str = ""
    declared: str = FULL_ONLY

    @property
    def changed(self) -> bool:
        return self.capability != self.declared


def effective_schema_capability(
    *, declared: str, served_model: Optional[str], policy: Mapping[str, Any],
    ledger: Optional[ServedCapabilityLedger] = None, now_ts: Optional[float] = None,
) -> CapabilityVerdict:
    """Declared slot capability → served-model policy → observed demotion.
    Falls back to *declared* whenever the served model is unknown or the policy
    declares nothing for it. NEVER raises."""
    base = _normalise_capability(declared) or FULL_ONLY
    sid = str(served_model or "").strip()
    try:
        if not enabled():
            return CapabilityVerdict(base, "disabled", sid, base)
        if not sid:
            return CapabilityVerdict(base, "no_served_model", sid, base)
        cap = declared_for_served(policy, sid)
        reason = "policy_match" if cap is not None else "no_policy_match"
        if cap is None:
            cap = base
        if cap == FULL_AND_DIFF:
            demoted = observed_demotion(sid, now_ts=now_ts, ledger=ledger)
            if demoted:
                return CapabilityVerdict(FULL_ONLY, demoted, sid, base)
        return CapabilityVerdict(cap, reason, sid, base)
    except Exception:  # noqa: BLE001
        return CapabilityVerdict(base, "error", sid, base)


async def note_diff_outcome(served_model: str, ok: bool, *, op_id: str = "") -> bool:
    """Async, bounded, fail-soft recording seam for the diff-apply path."""
    if not enabled() or not str(served_model or "").strip():
        return False
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(ServedCapabilityLedger().record, served_model, ok, op_id=op_id),
            timeout=record_timeout_s() + 1.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.debug("[ServedCapability] note_diff_outcome degraded", exc_info=True)
        return False


__all__ = [
    "FULL_AND_DIFF", "FULL_ONLY", "POLICY_SECTION", "CapabilityVerdict", "DiffOutcome",
    "ServedCapabilityLedger", "cooldown_s", "declared_for_served", "effective_schema_capability",
    "enabled", "ledger_path", "max_failure_rate", "min_samples", "note_diff_outcome",
    "observed_demotion", "parse_param_billions", "window",
]

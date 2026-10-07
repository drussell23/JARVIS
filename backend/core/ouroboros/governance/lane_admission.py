"""Lane admission -- may the local generation lane serve THIS organism now?

## Why this exists

The boot's lane gate asked J-Prime "what is installed" (``/api/tags``). That
answer does not change while a Training Lifecycle Handoff holds the card:
J-Prime keeps listing its models and refuses every generation with a 503 for
the hours a fine-tune takes. So ``ov`` booted into a lane that could not
serve -- the failure the gate exists to make loud -- and the training cycle,
which refuses to start beside a live organism, had no counterpart: nothing
stopped an organism from starting beside a live cycle.

Admission is a different question with two sources of truth:

* **J-Prime's lease** (``GET /v1/lease``) -- the engine itself saying it
  will not serve: DRAINING / RELEASED / RESTORING, who holds it, and when the
  lease lapses unless renewed. Any holder, not only ours.
* **The local cycle** (:func:`training_handoff.occupancy`) -- after the
  lease is returned, RESTORING and VERIFYING still use the served model
  exclusively and may swap its adapter on a rejection; and it knows the
  latest moment it can still hold the lane, from its own phase budgets.

An engine without a lease surface (plain Ollama: 404) has nothing to lend,
so it admits. An engine that cannot be asked is "not proven" -- reachability
already has an owner (the lane gate), so this never turns silence into a
refusal.

## Contract

Stdlib only, synchronous (the boot path is), bounded by the probe timeout
the sibling bring-up already uses, and NEVER raises. One reader, three
consumers: the lane gate (the only owner of the verdict), the sibling
bring-up (its status line), and the cockpit client (rendering a refusal).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("Ouroboros.LaneAdmission")

__all__ = ["LaneAdmission", "read_admission", "await_admission", "describe", "EXIT_LANE_LENT"]

#: POSIX ``sysexits.h`` EX_UNAVAILABLE: a service this program needs is not
#: available right now. Distinct from EX_CONFIG (78, nothing resolves on its
#: own) and EX_TEMPFAIL (75, the single-flight collision the cockpit retries
#: within seconds): this one resolves on its own, but in hours.
EXIT_LANE_LENT = 69

#: Engine lease states that refuse generations (J-Prime ``LeaseState``).
_SERVING = "serving"


@dataclass(frozen=True)
class LaneAdmission:
    #: True: serve. False: lent out. None: could not ask (not proven).
    admitting: Optional[bool]
    #: The engine's lease state, ``no_lease_surface`` (an engine with nothing
    #: to lend), or ``unknown``.
    engine_state: str
    holder: str = ""
    purpose: str = ""
    #: When the engine's lease lapses unless renewed (epoch s).
    lease_expires_at: Optional[float] = None
    #: The local cycle's own account (training_handoff.occupancy()).
    cycle: Optional[Dict[str, Any]] = None
    detail: str = ""
    checked_at: float = field(default_factory=time.time)

    @property
    def release_by(self) -> Optional[float]:
        """The latest moment the lane can stay lent: the cycle's bound when
        a cycle of ours holds it (renewals make the lease's own expiry a
        floor, not a bound), else the lease's expiry."""
        if self.cycle and self.cycle.get("release_by"):
            return float(self.cycle["release_by"])
        return self.lease_expires_at

    def release_in_s(self, now: Optional[float] = None) -> Optional[float]:
        rb = self.release_by
        return None if rb is None else max(0.0, rb - (time.time() if now is None else now))


def _cycle_occupancy() -> Optional[Dict[str, Any]]:
    try:
        from backend.core.ouroboros.governance.observability.training_handoff import occupancy
        return occupancy()
    except Exception:  # noqa: BLE001 -- the engine's lease still answers
        logger.debug("[LaneAdmission] cycle occupancy unreadable", exc_info=True)
        return None


def _probe_timeout_s() -> float:
    from backend.core.ouroboros.governance.trinity_siblings import probe_timeout_s
    return probe_timeout_s()


def read_admission(base_url: str, *, timeout_s: Optional[float] = None,
                   cycle_reader: Callable[[], Optional[Dict[str, Any]]] = _cycle_occupancy,
                   ) -> LaneAdmission:
    """One reading of both sources. NEVER raises."""
    cycle = cycle_reader()
    lane_held_by_cycle = bool(cycle and cycle.get("holds_lane"))
    try:
        t = _probe_timeout_s() if timeout_s is None else timeout_s
        with urllib.request.urlopen(base_url.rstrip("/") + "/v1/lease", timeout=t) as r:
            body = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # Nothing to lend on this engine; only a local cycle can occupy it.
            return LaneAdmission(not lane_held_by_cycle, "no_lease_surface", cycle=cycle)
        return LaneAdmission(False if lane_held_by_cycle else None, "unknown", cycle=cycle,
                             detail=f"lease surface answered HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001 -- unreachable/timeout: not proven
        return LaneAdmission(False if lane_held_by_cycle else None, "unknown", cycle=cycle,
                             detail=f"{type(exc).__name__}: {exc}"[:200])
    state = str((body or {}).get("state") or "unknown").lower()
    lease = (body or {}).get("lease") or {}
    expires = lease.get("expires_at")
    # Only an error of THIS lease describes this lease. An engine that does
    # not date its errors cannot prove that, so its error is not repeated.
    err, err_at, since = (body or {}).get("last_error") or "", (body or {}).get("last_error_at"), lease.get("acquired_at")
    current_err = (str(err) if err and isinstance(err_at, (int, float))
                   and (not isinstance(since, (int, float)) or err_at >= since) else "")
    return LaneAdmission(
        admitting=(state == _SERVING) and not lane_held_by_cycle,
        engine_state=state,
        holder=str(lease.get("holder") or ""),
        purpose=str(lease.get("purpose") or ""),
        lease_expires_at=float(expires) if isinstance(expires, (int, float)) else None,
        cycle=cycle,
        detail=current_err,
    )


def await_admission(base_url: str, budget_s: float, *,
                    say: Callable[[str], None] = print,
                    reader: Callable[[str], LaneAdmission] = read_admission,
                    sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic,
                    ) -> LaneAdmission:
    """Wait for the lane only when it will be back within ``budget_s``.

    Adaptive, not a fixed retry: a lane lent for the next few seconds (a
    model reloading after RESTORING, a lease about to lapse) is waited for;
    a lane lent for hours is reported at once, with when it comes back, so
    the operator decides instead of watching a spinner. Each wait is the
    lane's own remaining time, re-read every probe interval, never past the
    budget. Returns the last reading. NEVER raises.
    """
    deadline = clock() + max(0.0, budget_s)
    adm = reader(base_url)
    announced = False
    while adm.admitting is False:
        left = deadline - clock()
        remaining = adm.release_in_s()
        if left <= 0 or remaining is None or remaining > left:
            return adm
        if not announced:
            say(f"  local model is lent ({_who(adm)}); back within {_dur(remaining)} -- waiting")
            announced = True
        # Re-read at the cadence the sibling bring-up probes at: the lane's
        # remaining time can shrink (a restore finishing early), never grow
        # past what was reported without a new reading saying so.
        sleep(max(0.0, min(left, _probe_timeout_s())))
        adm = reader(base_url)
    return adm


def _dur(seconds: float) -> str:
    s = int(round(max(0.0, seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{sec:02d}s" if m else f"{sec}s")


def _who(adm: LaneAdmission) -> str:
    cyc = adm.cycle or {}
    if cyc.get("holds_lane"):
        return f"training cycle {cyc.get('run_id')} in {cyc.get('state')}"
    if adm.holder:
        return f"{adm.holder} ({adm.engine_state})"
    return f"engine {adm.engine_state}"


def describe(adm: LaneAdmission, *, now: Optional[float] = None) -> List[str]:
    """Plain lines for an operator: who has the lane, why, and when it is back."""
    now = time.time() if now is None else now
    lines = ["The local model is not serving this organism right now."]
    cyc = adm.cycle or {}
    if cyc.get("holds_lane"):
        lines.append(f"  holder  : Reactor training cycle {cyc.get('run_id')} "
                     f"({cyc.get('state')}, model {cyc.get('model') or '?'})")
        if cyc.get("trigger"):
            lines.append(f"  started : by {cyc['trigger']}")
    elif adm.holder:
        lines.append(f"  holder  : {adm.holder}")
    if adm.purpose:
        lines.append(f"  purpose : {adm.purpose}")
    lines.append(f"  engine  : {adm.engine_state}" + (f" -- {adm.detail}" if adm.detail else ""))
    rb = adm.release_by
    if rb is not None:
        when = time.strftime("%H:%M", time.localtime(rb))
        bound = "no later than" if cyc.get("release_by") else "lease lapses at (unless renewed)"
        lines.append(f"  back    : {bound} {when} (in {_dur(rb - now)})")
    lines.append("  The cycle hands the card back by itself; nothing needs stopping.")
    lines.append("  Run `ov` again after that time, or check progress with: "
                 "python3 -m backend.core.ouroboros.governance.observability.training_handoff status")
    return lines

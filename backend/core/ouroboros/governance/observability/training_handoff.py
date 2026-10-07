"""Training Lifecycle Handoff -- one exclusive cycle from corpus to a served fine-tune.

## What a cycle is

The served model ``qwen3-coder-ov:30b`` is the qwen3-coder 30B base (frozen,
4-bit) plus a LoRA adapter. A cycle fine-tunes a NEW adapter for the SAME base
on O+V's own landed/judged trajectories (GRPO), and puts it in front of O+V
only if it serves correctly:

    LABELING    git-truth labels for every landed candidate (landing_provenance)
    PREFLIGHT   the trainer's own corpus gate (exit 2 = nothing to learn)
    LEASING     J-Prime drains, stops every engine and VERIFIES the VRAM is free
    TRAINING    reactor's GRPO runner; the lease is renewed while it runs
    CONVERTING  PEFT adapter -> GGUF adapter (reactor's converter)
    PUBLISHING  uploaded to J-Prime's adapter registry, new version active
    RESTORING   lease released; J-Prime reloads the model WITH the new adapter
    VERIFYING   O+V's own client must get schema-valid answers on real tasks
    COMMITTED   ... or ROLLED_BACK to the previous adapter, verified again

## Exclusivity is structural, not polite

* One cycle at a time: a non-blocking ``flock`` on the state directory.
* Never beside a live organism: the organism's own single-flight lock is
  consulted; a cycle refuses while O+V runs, and O+V's boot sees the lease.
* The card is handed over by the process that owns inference (J-Prime's
  lease), so nothing can reload a model mid-training -- the failure observed
  live on 2026-10-07 when "evict then train" let O+V reload the 30B on top of
  a trainer.
* Every exit path -- success, refusal, error, cancellation -- releases the
  lease. If this process itself dies, J-Prime's lease expires and restores
  service only once measured VRAM admits it.

## Why its own process

A 30B GRPO cycle is hours. It used to run INSIDE the organism's shutdown,
which stretched one watchdog's deadline while an independent one killed the
organism mid-teardown (SIGKILL, 2026-10-07). The organism now only REQUESTS
a cycle (:func:`request_cycle`), which starts this module detached under
Task Scheduler so it survives the organism, the terminal and the WSL client
that launched it.

Every transition is appended to ``history.jsonl`` and mirrored to
``state.json``; ``status`` reads them. NEVER raises out of :func:`run_cycle`.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shlex
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.observability import training_trigger as tt

logger = logging.getLogger("Ouroboros.TrainingHandoff")

# --- env keys ---------------------------------------------------------------
_ENV_STATE_DIR = "JARVIS_TRAINING_HANDOFF_DIR"
_ENV_RUNS_DIR = "JARVIS_TRAINING_HANDOFF_RUNS_DIR"
_ENV_BASE_MODEL = "TRINITY_GRPO_BASE_MODEL"
_ENV_TRAIN_ARGS = "TRINITY_GRPO_TRAIN_ARGS"
_ENV_LEASE_TTL = "JARVIS_TRAINING_LEASE_TTL_S"
_ENV_CONVERT_TIMEOUT = "JARVIS_TRAINING_CONVERT_TIMEOUT_S"
_ENV_SMOKE_N = "JARVIS_TRAINING_SMOKE_PROMPTS"
_ENV_SMOKE_TIMEOUT = "JARVIS_TRAINING_SMOKE_TIMEOUT_S"
_ENV_HTTP_TIMEOUT = "JARVIS_TRAINING_JPRIME_TIMEOUT_S"
_ENV_LAUNCHER = "JARVIS_TRAINING_HANDOFF_LAUNCH_CMD"
_ENV_TIME_RESERVE = "JARVIS_TRAINING_TIME_RESERVE_S"

STATES = ("IDLE", "AWAITING_REQUESTER", "LABELING", "PREFLIGHT", "BASELINE", "LEASING", "TRAINING", "CONVERTING", "PUBLISHING",
          "RESTORING", "VERIFYING", "COMMITTED", "ROLLED_BACK", "REFUSED", "FAILED")


def state_dir() -> Path:
    raw = os.environ.get(_ENV_STATE_DIR, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".jarvis" / "training_handoff"


def runs_dir() -> Path:
    raw = os.environ.get(_ENV_RUNS_DIR, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / "grpo-runs"


def _jprime() -> str:
    from backend.core.ouroboros.governance.trinity_siblings import _jprime_url
    return _jprime_url().rstrip("/")


def _model() -> str:
    return (os.environ.get("JARVIS_LOCAL_MODEL_NAME", "") or "").strip()


# ---------------------------------------------------------------------------
# J-Prime over HTTP (stdlib; off the loop)
# ---------------------------------------------------------------------------

class JPrimeError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"J-Prime HTTP {status}: {body[:300]}")
        self.status = status


async def _http(method: str, path: str, *, body: Any = None, data: Optional[bytes] = None,
                headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = None) -> Any:
    url = _jprime() + path
    hdrs = dict(headers or {})
    payload = data
    if body is not None:
        payload = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    t = timeout or tt._num(_ENV_HTTP_TIMEOUT, 900.0, 5.0, 86400.0)

    def call() -> Any:
        req = urllib.request.Request(url, data=payload, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=t) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise JPrimeError(e.code, e.read().decode("utf-8", "replace")) from None
        return json.loads(raw) if raw.strip() else {}
    return await asyncio.to_thread(call)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class Cycle:
    run_id: str
    trigger: str
    state: str = "IDLE"
    started_at: float = field(default_factory=time.time)
    model: str = ""
    base_model: str = ""
    run_dir: str = ""
    lease_token: str = ""
    adapter_version: str = ""
    previous_version: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    outcome: str = ""

    def public(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d.pop("lease_token", None)
        return d


def _record(cycle: Cycle, state: str, **detail: Any) -> None:
    cycle.state = state
    if detail:
        cycle.detail[state.lower()] = detail
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "run_id": cycle.run_id, "state": state, **detail}
    with (d / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
    tmp = d / "state.json.tmp"
    tmp.write_text(json.dumps(cycle.public(), indent=2, default=str), encoding="utf-8")
    os.replace(tmp, d / "state.json")
    logger.warning("[TrainingHandoff] %s %s %s", cycle.run_id, state,
                   json.dumps(detail, default=str)[:300] if detail else "")


def status() -> Dict[str, Any]:
    try:
        return json.loads((state_dir() / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "IDLE"}


@contextlib.contextmanager
def _single_flight():
    """Non-blocking exclusive lock; yields False when another cycle holds it."""
    import fcntl
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    fh = open(d / "cycle.lock", "w")  # noqa: SIM115
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        fh.write(str(os.getpid()))
        fh.flush()
        yield True
    finally:
        fh.close()


def cycle_alive() -> bool:
    """Is a cycle running right now? The kernel lock is the truth, never
    state.json: a cycle that crashed leaves a stale state file, but its lock
    dies with it."""
    try:
        with _single_flight() as free:
            return not free
    except Exception:  # noqa: BLE001 -- cannot probe: say "not proven alive"
        return False


# The phases, in order, from taking the card to the last use of the served
# model. Each is bounded by its OWN knob (the one the phase spends), so the
# latest moment a cycle can still hold the lane is derived, never guessed.
_HOLDING = ("LEASING", "TRAINING", "CONVERTING", "PUBLISHING", "RESTORING", "VERIFYING")


def _phase_budget_s(state: str, detail: Dict[str, Any]) -> float:
    http = tt._num(_ENV_HTTP_TIMEOUT, 900.0, 5.0, 86400.0)
    if state == "TRAINING":
        return tt._num(tt._ENV_TRAIN_TIMEOUT, 43200.0, 60.0, 172800.0)
    if state == "CONVERTING":
        return tt._num(_ENV_CONVERT_TIMEOUT, 900.0, 30.0, 7200.0)
    if state == "VERIFYING":
        tasks = int(((detail or {}).get("verifying") or {}).get("tasks")
                    or tt._num(_ENV_SMOKE_N, 6.0, 1.0, 50.0))
        # The candidate's measurement, and -- on rejection -- the restored
        # incumbent's, plus the reject call between them.
        return 2 * tasks * tt._num(_ENV_SMOKE_TIMEOUT, 600.0, 10.0, 7200.0) + http
    return http       # LEASING / PUBLISHING / RESTORING: one J-Prime call each


def _state_entered_at(run_id: str, state: str) -> Optional[float]:
    """When ``run_id`` entered ``state``, from the append-only history."""
    try:
        with (state_dir() / "history.jsonl").open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - (1 << 16)))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("run_id") == run_id and row.get("state") == state:
            return float(row["ts"])
    return None


def occupancy() -> Optional[Dict[str, Any]]:
    """What a live cycle is doing to the served model, for anyone about to
    depend on it. None when no cycle runs. NEVER raises.

    ``holds_lane`` is True from taking the card until the last verification
    request: the lease covers LEASING..PUBLISHING, and RESTORING/VERIFYING
    still use the served model exclusively (and may swap its adapter on a
    rejection). ``release_by`` is the latest moment that can last, summed
    from each remaining phase's own budget -- an upper bound, not a forecast.
    """
    try:
        if not cycle_alive():
            return None
        st = status()
        state = str(st.get("state") or "")
        out: Dict[str, Any] = {"run_id": st.get("run_id"), "state": state,
                               "trigger": st.get("trigger"), "model": st.get("model"),
                               "holds_lane": state in _HOLDING, "since": None, "release_by": None}
        if state in _HOLDING:
            since = _state_entered_at(str(st.get("run_id")), state)
            out["since"] = since
            if since is not None:
                rest = _HOLDING[_HOLDING.index(state):]
                out["release_by"] = since + sum(_phase_budget_s(s, st.get("detail") or {}) for s in rest)
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[TrainingHandoff] occupancy unreadable", exc_info=True)
        return None


def _organism_live() -> Optional[int]:
    try:
        from backend.core.ouroboros.battle_test.singleton_lock import live_incumbent_pid
        from backend.core.ouroboros.cli.thin_client import repo_root
        return live_incumbent_pid(repo_root(), exclude_pid=os.getpid())
    except Exception:  # noqa: BLE001 -- unknown is not "live"; the lease still guards the card
        return None


# ---------------------------------------------------------------------------
# The requester: the organism that asked for this cycle is still ending
# ---------------------------------------------------------------------------
#
# A session asks for a cycle from inside its own teardown, so for the first
# seconds of the cycle the requester still holds the organism lock. The
# exclusivity gate cannot tell "the organism that is handing over the card"
# from "an organism that is using it", and refused every automatic request
# (2026-10-07: handoff-20261007-075127 refused pid 76605 -- its own caller).
#
# So the request is a durable record naming the requester by (pid, kernel
# start time) -- a pid alone can be reused -- and the latest moment its own
# teardown budget lets it live. The cycle waits for exactly that process
# to exit, on its pidfd (an event, not a poll), until that moment. Any OTHER
# live organism is still a refusal, and so is a requester outliving its own
# deadline: a wedged teardown is not a card handed over.

_REQUEST_FILE = "request.json"


def _proc_start_ticks(pid: int) -> Optional[int]:
    """The kernel's start time for ``pid`` (clock ticks since boot), or None
    when no such process is RUNNING. Together with the pid it names ONE process.

    A zombie (exited, not yet reaped by its parent) still has a /proc entry
    with its old start time, but it holds no lock, no card and no lane: it is
    gone in every sense this module asks about."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace")
        # comm (field 2) may contain spaces and parentheses; fields resume
        # after the LAST ')'. state is field 3 -> index 0; starttime is
        # field 22 -> index 19.
        fields = raw[raw.rindex(")") + 2:].split()
        if fields[0] in ("Z", "X"):
            return None
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _write_request(trigger: str, requester_pid: int, release_within_s: float) -> Dict[str, Any]:
    rec = {"trigger": trigger, "requester_pid": int(requester_pid),
           "requester_start": _proc_start_ticks(requester_pid),
           "requested_at": time.time(), "release_by": time.time() + max(0.0, float(release_within_s))}
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (_REQUEST_FILE + ".tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    os.replace(tmp, d / _REQUEST_FILE)
    return rec


def _read_request() -> Optional[Dict[str, Any]]:
    """The pending request, when it is still within its requester's deadline."""
    try:
        rec = json.loads((state_dir() / _REQUEST_FILE).read_text(encoding="utf-8"))
        if float(rec["release_by"]) >= time.time() and rec.get("requester_start") is not None:
            return rec
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _consume_request() -> None:
    with contextlib.suppress(OSError):
        (state_dir() / _REQUEST_FILE).unlink()


async def _await_exit(pid: int, start: int, timeout_s: float) -> bool:
    """True once the process (pid, start) no longer exists; False at timeout."""
    if _proc_start_ticks(pid) != start:
        return True
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    except (AttributeError, OSError):
        fd = None
    if fd is None:
        # No pidfd on this kernel/interpreter: re-check on the lease's own
        # renewal cadence fraction, never longer than what remains.
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(min(deadline - time.monotonic(), max(0.05, timeout_s / 100.0)))
            if _proc_start_ticks(pid) != start:
                return True
        return _proc_start_ticks(pid) != start
    try:
        # The pid may have been reused between the check and the open.
        if _proc_start_ticks(pid) != start:
            return True
        loop = asyncio.get_running_loop()
        exited = loop.create_future()
        loop.add_reader(fd, lambda: exited.done() or exited.set_result(True))
        try:
            await asyncio.wait_for(exited, timeout=max(0.0, timeout_s))
            return True
        except asyncio.TimeoutError:
            return _proc_start_ticks(pid) != start
        finally:
            loop.remove_reader(fd)
    finally:
        os.close(fd)


async def _blocking_organism(cycle: Cycle) -> Optional[int]:
    """The pid of a live organism this cycle must not take the card from,
    else None. The requester-in-teardown is waited out, not refused."""
    live = _organism_live()
    req = _read_request()
    if live and req and live == req["requester_pid"] and _proc_start_ticks(live) == req["requester_start"]:
        remaining = float(req["release_by"]) - time.time()
        _record(cycle, "AWAITING_REQUESTER", pid=live, trigger=req.get("trigger"),
                within_s=round(max(0.0, remaining), 1))
        if not await _await_exit(live, int(req["requester_start"]), remaining):
            return live
        live = _organism_live()
    return live


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

async def _base_model(model: str) -> str:
    """The HF base the served adapter was trained on, from the adapter's OWN
    header (served by J-Prime /api/show). Env override wins. Empty = refuse:
    fine-tuning a different base than the one served breaks the loop."""
    explicit = os.environ.get(_ENV_BASE_MODEL, "").strip()
    if explicit:
        return explicit
    show = await _http("POST", "/api/show", body={"model": model}, timeout=30)
    for a in show.get("adapters") or []:
        url = str(a.get("general.base_model.0.repo_url") or "")
        if "huggingface.co/" in url:
            return url.split("huggingface.co/", 1)[1].strip("/")
    return ""


def _train_argv(run_dir: Path, base: str) -> Optional[List[str]]:
    """The GRPO runner. TRINITY_GRPO_TRAIN_CMD still wins (an operator's
    experiment); otherwise reactor's own runner with the cycle's paths and
    the operator's extra args. The runner owns its memory-derived defaults."""
    explicit = (os.environ.get(tt._ENV_TRAIN_CMD) or "").strip()
    if explicit:
        return shlex.split(explicit)
    root, py = tt._reactor_root(), tt._reactor_python()
    if not root or not py:
        return None
    from backend.core.ouroboros.governance.observability.trajectory_recorder import events_dir
    # The time the trainer may spend STEPPING: this cycle's training timeout
    # less what the run needs around the steps (the calibration child, the
    # rung's model load, saving). The runner fits its step count to it, so a
    # correctly sized but slow window ends with an adapter instead of being
    # killed by the timeout with nothing.
    budget = (tt._num(tt._ENV_TRAIN_TIMEOUT, 43200.0, 60.0, 172800.0)
              - tt._num(_ENV_TIME_RESERVE, 2700.0, 0.0, 86400.0))
    return [py, "-u", str(root / "scripts" / "run_grpo_training.py"), "--model", base,
            "--telemetry-dir", str(events_dir()), "--output-dir", str(run_dir),
            "--json-out", str(run_dir / "train_report.json"),
            "--time-budget-s", str(max(0.0, budget)),
            *shlex.split(os.environ.get(_ENV_TRAIN_ARGS, ""))]


async def _renew_forever(token: str, ttl: float) -> None:
    while True:
        await asyncio.sleep(max(5.0, ttl / 3.0))
        try:
            await _http("POST", "/v1/lease/renew", body={"token": token}, timeout=30)
        except Exception as exc:  # noqa: BLE001 -- the trainer keeps running; J-Prime's
            # admission still measures the card before it ever reloads.
            logger.error("[TrainingHandoff] lease renew failed: %s", exc)


def _verify_response(text: str, row: Dict[str, Any]) -> str:
    """Would O+V ACT on this answer? Decided by O+V's own parsers -- the same
    two its generation loop dispatches through -- never by a looser check.

    A schema-constrained decoder makes even a corrupted model emit JSON that
    *looks* like an envelope; what a broken adapter cannot fake is a
    candidate that survives O+V's validation (envelope rules, AST, no
    placeholders) or a tool call naming a tool O+V has. Returns the accepted
    shape; raises with the parser's own reason otherwise."""
    from backend.core.ouroboros.governance import providers
    calls = providers._parse_tool_call_response(text)
    if calls:
        from backend.core.ouroboros.governance.tool_executor import _L1_MANIFESTS
        unknown = [c.name for c in calls if c.name not in _L1_MANIFESTS and not c.name.startswith("mcp_")]
        if unknown:
            raise ValueError(f"tool call names unknown tool(s) {unknown}")
        return f"tool_calls:{','.join(c.name for c in calls)}"
    from backend.core.ouroboros.governance.op_context import OperationContext
    from backend.core.ouroboros.cli.thin_client import repo_root
    target = str(row.get("file_path") or "")
    ctx = OperationContext.create(target_files=(target,) if target else (),
                                  description=f"post-training verification of {row.get('op_id', '')}")
    src = repo_root() / target if target else None
    try:
        src_hash = hashlib.sha256(src.read_bytes()).hexdigest() if src and src.is_file() else ""
    except OSError:
        src_hash = ""
    res = providers._parse_generation_response(text, "jprime-verify", 0.0, ctx, src_hash,
                                               str(src or ""), repo_root=repo_root())
    if getattr(res, "is_noop", False):
        return "noop"
    return f"candidates:{len(getattr(res, 'candidates', ()) or ())}"


async def _smoke(model: str, rows: List[Dict[str, Any]]) -> Tuple[int, List[str]]:
    """How many real tasks the SERVED model answers in a form O+V would act
    on (judged by O+V's own parsers, :func:`_verify_response`), through O+V's
    own client, constraint ladder and transport.

    Greedy decoding (temperature 0): the score is a property of the weights,
    not of a sample, so the incumbent and the candidate are compared on the
    same footing and a restored adapter reproduces its baseline exactly.
    Returns (accepted, per-task notes)."""
    from backend.core.ouroboros.governance.local_inference_director import LocalConfig, LocalPrimeClient
    client = LocalPrimeClient(LocalConfig.from_env())
    notes: List[str] = []
    accepted = 0
    timeout = tt._num(_ENV_SMOKE_TIMEOUT, 600.0, 10.0, 7200.0)
    # Reproducible measurement: J-Prime serves this model with no prompt-cache
    # reuse while the scope is open (greedy decoding drifts between cold and
    # warm cache). Bounded by the most this measurement can take, so an
    # abandoned scope closes itself.
    await _http("POST", f"/v1/models/{model}/evaluation",
                body={"on": True, "ttl_s": timeout * max(1, len(rows)) + 60}, timeout=30)
    try:
        for i, row in enumerate(rows):
            prompt = row["prompt"]
            try:
                res = await asyncio.wait_for(client.complete(
                    system="", user=prompt, prompt_tokens=max(1, len(prompt) // 4), temperature=0.0),
                    timeout=timeout)
                text = getattr(res, "text", "") or getattr(res, "content", "")
                notes.append(f"{i}:ok:{_verify_response(text, row)}")
                accepted += 1
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{i}:FAIL:{type(exc).__name__}:{str(exc)[:160]}")
    finally:
        with contextlib.suppress(Exception):
            if hasattr(client, "aclose"):
                await client.aclose()
        with contextlib.suppress(Exception):
            await _http("POST", f"/v1/models/{model}/evaluation", body={"on": False}, timeout=30)
    return accepted, notes


_ENV_VERIFY_TOLERANCE = "JARVIS_TRAINING_VERIFY_TOLERANCE"


def _required(baseline: int) -> int:
    """Tasks a candidate must pass: the incumbent's score less the operator's
    tolerance (default 0 -- no regression), and never zero: a model that
    answers nothing O+V can use is not shipped whatever the baseline."""
    tol = int(tt._num(_ENV_VERIFY_TOLERANCE, 0.0, 0.0, 1000.0))
    return max(1, baseline - tol)


def _smoke_rows(n: int) -> List[Dict[str, Any]]:
    """Real tasks, newest first: rows whose candidate LANDED (git-proven),
    else the newest genuine rows. A fine-tune that cannot answer the work O+V
    actually does is not shipped. Carries the target file so the candidate
    parser can judge the answer against the real source."""
    from backend.core.ouroboros.governance.observability.landing_provenance import read_labels
    from backend.core.ouroboros.governance.observability.trajectory_recorder import events_dir
    landed = {eid for eid, lab in read_labels()[0].items() if lab.get("landed") and lab.get("surviving")}
    picked: List[Dict[str, Any]] = []
    fallback: List[Dict[str, Any]] = []
    seen: set = set()
    for f in sorted(events_dir().glob("experience_*.jsonl"), reverse=True):
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            prompt = str(row.get("user_input") or "")
            if not prompt or prompt in seen:
                continue
            seen.add(prompt)
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            item = {"prompt": prompt, "file_path": meta.get("file_path", ""), "op_id": meta.get("op_id", "")}
            (picked if row.get("event_id") in landed else fallback).append(item)
        if len(picked) >= n:
            break
    return (picked + fallback)[:n]


# ---------------------------------------------------------------------------
# Yield: is there enough NEW, unlearned evidence to be worth a cycle?
# ---------------------------------------------------------------------------

_ENV_MIN_BATCH = "JARVIS_MIN_TRAINING_BATCH"


def min_training_batch() -> int:
    return int(tt._num(_ENV_MIN_BATCH, 15.0, 1.0, 1_000_000.0))


async def _trained_through(model: str) -> Tuple[float, str]:
    """When the ACTIVE adapter's evidence ends, from J-Prime's own records.

    A version this loop published carries ``source.trained_through`` (the
    newest landing it learned from). One that predates the registry (origin,
    e.g. the Ollama-built adapter) is bounded by its weight file's mtime:
    nothing committed after the file was written can have trained it.
    No adapter at all -> 0 (every landing is unlearned)."""
    # The decision is shared with the cockpit (served_identity), so the
    # adapter training counts against is the adapter the operator is shown.
    from backend.core.ouroboros.governance.served_identity import adapter_provenance
    versions = await _http("GET", f"/v1/adapters/{model}", timeout=30)
    show = await _http("POST", "/api/show", body={"model": model}, timeout=30)
    prov = adapter_provenance(versions, show.get("adapters"))   # raises when unknowable
    return float(prov.trained_through or 0.0), prov.source


async def training_yield(model: Optional[str] = None) -> Dict[str, Any]:
    """Count landed-and-surviving commits newer than what the active adapter
    learned. Git is the clock (each label's commit time); J-Prime says where
    the served adapter's evidence ends. NEVER raises -- an unanswerable yield
    is reported with ``error`` and treated as "not met" by the callers."""
    model = model or _model()
    out: Dict[str, Any] = {"model": model, "threshold": min_training_batch()}
    try:
        from backend.core.ouroboros.governance.observability.landing_provenance import (
            label_landings, read_labels)
        await label_landings()                      # current with git before counting
        labels, _, _ = read_labels()
        commits: Dict[str, float] = {}
        for lab in labels.values():
            if lab.get("landed") and lab.get("surviving"):
                commits[lab["commit_sha"]] = float(lab.get("committed_at") or 0.0)
        missing = [s for s, t in commits.items() if not t]
        if missing:                                 # labels written before committed_at existed
            from backend.core.ouroboros.governance.observability.landing_provenance import (
                _git, resolve_repo_and_ref)
            root, _ = await resolve_repo_and_ref()
            if root is not None:
                rc, txt = await _git(["show", "-s", "--format=%H %ct", *missing], root)
                for line in txt.splitlines() if rc == 0 else []:
                    sha, _, ct = line.partition(" ")
                    if sha in commits and ct.strip().isdigit():
                        commits[sha] = float(ct)
        through, source = await _trained_through(model)
        newer = sorted(t for t in commits.values() if t > through)
        out.update({"unlearned": len(newer), "landed_total": len(commits), "trained_through": through,
                    "trained_through_source": source, "newest_unlearned": newer[-1] if newer else None})
        out["met"] = len(newer) >= out["threshold"]
    except Exception as exc:  # noqa: BLE001
        out.update({"met": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
    return out


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------

async def run_cycle(*, trigger: str = "manual", force: bool = False,
                    deploy_gguf: Optional[Path] = None) -> Dict[str, Any]:
    """One exclusive cycle. ``deploy_gguf`` skips training and runs an
    existing adapter through the SAME publish/verify/reject tail. ``force``
    bypasses the yield threshold (never the other gates). Returns the cycle
    record. NEVER raises (except CancelledError, after the lease is released)."""
    run_id = time.strftime("handoff-%Y%m%d-%H%M%S")
    cycle = Cycle(run_id=run_id, trigger=trigger, model=_model())
    with _single_flight() as mine:
        if not mine:
            return {"run_id": run_id, "state": "REFUSED", "outcome": "another cycle is running"}
        try:
            if deploy_gguf is not None:
                return await _deploy_only(cycle, Path(deploy_gguf))
            return await _run(cycle, force=force)
        finally:
            _consume_request()
            if cycle.lease_token:
                # Every exit path gives the card back and restores serving.
                with contextlib.suppress(Exception):
                    await asyncio.shield(_http("POST", "/v1/lease/release",
                                               body={"token": cycle.lease_token}))
                    cycle.lease_token = ""


async def _finish(cycle: Cycle, state: str, outcome: str, **detail: Any) -> Dict[str, Any]:
    cycle.outcome = outcome
    _record(cycle, state, outcome=outcome, **detail)
    return cycle.public()


async def _baseline(cycle: Cycle) -> Tuple[List[Dict[str, Any]], int]:
    """The incumbent's score on the verification tasks, measured BEFORE the
    card is taken: the bar a candidate must meet and a rollback must
    reproduce. Same tasks for all three measurements."""
    rows = _smoke_rows(int(tt._num(_ENV_SMOKE_N, 6.0, 1.0, 50.0)))
    accepted, notes = await _smoke(cycle.model, rows) if rows else (0, [])
    _record(cycle, "BASELINE", tasks=len(rows), accepted=accepted, notes=notes)
    return rows, accepted


async def _lease(cycle: Cycle, purpose: str) -> Optional[Dict[str, Any]]:
    """Take the card from J-Prime and verify it on this side too. Returns
    None on success, else the finished (FAILED) cycle record."""
    live = _organism_live()
    if live:
        return await _finish(cycle, "REFUSED", f"organism pid {live} started before the card was taken")
    _record(cycle, "LEASING")
    ttl = tt._num(_ENV_LEASE_TTL, 900.0, 60.0, 86400.0)
    try:
        got = await _http("POST", "/v1/lease/acquire", body={
            "holder": f"training-handoff:{cycle.run_id}@{socket.gethostname()}",
            "purpose": purpose, "ttl_s": ttl})
    except Exception as exc:  # noqa: BLE001
        return await _finish(cycle, "FAILED", f"lease refused: {exc}")
    cycle.lease_token = got["token"]
    need = int(tt._num(tt._ENV_FREE_MIB, 24000.0, 0.0, 1_000_000.0))
    free = await tt._gpu_free_mib()
    _record(cycle, "LEASING", freed_mib=got.get("freed_mib"), gpu_free_mib=free, need_mib=need)
    if free is not None and free < need:
        return await _finish(cycle, "FAILED", f"card not free after lease: {free} MiB < {need}")
    return None


async def _run(cycle: Cycle, *, force: bool) -> Dict[str, Any]:
    live = await _blocking_organism(cycle)
    if live:
        return await _finish(cycle, "REFUSED", f"organism pid {live} is running; a cycle needs the card")
    if not cycle.model:
        return await _finish(cycle, "REFUSED", "JARVIS_LOCAL_MODEL_NAME is unset")

    # 1. Labels from git truth, then the yield: is there enough NEW evidence?
    _record(cycle, "LABELING")
    y = await training_yield(cycle.model)
    cycle.detail["labeling"] = {"yield": y}
    if not y.get("met") and not force:
        return await _finish(cycle, "REFUSED", "below_training_batch", yield_=y)

    # 2. The trainer's own corpus gate.
    _record(cycle, "PREFLIGHT")
    pre = tt._preflight_cmd()
    if not pre:
        return await _finish(cycle, "REFUSED", "preflight command unresolved")
    rc, out = await tt._run(pre, timeout_s=tt._num(tt._ENV_PREFLIGHT_TIMEOUT, 300.0, 10.0, 3600.0))
    if rc == 2:
        return await _finish(cycle, "REFUSED", "corpus_not_trainable", tail=out[-600:])
    if rc != 0:
        return await _finish(cycle, "FAILED", f"preflight rc={rc}", tail=out[-600:])

    try:
        cycle.base_model = await _base_model(cycle.model)
    except Exception as exc:  # noqa: BLE001
        return await _finish(cycle, "FAILED", f"cannot read served model provenance: {exc}")
    if not cycle.base_model:
        return await _finish(cycle, "REFUSED",
                             f"the served {cycle.model} names no HF base; set {_ENV_BASE_MODEL}")
    run_dir = runs_dir() / cycle.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    cycle.run_dir = str(run_dir)
    argv = _train_argv(run_dir, cycle.base_model)
    if not argv:
        return await _finish(cycle, "REFUSED", "train command unresolved")

    # 3. The bar, measured on the incumbent while it still serves.
    rows, baseline = await _baseline(cycle)
    if not rows:
        return await _finish(cycle, "REFUSED", "no verification tasks available")

    # 4. Exclusive handoff of the card, verified on both sides.
    failed = await _lease(cycle, f"GRPO fine-tune of {cycle.model}")
    if failed:
        return failed

    # 4. Train, renewing the lease for as long as the trainer runs.
    _record(cycle, "TRAINING", argv=argv)
    renew = asyncio.create_task(_renew_forever(cycle.lease_token, tt._num(_ENV_LEASE_TTL, 900.0, 60.0, 86400.0)))
    try:
        # The trainer's output is the only record of its per-step metrics
        # (rewards, clipped ratio, grad norms). It streams to train.log as
        # it is written: watchable for the hours the run takes, and kept
        # when a timeout reaps the trainer. Without it a "successful" cycle
        # cannot show whether the policy loss contributed or only the
        # router's auxiliary loss did.
        rc, out = await tt._run(argv, timeout_s=tt._num(tt._ENV_TRAIN_TIMEOUT, 43200.0, 60.0, 172800.0),
                                cwd=tt._reactor_root(), log_path=run_dir / "train.log")
    finally:
        renew.cancel()
    report: Dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        report = json.loads((run_dir / "train_report.json").read_text(encoding="utf-8"))
    if rc != 0:
        state = "REFUSED" if rc == 2 else "FAILED"
        return await _finish(cycle, state, f"trainer rc={rc}", refused=report.get("refused"),
                             tail=out[-800:])
    if not (run_dir / "adapter_model.safetensors").is_file():
        return await _finish(cycle, "FAILED", "trainer exited 0 but saved no adapter", tail=out[-800:])

    # 5. PEFT -> GGUF, through reactor's converter (by path: no torch import).
    _record(cycle, "CONVERTING")
    gguf = run_dir / "adapter.gguf"
    conv = [tt._reactor_python() or "python3",
            str(tt._reactor_root() / "reactor_core" / "quantization" / "adapter_gguf.py"),
            str(run_dir), str(gguf)]
    rc, out = await tt._run(conv, timeout_s=tt._num(_ENV_CONVERT_TIMEOUT, 900.0, 30.0, 7200.0))
    if rc != 0 or not gguf.is_file():
        return await _finish(cycle, "FAILED", f"conversion rc={rc}", tail=out[-600:])

    source = {"run_id": cycle.run_id, "base": cycle.base_model,
              "steps": (report.get("result") or {}).get("global_step"),
              # The newest landing this corpus held: the yield's next cutoff.
              "trained_through": y.get("newest_unlearned") or y.get("trained_through")}
    return await _publish_and_verify(cycle, gguf, source, rows, baseline, train=_train_summary(report))


async def _deploy_only(cycle: Cycle, gguf: Path) -> Dict[str, Any]:
    """An adapter produced elsewhere enters service through EXACTLY the gates a
    trained one does: exclusive lease, registry validation, restore, O+V's
    own verification, reject-and-restore on failure."""
    live = await _blocking_organism(cycle)
    if live:
        return await _finish(cycle, "REFUSED", f"organism pid {live} is running; a deploy needs the card")
    if not cycle.model:
        return await _finish(cycle, "REFUSED", "JARVIS_LOCAL_MODEL_NAME is unset")
    if not gguf.is_file():
        return await _finish(cycle, "FAILED", f"no adapter at {gguf}")
    rows, baseline = await _baseline(cycle)
    if not rows:
        return await _finish(cycle, "REFUSED", "no verification tasks available")
    failed = await _lease(cycle, f"adapter deploy to {cycle.model}")
    if failed:
        return failed
    return await _publish_and_verify(cycle, gguf, {"run_id": cycle.run_id, "deployed_from": str(gguf)},
                                     rows, baseline)


async def _publish_and_verify(cycle: Cycle, gguf: Path, source: Dict[str, Any],
                              rows: List[Dict[str, Any]], baseline: int, **detail: Any) -> Dict[str, Any]:
    """The shared tail, entered holding the lease: publish -> restore ->
    verify against the incumbent's baseline -> COMMITTED; or REJECT (weights
    deleted, last good restored) and verify the restoration reproduces the
    baseline -> ROLLED_BACK."""
    _record(cycle, "PUBLISHING", gguf_bytes=gguf.stat().st_size)
    data = gguf.read_bytes()
    try:
        pub = await _http("POST", f"/v1/adapters/{cycle.model}/publish", data=data, headers={
            "X-Adapter-SHA256": hashlib.sha256(data).hexdigest(),
            "X-Adapter-Source": json.dumps(source, default=str),
            "Content-Type": "application/octet-stream"})
    except Exception as exc:  # noqa: BLE001 -- refused at the registry: nothing went live
        return await _finish(cycle, "FAILED", f"publish refused: {exc}", **detail)
    cycle.adapter_version, cycle.previous_version = pub.get("active", ""), pub.get("previous", "")

    # Give the card back; J-Prime reloads the model WITH the new adapter.
    _record(cycle, "RESTORING", version=cycle.adapter_version)
    restored = await _http("POST", "/v1/lease/release",
                           body={"token": cycle.lease_token, "restore_models": [cycle.model]})
    cycle.lease_token = ""

    _record(cycle, "VERIFYING", restore=restored, baseline=baseline, tasks=len(rows))
    need = _required(baseline)
    notes: List[str] = []
    if restored.get("failed"):
        reason = f"failed to load: {restored['failed']}"
    else:
        accepted, notes = await _smoke(cycle.model, rows)
        if accepted >= need:
            return await _finish(cycle, "COMMITTED", f"{cycle.model} now serves {cycle.adapter_version} "
                                 f"({accepted}/{len(rows)} vs incumbent {baseline}/{len(rows)})",
                                 smoke=notes, **detail)
        reason = (f"verification {accepted}/{len(rows)} below the incumbent's {baseline}/{len(rows)} "
                  f"(need {need}): {'; '.join(n for n in notes if ':FAIL:' in n)[:240]}")

    rej = await _http("POST", f"/v1/adapters/{cycle.model}/reject",
                      body={"version": cycle.adapter_version, "reason": reason})
    back, notes_back = await _smoke(cycle.model, rows)
    verified = back >= baseline
    return await _finish(cycle, "ROLLED_BACK",
                         f"{cycle.adapter_version} rejected ({reason[:160]}); serving {rej.get('active')} "
                         f"again, {back}/{len(rows)} vs baseline {baseline}/{len(rows)} -- "
                         f"{'verified' if verified else 'UNVERIFIED -- investigate'}",
                         smoke_new=notes or [reason], smoke_rollback=notes_back, rejected=rej,
                         rollback_verified=verified, **detail)


def _train_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("refused", "corpus", "training_prompts", "ladder", "result", "output_dir")
    return {k: report.get(k) for k in keep if k in report}


# ---------------------------------------------------------------------------
# Requesting a cycle (from the organism) -- detached, never in its teardown
# ---------------------------------------------------------------------------

def request_timeout_s() -> float:
    """Bound on starting a detached cycle. ONE reader: the request uses it,
    and the organism's shutdown deadline adds exactly this for the hook."""
    return tt._num("JARVIS_TRAINING_HANDOFF_REQUEST_TIMEOUT_S", 180.0, 10.0, 1800.0)


def request_cycle(*, trigger: str, requester_pid: Optional[int] = None,
                  release_within_s: Optional[float] = None) -> Dict[str, Any]:
    """Start a cycle that outlives the caller. Returns at once. NEVER raises.

    ``JARVIS_TRAINING_HANDOFF_LAUNCH_CMD`` declares how (on this host:
    Task Scheduler via scripts/windows/start_detached_wsl.ps1, so the WSL VM
    stays up for the hours a 30B cycle takes). ``{trigger}`` is substituted.

    An organism asking from its own teardown names itself
    (``requester_pid``) and the most its teardown may still take
    (``release_within_s``); the cycle waits for exactly that process to exit
    instead of refusing it. See :func:`_blocking_organism`.
    """
    cmd = (os.environ.get(_ENV_LAUNCHER, "") or "").strip()
    if not cmd:
        return {"requested": False, "reason": f"{_ENV_LAUNCHER} is unset"}
    if cycle_alive():
        return {"requested": False, "reason": f"a cycle is in progress ({status().get('state')})"}
    import subprocess
    try:
        if requester_pid is not None:
            _write_request(trigger, requester_pid,
                           request_timeout_s() if release_within_s is None else release_within_s)
        argv = [p.replace("{trigger}", trigger) for p in shlex.split(cmd)]
        log = state_dir() / "launch.log"
        state_dir().mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as sink:
            proc = subprocess.run(argv, stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  timeout=request_timeout_s())
        return {"requested": proc.returncode == 0, "rc": proc.returncode, "log": str(log)}
    except Exception as exc:  # noqa: BLE001
        return {"requested": False, "reason": repr(exc)}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="training_handoff")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one cycle in the foreground")
    r.add_argument("--trigger", default="manual")
    r.add_argument("--force", action="store_true", help="bypass the yield threshold (never the other gates)")
    d = sub.add_parser("deploy", help="put an existing adapter GGUF through publish/verify/reject")
    d.add_argument("--gguf", required=True)
    d.add_argument("--trigger", default="deploy")
    sub.add_parser("yield", help="how much unlearned landed evidence has accumulated")
    q = sub.add_parser("request", help="start a detached cycle")
    q.add_argument("--trigger", default="manual")
    sub.add_parser("status")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # The organism's own configuration (model pin, J-Prime URL, the roadmap
    # secret the provenance ledger is MAC'd with), through the canonical loader.
    from backend.core.env_bootstrap import load_env_once
    load_env_once()
    if args.cmd == "status":
        print(json.dumps(status(), indent=2, default=str))
        return 0
    if args.cmd == "request":
        out = request_cycle(trigger=args.trigger)
        print(json.dumps(out))
        return 0 if out.get("requested") else 1
    if args.cmd == "yield":
        print(json.dumps(asyncio.run(training_yield()), indent=2, default=str))
        return 0
    if args.cmd == "deploy":
        res = asyncio.run(run_cycle(trigger=args.trigger, deploy_gguf=Path(args.gguf)))
    else:
        res = asyncio.run(run_cycle(trigger=args.trigger, force=args.force))
    print(json.dumps(res, indent=2, default=str))
    return 0 if res.get("state") in ("COMMITTED", "REFUSED") else 1


if __name__ == "__main__":
    raise SystemExit(main())

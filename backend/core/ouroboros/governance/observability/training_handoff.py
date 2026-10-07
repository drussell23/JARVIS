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

STATES = ("IDLE", "LABELING", "PREFLIGHT", "LEASING", "TRAINING", "CONVERTING", "PUBLISHING",
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


def _organism_live() -> Optional[int]:
    try:
        from backend.core.ouroboros.battle_test.singleton_lock import live_incumbent_pid
        from backend.core.ouroboros.cli.thin_client import repo_root
        return live_incumbent_pid(repo_root(), exclude_pid=os.getpid())
    except Exception:  # noqa: BLE001 -- unknown is not "live"; the lease still guards the card
        return None


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
    return [py, "-u", str(root / "scripts" / "run_grpo_training.py"), "--model", base,
            "--telemetry-dir", str(events_dir()), "--output-dir", str(run_dir),
            "--json-out", str(run_dir / "train_report.json"),
            *shlex.split(os.environ.get(_ENV_TRAIN_ARGS, ""))]


async def _renew_forever(token: str, ttl: float) -> None:
    while True:
        await asyncio.sleep(max(5.0, ttl / 3.0))
        try:
            await _http("POST", "/v1/lease/renew", body={"token": token}, timeout=30)
        except Exception as exc:  # noqa: BLE001 -- the trainer keeps running; J-Prime's
            # admission still measures the card before it ever reloads.
            logger.error("[TrainingHandoff] lease renew failed: %s", exc)


async def _smoke(model: str, prompts: List[str]) -> Tuple[bool, List[str]]:
    """O+V's OWN client against the restored model: every answer must be a
    schema-valid O+V envelope. Same client, same constraint ladder, same
    transport the organism uses -- not a second opinion."""
    from backend.core.ouroboros.governance.local_inference_director import LocalConfig, LocalPrimeClient
    client = LocalPrimeClient(LocalConfig.from_env())
    notes: List[str] = []
    ok = True
    timeout = tt._num(_ENV_SMOKE_TIMEOUT, 600.0, 10.0, 7200.0)
    try:
        for i, prompt in enumerate(prompts):
            try:
                res = await asyncio.wait_for(client.complete(
                    system="", user=prompt, prompt_tokens=max(1, len(prompt) // 4)), timeout=timeout)
                text = getattr(res, "text", "") or getattr(res, "content", "")
                env = json.loads(text)
                if not isinstance(env, dict) or not env.get("schema_version"):
                    raise ValueError("no schema_version in envelope")
                notes.append(f"{i}:ok:{env.get('schema_version')}")
            except Exception as exc:  # noqa: BLE001
                ok = False
                notes.append(f"{i}:FAIL:{type(exc).__name__}:{str(exc)[:120]}")
    finally:
        with contextlib.suppress(Exception):
            if hasattr(client, "aclose"):
                await client.aclose()
    return ok, notes


def _smoke_prompts(n: int) -> List[str]:
    """Real tasks, newest first: prompts whose candidate LANDED (git-proven),
    else the newest genuine prompts. A fine-tune that cannot answer the work
    O+V actually does is not shipped."""
    from backend.core.ouroboros.governance.observability.landing_provenance import read_labels
    from backend.core.ouroboros.governance.observability.trajectory_recorder import events_dir
    landed = {eid for eid, lab in read_labels()[0].items() if lab.get("landed") and lab.get("surviving")}
    picked: List[str] = []
    fallback: List[str] = []
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
            if not prompt or prompt in picked or prompt in fallback:
                continue
            (picked if row.get("event_id") in landed else fallback).append(prompt)
        if len(picked) >= n:
            break
    return (picked + fallback)[:n]


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------

async def run_cycle(*, trigger: str = "manual") -> Dict[str, Any]:
    """One exclusive cycle. Returns the cycle record. NEVER raises (except
    CancelledError, after the lease has been released)."""
    run_id = time.strftime("handoff-%Y%m%d-%H%M%S")
    cycle = Cycle(run_id=run_id, trigger=trigger, model=_model())
    with _single_flight() as mine:
        if not mine:
            return {"run_id": run_id, "state": "REFUSED", "outcome": "another cycle is running"}
        try:
            return await _run(cycle)
        finally:
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


async def _run(cycle: Cycle) -> Dict[str, Any]:
    live = _organism_live()
    if live:
        return await _finish(cycle, "REFUSED", f"organism pid {live} is running; a cycle needs the card")
    if not cycle.model:
        return await _finish(cycle, "REFUSED", "JARVIS_LOCAL_MODEL_NAME is unset")

    # 1. Labels from git truth, so the corpus the gate reads is current.
    _record(cycle, "LABELING")
    from backend.core.ouroboros.governance.observability.landing_provenance import label_landings
    lab = await label_landings()
    cycle.detail["labeling"] = {"summary": lab.summary()}

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

    # 3. Exclusive handoff of the card, verified on both sides.
    _record(cycle, "LEASING")
    ttl = tt._num(_ENV_LEASE_TTL, 900.0, 60.0, 86400.0)
    try:
        got = await _http("POST", "/v1/lease/acquire", body={
            "holder": f"training-handoff:{cycle.run_id}@{socket.gethostname()}",
            "purpose": f"GRPO fine-tune of {cycle.model}", "ttl_s": ttl})
    except Exception as exc:  # noqa: BLE001
        return await _finish(cycle, "FAILED", f"lease refused: {exc}")
    cycle.lease_token = got["token"]
    need = int(tt._num(tt._ENV_FREE_MIB, 24000.0, 0.0, 1_000_000.0))
    free = await tt._gpu_free_mib()
    _record(cycle, "LEASING", freed_mib=got.get("freed_mib"), gpu_free_mib=free, need_mib=need)
    if free is not None and free < need:
        return await _finish(cycle, "FAILED", f"card not free after lease: {free} MiB < {need}")

    # 4. Train, renewing the lease for as long as the trainer runs.
    _record(cycle, "TRAINING", argv=argv)
    renew = asyncio.create_task(_renew_forever(cycle.lease_token, ttl))
    try:
        rc, out = await tt._run(argv, timeout_s=tt._num(tt._ENV_TRAIN_TIMEOUT, 43200.0, 60.0, 172800.0),
                                cwd=tt._reactor_root())
    finally:
        renew.cancel()
    # The trainer's full output is the only record of its per-step metrics
    # (rewards, clipped ratio, grad norms): kept with the run, never only in
    # memory. Without it a "successful" cycle cannot show whether the policy
    # loss contributed or only the router's auxiliary loss did.
    with contextlib.suppress(OSError):
        (run_dir / "train.log").write_text(out, encoding="utf-8")
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

    # 6. Publish to J-Prime's registry (validated there from the file's header).
    _record(cycle, "PUBLISHING", gguf_bytes=gguf.stat().st_size)
    data = gguf.read_bytes()
    try:
        pub = await _http("POST", f"/v1/adapters/{cycle.model}/publish", data=data, headers={
            "X-Adapter-SHA256": hashlib.sha256(data).hexdigest(),
            "X-Adapter-Source": json.dumps({"run_id": cycle.run_id, "base": cycle.base_model,
                                            "steps": (report.get("result") or {}).get("global_step")}),
            "Content-Type": "application/octet-stream"})
    except Exception as exc:  # noqa: BLE001
        return await _finish(cycle, "FAILED", f"publish refused: {exc}")
    cycle.adapter_version, cycle.previous_version = pub.get("active", ""), pub.get("previous", "")

    # 7. Give the card back; J-Prime reloads the model WITH the new adapter.
    _record(cycle, "RESTORING", version=cycle.adapter_version)
    restored = await _http("POST", "/v1/lease/release",
                           body={"token": cycle.lease_token, "restore_models": [cycle.model]})
    cycle.lease_token = ""

    # 8. Verify by serving -- or roll back and verify THAT.
    _record(cycle, "VERIFYING", restore=restored)
    prompts = _smoke_prompts(int(tt._num(_ENV_SMOKE_N, 3.0, 1.0, 50.0)))
    ok = not restored.get("failed") and bool(prompts)
    notes: List[str] = []
    if ok:
        ok, notes = await _smoke(cycle.model, prompts)
    if ok:
        return await _finish(cycle, "COMMITTED", f"{cycle.model} now serves {cycle.adapter_version}",
                             smoke=notes, train=_train_summary(report))
    rb = await _http("POST", f"/v1/adapters/{cycle.model}/rollback")
    ok_back, notes_back = await _smoke(cycle.model, prompts) if prompts else (False, ["no prompts"])
    return await _finish(cycle, "ROLLED_BACK",
                         f"new adapter failed verification; serving {rb.get('active')} again "
                         f"({'verified' if ok_back else 'UNVERIFIED -- investigate'})",
                         smoke_new=notes or [str(restored.get("failed"))], smoke_rollback=notes_back,
                         train=_train_summary(report))


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


def request_cycle(*, trigger: str) -> Dict[str, Any]:
    """Start a cycle that outlives the caller. Returns at once. NEVER raises.

    ``JARVIS_TRAINING_HANDOFF_LAUNCH_CMD`` declares how (on this host:
    Task Scheduler via scripts/windows/start_detached_wsl.ps1, so the WSL VM
    stays up for the hours a 30B cycle takes). ``{trigger}`` is substituted.
    """
    cmd = (os.environ.get(_ENV_LAUNCHER, "") or "").strip()
    if not cmd:
        return {"requested": False, "reason": f"{_ENV_LAUNCHER} is unset"}
    # The kernel lock, not state.json, says whether a cycle is alive: a cycle
    # that crashed leaves a stale state file but its lock dies with it.
    with _single_flight() as free:
        if not free:
            return {"requested": False, "reason": f"a cycle is in progress ({status().get('state')})"}
    import subprocess
    try:
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
    res = asyncio.run(run_cycle(trigger=args.trigger))
    print(json.dumps(res, indent=2, default=str))
    return 0 if res.get("state") in ("COMMITTED", "REFUSED") else 1


if __name__ == "__main__":
    raise SystemExit(main())

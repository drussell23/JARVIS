"""Held-out benchmark -- is the served model more CAPABLE, on tasks it never trained on?

The training cycle's own gate (6 verification tasks) answers "does the new
adapter still answer in a form O+V can act on". The productivity tracker
answers "does the organism land more per hour" -- real, but exposed to how
much landable work each session found. Neither is a controlled measurement
of capability. This is.

## What a task is

A change a HUMAN made to this repository, with tests that prove it:

* a commit with no O+V ``Op-ID`` trailer (so it never passed through O+V and
  can never be in the training corpus), that modifies exactly ONE existing
  backend source file and adds or modifies at least one test file;
* the task: the commit's own subject and body as the goal, the source file
  at the PARENT commit as the code to change;
* the hidden tests: the commit's test files. They are never in the prompt
  (the snapshot is the parent tree, where they do not exist in this form).

A candidate task is kept only when its hidden tests are DECISIVE, measured
when the task set is built: run against the parent's source they do not all
pass, and against the commit's source they all do. A test that passes either
way measures nothing; one that fails either way measures the environment.

The repository was private until 2025-08 -- after the base model's release --
so the base model cannot have seen these commits in pre-training either.

## What a score is -- and what it deliberately is not

Per task: the share of its hidden tests that pass with the model's change
applied, normalised by the share the UNCHANGED source already passes::

    gain = (passed_with_candidate - passed_before) / (1 - passed_before)

clipped to [0, 1]: 0 = no better than leaving the code alone, 1 = all hidden
tests pass. Continuous (partial credit), and ground truth.

It is NOT the training reward. GRPO's reward (Reactor's tiered verifier) is
what the policy is optimised against; scoring the benchmark with it would
measure how well the model learned to please its reward, not whether it got
more capable. The reward's score is recorded BESIDE the gain instead, so a
rising reward with a flat gain -- reward hacking -- is visible.

## Determinism

The task set is BUILT once and frozen: each task stores its prompt (rendered
by O+V's own full codegen builder against the parent snapshot, with the
output contract production would choose for the served model at build time)
and its hidden test files verbatim; the set carries a fingerprint over all of
it. A run decodes greedily (temperature 0) inside J-Prime's reproducible-
evaluation scope, so two runs of the same weights on the same set agree, and
runs are only ever compared on the same fingerprint, task by task (paired).
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.HeldoutBenchmark")

TASKSET_SCHEMA = 1

_ENV_DIR = "JARVIS_BENCHMARK_DIR"
_ENV_TASKS = "JARVIS_BENCHMARK_TASKS"
_ENV_SINCE = "JARVIS_BENCHMARK_SINCE"
_ENV_TEST_TIMEOUT = "JARVIS_BENCHMARK_TEST_TIMEOUT_S"
_ENV_GEN_TIMEOUT = "JARVIS_BENCHMARK_GENERATION_TIMEOUT_S"
_ENV_ALPHA = "JARVIS_BENCHMARK_ALPHA"
_SRC_ROOT = "backend/"
_TEST_ROOT = "tests/"


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def benchmark_dir() -> Path:
    raw = os.environ.get(_ENV_DIR, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".jarvis" / "benchmark"


# ---------------------------------------------------------------------------
# Mining: which commits can be tasks
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    commit: str
    parent: str
    committed_at: str
    target: str
    tests: Tuple[str, ...]


_LOG_FORMAT = "@@%H|%P|%cI|%(trailers:key=Op-ID,valueonly,separator=;)"


def parse_log(text: str) -> List[Candidate]:
    """Candidates from ``git log --name-status --format=_LOG_FORMAT``. Pure.

    Kept: one parent (no merges), no Op-ID trailer (O+V never touched it),
    exactly one backend ``.py`` MODIFIED and no other backend file touched,
    and one or more test files under tests/ added or modified."""
    out: List[Candidate] = []
    cur: Optional[Dict[str, Any]] = None

    def close() -> None:
        if cur and not cur["opid"] and len(cur["parents"]) == 1 and len(cur["src"]) == 1 \
                and not cur["other_src"] and cur["tests"]:
            out.append(Candidate(commit=cur["sha"], parent=cur["parents"][0], committed_at=cur["at"],
                                 target=cur["src"][0], tests=tuple(sorted(cur["tests"]))))

    for line in text.splitlines():
        if line.startswith("@@"):
            close()
            sha, parents, at, opid = (line[2:].split("|") + ["", "", "", ""])[:4]
            cur = {"sha": sha, "parents": parents.split(), "at": at, "opid": opid.strip(),
                   "src": [], "other_src": False, "tests": []}
            continue
        if cur is None or not line.strip():
            continue
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        name = path.rsplit("/", 1)[-1]
        if path.startswith(_TEST_ROOT) and name.startswith("test_") and name.endswith(".py") \
                and status[:1] in ("A", "M"):
            cur["tests"].append(path)
        elif path.startswith(_SRC_ROOT) and path.endswith(".py"):
            if status == "M" and len(parts) == 2:
                cur["src"].append(path)
            else:
                cur["other_src"] = True
    close()
    return out


def selection_key(c: Candidate) -> str:
    """A deterministic, content-independent order: the commit hash itself.
    Spreads the sample across history and subsystems without anyone choosing
    which tasks the model is judged on."""
    return hashlib.sha256(c.commit.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Git snapshots
# ---------------------------------------------------------------------------

async def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec("git", "-C", str(repo), *args,
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}: {err.decode(errors='replace')[:300]}")
    return out.decode("utf-8", "replace")


@contextlib.asynccontextmanager
async def snapshot(repo: Path, rev: str):
    """A detached worktree of ``rev`` that is always removed again."""
    root = Path(tempfile.mkdtemp(prefix="heldout-", dir=str(benchmark_dir().parent)
                                 if benchmark_dir().parent.exists() else None))
    path = root / "tree"
    await _git(repo, "worktree", "add", "--detach", "--quiet", str(path), rev)
    try:
        yield path
    finally:
        with contextlib.suppress(Exception):
            await _git(repo, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(root, ignore_errors=True)
        with contextlib.suppress(Exception):
            await _git(repo, "worktree", "prune", check=False)


# ---------------------------------------------------------------------------
# Scoring: hidden tests, through O+V's own contained test runner
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TestScore:
    total: int
    failed: int
    timed_out: bool = False
    note: str = ""

    @property
    def share(self) -> float:
        """Share of hidden tests passing; 0 when none could run."""
        return 0.0 if self.total <= 0 else max(0.0, (self.total - self.failed) / self.total)


async def run_hidden_tests(tree: Path, tests: Dict[str, str]) -> TestScore:
    """Write the hidden tests into ``tree`` and run them with O+V's own test
    runner (the containment VALIDATE uses). A run that collected nothing --
    an import crash, a syntax error -- scores 0 with its note."""
    from backend.core.ouroboros.governance.test_runner import TestRunner
    paths = []
    for rel, content in tests.items():
        p = tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        paths.append(p)
    runner = TestRunner(repo_root=tree, timeout=_num(_ENV_TEST_TIMEOUT, 300.0))
    res = await runner.run(tuple(paths))
    note = "" if res.total else (res.stdout or "")[-300:]
    return TestScore(total=int(res.total), failed=int(res.failed), timed_out=bool(res.timed_out), note=note)


def gain(with_candidate: float, before: float) -> float:
    """How much of the gap between the unchanged code and full marks the
    change closed, clipped to [0, 1]. Pure."""
    room = 1.0 - before
    if room <= 0:
        return 0.0
    return min(1.0, max(0.0, (with_candidate - before) / room))


# ---------------------------------------------------------------------------
# Building the task set (once; frozen)
# ---------------------------------------------------------------------------

@dataclass
class Task:
    task_id: str
    commit: str
    parent: str
    committed_at: str
    target: str
    description: str
    prompt: str
    contract: str
    tests: Dict[str, str]
    before: float
    hidden_total: int

    def fingerprint_part(self) -> str:
        return hashlib.sha256(json.dumps(
            [self.task_id, self.prompt, self.contract, sorted(self.tests.items()), self.before],
            sort_keys=True).encode()).hexdigest()


def fingerprint(tasks: Sequence[Task]) -> str:
    """One hash over every task's prompt, contract, hidden tests and
    baseline: two runs are comparable only when it matches. Pure."""
    h = hashlib.sha256(f"schema={TASKSET_SCHEMA}".encode())
    for t in sorted(tasks, key=lambda t: t.task_id):
        h.update(t.fingerprint_part().encode())
    return h.hexdigest()


def _contract_for(target: str, tree: Path, served_model: str) -> bool:
    """force_full_content exactly as production would decide it for this
    target and the served model: its effective capability (policy + observed
    diff evidence), and creation-vs-edit."""
    from backend.core.ouroboros.governance import providers
    from backend.core.ouroboros.governance.brain_selector import BrainSelector
    from backend.core.ouroboros.governance.served_model_capability import FULL_ONLY
    verdict = BrainSelector().effective_schema_capability(declared=FULL_ONLY, served_model=served_model)
    return providers.resolve_force_full_content(schema_capability=verdict.capability,
                                                target_files=(target,), repo_root=tree)


def organism_environment() -> None:
    """Make this process decide as the organism decides. Every organism
    launcher hydrates the production envelope over ``.env`` -- that is where
    the diff schema is armed -- and a benchmark that rendered prompts without
    it would freeze whole-file contracts production no longer asks for.
    Operator-set values win, as in the launchers. Idempotent."""
    from backend.core.ouroboros.governance.init_guard import mark_hydrated
    from backend.core.ouroboros.governance.production_envelope import hydrate
    hydrate()
    mark_hydrated()            # as the launchers do once the envelope is applied


async def _render_prompt(tree: Path, target: str, description: str, force_full: bool) -> str:
    """The prompt O+V's full codegen builder writes for this change -- the
    single-shot form training rollouts and the cycle's verification use --
    through the same context-prune gate production applies before sending."""
    from backend.core.ouroboros.governance import providers
    from backend.core.ouroboros.governance.op_context import OperationContext
    ctx = OperationContext.create(target_files=(target,), description=description,
                                  op_id=f"heldout-{hashlib.sha1(description.encode()).hexdigest()[:12]}")
    prompt = providers._build_codegen_prompt(ctx, repo_root=tree, force_full_content=force_full)
    return await providers._context_prune_gate(prompt, "jprime")


async def build_taskset(repo: Path, *, n: Optional[int] = None, since: Optional[str] = None,
                        served_model: Optional[str] = None) -> Dict[str, Any]:
    """Mine, validate and freeze ``n`` decisive tasks. Writes
    ``taskset-<fingerprint12>.jsonl`` and returns its header."""
    organism_environment()
    n = int(n or _num(_ENV_TASKS, 30))
    since = since or os.environ.get(_ENV_SINCE, "") or "2025-08-01"
    served_model = served_model or os.environ.get("JARVIS_LOCAL_MODEL_NAME", "")
    log = await _git(repo, "log", "--no-merges", f"--since={since}", f"--format={_LOG_FORMAT}",
                     "--name-status", "HEAD")
    pool = sorted(parse_log(log), key=selection_key)
    tasks: List[Task] = []
    rejected: Dict[str, int] = {}
    for c in pool:
        if len(tasks) >= n:
            break
        try:
            task, why = await _validate(repo, c, served_model)
        except Exception as exc:  # noqa: BLE001 -- one bad candidate never ends the build
            task, why = None, f"error:{type(exc).__name__}"
            logger.info("[Heldout] %s rejected: %s", c.commit[:10], exc)
        if task is None:
            rejected[why] = rejected.get(why, 0) + 1
            continue
        tasks.append(task)
        logger.info("[Heldout] task %d/%d: %s %s (before %.2f of %d hidden)", len(tasks), n,
                    c.commit[:10], c.target, task.before, task.hidden_total)
    fp = fingerprint(tasks)
    header = {"schema": TASKSET_SCHEMA, "fingerprint": fp, "built_at": time.time(),
              "repo_head": (await _git(repo, "rev-parse", "HEAD")).strip(), "served_model": served_model,
              "since": since, "tasks": len(tasks), "candidates": len(pool), "rejected": rejected}
    out = benchmark_dir() / f"taskset-{fp[:12]}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"header": header}) + "\n")
        for t in tasks:
            fh.write(json.dumps(asdict(t)) + "\n")
    os.replace(tmp, out)
    header["path"] = str(out)
    return header


async def _validate(repo: Path, c: Candidate, served_model: str) -> Tuple[Optional[Task], str]:
    tests = {t: await _git(repo, "show", f"{c.commit}:{t}") for t in c.tests}
    message = (await _git(repo, "log", "-1", "--format=%B", c.commit)).strip()
    fixed = await _git(repo, "show", f"{c.commit}:{c.target}")
    async with snapshot(repo, c.parent) as tree:
        if not (tree / c.target).is_file():
            return None, "target_absent_at_parent"
        # The prompt first, from the untouched parent tree: the hidden tests
        # are written only afterwards, for scoring, and never seen by it.
        force_full = _contract_for(c.target, tree, served_model)
        prompt = await _render_prompt(tree, c.target, message, force_full)
        before = await run_hidden_tests(tree, tests)
        if before.total <= 0:
            return None, "hidden_tests_do_not_run"
        if before.share >= 1.0:
            return None, "not_decisive_passes_before"
        (tree / c.target).write_text(fixed, encoding="utf-8")
        after = await run_hidden_tests(tree, tests)
        if after.total <= 0 or after.failed:
            return None, "not_decisive_fails_after"
    return Task(task_id=c.commit[:12], commit=c.commit, parent=c.parent, committed_at=c.committed_at,
                target=c.target, description=message, prompt=prompt,
                contract="full_content" if force_full else "diff_capable",
                tests=tests, before=round(before.share, 6), hidden_total=after.total), "kept"


def load_taskset(path: Path) -> Tuple[Dict[str, Any], List[Task]]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])["header"]
    tasks = [Task(**json.loads(line)) for line in lines[1:] if line.strip()]
    if fingerprint(tasks) != header["fingerprint"]:
        raise ValueError(f"{path}: task set altered since it was built (fingerprint mismatch)")
    return header, tasks


# ---------------------------------------------------------------------------
# A run: the served model on every task
# ---------------------------------------------------------------------------

@dataclass
class TaskResult:
    task_id: str
    gain: float
    share: float
    accepted: bool
    note: str = ""
    completion_chars: int = 0
    reward: Optional[float] = None


async def run(taskset: Path, *, model: Optional[str] = None, repo: Optional[Path] = None) -> Dict[str, Any]:
    """Every task of the set through the served model, greedy, under J-Prime's
    reproducible-evaluation scope; each answer parsed by O+V's own parser,
    applied to the parent snapshot, scored by the hidden tests. Writes and
    returns the run record."""
    from backend.core.ouroboros.cli.thin_client import repo_root
    from backend.core.ouroboros.governance.observability import training_handoff as th
    from backend.core.ouroboros.governance.served_identity import resolve as served_identity
    organism_environment()
    header, tasks = load_taskset(taskset)
    repo = repo or Path(repo_root())
    model = model or th._model()
    ident = await asyncio.to_thread(served_identity, base=th._jprime(), model=model)
    adapter = ((ident or {}).get("adapter") or {}).get("version")
    gen_timeout = _num(_ENV_GEN_TIMEOUT, 600.0)
    results: List[TaskResult] = []
    completions: List[str] = []
    async with th.evaluation_scope(model, ttl_s=gen_timeout * max(1, len(tasks)) + 60) as client:
        for t in tasks:
            text = ""
            try:
                res = await asyncio.wait_for(client.complete(system="", user=t.prompt,
                                                             prompt_tokens=max(1, len(t.prompt) // 4),
                                                             temperature=0.0), timeout=gen_timeout)
                text = getattr(res, "text", "") or getattr(res, "content", "")
                results.append(await _score(repo, t, text))
            except Exception as exc:  # noqa: BLE001
                results.append(TaskResult(task_id=t.task_id, gain=0.0, share=0.0, accepted=False,
                                          note=f"{type(exc).__name__}: {str(exc)[:160]}"))
            completions.append(text)
            results[-1].completion_chars = len(text)
    rewards = await _reward_scores(completions, [t.prompt for t in tasks])
    for r, s in zip(results, rewards):
        r.reward = s
    record = {"fingerprint": header["fingerprint"], "taskset": str(taskset), "model": model,
              "adapter": adapter, "identity": ident,
              "ran_at": time.time(), "results": [asdict(r) for r in results],
              # What the model wrote, per task: a failure class is read from
              # these, not inferred from a score.
              "completions": {t.task_id: c for t, c in zip(tasks, completions)},
              "summary": summarize(results)}
    out = benchmark_dir() / "runs" / f"run-{time.strftime('%Y%m%d-%H%M%S')}-{header['fingerprint'][:12]}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")
    record["path"] = str(out)
    return record


async def _score(repo: Path, t: Task, text: str) -> TaskResult:
    """O+V's parser decides whether the answer is a change O+V would apply;
    the hidden tests decide how good it is."""
    from backend.core.ouroboros.governance import providers
    from backend.core.ouroboros.governance.op_context import OperationContext
    async with snapshot(repo, t.parent) as tree:
        src = tree / t.target
        ctx = OperationContext.create(target_files=(t.target,), description=t.description,
                                      op_id=f"heldout-{t.task_id}")
        src_hash = hashlib.sha256(src.read_bytes()).hexdigest()
        try:
            res = providers._parse_generation_response(text, "heldout", 0.0, ctx, src_hash, str(src),
                                                       repo_root=tree)
        except Exception as exc:  # noqa: BLE001 -- an answer O+V would not act on scores 0
            return TaskResult(task_id=t.task_id, gain=0.0, share=0.0, accepted=False,
                              note=f"rejected: {str(exc)[:160]}")
        cands = [c for c in (getattr(res, "candidates", ()) or ()) if isinstance(c, dict)]
        if getattr(res, "is_noop", False) or not cands:
            return TaskResult(task_id=t.task_id, gain=0.0, share=t.before, accepted=True, note="noop")
        best: Optional[TaskResult] = None
        for cand in cands:                      # the best candidate O+V would have chosen among
            body = str(cand.get("full_content") or "")
            target = str(cand.get("file_path") or t.target)
            if target != t.target or not body:
                continue
            src.write_text(body, encoding="utf-8")
            score = await run_hidden_tests(tree, t.tests)
            g = gain(score.share, t.before)
            if best is None or g > best.gain:
                best = TaskResult(task_id=t.task_id, gain=g, share=score.share, accepted=True,
                                  note="timed_out" if score.timed_out else score.note[:160])
        return best or TaskResult(task_id=t.task_id, gain=0.0, share=0.0, accepted=False,
                                  note="no candidate for the target file")


async def _reward_scores(completions: Sequence[str],
                         prompts: Sequence[str] = ()) -> List[Optional[float]]:
    """The training reward's own score for each completion (Reactor's
    ``grpo_verifier.verify_any``, in Reactor's interpreter -- the two repos
    share no venv), recorded BESIDE the gain: never the benchmark's score.
    Graded against the task's prompt, with the applier the trainer is given,
    so a diff is scored by where it lands. None when Reactor cannot be
    reached."""
    from backend.core.ouroboros.governance.observability import training_handoff as th
    from backend.core.ouroboros.governance.observability import training_trigger as tt
    py, root = tt._reactor_python(), tt._reactor_root()
    if not py or not root or not completions:
        return [None] * len(completions)
    script = ("import json,sys\n"
              "from reactor_core.training.grpo_verifier import verify_any\n"
              "for line in open(sys.argv[1], encoding='utf-8'):\n"
              "    row = json.loads(line)\n"
              "    print(json.dumps(verify_any(row['c'], prompt=row['p']).score), flush=True)\n")
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
        for i, c in enumerate(completions):
            fh.write(json.dumps({"c": c, "p": prompts[i] if i < len(prompts) else ""}) + "\n")
        feed = fh.name
    try:
        rc, out = await tt._run([py, "-c", script, feed], timeout_s=_num(_ENV_TEST_TIMEOUT, 300.0),
                                cwd=root, env={"PYTHONPATH": str(root), **th._diff_applier_contract()})
        vals = [float(x) for x in out.split() if x.strip()]
        return vals if rc == 0 and len(vals) == len(completions) else [None] * len(completions)
    except Exception:  # noqa: BLE001
        return [None] * len(completions)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(feed)


# ---------------------------------------------------------------------------
# Summaries and paired comparison
# ---------------------------------------------------------------------------

def summarize(results: Sequence[TaskResult]) -> Dict[str, Any]:
    g = [r.gain for r in results]
    rw = [r.reward for r in results if r.reward is not None]
    return {"tasks": len(g), "mean_gain": round(sum(g) / len(g), 4) if g else None,
            "solved": sum(1 for x in g if x >= 1.0), "accepted": sum(1 for r in results if r.accepted),
            "mean_reward": round(sum(rw) / len(rw), 4) if rw else None}


def _sign_test_p(wins: int, losses: int) -> Optional[float]:
    """One-sided exact sign test: P(X >= wins), X ~ Binomial(wins+losses, 1/2)."""
    n = wins + losses
    if n == 0:
        return None
    return min(1.0, sum(math.comb(n, k) for k in range(wins, n + 1)) / 2 ** n)


def compare(new: Dict[str, Any], old: Dict[str, Any], *, alpha: Optional[float] = None) -> Dict[str, Any]:
    """The new run against the old, task by task, on the SAME task set.
    Paired: each task is its own control, so task difficulty cancels. Exact
    sign test on the per-task gain differences (ties carry no evidence).
    Also reports the reward's movement beside the gain's, and flags reward
    rising without capability. Refuses runs of different task sets."""
    alpha = float(alpha if alpha is not None else _num(_ENV_ALPHA, 0.05))
    if new.get("fingerprint") != old.get("fingerprint"):
        return {"verdict": "incomparable", "detail": "different task sets (fingerprints differ)"}
    a = {r["task_id"]: r for r in old["results"]}
    pairs = [(r, a[r["task_id"]]) for r in new["results"] if r["task_id"] in a]
    diffs = [n["gain"] - o["gain"] for n, o in pairs]
    wins, losses = sum(1 for d in diffs if d > 0), sum(1 for d in diffs if d < 0)
    p_better, p_worse = _sign_test_p(wins, losses), _sign_test_p(losses, wins)
    rew = [(n.get("reward"), o.get("reward")) for n, o in pairs]
    rew = [(x, y) for x, y in rew if x is not None and y is not None]
    reward_delta = round(sum(x - y for x, y in rew) / len(rew), 4) if rew else None
    gain_delta = round(sum(diffs) / len(diffs), 4) if diffs else None
    if p_better is not None and p_better < alpha:
        verdict = "better"
    elif p_worse is not None and p_worse < alpha:
        verdict = "worse"
    else:
        verdict = "no_detectable_difference"
    return {"verdict": verdict, "tasks": len(pairs), "wins": wins, "losses": losses,
            "ties": len(pairs) - wins - losses, "mean_gain_delta": gain_delta,
            "p_better": p_better, "p_worse": p_worse, "alpha": alpha,
            "mean_reward_delta": reward_delta,
            "reward_without_capability": bool(reward_delta is not None and reward_delta > 0
                                              and verdict != "better")}


def latest_runs(fingerprint_: str, adapter: Optional[str] = None) -> List[Dict[str, Any]]:
    """Recorded runs of one task set, newest first (optionally one adapter's)."""
    out = []
    for f in sorted((benchmark_dir() / "runs").glob(f"run-*-{fingerprint_[:12]}.json"), reverse=True):
        with contextlib.suppress(OSError, ValueError):
            rec = json.loads(f.read_text(encoding="utf-8"))
            if adapter is None or rec.get("adapter") == adapter:
                out.append(rec)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="heldout_benchmark")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="mine, validate and freeze a task set")
    b.add_argument("--tasks", type=int, default=0)
    b.add_argument("--since", default="")
    r = sub.add_parser("run", help="score the served model on a task set")
    r.add_argument("taskset")
    c = sub.add_parser("compare", help="paired comparison of two run records")
    c.add_argument("new")
    c.add_argument("old")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from backend.core.env_bootstrap import load_env_once
    load_env_once()
    from backend.core.ouroboros.cli.thin_client import repo_root
    if args.cmd == "build":
        out: Any = asyncio.run(build_taskset(Path(repo_root()), n=args.tasks or None, since=args.since or None))
    elif args.cmd == "run":
        out = asyncio.run(run(Path(args.taskset)))
        out = {k: out[k] for k in ("path", "adapter", "summary")}
    else:
        out = compare(json.loads(Path(args.new).read_text()), json.loads(Path(args.old).read_text()))
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""A1 Live-Fire Chaos Harness orchestrator.

================================================================================
The *glue* that sequences the three halves of the A1 live-fire proof into one
runnable, cost-bounded, fail-safe run:

    inject (real bug) -> O+V soak (production) -> live SSE audit -> A1 verdict

It does NOT reimplement any of them. It ORCHESTRATES the existing scripts:

  * ``scripts/chaos_injector_ast.py``    -- inject / revert a real, test-detectable bug.
  * ``scripts/ouroboros_battle_test.py`` -- the production O+V soak (writes a session
                                            ``debug.log`` where ``[A1Trace]`` lands).
  * ``scripts/a1_graduation_auditor.py`` -- live SSE + log audit -> ``a1_verdict.json``.
  * ``scripts/sovereign_iac_hypervisor.py`` -- provisions the cost-bounded GCP Linux
                                            node (dead-man self-delete), runs a remote
                                            command, streams stdout. (``--remote`` mode.)
  * ``scripts/sovereign_sentinel.py``    -- the autonomous autopsy black-box (failure path).

THE LOAD-BEARING INVARIANT: chaos-revert-always. The injected bug is restored in
a ``finally`` block AND on SIGINT/SIGTERM (signal handlers) -- the repo must NEVER
be left broken, on ANY exit path (success, soak crash, GraduationFailedException,
timeout, KeyboardInterrupt, kill).

Two modes
---------
* ``--dry-run-local``  -- prove the WIRING locally with NO spend + NO real
  convergence expectation. With ``--stub-soak`` it emits a deterministic synthetic
  ``debug.log`` (5 A1Trace hops + flag signals + FSM phases + PR signal) so the
  REAL auditor parses a full PROVEN timeline end-to-end. Always reverts.
* ``--remote``         -- drive the IaC hypervisor to provision the Linux node,
  sync the repo, and run ``--execute-on-node`` remotely, streaming stdout back.
  Cost-bounded + node dead-man + teardown-always. REQUIRES the triple money-gate
  (``--i-understand-this-spends-money``); without it the harness REFUSES + prints
  a cost estimate.

Design: ``from __future__ import annotations``, Python 3.9+, ASCII-only,
env-knob driven, no org mutation outside the (reverted) chaos bug.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence


# ===========================================================================
# Paths + lazy import of the auditor (for flag derivation + verdict types).
# ===========================================================================

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)

# A1 launch manifest: deterministic, schema-validated, fail-closed config artifact.
# Provides the single authoritative apply point for model/forcing/epistemic flags.
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from a1_launch_manifest import (  # noqa: E402
    A1ManifestError,
    apply_manifest,
    build_manifest,
    load_and_validate,
    write_manifest,
)
_CHAOS_SCRIPT = os.path.join(_SCRIPTS_DIR, "chaos_injector_ast.py")
_SOAK_SCRIPT = os.path.join(_SCRIPTS_DIR, "ouroboros_battle_test.py")
_AUDITOR_SCRIPT = os.path.join(_SCRIPTS_DIR, "a1_graduation_auditor.py")
_HYPERVISOR_SCRIPT = os.path.join(_SCRIPTS_DIR, "sovereign_iac_hypervisor.py")
_SENTINEL_SCRIPT = os.path.join(_SCRIPTS_DIR, "sovereign_sentinel.py")
_LINUX_ENV_OVERLAY = os.path.join(_REPO_ROOT, "deploy", "ouroboros_linux_prod.env")
# The Omni-Soak overlay: sources the linux base (superset) + flips the full
# MAS / fan-out stack (WAVE3 parallel dispatch + DAG_COMPOSE + SWARM + the
# REVIEW/PLAN enforce promotions + safe flags). Armed only under JARVIS_A1_OMNI_SOAK.
_OMNI_ENV_OVERLAY = os.path.join(_REPO_ROOT, "deploy", "ouroboros_omni_prod.env")

# Cost model for the remote node (e2-standard-8 Spot ~ $0.08/hr; see hypervisor).
_NODE_COST_PER_HOUR = float(os.environ.get("JARVIS_A1_NODE_COST_PER_HOUR", "0.08"))


def _load_auditor_module():
    """Import the auditor as a module (reuse its CADENCE_POLICY flag loader,
    GraduationFailedException, and the pure A1GraduationAuditor core)."""
    spec = importlib.util.spec_from_file_location(
        "a1_graduation_auditor", _AUDITOR_SCRIPT
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("a1_graduation_auditor", mod)
    spec.loader.exec_module(mod)
    return mod


_AUD = _load_auditor_module()
GraduationFailedException = _AUD.GraduationFailedException


def _log(msg: str) -> None:
    print("[A1Harness] %s" % (msg,), flush=True)


def _truthy(val: Optional[str]) -> bool:
    """Standard env truthiness: 1/true/yes/on (case-insensitive)."""
    return str(val or "").strip().lower() in ("1", "true", "yes", "on")


def omni_soak_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """Whether the Omni-Soak is armed via ``JARVIS_A1_OMNI_SOAK`` (default off).
    When OFF the normal A1 soak is byte-identical (linux overlay + single inject)."""
    src = env if env is not None else os.environ
    return _truthy(src.get("JARVIS_A1_OMNI_SOAK"))


def _selected_overlay_path(env: Optional[Dict[str, str]] = None) -> str:
    """The env overlay to compose: the omni overlay when armed, else linux prod.
    The omni env ``source``s the linux base, so it is a strict superset."""
    return _OMNI_ENV_OVERLAY if omni_soak_enabled(env) else _LINUX_ENV_OVERLAY


# ===========================================================================
# Env composition -- the cognitive flags ON, DERIVED (no hardcoded list).
# ===========================================================================


_GRADUATION_LEDGER_SRC = os.path.join(
    _REPO_ROOT, "backend", "core", "ouroboros", "governance",
    "adaptation", "graduation_ledger.py",
)


def _derive_flags_from_cadence_source() -> List[str]:
    """Fallback derivation that reads the CADENCE_POLICY flag names straight from
    the ``graduation_ledger.py`` SOURCE via AST -- no heavy import (so it works
    even when the soak's runtime deps like aiohttp are absent on a dev box). This
    is STILL derived from the authoritative table, NOT a hardcoded list: it parses
    every ``flag_name=`` kwarg inside the ``CADENCE_POLICY`` assignment."""
    import ast

    with open(_GRADUATION_LEDGER_SRC, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=_GRADUATION_LEDGER_SRC)
    flags: List[str] = []
    for node in ast.walk(tree):
        # Find the `CADENCE_POLICY: ... = ( ... )` assignment, then collect every
        # `flag_name="..."` keyword in the contained CadencePolicyEntry(...) calls.
        is_cadence = False
        targets = []
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        else:
            continue
        if "CADENCE_POLICY" not in targets or value is None:
            continue
        is_cadence = True
        for sub in ast.walk(value):
            if isinstance(sub, ast.keyword) and sub.arg == "flag_name":
                if isinstance(sub.value, ast.Constant) and isinstance(sub.value.value, str):
                    flags.append(sub.value.value)
        if is_cadence:
            break
    return flags


def derive_cognitive_flags() -> List[str]:
    """The cognitive-flag set to turn ON for the soak. DERIVED from the auditor's
    CADENCE_POLICY-backed ``load_audit_flags`` (env-overridable via
    ``JARVIS_A1_AUDIT_FLAGS``) -- NEVER a hardcoded list in this module. If the
    heavy import path fails (e.g. aiohttp absent on a dev box), fall back to an
    AST parse of the CADENCE_POLICY SOURCE -- still derived, never hardcoded."""
    try:
        return list(_AUD.load_audit_flags())
    except Exception as exc:  # noqa: BLE001 -- fall back to source-AST derivation
        _log("CADENCE_POLICY import failed (%s); deriving flags from source AST"
             % (type(exc).__name__,))
        flags = _derive_flags_from_cadence_source()
        if not flags:
            raise
        return flags


def _parse_env_overlay(path: str) -> Dict[str, str]:
    """Parse a shell ``export KEY=VALUE`` overlay file into a flat dict. Pure
    stdlib, comment + quote tolerant. Never raises (missing file -> {})."""
    out: Dict[str, str] = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                # Strip an inline comment + surrounding quotes.
                val = val.split("#", 1)[0].strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key:
                    out[key] = val
    except Exception as exc:  # noqa: BLE001 -- overlay parse must never abort
        _log("overlay parse warning (%s): %s" % (path, exc))
    return out


def compose_env(*, base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Compose the soak env: the derived cognitive flags ON + the orchestration
    flags + the Linux production overlay. The result is printed for audit by the
    caller. Precedence (lowest->highest): process env -> linux overlay -> derived
    cognitive flags -> orchestration-required flags."""
    env: Dict[str, str] = dict(base_env if base_env is not None else os.environ)
    # 1. Production overlay. Always layer the linux base FIRST (pytest caps, AST
    #    pool, Claude-disabled autarky) -- the omni env `source`s it in bash, which
    #    the line-based parser cannot follow, so we compose the inheritance here.
    #    Under JARVIS_A1_OMNI_SOAK we then layer the omni overlay ON TOP (the full
    #    MAS / fan-out stack), making omni a strict superset. OFF -> linux only,
    #    byte-identical to the normal A1 soak.
    overlay_path = _selected_overlay_path(env)
    env.update(_parse_env_overlay(_LINUX_ENV_OVERLAY))
    if overlay_path != _LINUX_ENV_OVERLAY:
        env.update(_parse_env_overlay(overlay_path))
    _log("overlay=%s" % ("omni" if overlay_path == _OMNI_ENV_OVERLAY else "linux_prod",))
    # 2. Every derived cognitive flag ON.
    flags = derive_cognitive_flags()
    for flag in flags:
        env[flag] = "true"
    # 2b. Hand the SAME derived flag set to the auditor via its override env var.
    # The auditor's load_audit_flags() lacks the harness's AST-source fallback, so
    # on a node where the heavy CADENCE_POLICY import fails (aiohttp path) it dies
    # at flag_set_load (the run #7 wall) -- give it the already-derived list here.
    env["JARVIS_A1_AUDIT_FLAGS"] = ",".join(flags)
    # 3. Orchestration-required flags (a strategic GOAL source + SSE + A1Trace).
    env["JARVIS_ROADMAP_ORCHESTRATOR_ENABLED"] = "1"
    env["JARVIS_IDE_STREAM_ENABLED"] = "1"
    env["JARVIS_A1_TRACE_ENABLED"] = "1"
    env["JARVIS_IDE_OBSERVABILITY_ENABLED"] = "1"
    # 4. A1-harness explicit opt-ins (not in the linux_prod overlay; default-OFF in
    #    production to avoid cost surprises). Applied via the A1 launch manifest so
    #    the values have a single authoritative apply point (apply_manifest()).
    #    * NATIVE_TOOL_FORCING: tells DW to use native tool-call format instead of
    #      text-mode CoT -- required for the Iron Gate exploration-first check to
    #      see proper tool calls in the Venom loop, not raw JSON snippets.
    #    * EPISTEMIC_FEEDBACK: enables the gradient-deduced DW global-outage
    #      escalation path (Provider Quarantine / Cryo-DLQ) so provider health
    #      signals flow through the full feedback lane during A1 soaks.
    # 5. Topology-native Qwen3.5-397B-A17B-FP8 as the DW primary pin.
    #    The linux_prod overlay may pin a different model. For A1 soaks we want the
    #    Qwen default so repair/background routes resolve to Qwen (the model whose
    #    reasoning keepalives prevent SSE stream stalls on long generations).
    #    The overlay's value is deliberately overridden here at the harness layer;
    #    assert_dw_primary() still passes (non-empty value).
    #    apply_manifest() is the SINGLE authoritative apply point for these three
    #    flags -- do NOT set them individually outside this call.
    _a1_manifest = build_manifest(
        model="Qwen/Qwen3.5-397B-A17B-FP8",
        native_tool_forcing=True,
        epistemic_feedback=True,
    )
    apply_manifest(_a1_manifest, env)
    return env


# ===========================================================================
# Stub soak fixture -- a deterministic synthetic debug.log for the wiring proof.
# ===========================================================================


def write_stub_soak_log(path: str, *, goal_id: str = "GOAL-A1-STUB") -> None:
    """Write a synthetic soak ``debug.log`` that the REAL auditor will parse into
    an A1_DISPATCH_PROVEN verdict: the 5 A1Trace hops in order for one goal, the
    FSM CLASSIFY..APPLY phases (emitted via the auditor's SSE path in dry-run --
    here we additionally embed a structured operation_terminal + PR marker), and
    a gate-telemetry + autonomous-PR line. NO real loop, NO spend."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    lines = [
        "[A1Harness][stub] synthetic soak debug.log (dry-run wiring proof)",
        "[A1Trace] emit goal=%s source=roadmap" % goal_id,
        "[A1Trace] ingest goal=%s router=unified" % goal_id,
        "[A1Trace] dequeue goal=%s worker=bg-0" % goal_id,
        "[A1Trace] submit goal=%s orchestrator=fsm" % goal_id,
        "[A1Trace] accept goal=%s phase=GENERATE" % goal_id,
        # Gate telemetry the auditor correlates to cognitive-flag families.
        "[SemanticGuard] op=%s findings=0 ok" % goal_id,
        "[IronGate] tool_exploration_start op=%s reads=2" % goal_id,
        # Autonomous PR / commit signal.
        "AutoCommitter: commit created with O+V signature for %s" % goal_id,
        "[A1Harness][stub] PR opened: gh pr create -> ouroboros/review/%s" % goal_id,
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _stub_sse_events(goal_id: str = "GOAL-A1-STUB") -> List[Dict[str, Any]]:
    """The SSE event stream the auditor needs (FSM phases + terminal applied +
    PR signal) -- fed directly to the pure auditor core in the dry-run path."""
    return [
        {"event_type": "fsm_phase_changed", "payload": {"phase": "CLASSIFY"}},
        {"event_type": "fsm_phase_changed", "payload": {"phase": "ROUTE"}},
        {"event_type": "fsm_phase_changed", "payload": {"phase": "GENERATE"}},
        {"event_type": "fsm_phase_changed", "payload": {"phase": "APPLY"}},
        {"event_type": "operation_terminal", "payload": {"state": "applied", "op_id": goal_id}},
        {"event_type": "review_branch_created", "payload": {"op_id": goal_id}},
    ]


# ===========================================================================
# Chaos controller -- drives chaos_injector_ast.py via subprocess (real bug).
# ===========================================================================


class ChaosController:
    """Real chaos lifecycle: subprocesses ``chaos_injector_ast.py``. Each method
    returns a structured result; never raises on a non-zero injector rc (the
    harness decides). Revert is idempotent + best-effort (safe to call even if
    nothing was injected)."""

    def __init__(self, *, repo_root: str = _REPO_ROOT, test_timeout_s: float = 60.0,
                 runner: Optional[Callable[..., subprocess.CompletedProcess]] = None) -> None:
        self.repo_root = repo_root
        self.test_timeout_s = test_timeout_s
        self._run = runner or self._default_run

    @staticmethod
    def _default_run(argv: Sequence[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, _CHAOS_SCRIPT, *argv],
            capture_output=True, text=True, check=False,
        )

    def _json_out(self, cp: subprocess.CompletedProcess) -> Dict[str, Any]:
        try:
            return json.loads(cp.stdout or "{}")
        except Exception:  # noqa: BLE001
            return {}

    def status(self) -> Dict[str, Any]:
        cp = self._run(["--status", "--repo-root", self.repo_root])
        return self._json_out(cp)

    def list_candidates(self) -> int:
        cp = self._run(["--list-candidates", "--repo-root", self.repo_root])
        return int(self._json_out(cp).get("count", 0))

    def inject(self, seed: int) -> bool:
        """Inject a real bug. Returns True iff the manifest confirms the test went
        RED (the live-fire signal the soak must detect)."""
        cp = self._run([
            "--inject", "--seed", str(seed), "--force",
            "--repo-root", self.repo_root,
            "--test-timeout", str(self.test_timeout_s),
        ])
        if cp.returncode != 0:
            _log("chaos inject rc=%d stderr=%s" % (cp.returncode, (cp.stderr or "")[:200]))
        st = self.status()
        return bool(st.get("active")) and bool(st.get("test_red_post"))

    def inject_decomposable(self, n: int = 3) -> "tuple":
        """Inject N MUTUALLY-ISOLATED pure-leaf bugs (the DecomposableChaosInjector)
        so the swarm fans out N-way into parallel L3 subagents. Subprocesses the
        existing ``--inject-decomposable -n N`` CLI and parses its JSON status.

        Returns ``(ok, count)`` where ``ok`` is True iff exactly N targets were
        injected AND every one went RED (``test_red_post``). Reuses the same
        ``_run`` / JSON plumbing as ``inject``; revert (the N-entry manifest) is
        handled byte-identically by the existing ``--revert`` path."""
        cp = self._run([
            "--inject-decomposable", "-n", str(n), "--force",
            "--repo-root", self.repo_root,
            "--test-timeout", str(self.test_timeout_s),
        ])
        if cp.returncode != 0:
            _log("chaos inject-decomposable rc=%d stderr=%s"
                 % (cp.returncode, (cp.stderr or "")[:200]))
        out = self._json_out(cp)
        targets = out.get("targets") or []
        count = int(out.get("count", len(targets)))
        all_red = bool(targets) and all(
            bool(t.get("test_red_post")) for t in targets
        )
        ok = (count == int(n)) and all_red
        return (ok, count)

    def revert(self) -> bool:
        cp = self._run(["--revert", "--repo-root", self.repo_root])
        return cp.returncode == 0


# ===========================================================================
# Soak runner -- launches ouroboros_battle_test.py, finds its session debug.log.
# ===========================================================================


@dataclass
class SoakHandle:
    debug_log: str
    session_dir: str
    proc: Optional[subprocess.Popen]


class SoakRunner:
    """Launches the production O+V soak as a child process with the composed env,
    and discovers the session ``debug.log`` it writes under
    ``.ouroboros/sessions/bt-*``."""

    def __init__(self, *, repo_root: str = _REPO_ROOT, cost_cap: float = 0.0,
                 wall_seconds: int = 120) -> None:
        self.repo_root = repo_root
        self.cost_cap = cost_cap
        self.wall_seconds = wall_seconds
        self._proc: Optional[subprocess.Popen] = None

    def _sessions_root(self) -> str:
        return os.path.join(self.repo_root, ".ouroboros", "sessions")

    def _snapshot_sessions(self) -> set:
        root = self._sessions_root()
        try:
            return set(os.listdir(root))
        except OSError:
            return set()

    def launch(self, env: Dict[str, str], run_dir: str) -> SoakHandle:
        before = self._snapshot_sessions()
        argv = [
            sys.executable, _SOAK_SCRIPT,
            "--production-soak", "--headless",
            "--cost-cap", str(self.cost_cap),
            "--max-wall-seconds", str(self.wall_seconds),
        ]
        stdout_path = os.path.join(run_dir, "soak_stdout.log")
        os.makedirs(run_dir, exist_ok=True)
        fh = open(stdout_path, "w", encoding="utf-8")
        self._proc = subprocess.Popen(
            argv, cwd=self.repo_root, env=env, stdout=fh, stderr=subprocess.STDOUT,
        )
        debug_log = self._await_session_debug_log(before, deadline_s=60.0)
        session_dir = os.path.dirname(debug_log) if debug_log else self._sessions_root()
        return SoakHandle(debug_log=debug_log, session_dir=session_dir, proc=self._proc)

    def _await_session_debug_log(self, before: set, deadline_s: float) -> str:
        """Poll the sessions root for a NEW bt-* dir that the soak created, then
        return its debug.log path (created when the first line lands)."""
        root = self._sessions_root()
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            try:
                now = set(os.listdir(root))
            except OSError:
                now = set()
            new = sorted(now - before, reverse=True)
            for name in new:
                cand = os.path.join(root, name, "debug.log")
                # Return as soon as the new session dir exists (the auditor's
                # tail handles the file-not-yet-existing case).
                return cand
            if self._proc is not None and self._proc.poll() is not None:
                break
            time.sleep(0.5)
        # Fall back to a placeholder path under the sessions root.
        return os.path.join(root, "pending", "debug.log")

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.send_signal(signal.SIGTERM)
                try:
                    self._proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# Auditor runner -- subprocesses the live auditor (real), or runs core in-proc.
# ===========================================================================


class AuditorRunner:
    """Runs the live A1 GraduationAuditor against the SSE base + the soak
    debug.log, writing the verdict JSON. Subprocesses the real script so the
    auditor's reconnect/backoff/intervention-lock run unmodified."""

    def __init__(self, *, strict: bool = True) -> None:
        self.strict = strict

    def watch(self, *, base: str, log_file: str, timeout_s: float,
              verdict_out: str) -> Dict[str, Any]:
        argv = [
            sys.executable, _AUDITOR_SCRIPT, "--watch",
            "--base", base, "--log-file", log_file,
            "--timeout", str(timeout_s), "--verdict-out", verdict_out,
            "--strict" if self.strict else "--lenient",
        ]
        # The auditor's load_audit_flags() lacks the harness's AST-source fallback,
        # so on a node where the heavy CADENCE_POLICY import fails it dies at
        # flag_set_load (runs #7/#8). subprocess.run() inherits os.environ, which does
        # NOT carry the soak's composed JARVIS_A1_AUDIT_FLAGS -- so set it explicitly
        # on the auditor's env from the already-derived flag set (no CADENCE import).
        env = dict(os.environ)
        env.setdefault("JARVIS_A1_AUDIT_FLAGS", ",".join(derive_cognitive_flags()))
        cp = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
        if os.path.exists(verdict_out):
            try:
                return json.loads(open(verdict_out, encoding="utf-8").read())
            except Exception:  # noqa: BLE001
                pass
        return {"verdict": "failed", "proven": False,
                "failure_locus": "auditor_no_verdict_file",
                "stdout_tail": (cp.stdout or "")[-400:]}


class StubAuditorRunner:
    """Dry-run auditor: drives the REAL pure A1GraduationAuditor core directly
    with the synthetic SSE events + the stub log lines (no network, no soak).
    This is what proves the wiring end-to-end -> A1_DISPATCH_PROVEN."""

    def __init__(self, *, strict: bool = True, goal_id: str = "GOAL-A1-STUB") -> None:
        self.strict = strict
        self.goal_id = goal_id

    def watch(self, *, base: str, log_file: str, timeout_s: float,
              verdict_out: str) -> Dict[str, Any]:
        auditor = _AUD.A1GraduationAuditor(strict=self.strict)
        # Feed the synthetic SSE events (FSM phases + applied + PR signal).
        for ev in _stub_sse_events(self.goal_id):
            auditor.ingest_event(ev["event_type"], ev["payload"])
        # Feed the stub debug.log lines (A1Trace hops + gate telemetry + PR).
        try:
            with open(log_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    auditor.ingest_log_line(line.rstrip("\n"))
        except OSError:
            pass
        verdict = auditor.verdict()
        with open(verdict_out, "w", encoding="utf-8") as fh:
            fh.write(verdict.to_json())
        return verdict.to_dict()


# ===========================================================================
# Local autopsy black-box (failure path) -- reuse the sentinel's philosophy.
# ===========================================================================


def local_autopsy(*, run_id: str, autopsy_root: str, debug_log: str,
                  verdict: Dict[str, Any], chaos_manifest: str) -> str:
    """Capture a bounded, fail-soft black-box of the failed run: the soak
    debug.log tail, the verdict, and the chaos manifest. Mirrors the sentinel's
    AUTOPSY PROTOCOL (capture-before-teardown, never blocks teardown). Returns
    the autopsy dir path."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(autopsy_root, "%s_%s" % (run_id, stamp))
    try:
        os.makedirs(outdir, exist_ok=True)
        # Soak debug.log tail.
        try:
            with open(debug_log, "r", encoding="utf-8", errors="ignore") as fh:
                tail = fh.readlines()[-2000:]
            with open(os.path.join(outdir, "debug.log.tail"), "w", encoding="utf-8") as out:
                out.writelines(tail)
        except OSError:
            pass
        # Verdict + manifest snapshots.
        with open(os.path.join(outdir, "a1_verdict.json"), "w", encoding="utf-8") as fh:
            json.dump(verdict, fh, indent=2)
        try:
            if os.path.exists(chaos_manifest):
                with open(chaos_manifest, "r", encoding="utf-8") as src:
                    data = src.read()
                with open(os.path.join(outdir, "chaos_manifest.json"), "w", encoding="utf-8") as fh:
                    fh.write(data)
        except OSError:
            pass
        _log("AUTOPSY captured -> %s" % (outdir,))
    except Exception as exc:  # noqa: BLE001 -- autopsy must never block teardown
        _log("AUTOPSY error (proceeding): %r" % (exc,))
    return outdir


# ===========================================================================
# Black Box Flight Recorder -- checksum-gated, fail-CLOSED teardown.
#
# THE INVARIANT: on failure, the GCP node MUST NOT self-delete until the Black
# Box archive is confirmed (cryptographic sha256) received on the LOCAL Mac.
# Fail-CLOSED toward DATA PRESERVATION: uncertain -> HOLD the node, never burn.
#
# HONEST NAT REALITY: a GCP node cannot push INTO the NAT'd Mac. So the LOCAL
# orchestrator PULLS the archive over the IAP SSH tunnel it already holds
# (gcloud compute scp --tunnel-through-iap), recomputes the sha256 LOCALLY, and
# ONLY THEN authorizes the node's Compute-SA self-delete. Same guarantee, works
# through NAT.
# ===========================================================================


_BLACK_BOX_SCRIPT = os.path.join(_SCRIPTS_DIR, "a1_black_box.py")
# The node-side dir the bundler writes the archive into (env-overridable).
_BLACK_BOX_NODE_OUT = os.environ.get("JARVIS_A1_BLACKBOX_NODE_OUT", "/tmp/a1_black_box")
# The remote jarvis repo root on the node (matches the IaC sync target).
_BLACK_BOX_REMOTE_REPO = os.environ.get("JARVIS_A1_BLACKBOX_REMOTE_REPO", "/opt/trinity/jarvis")


def blackbox_pull_retries() -> int:
    """Bounded pull retries before HOLDING the node (env, default 3)."""
    try:
        return max(1, int(os.environ.get("JARVIS_A1_BLACKBOX_PULL_RETRIES", "3")))
    except ValueError:
        return 3


def assert_dw_primary(env: Dict[str, str]) -> "tuple":
    """DW-primary pre-launch assertion: Claude MUST be disabled AND a DW primary
    override MUST be set. Returns (ok, reason). The Linux overlay already sets
    both; this asserts they SURVIVED env composition (a soak that silently lost
    them would let Claude serve -- defeating the DW-primary audit)."""
    claude_disabled = (env.get("JARVIS_PROVIDER_CLAUDE_DISABLED", "") or "").strip().lower()
    dw_override = (env.get("JARVIS_DW_PRIMARY_OVERRIDE", "") or "").strip()
    if claude_disabled not in {"true", "1", "yes", "on"}:
        return False, ("JARVIS_PROVIDER_CLAUDE_DISABLED is %r (must be true) -- "
                       "Claude is not disabled; the DW-primary audit is invalid."
                       % (env.get("JARVIS_PROVIDER_CLAUDE_DISABLED"),))
    if not dw_override:
        return False, ("JARVIS_DW_PRIMARY_OVERRIDE is unset -- DW is not pinned as "
                       "primary; refusing to launch a non-DW-primary soak.")
    return True, ""


class IapBlackBoxTransport:
    """The local side of the Black Box pull -- drives the IaC hypervisor's IAP
    SSH/scp command builders (NO reimplementation) to: (1) run the node-side
    bundler over SSH, (2) PULL the archive + .sha256 over scp, (3) recompute the
    sha256 LOCALLY, (4) on a verified match, authorize the node self-delete via
    the hypervisor's burn_node. All subprocess is injectable for tests."""

    def __init__(self, *, node: str, hypervisor_args: Any,
                 runner: Optional[Callable[..., subprocess.CompletedProcess]] = None) -> None:
        self.node = node
        self.args = hypervisor_args
        self._run = runner or self._default_run
        self._hyper = None  # lazily imported (avoids heavy import on dev boxes)

    @staticmethod
    def _default_run(argv: Sequence[str]) -> subprocess.CompletedProcess:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False)

    def _hypervisor(self):
        if self._hyper is None:
            spec = importlib.util.spec_from_file_location(
                "sovereign_iac_hypervisor", _HYPERVISOR_SCRIPT)
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            sys.modules.setdefault("sovereign_iac_hypervisor", mod)
            spec.loader.exec_module(mod)
            self._hyper = mod
        return self._hyper

    def bundle_on_node(self, *, run_id: str, out_dir: str) -> Optional[Dict[str, str]]:
        """SSH-exec the node-side bundler; parse BLACK_BOX_ARCHIVE/SHA256."""
        hyper = self._hypervisor()
        remote = (
            "cd %s && python3 scripts/a1_black_box.py --bundle --run-id %s --out %s"
            % (_BLACK_BOX_REMOTE_REPO, run_id, out_dir)
        )
        argv = hyper._ssh_cmd(self.args, self.node, remote)
        cp = self._run(argv)
        out = (cp.stdout or "") + (cp.stderr or "")
        archive = sha = ""
        for line in out.splitlines():
            if line.startswith("BLACK_BOX_ARCHIVE="):
                archive = line.split("=", 1)[1].strip()
            elif line.startswith("BLACK_BOX_SHA256="):
                sha = line.split("=", 1)[1].strip()
        if not archive or not sha:
            _log("[BlackBox] node bundle did not emit archive+sha256 (out=%r)" % (out[-300:],))
            return None
        return {"archive": archive, "sha256": sha, "sha_path": archive + ".sha256"}

    def pull_archive(self, *, node_archive: str, node_sha_path: str,
                     local_dir: str) -> Optional[str]:
        """scp the archive (+ .sha256) over IAP into local_dir, recompute the
        sha256 of the PULLED archive locally. Returns the local hex digest, or
        None on any pull failure (fail-CLOSED -> the caller HOLDS the node)."""
        import hashlib
        hyper = self._hypervisor()
        os.makedirs(local_dir, exist_ok=True)
        for src in (node_archive, node_sha_path):
            # The hypervisor's _scp_cmd builds a PUSH (`... <local> <node>:<remote>`);
            # for a PULL we need `<node>:<remote> <local>`, so build the IAP scp
            # with the direction reversed (same flag shape, reused transport).
            argv = self._scp_pull_cmd(src, local_dir)
            cp = self._run(argv)
            if cp.returncode != 0:
                _log("[BlackBox] scp pull failed for %s rc=%d" % (src, cp.returncode))
                return None
        local_archive = os.path.join(local_dir, os.path.basename(node_archive))
        if not os.path.isfile(local_archive):
            _log("[BlackBox] pulled archive missing on disk: %s" % (local_archive,))
            return None
        digest = hashlib.sha256()
        try:
            with open(local_archive, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            _log("[BlackBox] local sha256 recompute failed: %r" % (exc,))
            return None
        return digest.hexdigest()

    def _scp_pull_cmd(self, node_src: str, local_dir: str) -> List[str]:
        """A PULL scp over IAP: `gcloud compute scp --recurse --tunnel-through-iap
        --project=.. --zone=.. <node>:<remote> <local>` (reuses the hypervisor's
        flag shape; only the direction differs from the push builder)."""
        project = getattr(self.args, "project", None)
        zone = getattr(self.args, "zone", None)
        cmd = ["gcloud", "compute", "scp", "--recurse"]
        if project:
            cmd.append("--project=%s" % (project,))
        if zone:
            cmd.append("--zone=%s" % (zone,))
        cmd.extend(["--tunnel-through-iap", "%s:%s" % (self.node, node_src), local_dir])
        return cmd

    def teardown(self, *, node: str) -> bool:
        """Authorize the node self-delete -- ONLY reached after a verified local
        sha256 match. Reuses the hypervisor's burn_node (quadruple teardown)."""
        hyper = self._hypervisor()
        try:
            hyper.burn_node(self.args, node)
            return True
        except Exception as exc:  # noqa: BLE001 -- a burn error never crashes the harness
            _log("[BlackBox] teardown warning: %r" % (exc,))
            return False


@dataclass
class TeardownDecision:
    authorized: bool
    held: bool
    reason: str = ""
    node_sha256: str = ""
    local_sha256: str = ""
    held_node_file: str = ""
    local_archive_dir: str = ""


class BlackBoxTeardownDecider:
    """The checksum-gated, fail-CLOSED teardown FSM. On a failed A1 run it:
      1. (node) runs the bundler -> archive + sha256 on the node;
      2. (local) PULLs the archive + .sha256 over IAP, recomputes the sha256;
      3. MATCH -> authorize teardown (the existing dead-man / IaC self-delete);
         MISMATCH or pull-fail (after bounded retries) -> DO NOT BURN: HOLD the
         node, write HELD_NODE.txt with the manual-extract command, loud log.
    Fail-CLOSED toward DATA PRESERVATION at every branch."""

    def __init__(self, *, run_id: str, node: str, autopsy_root: str,
                 transport: Any, pull_retries: int = 3) -> None:
        self.run_id = run_id
        self.node = node
        self.autopsy_root = autopsy_root
        self.transport = transport
        self.pull_retries = max(1, int(pull_retries))

    def _local_dir(self) -> str:
        return os.path.join(self.autopsy_root, self.run_id)

    def _hold(self, reason: str, *, node_sha: str = "", local_sha: str = "") -> TeardownDecision:
        """HOLD the node (never burn). Write HELD_NODE.txt with the manual-extract
        command so an operator can recover the data by hand."""
        _log("[BlackBox] DATA NOT CONFIRMED -- NODE HELD for manual extraction: %s" % (reason,))
        local_dir = self._local_dir()
        held_file = ""
        try:
            os.makedirs(local_dir, exist_ok=True)
            held_file = os.path.join(local_dir, "HELD_NODE.txt")
            manual = (
                "gcloud compute ssh %s --tunnel-through-iap "
                "--command 'cat %s/black_box_%s.tar.gz' > %s/black_box_%s.tar.gz"
                % (self.node, _BLACK_BOX_NODE_OUT, self.run_id, local_dir, self.run_id)
            )
            with open(held_file, "w", encoding="utf-8") as fh:
                fh.write(
                    "A1 BLACK BOX -- NODE HELD (data NOT confirmed on local Mac)\n"
                    "==========================================================\n"
                    "run_id     : %s\n" % (self.run_id,)
                    + "node       : %s\n" % (self.node,)
                    + "reason     : %s\n" % (reason,)
                    + "node_sha256 : %s\n" % (node_sha or "<none>",)
                    + "local_sha256: %s\n" % (local_sha or "<none>",)
                    + "\nThe node was NOT burned (fail-CLOSED to data preservation).\n"
                    + "Manually extract the Black Box, then delete the node by hand:\n\n"
                    + "  # 1. pull the archive over IAP:\n  %s\n\n" % (manual,)
                    + "  # 2. verify, THEN delete the node:\n"
                    + "  gcloud compute instances delete %s --quiet\n" % (self.node,)
                )
            _log("[BlackBox] HELD_NODE.txt written -> %s" % (held_file,))
        except OSError as exc:
            _log("[BlackBox] HELD_NODE.txt write warning: %r" % (exc,))
        return TeardownDecision(
            authorized=False, held=True, reason=reason,
            node_sha256=node_sha, local_sha256=local_sha,
            held_node_file=held_file, local_archive_dir=local_dir,
        )

    def run(self) -> TeardownDecision:
        # 1. Bundle on the node.
        bundle = self.transport.bundle_on_node(
            run_id=self.run_id, out_dir=_BLACK_BOX_NODE_OUT)
        if not bundle:
            return self._hold("node-side bundle failed -- no archive to confirm")
        node_sha = bundle.get("sha256", "")
        node_archive = bundle.get("archive", "")
        node_sha_path = bundle.get("sha_path", node_archive + ".sha256")
        local_dir = self._local_dir()

        # 2. PULL + recompute locally, bounded retries.
        local_sha: Optional[str] = None
        for attempt in range(1, self.pull_retries + 1):
            local_sha = self.transport.pull_archive(
                node_archive=node_archive, node_sha_path=node_sha_path,
                local_dir=local_dir)
            if local_sha is not None:
                break
            _log("[BlackBox] pull attempt %d/%d failed -- retrying"
                 % (attempt, self.pull_retries))
        if local_sha is None:
            return self._hold(
                "archive pull failed after %d retries -- data not on local Mac"
                % (self.pull_retries,), node_sha=node_sha)

        # 3. Compare. MATCH -> authorize teardown. MISMATCH -> HOLD.
        if local_sha != node_sha:
            return self._hold(
                "sha256 MISMATCH (node=%s local=%s) -- archive corrupt in transit"
                % (node_sha[:16], local_sha[:16]),
                node_sha=node_sha, local_sha=local_sha)
        _log("[BlackBox] sha256 VERIFIED on local Mac (%s) -- authorizing teardown"
             % (node_sha[:16],))
        self.transport.teardown(node=self.node)
        return TeardownDecision(
            authorized=True, held=False, reason="sha256 verified locally",
            node_sha256=node_sha, local_sha256=local_sha,
            local_archive_dir=local_dir,
        )


# ===========================================================================
# The orchestration run.
# ===========================================================================


@dataclass
class HarnessRun:
    run_id: str
    run_root: str
    autopsy_root: str
    cost_cap: float
    wall_seconds: int
    seed: int
    sse_base: str
    chaos: Any
    soak: Any
    auditor: Any
    autopsy_fn: Callable[..., Any] = local_autopsy
    stub_soak: bool = False
    stub_log_goal: str = "GOAL-A1-STUB"
    # Omni-Soak: when True the inject step fans out a 3-way decomposable chaos set
    # (3 mutually-isolated targets) so the MAS / swarm stack fans out 3-way. When
    # False the inject step uses the single-target path (normal A1 soak, byte-
    # identical). Set from JARVIS_A1_OMNI_SOAK at build time; default OFF.
    omni_soak: bool = False
    # The number of mutually-isolated targets to inject under the Omni-Soak. The 3
    # targets themselves are DISCOVERED by the injector (not a literal list); this
    # is only the fan-out width N.
    omni_targets: int = 3
    env: Optional[Dict[str, str]] = None
    # Black Box Flight Recorder: when set (remote on-node runs), a FAILED verdict
    # runs the checksum-gated, fail-CLOSED teardown decision BEFORE any node burn.
    blackbox_node: str = ""
    blackbox_teardown_fn: Optional[Callable[..., Any]] = None
    _verdict: Dict[str, Any] = field(default_factory=dict)

    def report_dir(self) -> str:
        return os.path.join(self.run_root, self.run_id)

    def _chaos_manifest_path(self) -> str:
        return os.path.join(_REPO_ROOT, ".jarvis", "chaos_manifest.json")

    def execute(self) -> int:
        """Run the full sequence. Chaos is ALWAYS reverted in finally. Returns 0
        iff A1_DISPATCH_PROVEN."""
        rd = self.report_dir()
        os.makedirs(rd, exist_ok=True)
        verdict: Dict[str, Any] = {"proven": False, "failure_locus": "not_run"}
        soak_launched = False
        injected = False
        self._debug_log_path = ""
        try:
            # a. PRE-FLIGHT.
            _log("STEP preflight: chaos status + candidate scan")
            st = self.chaos.status()
            if st.get("active"):
                _log("STEP preflight ABORT: an active chaos manifest already exists")
                verdict = {"proven": False, "failure_locus": "preflight:active_manifest"}
                return 1
            n = self.chaos.list_candidates()
            if n < 1:
                _log("STEP preflight ABORT: no viable chaos candidates (%d)" % (n,))
                verdict = {"proven": False, "failure_locus": "preflight:no_candidates"}
                return 1
            _log("STEP preflight OK: %d candidate(s)" % (n,))

            # b. INJECT (real bug, manifest, test confirmed RED). Under the Omni-
            #    Soak the inject fans out a 3-way decomposable chaos set (3 mutually-
            #    isolated targets) so the MAS / swarm stack actually fans out 3-way;
            #    OFF -> the single-target path is byte-identical. Revert-ALWAYS (the
            #    finally) covers BOTH the single + the N-entry manifest.
            if self.omni_soak:
                _log("STEP inject (omni): decomposable n=%d" % (self.omni_targets,))
                red, decomp_count = self.chaos.inject_decomposable(self.omni_targets)
            else:
                _log("STEP inject: seed=%d" % (self.seed,))
                red = self.chaos.inject(self.seed)
                decomp_count = 0
            injected = True
            if not red:
                if self.stub_soak:
                    # WIRING proof (dry-run --stub-soak): a dev-box pytest quirk
                    # (no green-test confirmable) must NOT block the plumbing
                    # validation. The injector was exercised + will be reverted;
                    # proceed to prove soak->audit->verdict wiring against the stub.
                    _log("STEP inject SOFT-SKIP (stub-soak): no RED-confirm on this "
                         "host; continuing to validate the wiring (not convergence)")
                else:
                    _log("STEP inject ABORT: test did not go RED post-injection")
                    verdict = {"proven": False, "failure_locus": "inject:not_red"}
                    return 1
            elif self.omni_soak:
                _log("STEP inject OK: %d decomposable targets RED" % (decomp_count,))
            else:
                _log("STEP inject OK: bug live, test RED")

            # c. LAUNCH SOAK.
            verdict_out = os.path.join(rd, "a1_verdict.json")
            if self.stub_soak:
                _log("STEP soak: STUB (synthetic debug.log, no spend)")
                debug_log = os.path.join(rd, "stub_debug.log")
                write_stub_soak_log(debug_log, goal_id=self.stub_log_goal)
                handle = SoakHandle(debug_log=debug_log, session_dir=rd, proc=None)
            else:
                _log("STEP soak: launching production O+V soak")
                handle = self.soak.launch(self.env or compose_env(), rd)
            soak_launched = True
            self._debug_log_path = handle.debug_log
            _log("STEP soak OK: debug.log=%s" % (handle.debug_log,))

            # d. LAUNCH AUDITOR (concurrent / parses the timeline).
            _log("STEP audit: watching SSE=%s log=%s" % (self.sse_base, handle.debug_log))
            verdict = self.auditor.watch(
                base=self.sse_base, log_file=handle.debug_log,
                timeout_s=float(self.wall_seconds), verdict_out=verdict_out,
            )
            self._verdict = verdict
            proven = bool(verdict.get("proven"))
            _log("STEP audit VERDICT: %s" % ("A1_DISPATCH_PROVEN" if proven else "FAILED"))

            # f. AUTOPSY ON FAILURE + BLACK BOX checksum-gated teardown.
            if not proven:
                _log("STEP autopsy: failed verdict -> black-box capture")
                self.autopsy_fn(
                    run_id=self.run_id, autopsy_root=self.autopsy_root,
                    debug_log=handle.debug_log, verdict=verdict,
                    chaos_manifest=self._chaos_manifest_path(),
                )
                # Black Box: PULL the node-side archive, verify the sha256 LOCALLY,
                # and ONLY authorize the node self-delete on a match -- else HOLD.
                self._blackbox_on_failure(verdict)
            return 0 if proven else 1
        except (KeyboardInterrupt, SystemExit):
            # SIGINT / explicit exit -- revert (finally) then propagate so the
            # process terminates as the operator expects. The repo is restored.
            verdict = {"proven": False, "failure_locus": "interrupted"}
            raise
        except GraduationFailedException as exc:
            # The Absolute Intervention-Lock tripped (mid-loop human gate) OR a
            # flag-set load failure. Autonomy NOT proven -> failed verdict + autopsy.
            _log("GraduationFailedException: %s" % (exc,))
            verdict = {
                "proven": False,
                "failure_locus": getattr(exc, "failure_locus", "graduation_failed") or "graduation_failed",
                "graduation_exception": exc.to_dict() if hasattr(exc, "to_dict") else str(exc),
            }
            self._verdict = verdict
            self._safe_autopsy(verdict)
            return 1
        except Exception as exc:  # noqa: BLE001 -- any orchestration error -> revert + autopsy
            _log("orchestration error: %r" % (exc,))
            verdict = {"proven": False, "failure_locus": "orchestration_error:%s" % (type(exc).__name__,)}
            self._verdict = verdict
            self._safe_autopsy(verdict)
            return 1
        finally:
            # g. ALWAYS: stop soak, revert chaos, collect the run report.
            if soak_launched and not self.stub_soak:
                try:
                    self.soak.stop()
                except Exception as exc:  # noqa: BLE001
                    _log("soak stop warning: %r" % (exc,))
            # CHAOS-REVERT-ALWAYS -- the repo must NEVER be left broken.
            _log("STEP revert: restoring chaos (always)")
            try:
                self.chaos.revert()
            except Exception as exc:  # noqa: BLE001 -- never let revert mask the real error
                _log("revert warning: %r" % (exc,))
            try:
                self._collect_report(verdict)
            except Exception as exc:  # noqa: BLE001
                _log("report collection warning: %r" % (exc,))
            _ = injected  # documented: revert is safe whether or not we injected

    def _safe_autopsy(self, verdict: Dict[str, Any]) -> None:
        """Invoke the autopsy black-box on a failure path. Best-effort: never
        raises (it must not mask the underlying error). Also runs the Black Box
        checksum-gated teardown decision (fail-CLOSED -> HOLD on any doubt)."""
        try:
            self.autopsy_fn(
                run_id=self.run_id, autopsy_root=self.autopsy_root,
                debug_log=getattr(self, "_debug_log_path", "") or "",
                verdict=verdict, chaos_manifest=self._chaos_manifest_path(),
            )
        except Exception as exc:  # noqa: BLE001
            _log("autopsy invocation warning: %r" % (exc,))
        self._blackbox_on_failure(verdict)

    def _blackbox_on_failure(self, verdict: Dict[str, Any]) -> Optional[Any]:
        """Run the Black Box checksum-gated teardown decision on a failed run.
        Only active when a remote node is wired (blackbox_teardown_fn set) --
        local/dry-run paths have no node to hold/burn, so this is a no-op there.

        Fail-CLOSED: any error in the decision path leaves the node UN-burned
        (we never reach a teardown call on an exception)."""
        if self.blackbox_teardown_fn is None:
            return None
        try:
            decision = self.blackbox_teardown_fn(
                run_id=self.run_id, node=self.blackbox_node,
                autopsy_root=self.autopsy_root,
                debug_log=getattr(self, "_debug_log_path", "") or "",
                verdict=verdict, chaos_manifest=self._chaos_manifest_path(),
            )
            self._blackbox_decision = decision
            if getattr(decision, "held", False):
                _log("[BlackBox] node HELD (data unconfirmed) -- NOT burned: %s"
                     % (getattr(decision, "reason", ""),))
            elif getattr(decision, "authorized", False):
                _log("[BlackBox] teardown AUTHORIZED (sha256 verified locally)")
            return decision
        except Exception as exc:  # noqa: BLE001 -- a decision error must HOLD, never burn
            _log("[BlackBox] decision error -- holding node (fail-CLOSED): %r" % (exc,))
            return None

    def _collect_report(self, verdict: Dict[str, Any]) -> None:
        rd = self.report_dir()
        os.makedirs(rd, exist_ok=True)
        # Ensure the verdict file exists in the report dir.
        vpath = os.path.join(rd, "a1_verdict.json")
        if not os.path.exists(vpath):
            with open(vpath, "w", encoding="utf-8") as fh:
                json.dump(verdict, fh, indent=2)
        # Copy the chaos manifest if present.
        man = self._chaos_manifest_path()
        if os.path.exists(man):
            try:
                with open(man, "r", encoding="utf-8") as src:
                    data = src.read()
                with open(os.path.join(rd, "chaos_manifest.json"), "w", encoding="utf-8") as fh:
                    fh.write(data)
            except OSError:
                pass
        manifest = {
            "run_id": self.run_id,
            "seed": self.seed,
            "cost_cap": self.cost_cap,
            "wall_seconds": self.wall_seconds,
            "sse_base": self.sse_base,
            "stub_soak": self.stub_soak,
            "verdict": verdict,
            "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with open(os.path.join(rd, "run_report.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        _log("STEP collect: run report -> %s" % (rd,))


# ===========================================================================
# Signal-handler revert backstop (chaos-revert-always on SIGINT/SIGTERM).
# ===========================================================================


_ACTIVE_CHAOS: List[Any] = []


def _install_revert_signal_handlers() -> None:
    """Install SIGINT/SIGTERM handlers that revert any active chaos before the
    process dies -- the repo must NEVER be left broken even on an external kill.
    The handler re-raises the default behaviour after reverting."""

    def _handler(signum, _frame):  # noqa: ANN001
        _log("signal %d received -- reverting chaos before exit" % (signum,))
        for chaos in list(_ACTIVE_CHAOS):
            try:
                chaos.revert()
            except Exception:  # noqa: BLE001
                pass
        # Restore default + re-raise so the process terminates as expected.
        try:
            signal.signal(signum, signal.SIG_DFL)
        except Exception:  # noqa: BLE001
            pass
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Not in main thread (e.g. under pytest) -- skip silently.
            pass


# ===========================================================================
# Remote mode -- the money-gated GCP node provisioning.
# ===========================================================================


def estimate_remote_cost(wall_seconds: int) -> float:
    return round(_NODE_COST_PER_HOUR * (wall_seconds / 3600.0), 4)


def make_blackbox_teardown_fn(*, node: str, hypervisor_args: Any,
                              pull_retries: Optional[int] = None) -> Callable[..., Any]:
    """Factory: a teardown function the HarnessRun calls on a FAILED verdict. It
    bundles the Black Box on the node, PULLs it over IAP, verifies the sha256
    LOCALLY, and authorizes the node self-delete ONLY on a match -- else HOLDs.
    Wired into build_live_run when a node + hypervisor args are available."""
    transport = IapBlackBoxTransport(node=node, hypervisor_args=hypervisor_args)
    retries = pull_retries if pull_retries is not None else blackbox_pull_retries()

    def _teardown(*, run_id, node, autopsy_root, debug_log, verdict, chaos_manifest):
        decider = BlackBoxTeardownDecider(
            run_id=run_id, node=node, autopsy_root=autopsy_root,
            transport=transport, pull_retries=retries,
        )
        return decider.run()

    return _teardown


def provision_and_run_remote(*, cost_cap: float, wall_seconds: int, seed: int,
                             extra_args: Optional[Sequence[str]] = None) -> int:
    """Drive sovereign_iac_hypervisor.py to provision the Linux node, sync the
    repo, and run this harness with --execute-on-node remotely (streaming stdout
    back). Cost-bounded + node dead-man + teardown-always (the hypervisor owns
    the node lifecycle / self-delete).

    BLACK BOX INVARIANT: the node-side dead-man fires on the completion-sentinel.
    To keep diagnostic data safe, the orchestrator must NOT let the node burn its
    Black Box before the archive is confirmed on the local Mac. We pass
    ``JARVIS_A1_BLACKBOX_HOLD_ON_FAILURE=1`` into the hypervisor env so the on-node
    failure path withholds the completion-sentinel until the local PULL + sha256
    verification authorizes it (the checksum-gated teardown). Only reached AFTER
    the money-gate."""
    # Propagate the Omni-Soak arming to the NODE so its compose_env loads the omni
    # overlay (the full MAS/fan-out stack) + the inject step uses the decomposable
    # 3-target injector. Without this prefix the node's compose_env never sees the
    # flag and falls back to the linux_prod overlay (no fan-out).
    _omni_prefix = "JARVIS_A1_OMNI_SOAK=1 " if omni_soak_enabled(os.environ) else ""
    remote_cmd = (
        "JARVIS_IAC_HYPERVISOR_ENABLED=1 " + _omni_prefix
        + "python3 scripts/a1_live_fire_chaos_harness.py "
        "--execute-on-node --cost-cap %s --max-wall-seconds %d --seed %d"
        % (cost_cap, wall_seconds, seed)
    )
    argv = [
        sys.executable, _HYPERVISOR_SCRIPT,
        "--execute", "--i-understand-this-spends-money",
        "--surgery-cmd", remote_cmd,
        "--surgery-timeout-s", str(wall_seconds + 600),
    ]
    # --on-demand pass-through (env-gated): provision a STANDARD (no Spot
    # preemption) node for an uninterrupted multi-stage run when the operator
    # opts in. JARVIS_IAC_ON_DEMAND in {1,true,yes} -> append --on-demand.
    if os.environ.get("JARVIS_IAC_ON_DEMAND", "").strip().lower() in {"1", "true", "yes"}:
        argv.append("--on-demand")
    if extra_args:
        argv.extend(extra_args)
    env = dict(os.environ)
    env["JARVIS_IAC_HYPERVISOR_ENABLED"] = "1"
    # GIT TRANSPORT: drive Run #4 over the fast-WAN git-clone transport (the node
    # clones origin at the EXACT local HEAD), replacing the <1MB/s IAP tar-pipe
    # that killed 3 A1 soak runs. Env-gated / default-ON for the A1 remote path
    # (the operator can pin tar via JARVIS_IAC_SYNC_TRANSPORT=tar before --remote).
    env.setdefault("JARVIS_IAC_SYNC_TRANSPORT", "git")
    # Signal the on-node run to fail-CLOSED on a failed verdict: HOLD the Black Box
    # (do not drop the completion-sentinel) until the local checksum verification.
    env.setdefault("JARVIS_A1_BLACKBOX_HOLD_ON_FAILURE", "1")
    # Write the A1 launch manifest locally alongside .env and inject it into the
    # hypervisor's secret-files list so it gets SCP'd to the SAME node directory.
    # The on-node boot path validates this manifest (fail-CLOSED) before the soak.
    _local_manifest_path = os.path.join(_REPO_ROOT, "A1_LAUNCH_MANIFEST.json")
    _remote_manifest = build_manifest(
        model="Qwen/Qwen3.5-397B-A17B-FP8",
        native_tool_forcing=True,
        epistemic_feedback=True,
        seed=seed,
        cost_cap=cost_cap,
        max_wall_seconds=wall_seconds,
    )
    write_manifest(_local_manifest_path, _remote_manifest)
    _log("[A1Manifest] wrote local manifest -> %s" % _local_manifest_path)
    # Append to JARVIS_IAC_SECRET_FILES so the hypervisor SCPs it with .env.
    _existing_secrets = env.get("JARVIS_IAC_SECRET_FILES", ".env")
    if "A1_LAUNCH_MANIFEST.json" not in _existing_secrets:
        env["JARVIS_IAC_SECRET_FILES"] = _existing_secrets + ",A1_LAUNCH_MANIFEST.json"
    _log("STEP remote: provisioning node + remote surgery (dead-man armed, "
         "Black Box checksum-gate active)")
    cp = subprocess.run(argv, env=env, check=False)
    return cp.returncode


# ===========================================================================
# Builders (also used by the test to spy the dry-run chaos controller).
# ===========================================================================


def _new_run_id() -> str:
    return time.strftime("a1-%Y%m%d-%H%M%S")


def build_dry_run_local(args: argparse.Namespace) -> HarnessRun:
    """Construct a HarnessRun for --dry-run-local: REAL chaos controller (so the
    injector + manifest + revert are exercised), a STUB soak (no spend), and the
    in-proc StubAuditorRunner driving the REAL auditor core for the PROVEN path."""
    run_id = _new_run_id()
    chaos = ChaosController(repo_root=_REPO_ROOT, test_timeout_s=args.chaos_test_timeout)
    auditor = StubAuditorRunner(strict=args.strict)
    return HarnessRun(
        run_id=run_id,
        run_root=os.path.join(os.getcwd(), "a1_runs"),
        autopsy_root=os.path.join(os.getcwd(), "a1_autopsy"),
        cost_cap=0.0,
        wall_seconds=args.max_wall_seconds,
        seed=args.seed,
        sse_base=args.base,
        chaos=chaos,
        soak=None,
        auditor=auditor,
        stub_soak=bool(args.stub_soak),
        omni_soak=omni_soak_enabled(),
    )


def build_live_run(args: argparse.Namespace) -> HarnessRun:
    """Construct a HarnessRun for the real on-node soak (--execute-on-node)."""
    run_id = _new_run_id()
    chaos = ChaosController(repo_root=_REPO_ROOT, test_timeout_s=args.chaos_test_timeout)
    soak = SoakRunner(repo_root=_REPO_ROOT, cost_cap=args.cost_cap,
                      wall_seconds=args.max_wall_seconds)
    auditor = AuditorRunner(strict=args.strict)
    env = compose_env()
    env["OUROBOROS_BATTLE_COST_CAP"] = str(args.cost_cap)
    env["OUROBOROS_BATTLE_MAX_WALL_SECONDS"] = str(args.max_wall_seconds)
    return HarnessRun(
        run_id=run_id,
        run_root=os.path.join(os.getcwd(), "a1_runs"),
        autopsy_root=os.path.join(os.getcwd(), "a1_autopsy"),
        cost_cap=args.cost_cap,
        wall_seconds=args.max_wall_seconds,
        seed=args.seed,
        sse_base=args.base,
        chaos=chaos,
        soak=soak,
        auditor=auditor,
        env=env,
        omni_soak=omni_soak_enabled(env),
    )


# ===========================================================================
# CLI
# ===========================================================================


def _print_composed_env_for_audit() -> None:
    env = compose_env()
    flags = derive_cognitive_flags()
    _log("composed env: %d derived cognitive flags ON (from CADENCE_POLICY)" % (len(flags),))
    for flag in sorted(flags):
        _log("  %s=%s" % (flag, env.get(flag, "?")))
    for k in ("JARVIS_ROADMAP_ORCHESTRATOR_ENABLED", "JARVIS_IDE_STREAM_ENABLED",
              "JARVIS_A1_TRACE_ENABLED", "JARVIS_PROVIDER_CLAUDE_DISABLED",
              "JARVIS_DW_PRIMARY_OVERRIDE",
              "OUROBOROS_BATTLE_MAX_WALL_SECONDS"):
        _log("  %s=%s" % (k, env.get(k, "<unset>")))
    # DW-primary pre-launch assertion -- surface its verdict for audit.
    ok, reason = assert_dw_primary(env)
    _log("  DW-primary assertion: %s%s"
         % ("PINNED" if ok else "NOT PINNED", "" if ok else " (%s)" % (reason,)))


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="a1_live_fire_chaos_harness.py",
        description=(
            "A1 Live-Fire Chaos Harness -- sequences inject -> O+V soak -> live "
            "audit -> A1 verdict, with chaos-revert-always + autopsy-on-failure "
            "+ cost-bounded money-gated remote node. Reuses the 5 existing scripts."
        ),
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run-local", action="store_true",
                      help="Prove the WIRING locally with NO spend (real inject+revert, "
                           "stub soak, real auditor core).")
    mode.add_argument("--remote", action="store_true",
                      help="Provision the cost-bounded GCP Linux node + run remotely. "
                           "REQUIRES --i-understand-this-spends-money.")
    mode.add_argument("--execute-on-node", action="store_true",
                      help="(internal) run the real soak+audit on the provisioned node.")
    mode.add_argument("--print-env", action="store_true",
                      help="Print the composed (derived) flag env for audit + exit.")

    p.add_argument("--stub-soak", action="store_true",
                   help="With --dry-run-local: emit a deterministic synthetic debug.log "
                        "(full PROVEN A1Trace timeline) instead of a real soak.")
    p.add_argument("--cost-cap", type=float,
                   default=float(os.environ.get("OUROBOROS_BATTLE_COST_CAP", "10.0")),
                   help="Hard USD cost cap passed to the soak + cost estimate.")
    p.add_argument("--max-wall-seconds", type=int,
                   default=int(os.environ.get("OUROBOROS_BATTLE_MAX_WALL_SECONDS", "3600")),
                   help="Hard wall-clock ceiling (soak + audit timeout).")
    p.add_argument("--seed", type=int, default=0,
                   help="Deterministic chaos candidate selection seed.")
    p.add_argument("--base", default=os.environ.get("JARVIS_OBSERVABILITY_BASE", "http://127.0.0.1:8099"),
                   help="Observability SSE base URL (loopback default).")
    p.add_argument("--chaos-test-timeout", type=float, default=60.0,
                   help="Per-test pytest timeout for the chaos injector RED-confirmation.")
    strict_default = (
        os.environ.get("JARVIS_A1_AUDIT_STRICT", "true").strip().lower()
        not in {"0", "false", "no", "off"}
    )
    p.add_argument("--strict", dest="strict", action="store_true", default=strict_default,
                   help="UNVERIFIABLE flags fail the audit (default).")
    p.add_argument("--lenient", dest="strict", action="store_false",
                   help="UNVERIFIABLE flags only warn (do not fail the wiring proof).")
    p.add_argument("--i-understand-this-spends-money", dest="money_gate",
                   action="store_true",
                   help="REAL-MONEY safety gate (required with --remote).")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.print_env:
        _print_composed_env_for_audit()
        return 0

    # --- REMOTE: money-gated ------------------------------------------------
    if args.remote:
        est = estimate_remote_cost(args.max_wall_seconds)
        if not args.money_gate:
            _log("REFUSED: --remote requires --i-understand-this-spends-money "
                 "(real-money safety gate).")
            _log("COST ESTIMATE: e2-standard-8 Spot ~$%.2f/hr x %d s = ~$%.2f "
                 "(plus the audit window)." % (_NODE_COST_PER_HOUR, args.max_wall_seconds, est))
            return 2
        _log("COST ESTIMATE: ~$%.2f for the node window (cap=$%.2f)." % (est, args.cost_cap))
        return provision_and_run_remote(
            cost_cap=args.cost_cap, wall_seconds=args.max_wall_seconds, seed=args.seed,
        )

    # --- DRY-RUN-LOCAL: wiring proof, no spend, always-revert --------------
    if args.dry_run_local:
        # Pin the derived flags into the env so the in-proc auditor core (which
        # also calls load_audit_flags) reuses them via the JARVIS_A1_AUDIT_FLAGS
        # override -- avoids a second heavy CADENCE_POLICY import on a dev box.
        if not os.environ.get("JARVIS_A1_AUDIT_FLAGS"):
            try:
                os.environ["JARVIS_A1_AUDIT_FLAGS"] = ",".join(derive_cognitive_flags())
            except Exception as exc:  # noqa: BLE001
                _log("flag derivation warning (continuing): %r" % (exc,))
        _print_composed_env_for_audit()
        run = build_dry_run_local(args)
        _ACTIVE_CHAOS.append(run.chaos)
        _install_revert_signal_handlers()
        try:
            rc = run.execute()
        finally:
            if run.chaos in _ACTIVE_CHAOS:
                _ACTIVE_CHAOS.remove(run.chaos)
        return rc

    # --- ON-NODE: the real soak+audit (only on the provisioned Linux node) --
    if args.execute_on_node:
        _print_composed_env_for_audit()
        # DW-primary pre-launch assertion: Claude MUST be disabled AND DW pinned.
        # The Linux overlay sets both; this asserts they SURVIVED composition.
        ok, reason = assert_dw_primary(compose_env())
        if not ok:
            _log("REFUSED: DW-primary not pinned -- %s" % (reason,))
            return 2
        # A1 launch manifest validation -- FAIL-CLOSED: the manifest must be
        # present and schema-valid before the soak may proceed.  The --remote path
        # writes it locally and SCPs it alongside .env via JARVIS_IAC_SECRET_FILES.
        _node_manifest_path = Path(_REPO_ROOT) / "A1_LAUNCH_MANIFEST.json"
        try:
            _node_manifest = load_and_validate(_node_manifest_path)
        except A1ManifestError as exc:
            print("[A1Manifest] FATAL: %s" % exc, file=sys.stderr)
            return 1
        apply_manifest(_node_manifest, dict())  # validate apply path is reachable
        _log("[A1Manifest] applied model=%s forcing=%s epistemic=%s" % (
            _node_manifest["model"],
            _node_manifest["native_tool_forcing"],
            _node_manifest["epistemic_feedback"],
        ))
        run = build_live_run(args)
        _ACTIVE_CHAOS.append(run.chaos)
        _install_revert_signal_handlers()
        try:
            rc = run.execute()
        finally:
            if run.chaos in _ACTIVE_CHAOS:
                _ACTIVE_CHAOS.remove(run.chaos)
        return rc

    _log("no mode selected -- see --help (--dry-run-local / --remote / --print-env).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

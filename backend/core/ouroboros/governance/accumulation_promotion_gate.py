"""Whether an accumulation-branch landing has earned a place on ``main``.

## What this is, and what it deliberately is NOT

The git mechanism already exists and is careful:
:meth:`WorktreeManager.promote_commits` does ff-merge-or-cherry-pick,
non-destructively, aborting a conflicted pick and surfacing a typed
``PromotionError('conflict_aborted')`` with the target byte-identical. The
in-flight POLICY half exists too — :mod:`workspace_promoter` consults LiveWork
and re-checks GENERATE-time drift at the end of an op that is still running.

Neither answers the question asked HERE, which is post-hoc and different in
kind: *a commit landed days ago, in a session that is over — should it move?*
The op's context is gone, so drift-against-GENERATE-hashes has nothing to
compare, and the operator is not mid-soak. What remains verifiable is what the
repository itself still holds: the commit's provenance, whether the code it
touches is covered and green, and whether it removed anything it did not
declare. This module asks exactly those three questions and then DELEGATES the
merge. It contains no git plumbing of its own by design.

## Why the third check exists

Provenance and tests were the two asked for, and on their own they would have
promoted the commit that prompted this module. ``7f8c686ce0`` carries valid
trailers and its module's tests pass — and it also deleted the module's
``__all__``, de-indented a docstring continuation line, and churned quote
style, while its own message says "Keep behaviour otherwise identical."

That is not a new failure mode. It is the whole-file re-emission signature this
repository has been fighting for weeks: a mid-size model asked to re-emit a
file reproduces *most* of it. Tests cannot see it — nothing asserts ``__all__``
— so a gate built only from tests would ratify exactly the damage the diff
schema was built to prevent. A structural check is cheap, mechanical, and
catches the one class of regression this lane actually produces.

The check is a SHRINK check, not an equality check: adding a public symbol is
ordinary work; silently dropping one is the defect.

## Failure direction

Fail CLOSED. Any check that does not return a clear pass refuses the
promotion, and a refusal is not a rollback: the accumulation branch is left
exactly where it was, which is already the quarantined reviewable artifact the
production posture calls for. Nothing here ever deletes, force-updates or
rewrites a ref.

Refusals are recorded to LessonMemory, because a refusal is precisely the
signal the generating lane needs and never got. Successful promotions are NOT
recorded there — LessonMemory is failure-mode memory that feeds few-shot
injection, and writing successes into it would degrade the corpus it exists to
be. The landing record belongs to
:mod:`goal_reconciliation_ledger`, which already owns "is this commit on the
landing ref".
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.PromotionGate")

__all__ = [
    "Finding",
    "PromotionVerdict",
    "PromotionConflictFault",
    "gate_enabled",
    "verify_commit",
    "promote_accumulation_commit",
]

#: The verdict a promotion earns when it changes nothing a caller can observe.
TRIVIAL_CHANGE_VIOLATION = "trivial_change_violation"

_ENV_ENABLED = "JARVIS_ACCUMULATION_PROMOTION_ENABLED"
_ENV_QUARANTINE_PREFIX = "JARVIS_PROMOTION_QUARANTINE_PREFIX"
_DEFAULT_QUARANTINE_PREFIX = "ouroboros/quarantine"

#: Trailers an autonomous commit must carry to be promotable. These are what
#: the AutoCommitter writes; a commit without them did not come from the
#: sanctioned lane, whatever its content looks like.
_REQUIRED_TRAILERS: Tuple[str, ...] = ("Op-ID", "Session", "Files")


def gate_enabled() -> bool:
    """Default OFF. Promotion moves work onto the operator's branch; it opts
    in explicitly, exactly as ``workspace_promoter`` does."""
    return (os.environ.get(_ENV_ENABLED, "") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _quarantine_prefix() -> str:
    raw = (os.environ.get(_ENV_QUARANTINE_PREFIX, "") or "").strip()
    return raw or _DEFAULT_QUARANTINE_PREFIX


class QuarantineIsolationFault(RuntimeError):
    """The conflicting state could not be pinned where it would be findable.

    Distinct from :class:`PromotionConflictFault`, which says the merge did not
    apply. This says the RECORD of that failure could not be written — a
    quarantine ref name already taken by a different commit, or a ref write
    the repository refused. Nothing about ``main`` changes either way; what is
    lost is the ability to find the wreckage later, which is the whole reason
    the ref exists.
    """

    def __init__(self, reason: str, detail: str = "", ref: str = ""):
        super().__init__(f"{reason}: {detail}"[:400])
        self.reason = reason
        self.detail = detail
        self.ref = ref


class PromotionConflictFault(RuntimeError):
    """A promotion could not complete against the tree as it stands.

    Carries the quarantine ref so the conflicting state is addressable rather
    than merely described. Raised for conflicts and lock contention — never
    for a policy refusal, which is an ordinary verdict and not a fault.
    """

    def __init__(self, reason: str, detail: str = "", quarantine_ref: str = ""):
        super().__init__(f"{reason}: {detail}"[:400])
        self.reason = reason
        self.detail = detail
        self.quarantine_ref = quarantine_ref


@dataclass(frozen=True)
class Finding:
    """One check's result. ``blocking`` is what the verdict actually reads."""

    check: str
    passed: bool
    detail: str = ""
    blocking: bool = True

    def render(self) -> str:
        mark = "ok" if self.passed else ("REFUSE" if self.blocking else "warn")
        return f"[{mark}] {self.check}: {self.detail}"[:300]


@dataclass(frozen=True)
class PromotionVerdict:
    promoted: bool
    state: str
    commit_shas: Tuple[str, ...] = ()
    landed_shas: Tuple[str, ...] = ()
    findings: Tuple[Finding, ...] = ()
    detail: str = ""

    @property
    def refusals(self) -> Tuple[Finding, ...]:
        return tuple(f for f in self.findings if not f.passed and f.blocking)

    def render(self) -> str:
        head = f"{self.state} ({'promoted' if self.promoted else 'not promoted'})"
        return "\n".join([head] + [f.render() for f in self.findings])


# ---------------------------------------------------------------------------
# git reads — read-only, bounded. Mutation is delegated, never done here.
# ---------------------------------------------------------------------------


def _git(args: Sequence[str], cwd: Path, timeout_s: float = 15.0) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout_s,
        )
        return proc.returncode, (proc.stdout or "")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[PromotionGate] git %s degraded: %r", args[:2], exc)
        return 1, ""


def _commit_message(sha: str, repo_root: Path) -> str:
    rc, out = _git(["log", "-1", "--format=%B", sha], repo_root)
    return out if rc == 0 else ""


def _touched_files(sha: str, repo_root: Path, base: str = "") -> Tuple[str, ...]:
    """Files the promotion would change, across the whole range.

    Promotion moves a SET of commits, so the range is the honest unit: an
    intermediate commit that breaks something a later one repairs has not
    changed what lands. Falls back to the single commit when no base is
    given.
    """
    if base:
        rc, out = _git(["diff", "--name-only", f"{base}..{sha}"], repo_root)
    else:
        rc, out = _git(["show", "--pretty=format:", "--name-only", sha], repo_root)
    if rc != 0:
        return ()
    return tuple(p.strip() for p in out.splitlines() if p.strip())


def _file_at(sha: str, path: str, repo_root: Path) -> Optional[str]:
    rc, out = _git(["show", f"{sha}:{path}"], repo_root)
    return out if rc == 0 else None


# ---------------------------------------------------------------------------
# The three checks
# ---------------------------------------------------------------------------


def _classify_commit(
    sha: str, repo_root: Path, branch: str, owner: str,
) -> Tuple[str, str]:
    """``(kind, detail)`` — ``autonomous``, ``operator`` or ``unknown``.

    An autonomous commit carries the AutoCommitter's trailers, and the branch
    name independently carries the session id, so the two must AGREE: a commit
    claiming a session its branch does not is not one this gate can vouch for.

    An operator commit carries no lane trailers and is authored by the
    repository's own configured identity — read from git config rather than
    named here, because a gate that hardcodes who the operator is breaks the
    moment the repository changes hands.

    Anything else is ``unknown``, and unknown refuses. The threat this closes
    is not a malicious author; it is an unreviewed commit drifting onto an
    accumulation branch and riding a legitimate landing onto main.
    """
    msg = _commit_message(sha, repo_root)
    if not msg:
        return "unknown", f"{sha[:12]}: no commit message"
    # Line-anchored, because a trailer is a line and a substring is not. The
    # naive `"Session:" in msg` form matched this file's own attribution
    # footer (`Claude-Session:`) and classified an operator commit as a
    # half-written autonomous one.
    missing = [
        t for t in _REQUIRED_TRAILERS
        if not re.search(rf"^{re.escape(t)}:", msg, re.MULTILINE)
    ]
    if not missing:
        m = re.search(r"^Session:\s*(\S+)", msg, re.MULTILINE)
        session = m.group(1) if m else ""
        if not session:
            return "unknown", f"{sha[:12]}: Session trailer present but empty"
        if session not in branch:
            return "unknown", (
                f"{sha[:12]}: claims session {session!r}, branch does not carry it"
            )
        op = re.search(r"^Op-ID:\s*(\S+)", msg, re.MULTILINE)
        return "autonomous", f"{sha[:12]} op={(op.group(1) if op else '?')[:24]}"
    rc, author = _git(["log", "-1", "--format=%ae", sha], repo_root)
    author = (author or "").strip()
    if len(missing) < len(_REQUIRED_TRAILERS):
        # Half a trailer block is worse than none: something wrote a lane
        # header and did not finish it, so neither story is credible.
        return "unknown", (
            f"{sha[:12]}: partial lane trailers (missing {', '.join(missing)})"
        )
    if rc == 0 and owner and author == owner:
        return "operator", f"{sha[:12]} by {author}"
    return "unknown", (
        f"{sha[:12]}: no lane trailers and author {author or '?'} "
        f"is not the repository owner"
    )


def _check_provenance(
    sha: str, repo_root: Path, branch: str, base: str = "",
) -> Finding:
    """Everything that would land must have a verifiable origin.

    Judged across the RANGE, because the range is what moves. Two rules:

    * no ``unknown`` commits — each is either the sanctioned lane's or the
      repository owner's;
    * at least one ``autonomous`` commit — otherwise this is not an
      accumulation promotion at all, and an ordinary merge is the honest tool
      rather than a gate built to vouch for machine-authored work.

    The single-commit form was the first shape of this check, and it refused
    its own collateral repair: an operator fix committed on top of a landing
    carries no lane trailers by construction. A gate that cannot tell "not
    from the lane" from "not from anyone" blocks the normal way defects get
    fixed.
    """
    rc, owner = _git(["config", "user.email"], repo_root)
    owner = (owner or "").strip() if rc == 0 else ""
    if base:
        rc, out = _git(["rev-list", f"{base}..{sha}"], repo_root)
        shas = [s.strip() for s in out.splitlines() if s.strip()] if rc == 0 else []
    else:
        shas = [sha]
    if not shas:
        return Finding("provenance", False, "nothing to promote in this range")
    kinds: Dict[str, List[str]] = {"autonomous": [], "operator": [], "unknown": []}
    for s in shas:
        kind, detail = _classify_commit(s, repo_root, branch, owner)
        kinds[kind].append(detail)
    if kinds["unknown"]:
        return Finding(
            "provenance", False,
            "unattributable commit(s): " + "; ".join(kinds["unknown"][:3]),
        )
    if not kinds["autonomous"]:
        return Finding(
            "provenance", False,
            "no autonomous commit in the range — that is an ordinary merge, "
            "not an accumulation promotion",
        )
    return Finding(
        "provenance", True,
        f"{len(kinds['autonomous'])} autonomous ({kinds['autonomous'][0]}), "
        f"{len(kinds['operator'])} operator",
    )


def _public_surface(source: str) -> Optional[Dict[str, Any]]:
    """Module-level public names plus declared ``__all__``. ``None`` if the
    source will not parse — which is itself a refusal, handled by the caller."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    names: set = set()
    declared: set = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                    try:
                        value = ast.literal_eval(node.value)
                        declared = {str(v) for v in value}
                    except Exception:  # noqa: BLE001
                        declared = set()
                elif isinstance(tgt, ast.Name) and not tgt.id.startswith("_"):
                    names.add(tgt.id)
    return {"names": names, "all": declared}


def _check_structure(sha: str, repo_root: Path, base: str = "") -> Finding:
    """Nothing public may vanish that the promotion did not declare removing.

    A SHRINK check: new public symbols are ordinary work. This is the one
    regression class the generating lane actually produces — a re-emitted file
    that reproduces most of itself — and no test in this repository can see it.

    Judged across the RANGE. A commit that drops an export and a later commit
    on the same branch that restores it are, together, no loss — and together
    is how they land.
    """
    parent = base or f"{sha}^"
    lost: List[str] = []
    for path in _touched_files(sha, repo_root, base=base):
        if not path.endswith(".py"):
            continue
        before = _file_at(parent, path, repo_root)
        after = _file_at(sha, path, repo_root)
        if before is None or after is None:
            continue                       # added or deleted file — not a shrink
        b, a = _public_surface(before), _public_surface(after)
        if a is None:
            return Finding("structure", False, f"{path} does not parse after the commit")
        if b is None:
            continue                       # it did not parse before either
        gone_names = b["names"] - a["names"]
        gone_all = b["all"] - a["all"]
        if b["all"] and not a["all"]:
            lost.append(f"{path}: __all__ removed ({len(b['all'])} exports)")
        elif gone_all:
            lost.append(f"{path}: __all__ lost {sorted(gone_all)}")
        if gone_names:
            lost.append(f"{path}: public symbols lost {sorted(gone_names)}")
    if lost:
        return Finding("structure", False, "; ".join(lost))
    return Finding("structure", True, "no public surface lost")


def _check_semantic_delta(sha: str, repo_root: Path, base: str = "") -> Finding:
    """Does this promotion change what the code DOES?

    ## The commit that forced it

    Soak bt-2026-09-17-180722 re-dispatched an already-landed goal and produced
    ``7e7fe18c3c``. Its entire diff: a duplicated ``# [Ouroboros] Modified
    by...`` banner, ``separators=(",", ":")`` re-quoted to single quotes, and a
    stripped trailing newline. The logging it claimed to add was already on
    main from the earlier landing. Provenance passed, structure passed, tests
    were green — the gate would have promoted a commit that does nothing.

    ## No threshold, because the right answer is an equality

    The temptation is to score "how much" changed and reject below some bar.
    That bar would be a constant nobody can justify, and it would eventually
    reject a real one-character fix. There is no bar here: the normalized AST
    either differs or it does not.

    ``ast.dump`` already erases exactly the churn that matters and nothing
    else — comments are not in the AST at all, whitespace and indentation are
    structure rather than text, quote style is not represented, and a trailing
    newline is invisible. What survives is what a caller could observe.

    ## What is deliberately NOT excluded

    "Redundant logging" is not filtered out, though it was asked for. Judging a
    logging call redundant needs semantics no AST comparison has — and the
    change that started this whole thread (``7f8c686ce0``) was *precisely* the
    addition of two logging calls, and it was real work. A rule that discards
    added logging would have rejected the one genuine autonomous landing this
    repository has produced.

    Non-Python files are counted as changed on a byte basis: this module has no
    business deciding whether a YAML edit is meaningful.
    """
    parent = base or f"{sha}^"
    files = _touched_files(sha, repo_root, base=base)
    if not files:
        return Finding("semantic_delta", False, "the range changes no files at all")
    unchanged: List[str] = []
    for path in files:
        before = _file_at(parent, path, repo_root)
        after = _file_at(sha, path, repo_root)
        if before is None or after is None:
            return Finding(
                "semantic_delta", True, f"{path} added or deleted — a real change",
            )
        if not path.endswith(".py"):
            if before != after:
                return Finding("semantic_delta", True, f"{path} changed")
            unchanged.append(path)
            continue
        try:
            # The SAME canonicalisation VALIDATE uses — one normaliser, or the
            # two seams disagree about what "no change" means and a candidate
            # refused upstream becomes promotable downstream.
            from backend.core.ouroboros.governance.declared_symbols import (  # noqa: E501,PLC0415
                canonical_ast_dump,
            )
            b, a = canonical_ast_dump(before), canonical_ast_dump(after)
        except SyntaxError:
            # Unparsable either side: fall back to bytes rather than claim a
            # no-op we cannot actually demonstrate.
            if before != after:
                return Finding("semantic_delta", True, f"{path} changed (unparsable)")
            unchanged.append(path)
            continue
        if a != b:
            return Finding("semantic_delta", True, f"{path} has a functional delta")
        unchanged.append(path)
    return Finding(
        "semantic_delta", False,
        "no functional change in " + ", ".join(unchanged[:3])
        + " — comments, formatting and quote style only",
    )


def _test_paths_for(files: Sequence[str], repo_root: Path) -> Tuple[str, ...]:
    """The conventional test files for the touched sources.

    Same convention the coverage sensor uses to decide a module is uncovered
    (``tests/**/test_<stem>.py``), so "covered" means the same thing to the
    gate as it does to the sensor that files the goal.
    """
    out: List[str] = []
    tests_root = repo_root / "tests"
    if not tests_root.is_dir():
        return ()
    for f in files:
        stem = Path(f).stem
        if not f.endswith(".py") or f.startswith("tests/"):
            continue
        for cand in tests_root.rglob(f"test_{stem}.py"):
            rel = str(cand.relative_to(repo_root)).replace("\\", "/")
            if rel not in out:
                out.append(rel)
    return tuple(out)


async def _check_coverage(
    sha: str, repo_root: Path, *, python_bin: str, timeout_s: float,
    base: str = "",
) -> Finding:
    """The touched code must be covered, and that cover must be green.

    Run against the tree AS IT STANDS. Promotion asks whether this commit can
    join the current main, so the current main is what it must be green
    against — checking it against its own parent would answer a question
    nobody asked.
    """
    files = _touched_files(sha, repo_root, base=base)
    tests = _test_paths_for(files, repo_root)
    if not tests:
        return Finding(
            "coverage", False,
            f"no tests/**/test_<stem>.py for {', '.join(files[:3]) or 'the touched files'}",
        )
    from backend.core.ouroboros.governance.process_session import (  # noqa: PLC0415
        contain_argv_async, reap_session,
    )
    proc = None
    try:
        # The commit under promotion is candidate code until it is promoted.
        proc = await asyncio.create_subprocess_exec(
            *await contain_argv_async(
                [python_bin, "-m", "pytest", *tests, "-q", "-p", "no:cacheprovider"],
                owner="promotion_gate",
            ),
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return Finding("coverage", False, f"tests exceeded {timeout_s:.0f}s")
    except Exception as exc:  # noqa: BLE001
        return Finding("coverage", False, f"could not run tests: {exc!r}"[:200])
    finally:
        # A timeout used to return with the run still going.
        if proc is not None:
            reap_session(proc.pid, owner="promotion_gate")
    text = re.sub(r"\x1b\[[0-9;]*m", "", (raw or b"").decode("utf-8", "replace"))
    failed = [
        ln for ln in text.splitlines()
        if ln.startswith("FAILED") or ln.startswith("ERROR")
    ]
    if proc.returncode != 0 or failed:
        return Finding(
            "coverage", False,
            f"{len(failed)} failing in {', '.join(tests[:3])}: "
            f"{failed[0][:120] if failed else 'nonzero exit'}",
        )
    return Finding("coverage", True, f"green: {', '.join(tests[:3])}")


# ---------------------------------------------------------------------------
# Verify, then delegate
# ---------------------------------------------------------------------------


async def verify_commit(
    sha: str, *, repo_root: Path, branch: str,
    python_bin: str = "python3", test_timeout_s: float = 300.0,
    base: str = "",
) -> Tuple[Finding, ...]:
    """Run every check. Read-only; mutates nothing, ever. NEVER raises.

    ``base`` makes the verification range-aware: pass the promotion target's
    head (``main``) to judge everything that would land, which is the set the
    merge actually moves. Omitted, it judges the single commit against its
    parent.
    """
    findings: List[Finding] = []
    try:
        rc, _ = _git(["cat-file", "-e", f"{sha}^{{commit}}"], repo_root)
        if rc != 0:
            return (Finding("exists", False, f"{sha[:12]} is not a commit here"),)
        findings.append(_check_provenance(sha, repo_root, branch, base=base))
        findings.append(_check_structure(sha, repo_root, base=base))
        findings.append(_check_semantic_delta(sha, repo_root, base=base))
        findings.append(await _check_coverage(
            sha, repo_root, python_bin=python_bin, timeout_s=test_timeout_s,
            base=base,
        ))
    except Exception as exc:  # noqa: BLE001 — an unfinished check is a refusal
        findings.append(Finding("gate", False, f"verification aborted: {exc!r}"[:200]))
    return tuple(findings)


async def _quarantine(
    sha: str, branch: str, repo_root: Path, reason: str,
) -> str:
    """Pin the conflicting state under a debug ref and return it.

    A NEW ref pointing at the branch tip — never a move, never a delete. The
    accumulation branch keeps its name and position; this only makes the
    conflicting state addressable after the fact, so a later session can find
    it without reconstructing which branch was involved.
    """
    ref = f"{_quarantine_prefix()}/{branch.rsplit('/', 1)[-1]}-{reason}"[:200]
    rc, existing = _git(["rev-parse", "--verify", "--quiet", ref], repo_root)
    if rc == 0:
        pinned = (existing or "").strip()
        rc2, want = _git(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], repo_root)
        want = (want or "").strip() if rc2 == 0 else ""
        if pinned and want and pinned != want:
            # A COLLISION, not a repeat: this name already describes a
            # different failure. Overwriting it would erase one forensic
            # record to write another, and both matter. Fail closed, keep the
            # existing pin, and say which ref is contested.
            raise QuarantineIsolationFault(
                "ref_collision",
                f"{ref} already pins {pinned[:12]}, refusing to repoint it "
                f"at {want[:12]}",
                ref=ref,
            )
        return ref                       # same state, already pinned
    rc, _ = _git(["branch", ref, sha], repo_root)
    if rc != 0:
        raise QuarantineIsolationFault(
            "ref_write_failed", f"git refused to create {ref}", ref=ref,
        )
    logger.warning("[PromotionGate] conflicting state pinned at %s", ref)
    return ref


def gc_stale_worktrees(repo_root: Path) -> Tuple[int, Tuple[str, ...]]:
    """Drop worktree administrative entries whose directories are gone.

    Soaks leave these behind — an abandoned session's worktree, a repair
    sandbox under /tmp that the OS cleared. Each one keeps a branch checked
    out, and a branch that git believes is checked out somewhere cannot be
    checked out here: the first attempt to work on the promoted branch failed
    with exactly that ("already used by worktree at ...").

    ``git worktree prune`` is the right tool and the safe one: it removes only
    entries whose working directory no longer exists, so it cannot discard a
    live tree or any uncommitted work inside one. Run AFTER forensic
    extraction, never before — the quarantine ref is what makes the state
    survivable, and this only cleans up bookkeeping.

    Returns ``(pruned_count, names)``. NEVER raises.
    """
    try:
        rc, before = _git(["worktree", "list", "--porcelain"], repo_root)
        if rc != 0:
            return 0, ()
        prunable: List[str] = []
        current = ""
        for line in before.splitlines():
            if line.startswith("worktree "):
                current = line.split(" ", 1)[1].strip()
            # `prunable` carries git's REASON on the same line ("gitdir file
            # points to non-existent location"), so this is a prefix test, not
            # an equality one.
            elif line.startswith("prunable") and current:
                prunable.append(current)
        if not prunable:
            return 0, ()
        rc, _ = _git(["worktree", "prune"], repo_root)
        if rc != 0:
            logger.warning("[PromotionGate] worktree prune failed")
            return 0, ()
        logger.info(
            "[PromotionGate] pruned %d stale worktree entr(ies): %s",
            len(prunable), ", ".join(p.rsplit("/", 1)[-1] for p in prunable[:4]),
        )
        return len(prunable), tuple(prunable)
    except Exception:  # noqa: BLE001
        logger.debug("[PromotionGate] worktree gc degraded", exc_info=True)
        return 0, ()


async def promote_accumulation_commit(
    *, sha: str, branch: str, repo_root: Path, target_root: Optional[Path] = None,
    manager: Any = None, python_bin: str = "python3",
    test_timeout_s: float = 300.0, record_lesson: Any = None,
    base: str = "",
) -> PromotionVerdict:
    """Verify, then delegate the merge. NEVER raises — faults become verdicts.

    ``manager`` is the :class:`WorktreeManager` that owns every git mutation;
    it is injected so this module has no git-writing surface of its own and so
    tests can drive the refusal paths without a repository. ``record_lesson``
    is the LessonMemory seam, injected for the same reason.
    """
    repo_root = Path(repo_root)
    target = Path(target_root) if target_root is not None else repo_root
    if not gate_enabled():
        return PromotionVerdict(False, "gate_disabled", (sha,), detail=_ENV_ENABLED)

    findings = await verify_commit(
        sha, repo_root=repo_root, branch=branch,
        python_bin=python_bin, test_timeout_s=test_timeout_s, base=base,
    )
    verdict_state = "verified"
    blocking = [f for f in findings if not f.passed and f.blocking]
    if blocking:
        detail = "; ".join(f.detail for f in blocking)[:400]
        if any(f.check == "semantic_delta" for f in blocking):
            detail = f"{TRIVIAL_CHANGE_VIOLATION}: {detail}"
        logger.warning(
            "[PromotionGate] %s REFUSED — %s. The accumulation branch is "
            "untouched and remains the reviewable artifact.", sha[:12], detail,
        )
        await _record(
            record_lesson, sha=sha, files=_touched_files(sha, repo_root),
            failure_class="promotion_refused", detail=detail,
        )
        return PromotionVerdict(
            False, "refused", (sha,), findings=findings, detail=detail,
        )

    if manager is None:
        try:
            from backend.core.ouroboros.governance.worktree_manager import (  # noqa: PLC0415
                WorktreeManager,
            )
            manager = WorktreeManager(repo_root)
        except Exception as exc:  # noqa: BLE001
            return PromotionVerdict(
                False, "no_mechanism", (sha,), findings=findings,
                detail=f"{exc!r}"[:200],
            )

    # The whole range, oldest first. Handing the mechanism only the tip makes
    # its fast-forward precondition ("the branch tip is exactly the last
    # promoted sha") unsatisfiable for a multi-commit branch, so it falls back
    # to cherry-picking a commit whose parent is not on the target — which
    # fails, correctly, and looks like a conflict when it is really a
    # mis-specified promotion. Promoting a BRANCH means promoting what is on
    # it.
    shas: List[str] = [sha]
    if base:
        rc, out = _git(["rev-list", "--reverse", f"{base}..{sha}"], repo_root)
        if rc == 0 and out.strip():
            shas = [s.strip() for s in out.splitlines() if s.strip()]
    try:
        result = await manager.promote_commits(
            target_root=str(target), branch=branch, commit_shas=shas,
        )
    except Exception as exc:  # noqa: BLE001
        reason = getattr(exc, "reason", "") or type(exc).__name__
        # A conflict is not a policy refusal: the tree moved under us. The
        # mechanism has already restored the target byte-identical, so the
        # only thing left is to make the state addressable and say so.
        try:
            ref = await _quarantine(sha, branch, repo_root, reason=str(reason)[:40])
        except QuarantineIsolationFault as iso:
            # The record could not be written. main is still untouched — that
            # is the mechanism's guarantee, not this function's — so the
            # verdict stands and says BOTH things went wrong, rather than
            # silently reporting a conflict whose evidence was never pinned.
            logger.error(
                "[PromotionGate] %s CONFLICT and its quarantine could not be "
                "written: %s", sha[:12], iso,
            )
            await _record(
                record_lesson, sha=sha, files=_touched_files(sha, repo_root),
                failure_class="quarantine_isolation",
                detail=f"{reason}; quarantine failed: {iso}",
            )
            return PromotionVerdict(
                False, "quarantine_isolation_fault", (sha,), findings=findings,
                detail=f"{reason}; {iso}"[:300],
            )
        # Bookkeeping only, and only now that the state is pinned: a stale
        # entry keeps a branch "checked out" somewhere that no longer exists,
        # which is what blocked the first attempt to work on this very branch.
        gc_stale_worktrees(repo_root)
        detail = f"{reason}: {exc}"[:300]
        logger.warning("[PromotionGate] %s CONFLICT — %s", sha[:12], detail)
        await _record(
            record_lesson, sha=sha, files=_touched_files(sha, repo_root),
            failure_class="promotion_conflict", detail=detail,
        )
        return PromotionVerdict(
            False, "conflict_quarantined", (sha,), findings=findings,
            detail=PromotionConflictFault(
                str(reason), detail, quarantine_ref=ref,
            ).args[0],
        )

    landed = tuple(getattr(result, "landed_shas", ()) or ())
    logger.info(
        "[PromotionGate] %s promoted onto %s (%s)",
        sha[:12], target, getattr(result, "strategy", "?"),
    )
    return PromotionVerdict(
        True, "promoted", (sha,), landed_shas=landed, findings=findings,
        detail=str(getattr(result, "strategy", "")),
    )


async def _record(
    seam: Any, *, sha: str, files: Sequence[str], failure_class: str, detail: str,
) -> None:
    """Route a refusal into LessonMemory. Fail-soft: never blocks a verdict.

    Only refusals and faults are recorded. LessonMemory is failure-mode memory
    that feeds few-shot injection — writing successes into it would degrade the
    corpus it exists to be.
    """
    try:
        if seam is None:
            from backend.core.ouroboros.governance.lesson_memory import (  # noqa: PLC0415
                record_lesson as seam,
            )
        await seam(
            op_id=f"promote-{sha[:12]}", target_files=list(files),
            phase="PROMOTE", failure_class=failure_class,
            error_text=detail, summary=f"promotion refused: {detail}"[:200],
            error_class=failure_class,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[PromotionGate] lesson record degraded", exc_info=True)

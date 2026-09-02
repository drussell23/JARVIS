#!/usr/bin/env python3
"""Stage work orders for a DPO farming soak — OPERATOR utility, never autonomous.

## Why this does not sign anything

The obvious way to get ops past `self_modification_unsanctioned_source` is
to mint HMAC-signed roadmap goals on demand. `strategy_signer`'s own module
docstring forbids exactly that, by name:

    This exists so the OPERATOR can deliberately attest authorship of a set
    of goals — it is NOT, and must never be, wired into any boot path. A
    roadmap the organism signs over its own goals would be a false
    authenticity claim and the self-authorization anti-pattern the cage
    forbids (operator = zero-order doll, §41.2).

The signature's entire meaning is "a human decided this". Software that
generates the goal AND the attestation produces cryptographically valid
signatures over an authorization nobody granted — a bypass with extra
steps, which is the one thing the farming effort must not become, since
the corpus it produces is what the model is then trained on.

## Why it does not need to

`RiskEngine._SELF_MOD_SENTINELS_BASE` scopes the cage to
`ouroboros/governance/` plus `ouroboros/{daemon,vital_scan,spinal_cord,
rem_sleep,rem_epoch}`; `_KERNEL_SENTINELS_BASE` adds `unified_supervisor`
and `_SECURITY_SENTINELS_BASE` adds `auth/`, `credential`, `secret`,
`token`, `.env`. **A target outside all of those is not self-modification
and requires no authorization at all** — the existing `.jarvis/roadmap.yaml`
says as much in its own note, having deliberately chosen an in-cage target
precisely so it *would* exercise the mechanism.

Farming does not want to exercise the cage. It wants ops that reach
VALIDATE so per-candidate verdicts exist to differentiate siblings. Ordinary
work on ordinary files does that, with the cage fully armed and untouched.

So this tool refuses, structurally, to name a sentinel path — it cannot be
repurposed into a cage bypass even by an operator in a hurry. For genuine
in-cage work, `--governance-target` writes an UNSIGNED roadmap doc and
prints the command for the operator to sign it themselves. It never signs.

## Format traps this encodes (each cost a soak to learn)

* `WorkOrderSensor` reads only the TAIL (`JARVIS_WORK_ORDER_RECENT_N`,
  default 3) of an append-only log — so items go at the BOTTOM.
* Every backticked token is a path candidate. Backtick ONLY the target.
* Prose containing the literal `NEXT:` is parsed as a work item.
* `.jarvis/work_order_seen.json` suppresses re-emission across sessions;
  a re-run needs it cleared or the same order will never fire again.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO = Path(__file__).resolve().parents[1]
PROGRESS = REPO / ".superpowers" / "sdd" / "progress.md"
SEEN = REPO / ".jarvis" / "work_order_seen.json"
ROADMAP_DRAFT = REPO / ".jarvis" / "roadmap.draft.yaml"

#: Re-derived from the live RiskEngine when importable, so this tool cannot
#: drift from the cage it is refusing to touch. The literals are the
#: fallback for a bare checkout, never a second opinion.
_FALLBACK_SENTINELS: Tuple[str, ...] = (
    "ouroboros/governance/", "ouroboros/daemon", "ouroboros/vital_scan",
    "ouroboros/spinal_cord", "ouroboros/rem_sleep", "ouroboros/rem_epoch",
    "unified_supervisor", "auth/", "credential", "secret", "token", ".env",
)


def live_sentinels() -> Tuple[str, ...]:
    """The cage's own path list, read from the engine that enforces it."""
    try:
        sys.path.insert(0, str(REPO))
        from backend.core.ouroboros.governance.risk_engine import RiskEngine
        eng = RiskEngine()
        return tuple(
            eng._self_mod_sentinels()
            + eng._kernel_sentinels()
            + eng._security_sentinels()
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not read live sentinels ({exc}); using fallback")
        return _FALLBACK_SENTINELS


def is_caged(rel_path: str, sentinels: Tuple[str, ...]) -> str:
    """Return the sentinel this path trips, or "" if it trips none."""
    p = rel_path.replace("\\", "/")
    for s in sentinels:
        if s in p:
            return s
    return ""


#: Ordinary, real improvements on small modules well outside every sentinel.
#: Deliberately DOC/TYPE-only: a farming soak wants many ops reaching
#: VALIDATE, not risky diffs. Each names its target ONCE, in backticks.
#: Phrases whose deliverable is INVISIBLE to the value gate.
#:
#: `CandidateValueGate` proves a change cosmetic by comparing `ast.dump()`
#: after `epistemic_shedder._DocstringStripper` -- so comments, formatting
#: and DOCSTRINGS never reach the comparison. A docstring-only patch is
#: therefore cosmetic BY CONSTRUCTION: the op completes as benign
#: `no_op_cosmetic`, which `classify_terminal_reason` maps to
#: ('unknown', 'intent_written', should_train=False).
#:
#: The first farming batch was six documentation tasks. It ran, it cleared
#: the cage, four of seven ops reached APPLY -- and produced 48
#: `no_op_cosmetic` rows out of 74, none trainable. The batch was chosen to
#: avoid the governance cage and accidentally chose the one deliverable the
#: value gate exists to discard. Refused structurally, like a sentinel
#: path, so the mistake cannot be repeated by someone in a hurry.
_COSMETIC_MARKERS: Tuple[str, ...] = (
    "docs only",
    "documentation only",
    "docstrings only",
    "comment only",
    "comments only",
    "change no executable line",
    "change no executable logic",
    "no executable change",
)


def assert_produces_executable_change(target: str, task: str) -> None:
    """Refuse a task whose deliverable cannot survive the value gate.

    Raises rather than warns: farming exists to produce TRAINABLE outcomes,
    and a task that can only yield `no_op_cosmetic` produces none. A warning
    in a staging script is read once and ignored forever.
    """
    low = (task or "").lower()
    hit = next((m for m in _COSMETIC_MARKERS if m in low), "")
    if hit:
        raise ValueError(
            "refusing to stage a cosmetic task for {!r}: contains {!r}. "
            "Docstrings are stripped before the AST comparison that decides "
            "no_op_cosmetic, so this can only terminate as "
            "('unknown', should_train=False) and yields no training signal. "
            "State a change to executable behaviour instead.".format(
                target, hit)
        )


#: Task-text signals for DESIGN FREEDOM -- work that admits more than one
#: correct implementation (a lookup table, a branch chain, a recursive
#: walk, an iterative one). Measured on soak bt-2026-09-02-003459: the
#: canonical tasks (re-raise an exception, swap `datetime.now()` for the
#: tz-aware form) collapsed to ONE structure across three draws at
#: temperatures 0.2/0.70/0.95, while the free-form tasks (a per-type
#: strategy table, a type guard with recursion + list handling, an
#: ok/error flag) drew 2-3 structurally distinct candidates. Sampling
#: cannot manufacture variance a task does not admit, so the batch should
#: LEAD with the work that can pair.
_FREEDOM_SIGNALS: Tuple[str, ...] = (
    "strategy", "table", "mapping", "depend on", "recurs", "depth",
    "bounded", "join", "collect", "handle list", "each element", "guard",
    "distinguish", "structurally", "ladder", "backoff", "retry", "per-",
    "policy", "fallback", "flag", "algorithm", "iterat", "walk",
)
_CANONICAL_SIGNALS: Tuple[str, ...] = (
    "re-raise", "reraise", "timezone", "datetime.now", "import timezone",
    "rename", "log the exception", "exc_info", "lift the hardcoded",
    "named module-level constants", "unused import",
)


def _branch_density(rel: str) -> float:
    """Decision points per definition in the target file, or 0.0.

    A file whose functions already branch a lot leaves more room for a
    different branching structure than a file of straight-line handlers.
    Read from disk with `ast`; never raises."""
    import ast  # noqa: PLC0415

    try:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return 0.0
    defs = [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not defs:
        return 0.0
    branches = sum(
        1 for n in ast.walk(tree)
        if isinstance(n, (ast.If, ast.For, ast.While, ast.Try, ast.With,
                          ast.BoolOp, ast.IfExp))
    )
    return branches / len(defs)


def design_freedom_score(rel: str, task: str) -> float:
    """How many correct shapes this task admits, as a sortable number.

    Task text decides most of it (+1 per freedom signal, -1 per canonical
    signal); the target's branch density adds a fraction so that, between
    two similarly worded tasks, the one on the more algorithmic file leads.
    Deterministic and explainable -- the dry run prints it beside each
    order so an operator can see WHY the batch is in this order."""
    low = (task or "").lower()
    score = 0.0
    score += sum(1.0 for s in _FREEDOM_SIGNALS if s in low)
    score -= sum(1.0 for s in _CANONICAL_SIGNALS if s in low)
    score += min(1.0, _branch_density(rel) / 4.0)
    return round(score, 3)


#: Real, small, self-contained defects on NON-cage modules. Each names a
#: concrete behavioural change, so the patch alters the AST and the op can
#: reach a trainable verdict. Grounded by reading the files -- every item
#: below is a defect that exists in the tree today, not invented busywork.
TASKS: List[Tuple[str, str]] = [
    ("backend/api/monitoring_endpoint.py",
     "Fix the error-code bug in control_monitoring: the "
     "HTTPException(status_code=400) raised for an invalid action is thrown "
     "INSIDE the try block, and HTTPException subclasses Exception, so the "
     "broad except catches it and re-raises it as a 500. A client sending an "
     "invalid action receives Internal Server Error instead of Bad Request. "
     "Re-raise HTTPException unchanged before the generic handler runs."),
    ("backend/api/model_status_api.py",
     "Replace every timezone-naive datetime.now() with "
     "datetime.now(timezone.utc) and import timezone, so emitted timestamps "
     "are unambiguous. Naive timestamps in an API response cannot be "
     "compared across hosts."),
    ("backend/api/audio_error_fallback.py",
     "Lift the hardcoded fallback policy (delay_ms 1000, max_retries 3) into "
     "named module-level constants so the retry contract has one definition, "
     "and make the fallback timestamp timezone-aware."),
    ("backend/api/display_routes.py",
     "The broad except handlers return a success-shaped payload carrying an "
     "error string, so a caller cannot distinguish success from failure "
     "without string-matching the body. Give the failure path an explicit "
     "ok/error flag so the two are structurally distinguishable."),
    ("backend/api/sse_contract.py",
     "Make the broad except blocks observable: log the exception with "
     "exc_info before degrading, so a silently-swallowed SSE fault leaves "
     "evidence. Keep behaviour otherwise identical."),
    ("backend/api/clean_vision_response.py",
     "The cleaning steps assume their input is a str; a None or non-str "
     "value raises deep inside instead of being rejected at the boundary. "
     "Add an explicit type guard at the public entry point that returns the "
     "empty result for unusable input."),
    # --- Sibling-entropy harvest batch (2026-09-01). Each of these admits
    # MORE THAN ONE correct implementation (a lookup table, a branch chain,
    # a recursive walk, an iterative one), which is what a GRPO group needs:
    # siblings that differ in STRUCTURE, not in docstring wording.
    ("backend/api/clean_vision_response.py",
     "clean_vision_response recurses into nested dicts with no depth guard, "
     "so a self-referential or deeply nested payload recurses until "
     "RecursionError, and a list payload (e.g. a list of text fragments) is "
     "str()-ified into a Python literal. Add a bounded recursion depth and "
     "handle list inputs by cleaning each element and joining the non-empty "
     "text parts, returning the existing formatting-issue fallback when the "
     "bound is exceeded."),
    ("backend/api/audio_error_fallback.py",
     "handle_audio_error returns one fixed fallback_strategy (retry, 1000ms, "
     "3 attempts) for EVERY error_type, so a not-allowed permission error and "
     "an aborted error are told to retry the same way a transient network "
     "error is. Make the fallback strategy depend on error_type: network "
     "retries with backoff, no-speech retries once with no delay, aborted "
     "and not-allowed do not retry and name the alternative, and an unknown "
     "type gets a conservative single retry. Keep the existing suggestions."),
    ("backend/api/sse_contract.py",
     "eventstream_frame_to_jarviskit reads only the FIRST data: line of a "
     "frame, but the SSE grammar allows a payload to span several data: "
     "lines that the consumer joins with a newline before parsing. A "
     "multi-line frame therefore fails json.loads and is dropped as "
     "unparseable. Collect every data: line in the frame in order and join "
     "them with a newline before decoding, leaving single-line frames "
     "byte-identical in behaviour."),
]


def build_orders(n: int, sentinels: Tuple[str, ...]) -> List[str]:
    out: List[str] = []
    # Most design freedom FIRST. WorkOrderSensor reads the tail of
    # progress.md and the pool is FIFO, so batch order is dispatch order:
    # the work most likely to pair should reach a worker first.
    ranked = sorted(
        TASKS, key=lambda t: design_freedom_score(t[0], t[1]), reverse=True,
    )
    for rel, instruction in ranked[:n]:
        # Both refusals are structural and both run BEFORE anything is
        # written: a cage trip means the work needs an operator signature
        # this tool must never mint, and a cosmetic task means the work
        # cannot produce a trainable outcome however cleanly it runs.
        assert_produces_executable_change(rel, instruction)
        trip = is_caged(rel, sentinels)
        if trip:
            raise SystemExit(
                f"REFUSED: target {rel!r} trips the cage sentinel {trip!r}.\n"
                "This tool provisions UNAUTHORIZED-BY-DESIGN work only. "
                "In-cage work needs an operator signature: use "
                "--governance-target."
            )
        if not (REPO / rel).is_file():
            print(f"  ! skipping {rel} (not present in this checkout)")
            continue
        # ONE backticked token, and no literal "NEXT:" inside the prose.
        print(f"  [freedom {design_freedom_score(rel, instruction):+.2f}] {rel}")
        out.append(f"NEXT: {instruction} Target file: `{rel}`")
    return out


def append_orders(orders: List[str], *, apply: bool) -> None:
    text = PROGRESS.read_text(encoding="utf-8") if PROGRESS.exists() else "## Queue\n"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    block = (
        f"\n<!-- DPO farming batch staged {stamp} by scripts/"
        "provision_farming_work.py — ordinary work on non-cage targets; "
        "no authorization was minted for these. -->\n\n"
        + "\n\n".join(orders) + "\n"
    )
    if not apply:
        print("\n--- would append to progress.md (BOTTOM = the tail the sensor reads) ---")
        print(block)
        return
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROGRESS, PROGRESS.with_suffix(".md.bak")) if PROGRESS.exists() else None
    PROGRESS.write_text(text.rstrip("\n") + "\n" + block, encoding="utf-8")
    print(f"  appended {len(orders)} order(s) to {PROGRESS.relative_to(REPO)}")


def clear_seen(*, apply: bool) -> None:
    if not SEEN.exists():
        print("  dedup ledger absent — nothing to clear")
        return
    if not apply:
        print(f"  would clear {SEEN.relative_to(REPO)} ({SEEN.read_text()[:80]})")
        return
    shutil.copy2(SEEN, SEEN.with_suffix(".json.bak"))
    SEEN.write_text("[]", encoding="utf-8")
    print(f"  cleared {SEEN.relative_to(REPO)} (backup .json.bak)")


def write_unsigned_roadmap(target: str, sentinels: Tuple[str, ...]) -> None:
    """Emit a roadmap goal for IN-CAGE work, UNSIGNED, for the operator.

    Deliberately stops one step short of authorization. Signing here is the
    self-authorization anti-pattern; signing is the operator's act.
    """
    trip = is_caged(target, sentinels)
    if not trip:
        print(f"  note: {target} trips no sentinel — it needs no roadmap at all.")
    doc: Dict[str, Any] = {
        "version": 1,
        "operator_id": os.environ.get("USER", "operator") + "-REVIEW-REQUIRED",
        "source": "operator_directed_agent_signed",
        "authority": "operator_directed",
        "signed": False,
        "note": (
            "DRAFT staged by scripts/provision_farming_work.py. It is "
            "UNSIGNED on purpose: strategy_signer forbids the organism "
            "signing its own goals (§41.2). Review the goal, then sign it "
            "yourself."
        ),
        "goals": [{
            "id": "farming-draft-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M"),
            "title": f"REVIEW AND EDIT before signing — draft goal for {target}",
            "description": "REPLACE THIS with the change you are authorizing.",
            "priority": "low",
            "success_criteria": "REPLACE THIS with what proves the change correct.",
            "depends_on": [],
            "target_files": [target],
            "max_duration_s": 1800,
        }],
    }
    try:
        import yaml  # type: ignore
        ROADMAP_DRAFT.write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        ROADMAP_DRAFT.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"\n  wrote UNSIGNED draft -> {ROADMAP_DRAFT.relative_to(REPO)}")
    print("\n  OPERATOR ACTION (this tool will not do it for you):")
    print("    1. edit the draft — the placeholders are deliberate")
    print("    2. review that target_files is what you intend to authorize")
    print("    3. sign it with YOUR existing secret, then move it into place:")
    print("       python3 -m backend.core.ouroboros.governance.strategy_signer \\")
    print(f"         {ROADMAP_DRAFT.relative_to(REPO)} \"$JARVIS_ROADMAP_READER_HMAC_SECRET\"")
    print("    (omit the secret and the CLI mints a NEW one, invalidating every")
    print("     signature you already issued.)")


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default is a dry run)")
    ap.add_argument("--count", type=int, default=len(TASKS),
                    help="how many work orders to stage")
    ap.add_argument("--keep-seen", action="store_true",
                    help="do NOT clear the dedup ledger")
    ap.add_argument("--governance-target", metavar="PATH",
                    help="emit an UNSIGNED roadmap draft for in-cage work")
    args = ap.parse_args(argv)

    sentinels = live_sentinels()
    print(f"cage sentinels in force ({len(sentinels)}): {', '.join(sentinels)}")

    if args.governance_target:
        write_unsigned_roadmap(args.governance_target, sentinels)
        return 0

    orders = build_orders(max(1, args.count), sentinels)
    if not orders:
        print("no usable targets in this checkout")
        return 1
    print(f"\nstaging {len(orders)} work order(s), all outside every sentinel:")
    for o in orders:
        print(f"  - {o[6:90]}...")
    append_orders(orders, apply=args.apply)
    if not args.keep_seen:
        clear_seen(apply=args.apply)
    print("\n" + ("done." if args.apply else "DRY RUN — re-run with --apply"))
    print("Note: WorkOrderSensor reads only the TAIL "
          "(JARVIS_WORK_ORDER_RECENT_N, default 3). Raise it to emit more "
          "than the last 3 of these.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

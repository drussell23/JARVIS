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
TASKS: List[Tuple[str, str]] = [
    ("backend/api/monitoring_endpoint.py",
     "Add a module-level docstring and complete type hints to every public "
     "function. Explain what the endpoint reports and who consumes it. "
     "DOCS AND TYPE HINTS ONLY — change no executable logic, add no imports "
     "beyond typing, touch no tests."),
    ("backend/api/sse_contract.py",
     "Document the SSE contract: add a module docstring stating the event "
     "shape and the ordering guarantees callers may rely on, and give every "
     "public function a docstring naming its failure mode. DOCS ONLY — "
     "change no executable line."),
    ("backend/api/clean_vision_response.py",
     "Add docstrings explaining what each cleaning step removes and why, "
     "plus type hints on the public surface. DOCS AND TYPE HINTS ONLY."),
    ("backend/api/audio_error_fallback.py",
     "Document the fallback ladder: which failure each rung handles and what "
     "the caller sees when every rung is exhausted. DOCS ONLY."),
    ("backend/api/model_status_api.py",
     "Add a module docstring and per-function docstrings describing the "
     "status fields returned and their staleness semantics. DOCS ONLY."),
    ("backend/api/display_routes.py",
     "Add type hints and docstrings to the public route handlers, naming the "
     "error each returns. DOCS AND TYPE HINTS ONLY."),
]


def build_orders(n: int, sentinels: Tuple[str, ...]) -> List[str]:
    out: List[str] = []
    for rel, instruction in TASKS[:n]:
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

#!/usr/bin/env python3
"""Empirical-closure verdict for the Pass B Graduation arc (Tier 3 #7).

Proves the graduation pattern landed cleanly across all 8 Pass B
modules + the centralized FlagRegistry seeds + the cost-contract
cage cross-file pin.

Five primary contracts (all in-process, no soak required):

  Contract 1 — All 12 Pass B FlagSpec entries seeded in
              flag_registry_seed (8 master flags + 4 path/cage knobs).
  Contract 2 — All 8 Pass B modules expose register_shipped_invariants
              + each substrate AST pin holds against current source.
  Contract 3 — 6 read-only / observational / operator-surface flags
              default-true (manifest, risk_class, ast_validator,
              shadow_pipeline, review_queue, repl_dispatcher).
  Contract 4 — 2 write-path flags STAY default-false
              (META_PHASE_RUNNER + REPLAY_EXECUTOR) — cost-contract
              preservation; graduation deferred to operator-paced
              3-clean-session arcs per W2(5) policy.
  Contract 5 — amendment_requires_operator() locked-true via the
              cross-file cage pin in order2_review_queue.

Exit codes:
    0 = all five primary contracts PASSED
    1 = at least one primary contract FAILED
"""
from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


_PASS_B_FLAG_NAMES = (
    "JARVIS_ORDER2_MANIFEST_LOADED",
    "JARVIS_ORDER2_MANIFEST_PATH",
    "JARVIS_ORDER2_RISK_CLASS_ENABLED",
    "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED",
    "JARVIS_SHADOW_PIPELINE_ENABLED",
    "JARVIS_SHADOW_REPLAY_CORPUS_PATH",
    "JARVIS_META_PHASE_RUNNER_ENABLED",
    "JARVIS_REPLAY_EXECUTOR_ENABLED",
    "JARVIS_ORDER2_REVIEW_QUEUE_ENABLED",
    "JARVIS_ORDER2_REVIEW_QUEUE_PATH",
    "JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR",
    "JARVIS_ORDER2_REPL_ENABLED",
)
_PASS_B_MODULES = (
    "order2_manifest", "order2_classifier", "ast_phase_runner_validator",
    "shadow_replay", "meta_phase_runner", "replay_executor",
    "order2_review_queue", "order2_repl_dispatcher",
)
_FLIP_TARGETS = (
    "order2_manifest:is_loaded",
    "order2_classifier:is_enabled",
    "ast_phase_runner_validator:is_enabled",
    "shadow_replay:is_enabled",
    "order2_review_queue:is_enabled",
    "order2_repl_dispatcher:is_enabled",
)
_KEEP_FALSE_TARGETS = (
    "meta_phase_runner:is_enabled",
    "replay_executor:is_enabled",
)


@dataclass(frozen=True)
class ContractVerdict:
    name: str
    passed: bool
    evidence: str
    details: Dict[str, object] = field(default_factory=dict)


def _eval_seeds() -> ContractVerdict:
    from backend.core.ouroboros.governance.flag_registry_seed import (
        SEED_SPECS,
    )
    seeded = {s.name for s in SEED_SPECS}
    missing = [n for n in _PASS_B_FLAG_NAMES if n not in seeded]
    return ContractVerdict(
        name="C1 All 12 Pass B FlagSpec entries seeded",
        passed=not missing,
        evidence=(
            f"seeded={len(_PASS_B_FLAG_NAMES) - len(missing)}/"
            f"{len(_PASS_B_FLAG_NAMES)}"
            + (f" missing={missing}" if missing else "")
        ),
    )


def _eval_invariants() -> ContractVerdict:
    failures: List[str] = []
    pin_status: List[str] = []
    for module_name in _PASS_B_MODULES:
        try:
            mod = __import__(
                f"backend.core.ouroboros.governance.meta.{module_name}",
                fromlist=[module_name],
            )
            invariants = mod.register_shipped_invariants()
            if not invariants:
                failures.append(
                    f"{module_name}: register_shipped_invariants "
                    "returned empty list"
                )
                continue
            for inv in invariants:
                target_path = REPO_ROOT / inv.target_file
                source = target_path.read_text(encoding="utf-8")
                tree = ast.parse(source)
                violations = inv.validate(tree, source)
                if violations:
                    failures.append(
                        f"{inv.invariant_name}: {violations[:3]}"
                    )
                else:
                    pin_status.append(inv.invariant_name)
        except Exception as exc:
            failures.append(
                f"{module_name}: invariant check raised {exc!r}"
            )
    return ContractVerdict(
        name="C2 All 8 register_shipped_invariants pins hold",
        passed=not failures,
        evidence=(
            f"pins={len(pin_status)} "
            f"({', '.join(pin_status[:3])}"
            + ("..." if len(pin_status) > 3 else "") + ")"
            + (f" failures={failures}" if failures else "")
        ),
    )


def _eval_flips_default_true() -> ContractVerdict:
    failures: List[str] = []
    flip_status: List[str] = []
    for target in _FLIP_TARGETS:
        module_name, fn_name = target.split(":")
        # Clear the env var so the default path is exercised.
        env_var = (
            "JARVIS_ORDER2_MANIFEST_LOADED"
            if module_name == "order2_manifest"
            else None
        )
        # Map module to its env var directly.
        env_map = {
            "order2_manifest": "JARVIS_ORDER2_MANIFEST_LOADED",
            "order2_classifier": "JARVIS_ORDER2_RISK_CLASS_ENABLED",
            "ast_phase_runner_validator":
                "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED",
            "shadow_replay": "JARVIS_SHADOW_PIPELINE_ENABLED",
            "order2_review_queue":
                "JARVIS_ORDER2_REVIEW_QUEUE_ENABLED",
            "order2_repl_dispatcher": "JARVIS_ORDER2_REPL_ENABLED",
        }
        env_var = env_map[module_name]
        os.environ.pop(env_var, None)
        try:
            mod = __import__(
                f"backend.core.ouroboros.governance.meta.{module_name}",
                fromlist=[module_name],
            )
            fn = getattr(mod, fn_name)
            result = fn()
            if result is True:
                flip_status.append(f"{target}=True")
            else:
                failures.append(f"{target} returned {result!r}")
        except Exception as exc:
            failures.append(f"{target} raised {exc!r}")
    return ContractVerdict(
        name="C3 Six read-only/operator flags default-true",
        passed=not failures,
        evidence=(
            f"flipped=[{', '.join(flip_status)}]"
            + (f" failures={failures}" if failures else "")
        ),
    )


def _eval_keep_false_default() -> ContractVerdict:
    failures: List[str] = []
    keep_status: List[str] = []
    env_map = {
        "meta_phase_runner": "JARVIS_META_PHASE_RUNNER_ENABLED",
        "replay_executor": "JARVIS_REPLAY_EXECUTOR_ENABLED",
    }
    for target in _KEEP_FALSE_TARGETS:
        module_name, fn_name = target.split(":")
        os.environ.pop(env_map[module_name], None)
        try:
            mod = __import__(
                f"backend.core.ouroboros.governance.meta.{module_name}",
                fromlist=[module_name],
            )
            fn = getattr(mod, fn_name)
            result = fn()
            if result is False:
                keep_status.append(f"{target}=False (cage)")
            else:
                failures.append(
                    f"{target} returned {result!r} -- "
                    "cost-contract violated; write-path graduated "
                    "without empirical soak validation"
                )
        except Exception as exc:
            failures.append(f"{target} raised {exc!r}")
    return ContractVerdict(
        name="C4 Two write-path flags STAY default-false (cage)",
        passed=not failures,
        evidence=(
            f"kept_false=[{', '.join(keep_status)}]"
            + (f" failures={failures}" if failures else "")
        ),
    )


def _eval_cage_locked() -> ContractVerdict:
    """C5 — amendment_requires_operator() returns True regardless of
    the env var value (the cage is structurally locked-true)."""
    from backend.core.ouroboros.governance.meta.order2_review_queue import (  # noqa: E501
        amendment_requires_operator,
    )
    cases: List[str] = []
    failures: List[str] = []
    env_var = "JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR"
    for env_value in (None, "false", "0", "no", "off", "garbage", "true"):
        if env_value is None:
            os.environ.pop(env_var, None)
            label = "<unset>"
        else:
            os.environ[env_var] = env_value
            label = env_value
        result = amendment_requires_operator()
        if result is True:
            cases.append(f"env={label}->True")
        else:
            failures.append(
                f"cage broken: env={label} returned {result!r}"
            )
    os.environ.pop(env_var, None)
    return ContractVerdict(
        name="C5 amendment_requires_operator() locked-true cage",
        passed=not failures,
        evidence=(
            f"cases=[{', '.join(cases)}]"
            + (f" failures={failures}" if failures else "")
        ),
    )


def main() -> int:
    print("Empirical-closure verdict for Pass B Graduation arc")
    print(f"  repo_root: {REPO_ROOT}")
    print()
    primary = [
        _eval_seeds(),
        _eval_invariants(),
        _eval_flips_default_true(),
        _eval_keep_false_default(),
        _eval_cage_locked(),
    ]
    for v in primary:
        mark = "PASS" if v.passed else "FAIL"
        print(f"  [{mark}] {v.name}")
        print(f"         {v.evidence}")
    print()
    if all(v.passed for v in primary):
        print("VERDICT: Pass B Graduation arc EMPIRICALLY CLOSED -- "
              "all five primary contracts PASSED.")
        return 0
    print("VERDICT: at least one primary contract FAILED -- "
          "Pass B Graduation arc not yet empirically closed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

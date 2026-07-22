"""Supply-chain & diff-entropy SemanticGuardian patterns (2026-07-22).

Root cause of the promoted lockfile-gutting (soak bt-2026-07-22-054947):
the risk plane evaluated file IDENTITY, not diff PAYLOAD. A
``requirements.txt`` op that stripped 76 pinned dependencies rated
``SAFE_AUTO`` and fast-forwarded onto the operator tree because nothing
weighed the semantic destruction the diff carried.

Two content-driven detectors close that class — no path allowlist, no
regex file-typing, no hardcoding:

1. ``dependency_pin_weakened`` — the Lockfile Pinning Invariant. A
   strict differential parse (``packaging.requirements``) of old vs new
   dependency content: any package whose exact pin (``==``) is REMOVED
   or LOOSENED (to ``>=`` / ``~=`` / ``>`` / unpinned) revokes
   ``SAFE_AUTO``. Adding a pin, or any purely-additive change, stays
   silent. Applicability reuses Gate 3's ``is_dependency_file``.

2. ``high_entropy_gutting`` — the Semantic Diff Entropy Cap. A
   file-agnostic "Destruction Ratio" over the in-process line diff
   (``difflib.SequenceMatcher``): NET line shrinkage beyond
   ``JARVIS_SEMGUARD_ENTROPY_MAX_DESTRUCTION`` (default 0.20) of the
   original length is the gutting signature — distinct from a balanced
   refactor (deletions ≈ insertions), which never fires. A minimum
   original-length floor avoids noise on tiny files.

Both are ``soft`` findings → ``recommend_tier_floor`` yields
``notify_apply``: the op is stripped of silent auto-apply and surfaced
for operator eyes (stricter-wins with any hard finding). Additive,
per-pattern kill switches, identical ``inspect()``/``Detection``
contract, fail-soft import so a load error can never disable the
existing guardian.
"""

from __future__ import annotations

import difflib
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- knobs


def _entropy_max_destruction() -> float:
    """``JARVIS_SEMGUARD_ENTROPY_MAX_DESTRUCTION`` — default 0.20. Net
    line-shrinkage ratio above which the gutting signature fires."""
    try:
        v = float(os.environ.get(
            "JARVIS_SEMGUARD_ENTROPY_MAX_DESTRUCTION", "0.20",
        ))
        return v if 0.0 < v < 1.0 else 0.20
    except (TypeError, ValueError):
        return 0.20


def _entropy_min_lines() -> int:
    """``JARVIS_SEMGUARD_ENTROPY_MIN_LINES`` — default 12. Files shorter
    than this never trip the entropy cap (a 3-line file losing 1 line is
    not a gutting)."""
    try:
        return max(1, int(os.environ.get(
            "JARVIS_SEMGUARD_ENTROPY_MIN_LINES", "12",
        )))
    except (TypeError, ValueError):
        return 12


# --------------------------------------------------------------------------- helpers


def _candidate_new_content(new_content: Any) -> str:
    return new_content if isinstance(new_content, str) else ""


def _parse_pins(content: str) -> "Dict[str, Optional[str]]":
    """``{canonical_name: exact_pin_version_or_None}`` for a requirements
    body, via ``packaging`` (strict differential parse — mandate 2).

    A name maps to its ``==`` version when the specifier is a single
    exact pin, else ``None`` (loose / ranged / unpinned). Lines that
    don't parse as a Requirement (``-r``, ``--hashes``, urls, markers-
    only) are skipped — they carry no pin to weaken. NEVER raises.
    """
    out: Dict[str, Optional[str]] = {}
    try:
        from packaging.requirements import Requirement
    except Exception:  # noqa: BLE001 — packaging absent → no invariant
        return out
    for raw in content.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            req = Requirement(line)
        except Exception:  # noqa: BLE001 — unparseable line, skip
            continue
        name = req.name.lower().replace("_", "-").replace(".", "-")
        exact: Optional[str] = None
        specs = list(req.specifier)
        if len(specs) == 1 and specs[0].operator == "==":
            exact = specs[0].version
        out[name] = exact
    return out


def _pat_dependency_pin_weakened(
    *, file_path: str, old_content: Any, new_content: Any,
) -> Optional[Any]:
    """Lockfile Pinning Invariant. Fires when an exact ``==`` pin is
    removed or loosened between old → new dependency content."""
    from backend.core.ouroboros.governance.semantic_guardian import (
        Detection,
    )
    try:
        from backend.core.ouroboros.governance.dependency_file_gate import (
            is_dependency_file,
        )
    except Exception:  # noqa: BLE001
        return None
    if not is_dependency_file(file_path or ""):
        return None
    old = old_content if isinstance(old_content, str) else ""
    new = _candidate_new_content(new_content)
    if not old:  # brand-new file — nothing to weaken
        return None

    old_pins = _parse_pins(old)
    new_pins = _parse_pins(new)
    weakened: List[str] = []
    for name, old_ver in old_pins.items():
        if old_ver is None:
            continue  # wasn't pinned — cannot be weakened
        if name not in new_pins:
            weakened.append(f"{name} (=={old_ver} → removed)")
        elif new_pins[name] is None:
            weakened.append(f"{name} (=={old_ver} → unpinned/loosened)")
        elif new_pins[name] != old_ver:
            # A pin CHANGE (==A → ==B) is a version bump, not a
            # weakening — still a supply-chain edit worth surfacing,
            # but the invariant here is specifically pin STRENGTH, so
            # a same-strength bump does not fire (keeps benign bumps
            # SAFE_AUTO per mandate edge-case 1's spirit).
            continue
    if not weakened:
        return None
    _shown = ", ".join(weakened[:5])
    _more = f" (+{len(weakened) - 5} more)" if len(weakened) > 5 else ""
    return Detection(
        pattern="dependency_pin_weakened",
        severity="soft",
        message=(
            f"Lockfile pinning invariant: {len(weakened)} exact pin(s) "
            f"removed or loosened [{_shown}{_more}] — revoking SAFE_AUTO, "
            f"NOTIFY_APPLY floor pending human validation"
        ),
        file_path=file_path,
        lines=(),
        snippet=_shown[:200],
    )


def _pat_high_entropy_gutting(
    *, file_path: str, old_content: Any, new_content: Any,
) -> Optional[Any]:
    """Semantic Diff Entropy Cap. Fires when the NET line shrinkage of
    the old → new diff exceeds the destruction-ratio threshold — the
    file-agnostic gutting signature (distinct from a balanced rewrite)."""
    from backend.core.ouroboros.governance.semantic_guardian import (
        Detection,
    )
    old = old_content if isinstance(old_content, str) else ""
    new = _candidate_new_content(new_content)
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    n_old = len(old_lines)
    if n_old < _entropy_min_lines():
        return None

    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    deletions = 0
    insertions = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            deletions += (i2 - i1)
        elif tag == "insert":
            insertions += (j2 - j1)
        elif tag == "replace":
            deletions += (i2 - i1)
            insertions += (j2 - j1)
    # NET shrinkage — a balanced refactor (deletions ≈ insertions) has a
    # near-zero net ratio and never fires; only true mass-removal does.
    net_removed = max(0, deletions - insertions)
    destruction_ratio = net_removed / max(1, n_old)
    threshold = _entropy_max_destruction()
    if destruction_ratio <= threshold:
        return None
    return Detection(
        pattern="high_entropy_gutting",
        severity="soft",
        message=(
            f"Diff entropy cap: net removal of {net_removed}/{n_old} lines "
            f"({destruction_ratio:.0%} > {threshold:.0%} destruction "
            f"ratio) — high-entropy gutting signature; revoking "
            f"SAFE_AUTO, NOTIFY_APPLY floor"
        ),
        file_path=file_path,
        lines=(),
        snippet="",
    )


#: Registered by semantic_guardian via the same additive hook as the
#: blindspot detectors (name -> detector). Per-pattern kill switches
#: (JARVIS_SEMGUARD_<NAME>_ENABLED) apply automatically.
SUPPLYCHAIN_PATTERNS: "Dict[str, Any]" = {
    "dependency_pin_weakened": _pat_dependency_pin_weakened,
    "high_entropy_gutting": _pat_high_entropy_gutting,
}


__all__ = ["SUPPLYCHAIN_PATTERNS"]

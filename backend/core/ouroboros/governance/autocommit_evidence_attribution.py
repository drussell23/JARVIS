"""Which soak does a commit belong to — by IDENTITY, not by clock.

THE ONE ROOT CAUSE BEHIND BOTH EDGE CASES
-----------------------------------------
``auto_commit_graduation_gate`` attributes a commit to a clean soak by
asking whether the commit's timestamp falls inside that soak's window.
Windows are built from consecutive ``SessionRecord.recorded_at_epoch``
values, and commit times come from ``%ct`` — the committer's clock. Two
distinct failures fall out of that single decision:

**Clock skew.** The Body runs on the Mac and the Engine on Windows WSL2.
A commit authored on one and a ledger row written on the other are stamped
by different clocks; a few seconds of drift moves a commit across a window
boundary and the evidence attaches to the wrong soak — or to none. The
verification is *deterministic* in the sense that it always computes the
same answer, and *unreliable* in the sense that the answer depends on two
machines agreeing about the time.

**Squash merges.** Squashing a PR replaces N commits with one. The SHAs the
window would have matched no longer exist on the branch. A window-based
reader sees zero Yellow-tier commits and concludes the soak produced no
evidence — which is indistinguishable, in the report, from a soak that
genuinely never fired AutoCommitter. One of those should block graduation
and the other should not.

So the fix is not two special cases. It is to stop inferring membership
from time when the commit can simply *say* which soak it belongs to.

ATTRIBUTION BY TRAILER, PROVENANCE ALWAYS STATED
------------------------------------------------
``auto_committer`` stamps a ``Session:`` trailer. A commit therefore
carries its own provenance, and matching it is an exact string comparison
against ``SessionRecord.session_id`` — no clocks on either side, correct
across worktrees, and unchanged by a squash that concatenates bodies
(which is what ``git merge --squash`` and GitHub's squash button both do
by default).

History predating that trailer has no anchor, so the window remains — but
it is reported as :attr:`Provenance.TIME_WINDOW`, an INFERENCE, never as
proof. This is the same discipline ``operation_advisor`` already applies to
blast radius (``measured`` / ``localized_lower_bound`` / ``unknown``): a
number that cannot be proven says so rather than borrowing the authority of
one that can.

THREE STATES, BECAUSE TWO FORCE A LIE
-------------------------------------
The gate counted soaks as *with evidence* or *missing evidence*. A soak
whose commits were squashed away belongs to neither: the evidence is not
absent, it is unreadable. Forcing it into "missing" resets the operator's
hard-won soak count for something they did by following normal PR hygiene.

``UNVERIFIABLE`` is the third state. It never counts as evidence — a
graduation gate must not pass on something it could not read — and it never
counts as a failure either. It is reported, named, and left for the operator
to resolve by re-soaking or by pointing the gate at the pre-squash ref.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.AutoCommitAttribution")

ATTRIBUTION_SCHEMA_VERSION: str = "autocommit_evidence_attribution.1"

#: The trailer `auto_committer` writes. Named ONCE and consumed from here by
#: both the writer and this reader, so the two cannot drift into disagreement
#: about the spelling of the anchor the whole verification rests on.
SESSION_TRAILER_KEY: str = "Session"

_SESSION_TRAILER_RE = re.compile(
    rf"^{SESSION_TRAILER_KEY}:[ \t]*(?P<sid>\S+)[ \t]*$",
    re.MULTILINE,
)

_ENV_REFLOG_ENABLED = "JARVIS_AUTOCOMMIT_GRAD_REFLOG_TRACE"
_ENV_REFLOG_MAX = "JARVIS_AUTOCOMMIT_GRAD_REFLOG_MAX"


def session_trailer_line(session_id: str) -> str:
    """The trailer to embed in a commit body. Empty when unknown.

    Empty rather than a placeholder: a trailer reading ``Session: unknown``
    would be matched by nothing and would look, to a later reader, like a
    session that no longer exists. An absent anchor must degrade to the
    window, not to a false one.
    """
    sid = (session_id or "").strip()
    return f"{SESSION_TRAILER_KEY}: {sid}" if sid else ""


def extract_session_id(body: str) -> str:
    """The soak this commit declares it belongs to, or ``""``."""
    match = _SESSION_TRAILER_RE.search(body or "")
    return match.group("sid") if match else ""


def reflog_trace_enabled() -> bool:
    """Default TRUE. Reads reflog; mutates nothing."""
    raw = os.environ.get(_ENV_REFLOG_ENABLED)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def reflog_max_entries() -> int:
    try:
        return max(1, int(os.environ.get(_ENV_REFLOG_MAX, "") or 2000))
    except (TypeError, ValueError):
        return 2000


class Provenance(str, enum.Enum):
    """How a soak's evidence was established. Closed 5-value taxonomy."""

    #: Exact `Session:` trailer match. Clock-independent, squash-durable.
    SESSION_TRAILER = "session_trailer"
    #: Commit timestamp fell inside the soak's window. An INFERENCE — two
    #: clocks must agree for it to be right.
    TIME_WINDOW = "time_window"
    #: Not on the branch, but recovered from the reflog.
    SQUASH_RECOVERED = "squash_recovered"
    #: The soak's commits are gone and the reflog does not hold them.
    #: Not evidence, and NOT a failure.
    SQUASH_LOST = "squash_lost"
    #: No anchor and no commits. A soak that genuinely produced nothing.
    ABSENT = "absent"

    @property
    def is_proof(self) -> bool:
        """Only an identity match proves membership."""
        return self in (Provenance.SESSION_TRAILER,
                        Provenance.SQUASH_RECOVERED)

    @property
    def is_unverifiable(self) -> bool:
        """Neither evidence nor failure — the third state."""
        return self is Provenance.SQUASH_LOST


#: Operator-facing reason text. On the surface, never a generic "locked".
PROVENANCE_REASON: Dict[Provenance, str] = {
    Provenance.SESSION_TRAILER:
        "verified by commit identity (Session trailer)",
    Provenance.TIME_WINDOW:
        "inferred from commit timestamps — no Session trailer on these "
        "commits, so this is not clock-independent proof",
    Provenance.SQUASH_RECOVERED:
        "recovered from the reflog after a squash merge",
    Provenance.SQUASH_LOST:
        "verification lost via squash — the O+V signatures were compacted "
        "away and the reflog no longer holds the originals",
    Provenance.ABSENT:
        "no O+V commit carrying Risk: NOTIFY_APPLY in this soak",
}


@dataclass(frozen=True)
class Attribution:
    """One soak's evidence, and how honestly it was established."""

    session_id: str
    provenance: Provenance
    yellow_hashes: Tuple[str, ...] = ()
    other_hashes: Tuple[str, ...] = ()

    @property
    def yellow_count(self) -> int:
        return len(self.yellow_hashes)

    @property
    def counts_as_evidence(self) -> bool:
        """A gate must never pass on something it could not read."""
        return bool(self.yellow_hashes) and not self.provenance.is_unverifiable

    @property
    def reason(self) -> str:
        return PROVENANCE_REASON.get(self.provenance, "")

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": ATTRIBUTION_SCHEMA_VERSION,
            "session_id": self.session_id,
            "provenance": self.provenance.value,
            "yellow_hashes": list(self.yellow_hashes),
            "other_hashes": list(self.other_hashes),
            "yellow_count": self.yellow_count,
            "counts_as_evidence": self.counts_as_evidence,
            "reason": self.reason,
        }


def attribute(
    session_id: str,
    commits: Sequence[Tuple[str, float, str]],
    *,
    window: Optional[Tuple[float, float]],
    classify,
) -> Attribution:
    """Attribute ``commits`` to one soak. Pure. NEVER raises.

    ``classify`` is the gate's own :func:`classify_commit_body` bound with
    its markers — injected rather than imported so this module holds no
    opinion about what an O+V commit looks like. The gate owns that
    definition; this owns membership.

    IDENTITY FIRST, ALWAYS. If any commit in the whole log declares this
    session, the trailer decides membership entirely and the window is not
    consulted — mixing them would let a skewed timestamp pull in a commit
    that another soak has explicitly claimed.
    """
    from backend.core.ouroboros.governance.auto_commit_graduation_gate import (
        CommitEvidenceKind,
    )

    claimed = [(h, b) for (h, _e, b) in commits
               if extract_session_id(b) == session_id and session_id]
    if claimed:
        yellow = tuple(h for h, b in claimed
                       if classify(b) is CommitEvidenceKind.YELLOW_TIER)
        other = tuple(h for h, b in claimed
                      if classify(b) is CommitEvidenceKind.OTHER_TIER)
        return Attribution(session_id, Provenance.SESSION_TRAILER,
                           yellow, other)

    if window is None:
        return Attribution(session_id, Provenance.ABSENT)

    start, end = window
    # A commit that DECLARES an owner is never available to a window
    # inference — not even to a soak with no claimed commits of its own.
    # Skew moves timestamps across boundaries; a trailer is the commit's own
    # statement about where it belongs, and an inference must never overrule
    # a declaration. Without this, soak A could be credited with evidence
    # that soak B had explicitly claimed, which is the theft the identity
    # path exists to prevent.
    inside = [(h, b) for (h, e, b) in commits
              if start <= e < end and not extract_session_id(b)]
    yellow = tuple(h for h, b in inside
                   if classify(b) is CommitEvidenceKind.YELLOW_TIER)
    other = tuple(h for h, b in inside
                  if classify(b) is CommitEvidenceKind.OTHER_TIER)
    if not yellow and not other:
        return Attribution(session_id, Provenance.ABSENT)
    return Attribution(session_id, Provenance.TIME_WINDOW, yellow, other)


async def read_reflog(*, timeout_s: int) -> Optional[List[Tuple[str, float, str]]]:
    """``[(hash, epoch, body), …]`` for commits only the reflog still holds.

    THE SQUASH RECOVERY PATH. A squash merge rewrites history: the original
    commits leave the branch but stay reachable through the reflog until it
    expires. Reading it turns "the evidence is gone" into "the evidence is
    here, one indirection away" for the window in which that is still true.

    ``git log -g`` — read-only, never ``shell=True``, argv list, bounded by
    both a timeout and an entry cap. Returns ``None`` when the reflog is
    unavailable (a fresh clone has none), which is a degrade rather than an
    error: a repository without a reflog has not lost anything, it simply
    cannot corroborate.
    """
    if not reflog_trace_enabled():
        return None
    from backend.core.ouroboros.governance.auto_commit_graduation_gate import (
        _FLD_SEP, _REC_SEP,
    )

    fmt = f"%H{_FLD_SEP}%ct{_FLD_SEP}%B{_REC_SEP}"
    args = ["git", "log", "-g", "--all",
            f"--max-count={reflog_max_entries()}",
            f"--format={fmt}", "--no-color"]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(),
                                        timeout=float(timeout_s))
    except asyncio.CancelledError:
        if proc is not None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        raise
    except Exception:  # noqa: BLE001 — git missing, timeout, exec failure
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
        logger.debug("[AutoCommitAttribution] reflog read failed",
                     exc_info=True)
        return None
    if proc.returncode != 0:
        return None

    records: List[Tuple[str, float, str]] = []
    for raw in out.decode("utf-8", errors="replace").split(_REC_SEP):
        raw = raw.strip("\n")
        if not raw:
            continue
        parts = raw.split(_FLD_SEP)
        if len(parts) < 3:
            continue
        try:
            records.append((parts[0].strip(), float(parts[1].strip()),
                            parts[2]))
        except (TypeError, ValueError):
            continue
    return records


def resolve_squash(
    attribution: Attribution,
    reflog: Optional[Sequence[Tuple[str, float, str]]],
    branch: Sequence[Tuple[str, float, str]],
    *,
    window: Optional[Tuple[float, float]],
    classify,
) -> Attribution:
    """Second chance for a soak the branch has lost. Pure. NEVER raises.

    Only ``ABSENT`` soaks are re-examined: a soak proven by trailer needs
    nothing, and one attributed by window has its commits on the branch.

    Three outcomes, and the third is claimed only on POSITIVE evidence:

    ``SQUASH_RECOVERED`` — the reflog still holds O+V commits for this soak.
    The original objects exist and carry their own signatures, so this is
    proof, not inference.

    ``SQUASH_LOST`` — the reflog proves that commits in this soak's window
    were rewritten off the branch, but none of the survivors carry
    recoverable evidence. Claimed ONLY on that positive proof of a rewrite.

    ``ABSENT`` (unchanged) — no rewrite is evident, so the honest reading is
    that the soak produced nothing. Also the answer when the reflog could not
    be read at all: "we could not look" must never be recorded as "we looked
    and it was gone". Those are different claims and only one of them is
    about the operator's history.

    The asymmetry is deliberate. ``SQUASH_LOST`` protects the operator's soak
    count, so inventing it from an absence would let any quiet soak launder
    itself into an excused one. It has to be earned by a rewrite we can see.
    """
    if attribution.provenance is not Provenance.ABSENT:
        return attribution
    if reflog is None:
        return attribution

    recovered = attribute(attribution.session_id, reflog, window=window,
                          classify=classify)
    if recovered.yellow_hashes or recovered.other_hashes:
        return Attribution(attribution.session_id,
                           Provenance.SQUASH_RECOVERED,
                           recovered.yellow_hashes, recovered.other_hashes)

    if window is None:
        return attribution
    start, end = window
    on_branch = {h for (h, e, _b) in branch if start <= e < end}
    rewritten = {h for (h, e, _b) in reflog
                 if start <= e < end and h not in on_branch}
    if rewritten:
        # Commits existed in this window and are no longer reachable from
        # the branch. Something rewrote them; their evidence, if any, is
        # unreadable rather than absent.
        return Attribution(attribution.session_id, Provenance.SQUASH_LOST)
    return attribution


__all__ = [
    "ATTRIBUTION_SCHEMA_VERSION",
    "PROVENANCE_REASON",
    "SESSION_TRAILER_KEY",
    "Attribution",
    "Provenance",
    "attribute",
    "extract_session_id",
    "read_reflog",
    "reflog_trace_enabled",
    "resolve_squash",
    "session_trailer_line",
]

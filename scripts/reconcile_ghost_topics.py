#!/usr/bin/env python3
"""Reconcile untracked ghost topic copies against the canonical corpus.

A sync client living over ``~/Documents`` littered ` 2`-suffixed conflict
copies inside ``docs/memory_topics/``. `memory_corpus` already stops them
reaching a GENERATE prompt; this decides what, if anything, they still hold
that the tracked corpus does not — and only then removes them.

Why this is not a delete script
-------------------------------
"Untracked and gitignored" is a statement about git, not about content. A
conflict copy can be the ONLY surviving record of an edit that was never
committed, and a blind ``rm`` cannot tell that case from a stale byte-copy.
So every ghost is classified with evidence and nothing is removed until its
content is provably present in the canonical file.

Classification, strongest claim first
--------------------------------------
``IDENTICAL``
    Normalised payload hash matches the canonical twin. Nothing to lose.

``STALE_SUBSET``
    Bodies match and the ghost's declared ``modules:`` are a SUBSET of the
    canonical's. The ghost is an older snapshot of the same document taken
    before a frontmatter enrichment pass — strictly poorer, never richer.
    Merging it would inject a stale ``modules: []`` over a populated one,
    which is the failure mode a naive "reclaim everything" loop produces.

``DIVERGED``
    The body differs, or the ghost declares a module the canonical does not.
    Genuine unmerged content. Body lines absent from the canonical are
    appended under ``## Reclaimed Context``; ghost-only modules are unioned
    into the canonical frontmatter. Nothing is overwritten.

``ORPHANED``
    No canonical twin. Promoted to the tracked path.

``CONFLICT``
    Something the classifier cannot decide (unreadable file, a promotion
    target that already exists). Reported and left on disk — the one outcome
    that must never be silently resolved.

Dry-run by default. ``--apply`` performs writes; ``--purge-dirs`` also
removes ghost directories once they hold nothing but reconciled files.
"""
from __future__ import annotations

import argparse
import enum
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# DRY: the parsing, hashing, and size-capped read all already exist and are
# the ones the router itself uses. A second implementation here would be a
# second definition of what a topic IS — the exact defect this arc fixed.
from backend.core.ouroboros.governance.module_routing import (  # noqa: E402
    _hash_content,
    _parse_modules_frontmatter,
)
from backend.core.ouroboros.governance.memory_corpus import (  # noqa: E402
    read_topic_text,
)

GHOST_SUFFIXES: Tuple[str, ...] = (" 2", " 3", " 4", " 5")


class Bucket(str, enum.Enum):
    IDENTICAL = "IDENTICAL"
    STALE_SUBSET = "STALE_SUBSET"
    DIVERGED = "DIVERGED"
    ORPHANED = "ORPHANED"
    CONFLICT = "CONFLICT"


@dataclass
class Verdict:
    ghost: Path
    canonical: Optional[Path]
    bucket: Bucket
    detail: str = ""
    body_lines_to_reclaim: List[str] = field(default_factory=list)
    modules_to_union: List[str] = field(default_factory=list)
    #: Ghost-only ``modules:`` entries that do NOT resolve to a regular file
    #: and are therefore excluded from the union. NAMED in the report rather
    #: than dropped silently — "zero data loss" is a claim about what the
    #: operator can still see, not only about what got written.
    unresolvable: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def split_frontmatter(text: str) -> Tuple[str, str]:
    """``(frontmatter_block, body)``. Frontmatter is ``""`` when absent.

    Deliberately structural rather than a YAML parse: the point is to compare
    BODIES independently of the metadata, and a parser would have to succeed
    on both sides to let that comparison happen at all.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return "", text
    for i in range(1, min(len(lines), 60)):
        if lines[i].strip() == "---":
            return "\n".join(lines[: i + 1]), "\n".join(lines[i + 1:])
    return "", text


def canonical_for(ghost: Path) -> Path:
    """The tracked path this ghost is a copy of.

    Handles a ghost SEGMENT anywhere in the path (``memory 2/x.md``) and a
    ghost FILENAME (``x 2.md``), because sync clients produce both.
    """
    parts: List[str] = []
    for seg in ghost.parts:
        stem, dot, ext = seg.rpartition(".")
        if dot and any(stem.endswith(s) for s in GHOST_SUFFIXES):
            parts.append(f"{stem[:-2]}{dot}{ext}")
            continue
        parts.append(next((seg[:-2] for s in GHOST_SUFFIXES
                           if seg.endswith(s)), seg))
    return Path(*parts)


def is_ghost(path: Path, tracked: set) -> bool:
    return str(path) not in tracked and any(
        seg.endswith(s) or seg.rpartition(".")[0].endswith(s)
        for seg in path.parts for s in GHOST_SUFFIXES
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _norm_body(body: str) -> List[str]:
    """Body lines with transport artefacts normalised away.

    Same normalisation ``_hash_content`` applies, for the same reason: CRLF
    and trailing whitespace are not authorship, and treating them as content
    would classify a sync artefact as reclaimable material.
    """
    return [ln.rstrip() for ln in
            body.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def classify(ghost: Path, tracked: set, root: Path) -> Verdict:
    """One ghost's verdict. NEVER raises — an error is a CONFLICT, not a crash.

    *ghost* is repo-relative, matching the key space ``git ls-files`` returns.
    Mixing absolute paths with a relative tracked-set makes every twin lookup
    miss, which fails closed to CONFLICT rather than deleting — but it also
    reconciles nothing, so the two spaces are kept explicitly separate here
    and joined only via *root*.
    """
    try:
        canonical = canonical_for(ghost)
        g_text = read_topic_text(root / ghost)
        if not g_text:
            return Verdict(ghost, None, Bucket.CONFLICT, "ghost unreadable/empty")

        if str(canonical) not in tracked:
            if (root / canonical).exists():
                return Verdict(ghost, canonical, Bucket.CONFLICT,
                               "twin exists on disk but is not tracked")
            return Verdict(ghost, canonical, Bucket.ORPHANED,
                           "no canonical twin — unique document")

        c_text = read_topic_text(root / canonical)
        if _hash_content(g_text) == _hash_content(c_text):
            return Verdict(ghost, canonical, Bucket.IDENTICAL,
                           "payload hashes match")

        _g_fm, g_body = split_frontmatter(g_text)
        _c_fm, c_body = split_frontmatter(c_text)

        g_mods = set(_parse_modules_frontmatter(g_text))
        c_mods = set(_parse_modules_frontmatter(c_text))

        # A ghost-only entry is only reclaimable if it could ever BE a routing
        # signal. `_structural_score` matches on path-tail against an op's
        # target files, so an entry naming a directory (`backend/voice/`) or a
        # path that does not exist (`tests/REPL`) can never match anything —
        # while a bare directory's tail (`voice`, `api`) CAN collide with an
        # unrelated real file and score a spurious 1.0.
        #
        # Resolving-to-a-regular-file is the structural test for that, and it
        # needs no extension list to maintain. Entries failing it are NAMED,
        # not dropped: the operator can re-add any by hand.
        extra_all = sorted(g_mods - c_mods)
        extra_mods = [m for m in extra_all if (root / m).is_file()]
        unresolvable = [m for m in extra_all if m not in extra_mods]

        g_lines = _norm_body(g_body)
        c_seen = set(_norm_body(c_body))
        unique_body = [ln for ln in g_lines if ln.strip() and ln not in c_seen]

        if not unique_body and not extra_mods:
            # Bodies agree and the ghost's reclaimable metadata is a subset:
            # an older snapshot, strictly poorer. Reclaiming it would
            # OVERWRITE richer frontmatter with poorer — the reclaim loop
            # actively destroying the thing it was written to protect.
            return Verdict(
                ghost, canonical, Bucket.STALE_SUBSET,
                f"body identical; modules ⊆ canonical "
                f"({len(g_mods)} ⊆ {len(c_mods)})",
                unresolvable=unresolvable,
            )

        return Verdict(
            ghost, canonical, Bucket.DIVERGED,
            f"{len(unique_body)} body line(s), {len(extra_mods)} module(s) "
            f"not in canonical",
            body_lines_to_reclaim=unique_body,
            modules_to_union=extra_mods,
            unresolvable=unresolvable,
        )
    except Exception as exc:  # noqa: BLE001
        return Verdict(ghost, None, Bucket.CONFLICT,
                       f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

RECLAIM_HEADING = "## Reclaimed Context"


def reclaim(verdict: Verdict, root: Path, *, apply: bool) -> str:
    """Fold a DIVERGED ghost's unique content into its canonical file.

    Appends rather than merges in place: an automated writer that edited
    existing prose would be rewriting authored memory on a heuristic. A
    clearly-fenced appendix is recoverable by a human; an in-place rewrite is
    not.
    """
    if verdict.canonical is None:
        return "no canonical target"
    canonical = root / verdict.canonical
    text = read_topic_text(canonical)
    fm, body = split_frontmatter(text)

    if verdict.modules_to_union and fm:
        existing = _parse_modules_frontmatter(text)
        merged = existing + [m for m in verdict.modules_to_union
                             if m not in existing]
        out_fm: List[str] = []
        for line in fm.split("\n"):
            if line.strip().startswith("modules:"):
                out_fm.append("modules: [" + ", ".join(merged) + "]")
            else:
                out_fm.append(line)
        fm = "\n".join(out_fm)

    if verdict.body_lines_to_reclaim:
        if RECLAIM_HEADING not in body:
            body = body.rstrip() + f"\n\n{RECLAIM_HEADING}\n"
        body = body.rstrip() + "\n\n" + "\n".join(
            verdict.body_lines_to_reclaim) + "\n"

    if apply:
        canonical.write_text((fm + "\n" + body) if fm else body,
                             encoding="utf-8")
    return (f"+{len(verdict.body_lines_to_reclaim)} line(s), "
            f"+{len(verdict.modules_to_union)} module(s)")


def promote(verdict: Verdict, root: Path, *, apply: bool) -> str:
    """Move an ORPHANED ghost to its canonical (tracked) path."""
    if verdict.canonical is None:
        return "no target path"
    target = root / verdict.canonical
    if target.exists():
        return "target exists — left in place"
    if apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root / verdict.ghost), str(target))
    return f"→ {target}"


def purge_empty_dirs(root: Path, *, apply: bool) -> List[Path]:
    """Remove ghost dirs that hold nothing at all.

    Emptiness is checked against EVERY entry, not just ``*.md``. A ghost
    directory carrying an unrelated file would otherwise be removed along
    with it — a reconciliation pass deleting something it never classified.
    """
    removed: List[Path] = []
    for path in sorted(root.rglob("*"), key=lambda p: -len(p.parts)):
        if not path.is_dir():
            continue
        if not any(path.name.endswith(s) for s in GHOST_SUFFIXES):
            continue
        if any(path.iterdir()):
            continue
        removed.append(path)
        if apply:
            path.rmdir()
    return removed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def tracked_set(root: Path) -> set:
    out = subprocess.run(
        ["git", "ls-files", "--", "docs/memory_topics"],
        cwd=str(root), capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        raise SystemExit("git ls-files failed — refusing to run without the "
                         "authority that defines the canonical corpus")
    return {p for p in out.stdout.split("\n") if p.endswith(".md")}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="perform writes (default: dry run)")
    ap.add_argument("--purge-dirs", action="store_true",
                    help="also remove ghost directories left empty")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    topics = root / "docs" / "memory_topics"
    tracked = tracked_set(root)

    ghosts = sorted(p.relative_to(root) for p in topics.rglob("*.md"))
    ghosts = [g for g in ghosts if is_ghost(g, tracked)]
    verdicts = [classify(g, tracked, root) for g in ghosts]

    buckets: Dict[Bucket, List[Verdict]] = {b: [] for b in Bucket}
    for v in verdicts:
        buckets[v.bucket].append(v)

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"\n  Ghost topic reconciliation — {mode}")
    print(f"  corpus: {len(tracked)} tracked · {len(ghosts)} ghost file(s)\n")
    print(f"  {'bucket':<14} {'count':>6}   action")
    print(f"  {'-'*14} {'-'*6}   {'-'*44}")
    actions = {
        Bucket.IDENTICAL: "purge (content provably present)",
        Bucket.STALE_SUBSET: "purge (older snapshot, strictly poorer)",
        Bucket.DIVERGED: "merge unique content into canonical",
        Bucket.ORPHANED: "promote to tracked path",
        Bucket.CONFLICT: "LEFT ON DISK — needs a human",
    }
    for b in Bucket:
        print(f"  {b.value:<14} {len(buckets[b]):>6}   {actions[b]}")
    print()

    for v in buckets[Bucket.DIVERGED]:
        print(f"  merge  {v.ghost}\n         {reclaim(v, root, apply=args.apply)}"
              f"  [{v.detail}]")
    for v in buckets[Bucket.ORPHANED]:
        print(f"  promote {v.ghost}  {promote(v, root, apply=args.apply)}")
    for v in buckets[Bucket.CONFLICT]:
        print(f"  CONFLICT {v.ghost}  — {v.detail}")

    # Named, never merely counted. A reclamation pass that says "27 entries
    # excluded" has lost them as surely as deleting them would.
    excluded = [(v.ghost, m) for v in verdicts for m in v.unresolvable]
    if excluded:
        print(f"\n  ghost-only module entries EXCLUDED from the union "
              f"({len(excluded)}) — not a regular file, so they cannot be a "
              f"routing signal:")
        for ghost, m in excluded:
            print(f"    {m}   [{ghost.name}]")

    purgeable = buckets[Bucket.IDENTICAL] + buckets[Bucket.STALE_SUBSET]
    if args.verbose:
        for v in purgeable[:20]:
            print(f"  purge  {v.ghost}  [{v.detail}]")
        if len(purgeable) > 20:
            print(f"  … and {len(purgeable) - 20} more")

    # Purge LAST: a DIVERGED ghost is only removable after its content has
    # actually landed in the canonical file, and in a dry run nothing landed.
    if args.apply:
        for v in purgeable + buckets[Bucket.DIVERGED]:
            try:
                (root / v.ghost).unlink()
            except Exception as exc:  # noqa: BLE001
                print(f"  ! could not remove {v.ghost}: {exc}")

    if args.purge_dirs:
        removed = purge_empty_dirs(topics, apply=args.apply)
        print(f"\n  ghost directories {'removed' if args.apply else 'removable'}"
              f": {len(removed)}")
        for d in removed:
            print(f"    {d}")

    outcome = ("wrote changes" if args.apply
               else "no changes written — rerun with --apply")
    print(f"\n  {outcome}\n")
    return 1 if buckets[Bucket.CONFLICT] else 0


if __name__ == "__main__":
    raise SystemExit(main())

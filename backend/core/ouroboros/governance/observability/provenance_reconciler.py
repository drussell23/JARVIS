"""Retroactive provenance repair for the trajectory corpus.

## What went wrong

`record_generation`'s main call site passed no ``model_id_override``, so the
recorder fell back to ``traj.model_id`` — the NOMINAL brain-catalog slot.
``brain_selector._DEFAULT_POLICY`` still carries legacy GCP ids
(``qwen-2.5-coder-7b``) and has no entry for the locally-served 30B, so
generations produced by ``qwen3-coder-ov:30b`` were filed under a 7B.

Measured: 480 of 2,892 rows — 16.6% of the corpus — across 34 sessions.

The corpus keys DPO preference pairs on ``model_id``. A pair built across two
models that were in fact ONE model is not a preference; it is noise presented
as signal, and it is worse than a missing row because nothing downstream can
detect it.

## Why this needs evidence, not a rule

The obvious repair — "rewrite every ``qwen-2.5-coder-7b`` row as the 30B" — is
wrong, and provably so on this host: ``qwen2.5-coder:7b`` IS installed in
Ollama, so a row bearing that name may be perfectly correct. Rewriting those
would create the same corruption in the opposite direction.

So each row is adjudicated against its own session, and the adjudication uses
an INDEPENDENT signal. The obvious source — ``[PrimeProvider] Generated ...
model=`` — is produced by ``reported_model_name``, which carries the very
fallback under investigation; using it would launder the bug into its own
evidence. ``[ModelPhysics] <name>: native_context=...`` instead reports what the
ENDPOINT said about the model it is serving (architecture, layer count, kv
heads), which cannot be echoed from the brain catalog.

A session is repaired only when ModelPhysics named exactly ONE model there and
that model is not the one the rows claim. Anything else — no log, no
ModelPhysics line, several models named — is left untouched and reported.

## Safety

Read-only by default (``dry_run=True``). A real run writes a timestamped
backup of every file it touches before modifying it, rewrites atomically via a
temp file and ``os.replace``, and is idempotent: a second run finds nothing to
do. Rows are matched by ``(session_id, model_id)``; every other field is
preserved byte-for-byte, including the ``event_id``, so a downstream consumer
that keyed on one still resolves.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ProvenanceReconciler")

__all__ = [
    "ReconcileReport",
    "adjudicate_sessions",
    "reconcile_corpus",
    "reconciler_enabled",
]

_ENV_ENABLED = "JARVIS_PROVENANCE_RECONCILER_ENABLED"

#: Reads the model the ENDPOINT described — independent of the brain catalog.
_PHYSICS_RE = re.compile(r"ModelPhysics\] ([A-Za-z0-9._:\-]+): native_context=")


def reconciler_enabled() -> bool:
    """Default OFF. A boot-time pass that REWRITES the training corpus is not
    something to arm by default; the operator runs it deliberately, reads the
    dry-run report, and only then lets it write."""
    return os.environ.get(_ENV_ENABLED, "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


@dataclass
class ReconcileReport:
    """What a pass found and what it changed."""

    scanned_rows: int = 0
    verified_rows: int = 0
    migrated_rows: int = 0
    unverifiable_rows: int = 0
    files_rewritten: int = 0
    backups: List[str] = field(default_factory=list)
    migrations: Dict[str, int] = field(default_factory=dict)
    unverifiable_reasons: Dict[str, int] = field(default_factory=dict)
    dry_run: bool = True

    def render(self) -> str:
        """Three outcomes, named honestly.

        An earlier draft reported every row it could not adjudicate as
        "skipped", which read as 1,069 problems on a corpus that had 484. A
        row nobody can verify is not a row known to be wrong, and a repair
        tool that inflates its own findings is not one an operator can act on.
        """
        head = "DRY RUN — nothing written" if self.dry_run else "APPLIED"
        lines = [
            f"[ProvenanceReconciler] {head}",
            f"  scanned       {self.scanned_rows} row(s)",
            f"  verified ok   {self.verified_rows}",
            f"  migrated      {self.migrated_rows}",
            f"  unverifiable  {self.unverifiable_rows} "
            f"(left exactly as found)",
        ]
        for old_new, n in sorted(self.migrations.items()):
            lines.append(f"    {n:>5}x  {old_new}")
        for why, n in sorted(self.unverifiable_reasons.items()):
            lines.append(f"    {n:>5}x  unverifiable: {why}")
        for b in self.backups:
            lines.append(f"  backup        {b}")
        return "\n".join(lines)


def _served_per_session(sessions_dir: Path) -> Dict[str, Tuple[str, str]]:
    """``session -> (model, verdict)`` from ModelPhysics alone.

    ``verdict`` is ``"ok"`` only when exactly one model was named. Everything
    else carries the reason it could not be established, so the caller can
    report precisely why a row was left alone.
    """
    out: Dict[str, Tuple[str, str]] = {}
    try:
        if not sessions_dir.is_dir():
            return out
        for sess_dir in sessions_dir.iterdir():
            if not sess_dir.is_dir():
                continue
            log = sess_dir / "debug.log"
            if not log.is_file():
                out[sess_dir.name] = ("", "no session log")
                continue
            try:
                txt = log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                out[sess_dir.name] = ("", "session log unreadable")
                continue
            names = set(_PHYSICS_RE.findall(txt))
            if not names:
                out[sess_dir.name] = ("", "ModelPhysics never ran")
            elif len(names) > 1:
                out[sess_dir.name] = ("", f"several models served: {sorted(names)}")
            else:
                out[sess_dir.name] = (next(iter(names)), "ok")
    except Exception:  # noqa: BLE001 — an unreadable tree repairs nothing
        logger.debug("[ProvenanceReconciler] session scan degraded", exc_info=True)
    return out


def adjudicate_sessions(sessions_dir: Path) -> Dict[str, Tuple[str, str]]:
    """Public seam over :func:`_served_per_session`, so a caller (or a test)
    can inspect the evidence WITHOUT touching the corpus."""
    return _served_per_session(Path(sessions_dir))


def reconcile_corpus(
    corpus_dir: Path,
    sessions_dir: Path,
    *,
    dry_run: bool = True,
    glob: str = "experience_*.jsonl",
) -> ReconcileReport:
    """Repair misattributed ``model_id`` rows. NEVER raises.

    A row is migrated only when its session's ModelPhysics evidence names
    exactly one model AND that model differs from what the row claims. The
    row's every other field, including ``event_id``, is preserved.
    """
    report = ReconcileReport(dry_run=dry_run)
    try:
        corpus_dir = Path(corpus_dir)
        served = _served_per_session(Path(sessions_dir))
        if not corpus_dir.is_dir():
            logger.info("[ProvenanceReconciler] no corpus at %s", corpus_dir)
            return report

        stamp = time.strftime("%Y%m%d-%H%M%S")
        for path in sorted(corpus_dir.glob(glob)):
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            out_lines: List[str] = []
            changed = 0
            for line in raw.splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001 — a bad line is left verbatim
                    out_lines.append(line)
                    continue
                report.scanned_rows += 1
                claimed = str(row.get("model_id", "") or "")
                sess = str(row.get("session_id", "") or "")
                truth, verdict = served.get(sess, ("", "session not found"))
                if not claimed or not sess:
                    out_lines.append(line)
                    continue
                if verdict == "ok" and truth and truth == claimed:
                    report.verified_rows += 1
                    out_lines.append(line)
                    continue
                if verdict == "ok" and truth and truth != claimed:
                    row["model_id"] = truth
                    # An audit trail ON the row: a corpus that was silently
                    # rewritten is as untrustworthy as one that was wrong.
                    row["model_id_reconciled_from"] = claimed
                    row["model_id_reconciled_evidence"] = "ModelPhysics"
                    key = f"{claimed} -> {truth}"
                    report.migrations[key] = report.migrations.get(key, 0) + 1
                    report.migrated_rows += 1
                    changed += 1
                    out_lines.append(json.dumps(row, sort_keys=True))
                    continue
                if verdict != "ok":
                    # No evidence either way. The row keeps whatever it has —
                    # this tool repairs what it can PROVE and reports the rest,
                    # because a guess written into the training corpus is the
                    # same class of damage it exists to undo.
                    report.unverifiable_rows += 1
                    key = f"{verdict} (claims {claimed})"
                    report.unverifiable_reasons[key] = (
                        report.unverifiable_reasons.get(key, 0) + 1
                    )
                out_lines.append(line)

            if changed and not dry_run:
                backup = path.with_name(f"{path.name}.bak-{stamp}")
                try:
                    backup.write_text(raw, encoding="utf-8")
                    report.backups.append(str(backup))
                    tmp = path.with_suffix(path.suffix + ".tmp")
                    tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
                    os.replace(tmp, path)
                    report.files_rewritten += 1
                except OSError:
                    logger.warning(
                        "[ProvenanceReconciler] could not rewrite %s — left "
                        "untouched", path, exc_info=True,
                    )
        logger.info("%s", report.render())
    except Exception:  # noqa: BLE001 — a failed repair must never break boot
        logger.warning("[ProvenanceReconciler] pass degraded", exc_info=True)
    return report


def _main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", default=str(
        Path.home() / ".jarvis" / "trinity" / "events"))
    parser.add_argument("--sessions", default=".ouroboros/sessions")
    parser.add_argument("--apply", action="store_true",
                        help="write the repair (default: dry run)")
    args = parser.parse_args(argv)
    rep = reconcile_corpus(
        Path(args.corpus), Path(args.sessions), dry_run=not args.apply,
    )
    print(rep.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

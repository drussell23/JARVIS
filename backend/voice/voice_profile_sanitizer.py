"""
Boot-time removal of biologically impossible values from stored voice profiles.

The extractors no longer write fabrications and the read path no longer reasons
from them, but neither of those removes what is already on disk. This does,
once per boot, off the event loop, and without ever destroying evidence it
cannot regenerate.

WHAT IS ON DISK
---------------
``~/.jarvis/learning/jarvis_learning.db``, ``speaker_profiles``, row 1::

    speaker_name      = Derek J. Russell
    speaking_rate_wpm = 420.0        # 40-300 wpm
    formant_f1_hz     = 42.91        # 150-1200 Hz
    formant_f2_hz     = 80.03        # 500-3500 Hz
    pitch_mean_hz     = 246.85       # plausible, kept
    voiceprint_embedding = <BLOB>    # the only thing that authorises anything

QUARANTINE, NOT DELETION — AND WHY
----------------------------------
The obvious reading of "purge the corrupted row" is ``DELETE FROM
speaker_profiles``. This does not do that by default, and the reason is worth
stating plainly because it is a deliberate departure:

**The embedding is not corrupt, and it is the only thing that can authorise an
unlock.** The verdict is ``posterior_prob >= threshold``, dominated by the
embedding comparison; the acoustic scalars are a minority contribution that now
abstains when absent. Deleting the row to remove three bad scalars would also
destroy a 192-dimensional voiceprint that passed every finiteness and dimension
check — leaving the operator unable to unlock by voice AT ALL until a full
re-enrolment completes, in exchange for removing values that the read gate
already neutralises.

So the default is to null the impossible columns and archive the original row
first. That satisfies the actual requirement — no impossible value survives on
disk, and the profile is flagged for re-enrolment — without a destructive step
that cannot be undone and that trades a working authenticator for a clean one.

Full-row purge remains available, and happens automatically in the one case
where the row genuinely has no salvageable content: an embedding that is
missing, non-finite, or of a dimension nothing can compare against. There, the
row cannot authorise anything, and keeping it only produces confusing partial
matches. ``JARVIS_VOICE_PROFILE_PURGE_MODE=row`` forces the destructive
behaviour for every violating profile if that is what the operator wants.

WHY IT NEVER BLOCKS THE LOOP
----------------------------
Every sqlite3 call runs in a worker thread via the repo's serialised offload
helper, not on the event loop, and the whole sweep is fire-and-forget. PR #70425
established the rule this follows: the starver was ``import`` blocking on a
module lock, and the fix was to funnel deferred work through ONE serialised
worker rather than scattering it across ``asyncio.to_thread`` calls that contend
for a shared executor. A boot hook that stalls the loop to clean a database is a
worse defect than the data it cleans.

Failure is never fatal. A locked, missing, read-only or schema-drifted database
leaves the profile untouched and logs why: the read gate in
``learning_database`` already prevents the stored values being reasoned from, so
this pass is hygiene, not a load-bearing control.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

try:  # pragma: no cover - depends on which root is on sys.path
    from backend.voice.biological_bounds import BiologicalBoundsValidator, Violation
except ImportError:  # pragma: no cover
    from voice.biological_bounds import BiologicalBoundsValidator, Violation

logger = logging.getLogger(__name__)

__all__ = [
    "SanitizationReport",
    "VoiceProfileSanitizer",
    "sanitize_voice_profiles",
    "schedule_voice_profile_sanitization",
    "default_profile_db_path",
]

#: Columns that describe the voice. Kept in step with ``learning_database``'s
#: ``_ACOUSTIC_COLUMNS``; the sanitiser intersects this with the table's actual
#: PRAGMA output, so a schema that predates any of them costs that column and
#: not the sweep.
ACOUSTIC_COLUMNS: Tuple[str, ...] = (
    "pitch_mean_hz", "pitch_std_hz", "pitch_range_hz", "pitch_min_hz", "pitch_max_hz",
    "formant_f1_hz", "formant_f2_hz", "formant_f3_hz", "formant_f4_hz",
    "spectral_centroid_hz", "spectral_rolloff_hz",
    "speaking_rate_wpm", "speech_rate_wpm", "average_pitch_hz", "pause_ratio",
    "jitter_percent", "shimmer_percent", "harmonic_to_noise_ratio_db",
)

_QUARANTINE_TABLE = "speaker_profiles_quarantine"
_FLAG_TABLE = "speaker_profile_flags"


def _env_flag(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def default_profile_db_path() -> Path:
    """
    Where the speaker profiles live.

    Resolved exactly as ``VoiceProfileConfig`` resolves it — ``JARVIS_DATA_DIR``
    then the ``learning/jarvis_learning.db`` suffix — because a sanitiser that
    computes its own path will one day clean a different file than the one the
    system reads. Callers that already hold a configured path should pass it
    rather than rely on this, and the boot hook does.
    """
    override = str(os.environ.get("JARVIS_SPEAKER_PROFILE_DB", "")).strip()
    if override:
        return Path(override).expanduser()
    data_dir = str(os.environ.get("JARVIS_DATA_DIR", "~/.jarvis")).strip() or "~/.jarvis"
    return Path(data_dir).expanduser() / "learning" / "jarvis_learning.db"


@dataclass
class SanitizationReport:
    """What the sweep found and did. Returned rather than logged-and-discarded."""

    scanned: int = 0
    profiles_cleaned: int = 0
    values_nulled: int = 0
    rows_purged: int = 0
    flagged_for_reenrollment: List[str] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)
    skipped_reason: Optional[str] = None
    duration_ms: float = 0.0

    @property
    def changed(self) -> bool:
        return bool(self.values_nulled or self.rows_purged)

    def describe(self) -> str:
        if self.skipped_reason:
            return f"skipped ({self.skipped_reason})"
        return (
            f"scanned={self.scanned} cleaned={self.profiles_cleaned} "
            f"nulled={self.values_nulled} purged={self.rows_purged} "
            f"flagged={len(self.flagged_for_reenrollment)} "
            f"in {self.duration_ms:.0f}ms"
        )


class VoiceProfileSanitizer:
    """
    Scans ``speaker_profiles`` and removes values no vocal tract can produce.

    All database work is synchronous and expected to run in a worker thread —
    :func:`sanitize_voice_profiles` is the async entry point that arranges that.
    Constructed with an explicit validator in tests; defaults to the
    environment-configured one in production.
    """

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        validator: Optional[BiologicalBoundsValidator] = None,
        *,
        purge_rows: Optional[bool] = None,
        timeout_s: float = 5.0,
    ) -> None:
        self.db_path = Path(db_path) if db_path else default_profile_db_path()
        self.validator = validator or BiologicalBoundsValidator.from_env()
        mode = str(os.environ.get("JARVIS_VOICE_PROFILE_PURGE_MODE", "columns")).strip().lower()
        self.purge_rows = (mode == "row") if purge_rows is None else purge_rows
        self.timeout_s = timeout_s

    # ── sync core (runs in a worker thread) ─────────────────────────────
    def run(self) -> SanitizationReport:
        report = SanitizationReport()
        started = time.monotonic()

        if not self.db_path.exists():
            report.skipped_reason = f"no database at {self.db_path}"
            return report

        connection: Optional[sqlite3.Connection] = None
        try:
            connection = sqlite3.connect(str(self.db_path), timeout=self.timeout_s)
            connection.row_factory = sqlite3.Row
            self._sweep(connection, report)
            connection.commit()
        except sqlite3.OperationalError as exc:
            # Locked or read-only. The read gate already neutralises the stored
            # values, so a failed sweep is hygiene deferred, not a control lost.
            report.skipped_reason = f"database unavailable ({exc})"
            logger.warning("[ProfileSanitizer] %s", report.skipped_reason)
        except Exception as exc:  # noqa: BLE001 — a hygiene pass may not take the boot down
            report.skipped_reason = f"{type(exc).__name__}: {exc}"
            logger.warning("[ProfileSanitizer] sweep failed — %s", report.skipped_reason)
        finally:
            if connection is not None:
                connection.close()
            report.duration_ms = (time.monotonic() - started) * 1000.0

        return report

    # ── internals ───────────────────────────────────────────────────────
    def _sweep(self, connection: sqlite3.Connection, report: SanitizationReport) -> None:
        columns = self._table_columns(connection, "speaker_profiles")
        if not columns:
            report.skipped_reason = "speaker_profiles table not present"
            return

        # Only columns this schema actually has. An older table costs that
        # column's check, never the sweep — the same PRAGMA-driven discipline
        # the metrics database adopted in d02ccb1014.
        checkable = [c for c in ACOUSTIC_COLUMNS if c in columns]
        if not checkable:
            report.skipped_reason = "no acoustic columns in this schema"
            return

        key = "speaker_id" if "speaker_id" in columns else None
        if key is None:
            report.skipped_reason = "speaker_profiles has no speaker_id key"
            return

        rows = connection.execute(
            f"SELECT * FROM speaker_profiles"  # noqa: S608 - fixed table name
        ).fetchall()
        report.scanned = len(rows)

        for row in rows:
            profile = dict(row)
            violations = self._violations(profile, checkable)
            if not violations:
                continue

            speaker = str(profile.get("speaker_name") or profile.get(key))
            report.violations.extend(f"{speaker}: {v.describe()}" for v in violations)

            self._archive(connection, profile)

            if self.purge_rows or self._embedding_is_unusable(profile, columns):
                self._purge_row(connection, key, profile[key], speaker, report)
            else:
                self._null_columns(connection, key, profile[key], violations, report)

            self._flag_for_reenrollment(connection, speaker, violations)
            report.flagged_for_reenrollment.append(speaker)
            report.profiles_cleaned += 1

    def _violations(self, profile: Dict[str, Any], columns: Sequence[str]) -> List[Violation]:
        found: List[Violation] = []
        for column in columns:
            value = profile.get(column)
            if value is None:
                continue
            violation = self.validator.check(column, value)
            if violation is not None:
                found.append(violation)
        return found

    def _embedding_is_unusable(self, profile: Dict[str, Any], columns: Sequence[str]) -> bool:
        """
        True when the row cannot authorise anything even after cleaning.

        Only then is a full purge the right call: with no usable voiceprint the
        profile produces no comparison, and keeping it yields confusing partial
        matches rather than a clean "not enrolled".

        Deliberately conservative — a BLOB that is merely *unexpected* is left
        alone. ``embedding_ops`` is the authority on decoding these, and this
        pass must not become a second, dumber opinion that deletes a profile
        the real decoder could have read.
        """
        if "voiceprint_embedding" not in columns:
            return False
        blob = profile.get("voiceprint_embedding")
        if blob is None:
            return True
        if isinstance(blob, (bytes, bytearray, memoryview)):
            raw = bytes(blob)
            # float32 vectors only; a length that is not a whole number of
            # float32s is a different encoding, not a broken embedding.
            return len(raw) == 0
        return False

    def _table_columns(self, connection: sqlite3.Connection, table: str) -> List[str]:
        try:
            return [r[1] for r in connection.execute(f"PRAGMA table_info({table})")]
        except sqlite3.Error:
            return []

    def _archive(self, connection: sqlite3.Connection, profile: Dict[str, Any]) -> None:
        """
        Keep the original row as JSON before touching it.

        Nothing here is recoverable from anywhere else, and a sanitiser that
        cannot be second-guessed is a sanitiser nobody can audit. The archive is
        text so it survives every future schema change to ``speaker_profiles``.
        """
        try:
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {_QUARANTINE_TABLE} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        speaker_name TEXT,
                        quarantined_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        reason TEXT,
                        original_row TEXT
                    )"""
            )
            serialisable = {
                k: (v if isinstance(v, (int, float, str, type(None))) else repr(v))
                for k, v in profile.items()
            }
            connection.execute(
                f"INSERT INTO {_QUARANTINE_TABLE} (speaker_name, reason, original_row) "
                f"VALUES (?, ?, ?)",
                (
                    str(profile.get("speaker_name") or ""),
                    "biologically impossible acoustic values",
                    json.dumps(serialisable, default=str),
                ),
            )
        except sqlite3.Error as exc:
            # An un-archivable row is still worth cleaning; say so rather than
            # silently proceeding as though a backup exists.
            logger.warning(
                "[ProfileSanitizer] could not archive '%s' (%s) — cleaning "
                "anyway, without a recoverable copy",
                profile.get("speaker_name"), exc,
            )

    def _null_columns(
        self,
        connection: sqlite3.Connection,
        key: str,
        key_value: Any,
        violations: Sequence[Violation],
        report: SanitizationReport,
    ) -> None:
        names = sorted({v.name for v in violations})
        assignments = ", ".join(f"{name} = NULL" for name in names)
        connection.execute(
            f"UPDATE speaker_profiles SET {assignments} WHERE {key} = ?",  # noqa: S608
            (key_value,),
        )
        report.values_nulled += len(names)
        logger.warning(
            "[ProfileSanitizer] nulled %d impossible value(s) on profile %s=%r: %s",
            len(names), key, key_value, ", ".join(names),
        )

    def _purge_row(
        self,
        connection: sqlite3.Connection,
        key: str,
        key_value: Any,
        speaker: str,
        report: SanitizationReport,
    ) -> None:
        connection.execute(
            f"DELETE FROM speaker_profiles WHERE {key} = ?", (key_value,)  # noqa: S608
        )
        report.rows_purged += 1
        logger.warning(
            "[ProfileSanitizer] PURGED profile '%s' (%s=%r) — archived to %s. "
            "Re-enrolment is required before voice authentication can succeed.",
            speaker, key, key_value, _QUARANTINE_TABLE,
        )

    def _flag_for_reenrollment(
        self,
        connection: sqlite3.Connection,
        speaker: str,
        violations: Sequence[Violation],
    ) -> None:
        """
        Record that this speaker needs enrolling again, durably.

        A log line is not a flag: it scrolls away, and the next boot has no way
        to know the profile it just loaded is a cleaned remnant rather than a
        complete one. This table is the durable answer, and the natural key on
        ``speaker_name`` makes repeated sweeps idempotent.
        """
        try:
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {_FLAG_TABLE} (
                        speaker_name TEXT PRIMARY KEY,
                        needs_reenrollment INTEGER NOT NULL DEFAULT 1,
                        flagged_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        detail TEXT
                    )"""
            )
            connection.execute(
                f"""INSERT INTO {_FLAG_TABLE} (speaker_name, needs_reenrollment, detail)
                    VALUES (?, 1, ?)
                    ON CONFLICT(speaker_name) DO UPDATE SET
                        needs_reenrollment = 1,
                        flagged_at = CURRENT_TIMESTAMP,
                        detail = excluded.detail""",
                (speaker, "; ".join(v.describe() for v in violations)),
            )
        except sqlite3.Error as exc:
            logger.warning("[ProfileSanitizer] could not flag '%s' (%s)", speaker, exc)


async def sanitize_voice_profiles(
    db_path: Optional[Union[str, Path]] = None,
    validator: Optional[BiologicalBoundsValidator] = None,
) -> SanitizationReport:
    """
    Run the sweep off the event loop and return what it did.

    The sqlite3 work goes through ``async_offload.call_off_loop`` when it is
    importable, and falls back to ``asyncio.to_thread`` when it is not.

    That preference is the whole point, not a stylistic one. ``call_off_loop``
    uses a pool *separate from asyncio's default executor*, which the 200+
    ``to_thread`` call sites in this repo share and which is sized for short
    work. PR #70425 measured what happens when blocking work is scattered across
    that shared pool instead: four threads contending at once and a 12.38 s loop
    stall. A boot-time database sweep is exactly the shape of work that belongs
    on the dedicated pool.
    """
    sanitizer = VoiceProfileSanitizer(db_path=db_path, validator=validator)

    try:  # pragma: no cover - availability differs per root
        try:
            from backend.core.async_offload import call_off_loop
        except ImportError:
            from core.async_offload import call_off_loop  # type: ignore
        return await call_off_loop(sanitizer.run)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 — never let hygiene break the boot
        logger.debug("[ProfileSanitizer] dedicated offload unavailable (%s)", exc)

    return await asyncio.to_thread(sanitizer.run)


def schedule_voice_profile_sanitization(
    db_path: Optional[Path] = None,
) -> "Optional[asyncio.Task[SanitizationReport]]":
    """
    Fire-and-forget the sweep on the running loop; ``None`` when there is none.

    Boot calls this. It returns immediately — nothing waits on the result, and
    nothing downstream depends on it having finished, because the read gate in
    ``learning_database`` is what actually prevents impossible stored values
    reaching a verdict. This pass only stops them being loaded again tomorrow.
    """
    if not _env_flag("JARVIS_VOICE_PROFILE_SANITIZE_ENABLED", True):
        logger.info("[ProfileSanitizer] disabled by "
                    "JARVIS_VOICE_PROFILE_SANITIZE_ENABLED")
        return None

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("[ProfileSanitizer] no running loop — not scheduling")
        return None

    async def _run() -> SanitizationReport:
        report = await sanitize_voice_profiles(db_path)
        if report.skipped_reason:
            logger.info("[ProfileSanitizer] %s", report.describe())
        elif report.changed:
            logger.warning(
                "[ProfileSanitizer] %s — re-enrol %s to restore full acoustic "
                "verification: python3 backend/voice/enroll_voice.py",
                report.describe(), ", ".join(report.flagged_for_reenrollment),
            )
        else:
            logger.info("[ProfileSanitizer] %s — all stored profiles are "
                        "biologically plausible", report.describe())
        return report

    task = loop.create_task(_run(), name="voice-profile-sanitizer")
    # Never let a failure here surface as an unretrieved-exception warning at
    # interpreter shutdown; this is hygiene and its failure is already logged.
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    return task

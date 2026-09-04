"""WorkOrderSensor (P0.2) — ingest the operator's OWN roadmap as work orders.

O+V is a proven executor that self-selects annotation-grade trivia because
nothing feeds it the operator's actual intent. The operator already maintains
a roadmap — the ``NEXT:`` markers in ``.superpowers/sdd/progress.md``, the plan
docs under ``docs/`` — but no sensor reads it. This closes that: it polls
operator-DECLARED artifact paths, extracts each open work item, resolves the
real files it names, and emits it as a ``source="roadmap"`` signal so P0.1's
value-derived priority floats it to the top of the queue instead of drowning
at the deferred floor.

Zero-Trust: the sensor reads ONLY the paths the operator explicitly opts in
via ``JARVIS_WORK_ORDER_SOURCES`` — nothing is trusted unless the operator
declares it (the opt-in IS the authorization boundary, analogous to the
roadmap_reader's HMAC signature). The item text becomes a work DESCRIPTION
that flows through the FULL cage (classify → route → GENERATE → Iron Gate →
SemanticGuardian → GATE → VERIFY) like every other signal — a work order can
never do what a normal signal can't, and it carries no elevated authority.

Everything is env-tunable (no hardcoded paths, markers, urgencies, or bounds).
Master ``JARVIS_WORK_ORDER_SENSOR_ENABLED`` default-FALSE (§33.1). Fail-soft:
an unreadable artifact, a torn ledger, or a bad item NEVER perturbs intake.
Cross-session dedup (a persisted seen-hash ledger) so a stable roadmap is not
re-emitted every boot. ``JARVIS_ALLOW_ROADMAP_REVISIT`` shadows that ledger in
memory for deep-sampling runs: hashes loaded from disk stop SUPPRESSING without
ever being deleted, so a deliberate re-run of a stable roadmap emits its work
again while the on-disk record stays truthful and complete.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, List, Optional, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)

logger = logging.getLogger(__name__)

_ENV_MASTER = "JARVIS_WORK_ORDER_SENSOR_ENABLED"
_ENV_SOURCES = "JARVIS_WORK_ORDER_SOURCES"
_ENV_MARKERS = "JARVIS_WORK_ORDER_MARKERS"
_ENV_DEFAULT_URGENCY = "JARVIS_WORK_ORDER_DEFAULT_URGENCY"
_ENV_RECENT_N = "JARVIS_WORK_ORDER_RECENT_N"
_ENV_MAX_ITEMS = "JARVIS_WORK_ORDER_MAX_ITEMS"
_ENV_MAX_BYTES = "JARVIS_WORK_ORDER_MAX_BYTES"
_ENV_INTERVAL_S = "JARVIS_WORK_ORDER_INTERVAL_S"
_ENV_SEEN_LEDGER = "JARVIS_WORK_ORDER_SEEN_LEDGER"
_ENV_SEEN_CAP = "JARVIS_WORK_ORDER_SEEN_CAP"
_ENV_ALLOW_REVISIT = "JARVIS_ALLOW_ROADMAP_REVISIT"

# Operator intent is, by construction, an EXPLICIT declaration of what should
# happen next — so it defaults to a high urgency, which is exactly the
# "explicit override" P0.1 escalates out of the deferred floor. Env-tunable.
_DEFAULT_SOURCES = ".superpowers/sdd/progress.md"
_DEFAULT_MARKERS = "NEXT:"
_DEFAULT_URGENCY = "high"
_DEFAULT_RECENT_N = 3          # tail of an append-only log; 0 = all items
_DEFAULT_MAX_ITEMS = 20
_DEFAULT_MAX_BYTES = 2_000_000
_DEFAULT_INTERVAL_S = 900.0
_DEFAULT_SEEN_CAP = 1000
_VALID_URGENCIES = ("low", "normal", "high", "critical")

# File tokens the operator might name in a work item. Backticked paths win;
# bare path-like tokens are a fallback. A trailing :line-number is stripped.
# Only paths that ACTUALLY EXIST under the repo are kept — a real target is
# what lets P0.1's signal_value band the work order (executable vs cosmetic).
_BACKTICK_PATH = re.compile(r"`([^`\n]+?)`")
_BARE_PATH = re.compile(r"(?<![`\w])([\w./-]+\.[A-Za-z0-9]{1,6})(?::\d+)?")


_TRUTHY = ("1", "true", "yes", "on")


def _truthy_env(name: str, default: str = "false") -> bool:
    """One truthiness rule for every gate in this module. NEVER raises."""
    try:
        return os.getenv(name, default).strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001 — a hostile env never breaks a gate
        return False


def sensor_enabled() -> bool:
    """Master gate — default-FALSE per §33.1. NEVER raises."""
    return _truthy_env(_ENV_MASTER)


def revisit_enabled() -> bool:
    """Deep-sampling gate — default-FALSE, so a normal boot keeps the dedup.

    Read once at INIT rather than per scan: the shadow is a property of the
    ledger snapshot THIS process loaded. A flag flipped mid-session must not
    retroactively un-suppress hashes this session has already emitted, which
    is exactly what a per-scan read would do on the next poll.
    """
    return _truthy_env(_ENV_ALLOW_REVISIT)


def _csv_env(name: str, default: str) -> List[str]:
    raw = os.getenv(name, "")
    raw = raw if raw.strip() else default
    return [p.strip() for p in raw.split(",") if p.strip()]


def _int_env(name: str, default: int, *, floor: int = 0) -> int:
    try:
        return max(floor, int(os.getenv(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float, *, floor: float = 0.0) -> float:
    try:
        return max(floor, float(os.getenv(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


def _default_urgency() -> str:
    raw = os.getenv(_ENV_DEFAULT_URGENCY, "").strip().lower()
    return raw if raw in _VALID_URGENCIES else _DEFAULT_URGENCY


class WorkOrderSensor:
    """Polls operator-declared roadmap artifacts and emits work-order signals."""

    name = "work_order"

    def __init__(
        self,
        *,
        repo: str,
        router: Any,
        project_root: Path,
        poll_interval_s: Optional[float] = None,
        seen_ledger_path: Optional[Path] = None,
    ) -> None:
        self._repo = repo
        self._router = router
        self._root = Path(project_root)
        self._poll_interval_s = (
            poll_interval_s if poll_interval_s is not None
            else _float_env(_ENV_INTERVAL_S, _DEFAULT_INTERVAL_S, floor=5.0)
        )
        self._seen_ledger_path = (
            Path(seen_ledger_path) if seen_ledger_path is not None
            else self._resolved_seen_ledger()
        )
        self._seen: "list[str]" = []  # ordered (bounded ring); membership via set
        self._seen_set: set = set()
        # Hashes that were on DISK when this process started and are therefore
        # exempt from suppression under JARVIS_ALLOW_ROADMAP_REVISIT. Empty
        # unless the flag is on, so the default path is byte-for-byte the old
        # behaviour. _record_seen discharges an entry once this session has
        # actually re-emitted it — the exemption is worth exactly one re-emit,
        # so a multi-poll session does not re-emit the same roadmap each hour.
        self._revisit_shadow: set = set()
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._load_seen()

    # ── source resolution ────────────────────────────────────────────────
    def _resolved_seen_ledger(self) -> Path:
        raw = os.getenv(_ENV_SEEN_LEDGER, "").strip()
        if raw:
            return Path(raw)
        return self._root / ".jarvis" / "work_order_seen.json"

    def _iter_source_paths(self) -> List[Path]:
        """Glob every operator-declared source pattern under the repo root.
        De-duplicated, existing regular files only. NEVER raises."""
        out: List[Path] = []
        seen_paths: set = set()
        for pattern in _csv_env(_ENV_SOURCES, _DEFAULT_SOURCES):
            try:
                # Absolute patterns are honored verbatim; relative ones glob
                # under the repo root. glob handles both literal paths and
                # wildcard patterns (docs/**/*.md).
                p = Path(pattern)
                candidates = (
                    [p] if p.is_absolute()
                    else list(self._root.glob(pattern))
                )
                for c in candidates:
                    rc = c.resolve()
                    if rc.is_file() and rc not in seen_paths:
                        seen_paths.add(rc)
                        out.append(rc)
            except Exception:  # noqa: BLE001 — a bad pattern never breaks scan
                continue
        return out

    # ── item extraction ──────────────────────────────────────────────────
    def _extract_items(self, text: str) -> List[Tuple[str, str]]:
        """Return (marker, item_text) for EVERY marker-matched line, in
        document order. Recency is applied later — over the target-BEARING
        candidates, not raw lines — so a prose-only tail (common in an
        append-only log) never masks the recent file-scoped roadmap items."""
        markers = _csv_env(_ENV_MARKERS, _DEFAULT_MARKERS)
        items: List[Tuple[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            for marker in markers:
                idx = stripped.find(marker)
                if idx == -1:
                    continue
                body = stripped[idx + len(marker):].strip()
                if body:
                    items.append((marker, body))
                break
        return items

    def _extract_targets(self, text: str) -> Tuple[str, ...]:
        """Resolve the real repo files a work item names (backticked paths
        first, bare path-like tokens as fallback). Only files that EXIST are
        kept — a real target is what lets P0.1 band the work order."""
        found: List[str] = []
        seen: set = set()

        def _consider(raw: str) -> None:
            raw = raw.strip().rstrip(":")
            raw = re.sub(r":\d+$", "", raw)  # strip a trailing :line-number
            if not raw or raw in seen:
                return
            cand = Path(raw)
            abs_cand = cand if cand.is_absolute() else (self._root / raw)
            try:
                if abs_cand.is_file():
                    seen.add(raw)
                    found.append(raw)
            except Exception:  # noqa: BLE001
                pass

        for m in _BACKTICK_PATH.finditer(text):
            _consider(m.group(1))
        for m in _BARE_PATH.finditer(text):
            _consider(m.group(1))
        return tuple(found)

    def _suppressed(self, item_hash: str) -> bool:
        """Has this item already been emitted, for purposes of THIS scan?

        The ledger carries two meanings that are normally the same answer:
        "we have seen this" (accumulation) and "do not emit this"
        (suppression). Revisit mode separates them — the shadow keeps
        accumulation intact while standing down suppression for the hashes
        that predate this process. NEVER raises.
        """
        if item_hash not in self._seen_set:
            return False
        return item_hash not in self._revisit_shadow

    def _item_hash(self, source_rel: str, item_text: str) -> str:
        h = hashlib.sha256()
        h.update(source_rel.encode("utf-8", "replace"))
        h.update(b"\x00")
        # Normalize whitespace so a reflow doesn't re-emit an item.
        h.update(" ".join(item_text.split()).encode("utf-8", "replace"))
        return h.hexdigest()[:16]

    # ── scan ─────────────────────────────────────────────────────────────
    async def scan_once(self) -> List[IntentEnvelope]:
        """One scan across all declared sources. Emits an envelope per NEW
        (unseen) work item, ingests it, and records the hash. NEVER raises."""
        if not sensor_enabled():
            return []
        emitted: List[IntentEnvelope] = []
        max_items = _int_env(_ENV_MAX_ITEMS, _DEFAULT_MAX_ITEMS, floor=1)
        max_bytes = _int_env(_ENV_MAX_BYTES, _DEFAULT_MAX_BYTES, floor=1024)
        recent_n = _int_env(_ENV_RECENT_N, _DEFAULT_RECENT_N)
        urgency = _default_urgency()
        new_hashes: List[str] = []
        try:
            for path in self._iter_source_paths():
                if len(emitted) >= max_items:
                    break
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    if len(text) > max_bytes:
                        text = text[-max_bytes:]  # keep the live tail
                except OSError:
                    continue
                try:
                    source_rel = str(path.relative_to(self._root))
                except ValueError:
                    source_rel = str(path)
                # Build target-BEARING candidates in document order. A
                # target-less item (prose the pipeline can't localize) is
                # marked seen + skipped so it never re-processes or log-spams.
                candidates: List[Tuple[str, IntentEnvelope]] = []
                for marker, item_text in self._extract_items(text):
                    ih = self._item_hash(source_rel, item_text)
                    if self._suppressed(ih):
                        continue
                    env = self._build_envelope(
                        source_rel, marker, item_text, urgency,
                    )
                    if env is None:
                        new_hashes.append(ih)  # target-less → seen, skip
                        continue
                    candidates.append((ih, env))
                # Recency over ACTIONABLE items: in an append-only log the live
                # roadmap is the tail of file-scoped work — a prose-only tail
                # never masks it. RECENT_N=0 keeps all (finite task lists).
                if recent_n > 0 and len(candidates) > recent_n:
                    candidates = candidates[-recent_n:]
                for ih, env in candidates:
                    if len(emitted) >= max_items:
                        break
                    try:
                        await self._router.ingest(env)
                        emitted.append(env)
                        new_hashes.append(ih)
                    except Exception:  # noqa: BLE001 — one bad ingest ≠ dead scan
                        # Do NOT mark seen — a transient ingest fault retries.
                        logger.debug(
                            "[WorkOrderSensor] ingest failed for %s", ih,
                            exc_info=True,
                        )
            if new_hashes:
                self._record_seen(new_hashes)
                logger.info(
                    "[WorkOrderSensor] emitted %d work order(s) from %d source(s)",
                    len(emitted), len(_csv_env(_ENV_SOURCES, _DEFAULT_SOURCES)),
                )
        except Exception:  # noqa: BLE001 — scan is best-effort, never fatal
            logger.debug("[WorkOrderSensor] scan_once failed", exc_info=True)
        return emitted

    def _build_envelope(
        self, source_rel: str, marker: str, item_text: str, urgency: str,
    ) -> Optional[IntentEnvelope]:
        try:
            targets = self._extract_targets(item_text)
            if not targets:
                # A work order must name a subject O+V can act on. Prose-only
                # roadmap items (e.g. "run the soak") are the operator's, not
                # autonomous code work — skip them rather than emit a
                # target-less op the pipeline can't localize or value-band.
                logger.debug(
                    "[WorkOrderSensor] skipped target-less item from %s: %r",
                    source_rel, item_text[:80],
                )
                return None
            evidence = {
                "work_order_source": source_rel,
                "work_order_marker": marker,
                "work_order": True,
            }
            # ── Slice 20 — Delegated Provenance ──
            # If the operator-SIGNED roadmap (.jarvis/roadmap.yaml, HMAC via
            # roadmap_reader) contains a goal whose declared scope covers this
            # item's every target, attach that goal's claim POINTER so the
            # risk engine can verify delegated authority at classify time.
            # The claim grants nothing by itself — verification re-derives
            # signature/goal/scope from ground truth. No match / feature off
            # / any fault ⇒ no claim ⇒ governance surfaces stay blocked
            # exactly as before (fail-closed).
            try:
                from backend.core.ouroboros.governance.delegated_provenance import (  # noqa: E501
                    claim_for_targets,
                )
                _claim = claim_for_targets(targets)
                if _claim is not None:
                    evidence["provenance"] = _claim
            except Exception:  # noqa: BLE001 — provenance is additive, never fatal
                logger.debug(
                    "[WorkOrderSensor] provenance claim lookup degraded",
                    exc_info=True,
                )
            return make_envelope(
                source="roadmap",
                description=item_text[:2000],
                target_files=targets,
                repo=self._repo,
                confidence=0.95,  # operator-authored intent — high confidence
                urgency=urgency,
                evidence=evidence,
                requires_human_ack=False,  # operator already authorized it
            )
        except Exception:  # noqa: BLE001 — a malformed item never breaks scan
            logger.debug(
                "[WorkOrderSensor] envelope build failed for %s", source_rel,
                exc_info=True,
            )
            return None

    # ── dedup ledger (cross-session) ─────────────────────────────────────
    def _load_seen(self) -> None:
        try:
            if self._seen_ledger_path.is_file():
                data = json.loads(
                    self._seen_ledger_path.read_text(encoding="utf-8"),
                )
                if isinstance(data, list):
                    self._seen = [str(x) for x in data]
                    self._seen_set = set(self._seen)
        except Exception:  # noqa: BLE001 — a torn ledger just starts empty
            self._seen, self._seen_set = [], set()
        # Shadow the snapshot, never the file: the ledger is left on disk
        # exactly as found, so turning the flag off restores suppression for
        # every hash it ever recorded. A torn ledger shadows nothing, which
        # is correct — there is no suppression to stand down.
        if revisit_enabled():
            self._revisit_shadow = set(self._seen_set)
            if self._revisit_shadow:
                logger.info(
                    "[WorkOrderSensor] revisit ON — %d seen hash(es) shadowed "
                    "for re-emission; ledger %s left intact",
                    len(self._revisit_shadow), self._seen_ledger_path,
                )

    def _record_seen(self, hashes: List[str]) -> None:
        cap = _int_env(_ENV_SEEN_CAP, _DEFAULT_SEEN_CAP, floor=1)
        for h in hashes:
            # Spend the exemption: re-emitted once is what revisit buys, so
            # the next poll in this same session suppresses normally.
            self._revisit_shadow.discard(h)
            if h not in self._seen_set:
                self._seen.append(h)
                self._seen_set.add(h)
        if len(self._seen) > cap:  # bounded ring — drop oldest
            drop = self._seen[:-cap]
            self._seen = self._seen[-cap:]
            self._seen_set.difference_update(drop)
        try:
            self._seen_ledger_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._seen_ledger_path.with_suffix(
                self._seen_ledger_path.suffix + ".tmp",
            )
            tmp.write_text(json.dumps(self._seen), encoding="utf-8")
            os.replace(str(tmp), str(self._seen_ledger_path))
        except Exception:  # noqa: BLE001 — persistence best-effort
            logger.debug("[WorkOrderSensor] seen-ledger write failed",
                         exc_info=True)

    # ── lifecycle (mirrors the standalone-sensor contract) ───────────────
    async def start(self) -> None:
        self._running = True
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="work_order_sensor_poll",
        )
        logger.info(
            "[WorkOrderSensor] started poll_interval=%.0fs sources=%s enabled=%s",
            self._poll_interval_s, _csv_env(_ENV_SOURCES, _DEFAULT_SOURCES),
            sensor_enabled(),
        )

    async def stop(self) -> None:
        self._running = False
        task = self._poll_task
        self._poll_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a scan fault never kills the loop
                logger.debug("[WorkOrderSensor] poll iteration failed",
                             exc_info=True)
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                raise


__all__ = ["WorkOrderSensor", "sensor_enabled", "revisit_enabled"]

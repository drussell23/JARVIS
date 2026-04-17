"""Session replay viewer — single standalone HTML audit per battle test.

§8 says "every autonomous decision is visible," but the data is
distributed across debug.log, summary.json, cost_tracker.json, and
the per-op ledger JSONL files. Operators have to grep to correlate
them. This module consolidates all four sources into one
``replay.html`` written next to ``summary.json`` at shutdown.

Design principles:

  * **Self-contained HTML** — no network requests, no CDN, no external
    JS or CSS. Embedded stylesheet + minimal interactive JS inline.
    Operators can open it offline, archive it, attach to incident
    reports, etc.
  * **Grep-friendly** — every event is rendered as plain text so
    Cmd+F works. Decorative CSS only. No graphics that substitute
    for content.
  * **Partial-data tolerant** — missing debug.log / summary.json /
    ledger files produce a degraded-but-useful view, not a crash.
  * **Shutdown-time only** — V1 is static. Live-updating replay is a
    separate project (needs WebSocket or SSE + a running harness
    daemon).
  * **No live-running-system authority** — the viewer is read-only
    against session artifacts. It never mutates state, never calls
    orchestrator methods, never touches governance.

Env gates:

    JARVIS_SESSION_REPLAY_ENABLED   default 1
        Master switch. Set 0 to skip replay generation entirely
        (honest value: shaves ~50ms off shutdown for CI runs where
        nobody will look at the viewer).
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.SessionReplay")

_ENV_ENABLED = "JARVIS_SESSION_REPLAY_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def replay_enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "1").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Parsed data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayEvent:
    """One timeline entry — a parsed INFO line from debug.log.

    ``category`` groups related events (e.g. "guardian", "inference",
    "stream", "decision") so the viewer can filter by channel.
    """

    timestamp: str              # ISO-ish from debug.log
    logger: str                 # logger module name
    level: str                  # INFO | WARNING | ERROR | DEBUG
    category: str               # one of the known-prefix categories
    op_id: str                  # extracted from the line if present
    message: str                # raw message text
    fields: Dict[str, str] = field(default_factory=dict)  # parsed key=value pairs


@dataclass(frozen=True)
class ReplayOp:
    """Aggregated per-op record built from ledger + debug.log traces."""

    op_id: str
    short_op_id: str
    phases: Tuple[str, ...] = ()         # ledger state progression
    final_state: str = ""                # terminal or last-observed
    target_files: Tuple[str, ...] = ()
    goal: str = ""
    risk_tier: str = ""
    route: str = ""
    cost_usd: float = 0.0
    commit_hash: str = ""
    decision_outcomes: Tuple[str, ...] = ()


@dataclass
class ReplayData:
    """Everything the HTML assembler needs, derived from session artifacts."""

    session_id: str = ""
    stop_reason: str = ""
    duration_s: float = 0.0
    started_at_iso: str = ""
    ops: List[ReplayOp] = field(default_factory=list)
    events: List[ReplayEvent] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    cost_tracker: Dict[str, Any] = field(default_factory=dict)
    guardian_findings_count: int = 0
    inference_builds_count: int = 0
    stream_renders_count: int = 0
    # Per-category event counts for the top-bar filter checkboxes.
    category_counts: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# debug.log parser
# ---------------------------------------------------------------------------


# A representative line looks like:
#   2026-04-17T03:15:22 [Ouroboros.Orchestrator] INFO [SemanticGuard] op=X findings=...
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+"
    r"\[(?P<logger>[^\]]+)\]\s+"
    r"(?P<level>INFO|WARNING|ERROR|DEBUG)\s+"
    r"(?P<message>.*)$"
)

_KV_RE = re.compile(r"(\w+)=([^\s]+)")
# Match op-<uuidv7-ish>-<suffix> anywhere in the message. Suffixes are
# the known envelope families (cau=causal, sig=signal, ikey=idempotency,
# lse=lease). Matches whether it's bare ("op-abc-cau") or prefixed
# ("op=op-abc-cau" / "op_id=op-abc-cau").
_OP_ID_RE = re.compile(r"\b(op-[0-9a-f-]{3,}-(?:cau|sig|ikey|lse))\b")

# Category detection from line message prefixes.
_CATEGORY_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("[SemanticGuard]", "guardian"),
    ("[GoalInference]", "inference"),
    ("[StreamRender]", "stream"),
    ("[LastSessionSummary]", "lss"),
    ("[SemanticIndex]", "semantic_index"),
    ("[ConversationBridge]", "conv_bridge"),
    ("[Orchestrator]", "orchestrator"),
    ("[Harness]", "harness"),
    ("[GovernedLoop]", "gls"),
    ("[CommProtocol]", "comm"),
    ("[Plugins]", "plugins"),
    ("[Resume]", "resume"),
    ("[StatusLine]", "status_line"),
    ("[ClassifyClarify]", "clarify"),
    ("[NotifyApply]", "notify_apply"),
    ("[TDDDirective]", "tdd"),
    ("[MCPServer]", "mcp"),
)


def _categorize(message: str) -> str:
    for prefix, category in _CATEGORY_PREFIXES:
        if prefix in message:
            return category
    return "other"


def _extract_op_id(text: str) -> str:
    m = _OP_ID_RE.search(text)
    return m.group(1) if m else ""


def parse_debug_log(path: Path) -> List[ReplayEvent]:
    """Regex-parse the session's debug.log into structured events.

    Never raises — malformed lines are skipped. Missing file returns
    empty list.
    """
    if not path.is_file():
        return []
    out: List[ReplayEvent] = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return out
    for line in raw.splitlines():
        m = _LINE_RE.match(line.strip())
        if m is None:
            continue
        message = m.group("message")
        category = _categorize(message)
        op_id = _extract_op_id(message)
        fields: Dict[str, str] = {}
        # Harvest key=value pairs from structured INFO lines like
        # [SemanticGuard] op=X findings=2 …
        for kv in _KV_RE.finditer(message):
            k, v = kv.group(1), kv.group(2)
            # Avoid over-capturing — keep only recognized-shape keys.
            if k and len(k) <= 30 and len(v) <= 200:
                fields[k] = v
        out.append(ReplayEvent(
            timestamp=m.group("ts"),
            logger=m.group("logger"),
            level=m.group("level"),
            category=category,
            op_id=op_id,
            message=message,
            fields=fields,
        ))
    return out


# ---------------------------------------------------------------------------
# Ledger parser — correlates per-op FSM trails
# ---------------------------------------------------------------------------


def _parse_ledger_file(path: Path) -> Optional[ReplayOp]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    entries: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not entries:
        return None
    op_id = str(entries[-1].get("op_id", "")) or path.stem
    phases: List[str] = []
    final_state = ""
    goal = ""
    target_files: List[str] = []
    risk_tier = ""
    commit_hash = ""
    for e in entries:
        state = str(e.get("state", "")).lower()
        if state:
            phases.append(state)
            final_state = state
        data = e.get("data") or {}
        if not isinstance(data, dict):
            continue
        if state == "planned":
            if isinstance(data.get("goal"), str):
                goal = data["goal"]
            if isinstance(data.get("target_files"), list):
                target_files = [
                    str(x) for x in data["target_files"]
                    if isinstance(x, str)
                ]
            elif isinstance(data.get("target_file"), str):
                target_files = [data["target_file"]]
            if isinstance(data.get("risk_tier"), str):
                risk_tier = data["risk_tier"]
        if state == "applied":
            if isinstance(data.get("commit_hash"), str):
                commit_hash = data["commit_hash"]
    short = op_id.split("-", 1)[1][:10] if "-" in op_id else op_id[:10]
    return ReplayOp(
        op_id=op_id,
        short_op_id=short,
        phases=tuple(phases),
        final_state=final_state,
        target_files=tuple(target_files),
        goal=goal,
        risk_tier=risk_tier,
        commit_hash=commit_hash,
    )


def parse_ledger_for_session(
    ledger_root: Path, session_events: Sequence[ReplayEvent],
) -> List[ReplayOp]:
    """Correlate ledger files with ops observed in this session's
    debug.log. We filter to op_ids that appear in the events list so
    old unrelated ledger files don't pollute the session view.
    """
    if not ledger_root.is_dir():
        return []
    seen_op_ids: set = {e.op_id for e in session_events if e.op_id}
    ops: List[ReplayOp] = []
    for path in sorted(ledger_root.glob("op-*.jsonl")):
        # Quick filter by filename to avoid parsing every ledger file
        # in the repo — only parse ones whose stem appears in our op set.
        stem_op = path.stem
        if stem_op not in seen_op_ids:
            continue
        op = _parse_ledger_file(path)
        if op is not None:
            ops.append(op)
    return ops


# ---------------------------------------------------------------------------
# JSON artifact readers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Builder — assembles ReplayData from all artifact sources
# ---------------------------------------------------------------------------


class SessionReplayBuilder:
    """Read session artifacts + render ``replay.html`` next to them."""

    def __init__(self, session_dir: Path) -> None:
        self._session_dir = Path(session_dir)

    def build(self) -> Optional[Path]:
        """Build + write replay.html. Returns the written path on
        success, ``None`` when the env gate is off or the session dir
        is missing. Never raises — any partial-data failure degrades
        the view rather than aborting shutdown."""
        if not replay_enabled():
            logger.debug(
                "[SessionReplay] disabled via %s", _ENV_ENABLED,
            )
            return None
        if not self._session_dir.is_dir():
            logger.debug(
                "[SessionReplay] session dir missing: %s",
                self._session_dir,
            )
            return None

        try:
            data = self._collect()
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SessionReplay] data collection failed", exc_info=True,
            )
            data = ReplayData(
                session_id=self._session_dir.name,
                stop_reason="replay_collection_failed",
            )

        try:
            html_text = _render_html(data)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SessionReplay] render failed — writing fallback",
                exc_info=True,
            )
            html_text = _render_minimal_fallback(data)

        out_path = self._session_dir / "replay.html"
        try:
            out_path.write_text(html_text, encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.warning(
                "[SessionReplay] write failed at %s", out_path,
                exc_info=True,
            )
            return None

        logger.info(
            "[SessionReplay] written: %s "
            "(ops=%d events=%d guardian=%d inference=%d)",
            out_path,
            len(data.ops),
            len(data.events),
            data.guardian_findings_count,
            data.inference_builds_count,
        )
        return out_path

    # ------------------------------------------------------------------
    # Data collection
    # ------------------------------------------------------------------

    def _collect(self) -> ReplayData:
        events = parse_debug_log(self._session_dir / "debug.log")
        summary = _read_json(self._session_dir / "summary.json")
        cost_tracker = _read_json(self._session_dir / "cost_tracker.json")

        # Correlate ledger files for ops that appeared in THIS session.
        repo_root_guess = self._guess_repo_root()
        ledger_root = (
            repo_root_guess
            / ".ouroboros" / "state" / "ouroboros" / "ledger"
            if repo_root_guess else None
        )
        ops = (
            parse_ledger_for_session(ledger_root, events)
            if ledger_root else []
        )

        # Per-category counts for the filter UI.
        cat_counts: Dict[str, int] = {}
        guardian_n = 0
        inference_n = 0
        stream_n = 0
        for ev in events:
            cat_counts[ev.category] = cat_counts.get(ev.category, 0) + 1
            if ev.category == "guardian":
                guardian_n += 1
            elif ev.category == "inference":
                inference_n += 1
            elif ev.category == "stream":
                stream_n += 1

        return ReplayData(
            session_id=(
                str(summary.get("session_id"))
                if summary.get("session_id")
                else self._session_dir.name
            ),
            stop_reason=str(summary.get("stop_reason", "")),
            duration_s=float(summary.get("duration_s", 0.0) or 0.0),
            started_at_iso=_format_session_start(events),
            ops=ops,
            events=events,
            summary=summary,
            cost_tracker=cost_tracker,
            guardian_findings_count=guardian_n,
            inference_builds_count=inference_n,
            stream_renders_count=stream_n,
            category_counts=cat_counts,
        )

    def _guess_repo_root(self) -> Optional[Path]:
        """Walk up from the session dir to find a ``.git`` parent —
        the repo root. Ledger files live under ``<repo>/.ouroboros/``.
        Returns None if we can't confidently locate it."""
        cur = self._session_dir.resolve()
        for _ in range(8):
            if (cur / ".git").exists():
                return cur
            if cur.parent == cur:
                break
            cur = cur.parent
        return None


def _format_session_start(events: Sequence[ReplayEvent]) -> str:
    if not events:
        return ""
    return events[0].timestamp


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


_CSS = """
* { box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                 Helvetica, Arial, sans-serif;
    background: #0d1117; color: #c9d1d9; margin: 0; padding: 2rem;
    line-height: 1.5;
}
h1, h2, h3 {
    color: #f0f6fc; margin-top: 1.5em; border-bottom: 1px solid #30363d;
    padding-bottom: 0.25em;
}
h1 { font-size: 1.8em; }
h2 { font-size: 1.3em; }
h3 { font-size: 1.05em; }
code, pre {
    font-family: "SF Mono", ui-monospace, "Cascadia Code", Consolas, monospace;
    background: #161b22; padding: 0.1em 0.3em; border-radius: 3px;
    font-size: 0.9em;
}
pre {
    padding: 1em; overflow-x: auto; border: 1px solid #30363d;
    white-space: pre-wrap; word-wrap: break-word;
}
table {
    border-collapse: collapse; width: 100%; margin: 1em 0;
    background: #161b22; border: 1px solid #30363d;
}
th, td {
    padding: 0.5em 0.75em; text-align: left; border-bottom: 1px solid #30363d;
    font-size: 0.9em;
}
th { background: #21262d; color: #f0f6fc; cursor: pointer; }
th.asc::after { content: " ▲"; color: #58a6ff; }
th.desc::after { content: " ▼"; color: #58a6ff; }
tr:hover { background: #1f242b; }
.header {
    background: #161b22; border: 1px solid #30363d; border-radius: 6px;
    padding: 1rem 1.5rem; margin-bottom: 2rem;
}
.header-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 0.75rem;
}
.kv { display: flex; flex-direction: column; }
.kv-label { font-size: 0.75em; color: #8b949e; text-transform: uppercase; }
.kv-value { font-size: 1.1em; color: #f0f6fc; font-weight: 600; }
.badge {
    display: inline-block; padding: 0.1em 0.5em; border-radius: 10px;
    font-size: 0.75em; font-weight: 600;
}
.badge.ok { background: #1a7f37; color: #ffffff; }
.badge.fail { background: #da3633; color: #ffffff; }
.badge.warn { background: #d29922; color: #24292f; }
.badge.neutral { background: #30363d; color: #c9d1d9; }
.badge.guardian { background: #8957e5; color: #ffffff; }
.badge.inference { background: #1f6feb; color: #ffffff; }
.badge.stream { background: #238636; color: #ffffff; }
.filters {
    display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 1em 0;
    padding: 0.75em; background: #161b22; border-radius: 6px;
    border: 1px solid #30363d;
}
.filters label {
    cursor: pointer; padding: 0.25em 0.5em; border-radius: 4px;
    background: #21262d; font-size: 0.85em;
    display: inline-flex; align-items: center; gap: 0.25em;
}
.filters input[type="checkbox"] { margin: 0; }
#search {
    width: 100%; padding: 0.5em 0.75em; background: #0d1117;
    color: #c9d1d9; border: 1px solid #30363d; border-radius: 4px;
    font-size: 0.95em; font-family: inherit; margin-bottom: 0.5em;
}
#search:focus { outline: none; border-color: #58a6ff; }
.timeline-entry {
    display: grid; grid-template-columns: 7em 7em 8em 1fr;
    gap: 0.75em; padding: 0.35em 0.5em; border-bottom: 1px solid #21262d;
    font-size: 0.85em; font-family: ui-monospace, monospace;
}
.timeline-entry.level-WARNING { background: rgba(210, 153, 34, 0.1); }
.timeline-entry.level-ERROR { background: rgba(218, 54, 51, 0.15); }
.timeline-entry.level-DEBUG { opacity: 0.5; }
.timeline-entry:hover { background: #1f242b; }
.timeline-ts { color: #8b949e; }
.timeline-cat { color: #58a6ff; }
.timeline-op { color: #8957e5; }
details {
    margin: 0.5em 0; padding: 0.5em 1em; background: #161b22;
    border: 1px solid #30363d; border-radius: 4px;
}
details summary {
    cursor: pointer; font-weight: 600; color: #f0f6fc;
    user-select: none;
}
details summary:hover { color: #58a6ff; }
.stats-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 0.75rem; margin: 1em 0;
}
.stat-card {
    background: #161b22; border: 1px solid #30363d; border-radius: 4px;
    padding: 0.75em 1em;
}
.stat-value { font-size: 1.5em; font-weight: 700; color: #58a6ff; }
.stat-label { font-size: 0.8em; color: #8b949e; text-transform: uppercase; }
.footer {
    margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #30363d;
    color: #8b949e; font-size: 0.8em; text-align: center;
}
.hidden { display: none !important; }
"""

_JS = """
(function() {
    // Timeline filter state.
    const categoryFilter = new Set();
    const searchInput = document.getElementById('search');
    const timeline = document.getElementById('timeline');
    const filterBoxes = document.querySelectorAll(
        '.filters input[type=\"checkbox\"]'
    );

    function rebuildCategoryFilter() {
        categoryFilter.clear();
        filterBoxes.forEach(function(cb) {
            if (cb.checked) categoryFilter.add(cb.value);
        });
    }
    rebuildCategoryFilter();

    function applyFilters() {
        const query = (searchInput.value || '').toLowerCase().trim();
        const entries = timeline ? timeline.querySelectorAll('.timeline-entry') : [];
        for (const el of entries) {
            const cat = el.getAttribute('data-category') || '';
            const text = (el.textContent || '').toLowerCase();
            const catOK = categoryFilter.size === 0 || categoryFilter.has(cat);
            const queryOK = !query || text.includes(query);
            el.classList.toggle('hidden', !(catOK && queryOK));
        }
        // Also filter ops table.
        const opRows = document.querySelectorAll('table.ops tbody tr');
        for (const row of opRows) {
            const text = (row.textContent || '').toLowerCase();
            row.classList.toggle('hidden', !!query && !text.includes(query));
        }
    }
    if (searchInput) searchInput.addEventListener('input', applyFilters);
    filterBoxes.forEach(function(cb) {
        cb.addEventListener('change', function() {
            rebuildCategoryFilter();
            applyFilters();
        });
    });

    // Sortable tables: click a th to sort.
    document.querySelectorAll('table.sortable').forEach(function(tbl) {
        const ths = tbl.querySelectorAll('th');
        ths.forEach(function(th, idx) {
            th.addEventListener('click', function() {
                const dir = th.classList.contains('asc') ? 'desc' : 'asc';
                ths.forEach(function(x) {
                    x.classList.remove('asc');
                    x.classList.remove('desc');
                });
                th.classList.add(dir);
                const tbody = tbl.tBodies[0];
                if (!tbody) return;
                const rows = Array.prototype.slice.call(tbody.rows);
                rows.sort(function(a, b) {
                    const av = (a.cells[idx] || {}).textContent || '';
                    const bv = (b.cells[idx] || {}).textContent || '';
                    const an = parseFloat(av.replace(/[^0-9.\\-]/g, ''));
                    const bn = parseFloat(bv.replace(/[^0-9.\\-]/g, ''));
                    let r = 0;
                    if (!isNaN(an) && !isNaN(bn)) r = an - bn;
                    else r = av.localeCompare(bv);
                    return dir === 'asc' ? r : -r;
                });
                for (const r of rows) tbody.appendChild(r);
            });
        });
    });
})();
"""


def _h(text: Any) -> str:
    """HTML-escape + coerce to str."""
    if text is None:
        return ""
    return html.escape(str(text), quote=True)


def _fmt_duration(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _state_badge(state: str) -> str:
    s = state.lower()
    cls = {
        "applied": "ok",
        "failed": "fail",
        "blocked": "fail",
        "rolled_back": "fail",
        "gating": "warn",
        "validating": "warn",
        "applying": "warn",
        "sandboxing": "neutral",
        "planned": "neutral",
    }.get(s, "neutral")
    return f'<span class="badge {cls}">{_h(state or "?")}</span>'


def _render_html(data: ReplayData) -> str:
    """Compose the full replay.html document. Every section degrades
    cleanly when its source data is absent."""
    lines: List[str] = []
    lines.append("<!doctype html>")
    lines.append('<html lang="en"><head>')
    lines.append('<meta charset="utf-8">')
    lines.append(f'<title>Ouroboros Session Replay — {_h(data.session_id)}</title>')
    lines.append(f"<style>{_CSS}</style>")
    lines.append("</head><body>")

    lines.append('<div class="header">')
    lines.append(
        f'<h1>🐍 Ouroboros Session Replay</h1>'
    )
    lines.append('<div class="header-grid">')
    lines.append(_kv("session", data.session_id))
    lines.append(_kv("stop_reason", data.stop_reason or "—"))
    lines.append(_kv("duration", _fmt_duration(data.duration_s)))
    lines.append(_kv("started_at", data.started_at_iso or "—"))
    cost = data.summary.get("cost_total", 0.0)
    lines.append(_kv("cost_total", f"${float(cost or 0.0):.4f}"))
    stats = data.summary.get("stats") or {}
    if isinstance(stats, dict):
        lines.append(_kv("ops attempted", str(stats.get("attempted", 0))))
        lines.append(_kv("ops completed", str(stats.get("completed", 0))))
        lines.append(_kv("ops failed", str(stats.get("failed", 0))))
    lines.append('</div></div>')

    # Top-line stat cards.
    lines.append("<h2>Overview</h2>")
    lines.append('<div class="stats-grid">')
    lines.append(_stat_card("Events", str(len(data.events))))
    lines.append(_stat_card("Ops (ledger)", str(len(data.ops))))
    lines.append(_stat_card("Guardian findings", str(data.guardian_findings_count)))
    lines.append(_stat_card("Inference builds", str(data.inference_builds_count)))
    lines.append(_stat_card("Stream renders", str(data.stream_renders_count)))
    branch = data.summary.get("branch_stats") or {}
    if isinstance(branch, dict):
        commits = branch.get("commits", 0)
        lines.append(_stat_card("Commits", str(commits)))
    lines.append('</div>')

    # Cost breakdown.
    breakdown = data.summary.get("cost_breakdown") or {}
    if isinstance(breakdown, dict) and breakdown:
        lines.append("<h2>Cost breakdown</h2>")
        lines.append('<table class="sortable">')
        lines.append('<thead><tr><th>provider</th><th>usd</th></tr></thead><tbody>')
        for provider, amt in sorted(
            breakdown.items(), key=lambda kv: float(kv[1] or 0), reverse=True,
        ):
            lines.append(
                f'<tr><td>{_h(provider)}</td>'
                f'<td>${float(amt or 0):.4f}</td></tr>'
            )
        lines.append('</tbody></table>')

    # Ops table.
    if data.ops:
        lines.append("<h2>Ops</h2>")
        lines.append(
            '<table class="ops sortable">'
            '<thead><tr>'
            '<th>op</th><th>final state</th><th>risk tier</th>'
            '<th>target files</th><th>commit</th><th>goal</th>'
            '</tr></thead><tbody>'
        )
        for op in data.ops:
            targets = ", ".join(op.target_files[:3])
            if len(op.target_files) > 3:
                targets += f" +{len(op.target_files) - 3}"
            commit_short = op.commit_hash[:10] if op.commit_hash else "—"
            lines.append(
                '<tr>'
                f'<td><code>{_h(op.short_op_id)}</code></td>'
                f'<td>{_state_badge(op.final_state)}</td>'
                f'<td>{_h(op.risk_tier or "—")}</td>'
                f'<td>{_h(targets or "—")}</td>'
                f'<td><code>{_h(commit_short)}</code></td>'
                f'<td>{_h((op.goal or "")[:80])}</td>'
                '</tr>'
            )
        lines.append('</tbody></table>')

        # Collapsible per-op ledger trail.
        lines.append("<h3>Op phase trails</h3>")
        for op in data.ops:
            lines.append(f'<details>')
            lines.append(
                f'<summary><code>{_h(op.short_op_id)}</code> '
                f'— {_state_badge(op.final_state)} — '
                f'{_h((op.goal or "")[:70])}</summary>'
            )
            lines.append('<pre>')
            for i, phase in enumerate(op.phases, 1):
                lines.append(f"  {i:>2}. {_h(phase)}")
            lines.append('</pre>')
            lines.append('</details>')

    # Guardian + inference summaries — carry detail from individual
    # INFO lines so the operator doesn't have to scroll the timeline.
    guardian_events = [e for e in data.events if e.category == "guardian"]
    if guardian_events:
        lines.append("<h2>SemanticGuardian findings</h2>")
        lines.append(
            '<table class="sortable"><thead><tr>'
            '<th>ts</th><th>op</th><th>findings</th>'
            '<th>hard</th><th>soft</th>'
            '<th>risk before → after</th><th>patterns</th>'
            '</tr></thead><tbody>'
        )
        for ev in guardian_events:
            fields = ev.fields
            lines.append(
                '<tr>'
                f'<td>{_h(ev.timestamp)}</td>'
                f'<td><code>{_h(ev.op_id[:18] if ev.op_id else "—")}</code></td>'
                f'<td>{_h(fields.get("findings", "0"))}</td>'
                f'<td>{_h(fields.get("hard", "0"))}</td>'
                f'<td>{_h(fields.get("soft", "0"))}</td>'
                f'<td>{_h(fields.get("risk_before", ""))} → '
                f'{_h(fields.get("risk_after", ""))}</td>'
                f'<td>{_h(fields.get("patterns", ""))}</td>'
                '</tr>'
            )
        lines.append('</tbody></table>')

    inference_events = [e for e in data.events if e.category == "inference"]
    if inference_events:
        lines.append("<h2>Goal inference trajectory</h2>")
        lines.append(
            '<table class="sortable"><thead><tr>'
            '<th>ts</th><th>samples</th><th>hypotheses</th>'
            '<th>top_conf</th><th>build_ms</th>'
            '</tr></thead><tbody>'
        )
        for ev in inference_events:
            fields = ev.fields
            lines.append(
                '<tr>'
                f'<td>{_h(ev.timestamp)}</td>'
                f'<td>{_h(fields.get("samples", ""))}</td>'
                f'<td>{_h(fields.get("hypotheses", ""))}</td>'
                f'<td>{_h(fields.get("top_conf", ""))}</td>'
                f'<td>{_h(fields.get("build_ms", ""))}</td>'
                '</tr>'
            )
        lines.append('</tbody></table>')

    # Timeline — every INFO event from debug.log with filter + search.
    lines.append("<h2>Event timeline</h2>")
    lines.append('<input type="text" id="search" placeholder="search events, ops, files…">')
    lines.append('<div class="filters">')
    for cat, count in sorted(data.category_counts.items(), key=lambda kv: -kv[1]):
        lines.append(
            '<label>'
            f'<input type="checkbox" value="{_h(cat)}" checked>'
            f'{_h(cat)} <span class="badge neutral">{count}</span>'
            '</label>'
        )
    lines.append('</div>')

    lines.append('<div id="timeline">')
    # Render a bounded number of events inline to keep the page small
    # for very long sessions. Operators can grep debug.log directly for
    # more detail.
    MAX_EVENTS = 2000
    rendered = data.events[:MAX_EVENTS]
    for ev in rendered:
        level_class = f"level-{ev.level}"
        msg_short = ev.message[:240] + (
            " …" if len(ev.message) > 240 else ""
        )
        lines.append(
            f'<div class="timeline-entry {level_class}" '
            f'data-category="{_h(ev.category)}">'
            f'<span class="timeline-ts">{_h(ev.timestamp)}</span>'
            f'<span class="timeline-cat">{_h(ev.category)}</span>'
            f'<span class="timeline-op">{_h(ev.op_id[:18] if ev.op_id else "—")}</span>'
            f'<span class="timeline-msg">{_h(msg_short)}</span>'
            '</div>'
        )
    if len(data.events) > MAX_EVENTS:
        lines.append(
            f'<div class="timeline-entry level-DEBUG">'
            f'<span></span><span></span><span></span>'
            f'<span>… {len(data.events) - MAX_EVENTS} more events elided '
            f'(page cap {MAX_EVENTS}; grep debug.log for full trace)</span>'
            '</div>'
        )
    lines.append('</div>')

    # Footer.
    lines.append('<div class="footer">')
    built = datetime.now().isoformat(timespec="seconds")
    lines.append(
        f'rendered by ouroboros.battle_test.session_replay @ {_h(built)}'
    )
    lines.append('</div>')
    lines.append(f"<script>{_JS}</script>")
    lines.append("</body></html>")
    return "\n".join(lines)


def _kv(label: str, value: str) -> str:
    return (
        f'<div class="kv">'
        f'<span class="kv-label">{_h(label)}</span>'
        f'<span class="kv-value">{_h(value)}</span>'
        f'</div>'
    )


def _stat_card(label: str, value: str) -> str:
    return (
        f'<div class="stat-card">'
        f'<div class="stat-value">{_h(value)}</div>'
        f'<div class="stat-label">{_h(label)}</div>'
        f'</div>'
    )


def _render_minimal_fallback(data: ReplayData) -> str:
    """Bare-minimum HTML when the full renderer raises. Keeps session
    artifacts auditable even under pathological conditions."""
    return (
        "<!doctype html><html><head>"
        f"<title>Session {_h(data.session_id)}</title>"
        "</head><body>"
        f"<h1>Session Replay (minimal fallback)</h1>"
        f"<p>session_id: <code>{_h(data.session_id)}</code></p>"
        f"<p>stop_reason: <code>{_h(data.stop_reason)}</code></p>"
        f"<p>duration_s: <code>{data.duration_s:.1f}</code></p>"
        f"<p>events: {len(data.events)}</p>"
        f"<p>ops: {len(data.ops)}</p>"
        "<p>full renderer failed; see debug.log for the error trace.</p>"
        "</body></html>"
    )

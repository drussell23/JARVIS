"""UserPreferenceMemory — persistent typed memory across O+V sessions.

Gives Ouroboros+Venom (O+V) the ability to remember facts about the user,
honor their explicit preferences and corrections, and enforce forbidden-path
policy across autonomous operations. Modeled on the Claude Code auto-memory
system (typed .md files with frontmatter + a human-readable index), adapted
to O+V's deterministic-first pipeline.

Manifesto alignment
-------------------

§4 (Synthetic soul) — episodic awareness across sessions. The organism
remembers who it is working with and what they have taught it.

§6 (Threshold-triggered neuroplasticity) — the store learns from
postmortems: when a human rejects an approval, the stated reason becomes
a feedback memory that informs future generations on similar work.

§7 (Absolute observability) — every memory is a plain .md file on disk;
any human can read, edit, or delete the organism's beliefs. No opaque
embeddings, no hidden state.

Memory types
------------

========== ============================================================
type       meaning
========== ============================================================
USER       Facts about the user: role, expertise, preferences, goals.
FEEDBACK   Corrections or validated approaches. Structured with a
           *why* (the reason) and *how_to_apply* (the trigger condition).
           Auto-extracted from approval rejections.
PROJECT    Ongoing work context. Absolute dates only (no "last Thursday").
           Decays fast — prefer verification over recall.
REFERENCE  Pointers to external systems: dashboards, ticket boards, docs.
FORBIDDEN  Hard path policy. Matches substring against the repo-relative
_PATH      POSIX path. Enforced at the ToolExecutor protected-path layer
           via ``register_protected_path_provider``.
STYLE      Code-style and communication preferences. Injected into every
           generation prompt.
========== ============================================================

Storage layout
--------------

::

    .jarvis/user_preferences/
        MEMORY.md                # human-readable index, rebuilt on every write
        user_role.md             # one .md per memory
        feedback_no_mock_db.md
        forbidden_path_auth.md
        ...

Every memory file carries YAML frontmatter (``---`` delimited). The body
below the frontmatter is free-form markdown. Files are the source of
truth — the index is rebuilt deterministically from them. An external
edit to a memory file is immediately reflected on next ``load()``.

Integration
-----------

1. **StrategicDirection** — ``StrategicDirectionService`` accepts an
   optional ``user_prefs`` param. ``format_for_prompt(ctx)`` appends a
   scoped "User Preferences" section filtered by relevance to the op.

2. **ToolExecutor forbidden paths** — the store registers itself as a
   ``protected_path_provider`` via
   ``tool_executor.register_protected_path_provider``. Every mutating
   call (edit_file / write_file / delete_file) consults the provider
   in addition to the hardcoded list and ``JARVIS_VENOM_PROTECTED_PATHS``.

3. **Postmortem learning** — the orchestrator calls
   ``store.record_approval_rejection(ctx, reason)`` when the human
   rejects an APPROVAL_REQUIRED operation. A FEEDBACK memory is created
   with the stated reason in the ``why`` field.

Boundary principle
------------------

Deterministic: file parsing, frontmatter round-trip, path matching,
relevance scoring (path overlap + tag match + type weight). All zero
model inference. The store never talks to an LLM.

Agentic: how the generation prompt *uses* the injected memories. That
lives downstream in the provider's prompt template, not here.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


_MEMORY_DIR = ".jarvis/user_preferences"
_INDEX_FILENAME = "MEMORY.md"

# Max memories returned by ``find_relevant`` — keeps prompt injection bounded.
_DEFAULT_RELEVANCE_LIMIT = int(
    os.environ.get("JARVIS_USER_PREF_RELEVANCE_LIMIT", "8")
)

# Scoring weights. Higher = stronger bias. Tuned so a direct path match
# outranks a tag match which outranks a pure type relevance bonus.
_SCORE_PATH_MATCH = 10
_SCORE_TAG_MATCH = 4
_SCORE_TYPE_BONUS_USER = 2
_SCORE_TYPE_BONUS_STYLE = 3
_SCORE_TYPE_BONUS_PROJECT = 1

# Frontmatter lexer pattern — YAML-like but we only parse scalar and list values.
_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?P<body>.*?)\n---\s*\n?(?P<content>.*)$",
    re.DOTALL,
)

# Safe slug pattern: lowercase alnum + underscore. Everything else collapses.
_SLUG_SAFE_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Enums and dataclasses
# ---------------------------------------------------------------------------


class MemoryType(str, Enum):
    """Kind of memory — controls how it is rendered and scored."""

    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"
    FORBIDDEN_PATH = "forbidden_path"
    STYLE = "style"

    @classmethod
    def from_str(cls, raw: str) -> "MemoryType":
        """Lenient parser. Unknown values fall back to USER."""
        if not raw:
            return cls.USER
        normal = raw.strip().lower()
        for member in cls:
            if member.value == normal:
                return member
        return cls.USER


@dataclass(frozen=True)
class UserMemory:
    """A single persisted memory entry.

    Frozen so ``UserPreferenceStore`` can safely hand instances out to
    callers without risk of mutation. Use :meth:`replace` via the
    ``dataclasses.replace`` helper to produce edited copies.
    """

    id: str                                   # stable slug: {type}_{name_slug}
    type: MemoryType
    name: str
    description: str                          # one-line; used for relevance scoring
    content: str                              # body markdown
    why: str = ""                             # optional — feedback/project motivation
    how_to_apply: str = ""                    # optional — when this memory applies
    source: str = "user"                      # "user" | "postmortem:op-id" | ...
    tags: Tuple[str, ...] = ()
    paths: Tuple[str, ...] = ()               # path scope (forbidden_path uses this)
    created_at: str = ""                      # ISO 8601 UTC
    updated_at: str = ""                      # ISO 8601 UTC

    def matches_path(self, rel_path: str) -> bool:
        """Substring match over ``paths`` — used by forbidden-path checks."""
        if not self.paths or not rel_path:
            return False
        norm = rel_path.replace("\\", "/")
        for p in self.paths:
            if p and p in norm:
                return True
        return False

    def to_markdown(self) -> str:
        """Serialise to the on-disk .md format (frontmatter + body)."""
        lines: List[str] = ["---"]
        lines.append(f"id: {self.id}")
        lines.append(f"type: {self.type.value}")
        lines.append(f"name: {_yaml_escape(self.name)}")
        lines.append(f"description: {_yaml_escape(self.description)}")
        lines.append(f"source: {_yaml_escape(self.source or 'user')}")
        if self.tags:
            lines.append(f"tags: [{', '.join(_yaml_escape(t) for t in self.tags)}]")
        if self.paths:
            lines.append(f"paths: [{', '.join(_yaml_escape(p) for p in self.paths)}]")
        if self.created_at:
            lines.append(f"created_at: {self.created_at}")
        if self.updated_at:
            lines.append(f"updated_at: {self.updated_at}")
        lines.append("---")
        lines.append("")
        body_parts: List[str] = []
        if self.content.strip():
            body_parts.append(self.content.strip())
        if self.why.strip():
            body_parts.append(f"**Why:** {self.why.strip()}")
        if self.how_to_apply.strip():
            body_parts.append(f"**How to apply:** {self.how_to_apply.strip()}")
        lines.append("\n\n".join(body_parts) if body_parts else "")
        if not lines[-1].endswith("\n"):
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Global protected-path provider hook (set by UserPreferenceStore on load)
# ---------------------------------------------------------------------------


_PROTECTED_PATH_PROVIDER: Optional[Callable[[], Iterable[str]]] = None
_PROTECTED_PATH_LOCK = threading.Lock()


def register_protected_path_provider(
    provider: Optional[Callable[[], Iterable[str]]],
) -> None:
    """Install (or clear) a callable that returns extra protected-path substrings.

    ``ToolExecutor._is_protected_path`` consults this provider on every
    mutating call. Passing ``None`` clears the hook (used by tests).

    The provider must return quickly and must never raise — callers
    wrap it in ``try/except`` but a misbehaving provider still makes
    every edit slower.
    """
    global _PROTECTED_PATH_PROVIDER
    with _PROTECTED_PATH_LOCK:
        _PROTECTED_PATH_PROVIDER = provider


def get_protected_path_provider() -> Optional[Callable[[], Iterable[str]]]:
    """Return the currently registered provider, or None."""
    with _PROTECTED_PATH_LOCK:
        return _PROTECTED_PATH_PROVIDER


# ---------------------------------------------------------------------------
# UserPreferenceStore
# ---------------------------------------------------------------------------


class UserPreferenceStore:
    """Persistent, typed memory store for O+V.

    One instance per process — the store is thread-safe for reads and
    writes via an internal lock. Call :meth:`load` once at startup (or
    allow the lazy auto-load in the constructor) and reuse the instance.

    Parameters
    ----------
    project_root:
        Repo root. Memories live in ``<project_root>/.jarvis/user_preferences``.
    auto_register_protected_paths:
        When ``True`` (default) the store installs itself as the global
        protected-path provider for ``tool_executor``. Set to ``False``
        in tests that want to manage the hook manually.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        auto_register_protected_paths: bool = True,
    ) -> None:
        self._root = Path(project_root).resolve()
        self._dir = self._root / _MEMORY_DIR
        self._memories: Dict[str, UserMemory] = {}
        self._lock = threading.RLock()
        self._auto_register = auto_register_protected_paths
        self.load()
        if auto_register_protected_paths:
            register_protected_path_provider(self._provide_protected_paths)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Scan the memory directory and rebuild the in-memory index.

        Silent on missing directory (first-run case). Corrupt files are
        logged and skipped — one bad memory must never break the loader.
        """
        with self._lock:
            self._memories.clear()
            if not self._dir.is_dir():
                return
            for entry in sorted(self._dir.iterdir()):
                if not entry.is_file():
                    continue
                if entry.name == _INDEX_FILENAME:
                    continue
                if entry.suffix != ".md":
                    continue
                try:
                    raw = entry.read_text(encoding="utf-8", errors="replace")
                    memory = self._parse_markdown(raw, fallback_id=entry.stem)
                    if memory is not None:
                        self._memories[memory.id] = memory
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[UserPreferenceStore] Failed to parse %s: %s",
                        entry.name,
                        exc,
                    )
            logger.info(
                "[UserPreferenceStore] Loaded %d memories from %s",
                len(self._memories),
                self._dir,
            )

    def reload(self) -> None:
        """Alias for :meth:`load` — re-reads from disk, discarding caches."""
        self.load()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(
        self,
        memory_type: MemoryType,
        name: str,
        description: str,
        content: str = "",
        *,
        why: str = "",
        how_to_apply: str = "",
        source: str = "user",
        tags: Sequence[str] = (),
        paths: Sequence[str] = (),
    ) -> UserMemory:
        """Create or replace a memory. Returns the persisted instance.

        If a memory with the same ``id`` already exists, its content is
        overwritten *in place* — this is the "upsert" behaviour the
        postmortem hooks rely on to deduplicate repeat rejections.
        """
        if not name or not name.strip():
            raise ValueError("UserMemory name cannot be empty")
        if not description or not description.strip():
            raise ValueError("UserMemory description cannot be empty")

        mem_id = _build_memory_id(memory_type, name)
        now = _utc_now_iso()
        with self._lock:
            existing = self._memories.get(mem_id)
            memory = UserMemory(
                id=mem_id,
                type=memory_type,
                name=name.strip(),
                description=description.strip(),
                content=content.strip(),
                why=why.strip(),
                how_to_apply=how_to_apply.strip(),
                source=source.strip() or "user",
                tags=tuple(t.strip() for t in tags if t and t.strip()),
                paths=tuple(p.strip() for p in paths if p and p.strip()),
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            self._memories[mem_id] = memory
            self._persist_memory(memory)
            self._rebuild_index()
        return memory

    def get(self, mem_id: str) -> Optional[UserMemory]:
        """Return the memory with the given id, or ``None``."""
        with self._lock:
            return self._memories.get(mem_id)

    def update(self, mem_id: str, **changes: Any) -> Optional[UserMemory]:
        """Shallow-update an existing memory.

        Returns the updated instance or ``None`` if the id is unknown.
        Only these fields are mutable: description, content, why,
        how_to_apply, tags, paths, source. Attempting to change ``id``,
        ``type``, ``name``, or ``created_at`` is silently ignored — those
        are the identity of the memory. Create a new one instead.
        """
        _MUTABLE_FIELDS = {
            "description", "content", "why", "how_to_apply",
            "tags", "paths", "source",
        }
        with self._lock:
            existing = self._memories.get(mem_id)
            if existing is None:
                return None
            filtered: Dict[str, Any] = {}
            for k, v in changes.items():
                if k not in _MUTABLE_FIELDS:
                    continue
                if k in ("tags", "paths"):
                    filtered[k] = tuple(
                        str(x).strip() for x in (v or ()) if str(x).strip()
                    )
                else:
                    filtered[k] = str(v) if v is not None else ""
            if not filtered:
                return existing
            updated = replace(
                existing,
                updated_at=_utc_now_iso(),
                **filtered,
            )
            self._memories[mem_id] = updated
            self._persist_memory(updated)
            self._rebuild_index()
            return updated

    def delete(self, mem_id: str) -> bool:
        """Remove a memory. Returns True if it existed."""
        with self._lock:
            existing = self._memories.pop(mem_id, None)
            if existing is None:
                return False
            try:
                path = self._dir / f"{mem_id}.md"
                if path.exists():
                    path.unlink()
            except OSError as exc:
                logger.warning(
                    "[UserPreferenceStore] Failed to unlink %s: %s",
                    mem_id,
                    exc,
                )
            self._rebuild_index()
            return True

    def list_all(self) -> List[UserMemory]:
        """Return all memories, sorted by type then id."""
        with self._lock:
            return sorted(
                self._memories.values(),
                key=lambda m: (m.type.value, m.id),
            )

    def find_by_type(self, memory_type: MemoryType) -> List[UserMemory]:
        """Return all memories of the given type."""
        with self._lock:
            return [m for m in self._memories.values() if m.type is memory_type]

    # ------------------------------------------------------------------
    # Forbidden-path lookup (ToolExecutor integration)
    # ------------------------------------------------------------------

    def find_forbidden_for_path(self, rel_path: str) -> List[UserMemory]:
        """Return FORBIDDEN_PATH memories whose ``paths`` match ``rel_path``."""
        with self._lock:
            return [
                m
                for m in self._memories.values()
                if m.type is MemoryType.FORBIDDEN_PATH and m.matches_path(rel_path)
            ]

    def _provide_protected_paths(self) -> List[str]:
        """Callback for the tool_executor hook — returns substring list."""
        with self._lock:
            out: List[str] = []
            for m in self._memories.values():
                if m.type is MemoryType.FORBIDDEN_PATH:
                    out.extend(m.paths)
            return out

    # ------------------------------------------------------------------
    # Relevance scoring (StrategicDirection integration)
    # ------------------------------------------------------------------

    def find_relevant(
        self,
        *,
        target_files: Sequence[str] = (),
        description: str = "",
        risk_tier: str = "",
        limit: int = _DEFAULT_RELEVANCE_LIMIT,
    ) -> List[UserMemory]:
        """Return the top-``limit`` memories scored for the current op.

        Scoring dimensions (all additive):

        1. **Path overlap** — any ``paths`` substring matches any
           ``target_files`` entry. Strongest signal.
        2. **Tag match** — any tag appears in the description (case
           insensitive) or matches the risk tier.
        3. **Type bonus** — USER/STYLE memories always get a baseline
           bonus so they are never pushed out of the prompt by
           noisier feedback entries.

        Ties are broken by updated_at descending (freshest wins).
        """
        desc_lower = (description or "").lower()
        risk_lower = (risk_tier or "").lower()
        scored: List[Tuple[int, str, UserMemory]] = []

        with self._lock:
            for mem in self._memories.values():
                score = self._score_memory(
                    mem,
                    target_files=target_files,
                    desc_lower=desc_lower,
                    risk_lower=risk_lower,
                )
                if score <= 0:
                    continue
                scored.append((score, mem.updated_at or "", mem))

        # sort by (-score, -updated_at) so best-and-freshest wins
        scored.sort(key=lambda t: (-t[0], _negate_iso(t[1])))
        return [m for _, _, m in scored[: max(0, int(limit))]]

    @staticmethod
    def _score_memory(
        mem: UserMemory,
        *,
        target_files: Sequence[str],
        desc_lower: str,
        risk_lower: str,
    ) -> int:
        score = 0

        # Type baseline bonuses.
        if mem.type is MemoryType.USER:
            score += _SCORE_TYPE_BONUS_USER
        elif mem.type is MemoryType.STYLE:
            score += _SCORE_TYPE_BONUS_STYLE
        elif mem.type is MemoryType.PROJECT:
            score += _SCORE_TYPE_BONUS_PROJECT

        # Path overlap — strongest individual signal.
        if mem.paths and target_files:
            for tf in target_files:
                norm = tf.replace("\\", "/") if tf else ""
                if not norm:
                    continue
                for p in mem.paths:
                    if p and p in norm:
                        score += _SCORE_PATH_MATCH
                        break

        # Tag match against description or risk tier.
        if mem.tags:
            for tag in mem.tags:
                tag_l = tag.lower()
                if not tag_l:
                    continue
                if tag_l in desc_lower or tag_l == risk_lower:
                    score += _SCORE_TAG_MATCH

        # Forbidden-path memories ALWAYS surface when they match a target.
        if mem.type is MemoryType.FORBIDDEN_PATH and mem.paths:
            for tf in target_files:
                norm = tf.replace("\\", "/") if tf else ""
                if any(p and p in norm for p in mem.paths):
                    score += _SCORE_PATH_MATCH * 2
                    break

        return score

    # ------------------------------------------------------------------
    # Prompt rendering (StrategicDirection integration)
    # ------------------------------------------------------------------

    def format_for_prompt(
        self,
        *,
        target_files: Sequence[str] = (),
        description: str = "",
        risk_tier: str = "",
        limit: int = _DEFAULT_RELEVANCE_LIMIT,
    ) -> str:
        """Render a compact "User Preferences" section for generation prompts.

        Returns an empty string when no memories are relevant — the
        caller should *not* inject an empty section. Every entry lists
        the type, name, description, and (when present) the why /
        how_to_apply annotations that make the memory actionable.
        """
        memories = self.find_relevant(
            target_files=target_files,
            description=description,
            risk_tier=risk_tier,
            limit=limit,
        )
        if not memories:
            return ""

        lines: List[str] = [
            "## User Preferences (persistent memory)\n",
            (
                "The following facts were recorded across prior sessions. "
                "They represent the user's explicit preferences, feedback, "
                "forbidden paths, and external references. Honour them "
                "without asking — contradicting a memory is a rejection "
                "cause.\n"
            ),
        ]
        for mem in memories:
            tag_str = f" [{', '.join(mem.tags)}]" if mem.tags else ""
            lines.append(
                f"- **{mem.type.value}:{mem.name}**{tag_str} — {mem.description}"
            )
            if mem.why:
                lines.append(f"    - Why: {mem.why}")
            if mem.how_to_apply:
                lines.append(f"    - How to apply: {mem.how_to_apply}")
            if mem.type is MemoryType.FORBIDDEN_PATH and mem.paths:
                lines.append(
                    f"    - HARD BLOCK on paths: {', '.join(mem.paths)}"
                )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Postmortem learning hooks
    # ------------------------------------------------------------------

    def record_approval_rejection(
        self,
        *,
        op_id: str,
        description: str,
        target_files: Sequence[str],
        reason: str,
        approver: str = "human",
    ) -> Optional[UserMemory]:
        """Convert an approval rejection into a FEEDBACK memory.

        Deduplicates by op signature: repeated rejections of the same
        op-description shape update the existing memory rather than
        piling up identical entries. Returns the persisted memory, or
        ``None`` if the inputs were too sparse to build a meaningful
        entry.
        """
        cleaned_reason = (reason or "").strip()
        if not cleaned_reason:
            return None
        desc_short = (description or "unknown operation").strip()[:100]
        name = f"rejection_{_slug(desc_short, max_len=40)}"
        if not name.strip("_"):
            name = f"rejection_{op_id}"
        how = "When a future op resembles the rejected one, avoid repeating it."
        paths = tuple(p for p in target_files[:4] if p)
        try:
            return self.add(
                memory_type=MemoryType.FEEDBACK,
                name=name,
                description=f"Rejected approach: {desc_short}",
                content=(
                    "This memory was auto-extracted from an approval rejection. "
                    "It encodes the user's stated reason for blocking the "
                    "operation and should be treated as a binding constraint "
                    "on future work with similar shape."
                ),
                why=cleaned_reason[:400],
                how_to_apply=how,
                source=f"approval_reject:{op_id}:{approver or 'human'}",
                tags=("rejection", "approval"),
                paths=paths,
            )
        except ValueError:
            logger.debug(
                "[UserPreferenceStore] Skipped rejection memory for op %s — "
                "inputs too sparse",
                op_id,
            )
            return None

    def record_rollback(
        self,
        *,
        op_id: str,
        description: str,
        target_files: Sequence[str],
        failure_class: str,
        summary: str,
    ) -> Optional[UserMemory]:
        """Convert a VERIFY-phase rollback into a FEEDBACK memory.

        ``failure_class`` should be one of ``test`` / ``build`` /
        ``infra`` / ``budget`` as per ``ValidationResult.failure_class``.
        The resulting memory is tagged with the class so future ops can
        filter by it.
        """
        cleaned = (summary or "").strip()
        if not cleaned:
            return None
        desc_short = (description or "unknown operation").strip()[:100]
        name = f"rollback_{failure_class}_{_slug(desc_short, max_len=30)}"
        paths = tuple(p for p in target_files[:4] if p)
        try:
            return self.add(
                memory_type=MemoryType.FEEDBACK,
                name=name,
                description=f"Rolled back ({failure_class}): {desc_short}",
                content=(
                    "This memory was auto-extracted from a post-APPLY "
                    "rollback. A future op that touches similar files "
                    "should first verify whether this failure mode still "
                    "applies before re-attempting the same approach."
                ),
                why=cleaned[:400],
                how_to_apply=(
                    "Check the target files for the same failure mode "
                    "before re-attempting."
                ),
                source=f"rollback:{op_id}",
                tags=("rollback", failure_class or "unknown"),
                paths=paths,
            )
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Internals — persistence
    # ------------------------------------------------------------------

    def _persist_memory(self, memory: UserMemory) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"{memory.id}.md"
            path.write_text(memory.to_markdown(), encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "[UserPreferenceStore] Failed to persist %s: %s",
                memory.id,
                exc,
            )

    def _rebuild_index(self) -> None:
        """Rewrite MEMORY.md — one line per memory, grouped by type."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            lines: List[str] = [
                "# User Preference Memory — Index",
                "",
                (
                    "Auto-generated by `UserPreferenceStore`. "
                    "Edit individual memory files directly; this index is "
                    "rebuilt on every write."
                ),
                "",
            ]
            grouped: Dict[MemoryType, List[UserMemory]] = {}
            for mem in self._memories.values():
                grouped.setdefault(mem.type, []).append(mem)
            for mem_type in MemoryType:
                items = grouped.get(mem_type, [])
                if not items:
                    continue
                lines.append(f"## {mem_type.value}")
                lines.append("")
                for mem in sorted(items, key=lambda m: m.name):
                    lines.append(
                        f"- [{mem.name}]({mem.id}.md) — {mem.description}"
                    )
                lines.append("")
            (self._dir / _INDEX_FILENAME).write_text(
                "\n".join(lines), encoding="utf-8"
            )
        except OSError as exc:
            logger.debug("[UserPreferenceStore] Index rebuild failed: %s", exc)

    # ------------------------------------------------------------------
    # Internals — parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_markdown(raw: str, *, fallback_id: str) -> Optional[UserMemory]:
        """Parse an on-disk memory file into a ``UserMemory``.

        Tolerant — missing fields become defaults. Returns ``None`` only
        if the required identity fields (id, type, name, description)
        cannot be resolved even with fallbacks.
        """
        match = _FRONTMATTER_RE.match(raw)
        if not match:
            return None
        header = _parse_frontmatter_body(match.group("body"))
        body = match.group("content").strip()

        mem_id = (header.get("id") or fallback_id or "").strip()
        mem_type = MemoryType.from_str(header.get("type", ""))
        name = (header.get("name") or "").strip()
        description = (header.get("description") or "").strip()
        if not mem_id or not name or not description:
            return None

        # Split body on "**Why:**" / "**How to apply:**" markers so the
        # structured annotations round-trip through the file format.
        content, why, how_to_apply = _split_annotations(body)

        tags = _parse_list_value(header.get("tags", ""))
        paths = _parse_list_value(header.get("paths", ""))

        return UserMemory(
            id=mem_id,
            type=mem_type,
            name=name,
            description=description,
            content=content,
            why=why,
            how_to_apply=how_to_apply,
            source=(header.get("source") or "user").strip(),
            tags=tuple(tags),
            paths=tuple(paths),
            created_at=(header.get("created_at") or "").strip(),
            updated_at=(header.get("updated_at") or "").strip(),
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(text: str, *, max_len: int = 60) -> str:
    """Produce a filesystem-safe slug from arbitrary text."""
    if not text:
        return ""
    lowered = text.strip().lower()
    slug = _SLUG_SAFE_RE.sub("_", lowered).strip("_")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("_")
    return slug


def _build_memory_id(memory_type: MemoryType, name: str) -> str:
    """Build a stable id from type + name."""
    slug = _slug(name, max_len=60)
    if not slug:
        slug = "unnamed"
    return f"{memory_type.value}_{slug}"


def _yaml_escape(value: str) -> str:
    """Minimal YAML escaping for scalar values written to frontmatter.

    We only support the subset we emit — quoted strings are needed when
    the value contains a colon, a leading dash, or a newline. Everything
    else passes through.
    """
    s = str(value) if value else ""
    needs_quote = (
        ":" in s
        or s.startswith("-")
        or s.startswith("#")
        or "\n" in s
        or s.strip() != s
        or s == ""
    )
    if not needs_quote:
        return s
    escaped = s.replace("\\", "\\\\").replace("\"", "\\\"")
    return f"\"{escaped}\""


def _parse_frontmatter_body(body: str) -> Dict[str, str]:
    """Extract ``key: value`` pairs from a YAML-ish frontmatter block.

    Intentionally simple — we only support scalars and the inline-list
    form ``[a, b, c]``. No multiline, no anchors, no nested maps.
    """
    out: Dict[str, str] = {}
    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("\"") and value.endswith("\"") and len(value) >= 2:
            value = value[1:-1].replace("\\\"", "\"").replace("\\\\", "\\")
        out[key] = value
    return out


def _parse_list_value(raw: str) -> List[str]:
    """Parse an inline list like ``[a, b, "c d"]``."""
    if not raw:
        return []
    stripped = raw.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        # Allow a bare comma-separated fallback for hand-edited files.
        return [s.strip() for s in stripped.split(",") if s.strip()]
    inner = stripped[1:-1]
    if not inner.strip():
        return []
    out: List[str] = []
    buf: List[str] = []
    in_quote = False
    for ch in inner:
        if ch == "\"":
            in_quote = not in_quote
            continue
        if ch == "," and not in_quote:
            piece = "".join(buf).strip()
            if piece:
                out.append(piece)
            buf = []
            continue
        buf.append(ch)
    piece = "".join(buf).strip()
    if piece:
        out.append(piece)
    return out


_WHY_MARKER = "**Why:**"
_HOW_MARKER = "**How to apply:**"


def _split_annotations(body: str) -> Tuple[str, str, str]:
    """Split a memory body into (content, why, how_to_apply).

    Round-trip partner of :meth:`UserMemory.to_markdown`. If the markers
    are absent, the whole body becomes ``content`` and the annotations
    are empty strings.
    """
    content, why, how = body, "", ""
    if _HOW_MARKER in content:
        content, _, how = content.partition(_HOW_MARKER)
        how = how.strip()
    if _WHY_MARKER in content:
        content, _, why = content.partition(_WHY_MARKER)
        why = why.strip()
    return content.strip(), why, how


def _negate_iso(iso: str) -> str:
    """Return a sort key such that *larger* iso strings come first.

    Used by :meth:`find_relevant` — we sort ascending on ``-score`` then
    on this negated-iso form so that ties in score are broken in favour
    of the freshest memory.
    """
    # Invert character codes so lexicographic ascending sort becomes descending.
    return "".join(chr(0x7F - (ord(c) & 0x7F)) for c in iso)

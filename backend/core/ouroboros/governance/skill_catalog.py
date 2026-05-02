"""
SkillCatalog + SkillInvoker + SkillMarketplace — Slices 2/3/4 bundled.
======================================================================

Three tightly-coupled primitives for the first-class skill system:

* :class:`SkillCatalog` — per-process registry of loaded
  :class:`SkillManifest` objects. Lookup by qualified name. Authority
  boundary: registrations require an authoritative source
  (``operator`` / ``orchestrator``); the model can INVOKE from the
  catalog but cannot register manifests.

* :class:`SkillInvoker` — resolves a manifest's ``entrypoint`` string
  to a callable, validates supplied args against the manifest's
  ``args_schema``, runs the handler, and returns a structured
  :class:`SkillInvocationOutcome`. Bounded output preview for SSE /
  audit.

* :class:`SkillMarketplace` — filesystem-backed discovery + install +
  remove. Skills live at ``<root>/<name>/manifest.yaml`` or
  ``<root>/<plugin>/<skill>/manifest.yaml``. Install copies (or
  symlinks) a source tree into the marketplace root; remove deletes
  it. Discovery walks the tree and auto-registers loaded manifests
  into the :class:`SkillCatalog`.

Plus a ``/skills`` REPL dispatcher and listener hooks for Slice 5's
IDE observability bridge.

Manifesto alignment
-------------------

* §1 — :meth:`SkillCatalog.register` refuses ``model`` source.
  :meth:`SkillInvoker.invoke` routes through the registered
  entrypoint regardless of who *started* the invocation, because
  invocation is not authority; the entrypoint was authored by the
  plugin author and blessed at register time by the operator.
* §5 — deterministic. Manifest parse, entrypoint resolution, arg
  validation are all pure code.
* §7 — fail-closed. Unknown skill → structured error; schema violation
  → :class:`SkillArgsError`; entrypoint import failure →
  :class:`SkillInvocationError`; handler raise → outcome with
  ``ok=False`` + error message.
* §8 — every register / unregister / install / invoke emits
  ``[SkillCatalog]`` / ``[SkillInvoker]`` INFO log line; Slice 5
  bridges to SSE.
"""
from __future__ import annotations

import asyncio
import enum
import importlib
import logging
import os
import shlex
import shutil
import textwrap
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple,
)

from backend.core.ouroboros.governance.skill_manifest import (
    SkillArgsError,
    SkillManifest,
    SkillManifestError,
    validate_args,
)
# Slice 2 (SkillRegistry-AutonomousReach): trigger lookup index.
# Public spec_matches_invocation is the SAME predicate
# compute_should_fire uses internally -- the catalog narrows
# candidates; the decision function still authoritatively decides
# fire/skip. No parallel decision path, no duplication.
from backend.core.ouroboros.governance.skill_trigger import (
    SkillInvocation,
    SkillTriggerKind,
    spec_matches_invocation,
)

logger = logging.getLogger("Ouroboros.SkillCatalog")


SKILL_CATALOG_SCHEMA_VERSION: str = "skill_catalog.v1"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SkillCatalogError(Exception):
    """Base for catalog-level errors."""


class SkillAuthorityError(SkillCatalogError):
    """Raised when a non-authoritative source tries to mutate state."""


class SkillInvocationError(Exception):
    """Raised when invocation infrastructure fails (import, lookup)."""


# ---------------------------------------------------------------------------
# Authority source (§1)
# ---------------------------------------------------------------------------


class SkillSource(str, enum.Enum):
    OPERATOR = "operator"
    ORCHESTRATOR = "orchestrator"
    # MODEL deliberately absent — write paths refuse.


_AUTHORITATIVE_SOURCES: FrozenSet[SkillSource] = frozenset({
    SkillSource.OPERATOR, SkillSource.ORCHESTRATOR,
})


# ---------------------------------------------------------------------------
# SkillCatalog
# ---------------------------------------------------------------------------


class SkillCatalog:
    """Per-process registry of :class:`SkillManifest` records.

    Looked up by :attr:`SkillManifest.qualified_name` (``plugin:skill``
    or bare ``skill``). Thread-safe. Listener hooks feed Slice 5's
    observability bridge.
    """

    def __init__(self, *, max_skills: int = 512) -> None:
        self._lock = threading.Lock()
        self._by_qualified_name: Dict[str, SkillManifest] = {}
        self._max = max(1, max_skills)
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        # ----- Slice 2 additive trigger lookup index -----
        # Maps SkillTriggerKind -> List[(qualified_name, spec_index)]
        # so :meth:`triggers_for_signal` is O(K) where K is the
        # candidate count for the invocation kind, not O(N x M)
        # over all manifests x specs. Stored by qualified_name (not
        # direct manifest ref) so unregister cleanup is a simple
        # filter; manifest is rehydrated from _by_qualified_name at
        # lookup time (defensive -- skip if it disappeared).
        self._triggers_by_kind: Dict[
            SkillTriggerKind, List[Tuple[str, int]]
        ] = {}

    # --- lifecycle -------------------------------------------------------

    def register(
        self,
        manifest: SkillManifest,
        *,
        source: SkillSource,
    ) -> SkillManifest:
        """Add a manifest. Raises on authority / duplicate / cap."""
        if source not in _AUTHORITATIVE_SOURCES:
            raise SkillAuthorityError(
                f"skill source {source!r} not authoritative"
            )
        if not isinstance(manifest, SkillManifest):
            raise SkillCatalogError(
                "manifest must be a SkillManifest instance"
            )
        qname = manifest.qualified_name
        with self._lock:
            if qname in self._by_qualified_name:
                raise SkillCatalogError(
                    f"skill already registered: {qname}"
                )
            if len(self._by_qualified_name) >= self._max:
                raise SkillCatalogError(
                    f"catalog cap {self._max} reached"
                )
            self._by_qualified_name[qname] = manifest
            # Index trigger specs (Slice 2 additive). Atomic with
            # the primary write so triggers_for_signal can never
            # observe a partially-registered manifest.
            self._index_manifest_triggers_locked(manifest)
        logger.info(
            "[SkillCatalog] registered qname=%s version=%s source=%s",
            qname, manifest.version, source.value,
        )
        self._fire("skill_registered", manifest)
        return manifest

    def unregister(self, qualified_name: str) -> bool:
        with self._lock:
            manifest = self._by_qualified_name.pop(qualified_name, None)
            if manifest is not None:
                # Cleanup trigger index atomically with the primary
                # delete so triggers_for_signal can never return an
                # entry whose manifest no longer exists.
                self._unindex_manifest_triggers_locked(qualified_name)
        if manifest is None:
            return False
        logger.info(
            "[SkillCatalog] unregistered qname=%s", qualified_name,
        )
        self._fire("skill_unregistered", manifest)
        return True

    def get(self, qualified_name: str) -> Optional[SkillManifest]:
        with self._lock:
            return self._by_qualified_name.get(qualified_name)

    def has(self, qualified_name: str) -> bool:
        with self._lock:
            return qualified_name in self._by_qualified_name

    def list_all(self) -> List[SkillManifest]:
        with self._lock:
            return sorted(
                self._by_qualified_name.values(),
                key=lambda m: m.qualified_name,
            )

    def list_by_namespace(
        self, namespace: Optional[str],
    ) -> List[SkillManifest]:
        with self._lock:
            return sorted(
                (
                    m for m in self._by_qualified_name.values()
                    if m.plugin_namespace == namespace
                ),
                key=lambda m: m.qualified_name,
            )

    # --- listeners -------------------------------------------------------

    def on_change(
        self, listener: Callable[[Dict[str, Any]], None],
    ) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsub

    def _fire(self, event_type: str, manifest: SkillManifest) -> None:
        payload = {
            "event_type": event_type,
            "qualified_name": manifest.qualified_name,
            "projection": manifest.project(),
        }
        for l in list(self._listeners):
            try:
                l(payload)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[SkillCatalog] listener exception on %s: %s",
                    event_type, exc,
                )

    # --- Slice 2: trigger lookup index -----------------------------------

    def _index_manifest_triggers_locked(
        self, manifest: SkillManifest,
    ) -> None:
        """Add every spec in ``manifest.trigger_specs`` to the
        kind-keyed index. MUST be called with ``self._lock`` held.

        Defensive: skips garbage entries (non-SkillTriggerSpec /
        invalid kind) without raising. Pre-arc manifests with the
        default empty ``trigger_specs`` tuple add zero entries.
        """
        try:
            specs = tuple(
                getattr(manifest, "trigger_specs", ()) or (),
            )
        except Exception:  # noqa: BLE001 -- defensive
            return
        qname = manifest.qualified_name
        for idx, spec in enumerate(specs):
            kind = getattr(spec, "kind", None)
            if not isinstance(kind, SkillTriggerKind):
                continue
            self._triggers_by_kind.setdefault(kind, []).append(
                (qname, idx),
            )

    def _unindex_manifest_triggers_locked(
        self, qualified_name: str,
    ) -> None:
        """Remove every entry referencing ``qualified_name`` from
        the kind-keyed index. MUST be called with ``self._lock``
        held. NEVER raises."""
        for kind in list(self._triggers_by_kind.keys()):
            entries = self._triggers_by_kind[kind]
            filtered = [e for e in entries if e[0] != qualified_name]
            if filtered:
                self._triggers_by_kind[kind] = filtered
            else:
                # Drop empty buckets so the index size matches the
                # actual trigger surface; keeps observability counts
                # honest.
                del self._triggers_by_kind[kind]

    def triggers_for_signal(
        self, invocation: SkillInvocation,
    ) -> List[Tuple[SkillManifest, int]]:
        """Return ``(manifest, spec_index)`` candidates whose
        trigger spec matches ``invocation``. NEVER raises.

        The catalog NARROWS the candidate set; the load-bearing
        fire/skip decision still belongs to
        :func:`compute_should_fire` (master flag, reach gate, risk
        gate). Callers (Slice 3 observer) should iterate the
        returned candidates and call ``compute_should_fire`` for
        each.

        Returns empty list when:
          * invocation isn't a :class:`SkillInvocation` instance
          * invocation kind isn't a :class:`SkillTriggerKind`
          * no manifest has a matching spec (the common-case quiet
            path -- 99%+ of signals)

        Defensive: re-checks each candidate's manifest still exists
        in :attr:`_by_qualified_name` before returning, so a race
        between unregister + lookup can't yield a phantom candidate.
        """
        try:
            if not isinstance(invocation, SkillInvocation):
                return []
            kind = invocation.triggered_by_kind
            if not isinstance(kind, SkillTriggerKind):
                return []
            with self._lock:
                # Snapshot the (qname, spec_index) entries for this
                # kind so we don't hold the lock during the
                # per-spec match check.
                entries = list(
                    self._triggers_by_kind.get(kind, ()),
                )
                # Snapshot the manifests too (manifests are frozen,
                # so capturing references is safe).
                manifest_snapshot = dict(self._by_qualified_name)
            out: List[Tuple[SkillManifest, int]] = []
            for qname, spec_index in entries:
                manifest = manifest_snapshot.get(qname)
                if manifest is None:
                    continue  # raced with unregister -- skip
                try:
                    specs = tuple(manifest.trigger_specs or ())
                except Exception:  # noqa: BLE001 -- defensive
                    continue
                if spec_index < 0 or spec_index >= len(specs):
                    continue
                spec = specs[spec_index]
                if spec_matches_invocation(spec, invocation):
                    out.append((manifest, spec_index))
            return out
        except Exception as exc:  # noqa: BLE001 -- last-resort
            logger.debug(
                "[SkillCatalog] triggers_for_signal degraded: %s",
                exc,
            )
            return []

    def trigger_index_counts(self) -> Dict[str, int]:
        """Return ``{kind_value: spec_count}`` snapshot of the
        trigger index. Observability helper -- consumed by
        ``/skills`` REPL + Slice 5 graduation tests.

        Empty kinds are absent (the index drops empty buckets on
        unregister). NEVER raises."""
        try:
            with self._lock:
                return {
                    kind.value: len(entries)
                    for kind, entries in self._triggers_by_kind.items()
                }
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.debug(
                "[SkillCatalog] trigger_index_counts degraded: %s",
                exc,
            )
            return {}

    # --- lifecycle: reset --------------------------------------------------

    def reset(self) -> None:
        """Test helper. Clears primary index, listeners, and the
        Slice 2 trigger-by-kind index."""
        with self._lock:
            self._by_qualified_name.clear()
            self._listeners.clear()
            self._triggers_by_kind.clear()


# Module singleton
_default_catalog: Optional[SkillCatalog] = None
_catalog_lock = threading.Lock()


def get_default_catalog() -> SkillCatalog:
    global _default_catalog
    with _catalog_lock:
        if _default_catalog is None:
            _default_catalog = SkillCatalog()
        return _default_catalog


def reset_default_catalog() -> None:
    global _default_catalog
    with _catalog_lock:
        if _default_catalog is not None:
            _default_catalog.reset()
        _default_catalog = None


# ---------------------------------------------------------------------------
# SkillInvoker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillInvocationOutcome:
    """Result from :meth:`SkillInvoker.invoke`."""

    qualified_name: str
    ok: bool
    duration_ms: float
    result_preview: str = ""
    error: Optional[str] = None
    schema_version: str = SKILL_CATALOG_SCHEMA_VERSION


class SkillInvoker:
    """Resolves entrypoints + runs skills with validated args."""

    def __init__(
        self,
        *,
        catalog: Optional[SkillCatalog] = None,
    ) -> None:
        self._catalog = catalog or get_default_catalog()
        self._resolved_cache: Dict[str, Callable[..., Any]] = {}
        self._cache_lock = threading.Lock()

    def resolve_entrypoint(
        self, manifest: SkillManifest,
    ) -> Callable[..., Any]:
        """Import the entrypoint's module + return the callable.

        Accepts ``"pkg.mod:name"`` (preferred) or ``"pkg.mod.name"``.
        Raises :class:`SkillInvocationError` on any failure.
        """
        ep = manifest.entrypoint
        with self._cache_lock:
            cached = self._resolved_cache.get(ep)
        if cached is not None:
            return cached
        if ":" in ep:
            module_path, attr = ep.split(":", 1)
        else:
            module_path, _, attr = ep.rpartition(".")
            if not module_path:
                raise SkillInvocationError(
                    f"entrypoint {ep!r} has no module path"
                )
        try:
            mod = importlib.import_module(module_path)
        except ImportError as exc:
            raise SkillInvocationError(
                f"could not import {module_path!r}: {exc}"
            ) from exc
        if not hasattr(mod, attr):
            raise SkillInvocationError(
                f"module {module_path!r} has no attribute {attr!r}"
            )
        fn = getattr(mod, attr)
        if not callable(fn):
            raise SkillInvocationError(
                f"entrypoint {ep!r} is not callable"
            )
        with self._cache_lock:
            self._resolved_cache[ep] = fn
        return fn

    async def invoke(
        self,
        qualified_name: str,
        *,
        args: Optional[Mapping[str, Any]] = None,
        output_preview_chars: int = 400,
    ) -> SkillInvocationOutcome:
        """Validate args, resolve entrypoint, run it, return outcome.

        Sync handlers are supported (the invoker awaits the result if
        it's a coroutine, otherwise passes through).
        """
        manifest = self._catalog.get(qualified_name)
        if manifest is None:
            return SkillInvocationOutcome(
                qualified_name=qualified_name,
                ok=False, duration_ms=0.0,
                error=f"unknown skill: {qualified_name}",
            )
        # Validate args against the manifest schema
        try:
            normalised = validate_args(manifest.args_schema, args or {})
        except SkillArgsError as exc:
            return SkillInvocationOutcome(
                qualified_name=qualified_name,
                ok=False, duration_ms=0.0,
                error=f"args_validation_error: {exc}",
            )
        # Resolve entrypoint
        try:
            fn = self.resolve_entrypoint(manifest)
        except SkillInvocationError as exc:
            return SkillInvocationOutcome(
                qualified_name=qualified_name,
                ok=False, duration_ms=0.0,
                error=f"entrypoint_error: {exc}",
            )
        t0 = time.monotonic()
        try:
            result = fn(manifest, dict(normalised))
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:  # noqa: BLE001
            duration_ms = (time.monotonic() - t0) * 1000.0
            logger.info(
                "[SkillInvoker] qname=%s ok=False duration_ms=%.1f "
                "error=%s", qualified_name, duration_ms, exc,
            )
            return SkillInvocationOutcome(
                qualified_name=qualified_name,
                ok=False, duration_ms=duration_ms,
                error=f"handler_raise:{type(exc).__name__}:{exc}",
            )
        duration_ms = (time.monotonic() - t0) * 1000.0
        # Bounded preview for audit
        preview = str(result) if result is not None else ""
        if len(preview) > output_preview_chars:
            preview = preview[: max(1, output_preview_chars - 3)] + "..."
        logger.info(
            "[SkillInvoker] qname=%s ok=True duration_ms=%.1f",
            qualified_name, duration_ms,
        )
        return SkillInvocationOutcome(
            qualified_name=qualified_name,
            ok=True,
            duration_ms=duration_ms,
            result_preview=preview,
        )


# ---------------------------------------------------------------------------
# SkillMarketplace — filesystem-backed discovery + install + remove
# ---------------------------------------------------------------------------


_MANIFEST_FILENAME = "manifest.yaml"


class SkillMarketplace:
    """Discovers, installs, and removes skills in a filesystem root.

    Layout::

        <root>/<name>/manifest.yaml              # bare skill
        <root>/<plugin>/<skill>/manifest.yaml    # namespaced

    ``discover()`` walks the tree and calls :meth:`SkillCatalog.register`
    for every valid manifest. Broken manifests are logged + skipped;
    one bad file doesn't prevent the rest from loading.
    """

    def __init__(
        self,
        root: Path,
        *,
        catalog: Optional[SkillCatalog] = None,
    ) -> None:
        self._root = Path(root).resolve()
        self._catalog = catalog or get_default_catalog()

    @property
    def root(self) -> Path:
        return self._root

    # --- install / remove ------------------------------------------------

    def install_from_directory(
        self,
        source: Path,
        *,
        source_tag: SkillSource = SkillSource.OPERATOR,
        replace_existing: bool = False,
    ) -> SkillManifest:
        """Copy a source skill directory into the marketplace root.

        Expects ``<source>/manifest.yaml`` — parses it first to catch
        broken manifests before touching disk. Copies into
        ``<root>/<qualified_path>/``; registers the resulting manifest.
        """
        src = Path(source).resolve()
        manifest_src = src / _MANIFEST_FILENAME
        if not manifest_src.exists():
            raise SkillCatalogError(
                f"install: no manifest.yaml at {src}"
            )
        # Parse BEFORE touching disk — cheap validation.
        m = SkillManifest.from_yaml_file(manifest_src)
        dest_dir = self._dest_dir_for(m)
        if dest_dir.exists():
            if not replace_existing:
                raise SkillCatalogError(
                    f"install: {dest_dir} already exists "
                    "(pass replace_existing=True to overwrite)"
                )
            shutil.rmtree(dest_dir)
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest_dir)
        # Re-parse from installed location so path is correct
        installed = SkillManifest.from_yaml_file(
            dest_dir / _MANIFEST_FILENAME,
        )
        # Unregister any prior entry; then register
        self._catalog.unregister(installed.qualified_name)
        self._catalog.register(installed, source=source_tag)
        logger.info(
            "[SkillMarketplace] installed qname=%s source=%s dest=%s",
            installed.qualified_name, str(src), str(dest_dir),
        )
        return installed

    def remove(self, qualified_name: str) -> bool:
        """Unregister + delete the on-disk skill directory."""
        manifest = self._catalog.get(qualified_name)
        if manifest is None:
            return False
        dest_dir = self._dest_dir_for(manifest)
        self._catalog.unregister(qualified_name)
        if dest_dir.exists():
            shutil.rmtree(dest_dir, ignore_errors=True)
        logger.info(
            "[SkillMarketplace] removed qname=%s dir=%s",
            qualified_name, str(dest_dir),
        )
        return True

    def _dest_dir_for(self, manifest: SkillManifest) -> Path:
        if manifest.plugin_namespace:
            return self._root / manifest.plugin_namespace / manifest.name
        return self._root / manifest.name

    # --- discovery -------------------------------------------------------

    def discover(
        self,
        *,
        source_tag: SkillSource = SkillSource.OPERATOR,
    ) -> List[SkillManifest]:
        """Walk the marketplace root; register every valid manifest.

        Returns the list of newly-registered manifests. Already-
        registered qualified_names are skipped silently (idempotent
        discovery). Broken manifests are logged + skipped.
        """
        if not self._root.exists():
            return []
        loaded: List[SkillManifest] = []
        for manifest_path in self._root.rglob(_MANIFEST_FILENAME):
            try:
                m = SkillManifest.from_yaml_file(manifest_path)
            except SkillManifestError as exc:
                logger.warning(
                    "[SkillMarketplace] skipping malformed manifest %s: %s",
                    manifest_path, exc,
                )
                continue
            if self._catalog.has(m.qualified_name):
                continue
            try:
                self._catalog.register(m, source=source_tag)
            except SkillCatalogError as exc:
                logger.warning(
                    "[SkillMarketplace] could not register %s: %s",
                    m.qualified_name, exc,
                )
                continue
            loaded.append(m)
        logger.info(
            "[SkillMarketplace] discover loaded=%d root=%s",
            len(loaded), str(self._root),
        )
        return loaded


# ---------------------------------------------------------------------------
# REPL dispatcher
# ---------------------------------------------------------------------------


@dataclass
class SkillDispatchResult:
    ok: bool
    text: str
    matched: bool = True


_SKILLS_HELP = textwrap.dedent(
    """
    Skill system commands
    ---------------------
      /skills                          — list registered skills
      /skills list                     — same as above
      /skills show <qualified-name>    — full manifest detail
      /skills run <qualified-name> [<arg=value>...]
                                       — invoke a skill with args
      /skills install <path>           — install a skill from a directory
      /skills remove <qualified-name>  — uninstall a skill
      /skills discover                 — scan marketplace root + register
      /skills help                     — this text
    """
).strip()


_COMMANDS = frozenset({"/skills"})


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _COMMANDS


def dispatch_skill_command(
    line: str,
    *,
    catalog: Optional[SkillCatalog] = None,
    invoker: Optional[SkillInvoker] = None,
    marketplace: Optional[SkillMarketplace] = None,
    run_coroutine: Optional[Callable[[Any], Any]] = None,
) -> SkillDispatchResult:
    """One-call REPL dispatcher for ``/skills`` subcommands.

    ``run_coroutine`` is only consulted for ``/skills run`` — it
    runs a coroutine to completion (in production: pass the SerpentFlow
    ``asyncio.run`` or event-loop-bound helper). If absent,
    ``/skills run`` is refused with a structured error.
    """
    if not _matches(line):
        return SkillDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return SkillDispatchResult(
            ok=False, text=f"  /skills parse error: {exc}",
        )
    if not tokens:
        return SkillDispatchResult(ok=False, text="", matched=False)
    cat = catalog or get_default_catalog()
    args = tokens[1:]

    if not args or args[0] in ("list",):
        return _skills_list(cat)
    head = args[0]
    if head == "help":
        return SkillDispatchResult(ok=True, text=_SKILLS_HELP)
    if head == "show":
        if len(args) < 2:
            return SkillDispatchResult(
                ok=False, text="  /skills show <qualified-name>",
            )
        return _skills_show(cat, args[1])
    if head == "run":
        inv = invoker or SkillInvoker(catalog=cat)
        return _skills_run(inv, args[1:], run_coroutine)
    if head == "install":
        if marketplace is None:
            return SkillDispatchResult(
                ok=False,
                text="  /skills install: no marketplace configured",
            )
        if len(args) < 2:
            return SkillDispatchResult(
                ok=False, text="  /skills install <path>",
            )
        return _skills_install(marketplace, args[1])
    if head == "remove":
        if marketplace is None:
            return SkillDispatchResult(
                ok=False,
                text="  /skills remove: no marketplace configured",
            )
        if len(args) < 2:
            return SkillDispatchResult(
                ok=False, text="  /skills remove <qualified-name>",
            )
        return _skills_remove(marketplace, args[1])
    if head == "discover":
        if marketplace is None:
            return SkillDispatchResult(
                ok=False,
                text="  /skills discover: no marketplace configured",
            )
        return _skills_discover(marketplace)
    # Short form: /skills <qualified-name>
    return _skills_show(cat, head)


def _skills_list(cat: SkillCatalog) -> SkillDispatchResult:
    skills = cat.list_all()
    if not skills:
        return SkillDispatchResult(ok=True, text="  (no skills registered)")
    lines: List[str] = [f"  Registered skills ({len(skills)}):"]
    for m in skills:
        lines.append(
            f"  - {m.qualified_name:<30} v{m.version:<10} "
            f"{m.description[:60]}"
        )
    return SkillDispatchResult(ok=True, text="\n".join(lines))


def _skills_show(cat: SkillCatalog, qname: str) -> SkillDispatchResult:
    m = cat.get(qname)
    if m is None:
        return SkillDispatchResult(
            ok=False, text=f"  /skills: unknown skill: {qname}",
        )
    proj = m.project()
    lines = [
        f"  Skill {m.qualified_name}",
        f"    description : {proj['description']}",
        f"    trigger     : {proj['trigger']}",
        f"    usage       : {proj['usage']}",
        f"    entrypoint  : {proj['entrypoint']}",
        f"    version     : {proj['version']}",
        f"    author      : {proj['author']}",
        f"    permissions : {proj['permissions']}",
        f"    args_schema : {sorted(proj['args_schema'].keys())}",
        f"    path        : {proj['path']}",
    ]
    return SkillDispatchResult(ok=True, text="\n".join(lines))


def _skills_run(
    inv: SkillInvoker,
    args: Sequence[str],
    run_coroutine: Optional[Callable[[Any], Any]],
) -> SkillDispatchResult:
    if not args:
        return SkillDispatchResult(
            ok=False, text="  /skills run <qualified-name> [arg=value...]",
        )
    if run_coroutine is None:
        return SkillDispatchResult(
            ok=False,
            text="  /skills run: no coroutine runner configured",
        )
    qname = args[0]
    raw_args = args[1:]
    parsed: Dict[str, Any] = {}
    for kv in raw_args:
        if "=" not in kv:
            return SkillDispatchResult(
                ok=False,
                text=f"  /skills run: bad arg form {kv!r} (need k=v)",
            )
        k, _, v = kv.partition("=")
        parsed[k] = _coerce_scalar(v)
    try:
        outcome = run_coroutine(inv.invoke(qname, args=parsed))
    except Exception as exc:  # noqa: BLE001
        return SkillDispatchResult(
            ok=False, text=f"  /skills run: {exc}",
        )
    if not isinstance(outcome, SkillInvocationOutcome):
        return SkillDispatchResult(
            ok=False,
            text=f"  /skills run: unexpected outcome type: {type(outcome).__name__}",
        )
    if outcome.ok:
        return SkillDispatchResult(
            ok=True,
            text=(
                f"  ran {qname} duration_ms={outcome.duration_ms:.1f}\n"
                f"  preview: {outcome.result_preview}"
            ),
        )
    return SkillDispatchResult(
        ok=False,
        text=f"  /skills run: {qname} failed: {outcome.error}",
    )


def _coerce_scalar(v: str) -> Any:
    """Best-effort CLI-style coercion: int / float / bool / string."""
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _skills_install(
    market: SkillMarketplace, path: str,
) -> SkillDispatchResult:
    try:
        m = market.install_from_directory(Path(path))
    except (SkillCatalogError, SkillManifestError) as exc:
        return SkillDispatchResult(
            ok=False, text=f"  /skills install: {exc}",
        )
    return SkillDispatchResult(
        ok=True,
        text=f"  installed: {m.qualified_name} v{m.version}",
    )


def _skills_remove(
    market: SkillMarketplace, qname: str,
) -> SkillDispatchResult:
    ok = market.remove(qname)
    if not ok:
        return SkillDispatchResult(
            ok=False, text=f"  /skills remove: unknown skill: {qname}",
        )
    return SkillDispatchResult(
        ok=True, text=f"  removed: {qname}",
    )


def _skills_discover(market: SkillMarketplace) -> SkillDispatchResult:
    loaded = market.discover()
    return SkillDispatchResult(
        ok=True,
        text=f"  discovered: {len(loaded)} new skill(s)",
    )


# ---------------------------------------------------------------------------
# Module singletons
# ---------------------------------------------------------------------------


_default_invoker: Optional[SkillInvoker] = None
_invoker_lock = threading.Lock()


def get_default_invoker() -> SkillInvoker:
    global _default_invoker
    with _invoker_lock:
        if _default_invoker is None:
            _default_invoker = SkillInvoker()
        return _default_invoker


def reset_default_invoker() -> None:
    global _default_invoker
    with _invoker_lock:
        _default_invoker = None


__all__ = [
    "SKILL_CATALOG_SCHEMA_VERSION",
    "SkillAuthorityError",
    "SkillCatalog",
    "SkillCatalogError",
    "SkillDispatchResult",
    "SkillInvocationError",
    "SkillInvocationOutcome",
    "SkillInvoker",
    "SkillMarketplace",
    "SkillSource",
    "dispatch_skill_command",
    "get_default_catalog",
    "get_default_invoker",
    "reset_default_catalog",
    "reset_default_invoker",
]

_ = (datetime, timezone, os, field, Tuple)  # silence unused-import guards

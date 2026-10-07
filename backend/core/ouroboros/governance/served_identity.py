"""Served identity -- WHO is answering: engine, model, adapter, and vision.

## Why this exists

The cockpit named the model (``qwen3-coder-ov:30b``) but not what stands
behind the name, and on a local lane the name is the one thing that does NOT
change when the organism learns: a Training Lifecycle Handoff publishes a new
adapter under the same tag. An operator could not tell the September adapter
from the one trained this morning, nor whether J-Prime or a stray Ollama was
answering, nor where screenshots were being read.

## One decision, two readers

"Which adapter is active, and where does its evidence end?" was answered
inside the training handoff (the yield gate's cutoff). The cockpit needs the
same answer; two implementations would drift until the cockpit named one
adapter while training counted against another. :func:`adapter_provenance`
is that decision over the engine's own records -- the registry's active
version and its ``trained_through``, else the served weight file's mtime --
and both the handoff and this module call it.

## Contract

Stdlib only. Resolved ONCE per session at the boot gate (the served adapter
cannot change while an organism runs: a cycle refuses beside one) and cached;
the cockpit's hydration frame reads the cache, so attaching never blocks on
the network. Every reader is bounded by the sibling probe timeout and NEVER
raises, except :func:`adapter_provenance`'s fail-closed refusal, which its
callers handle.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.ServedIdentity")

__all__ = ["EngineIdentity", "AdapterProvenance", "AdapterCutoffUnknown", "engine_identity",
           "adapter_provenance", "read_adapter_provenance", "resolve", "current", "set_current",
           "describe_line", "short_label"]


class AdapterCutoffUnknown(RuntimeError):
    """An adapter IS served but where its training evidence ends cannot be read."""


@dataclass(frozen=True)
class EngineIdentity:
    name: str            # "J-Prime" | "Ollama" | "" (unidentified)
    version: str
    url: str


@dataclass(frozen=True)
class AdapterProvenance:
    #: Registry version name ("origin", "20261007-…"); "" = no adapter.
    version: str
    #: Newest evidence the adapter can have learned from (epoch s), if known.
    trained_through: Optional[float]
    #: Where ``trained_through`` came from: ``registry:<version>``,
    #: ``adapter_file_mtime``, ``no_adapter`` or ``no_registry``.
    source: str
    published_at: Optional[float] = None


def _timeout() -> float:
    from backend.core.ouroboros.governance.trinity_siblings import probe_timeout_s
    return probe_timeout_s()


def _get_json(base: str, path: str, *, body: Any = None, timeout: Optional[float] = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=_timeout() if timeout is None else timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def engine_identity(base: str, *, timeout: Optional[float] = None) -> EngineIdentity:
    """What engine answers at ``base``, from its own words. NEVER raises."""
    base = (base or "").rstrip("/")
    if base.endswith("/v1"):                    # an OpenAI-style base names the same engine
        base = base[: -len("/v1")]
    try:
        h = _get_json(base, "/health", timeout=timeout)
        if isinstance(h, dict) and h.get("service") == "jarvis_prime":
            return EngineIdentity("J-Prime", str(h.get("version") or ""), base)
    except Exception:  # noqa: BLE001 -- not J-Prime, or not reachable; ask the next way
        pass
    try:
        v = _get_json(base, "/api/version", timeout=timeout)
        if isinstance(v, dict) and v.get("version"):
            return EngineIdentity("Ollama", str(v["version"]), base)
    except Exception:  # noqa: BLE001
        pass
    return EngineIdentity("", "", base)


def adapter_provenance(versions: Optional[Dict[str, Any]],
                       show_adapters: Optional[List[Dict[str, Any]]]) -> AdapterProvenance:
    """The active adapter and where its evidence ends, from the engine's records.

    ``versions`` is J-Prime's ``GET /v1/adapters/{model}`` (None: the engine
    has no registry); ``show_adapters`` is ``POST /api/show``'s ``adapters``.
    A version this loop published carries ``source.trained_through`` (the
    newest landing it learned from). One that predates the registry (origin,
    e.g. the Ollama-built adapter) is bounded by its weight file's mtime:
    nothing committed after the file was written can have trained it.

    Raises :class:`AdapterCutoffUnknown` when an adapter is served but no
    cutoff is readable -- a reader counting evidence must not treat that as
    "learned nothing" and start an hours-long cycle on evidence it may hold.
    """
    active = str((versions or {}).get("active") or "")
    entry = next((v for v in (versions or {}).get("versions") or [] if v.get("version") == active), None)
    published = (entry or {}).get("published_at")
    through = ((entry or {}).get("source") or {}).get("trained_through")
    if through:
        return AdapterProvenance(active, float(through), f"registry:{active}", published)
    adapters = show_adapters or []
    if not adapters:
        return AdapterProvenance("", None, "no_adapter" if versions is not None else "no_registry")
    mtimes = [a.get("mtime") for a in adapters if a.get("mtime")]
    if len(mtimes) != len(adapters):
        raise AdapterCutoffUnknown(f"{len(adapters)} adapter(s) served with no readable training cutoff "
                                   "(J-Prime too old to report adapter mtime?)")
    return AdapterProvenance(active or "unregistered", float(max(mtimes)), "adapter_file_mtime", published)


def read_adapter_provenance(base: str, model: str, *, timeout: Optional[float] = None) -> AdapterProvenance:
    """Fetch both records and decide. Raises what :func:`adapter_provenance`
    raises, and the transport's errors -- callers choose their failure mode."""
    try:
        versions = _get_json(base, f"/v1/adapters/{urllib.parse.quote(model, safe=':')}", timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        versions = None                              # an engine without a registry
    show = _get_json(base, "/api/show", body={"model": model}, timeout=timeout)
    return adapter_provenance(versions, (show or {}).get("adapters"))


# ---------------------------------------------------------------------------
# The session's identity: resolved at the boot gate, read by the cockpit
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_current: Dict[str, Any] = {}


def resolve(*, base: str, model: str, vision_base: str = "", vision_model: str = "") -> Dict[str, Any]:
    """Who is answering this session. NEVER raises; unknown parts are omitted."""
    out: Dict[str, Any] = {"model": model, "resolved_at": time.time()}
    try:
        eng = engine_identity(base)
        out["engine"] = asdict(eng)
        if eng.name:
            try:
                out["adapter"] = asdict(read_adapter_provenance(eng.url, model))
            except Exception as exc:  # noqa: BLE001 -- named as unknown, never guessed
                out["adapter"] = {"version": "", "error": f"{type(exc).__name__}: {exc}"[:200]}
        if vision_model:
            out["vision"] = {"model": vision_model, "engine": asdict(engine_identity(vision_base))}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ServedIdentity] resolve degraded: %s", exc, exc_info=True)
    return out


def set_current(identity: Dict[str, Any]) -> None:
    with _lock:
        _current.clear()
        _current.update(identity or {})


def current() -> Dict[str, Any]:
    with _lock:
        return dict(_current)


def _day(ts: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""


def _adapter_text(adapter: Dict[str, Any]) -> str:
    if not adapter:
        return ""
    if adapter.get("error"):
        return "adapter unknown"
    if not adapter.get("version"):
        return "base weights" if adapter.get("source") == "no_adapter" else ""
    through = _day(adapter.get("trained_through"))
    return f"adapter {adapter['version']}" + (f" (learned through {through})" if through else "")


def _engine_text(engine: Dict[str, Any]) -> str:
    if not engine or not engine.get("name"):
        return "an unidentified engine"
    return f"{engine['name']} {engine.get('version') or ''}".strip()


def describe_line(identity: Dict[str, Any]) -> str:
    """One full sentence for the boot log and the cockpit banner."""
    if not identity or not identity.get("model"):
        return ""
    parts = [f"{identity['model']} via {_engine_text(identity.get('engine') or {})}"]
    adapter = _adapter_text(identity.get("adapter") or {})
    if adapter:
        parts.append(adapter)
    vision = identity.get("vision") or {}
    if vision.get("model"):
        parts.append(f"vision {vision['model']} via {_engine_text(vision.get('engine') or {})}")
    return " · ".join(parts)


def short_label(identity: Dict[str, Any]) -> str:
    """The compact suffix for the always-visible toolbar: engine/adapter."""
    if not identity:
        return ""
    engine = (identity.get("engine") or {}).get("name") or ""
    adapter = identity.get("adapter") or {}
    version = adapter.get("version") or ("base" if adapter.get("source") == "no_adapter" else "")
    if adapter.get("error"):
        version = "adapter?"
    return "/".join(p for p in (engine, version) if p)


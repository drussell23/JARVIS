"""torch_hub_loader — the one way this codebase loads a model from torch.hub.

Why this exists
---------------

``torch.hub.load`` defaults to ``trust_repo="check"``: for any repository not
already in ``<hub_dir>/trusted_list`` (and not yet downloaded) it calls
``input()`` and waits for ``y/N`` on stdin. In a daemon that is a thread parked
forever on a console nobody is watching, and the prompt text interleaved into
the supervisor's terminal (measured 2026-10-15, boot
``supervisor-20261015-085516``: ``snakers4_silero-vad does not belong to the
list of trusted repositories ... (y/N)?``). ``trust_repo=False`` prompts too.
The only non-interactive answers are ``True`` or not calling it at all.

So the trust decision is DECLARED, not asked:

* ``JARVIS_TORCH_HUB_TRUSTED_REPOS`` — comma-separated ``owner/name`` list of
  repositories this install may download hub code from. A repository not on it
  is refused with an error that names the variable; torch is never consulted,
  so nothing can prompt.
* A repository already present in the hub cache loads with ``source="local"``:
  no network, no trust check, no GitHub API call.

Blocking, by design
-------------------

Every function here may download code and weights. **None may be called from a
running event loop's thread** — :func:`load_hub_model` raises if it is, rather
than silently stalling every coroutine for the length of a download. Callers
run it in an executor (``loop.run_in_executor`` / ``asyncio.to_thread``).
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
from pathlib import Path
from typing import Any, FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "HubRepoNotTrusted",
    "cached_repo_dir",
    "load_hub_model",
    "load_silero_vad",
    "trusted_hub_repos",
]

#: Env var holding the declared trust list (``owner/name``, comma-separated).
TRUSTED_REPOS_ENV = "JARVIS_TORCH_HUB_TRUSTED_REPOS"
#: The Silero VAD source repository (overridable via JARVIS_SILERO_VAD_REPO).
_DEFAULT_SILERO_REPO = "snakers4/silero-vad"
#: The repositories this codebase itself loads; the default trust list.
_DEFAULT_TRUSTED_REPOS = _DEFAULT_SILERO_REPO

#: Silero VAD source repository and entry point (env-overridable).
_SILERO_REPO_ENV = "JARVIS_SILERO_VAD_REPO"
_SILERO_MODEL_ENV = "JARVIS_SILERO_VAD_MODEL"
#: "auto" (ONNX iff onnxruntime is installed), "true" or "false".
_SILERO_ONNX_ENV = "JARVIS_SILERO_VAD_ONNX"


class HubRepoNotTrusted(PermissionError):
    """A torch.hub repository was requested that the install has not trusted."""


def trusted_hub_repos() -> FrozenSet[str]:
    """The declared trust list, normalised to lower-case ``owner/name``."""
    raw = os.environ.get(TRUSTED_REPOS_ENV)
    if raw is None:
        raw = _DEFAULT_TRUSTED_REPOS
    return frozenset(
        part.strip().lower().split(":", 1)[0]
        for part in raw.split(",")
        if part.strip()
    )


def _split_repo(repo: str) -> Tuple[str, str]:
    # torch.hub accepts "owner/name[:ref]".
    owner_name = repo.split(":", 1)[0]
    if owner_name.count("/") != 1:
        raise ValueError(f"torch.hub repo must be 'owner/name[:ref]', got {repo!r}")
    owner, name = owner_name.split("/")
    return owner, name


def cached_repo_dir(repo: str) -> Optional[Path]:
    """The hub-cache checkout of ``repo``, if one was already downloaded.

    torch.hub stores a GitHub repo as ``<hub_dir>/<owner>_<name>_<ref>``. The
    ref is whatever branch was the default at download time (silero-vad was
    ``master``), so it is matched rather than assumed. A directory without a
    ``hubconf.py`` is a partial download and is not a cache hit."""
    import torch  # noqa: PLC0415 — heavy, only on the load path

    owner, name = _split_repo(repo)
    ref = repo.split(":", 1)[1] if ":" in repo else None
    hub_dir = Path(torch.hub.get_dir())
    if not hub_dir.is_dir():
        return None
    prefix = f"{owner}_{name}_"
    candidates = sorted(
        p for p in hub_dir.iterdir()
        if p.is_dir()
        and p.name.startswith(prefix)
        and (ref is None or p.name == prefix + ref.replace("/", "_"))
        and (p / "hubconf.py").is_file()
    )
    return candidates[0] if candidates else None


def _refuse_on_event_loop(what: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        f"{what} blocks (it may download code and weights) and was called on "
        "the event loop's thread; run it in an executor"
    )


def load_hub_model(repo: str, model: str, **kwargs: Any) -> Any:
    """``torch.hub.load`` without the stdin prompt. BLOCKS.

    Cache hit → ``source="local"``. Otherwise the repo must be on the declared
    trust list (:data:`TRUSTED_REPOS_ENV`), and is fetched with
    ``trust_repo=True``; if it is not, :class:`HubRepoNotTrusted` is raised and
    torch is never called."""
    _refuse_on_event_loop(f"torch.hub load of {repo}")
    import torch  # noqa: PLC0415

    kwargs.pop("trust_repo", None)  # the decision is this module's, not the caller's
    kwargs.pop("source", None)

    local = cached_repo_dir(repo)
    if local is not None:
        logger.info("[TorchHub] %s:%s from local cache %s", repo, model, local)
        return torch.hub.load(str(local), model, source="local", **kwargs)

    owner, name = _split_repo(repo)
    if f"{owner}/{name}".lower() not in trusted_hub_repos():
        raise HubRepoNotTrusted(
            f"torch.hub repository {owner}/{name} is not trusted on this install "
            f"and is not cached; add it to {TRUSTED_REPOS_ENV} to allow the "
            "download (a daemon cannot answer torch.hub's y/N prompt)"
        )
    logger.info("[TorchHub] downloading %s:%s (declared trusted via %s)",
                repo, model, TRUSTED_REPOS_ENV)
    return torch.hub.load(repo, model, source="github", trust_repo=True, **kwargs)


def _silero_use_onnx(requested: Optional[bool]) -> bool:
    if requested is not None:
        return requested
    raw = (os.environ.get(_SILERO_ONNX_ENV, "auto") or "auto").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    # auto: the ONNX build needs onnxruntime; asking for it without the
    # package is a guaranteed ModuleNotFoundError after a download.
    return importlib.util.find_spec("onnxruntime") is not None


def load_silero_vad(onnx: Optional[bool] = None) -> Tuple[Any, Any]:
    """Load Silero VAD, returning ``(model, utils)``. BLOCKS.

    ``onnx=None`` follows ``JARVIS_SILERO_VAD_ONNX`` (default ``auto``: ONNX
    when onnxruntime is installed, the TorchScript model otherwise)."""
    repo = os.environ.get(_SILERO_REPO_ENV, "") or _DEFAULT_SILERO_REPO
    model = os.environ.get(_SILERO_MODEL_ENV, "") or "silero_vad"
    return load_hub_model(
        repo, model, onnx=_silero_use_onnx(onnx), force_reload=False,
    )

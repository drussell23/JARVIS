"""Allow running as: python3 -m brainstem

v351.0: Thin shim that boots the FULL backend in HUD mode.
Sets JARVIS_MODE=hud so the backend starts with:
  - IPC server on 8742 (for Swift HUD)
  - Full stack: Ouroboros, Doubleword, Claude, Vision, Ghost Hands
  - No Trinity cross-repo, no GCP VM lifecycle

Legacy mode: set JARVIS_BRAINSTEM_LEGACY=true to use the old
lightweight brainstem (brainstem.main) instead.
"""
import os
import sys

if __name__ == "__main__":
    # Legacy mode: use old brainstem for backwards compatibility
    if os.environ.get("JARVIS_BRAINSTEM_LEGACY", "").lower() in ("1", "true"):
        import asyncio
        from brainstem.main import main
        asyncio.run(main())
        sys.exit(0)

    # HUD mode: launch the full backend stack
    os.environ["JARVIS_MODE"] = "hud"

    # Ensure backend is importable (add repo root to path)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host="127.0.0.1",
        port=int(os.environ.get("JARVIS_BACKEND_PORT", "8010")),
        log_level="info",
    )

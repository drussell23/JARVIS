#!/usr/bin/env python3
"""
JARVIS Git Hooks Installer
==========================

Installs the file integrity protection hooks into the local git repository.

Features:
- Pre-commit hook for syntax and truncation detection
- Commit-msg hook for integrity metadata
- Automatic backup of existing hooks
- Easy uninstall option

Usage:
    python scripts/install_hooks.py          # Install hooks
    python scripts/install_hooks.py install  # Install hooks
    python scripts/install_hooks.py remove   # Remove hooks
    python scripts/install_hooks.py status   # Check status

Author: JARVIS System
"""

import os
import shutil
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

# ANSI colors
class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    RESET = '\033[0m'


def color(text: str, *styles: str) -> str:
    return ''.join(styles) + text + Colors.RESET


def get_project_root() -> Path:
    """Get the project root directory."""
    # Try to find the root by looking for .git
    current = Path(__file__).resolve().parent
    
    while current != current.parent:
        if (current / ".git").is_dir():
            return current
        current = current.parent
    
    # Fallback to parent of scripts
    return Path(__file__).resolve().parent.parent


def get_hooks_dir() -> Path:
    """Get the git hooks directory."""
    root = get_project_root()
    return root / ".git" / "hooks"


def get_source_hooks_dir() -> Path:
    """Get the source hooks directory."""
    return Path(__file__).resolve().parent / "hooks"


HOOKS_TO_INSTALL = ["pre-commit", "commit-msg", "post-commit"]


def backup_hook(hooks_dir: Path, hook_name: str) -> bool:
    """Backup an existing hook if it exists."""
    hook_path = hooks_dir / hook_name
    
    if hook_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = hooks_dir / f"{hook_name}.backup.{timestamp}"
        
        try:
            shutil.copy2(hook_path, backup_path)
            print(f"  📦 Backed up existing {hook_name} to {backup_path.name}")
            return True
        except Exception as e:
            print(f"  ⚠️ Failed to backup {hook_name}: {e}")
            return False
    
    return True


def install_hook(hooks_dir: Path, source_dir: Path, hook_name: str) -> Tuple[bool, str]:
    """Install a single hook."""
    source_path = source_dir / hook_name
    dest_path = hooks_dir / hook_name
    
    if not source_path.exists():
        return False, f"Source hook {hook_name} not found at {source_path}"
    
    try:
        # Backup existing hook
        if dest_path.exists():
            backup_hook(hooks_dir, hook_name)
        
        # Copy the hook
        shutil.copy2(source_path, dest_path)
        
        # Make executable
        current_mode = dest_path.stat().st_mode
        dest_path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        
        return True, f"Installed {hook_name}"
        
    except Exception as e:
        return False, f"Failed to install {hook_name}: {e}"


def remove_hook(hooks_dir: Path, hook_name: str) -> Tuple[bool, str]:
    """Remove a hook."""
    hook_path = hooks_dir / hook_name
    
    if not hook_path.exists():
        return True, f"{hook_name} not installed"
    
    try:
        # Check if it's our hook by looking for JARVIS signature
        content = hook_path.read_text()
        if "JARVIS" not in content:
            return False, f"{hook_name} exists but is not a JARVIS hook - not removing"
        
        # Backup before removing
        backup_hook(hooks_dir, hook_name)
        
        # Remove
        hook_path.unlink()
        
        return True, f"Removed {hook_name}"
        
    except Exception as e:
        return False, f"Failed to remove {hook_name}: {e}"


def get_hook_status(hooks_dir: Path, hook_name: str) -> str:
    """Get status of a hook."""
    hook_path = hooks_dir / hook_name
    
    if not hook_path.exists():
        return color("not installed", Colors.YELLOW)
    
    try:
        content = hook_path.read_text()
        if "JARVIS" in content:
            # Check if executable
            if os.access(hook_path, os.X_OK):
                return color("installed ✓", Colors.GREEN)
            else:
                return color("installed (not executable)", Colors.YELLOW)
        else:
            return color("custom hook exists", Colors.YELLOW)
    except Exception:
        return color("error reading", Colors.RED)


def cmd_install() -> int:
    """Install all hooks."""
    print(color("\n🔧 Installing JARVIS Git Hooks", Colors.CYAN, Colors.BOLD))
    print(color("=" * 50, Colors.CYAN))
    
    hooks_dir = get_hooks_dir()
    source_dir = get_source_hooks_dir()
    
    if not hooks_dir.exists():
        print(color(f"❌ Git hooks directory not found: {hooks_dir}", Colors.RED))
        print("  Are you in a git repository?")
        return 1
    
    if not source_dir.exists():
        print(color(f"❌ Source hooks directory not found: {source_dir}", Colors.RED))
        return 1
    
    print(f"\nHooks directory: {hooks_dir}")
    print(f"Source directory: {source_dir}\n")
    
    success_count = 0
    fail_count = 0
    
    for hook_name in HOOKS_TO_INSTALL:
        success, message = install_hook(hooks_dir, source_dir, hook_name)
        
        if success:
            print(f"  {color('✓', Colors.GREEN)} {message}")
            success_count += 1
        else:
            print(f"  {color('✗', Colors.RED)} {message}")
            fail_count += 1
    
    print()
    
    if fail_count == 0:
        print(color("✅ All hooks installed successfully!", Colors.GREEN, Colors.BOLD))
        print()
        print(color("What happens now:", Colors.CYAN))
        print("  • Pre-commit: Validates Python files before each commit")
        print("  • Commit-msg: Adds integrity verification metadata")
        print()
        print(color("To bypass (not recommended):", Colors.YELLOW))
        print("  git commit --no-verify")
        print()
        return 0
    else:
        print(color(f"⚠️ {fail_count} hook(s) failed to install", Colors.YELLOW))
        return 1


def cmd_remove() -> int:
    """Remove all hooks."""
    print(color("\n🗑️ Removing JARVIS Git Hooks", Colors.CYAN, Colors.BOLD))
    print(color("=" * 50, Colors.CYAN))
    
    hooks_dir = get_hooks_dir()
    
    if not hooks_dir.exists():
        print(color(f"❌ Git hooks directory not found: {hooks_dir}", Colors.RED))
        return 1
    
    success_count = 0
    fail_count = 0
    
    for hook_name in HOOKS_TO_INSTALL:
        success, message = remove_hook(hooks_dir, hook_name)
        
        if success:
            print(f"  {color('✓', Colors.GREEN)} {message}")
            success_count += 1
        else:
            print(f"  {color('✗', Colors.RED)} {message}")
            fail_count += 1
    
    print()
    
    if fail_count == 0:
        print(color("✅ All JARVIS hooks removed!", Colors.GREEN))
        return 0
    else:
        print(color(f"⚠️ {fail_count} hook(s) could not be removed", Colors.YELLOW))
        return 1


def cmd_status() -> int:
    """Show status of all hooks."""
    print(color("\n📊 JARVIS Git Hooks Status", Colors.CYAN, Colors.BOLD))
    print(color("=" * 50, Colors.CYAN))
    
    hooks_dir = get_hooks_dir()
    
    if not hooks_dir.exists():
        print(color(f"❌ Git hooks directory not found: {hooks_dir}", Colors.RED))
        return 1
    
    print(f"\nHooks directory: {hooks_dir}\n")
    
    for hook_name in HOOKS_TO_INSTALL:
        status = get_hook_status(hooks_dir, hook_name)
        print(f"  {hook_name}: {status}")
    
    print()
    return 0


def main() -> int:
    """Main entry point."""
    if len(sys.argv) < 2:
        cmd = "install"
    else:
        cmd = sys.argv[1].lower()
    
    if cmd in ("install", "i"):
        return cmd_install()
    elif cmd in ("remove", "r", "uninstall"):
        return cmd_remove()
    elif cmd in ("status", "s"):
        return cmd_status()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: install_hooks.py [install|remove|status]")
        return 1


if __name__ == "__main__":
    sys.exit(main())


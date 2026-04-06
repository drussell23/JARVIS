"""BranchManager — accumulation branch lifecycle for the Battle Test Runner.

Creates a timestamped branch, commits auto-applied SAFE_AUTO operations with
structured messages, and exposes diff stats.  Main stays clean; the branch is
reviewed by a human after the session ends.
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class BranchManager:
    """Manage a single accumulation branch for a battle test session.

    Parameters
    ----------
    repo_path:
        Absolute path to the git repository root.
    branch_prefix:
        Prefix used when constructing the timestamped branch name.
        Defaults to ``"ouroboros/battle-test"``.
    """

    def __init__(
        self,
        repo_path: Path,
        branch_prefix: str = "ouroboros/battle-test",
    ) -> None:
        self._repo_path = Path(repo_path)
        self._branch_prefix = branch_prefix
        self._branch_name: Optional[str] = None
        self._commit_count: int = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def branch_name(self) -> Optional[str]:
        """The name of the accumulation branch, or ``None`` before :meth:`create_branch`."""
        return self._branch_name

    @property
    def commit_count(self) -> int:
        """Number of operations committed to the accumulation branch this session."""
        return self._commit_count

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_branch(self) -> str:
        """Create a timestamped accumulation branch and check it out.

        Raises
        ------
        RuntimeError
            If the working tree is not clean (has staged or unstaged changes).

        Returns
        -------
        str
            The full branch name, e.g. ``"ouroboros/battle-test/20260406-153012"``.
        """
        self._assert_clean()

        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = f"{self._branch_prefix}/{timestamp}"
        self._git("checkout", "-b", name)
        self._branch_name = name
        logger.info("BranchManager: created accumulation branch %s", name)
        return name

    def commit_operation(
        self,
        files: List[Path],
        sensor: str,
        description: str,
        op_id: str,
        risk_tier: str,
        composite_score: float,
        technique: str,
    ) -> str:
        """Stage *files* and commit them with a structured message.

        Parameters
        ----------
        files:
            List of absolute (or repo-relative) paths to stage.
        sensor:
            The sensor that raised the operation (used in commit subject).
        description:
            Short human-readable description of the change.
        op_id:
            Unique operation identifier.
        risk_tier:
            Risk classification (e.g. ``"SAFE_AUTO"``).
        composite_score:
            Numeric composite risk score.
        technique:
            The technique applied.

        Returns
        -------
        str
            Short git SHA of the new commit.
        """
        for f in files:
            self._git("add", str(f))

        message = (
            f"ouroboros({sensor}): {description}\n"
            f"\n"
            f"Operation: {op_id}\n"
            f"Risk: {risk_tier}\n"
            f"Composite Score: {composite_score:.4f}\n"
            f"Technique: {technique}\n"
            f"Auto-applied: true"
        )
        self._git("commit", "-m", message)

        sha = self._git("rev-parse", "--short", "HEAD").strip()
        self._commit_count += 1
        logger.debug("BranchManager: committed op %s as %s", op_id, sha)
        return sha

    def get_diff_stats(self) -> Dict[str, int]:
        """Return diff statistics relative to the branch point.

        Compares the accumulation branch against its merge base with the
        branch that existed before (the parent of the first accumulation
        commit), counting commits, files changed, insertions, and deletions.

        Returns
        -------
        dict
            Keys: ``commits``, ``files_changed``, ``insertions``, ``deletions``.
        """
        if self._branch_name is None:
            return {"commits": 0, "files_changed": 0, "insertions": 0, "deletions": 0}

        # Find the commit where this branch diverged from its parent
        # Use git log to count commits on this branch not on any other
        try:
            merge_base_out = self._git(
                "merge-base", "--fork-point", "HEAD~" + str(self._commit_count), "HEAD"
            ).strip()
        except subprocess.CalledProcessError:
            # Fallback: compare against the commit before first ouroboros commit
            merge_base_out = self._git(
                "rev-parse", f"HEAD~{self._commit_count}"
            ).strip()

        # Count commits
        commits_out = self._git(
            "rev-list", "--count", f"{merge_base_out}..HEAD"
        ).strip()
        commits = int(commits_out) if commits_out.isdigit() else self._commit_count

        # Get diff --stat output between base and HEAD
        stat_out = self._git("diff", "--stat", merge_base_out, "HEAD")

        files_changed, insertions, deletions = _parse_diff_stat(stat_out)

        return {
            "commits": commits,
            "files_changed": files_changed,
            "insertions": insertions,
            "deletions": deletions,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _git(self, *args: str) -> str:
        """Run a git command in :attr:`_repo_path` and return stdout.

        Raises
        ------
        subprocess.CalledProcessError
            If git exits with a non-zero status.
        """
        cmd = ["git", "-C", str(self._repo_path), *args]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout

    def _assert_clean(self) -> None:
        """Raise RuntimeError if the working tree or index is dirty."""
        status = self._git("status", "--porcelain").strip()
        if status:
            raise RuntimeError(
                f"Repository at {self._repo_path} is dirty; cannot create accumulation branch.\n"
                f"Unclean files:\n{status}"
            )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _parse_diff_stat(stat_output: str) -> tuple[int, int, int]:
    """Parse ``git diff --stat`` output and return (files_changed, insertions, deletions).

    Looks for the summary line at the end, e.g.:
        ``3 files changed, 10 insertions(+), 2 deletions(-)``

    Falls back to counting ``|`` separator lines when the summary is absent.
    """
    files_changed = 0
    insertions = 0
    deletions = 0

    for line in reversed(stat_output.splitlines()):
        line = line.strip()
        if not line:
            continue
        # Match summary line
        m = re.search(r"(\d+) file", line)
        if m:
            files_changed = int(m.group(1))
            ins_m = re.search(r"(\d+) insertion", line)
            del_m = re.search(r"(\d+) deletion", line)
            if ins_m:
                insertions = int(ins_m.group(1))
            if del_m:
                deletions = int(del_m.group(1))
            break

    return files_changed, insertions, deletions

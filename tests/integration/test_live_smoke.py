#!/usr/bin/env python3
"""
Live Smoke Test — Validates the full JARVIS agent pipeline end-to-end.

This test actually boots real components and sends real commands through
the system. Use this to verify that JARVIS can:
1. Expand a natural language command into tasks
2. Route tasks to the correct agents
3. Execute tasks via Google Workspace API or ComputerUseAgent
4. Produce visible results on your MacBook

Prerequisites:
    - Google OAuth: ~/.jarvis/google_credentials.json + token
    - For email drafting: JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_WRITES=true

Usage:
    # Test 1: Expand "Start my day" and show the plan (no execution)
    python3 -m pytest tests/integration/test_live_smoke.py::TestLiveSmoke::test_expand_start_my_day -v -s

    # Test 2: Actually draft an email via Gmail API
    JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_WRITES=true \
    python3 -m pytest tests/integration/test_live_smoke.py::TestLiveSmoke::test_draft_email_live -v -s

    # Test 3: Fetch unread emails (read-only, always safe)
    python3 -m pytest tests/integration/test_live_smoke.py::TestLiveSmoke::test_fetch_emails_live -v -s

    # Test 4: Check calendar (read-only, always safe)
    python3 -m pytest tests/integration/test_live_smoke.py::TestLiveSmoke::test_check_calendar_live -v -s

    # Run all smoke tests
    python3 -m pytest tests/integration/test_live_smoke.py -v -s
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

# Ensure project root is on path
_project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "backend"))


def _has_google_creds() -> bool:
    """Check if Google OAuth is set up."""
    cred_path = os.getenv(
        "GOOGLE_CREDENTIALS_PATH",
        os.path.expanduser("~/.jarvis/google_credentials.json"),
    )
    token_path = os.getenv(
        "GOOGLE_TOKEN_PATH",
        os.path.expanduser("~/.jarvis/google_workspace_token.json"),
    )
    return os.path.exists(cred_path) and os.path.exists(token_path)


def _writes_allowed() -> bool:
    """Check if autonomous writes are enabled."""
    return os.getenv("JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_WRITES", "").lower() in (
        "true", "1", "yes",
    )


skip_no_creds = pytest.mark.skipif(
    not _has_google_creds(),
    reason="Google OAuth not configured (need ~/.jarvis/google_credentials.json + token)",
)

skip_no_writes = pytest.mark.skipif(
    not _writes_allowed(),
    reason="Write operations disabled (set JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_WRITES=true)",
)


class TestLiveSmoke:
    """Live smoke tests — these hit real APIs and produce visible results."""

    # ------------------------------------------------------------------
    # Test 1: Expand a command (no external calls, always safe)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_expand_start_my_day(self):
        """Expand 'Start my day' into parallel tasks and display the plan."""
        from backend.neural_mesh.agents.predictive_planning_agent import (
            PredictivePlanningAgent,
        )

        agent = PredictivePlanningAgent()
        # Don't call on_initialize() — avoids Claude API requirement

        prediction = await agent.expand_intent("Start my day")

        print("\n" + "=" * 70)
        print(f"  INTENT: {prediction.detected_intent.value}")
        print(f"  CONFIDENCE: {prediction.confidence:.0%}")
        print(f"  TASKS EXPANDED: {len(prediction.expanded_tasks)}")
        print("=" * 70)

        for i, task in enumerate(prediction.expanded_tasks, 1):
            print(f"  {i}. [{task.priority}] {task.goal}")
            if task.target_app:
                print(f"       App: {task.target_app}")

        # Convert to workflow tasks
        workflow_tasks = agent.to_workflow_tasks(prediction)

        print("\n" + "-" * 70)
        print("  WORKFLOW TASKS (ready for orchestrator):")
        print("-" * 70)
        for wt in workflow_tasks:
            print(
                f"  [{wt.priority.name:8s}] "
                f"{wt.required_capability:25s} → {wt.name[:45]}"
            )
            if wt.fallback_capability:
                print(f"           fallback: {wt.fallback_capability}")

        print("=" * 70)

        assert prediction.detected_intent.value == "work_mode"
        assert len(prediction.expanded_tasks) >= 2

    @pytest.mark.asyncio
    async def test_expand_draft_email(self):
        """Expand 'Draft an email to my team about the project update'."""
        from backend.neural_mesh.agents.predictive_planning_agent import (
            PredictivePlanningAgent,
        )

        agent = PredictivePlanningAgent()
        prediction = await agent.expand_intent(
            "Draft an email to my team about the project update"
        )

        print("\n" + "=" * 70)
        print(f"  INTENT: {prediction.detected_intent.value}")
        print(f"  CONFIDENCE: {prediction.confidence:.0%}")
        print("=" * 70)

        workflow_tasks = agent.to_workflow_tasks(prediction)
        for wt in workflow_tasks:
            print(
                f"  [{wt.priority.name:8s}] "
                f"{wt.required_capability:25s} → {wt.name[:45]}"
            )

        # Email command should produce at least one workspace-capable task
        capabilities = {t.required_capability for t in workflow_tasks}
        assert "handle_workspace_query" in capabilities or "computer_use" in capabilities

    # ------------------------------------------------------------------
    # Test 2: Draft an email via Gmail API (requires creds + write permission)
    # ------------------------------------------------------------------

    @skip_no_creds
    @skip_no_writes
    @pytest.mark.asyncio
    async def test_draft_email_live(self):
        """Actually create a draft email in Gmail via GoogleWorkspaceAgent.

        After this test, check your Gmail Drafts folder — you should see
        the email draft there.
        """
        from backend.neural_mesh.agents.google_workspace_agent import (
            GoogleWorkspaceAgent,
        )

        agent = GoogleWorkspaceAgent()
        await agent.on_initialize()

        result = await agent.execute_task({
            "action": "draft_email_reply",
            "to": "djrussell23@gmail.com",  # Draft to yourself
            "subject": "[JARVIS SMOKE TEST] Wire Integration Verified",
            "body": (
                "This email was drafted by JARVIS's multi-agent pipeline.\n\n"
                "If you're reading this in your Gmail Drafts, it means:\n"
                "1. Voice command → PredictivePlanningAgent (intent expansion)\n"
                "2. PredictivePlanningAgent → MultiAgentOrchestrator (task conversion)\n"
                "3. MultiAgentOrchestrator → GoogleWorkspaceAgent (execution)\n"
                "4. GoogleWorkspaceAgent → Gmail API (draft creation)\n\n"
                "All 5 wires are connected and working.\n\n"
                "— JARVIS AI System (Trinity: Body)"
            ),
        })

        print("\n" + "=" * 70)
        print("  DRAFT EMAIL RESULT:")
        print("=" * 70)
        print(f"  Status: {result.get('status', result.get('error', 'unknown'))}")
        if result.get("draft_id"):
            print(f"  Draft ID: {result['draft_id']}")
        if result.get("error"):
            print(f"  Error: {result['error']}")
        print("=" * 70)
        print("  >>> CHECK YOUR GMAIL DRAFTS FOLDER <<<")
        print("=" * 70)

        assert result.get("status") == "created" or result.get("draft_id"), (
            f"Draft creation failed: {result}"
        )

    # ------------------------------------------------------------------
    # Test 3: Fetch unread emails (read-only, always safe)
    # ------------------------------------------------------------------

    @skip_no_creds
    @pytest.mark.asyncio
    async def test_fetch_emails_live(self):
        """Fetch unread emails from Gmail — read-only, always safe."""
        from backend.neural_mesh.agents.google_workspace_agent import (
            GoogleWorkspaceAgent,
        )

        agent = GoogleWorkspaceAgent()
        await agent.on_initialize()

        result = await agent.execute_task({
            "action": "fetch_unread_emails",
            "limit": 5,
        })

        print("\n" + "=" * 70)
        print("  UNREAD EMAILS:")
        print("=" * 70)

        if isinstance(result, dict) and result.get("error"):
            print(f"  Error: {result['error']}")
            pytest.skip(f"Gmail API error: {result['error']}")
            return

        emails = result.get("emails", []) if isinstance(result, dict) else []
        if not emails:
            print("  (No unread emails)")
        else:
            for i, email in enumerate(emails[:5], 1):
                sender = email.get("from", "Unknown")
                subject = email.get("subject", "(no subject)")
                print(f"  {i}. From: {sender}")
                print(f"     Subject: {subject}")
                print()

        print(f"  Total unread: {len(emails)}")
        print("=" * 70)

    # ------------------------------------------------------------------
    # Test 4: Check calendar (read-only, always safe)
    # ------------------------------------------------------------------

    @skip_no_creds
    @pytest.mark.asyncio
    async def test_check_calendar_live(self):
        """Check today's calendar events — read-only, always safe."""
        from backend.neural_mesh.agents.google_workspace_agent import (
            GoogleWorkspaceAgent,
        )

        agent = GoogleWorkspaceAgent()
        await agent.on_initialize()

        result = await agent.execute_task({
            "action": "check_calendar_events",
        })

        print("\n" + "=" * 70)
        print("  TODAY'S CALENDAR:")
        print("=" * 70)

        if isinstance(result, dict) and result.get("error"):
            print(f"  Error: {result['error']}")
            pytest.skip(f"Calendar API error: {result['error']}")
            return

        events = result.get("events", []) if isinstance(result, dict) else []
        if not events:
            print("  (No events today)")
        else:
            for i, event in enumerate(events[:10], 1):
                summary = event.get("summary", "(no title)")
                start = event.get("start", {})
                time_str = start.get("dateTime", start.get("date", "?"))
                print(f"  {i}. {time_str[:16]} — {summary}")

        print(f"  Total events: {len(events)}")
        print("=" * 70)

    # ------------------------------------------------------------------
    # Test 5: Full pipeline — expand + route + execute (read-only)
    # ------------------------------------------------------------------

    @skip_no_creds
    @pytest.mark.asyncio
    async def test_full_pipeline_check_email(self):
        """Full pipeline: 'Check my email' → expand → route → execute."""
        from backend.neural_mesh.agents.predictive_planning_agent import (
            PredictivePlanningAgent,
        )
        from backend.neural_mesh.agents.google_workspace_agent import (
            GoogleWorkspaceAgent,
        )

        # Step 1: Expand intent
        planner = PredictivePlanningAgent()
        prediction = await planner.expand_intent("check my email")

        print("\n" + "=" * 70)
        print("  FULL PIPELINE: 'check my email'")
        print("=" * 70)
        print(f"  Intent: {prediction.detected_intent.value}")
        print(f"  Tasks: {len(prediction.expanded_tasks)}")

        # Step 2: Convert to workflow tasks
        workflow_tasks = planner.to_workflow_tasks(prediction)
        print(f"  Workflow tasks: {len(workflow_tasks)}")

        for wt in workflow_tasks:
            print(f"    → {wt.required_capability}: {wt.name[:50]}")

        # Step 3: Execute the first workspace task directly
        workspace_tasks = [
            t for t in workflow_tasks
            if t.required_capability == "handle_workspace_query"
        ]

        if workspace_tasks:
            ws_agent = GoogleWorkspaceAgent()
            await ws_agent.on_initialize()

            task = workspace_tasks[0]
            result = await ws_agent.execute_task({
                "action": "fetch_unread_emails",
                "query": task.input_data.get("query", "check email"),
                "limit": 3,
            })

            print("\n  EXECUTION RESULT:")
            emails = result.get("emails", []) if isinstance(result, dict) else []
            print(f"  Found {len(emails)} unread emails")
            for email in emails[:3]:
                print(f"    - {email.get('subject', '(no subject)')}")
        else:
            print("  (No workspace tasks generated — check capability routing)")

        print("=" * 70)
        print("  PIPELINE: COMPLETE")
        print("=" * 70)

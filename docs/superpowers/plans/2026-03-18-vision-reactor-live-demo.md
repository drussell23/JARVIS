# J-Prime Vision + Reactor Learning + Live Demo Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement.

**Goal:** Wire J-Prime LLaVA vision into NativeAppControlAgent/VisualBrowserAgent, emit failure experiences to Reactor, and run a live demo of JARVIS visually controlling WhatsApp and Gmail.

**Architecture:** NativeAppControlAgent and VisualBrowserAgent already have vision method stubs. Wire them to the real PrimeClient.send_vision_request() (port 8001, LLaVA). Add CrossRepoExperienceForwarder calls for tier fallbacks and app failures. Build a live demo that shows JARVIS typing in WhatsApp and composing in Gmail.

**Tech Stack:** PrimeClient (send_vision_request), CrossRepoExperienceForwarder, Playwright, PyAutoGUI/AppleScript, screencapture.

---

## Task 1: Fix NativeAppControlAgent vision to use real PrimeClient API

Verify and fix `_ask_jprime_for_action()` to use `get_jarvis_prime_client()` + `send_vision_request()` with proper structured JSON prompt.

## Task 2: Fix VisualBrowserAgent vision to use real PrimeClient API

Same fix for `_ask_vision_model()`.

## Task 3: Add Reactor failure learning to ExecutionTierRouter + orchestrator

When: app not installed, tier fallback triggered, task failed after retries. Emit via CrossRepoExperienceForwarder.forward_experience().

## Task 4: Live demo — JARVIS controls WhatsApp and Gmail visually

Script that: opens WhatsApp, types a message via vision loop, opens Gmail compose, types an email draft visually.

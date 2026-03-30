# Trinity Cloud Split — Plan 3: Apple Clients (Watch + iPhone)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build native Swift apps for Apple Watch Ultra 2 and iPhone 15 Pro Max that connect to the Vercel nervous system. Watch provides voice-from-wrist via Action Button. iPhone provides a mobile command center with Ouroboros PR review.

**Architecture:** Shared `JARVISKit` Swift Package (HMAC auth, SSE parsing, command sending) consumed by both `JARVISWatch` (watchOS) and `JARVISPhone` (iOS) app targets. All communication goes through the Vercel API routes already deployed at `jarvis-cloud-five.vercel.app`.

**Tech Stack:** Swift 6, SwiftUI, CryptoKit (HMAC-SHA256), URLSession (SSE streaming), Apple Speech framework (on-device STT), WatchKit, WatchConnectivity

**Spec:** `docs/superpowers/specs/2026-03-29-trinity-cloud-split-design.md` — Section 7

**Depends on:** Plan 1 (Vercel App — deployed and verified)

**Apple Developer Account:** Required for Watch/iPhone deployment and APNs push notifications.

---

## Project Structure

This plan creates a new Xcode workspace with 3 targets:

```
JARVIS-Apple/
  JARVIS-Apple.xcworkspace
  JARVISKit/                          # Shared Swift Package
    Package.swift
    Sources/JARVISKit/
      Auth/
        DeviceAuth.swift              # HMAC-SHA256 signing
        KeychainStore.swift           # Secure storage
        StreamToken.swift             # Opaque token request
      Networking/
        CommandSender.swift           # POST /api/command
        SSEClient.swift               # URLSession SSE consumer
        APITypes.swift                # All shared types
      Voice/
        SpeechTranscriber.swift       # Apple Speech wrapper
    Tests/JARVISKitTests/
      DeviceAuthTests.swift
      SSEParserTests.swift
      CommandSenderTests.swift
  JARVISWatch/                        # watchOS app target
    JARVISWatchApp.swift
    Views/
      StatusView.swift
      ActiveCommandView.swift
    Services/
      WatchSessionManager.swift
      HapticEngine.swift
  JARVISPhone/                        # iOS app target
    JARVISPhoneApp.swift
    Views/
      CommandCenter/
        CommandInputView.swift
        StreamingResponseView.swift
      Ouroboros/
        PRQueueView.swift
      Settings/
        PairingView.swift
        SettingsView.swift
    Services/
      PhoneSessionManager.swift
      PushHandler.swift
```

---

## Phase A: JARVISKit (Shared Package) — 6 tasks

Builds the shared Swift library that both Watch and iPhone apps consume.

### Task 1: Package scaffold + types
### Task 2: HMAC auth (DeviceAuth + KeychainStore)
### Task 3: SSE parser + client
### Task 4: Command sender
### Task 5: Speech transcriber
### Task 6: Package tests (auth + SSE + sender)

## Phase B: JARVISWatch (watchOS) — 3 tasks

### Task 7: Watch app scaffold + status view
### Task 8: Action Button + voice intake flow
### Task 9: SSE integration + haptics

## Phase C: JARVISPhone (iOS) — 3 tasks

### Task 10: Phone app scaffold + command center
### Task 11: Ouroboros PR review UI
### Task 12: Settings + pairing + push notifications

---

**Total: 12 tasks across 3 phases**

**Note:** This plan provides the architecture and task breakdown. Detailed step-by-step code for each task will be provided during execution, following the same subagent-driven pattern as Plans 1-2. The Swift code must match the HMAC canonical format exactly as implemented in the TypeScript server and Python brainstem.

---

## Deferred

- Watch complications (show last daemon message on watch face)
- Watch standalone mode (when iPhone is not nearby)
- iPhone widgets (command shortcuts, device status)
- APNs server-side implementation (Vercel → Apple Push)

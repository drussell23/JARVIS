// JARVISHUD/Services/HiveStore.swift
// @Observable store for Hive events received via IPC.
// Fans out from BrainstemLauncher alongside AppState (PythonBridge).

import Foundation
import Observation

@Observable
class HiveStore {

    // MARK: - Published State

    var cognitiveState: CognitiveStateValue = .baseline
    var threads: [HiveThread] = []

    var activeThreadCount: Int {
        threads.filter(\.isActive).count
    }

    var sortedThreads: [HiveThread] {
        threads.sorted { $0.lastActivityAt > $1.lastActivityAt }
    }

    // MARK: - Event Handling (called from BrainstemLauncher)

    @MainActor
    func handleEvent(eventType: String, data: [String: Any]) {
        switch eventType {
        case "agent_log", "persona_reasoning":
            handleMessage(eventType: eventType, data: data)
        case "thread_lifecycle":
            handleThreadLifecycle(data: data)
        case "cognitive_transition":
            handleCognitiveTransition(data: data)
        default:
            break
        }
    }

    // MARK: - Private Handlers

    @MainActor
    private func handleMessage(eventType: String, data: [String: Any]) {
        guard let (threadId, message) = HiveEventParser.parseMessage(eventType: eventType, data: data) else { return }

        if let index = threads.firstIndex(where: { $0.id == threadId }) {
            threads[index].messages.append(message)
            threads[index].lastActivityAt = Date()
            if case .personaReasoning(let pr) = message {
                threads[index].tokensConsumed += pr.tokenCost
            }
        } else {
            // Thread not seen yet — create placeholder from first message
            let title = (data["category"] as? String)?.replacingOccurrences(of: "_", with: " ").capitalized ?? "Thread \(threadId.suffix(6))"
            var thread = HiveThread(
                id: threadId,
                title: title,
                state: .open,
                messages: [message],
                tokensConsumed: 0,
                tokenBudget: 50000,
                linkedOpId: nil,
                lastActivityAt: Date()
            )
            if case .personaReasoning(let pr) = message {
                thread.tokensConsumed = pr.tokenCost
            }
            threads.append(thread)
        }
    }

    @MainActor
    private func handleThreadLifecycle(data: [String: Any]) {
        guard let (threadId, state) = HiveEventParser.parseThreadLifecycle(data: data) else { return }

        if let index = threads.firstIndex(where: { $0.id == threadId }) {
            threads[index].state = state
            threads[index].lastActivityAt = Date()
            if let opId = data["linked_op_id"] as? String {
                threads[index].linkedOpId = opId
            }
        } else {
            // Thread lifecycle arrived before any messages — create placeholder
            let title = (data["title"] as? String) ?? "Thread \(threadId.suffix(6))"
            threads.append(HiveThread(
                id: threadId,
                title: title,
                state: state,
                messages: [],
                tokensConsumed: 0,
                tokenBudget: 50000,
                linkedOpId: data["linked_op_id"] as? String,
                lastActivityAt: Date()
            ))
        }
    }

    @MainActor
    private func handleCognitiveTransition(data: [String: Any]) {
        if let newState = HiveEventParser.parseCognitiveTransition(data: data) {
            cognitiveState = newState
        }
    }
}

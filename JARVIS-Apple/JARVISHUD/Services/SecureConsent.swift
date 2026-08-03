//  SecureConsent.swift
//  JARVISHUD
//
//  Touch ID / Apple Watch approval for gated macOS capabilities.
//
//  WHY THIS LIVES IN SWIFT AND NOT PYTHON
//  ---------------------------------------
//  Measured on the backend interpreter:
//
//      LAContext.canEvaluatePolicy(.deviceOwnerAuthentication) -> false
//      biometryType                                            -> 0 (none)
//      NSBundle.mainBundle().bundleIdentifier()                -> nil
//
//  LocalAuthentication requires a SIGNED APP BUNDLE identity. A bare
//  interpreter has none, so LAContext refuses every policy regardless of the
//  hardware. That is not an obstacle to route around — it is macOS declining
//  to let an unsigned process speak for the Secure Enclave, and it is the
//  reason the prompt belongs here. A Python-side LAContext call would have
//  been the spoofable path, not the safe one.
//
//  THE NONCE
//  ---------
//  The signed bundle stops an unsigned process from ASKING. It does nothing
//  about REPLAY — anything that can write to the local IPC socket could
//  capture one `{"approved": true}` and resend it forever. So Python mints a
//  one-time `secrets.token_urlsafe(32)` challenge when it suspends the call,
//  and a verdict is only an answer if it echoes THAT challenge. Python
//  compares with `hmac.compare_digest` and pops it on use.
//
//  We never generate the nonce here and never cache one. Echo only.

import Foundation
import LocalAuthentication

@MainActor
final class SecureConsent {

    static let shared = SecureConsent()
    private init() {}

    /// A consent request as it arrives from the backend over the Brainstem IPC.
    struct Challenge {
        let requestId: String
        let nonce: String
        let capability: String
        let detail: String

        init?(_ data: [String: Any]) {
            guard let requestId = data["request_id"] as? String,
                  let nonce = data["nonce"] as? String,
                  !requestId.isEmpty, !nonce.isEmpty else {
                // Fail CLOSED on a malformed challenge. A request we cannot
                // identify is a request we must not approve — and silently
                // treating it as "no consent needed" is how a gate becomes
                // decorative.
                return nil
            }
            self.requestId = requestId
            self.nonce = nonce
            self.capability = (data["capability"] as? String) ?? "unknown"
            self.detail = (data["detail"] as? String) ?? ""
        }
    }

    /// Prompt for owner authentication, then answer the backend.
    ///
    /// Uses `.deviceOwnerAuthentication` rather than
    /// `.deviceOwnerAuthenticationWithBiometrics` deliberately: the biometrics
    /// policy has NO password fallback, so a wet finger or a closed lid leaves
    /// the operator unable to approve anything at all. `.deviceOwnerAuthentication`
    /// falls back to the login password, which is the same secret Touch ID is
    /// standing in for — no weaker, and it cannot lock the owner out.
    ///
    /// No custom UI: the system dialog IS the prompt. Anything we drew
    /// ourselves would be a phishable imitation of it.
    func request(_ challenge: Challenge) {
        let context = LAContext()
        context.localizedCancelTitle = "Deny"

        // A fresh context per request. Reusing one lets macOS honour a recent
        // successful evaluation without re-prompting (`touchIDAuthenticationAllowableReuseDuration`),
        // which would silently approve a SECOND capability on the strength of
        // the first — exactly the replay the nonce exists to prevent, arriving
        // from inside the OS instead of over the socket.
        context.touchIDAuthenticationAllowableReuseDuration = 0

        var policyError: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication,
                                        error: &policyError) else {
            // No enrolled biometric AND no password path. Deny, and say why —
            // an operator who cannot approve needs to know that is the reason,
            // not watch a request vanish.
            respond(challenge, approved: false,
                    reason: "owner authentication unavailable: "
                          + (policyError?.localizedDescription ?? "unknown"))
            return
        }

        let reason = challenge.detail.isEmpty
            ? "authorise \(challenge.capability)"
            : challenge.detail

        context.evaluatePolicy(.deviceOwnerAuthentication,
                               localizedReason: reason) { [weak self] ok, error in
            // The callback lands on an arbitrary queue; every field we touch is
            // MainActor-isolated.
            Task { @MainActor in
                guard let self else { return }
                if ok {
                    self.respond(challenge, approved: true, reason: "")
                } else {
                    let code = (error as? LAError)?.code
                    self.respond(challenge, approved: false,
                                 reason: Self.describe(code, error))
                }
            }
        }
    }

    /// Echo the challenge back with the verdict. The nonce is the whole point.
    private func respond(_ challenge: Challenge, approved: Bool, reason: String) {
        BrainstemLauncher.shared.sendEvent(
            eventType: "consent_verdict",
            data: [
                "request_id": challenge.requestId,
                // ECHOED, never regenerated. Python pops it and compares in
                // constant time; a verdict without it is not an answer.
                "nonce": challenge.nonce,
                "approved": approved,
                "status": approved ? "APPROVED" : "REJECTED",
                "capability": challenge.capability,
                "reason": reason,
            ]
        )
    }

    /// Distinguish "the operator said no" from "the machine could not ask".
    /// Both deny, and they need opposite follow-ups.
    private static func describe(_ code: LAError.Code?, _ error: Error?) -> String {
        switch code {
        case .userCancel:            return "operator denied"
        case .userFallback:          return "operator chose password fallback"
        case .authenticationFailed:  return "authentication failed"
        case .systemCancel:          return "system cancelled the prompt"
        case .appCancel:             return "app cancelled the prompt"
        case .biometryLockout:       return "biometry locked out — password required"
        case .biometryNotEnrolled:   return "no biometry enrolled"
        case .biometryNotAvailable:  return "biometry unavailable"
        default:
            return error?.localizedDescription ?? "denied"
        }
    }
}

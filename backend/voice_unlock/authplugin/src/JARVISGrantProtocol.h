/**
 * JARVISGrantProtocol.h
 * The contract between the Authorization Plugin and the grant broker.
 *
 * Shared by both sides so the wire shape has exactly one definition. The plugin
 * links this header; the broker links this header; there is no second copy to
 * drift.
 *
 * WHAT A GRANT IS, AND WHAT IT IS NOT
 * -----------------------------------
 * A grant is an assertion by the JARVIS backend that a human it has already
 * authenticated -- by voice, and by whatever else the backend layers on -- asked
 * for the screen to be unlocked, just now. It is NOT a credential. It carries no
 * password, no key material, and nothing that could unlock anything if it
 * leaked. The worst a stolen grant can do is unlock a screen once, within its
 * TTL, on the machine that issued it.
 *
 * That property is the whole point of this design. The system it replaces kept
 * the user's actual login password in cleartext.
 */

#ifndef JARVIS_GRANT_PROTOCOL_H
#define JARVIS_GRANT_PROTOCOL_H

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// Schema version of the grant payload. Bumped on any breaking field change so
/// a stale plugin and a fresh broker refuse each other loudly rather than
/// misinterpreting each other's dictionaries.
extern NSString *const JARVISGrantSchemaVersion;

#pragma mark - Verdict

/**
 * What the broker says when the plugin asks.
 *
 * Deliberately NOT a BOOL. "no grant available" and "the broker is unreachable"
 * must be distinguishable in the plugin's logs, because they mean completely
 * different things operationally -- one is the normal resting state on every
 * manual unlock, the other means the daemon died. Collapsing them into `false`
 * is how a broken daemon hides behind a working one.
 *
 * Every value other than Granted yields to the native password UI. There is no
 * verdict that denies the user their own machine: the plugin's only power is to
 * let someone in early, never to keep them out.
 */
typedef NS_ENUM(NSInteger, JARVISGrantVerdict) {
    /// A valid, unexpired, single-use grant existed and has been consumed.
    JARVISGrantVerdictGranted = 0,
    /// The broker answered, and there is no grant. The overwhelmingly common case.
    JARVISGrantVerdictNoGrant = 1,
    /// A grant existed but had aged past its TTL. Reported separately from
    /// NoGrant so a chronically-too-short TTL is visible as itself.
    JARVISGrantVerdictExpired = 2,
    /// The broker rejected US -- our code signature did not satisfy its
    /// requirement. Should be impossible in a correct install; if it is ever
    /// seen, something is impersonating one side of this channel.
    JARVISGrantVerdictRejected = 3,
    /// No answer inside the deadline. The dead man's switch of the auth path.
    JARVISGrantVerdictTimedOut = 4,
    /// Broker unreachable: not running, not registered, or refusing connections.
    JARVISGrantVerdictUnavailable = 5,
    /// The operator physically held the panic key. Not even asked.
    JARVISGrantVerdictPanicBypass = 6,
};

/// Human-readable name for a verdict, for logs. Never returns nil.
extern NSString *JARVISGrantVerdictName(JARVISGrantVerdict verdict);

#pragma mark - Wire protocol

/**
 * The XPC interface the broker vends.
 *
 * One method. The narrowness is intentional: the attack surface of an XPC
 * service is its interface, and this one cannot be asked to do anything except
 * answer a yes/no about a grant that the caller cannot influence.
 *
 * Note what is absent -- there is no `depositGrant:` here. Deposits arrive on a
 * separate, separately-validated interface, so that a compromised plugin (which
 * runs inside SecurityAgent and is the more exposed side) cannot mint the very
 * grants it consumes.
 */
@protocol JARVISGrantConsumer <NSObject>

/**
 * Atomically consume any valid grant.
 *
 * Consumption is single-use and happens broker-side under a lock: two
 * simultaneous unlock attempts cannot both be satisfied by one grant, and a
 * replayed request finds nothing.
 *
 * @param schemaVersion Caller's schema. Mismatch yields Rejected rather than a
 *                      best-effort interpretation of an unknown payload.
 * @param reply         Verdict plus a correlation id for cross-log stitching.
 */
- (void)consumeGrantWithSchemaVersion:(NSString *)schemaVersion
                                reply:(void (^)(JARVISGrantVerdict verdict,
                                                NSString *_Nullable correlationId))reply;

@end

/**
 * The interface JARVIS itself uses to deposit a grant. Separate from
 * JARVISGrantConsumer on purpose -- see above.
 */
@protocol JARVISGrantDepositor <NSObject>

/**
 * Deposit a single-use grant.
 *
 * @param schemaVersion Caller's schema version.
 * @param ttlSeconds    Requested lifetime. The broker clamps this to its own
 *                      configured ceiling; a caller cannot mint an immortal
 *                      grant by asking for one.
 * @param reason        Short operator-facing string for the audit log, e.g.
 *                      "voice: unlock my screen". Never contains credentials.
 * @param reply         Whether the deposit was accepted, and the grant id.
 */
- (void)depositGrantWithSchemaVersion:(NSString *)schemaVersion
                           ttlSeconds:(NSTimeInterval)ttlSeconds
                               reason:(NSString *)reason
                                reply:(void (^)(BOOL accepted,
                                                NSString *_Nullable grantId))reply;

@end

NS_ASSUME_NONNULL_END

#endif /* JARVIS_GRANT_PROTOCOL_H */

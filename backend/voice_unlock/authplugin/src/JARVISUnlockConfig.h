/**
 * JARVISUnlockConfig.h
 * Every tunable in the plugin, resolved once, from the bundle's Info.plist.
 *
 * WHY INFO.PLIST AND NOT ENVIRONMENT VARIABLES
 * --------------------------------------------
 * The rest of JARVIS is env-var driven, and copying that here would be cargo
 * culting rather than DRY. This code runs inside SecurityAgent, a process the
 * operator does not launch and whose environment they cannot set. An env var
 * would be unreadable in production and settable only in a test harness, which
 * is the worst possible property for a security control: configuration that
 * exists for tests and is inert in the field.
 *
 * Info.plist is the macOS-native configuration channel for a bundle. It is
 * readable from inside SecurityAgent, it is covered by the bundle's code
 * signature -- so tampering with a tunable invalidates the signature that the
 * broker checks -- and it is inspectable with `plutil` during an incident.
 *
 * Defaults are the fallback if a key is absent or malformed, never a silent
 * substitute for a key that is present and out of range: those are clamped and
 * logged, because a value the operator wrote and the code ignored is a lie.
 */

#ifndef JARVIS_UNLOCK_CONFIG_H
#define JARVIS_UNLOCK_CONFIG_H

#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>

NS_ASSUME_NONNULL_BEGIN

@interface JARVISUnlockConfig : NSObject

/// Resolved once per plugin load from the bundle containing this code.
+ (instancetype)sharedConfig;

/**
 * How long the mechanism will wait for the broker before yielding to the
 * native password UI.
 *
 * This is the dead man's switch of the authentication path. It is small by
 * design and clamped hard: the cost of being too slow is a user staring at a
 * frozen lock screen, which is the failure mode that turns an unlock feature
 * into a support call. Key: JARVISGrantTimeoutMilliseconds. Default 500ms.
 * Clamped to [50, 2000].
 */
@property (nonatomic, readonly) NSTimeInterval grantTimeoutSeconds;

/**
 * Mach service name of the grant broker.
 *
 * Key: JARVISBrokerMachServiceName. No default -- if this is absent the plugin
 * refuses to look anything up and yields immediately. A missing service name
 * must not fall back to a guessed one; guessing which Mach service to hand an
 * unlock question to is how you get answered by the wrong process.
 */
@property (nonatomic, readonly, nullable) NSString *brokerMachServiceName;

/**
 * Code signing requirement the broker must satisfy for the plugin to trust its
 * answer. Key: JARVISBrokerCodeRequirement.
 *
 * Validation is mutual. The broker checking the plugin is the obvious half; the
 * plugin checking the broker is the half people forget, and it is the half that
 * matters here, because the plugin is asking "should I let this person in" and
 * a spoofed answer is an unlock.
 */
@property (nonatomic, readonly, nullable) NSString *brokerCodeRequirement;

/**
 * Virtual keycode which, held during Invoke, bypasses JARVIS entirely.
 *
 * Key: JARVISPanicKeyCode. Default kVK_Option (0x3A). The escape hatch for a
 * misbehaving daemon that a user needs to get past without a terminal -- hold
 * the key, get the stock password prompt, no matter what any software thinks.
 */
@property (nonatomic, readonly) CGKeyCode panicKeyCode;

/// Whether the panic choke is consulted at all. Key: JARVISPanicChokeEnabled.
/// Default YES. Present so it can be disabled in automated tests, never in the
/// shipped Info.plist.
@property (nonatomic, readonly) BOOL panicChokeEnabled;

/**
 * Master switch. Key: JARVISGrantMechanismEnabled. Default YES.
 *
 * Disabled means the mechanism loads, logs, and immediately yields -- the
 * cheapest possible no-op. It deliberately does NOT mean "do not load": a
 * mechanism named in the authorization database that fails to load is a broken
 * chain, and the safe way to turn this off without editing the database is to
 * make it trivially transparent.
 */
@property (nonatomic, readonly) BOOL mechanismEnabled;

/// Schema version this build speaks. Read from the bundle so the plugin and
/// broker versions are visible in `plutil` output during an incident.
@property (nonatomic, readonly) NSString *schemaVersion;

/// Snapshot of the resolved values, for a single log line at load.
- (NSString *)describeResolved;

@end

NS_ASSUME_NONNULL_END

#endif /* JARVIS_UNLOCK_CONFIG_H */

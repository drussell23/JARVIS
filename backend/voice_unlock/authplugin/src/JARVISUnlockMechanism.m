/**
 * JARVISUnlockMechanism.m
 * The Authorization Plugin that lets a voice-authenticated unlock satisfy
 * system.login.screensaver without a password existing anywhere.
 *
 * THE ONE INVARIANT
 * -----------------
 * This code runs inside SecurityAgent, on the path between a human and their
 * own locked machine. It may make that path FASTER. It may never make it
 * SLOWER, and it may never make it IMPOSSIBLE.
 *
 * Everything below follows from that. There is no code path that returns
 * kAuthorizationResultDeny. There is no code path that blocks the invoking
 * thread. There is no error -- unreachable broker, malformed config, spoofed
 * signature, panicking Objective-C runtime -- that produces anything except
 * "yield to the native password UI". The plugin's only power is to let someone
 * in early. It cannot keep anyone out.
 *
 * WHY IT DOES NOT BLOCK
 * ---------------------
 * MechanismInvoke returns noErr immediately, and SetResult is called later from
 * whichever of two racers arrives first: the broker's XPC reply, or a
 * dispatch_after deadline. SecurityAgent's thread is never held for even the
 * 500ms budget.
 *
 * A semaphore-and-wait would have been three lines shorter and is the shape
 * most examples use. It is also how you wedge a lock screen: SecurityAgent
 * would sit inside our frame for the whole timeout, and any bug that lost the
 * reply would hold it forever. The AuthorizationPlugin API is asynchronous
 * precisely so that a mechanism cannot do this, and taking the synchronous
 * shortcut would discard the guarantee the API exists to provide.
 *
 * The deadline is armed BEFORE the request is sent, for the same reason the
 * probe arms its dead man's switch before mutating the rule: there must be no
 * window in which the operation is in flight and nothing is scheduled to end it.
 */

#import <Foundation/Foundation.h>
#import <Security/AuthorizationPlugin.h>
#import <Security/AuthSession.h>
#import <CoreGraphics/CoreGraphics.h>
#import <os/log.h>
#import <stdatomic.h>

#import "JARVISGrantProtocol.h"
#import "JARVISUnlockConfig.h"

#pragma mark - Plugin / mechanism state

typedef struct {
    const AuthorizationCallbacks *callbacks;
    os_log_t log;
} JARVISPlugin;

typedef struct {
    JARVISPlugin *plugin;
    AuthorizationEngineRef engine;
    /// Guarantees exactly one SetResult per invocation, whichever racer wins.
    atomic_flag resultDelivered;
} JARVISMechanism;

#pragma mark - Result delivery

/**
 * Deliver the verdict, exactly once.
 *
 * The timeout and the XPC reply race by design, and both call this. The atomic
 * flag is what makes that safe: a second SetResult on the same engine is
 * undefined behaviour in an API whose failure mode is a stuck lock screen.
 */
static void JARVISDeliver(JARVISMechanism *mech,
                          AuthorizationResult result,
                          JARVISGrantVerdict verdict,
                          NSTimeInterval elapsed) {
    if (atomic_flag_test_and_set(&mech->resultDelivered)) {
        // Lost the race. The winner already answered; say so at debug level
        // rather than dropping it silently, because a persistent stream of
        // these means the timeout is mistuned.
        os_log_debug(mech->plugin->log,
                     "verdict %{public}@ arrived after the result was delivered (%.0fms)",
                     JARVISGrantVerdictName(verdict), elapsed * 1000.0);
        return;
    }

    os_log(mech->plugin->log,
           "verdict=%{public}@ result=%{public}s elapsed=%.0fms",
           JARVISGrantVerdictName(verdict),
           result == kAuthorizationResultAllow ? "allow" : "yield",
           elapsed * 1000.0);

    OSStatus status = mech->plugin->callbacks->SetResult(mech->engine, result);
    if (status != errAuthorizationSuccess) {
        os_log_error(mech->plugin->log, "SetResult failed: %d", (int)status);
    }
}

/**
 * Yield to the native password UI.
 *
 * kAuthorizationResultAllow, not Deny. In an evaluate-mechanisms chain, Allow
 * means "this mechanism is satisfied, continue" -- the later builtin:authenticate
 * then does the real work and prompts as it always has. Deny would fail the
 * whole right and lock the user out of their own machine, which is the outcome
 * this plugin exists to never cause.
 *
 * That distinction is the single most load-bearing line in this file.
 */
static void JARVISYield(JARVISMechanism *mech, JARVISGrantVerdict why, NSTimeInterval elapsed) {
    JARVISDeliver(mech, kAuthorizationResultAllow, why, elapsed);
}

#pragma mark - Hardware panic choke

/**
 * Is the operator physically holding the panic key right now?
 *
 * Read from kCGEventSourceStateHIDSystemState -- the hardware state, not the
 * combined state a synthetic event could forge. Nothing in software can hold
 * this key down on the operator's behalf, which is exactly the property wanted:
 * an escape hatch that outranks every other layer here, usable by someone
 * standing at a locked machine with no terminal and no idea what went wrong.
 *
 * Fails toward bypass. If this cannot be determined, assume the human wants out.
 */
static BOOL JARVISPanicHeld(JARVISUnlockConfig *config, os_log_t log) {
    if (!config.panicChokeEnabled) { return NO; }

    @try {
        if (CGEventSourceKeyState(kCGEventSourceStateHIDSystemState, config.panicKeyCode)) {
            os_log(log, "panic choke: key 0x%02X held; bypassing JARVIS entirely",
                   (unsigned)config.panicKeyCode);
            return YES;
        }
        return NO;
    } @catch (NSException *e) {
        os_log_error(log, "panic choke probe threw (%{public}@); assuming bypass",
                     e.name);
        return YES;
    }
}

#pragma mark - Broker query

/**
 * Ask the broker whether a grant exists, without ever waiting for the answer.
 *
 * Returns immediately. Exactly one of two things will later call JARVISDeliver:
 * the reply block, or the deadline armed here before the request goes out.
 */
static void JARVISAskBroker(JARVISMechanism *mech, JARVISUnlockConfig *config) {
    os_log_t log = mech->plugin->log;
    NSDate *started = [NSDate date];

    // Arm the deadline FIRST. If anything below throws, misbehaves, or simply
    // never calls back, this still fires and the user still gets their prompt.
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW,
                                 (int64_t)(config.grantTimeoutSeconds * NSEC_PER_SEC)),
                   dispatch_get_global_queue(QOS_CLASS_USER_INTERACTIVE, 0), ^{
        JARVISYield(mech, JARVISGrantVerdictTimedOut,
                    -[started timeIntervalSinceNow]);
    });

    NSString *service = config.brokerMachServiceName;
    NSString *requirement = config.brokerCodeRequirement;
    if (service == nil || requirement == nil) {
        // Config already logged the specifics at load. Yield now rather than
        // making the user wait out a timeout for a decision already made.
        JARVISYield(mech, JARVISGrantVerdictUnavailable, -[started timeIntervalSinceNow]);
        return;
    }

    NSXPCConnection *connection =
        [[NSXPCConnection alloc] initWithMachServiceName:service
                                                 options:NSXPCConnectionPrivileged];
    connection.remoteObjectInterface =
        [NSXPCInterface interfaceWithProtocol:@protocol(JARVISGrantConsumer)];

    // Mutual validation. The broker checks us; we check the broker. A plugin
    // that trusted whatever answered its Mach lookup would take "unlock this
    // screen" from any process that won the name.
    //
    // Availability-guarded rather than assumed: if the OS cannot enforce the
    // requirement, we do not proceed with an unverified peer. Yielding costs
    // the user one password entry; trusting an unchecked broker costs them the
    // machine.
    if (@available(macOS 13.0, *)) {
        [connection setCodeSigningRequirement:requirement];
    } else {
        os_log_error(log,
                     "code signing requirements unsupported on this OS; "
                     "refusing to trust an unverified broker");
        JARVISYield(mech, JARVISGrantVerdictRejected, -[started timeIntervalSinceNow]);
        [connection invalidate];
        return;
    }

    // Both handlers yield. An interrupted or invalidated connection is
    // indistinguishable from a dead broker from here, and both mean "prompt".
    connection.interruptionHandler = ^{
        JARVISYield(mech, JARVISGrantVerdictUnavailable, -[started timeIntervalSinceNow]);
    };
    connection.invalidationHandler = ^{
        JARVISYield(mech, JARVISGrantVerdictUnavailable, -[started timeIntervalSinceNow]);
    };

    [connection resume];

    id<JARVISGrantConsumer> broker =
        [connection remoteObjectProxyWithErrorHandler:^(NSError *error) {
            os_log_error(log, "broker proxy error: %{public}@", error.localizedDescription);
            JARVISYield(mech, JARVISGrantVerdictUnavailable, -[started timeIntervalSinceNow]);
        }];

    [broker consumeGrantWithSchemaVersion:config.schemaVersion
                                    reply:^(JARVISGrantVerdict verdict,
                                            NSString *correlationId) {
        NSTimeInterval elapsed = -[started timeIntervalSinceNow];
        if (correlationId.length > 0) {
            os_log_debug(log, "broker correlation=%{public}@", correlationId);
        }
        if (verdict == JARVISGrantVerdictGranted) {
            JARVISDeliver(mech, kAuthorizationResultAllow, verdict, elapsed);
        } else {
            JARVISYield(mech, verdict, elapsed);
        }
        [connection invalidate];
    }];
}

#pragma mark - AuthorizationPlugin interface

static OSStatus JARVISMechanismCreate(AuthorizationPluginRef inPlugin,
                                      AuthorizationEngineRef inEngine,
                                      AuthorizationMechanismId mechanismId,
                                      AuthorizationMechanismRef *outMechanism) {
    if (inPlugin == NULL || outMechanism == NULL) { return errAuthorizationInternal; }

    JARVISPlugin *plugin = (JARVISPlugin *)inPlugin;
    JARVISMechanism *mech = calloc(1, sizeof(JARVISMechanism));
    if (mech == NULL) { return errAuthorizationInternal; }

    mech->plugin = plugin;
    mech->engine = inEngine;
    atomic_flag_clear(&mech->resultDelivered);

    os_log_debug(plugin->log, "mechanism created: %{public}s",
                 mechanismId ? mechanismId : "(unnamed)");

    *outMechanism = (AuthorizationMechanismRef)mech;
    return errAuthorizationSuccess;
}

static OSStatus JARVISMechanismInvoke(AuthorizationMechanismRef inMechanism) {
    JARVISMechanism *mech = (JARVISMechanism *)inMechanism;
    if (mech == NULL) { return errAuthorizationInternal; }

    // Every invocation starts fresh: the same mechanism object is reused across
    // retries, and a flag left set from a previous attempt would swallow this
    // attempt's result and hang the chain.
    atomic_flag_clear(&mech->resultDelivered);

    @autoreleasepool {
        @try {
            JARVISUnlockConfig *config = [JARVISUnlockConfig sharedConfig];

            if (!config.mechanismEnabled) {
                JARVISYield(mech, JARVISGrantVerdictUnavailable, 0);
                return errAuthorizationSuccess;
            }

            // Checked before anything else, and before any IPC: the panic key
            // must work when the broker is the thing that is broken.
            if (JARVISPanicHeld(config, mech->plugin->log)) {
                JARVISYield(mech, JARVISGrantVerdictPanicBypass, 0);
                return errAuthorizationSuccess;
            }

            JARVISAskBroker(mech, config);
        } @catch (NSException *e) {
            // An exception escaping into SecurityAgent is how a lock screen
            // dies. Nothing gets past this frame.
            os_log_error(mech->plugin->log,
                         "unhandled exception in Invoke (%{public}@); yielding",
                         e.name);
            JARVISYield(mech, JARVISGrantVerdictUnavailable, 0);
        }
    }

    // Returns immediately in every path above. SetResult happens later, from
    // whichever racer wins.
    return errAuthorizationSuccess;
}

static OSStatus JARVISMechanismDeactivate(AuthorizationMechanismRef inMechanism) {
    JARVISMechanism *mech = (JARVISMechanism *)inMechanism;
    if (mech == NULL) { return errAuthorizationInternal; }
    return mech->plugin->callbacks->DidDeactivate(mech->engine);
}

static OSStatus JARVISMechanismDestroy(AuthorizationMechanismRef inMechanism) {
    if (inMechanism != NULL) { free(inMechanism); }
    return errAuthorizationSuccess;
}

static OSStatus JARVISPluginDestroy(AuthorizationPluginRef inPlugin) {
    if (inPlugin != NULL) { free(inPlugin); }
    return errAuthorizationSuccess;
}

static AuthorizationPluginInterface gInterface = {
    kAuthorizationPluginInterfaceVersion,
    &JARVISPluginDestroy,
    &JARVISMechanismCreate,
    &JARVISMechanismInvoke,
    &JARVISMechanismDeactivate,
    &JARVISMechanismDestroy,
};

/**
 * SecurityAgent's entry point into this bundle.
 *
 * A failure here means the mechanism cannot load, which means the authorization
 * chain names something that does not exist -- so this returns an error only for
 * conditions under which we genuinely cannot function, and never for anything
 * we could instead survive and log.
 */
OSStatus AuthorizationPluginCreate(const AuthorizationCallbacks *callbacks,
                                   AuthorizationPluginRef *outPlugin,
                                   const AuthorizationPluginInterface **outPluginInterface) {
    if (callbacks == NULL || outPlugin == NULL || outPluginInterface == NULL) {
        return errAuthorizationInternal;
    }
    if (callbacks->version < kAuthorizationCallbacksVersion) {
        return errAuthorizationInternal;
    }

    JARVISPlugin *plugin = calloc(1, sizeof(JARVISPlugin));
    if (plugin == NULL) { return errAuthorizationInternal; }

    plugin->callbacks = callbacks;
    plugin->log = os_log_create("com.jarvis.unlockplugin", "mechanism");

    // One line, at load, naming every resolved tunable. During an incident the
    // first question is always "what did it actually think its config was".
    os_log(plugin->log, "loaded: %{public}@",
           [[JARVISUnlockConfig sharedConfig] describeResolved]);

    *outPlugin = (AuthorizationPluginRef)plugin;
    *outPluginInterface = &gInterface;
    return errAuthorizationSuccess;
}

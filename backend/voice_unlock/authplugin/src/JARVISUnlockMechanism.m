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
 * whichever of two racers arrives first: the broker's XPC reply, or a deadline
 * timer. SecurityAgent's thread is never held for even the 500ms budget.
 *
 * A semaphore-and-wait would have been three lines shorter and is the shape
 * most examples use. It is also how you wedge a lock screen: SecurityAgent
 * would sit inside our frame for the whole timeout, and any bug that lost the
 * reply would hold it forever. The AuthorizationPlugin API is asynchronous
 * precisely so that a mechanism cannot do this, and taking the synchronous
 * shortcut would discard the guarantee the API exists to provide.
 *
 * WHY THE RACE ARBITER IS A REFCOUNTED OBJECT AND NOT A FLAG IN THE MECHANISM
 * --------------------------------------------------------------------------
 * This is the correction of a deterministic use-after-free that black-screened
 * the lock screen 27 times before it was found.
 *
 * The previous design armed an uncancellable `dispatch_after` and put the
 * "already delivered" atomic_flag inside the calloc'd JARVISMechanism. But
 * SecurityAgent calls MechanismDestroy -- which frees that allocation -- the
 * instant the chain advances past us, and every escaping callback here can fire
 * afterwards. So the deadline block dereferenced freed memory, and worse: the
 * guard lived inside the object whose lifetime it was supposed to be guarding,
 * which means reading the flag to ask "did I lose the race?" WAS the
 * use-after-free. A guard placed inside the thing it guards is not a guard.
 *
 * The fix is ownership, not defensiveness. JARVISDelivery is an ARC object that
 * every racer co-owns strongly. Its memory therefore cannot go away while any
 * callback can still run -- not by discipline, but by construction.
 *
 * WHY THESE BLOCKS CAPTURE `self` STRONGLY, ON PURPOSE
 * ---------------------------------------------------
 * A weak capture here would trade a crash for a hang. If the deadline block
 * held only a weak reference and the mechanism's reference were dropped first,
 * strongSelf would be nil, the block would return early, SetResult would never
 * be called, and the chain would stall forever -- the same black screen,
 * reached through nil instead of through a segfault. Weak capture is the right
 * default for a delegate. It is the wrong tool for a completion guarantee.
 *
 * That creates two intentional retain cycles: delivery -> _deadline -> timer
 * block -> delivery, and connection -> handler block -> delivery -> connection.
 * Both are broken deterministically in -teardownOnQueue, which every exit path
 * runs. This is the documented pattern for a self-owned dispatch source: strong
 * capture plus an explicit cancel, never weak references. And the cycles are
 * bounded regardless -- authorizationhosthelper hosts exactly one authorization
 * session and then exits.
 *
 * WHAT MAKES THE ENGINE HANDLE SAFE, SEPARATELY FROM THE MEMORY
 * ------------------------------------------------------------
 * Safe memory is not sufficient. AuthorizationEngineRef is owned by
 * SecurityAgent and is dead after MechanismDestroy, so calling SetResult on it
 * afterwards is a second, independent fault. All mutable state here is touched
 * only on a serial queue; -invalidate is a dispatch_sync BARRIER, so it waits
 * out any SetResult already in flight and then nils the engine, after which no
 * future SetResult can run. Only then does MechanismDestroy free.
 *
 * -deliver is async rather than sync deliberately. Enqueueing on a serial queue
 * costs sub-microseconds and holds no thread, whereas holding a lock across
 * SetResult -- which is IPC to SecurityAgent -- would reintroduce exactly the
 * kind of hang this file exists to prevent.
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

#import "JARVISGrantProtocol.h"
#import "JARVISUnlockConfig.h"

#pragma mark - Plugin / mechanism state

typedef struct {
    const AuthorizationCallbacks *callbacks;
    os_log_t log;
} JARVISPlugin;

@class JARVISDelivery;

typedef struct {
    JARVISPlugin *plugin;
    AuthorizationEngineRef engine;
    /**
     * CFBridgingRetain'd JARVISDelivery for the CURRENT invocation, or NULL.
     *
     * A void* and manual ownership because ARC does not manage Objective-C
     * pointers held in malloc'd structs. Every store here is paired with a
     * CFBridgingRelease; -Invoke and -Destroy are the only writers.
     */
    void *delivery;
} JARVISMechanism;

#pragma mark - Shared validation

/**
 * One definition of "is this handle safe to dereference", used by every entry
 * point the OS can call.
 *
 * These are not paranoia. MechanismDeactivate previously walked
 * mech->plugin->callbacks->DidDeactivate with no guard on plugin, on the same
 * teardown path where the use-after-free lived: a null plugin there is a second
 * crash in the same window.
 */
static BOOL JARVISPluginUsable(const JARVISPlugin *plugin) {
    return plugin != NULL && plugin->callbacks != NULL;
}

static BOOL JARVISMechUsable(const JARVISMechanism *mech) {
    return mech != NULL && JARVISPluginUsable(mech->plugin) && mech->engine != NULL;
}

/// Never returns NULL, so no logging call site needs its own guard.
static os_log_t JARVISLogOf(const JARVISMechanism *mech) {
    if (mech != NULL && mech->plugin != NULL && mech->plugin->log != NULL) {
        return mech->plugin->log;
    }
    return OS_LOG_DEFAULT;
}

#pragma mark - JARVISDelivery

/**
 * The race arbiter: exactly one SetResult per invocation, whichever racer wins,
 * and a hard guarantee that SOMETHING answers.
 *
 * Fail-open is a property of this class, not of its callers. Every terminal
 * condition -- timeout, dropped connection, rejected peer, nil engine, a
 * dispatch source that could not even be created -- routes to -yieldWithVerdict:
 * and advances the chain to builtin:authenticate. There is no path that
 * declines to answer.
 */
@interface JARVISDelivery : NSObject

- (instancetype)initWithCallbacks:(const AuthorizationCallbacks *)callbacks
                           engine:(AuthorizationEngineRef)engine
                              log:(os_log_t)log NS_DESIGNATED_INITIALIZER;
- (instancetype)init NS_UNAVAILABLE;

/// Arm the dead man's switch. Yields immediately if the timer cannot be made.
- (void)armDeadlineAfter:(NSTimeInterval)seconds;

/// Hand the connection over so teardown can dismantle it deterministically.
- (void)adoptConnection:(NSXPCConnection *)connection;

/// Satisfied: this mechanism is done, continue the chain.
- (void)grantWithVerdict:(JARVISGrantVerdict)verdict;

/**
 * Yield to the native password UI.
 *
 * kAuthorizationResultAllow, not Deny. In an evaluate-mechanisms chain, Allow
 * means "this mechanism is satisfied, continue" -- the later
 * builtin:authenticate then does the real work and prompts as it always has.
 * Deny would fail the whole right and lock the user out of their own machine,
 * which is the outcome this plugin exists to never cause.
 *
 * That distinction is the single most load-bearing line in this file.
 */
- (void)yieldWithVerdict:(JARVISGrantVerdict)verdict;

/**
 * Barrier. Waits out any in-flight SetResult, drops the engine so no later one
 * can run, and breaks every retain cycle. Safe to call more than once.
 *
 * MUST be called before the owning JARVISMechanism is freed.
 */
- (void)invalidate;

@end

@implementation JARVISDelivery {
    /// Serializes every mutation below. All ivars are queue-confined.
    dispatch_queue_t _q;
    const AuthorizationCallbacks *_callbacks;
    AuthorizationEngineRef _engine;
    os_log_t _log;
    BOOL _delivered;
    dispatch_source_t _deadline;
    NSXPCConnection *_connection;
    NSDate *_startedAt;
}

- (instancetype)initWithCallbacks:(const AuthorizationCallbacks *)callbacks
                           engine:(AuthorizationEngineRef)engine
                              log:(os_log_t)log {
    self = [super init];
    if (self == nil) { return nil; }

    // User-interactive: a human is staring at a locked screen waiting for this.
    _q = dispatch_queue_create("com.jarvis.unlockplugin.delivery",
                               dispatch_queue_attr_make_with_qos_class(
                                   DISPATCH_QUEUE_SERIAL, QOS_CLASS_USER_INTERACTIVE, 0));
    _callbacks = callbacks;
    _engine = engine;
    _log = (log != NULL) ? log : OS_LOG_DEFAULT;
    _delivered = NO;
    _startedAt = [NSDate date];
    return self;
}

#pragma mark Delivery

- (void)grantWithVerdict:(JARVISGrantVerdict)verdict {
    [self deliver:kAuthorizationResultAllow verdict:verdict granted:YES];
}

- (void)yieldWithVerdict:(JARVISGrantVerdict)verdict {
    [self deliver:kAuthorizationResultAllow verdict:verdict granted:NO];
}

- (void)deliver:(AuthorizationResult)result
        verdict:(JARVISGrantVerdict)verdict
        granted:(BOOL)granted {
    // Strong capture is the point: this block co-owns the arbiter, so the
    // arbiter cannot be freed before it runs. See the file header.
    dispatch_async(_q, ^{
        NSTimeInterval elapsed = -[self->_startedAt timeIntervalSinceNow];

        if (self->_delivered) {
            // Lost the race. The winner already answered; say so at debug level
            // rather than dropping it silently, because a persistent stream of
            // these means the timeout is mistuned.
            os_log_debug(self->_log,
                         "verdict %{public}@ arrived after the result was delivered (%.0fms)",
                         JARVISGrantVerdictName(verdict), elapsed * 1000.0);
            return;
        }

        if (self->_engine == NULL || !self->_callbacks || !self->_callbacks->SetResult) {
            // The engine died under us -- SecurityAgent tore the session down
            // before anyone answered. Nothing to deliver to, and nothing we can
            // do about it, but it must be visible: this is the shape a wedged
            // lock screen would have if the barrier below were ever wrong.
            os_log_error(self->_log,
                         "engine gone before delivery; verdict %{public}@ dropped (%.0fms)",
                         JARVISGrantVerdictName(verdict), elapsed * 1000.0);
            self->_delivered = YES;
            [self teardownOnQueue];
            return;
        }

        self->_delivered = YES;

        os_log(self->_log,
               "verdict=%{public}@ result=%{public}s elapsed=%.0fms",
               JARVISGrantVerdictName(verdict),
               granted ? "grant" : "yield",
               elapsed * 1000.0);

        OSStatus status = self->_callbacks->SetResult(self->_engine, result);
        if (status != errAuthorizationSuccess) {
            os_log_error(self->_log, "SetResult failed: %d", (int)status);
        }

        // Answered. Nothing else may fire, and the cycles die here rather than
        // waiting for -invalidate.
        [self teardownOnQueue];
    });
}

#pragma mark Deadline

- (void)armDeadlineAfter:(NSTimeInterval)seconds {
    dispatch_async(_q, ^{
        if (self->_delivered || self->_engine == NULL) { return; }

        dispatch_source_t timer =
            dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, self->_q);
        if (timer == nil) {
            // Fail open, loudly. An unarmed deadline is the one state this
            // class must never sit in: it is a lock screen with nothing
            // scheduled to end the wait. Answer now instead.
            os_log_error(self->_log,
                         "could not create deadline timer; yielding immediately");
            dispatch_async(self->_q, ^{
                [self yieldWithVerdict:JARVISGrantVerdictUnavailable];
            });
            return;
        }

        dispatch_source_set_timer(timer,
                                  dispatch_time(DISPATCH_TIME_NOW,
                                                (int64_t)(seconds * NSEC_PER_SEC)),
                                  DISPATCH_TIME_FOREVER,   // one-shot
                                  (uint64_t)(NSEC_PER_MSEC * 10));
        dispatch_source_set_event_handler(timer, ^{
            // Cancel first: a one-shot must not be able to fire twice, and the
            // cycle through _deadline is broken by teardown inside -deliver.
            dispatch_source_cancel(timer);
            [self yieldWithVerdict:JARVISGrantVerdictTimedOut];
        });

        self->_deadline = timer;
        dispatch_resume(timer);
    });
}

#pragma mark Teardown

- (void)adoptConnection:(NSXPCConnection *)connection {
    dispatch_async(_q, ^{
        if (self->_delivered || self->_engine == NULL) {
            // Already finished, or finishing. Do not leave a live connection
            // behind whose handlers could re-enter after we are done.
            [connection invalidate];
            return;
        }
        self->_connection = connection;
    });
}

/// Queue-confined. Idempotent. Breaks both intentional retain cycles.
- (void)teardownOnQueue {
    if (_deadline != nil) {
        dispatch_source_cancel(_deadline);
        _deadline = nil;                       // cycle: self -> source -> block -> self
    }
    if (_connection != nil) {
        // Clearing the handlers BEFORE invalidating is load-bearing. The old
        // code invalidated the connection on its own success path while its
        // invalidationHandler was still installed, so a normal, deliberate
        // teardown re-entered the yield path as though the broker had died.
        _connection.interruptionHandler = nil;
        _connection.invalidationHandler = nil; // cycle: connection -> block -> self
        [_connection invalidate];
        _connection = nil;
    }
}

- (void)invalidate {
    // dispatch_sync is the barrier that makes the engine handle safe: when this
    // returns, any SetResult already in flight has completed and no future one
    // can begin. Only then may the caller free the mechanism.
    //
    // Deadlock-free because -invalidate is only ever called from
    // MechanismDestroy, on SecurityAgent's thread, never from _q itself.
    dispatch_sync(_q, ^{
        self->_engine = NULL;
        self->_callbacks = NULL;
        [self teardownOnQueue];
    });
}

@end

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
 * Returns immediately. Every terminal path -- reply, timeout, interruption,
 * invalidation, proxy error, missing config -- lands on the delivery object,
 * which answers exactly once.
 */
static void JARVISAskBroker(JARVISDelivery *delivery,
                            JARVISUnlockConfig *config,
                            os_log_t log) {
    // Arm the deadline FIRST. If anything below throws, misbehaves, or simply
    // never calls back, this still fires and the user still gets their prompt.
    [delivery armDeadlineAfter:config.grantTimeoutSeconds];

    NSString *service = config.brokerMachServiceName;
    NSString *requirement = config.brokerCodeRequirement;
    if (service == nil || requirement == nil) {
        // Config already logged the specifics at load. Yield now rather than
        // making the user wait out a timeout for a decision already made.
        [delivery yieldWithVerdict:JARVISGrantVerdictUnavailable];
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
        [connection invalidate];
        [delivery yieldWithVerdict:JARVISGrantVerdictRejected];
        return;
    }

    // Handed over before resume, so teardown owns it from here on and no
    // handler can outlive the arbiter's answer.
    [delivery adoptConnection:connection];

    // Both handlers yield. An interrupted or invalidated connection is
    // indistinguishable from a dead broker from here, and both mean "prompt".
    connection.interruptionHandler = ^{
        [delivery yieldWithVerdict:JARVISGrantVerdictUnavailable];
    };
    connection.invalidationHandler = ^{
        [delivery yieldWithVerdict:JARVISGrantVerdictUnavailable];
    };

    [connection resume];

    id<JARVISGrantConsumer> broker =
        [connection remoteObjectProxyWithErrorHandler:^(NSError *error) {
            os_log_error(log, "broker proxy error: %{public}@", error.localizedDescription);
            [delivery yieldWithVerdict:JARVISGrantVerdictUnavailable];
        }];

    [broker consumeGrantWithSchemaVersion:config.schemaVersion
                                    reply:^(JARVISGrantVerdict verdict,
                                            NSString *correlationId) {
        if (correlationId.length > 0) {
            os_log_debug(log, "broker correlation=%{public}@", correlationId);
        }
        if (verdict == JARVISGrantVerdictGranted) {
            [delivery grantWithVerdict:verdict];
        } else {
            [delivery yieldWithVerdict:verdict];
        }
        // No explicit invalidate here: -deliver tears the connection down with
        // its handlers already cleared, so the normal path cannot masquerade as
        // a broker failure.
    }];
}

#pragma mark - AuthorizationPlugin interface

static OSStatus JARVISMechanismCreate(AuthorizationPluginRef inPlugin,
                                      AuthorizationEngineRef inEngine,
                                      AuthorizationMechanismId mechanismId,
                                      AuthorizationMechanismRef *outMechanism) {
    if (inPlugin == NULL || outMechanism == NULL) { return errAuthorizationInternal; }

    JARVISPlugin *plugin = (JARVISPlugin *)inPlugin;
    if (!JARVISPluginUsable(plugin)) { return errAuthorizationInternal; }

    JARVISMechanism *mech = calloc(1, sizeof(JARVISMechanism));
    if (mech == NULL) { return errAuthorizationInternal; }

    mech->plugin = plugin;
    mech->engine = inEngine;
    mech->delivery = NULL;

    os_log_debug(plugin->log, "mechanism created: %{public}s",
                 mechanismId ? mechanismId : "(unnamed)");

    *outMechanism = (AuthorizationMechanismRef)mech;
    return errAuthorizationSuccess;
}

/// Retire the previous invocation's arbiter, if any. Barriered, so nothing from
/// the old attempt can answer the new one.
static void JARVISRetireDelivery(JARVISMechanism *mech) {
    if (mech == NULL || mech->delivery == NULL) { return; }
    JARVISDelivery *previous = (JARVISDelivery *)CFBridgingRelease(mech->delivery);
    mech->delivery = NULL;
    [previous invalidate];
}

static OSStatus JARVISMechanismInvoke(AuthorizationMechanismRef inMechanism) {
    JARVISMechanism *mech = (JARVISMechanism *)inMechanism;
    if (!JARVISMechUsable(mech)) { return errAuthorizationInternal; }

    // The same mechanism object is reused across retries. A fresh arbiter per
    // invocation is what makes that safe -- the old design cleared a flag,
    // which let a previous attempt's in-flight callback answer THIS attempt.
    JARVISRetireDelivery(mech);

    JARVISDelivery *delivery =
        [[JARVISDelivery alloc] initWithCallbacks:mech->plugin->callbacks
                                          engine:mech->engine
                                             log:JARVISLogOf(mech)];
    if (delivery == nil) {
        // Cannot arbitrate, so cannot promise an answer. Yield synchronously
        // through the raw callbacks -- mech is provably alive on this frame --
        // rather than returning an error that stalls the chain.
        os_log_error(JARVISLogOf(mech), "delivery alloc failed; yielding directly");
        mech->plugin->callbacks->SetResult(mech->engine, kAuthorizationResultAllow);
        return errAuthorizationSuccess;
    }
    mech->delivery = (void *)CFBridgingRetain(delivery);

    @autoreleasepool {
        @try {
            JARVISUnlockConfig *config = [JARVISUnlockConfig sharedConfig];

            if (!config.mechanismEnabled) {
                [delivery yieldWithVerdict:JARVISGrantVerdictUnavailable];
                return errAuthorizationSuccess;
            }

            // Checked before anything else, and before any IPC: the panic key
            // must work when the broker is the thing that is broken. Note that
            // no deadline is armed on this path, so it cannot interact with the
            // timer at all -- which is why holding the key is a clean bypass
            // even when everything else here is misbehaving.
            if (JARVISPanicHeld(config, JARVISLogOf(mech))) {
                [delivery yieldWithVerdict:JARVISGrantVerdictPanicBypass];
                return errAuthorizationSuccess;
            }

            JARVISAskBroker(delivery, config, JARVISLogOf(mech));
        } @catch (NSException *e) {
            // An exception escaping into SecurityAgent is how a lock screen
            // dies. Nothing gets past this frame.
            os_log_error(JARVISLogOf(mech),
                         "unhandled exception in Invoke (%{public}@); yielding",
                         e.name);
            [delivery yieldWithVerdict:JARVISGrantVerdictUnavailable];
        }
    }

    // Returns immediately in every path above. SetResult happens later, from
    // whichever racer wins.
    return errAuthorizationSuccess;
}

static OSStatus JARVISMechanismDeactivate(AuthorizationMechanismRef inMechanism) {
    JARVISMechanism *mech = (JARVISMechanism *)inMechanism;
    if (!JARVISMechUsable(mech) || mech->plugin->callbacks->DidDeactivate == NULL) {
        return errAuthorizationInternal;
    }
    return mech->plugin->callbacks->DidDeactivate(mech->engine);
}

static OSStatus JARVISMechanismDestroy(AuthorizationMechanismRef inMechanism) {
    JARVISMechanism *mech = (JARVISMechanism *)inMechanism;
    if (mech == NULL) { return errAuthorizationSuccess; }

    // Order is the entire fix. -invalidate is a barrier: it waits out any
    // SetResult in flight, drops the engine so no later one can run, and breaks
    // the retain cycles. Only after it returns is it safe to free -- and only
    // then does the arbiter's own memory become unreachable, which the ARC
    // refcount, not this free(), is what governs.
    JARVISRetireDelivery(mech);

    mech->plugin = NULL;
    mech->engine = NULL;
    free(mech);
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

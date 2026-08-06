/**
 * JARVISUnlockBroker.m
 * The grant broker: a root LaunchDaemon holding at most one single-use,
 * TTL-bounded unlock grant.
 *
 * WHY TWO MACH SERVICES AND NOT ONE
 * ---------------------------------
 * `-[NSXPCListener setConnectionCodeSigningRequirement:]` applies one
 * requirement to every connection a listener accepts. Depositing a grant and
 * consuming one are privileges of completely different weight -- minting an
 * unlock versus spending one -- so putting both behind a single requirement
 * would mean anything allowed to ask "is there a grant?" is also allowed to say
 * "there is now."
 *
 * So there are two listeners on two Mach services, each with its own
 * requirement, each vending exactly one interface. The plugin's signature
 * satisfies the consume service and nothing else. The JARVIS backend's
 * signature satisfies the deposit service and nothing else. The separation is
 * enforced by the kernel at the service boundary rather than by a branch in our
 * code that could be wrong.
 *
 * This matters most in the case that is otherwise hardest to defend: the plugin
 * runs inside SecurityAgent and is the more exposed of the two peers. If it is
 * ever compromised, it can spend a grant that JARVIS already minted -- an
 * unlock the user asked for anyway, moments earlier. It cannot mint one.
 *
 * WHY CONFIG IS ENVIRONMENT-DRIVEN HERE AND INFO.PLIST IN THE PLUGIN
 * -----------------------------------------------------------------
 * Not an inconsistency -- each side uses the native channel for its own form.
 * The plugin is a bundle loaded into a process the operator never launches, so
 * its Info.plist is the only configuration that is both readable in production
 * and covered by the code signature. The broker is a LaunchDaemon the operator
 * installs, whose plist carries EnvironmentVariables that launchd applies at
 * exec: readable, root-owned, and consistent with the env-var convention the
 * rest of JARVIS already uses.
 *
 * Neither side has a default for a service name or a code requirement. A broker
 * that invented a requirement when its configuration was missing would be a
 * broker that accepts anyone.
 */

#import <Foundation/Foundation.h>
#import <os/log.h>

#import "JARVISGrantProtocol.h"

#pragma mark - Configuration

static NSString *const kEnvConsumeService     = @"JARVIS_BROKER_CONSUME_SERVICE";
static NSString *const kEnvDepositService     = @"JARVIS_BROKER_DEPOSIT_SERVICE";
static NSString *const kEnvConsumerRequirement = @"JARVIS_BROKER_CONSUMER_REQUIREMENT";
static NSString *const kEnvDepositorRequirement = @"JARVIS_BROKER_DEPOSITOR_REQUIREMENT";
static NSString *const kEnvMaxGrantTTL        = @"JARVIS_BROKER_MAX_GRANT_TTL_S";
static NSString *const kEnvSchemaVersion      = @"JARVIS_BROKER_SCHEMA_VERSION";

static const NSTimeInterval kDefaultMaxGrantTTL = 30.0;
static const NSTimeInterval kMinMaxGrantTTL     = 1.0;
static const NSTimeInterval kMaxMaxGrantTTL     = 300.0;

@interface JARVISBrokerConfig : NSObject
@property (nonatomic, readonly, nullable) NSString *consumeServiceName;
@property (nonatomic, readonly, nullable) NSString *depositServiceName;
@property (nonatomic, readonly, nullable) NSString *consumerRequirement;
@property (nonatomic, readonly, nullable) NSString *depositorRequirement;
@property (nonatomic, readonly) NSTimeInterval maxGrantTTL;
@property (nonatomic, readonly) NSString *schemaVersion;
@property (nonatomic, readonly) BOOL usable;
@end

@implementation JARVISBrokerConfig

+ (instancetype)fromEnvironment:(NSDictionary<NSString *, NSString *> *)env log:(os_log_t)log {
    JARVISBrokerConfig *c = [[JARVISBrokerConfig alloc] init];
    [c loadFrom:env log:log];
    return c;
}

static NSString *_Nullable JARVISNonEmpty(NSDictionary *env, NSString *key) {
    id v = env[key];
    if (![v isKindOfClass:[NSString class]]) { return nil; }
    NSString *s = [(NSString *)v stringByTrimmingCharactersInSet:
                   [NSCharacterSet whitespaceAndNewlineCharacterSet]];
    return s.length > 0 ? s : nil;
}

- (void)loadFrom:(NSDictionary<NSString *, NSString *> *)env log:(os_log_t)log {
    _consumeServiceName   = JARVISNonEmpty(env, kEnvConsumeService);
    _depositServiceName   = JARVISNonEmpty(env, kEnvDepositService);
    _consumerRequirement  = JARVISNonEmpty(env, kEnvConsumerRequirement);
    _depositorRequirement = JARVISNonEmpty(env, kEnvDepositorRequirement);

    NSString *rawTTL = JARVISNonEmpty(env, kEnvMaxGrantTTL);
    NSTimeInterval ttl = rawTTL ? rawTTL.doubleValue : kDefaultMaxGrantTTL;
    if (ttl <= 0) { ttl = kDefaultMaxGrantTTL; }
    NSTimeInterval clamped = MIN(MAX(ttl, kMinMaxGrantTTL), kMaxMaxGrantTTL);
    if (fabs(clamped - ttl) > DBL_EPSILON) {
        os_log_error(log, "config: %{public}@=%.1fs clamped to %.1fs",
                     kEnvMaxGrantTTL, ttl, clamped);
    }
    _maxGrantTTL = clamped;

    _schemaVersion = JARVISNonEmpty(env, kEnvSchemaVersion) ?: JARVISGrantSchemaVersion;

    // Fail closed, and say exactly which key is missing. A daemon that starts
    // with half a configuration is a daemon whose security properties nobody
    // can state.
    NSMutableArray<NSString *> *missing = [NSMutableArray array];
    if (!_consumeServiceName)   { [missing addObject:kEnvConsumeService]; }
    if (!_depositServiceName)   { [missing addObject:kEnvDepositService]; }
    if (!_consumerRequirement)  { [missing addObject:kEnvConsumerRequirement]; }
    if (!_depositorRequirement) { [missing addObject:kEnvDepositorRequirement]; }

    if (missing.count > 0) {
        os_log_error(log, "refusing to start; missing required config: %{public}@",
                     [missing componentsJoinedByString:@", "]);
        _usable = NO;
    } else if ([_consumeServiceName isEqualToString:_depositServiceName]) {
        // The entire separation-of-privilege argument rests on these being
        // distinct. Collapsing them by misconfiguration would silently put
        // minting and spending behind one requirement.
        os_log_error(log,
                     "refusing to start; %{public}@ and %{public}@ are identical (%{public}@). "
                     "Deposit and consume must not share a service.",
                     kEnvConsumeService, kEnvDepositService, _consumeServiceName);
        _usable = NO;
    } else {
        _usable = YES;
    }
}

@end

#pragma mark - Grant store

/**
 * At most one outstanding grant, mutated only on a serial queue.
 *
 * A serial queue rather than a mutex, so nothing ever blocks: consume and
 * deposit are both async, and the queue is what makes "check TTL then clear"
 * atomic. Two simultaneous unlock attempts cannot both be satisfied by one
 * grant, and a replay finds nothing.
 *
 * Capacity is one by construction. A queue of grants would let a burst of voice
 * commands stockpile unlocks that outlive the moment the user asked for them --
 * "unlock my screen" said three times is one intent, not three.
 */
@interface JARVISGrantStore : NSObject
- (instancetype)initWithLog:(os_log_t)log;
- (void)depositWithTTL:(NSTimeInterval)ttl
                reason:(NSString *)reason
                 reply:(void (^)(BOOL accepted, NSString *_Nullable grantId))reply;
- (void)consumeWithReply:(void (^)(JARVISGrantVerdict verdict,
                                   NSString *_Nullable correlationId))reply;
@end

@implementation JARVISGrantStore {
    dispatch_queue_t _queue;
    os_log_t _log;
    NSString *_grantId;
    NSDate *_expiresAt;
    NSString *_reason;
}

- (instancetype)initWithLog:(os_log_t)log {
    self = [super init];
    if (self) {
        _log = log;
        _queue = dispatch_queue_create("com.jarvis.unlockbroker.grants",
                                       DISPATCH_QUEUE_SERIAL);
    }
    return self;
}

- (void)depositWithTTL:(NSTimeInterval)ttl
                reason:(NSString *)reason
                 reply:(void (^)(BOOL, NSString *_Nullable))reply {
    dispatch_async(_queue, ^{
        if (self->_grantId != nil) {
            // Replacing rather than refusing: the newest intent is the live one,
            // and the old grant is discarded rather than queued. Logged because
            // a steady stream means something upstream is depositing on repeat.
            os_log(self->_log, "deposit replaces outstanding grant %{public}@",
                   self->_grantId);
        }

        NSString *grantId = [[NSUUID UUID] UUIDString];
        self->_grantId = grantId;
        self->_expiresAt = [NSDate dateWithTimeIntervalSinceNow:ttl];
        self->_reason = [reason copy];

        os_log(self->_log, "grant %{public}@ deposited ttl=%.1fs reason=%{public}@",
               grantId, ttl, reason);
        reply(YES, grantId);
    });
}

- (void)consumeWithReply:(void (^)(JARVISGrantVerdict, NSString *_Nullable))reply {
    dispatch_async(_queue, ^{
        if (self->_grantId == nil) {
            // The resting state on every manual unlock. Debug level: logging it
            // at default would bury real events under normal password entry.
            os_log_debug(self->_log, "consume: no grant outstanding");
            reply(JARVISGrantVerdictNoGrant, nil);
            return;
        }

        NSString *grantId = self->_grantId;
        BOOL expired = ([self->_expiresAt timeIntervalSinceNow] <= 0);

        // Cleared on BOTH paths, before the reply. Single-use means single-use
        // even when the single use failed: leaving an expired grant in place
        // would let a later consumer race the clock against a grant the user
        // has forgotten they authorised.
        self->_grantId = nil;
        self->_expiresAt = nil;
        NSString *reason = self->_reason;
        self->_reason = nil;

        if (expired) {
            os_log(self->_log, "grant %{public}@ EXPIRED unconsumed (reason=%{public}@)",
                   grantId, reason);
            reply(JARVISGrantVerdictExpired, grantId);
        } else {
            os_log(self->_log, "grant %{public}@ CONSUMED (reason=%{public}@)",
                   grantId, reason);
            reply(JARVISGrantVerdictGranted, grantId);
        }
    });
}

@end

#pragma mark - Listener delegates

/**
 * One delegate per service. Each vends exactly one interface, and the listener's
 * code signing requirement -- set at construction, enforced by the OS -- decides
 * who is allowed to reach it at all.
 */
@interface JARVISListenerDelegate : NSObject <NSXPCListenerDelegate>
@property (nonatomic, strong) NSXPCInterface *vendedInterface;
@property (nonatomic, weak) id exportedObject;
@property (nonatomic, copy) NSString *label;
@property (nonatomic) os_log_t log;
@end

@implementation JARVISListenerDelegate

- (BOOL)listener:(NSXPCListener *)listener
shouldAcceptNewConnection:(NSXPCConnection *)newConnection {
    // By the time this runs the OS has already enforced the listener's code
    // signing requirement, so a peer reaching here has proven its identity.
    // What remains is to hand it the one interface this service exists to vend.
    newConnection.exportedInterface = self.vendedInterface;
    newConnection.exportedObject = self.exportedObject;

    os_log(self.log, "%{public}@: accepted connection from pid %d",
           self.label, newConnection.processIdentifier);

    [newConnection resume];
    return YES;
}

@end

#pragma mark - Broker

@interface JARVISUnlockBroker : NSObject <JARVISGrantConsumer, JARVISGrantDepositor>
@end

@implementation JARVISUnlockBroker {
    JARVISBrokerConfig *_config;
    JARVISGrantStore *_store;
    os_log_t _log;
    NSXPCListener *_consumeListener;
    NSXPCListener *_depositListener;
    JARVISListenerDelegate *_consumeDelegate;
    JARVISListenerDelegate *_depositDelegate;
}

- (instancetype)initWithConfig:(JARVISBrokerConfig *)config log:(os_log_t)log {
    self = [super init];
    if (self) {
        _config = config;
        _log = log;
        _store = [[JARVISGrantStore alloc] initWithLog:log];
    }
    return self;
}

- (BOOL)start {
    if (!_config.usable) { return NO; }

    if (@available(macOS 13.0, *)) {
        // Fine. Requirements are enforceable.
    } else {
        // Without enforceable peer requirements this daemon would accept any
        // caller on a privileged Mach service whose sole purpose is unlocking
        // screens. Refusing to start is the only defensible behaviour.
        os_log_error(_log,
                     "refusing to start: peer code signing requirements are "
                     "unenforceable before macOS 13");
        return NO;
    }

    _consumeDelegate = [[JARVISListenerDelegate alloc] init];
    _consumeDelegate.vendedInterface =
        [NSXPCInterface interfaceWithProtocol:@protocol(JARVISGrantConsumer)];
    _consumeDelegate.exportedObject = self;
    _consumeDelegate.label = @"consume";
    _consumeDelegate.log = _log;

    _depositDelegate = [[JARVISListenerDelegate alloc] init];
    _depositDelegate.vendedInterface =
        [NSXPCInterface interfaceWithProtocol:@protocol(JARVISGrantDepositor)];
    _depositDelegate.exportedObject = self;
    _depositDelegate.label = @"deposit";
    _depositDelegate.log = _log;

    _consumeListener = [[NSXPCListener alloc]
                        initWithMachServiceName:_config.consumeServiceName];
    _consumeListener.delegate = _consumeDelegate;

    _depositListener = [[NSXPCListener alloc]
                        initWithMachServiceName:_config.depositServiceName];
    _depositListener.delegate = _depositDelegate;

    if (@available(macOS 13.0, *)) {
        [_consumeListener setConnectionCodeSigningRequirement:_config.consumerRequirement];
        [_depositListener setConnectionCodeSigningRequirement:_config.depositorRequirement];
    }

    [_consumeListener resume];
    [_depositListener resume];

    os_log(_log,
           "broker started: consume=%{public}@ deposit=%{public}@ maxTTL=%.1fs schema=%{public}@",
           _config.consumeServiceName, _config.depositServiceName,
           _config.maxGrantTTL, _config.schemaVersion);
    return YES;
}

#pragma mark JARVISGrantConsumer

- (void)consumeGrantWithSchemaVersion:(NSString *)schemaVersion
                                reply:(void (^)(JARVISGrantVerdict, NSString *_Nullable))reply {
    if (![schemaVersion isEqualToString:_config.schemaVersion]) {
        // Refuse rather than interpret. A plugin and a broker that disagree
        // about the payload shape must fail visibly, not guess.
        os_log_error(_log, "consume: schema mismatch (peer=%{public}@ ours=%{public}@)",
                     schemaVersion, _config.schemaVersion);
        reply(JARVISGrantVerdictRejected, nil);
        return;
    }
    [_store consumeWithReply:reply];
}

#pragma mark JARVISGrantDepositor

- (void)depositGrantWithSchemaVersion:(NSString *)schemaVersion
                           ttlSeconds:(NSTimeInterval)ttlSeconds
                               reason:(NSString *)reason
                                reply:(void (^)(BOOL, NSString *_Nullable))reply {
    if (![schemaVersion isEqualToString:_config.schemaVersion]) {
        os_log_error(_log, "deposit: schema mismatch (peer=%{public}@ ours=%{public}@)",
                     schemaVersion, _config.schemaVersion);
        reply(NO, nil);
        return;
    }

    // The caller asks; the broker decides. A depositor cannot mint a grant that
    // outlives the broker's ceiling by requesting a longer one, and a
    // nonsensical TTL becomes the ceiling rather than an error the caller might
    // ignore.
    NSTimeInterval ttl = ttlSeconds;
    if (!(ttl > 0) || ttl > _config.maxGrantTTL) {
        os_log(_log, "deposit: requested ttl %.1fs -> %.1fs (broker ceiling)",
               ttlSeconds, _config.maxGrantTTL);
        ttl = _config.maxGrantTTL;
    }

    NSString *safeReason = [reason isKindOfClass:[NSString class]] && reason.length > 0
        ? reason : @"(unspecified)";

    [_store depositWithTTL:ttl reason:safeReason reply:reply];
}

@end

#pragma mark - Entry point

int main(int argc, const char *argv[]) {
    (void)argc; (void)argv;
    @autoreleasepool {
        os_log_t log = os_log_create("com.jarvis.unlockbroker", "broker");

        JARVISBrokerConfig *config =
            [JARVISBrokerConfig fromEnvironment:NSProcessInfo.processInfo.environment log:log];

        JARVISUnlockBroker *broker =
            [[JARVISUnlockBroker alloc] initWithConfig:config log:log];

        if (![broker start]) {
            // Exit non-zero so launchd's KeepAlive/SuccessfulExit policy shows a
            // refusal rather than a process that is up and silently accepting
            // nothing.
            os_log_error(log, "broker did not start");

            // AND to stderr, which launchd captures to StandardErrorPath.
            // os_log alone puts the only explanation of a non-starting daemon
            // in the unified log, where it is found only by someone who already
            // suspects this daemon -- and the symptom (unlock silently never
            // works) points at the plugin, not here. Duplicated deliberately:
            // the diagnosis has to be waiting in the file the operator reaches
            // for first.
            fprintf(stderr,
                    "[jarvis-unlockbroker] refusing to start; see `log show "
                    "--predicate 'subsystem == \"com.jarvis.unlockbroker\"' --last 5m` "
                    "for the specific missing or invalid configuration key.\n");
            fflush(stderr);
            return EXIT_FAILURE;
        }

        [[NSRunLoop currentRunLoop] run];
    }
    return EXIT_SUCCESS;
}

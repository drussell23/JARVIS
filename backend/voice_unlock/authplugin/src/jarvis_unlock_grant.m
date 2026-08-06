/**
 * jarvis_unlock_grant.m
 * The signed deposit helper. JARVIS execs this; this talks to the broker.
 *
 * WHY THIS BINARY EXISTS AT ALL
 * -----------------------------
 * The obvious design is for the Python backend to open the XPC connection
 * itself via PyObjC. That design cannot be secured, and the reason is worth
 * stating precisely because it looks like an extra hop for nothing.
 *
 * The broker authorises its callers by code signing requirement. For that to
 * mean anything, the caller must have a narrow, stable identity. A Python
 * interpreter has neither: `python3.11` is a shared binary whose job is to run
 * whatever script it is handed. A requirement naming it would authorise every
 * script on the machine to mint screen unlocks -- including one an attacker
 * dropped in a directory JARVIS imports from. The requirement would be
 * syntactically present and semantically empty.
 *
 * So the deposit privilege belongs to a binary that does exactly one thing and
 * can be signed as itself. This helper is ~100 lines with no configuration
 * surface, no plugin loading, and no way to be repurposed: its designated
 * requirement is a real statement about what is asking.
 *
 * That is also why it takes the reason as an argument and nothing else. It has
 * no ability to specify a TTL beyond asking, no ability to name a service, and
 * no ability to choose a requirement. Everything that could weaken the grant is
 * decided by the broker or by the LaunchDaemon plist, not by whoever execs this.
 *
 * EXIT CODES ARE THE INTERFACE
 * ----------------------------
 * stdout is for humans. The exit code is what the Python bridge reads, and each
 * value is a distinct operational condition -- "broker is down" and "broker
 * refused us" must never collapse into a single failure, because the first is a
 * daemon to restart and the second is an install to investigate.
 */

#import <Foundation/Foundation.h>
#import "JARVISGrantProtocol.h"

typedef NS_ENUM(int, JARVISGrantExit) {
    JARVISGrantExitDeposited   = 0,  ///< Grant accepted.
    JARVISGrantExitUsage       = 64, ///< Bad arguments (EX_USAGE).
    JARVISGrantExitConfig      = 78, ///< Missing configuration (EX_CONFIG).
    JARVISGrantExitUnavailable = 69, ///< Broker unreachable (EX_UNAVAILABLE).
    JARVISGrantExitRejected    = 77, ///< Broker refused us (EX_NOPERM).
    JARVISGrantExitTimeout     = 75, ///< No answer in time (EX_TEMPFAIL).
};

static NSString *const kEnvDepositService = @"JARVIS_BROKER_DEPOSIT_SERVICE";
static NSString *const kEnvBrokerRequirement = @"JARVIS_BROKER_CODE_REQUIREMENT";
static NSString *const kEnvRequestTTL = @"JARVIS_GRANT_REQUEST_TTL_S";
static NSString *const kEnvDepositTimeout = @"JARVIS_GRANT_DEPOSIT_TIMEOUT_S";

static const NSTimeInterval kDefaultRequestTTL = 20.0;
static const NSTimeInterval kDefaultDepositTimeout = 3.0;
static const NSTimeInterval kMinDepositTimeout = 0.25;
static const NSTimeInterval kMaxDepositTimeout = 15.0;

static NSString *_Nullable EnvString(NSString *key) {
    NSString *v = NSProcessInfo.processInfo.environment[key];
    v = [v stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
    return v.length > 0 ? v : nil;
}

static NSTimeInterval EnvInterval(NSString *key, NSTimeInterval fallback,
                                  NSTimeInterval lo, NSTimeInterval hi) {
    NSString *raw = EnvString(key);
    if (raw == nil) { return fallback; }
    NSTimeInterval v = raw.doubleValue;
    if (!(v > 0)) { return fallback; }
    return MIN(MAX(v, lo), hi);
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 2) {
            fprintf(stderr,
                    "usage: jarvis-unlock-grant <reason>\n"
                    "  Deposits a single-use screen-unlock grant with the broker.\n"
                    "  The reason is recorded in the broker's audit log and must\n"
                    "  never contain credentials.\n");
            return JARVISGrantExitUsage;
        }

        NSString *reason = [NSString stringWithUTF8String:argv[1]] ?: @"(unspecified)";

        NSString *service = EnvString(kEnvDepositService);
        NSString *requirement = EnvString(kEnvBrokerRequirement);
        if (service == nil || requirement == nil) {
            // Same principle as both other components: no guessed defaults. A
            // helper that invented a service name could hand "mint me an
            // unlock" to whatever process won that name.
            fprintf(stderr, "[jarvis-unlock-grant] missing %s and/or %s\n",
                    kEnvDepositService.UTF8String, kEnvBrokerRequirement.UTF8String);
            return JARVISGrantExitConfig;
        }

        NSTimeInterval ttl = EnvInterval(kEnvRequestTTL, kDefaultRequestTTL, 1.0, 300.0);
        NSTimeInterval timeout = EnvInterval(kEnvDepositTimeout, kDefaultDepositTimeout,
                                             kMinDepositTimeout, kMaxDepositTimeout);

        NSXPCConnection *connection =
            [[NSXPCConnection alloc] initWithMachServiceName:service
                                                     options:NSXPCConnectionPrivileged];
        connection.remoteObjectInterface =
            [NSXPCInterface interfaceWithProtocol:@protocol(JARVISGrantDepositor)];

        // We validate the broker just as it validates us. Without this we would
        // deposit an unlock grant into whatever answered the name.
        if (@available(macOS 13.0, *)) {
            [connection setCodeSigningRequirement:requirement];
        } else {
            fprintf(stderr, "[jarvis-unlock-grant] peer requirements unenforceable "
                            "before macOS 13; refusing\n");
            return JARVISGrantExitRejected;
        }

        // Distinguishes "no answer" from "refused". Both are failures; only one
        // means the daemon needs restarting.
        __block JARVISGrantExit outcome = JARVISGrantExitTimeout;
        __block NSString *grantId = nil;
        dispatch_semaphore_t done = dispatch_semaphore_create(0);

        connection.invalidationHandler = ^{
            // Reached both when the broker is absent and when it rejects our
            // signature; NSXPCConnection does not distinguish them to the
            // client. Reported as Rejected rather than Unavailable would be a
            // guess, so it stays Unavailable and the broker's own log is the
            // authority on which it was.
            outcome = JARVISGrantExitUnavailable;
            dispatch_semaphore_signal(done);
        };
        connection.interruptionHandler = ^{
            outcome = JARVISGrantExitUnavailable;
            dispatch_semaphore_signal(done);
        };

        [connection resume];

        id<JARVISGrantDepositor> broker =
            [connection remoteObjectProxyWithErrorHandler:^(NSError *error) {
                fprintf(stderr, "[jarvis-unlock-grant] %s\n",
                        error.localizedDescription.UTF8String);
                outcome = JARVISGrantExitUnavailable;
                dispatch_semaphore_signal(done);
            }];

        [broker depositGrantWithSchemaVersion:JARVISGrantSchemaVersion
                                   ttlSeconds:ttl
                                       reason:reason
                                        reply:^(BOOL accepted, NSString *gid) {
            outcome = accepted ? JARVISGrantExitDeposited : JARVISGrantExitRejected;
            grantId = gid;
            dispatch_semaphore_signal(done);
        }];

        // Blocking is correct HERE and wrong in the mechanism. This is a
        // short-lived CLI whose only job is this one call; nothing is waiting on
        // its thread. The mechanism runs inside SecurityAgent on the path to a
        // locked machine, where the same pattern would wedge a lock screen.
        if (dispatch_semaphore_wait(done,
                dispatch_time(DISPATCH_TIME_NOW, (int64_t)(timeout * NSEC_PER_SEC))) != 0) {
            fprintf(stderr, "[jarvis-unlock-grant] no answer in %.1fs\n", timeout);
            outcome = JARVISGrantExitTimeout;
        }

        [connection invalidate];

        if (outcome == JARVISGrantExitDeposited) {
            fprintf(stdout, "%s\n", (grantId ?: @"(no id)").UTF8String);
        }
        return outcome;
    }
}

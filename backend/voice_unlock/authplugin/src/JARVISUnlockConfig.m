/**
 * JARVISUnlockConfig.m
 */

#import "JARVISUnlockConfig.h"
#import "JARVISGrantProtocol.h"
#import <Carbon/Carbon.h>
#import <os/log.h>

#pragma mark - Defaults and bounds

// Named constants rather than literals at the point of use: the bound and the
// value it bounds are defined adjacently and cannot drift apart.
static const NSTimeInterval kDefaultGrantTimeoutSeconds = 0.500;
static const NSTimeInterval kMinGrantTimeoutSeconds     = 0.050;
static const NSTimeInterval kMaxGrantTimeoutSeconds     = 2.000;

static NSString *const kKeyTimeoutMs        = @"JARVISGrantTimeoutMilliseconds";
static NSString *const kKeyMachService      = @"JARVISBrokerMachServiceName";
static NSString *const kKeyCodeRequirement  = @"JARVISBrokerCodeRequirement";
static NSString *const kKeyPanicKeyCode     = @"JARVISPanicKeyCode";
static NSString *const kKeyPanicEnabled     = @"JARVISPanicChokeEnabled";
static NSString *const kKeyMechanismEnabled = @"JARVISGrantMechanismEnabled";
static NSString *const kKeySchemaVersion    = @"JARVISGrantSchemaVersion";

@interface JARVISUnlockConfig ()
@property (nonatomic, readwrite) NSTimeInterval grantTimeoutSeconds;
@property (nonatomic, readwrite, nullable) NSString *brokerMachServiceName;
@property (nonatomic, readwrite, nullable) NSString *brokerCodeRequirement;
@property (nonatomic, readwrite) CGKeyCode panicKeyCode;
@property (nonatomic, readwrite) BOOL panicChokeEnabled;
@property (nonatomic, readwrite) BOOL mechanismEnabled;
@property (nonatomic, readwrite) NSString *schemaVersion;
@end

@implementation JARVISUnlockConfig

+ (instancetype)sharedConfig {
    static JARVISUnlockConfig *shared = nil;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        shared = [[JARVISUnlockConfig alloc] initFromBundle];
    });
    return shared;
}

/// Reads a BOOL that may legitimately be absent. Absent -> fallback; present but
/// not a boolean -> fallback, loudly. A misspelled type must not read as NO.
static BOOL JARVISBoolValue(id raw, BOOL fallback, NSString *key, os_log_t log) {
    if (raw == nil) { return fallback; }
    if ([raw isKindOfClass:[NSNumber class]]) { return [raw boolValue]; }
    os_log_error(log, "config: %{public}@ is not a boolean; using default %d",
                 key, fallback);
    return fallback;
}

- (instancetype)initFromBundle {
    self = [super init];
    if (!self) { return nil; }

    os_log_t log = os_log_create("com.jarvis.unlockplugin", "config");

    // The bundle containing THIS class, not mainBundle -- mainBundle inside
    // SecurityAgent is SecurityAgent, whose Info.plist is Apple's.
    NSBundle *bundle = [NSBundle bundleForClass:[self class]];
    NSDictionary *info = bundle.infoDictionary ?: @{};

    // --- timeout: clamp rather than reject, and say so when clamping ---
    id rawTimeout = info[kKeyTimeoutMs];
    NSTimeInterval timeout = kDefaultGrantTimeoutSeconds;
    if ([rawTimeout isKindOfClass:[NSNumber class]]) {
        NSTimeInterval requested = [rawTimeout doubleValue] / 1000.0;
        timeout = MIN(MAX(requested, kMinGrantTimeoutSeconds), kMaxGrantTimeoutSeconds);
        if (fabs(timeout - requested) > DBL_EPSILON) {
            os_log_error(log,
                         "config: %{public}@=%.0fms clamped to %.0fms; "
                         "a value outside [%.0f, %.0f] would make the lock screen "
                         "either untestable or visibly frozen",
                         kKeyTimeoutMs, requested * 1000.0, timeout * 1000.0,
                         kMinGrantTimeoutSeconds * 1000.0, kMaxGrantTimeoutSeconds * 1000.0);
        }
    } else if (rawTimeout != nil) {
        os_log_error(log, "config: %{public}@ is not a number; using %.0fms",
                     kKeyTimeoutMs, kDefaultGrantTimeoutSeconds * 1000.0);
    }
    _grantTimeoutSeconds = timeout;

    // --- broker identity: no defaults, deliberately ---
    id rawService = info[kKeyMachService];
    _brokerMachServiceName = [rawService isKindOfClass:[NSString class]] && [rawService length] > 0
        ? rawService : nil;
    if (_brokerMachServiceName == nil) {
        os_log_error(log,
                     "config: %{public}@ missing; the mechanism will yield without "
                     "contacting anything. Guessing a Mach service name would risk "
                     "asking the wrong process whether to unlock this screen.",
                     kKeyMachService);
    }

    id rawRequirement = info[kKeyCodeRequirement];
    _brokerCodeRequirement = [rawRequirement isKindOfClass:[NSString class]] && [rawRequirement length] > 0
        ? rawRequirement : nil;
    if (_brokerCodeRequirement == nil) {
        os_log_error(log,
                     "config: %{public}@ missing; the mechanism will yield rather than "
                     "trust an unverified broker.",
                     kKeyCodeRequirement);
    }

    // --- panic choke ---
    id rawPanicKey = info[kKeyPanicKeyCode];
    _panicKeyCode = [rawPanicKey isKindOfClass:[NSNumber class]]
        ? (CGKeyCode)[rawPanicKey unsignedIntegerValue]
        : (CGKeyCode)kVK_Option;
    _panicChokeEnabled = JARVISBoolValue(info[kKeyPanicEnabled], YES, kKeyPanicEnabled, log);

    // --- master switch ---
    _mechanismEnabled = JARVISBoolValue(info[kKeyMechanismEnabled], YES, kKeyMechanismEnabled, log);

    id rawSchema = info[kKeySchemaVersion];
    _schemaVersion = [rawSchema isKindOfClass:[NSString class]] && [rawSchema length] > 0
        ? rawSchema : JARVISGrantSchemaVersion;

    return self;
}

- (NSString *)describeResolved {
    return [NSString stringWithFormat:
            @"enabled=%d timeout=%.0fms service=%@ requirement=%@ panic=%d key=0x%02X schema=%@",
            _mechanismEnabled,
            _grantTimeoutSeconds * 1000.0,
            _brokerMachServiceName ?: @"<missing>",
            _brokerCodeRequirement ? @"<set>" : @"<missing>",
            _panicChokeEnabled,
            (unsigned)_panicKeyCode,
            _schemaVersion];
}

@end

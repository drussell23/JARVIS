/**
 * JARVISGrantProtocol.m
 * Definitions for the symbols declared in JARVISGrantProtocol.h.
 *
 * These live in their own translation unit because BOTH sides link them. They
 * were briefly defined in JARVISUnlockConfig.m, which linked fine for the plugin
 * and left the broker with undefined symbols -- and the tempting fix (link the
 * plugin's config into the daemon) would have dragged NSBundle/Info.plist
 * resolution into a process that has no bundle, to obtain a string constant.
 *
 * A shared header needs a shared implementation, not a borrowed one.
 */

#import "JARVISGrantProtocol.h"

NSString *const JARVISGrantSchemaVersion = @"grant.1";

NSString *JARVISGrantVerdictName(JARVISGrantVerdict verdict) {
    switch (verdict) {
        case JARVISGrantVerdictGranted:      return @"granted";
        case JARVISGrantVerdictNoGrant:      return @"no_grant";
        case JARVISGrantVerdictExpired:      return @"expired";
        case JARVISGrantVerdictRejected:     return @"rejected";
        case JARVISGrantVerdictTimedOut:     return @"timed_out";
        case JARVISGrantVerdictUnavailable:  return @"unavailable";
        case JARVISGrantVerdictPanicBypass:  return @"panic_bypass";
    }
    // Not a `default:` case, so adding a verdict still produces a compiler
    // warning here rather than silently stringifying as "unknown".
    return [NSString stringWithFormat:@"unknown(%ld)", (long)verdict];
}

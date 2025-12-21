/**
 * UpdateNotificationBadge Component v3.0 - Zero-Touch Edition
 * =============================================================
 *
 * Displays notification badges/modals for:
 * - Remote updates available (new version on GitHub)
 * - Local changes detected (your commits, pushes, code changes)
 * - Restart recommendations (when code changes require restart)
 * - Zero-Touch autonomous update status (v3.0)
 * - Dead Man's Switch monitoring status (v3.0)
 * - Update classification (security/critical/minor/major) (v3.0)
 *
 * Features:
 * - Animated badge that appears when updates/changes are available
 * - Priority-based styling (normal, security, breaking changes, local)
 * - Rich information display (commits behind, summary, highlights)
 * - "Update Now" / "Restart Now" buttons for immediate action
 * - "Later" button to dismiss temporarily
 * - Voice command hint
 * - Local change awareness with auto-restart countdown
 * - Zero-Touch mode indicator with autonomous status (v3.0)
 * - DMS probation progress indicator (v3.0)
 * - Update classification badges (v3.0)
 *
 * Usage:
 *   <UpdateNotificationBadge />
 *
 * Place alongside MaintenanceOverlay in your app root.
 */

import React, { useState, useCallback, useEffect, useMemo } from 'react';
import { useUnifiedWebSocket } from '../services/UnifiedWebSocketService';
import './UpdateNotificationBadge.css';

const UpdateNotificationBadge = () => {
    const {
        updateAvailable,
        updateInfo,
        dismissUpdate,
        localChangesDetected,
        localChangeInfo,
        dismissLocalChanges,
        sendReliable,
        // v3.0: Zero-Touch states
        zeroTouchActive,
        zeroTouchStatus,
        dmsActive,
        dmsStatus,
        // v5.0: Hot Reload states
        hotReloadActive,
        hotReloadStatus,
        devModeEnabled,
    } = useUnifiedWebSocket();

    const [showModal, setShowModal] = useState(false);
    const [updating, setUpdating] = useState(false);
    const [restartCountdown, setRestartCountdown] = useState(null);
    
    // v3.0: Zero-Touch UI states
    const [showZeroTouchDetails, setShowZeroTouchDetails] = useState(false);

    // Determine if we're showing remote update or local changes
    const isLocalChange = localChangesDetected && localChangeInfo;
    const isRemoteUpdate = updateAvailable && updateInfo;
    const isZeroTouchUpdate = zeroTouchActive && zeroTouchStatus;
    const isHotReload = hotReloadActive && hotReloadStatus;
    const hasNotification = isLocalChange || isRemoteUpdate || isZeroTouchUpdate;
    
    // v5.0: Show dev mode indicator (separate from notifications)
    const showDevModeIndicator = devModeEnabled && !isHotReload;
    
    // v3.0: Memoized update classification
    const updateClassification = useMemo(() => {
        if (!updateInfo?.classification) return null;
        const classMap = {
            security: { label: 'Security', icon: '🔒', color: '#ff4444', priority: 4 },
            critical: { label: 'Critical', icon: '⚠️', color: '#ff8800', priority: 3 },
            minor: { label: 'Minor', icon: '📦', color: '#00ff41', priority: 2 },
            major: { label: 'Major', icon: '🚀', color: '#00aaff', priority: 1 },
            patch: { label: 'Patch', icon: '🔧', color: '#888888', priority: 0 },
        };
        return classMap[updateInfo.classification] || classMap.patch;
    }, [updateInfo?.classification]);

    // Handle "Update Now" button click (for remote updates)
    const handleUpdateNow = useCallback(async () => {
        setUpdating(true);
        try {
            await sendReliable({
                type: 'command',
                command: 'update_system',
                source: 'ui_button',
            }, 'general', 5000);

            setShowModal(false);
        } catch (error) {
            console.error('Failed to trigger update:', error);
            setUpdating(false);
        }
    }, [sendReliable]);

    // Handle "Restart Now" button click (for local changes)
    const handleRestartNow = useCallback(async () => {
        setUpdating(true);
        try {
            await sendReliable({
                type: 'command',
                command: 'restart_system',
                source: 'ui_button',
                reason: localChangeInfo?.restart_reason || 'Applying code changes',
            }, 'general', 5000);

            setShowModal(false);
        } catch (error) {
            console.error('Failed to trigger restart:', error);
            setUpdating(false);
        }
    }, [sendReliable, localChangeInfo]);

    // Handle "Later" button click
    const handleLater = useCallback(() => {
        if (isLocalChange) {
            dismissLocalChanges();
        } else {
            dismissUpdate();
        }
        setShowModal(false);
        setRestartCountdown(null);
    }, [isLocalChange, dismissUpdate, dismissLocalChanges]);

    // Toggle modal visibility
    const toggleModal = useCallback(() => {
        setShowModal(prev => !prev);
    }, []);

    // Handle auto-restart countdown (when restart is recommended)
    useEffect(() => {
        if (localChangeInfo?.restart_recommended && !restartCountdown) {
            // Start countdown (5 seconds by default, configurable from backend)
            setRestartCountdown(5);
        }
    }, [localChangeInfo?.restart_recommended, restartCountdown]);

    useEffect(() => {
        if (restartCountdown !== null && restartCountdown > 0) {
            const timer = setTimeout(() => {
                setRestartCountdown(prev => prev - 1);
            }, 1000);
            return () => clearTimeout(timer);
        }
    }, [restartCountdown]);

    // Don't render if no notification AND no dev mode indicator
    if (!hasNotification && !showDevModeIndicator) {
        return null;
    }

    // Determine badge style based on type and priority
    const getBadgeClass = () => {
        // v5.0: Dev Mode indicator
        if (showDevModeIndicator) return 'badge-dev-mode';
        // v3.0: Zero-Touch autonomous update
        if (isZeroTouchUpdate) {
            if (zeroTouchStatus?.state === 'applying') return 'badge-zero-touch-active';
            if (zeroTouchStatus?.state === 'validating') return 'badge-zero-touch-validating';
            if (zeroTouchStatus?.state === 'staging') return 'badge-zero-touch-staging';
            if (zeroTouchStatus?.classification === 'security') return 'badge-security';
            return 'badge-zero-touch';
        }
        if (isLocalChange) {
            if (localChangeInfo.restart_recommended) return 'badge-restart';
            if (localChangeInfo.changeType === 'push') return 'badge-push';
            return 'badge-local';
        }
        // v3.0: Classification-based styling
        if (updateInfo?.classification === 'security') return 'badge-security';
        if (updateInfo?.classification === 'critical') return 'badge-critical';
        if (updateInfo?.security_update) return 'badge-security';
        if (updateInfo?.breaking_changes) return 'badge-breaking';
        if (updateInfo?.priority === 'high') return 'badge-high';
        return 'badge-normal';
    };

    // Get badge icon
    const getBadgeIcon = () => {
        // v5.0: Dev Mode indicator
        if (showDevModeIndicator) return '🔥';
        // v3.0: Zero-Touch icons
        if (isZeroTouchUpdate) {
            if (zeroTouchStatus?.state === 'applying') return '⚡';
            if (zeroTouchStatus?.state === 'validating') return '🔍';
            if (zeroTouchStatus?.state === 'staging') return '📦';
            return '🤖';
        }
        if (isLocalChange) {
            if (localChangeInfo.restart_recommended) return '🔄';
            if (localChangeInfo.changeType === 'push') return '📤';
            if (localChangeInfo.changeType === 'commit') return '📝';
            return '💻';
        }
        // v3.0: Classification icons
        if (updateClassification) return updateClassification.icon;
        if (updateInfo?.security_update) return '🔒';
        if (updateInfo?.breaking_changes) return '⚠️';
        return '📦';
    };

    // Get badge text
    const getBadgeText = () => {
        // v5.0: Dev Mode indicator
        if (showDevModeIndicator) return 'Dev Mode';
        // v3.0: Zero-Touch status text
        if (isZeroTouchUpdate) {
            if (zeroTouchStatus?.state === 'applying') return 'Auto-Updating';
            if (zeroTouchStatus?.state === 'validating') return 'Validating';
            if (zeroTouchStatus?.state === 'staging') return 'Staging';
            if (zeroTouchStatus?.state === 'pending') return 'Auto-Update Ready';
            return 'Zero-Touch Active';
        }
        if (isLocalChange) {
            if (localChangeInfo.restart_recommended) return 'Restart Recommended';
            if (localChangeInfo.changeType === 'push') return 'Code Pushed';
            if (localChangeInfo.changeType === 'commit') return 'New Commit';
            return 'Changes Detected';
        }
        // v3.0: Classification labels
        if (updateClassification) return `${updateClassification.label} Update`;
        return 'Update Available';
    };

    // Get modal title
    const getModalTitle = () => {
        if (isZeroTouchUpdate) {
            return 'Zero-Touch Autonomous Update';
        }
        if (isLocalChange) {
            if (localChangeInfo.restart_recommended) return 'Restart Recommended';
            return 'Local Changes Detected';
        }
        // v3.0: Classification-aware titles
        if (updateClassification) {
            return `${updateClassification.label} Update Available`;
        }
        return 'System Update Available';
    };
    
    // v3.0: Get DMS status display
    const getDmsStatusDisplay = () => {
        if (!dmsActive || !dmsStatus) return null;
        
        const { state, healthScore, probationRemaining, consecutiveFailures } = dmsStatus;
        
        return {
            state,
            healthPercent: Math.round((healthScore || 1) * 100),
            timeRemaining: probationRemaining ? Math.ceil(probationRemaining) : 0,
            failures: consecutiveFailures || 0,
            isHealthy: (healthScore || 1) >= 0.8,
        };
    };
    
    const dmsDisplay = getDmsStatusDisplay();

    return (
        <>
            {/* Floating Badge */}
            <button
                className={`update-notification-badge ${getBadgeClass()}`}
                onClick={toggleModal}
                title="Click for details"
            >
                <span className="badge-icon">{getBadgeIcon()}</span>
                <span className="badge-text">{getBadgeText()}</span>
                {isRemoteUpdate && updateInfo.commits_behind > 0 && (
                    <span className="badge-count">{updateInfo.commits_behind}</span>
                )}
                {isLocalChange && localChangeInfo.commits_since_start > 0 && (
                    <span className="badge-count">{localChangeInfo.commits_since_start}</span>
                )}
                {restartCountdown !== null && restartCountdown > 0 && (
                    <span className="badge-countdown">{restartCountdown}s</span>
                )}
                <span className="badge-pulse" />
            </button>

            {/* Modal Overlay */}
            {showModal && (
                <div className="update-modal-overlay" onClick={handleLater}>
                    <div
                        className={`update-modal ${getBadgeClass()}`}
                        onClick={(e) => e.stopPropagation()}
                    >
                        {/* Header */}
                        <div className="update-modal-header">
                            <span className="modal-icon">{getBadgeIcon()}</span>
                            <h2>{getModalTitle()}</h2>
                            <button
                                className="modal-close"
                                onClick={handleLater}
                                title="Close"
                            >
                                ×
                            </button>
                        </div>

                        {/* Content */}
                        <div className="update-modal-content">
                            {/* Summary */}
                            <p className="update-summary">
                                {isZeroTouchUpdate 
                                    ? zeroTouchStatus?.message || 'Autonomous update in progress'
                                    : isLocalChange 
                                        ? localChangeInfo.summary 
                                        : updateInfo?.summary}
                            </p>
                            
                            {/* v3.0: Zero-Touch Status Panel */}
                            {isZeroTouchUpdate && (
                                <div className="zero-touch-status-panel">
                                    <div className="zt-header">
                                        <span className="zt-icon">🤖</span>
                                        <span className="zt-title">Zero-Touch Mode Active</span>
                                    </div>
                                    
                                    {/* Current Phase */}
                                    <div className="zt-phase">
                                        <span className="phase-label">Phase:</span>
                                        <span className={`phase-value phase-${zeroTouchStatus?.state}`}>
                                            {zeroTouchStatus?.state?.replace(/_/g, ' ').toUpperCase() || 'INITIALIZING'}
                                        </span>
                                    </div>
                                    
                                    {/* Classification Badge */}
                                    {zeroTouchStatus?.classification && (
                                        <div className="zt-classification">
                                            <span className={`classification-badge classification-${zeroTouchStatus.classification}`}>
                                                {zeroTouchStatus.classification.toUpperCase()}
                                            </span>
                                        </div>
                                    )}
                                    
                                    {/* Validation Progress */}
                                    {zeroTouchStatus?.validationProgress && (
                                        <div className="zt-validation">
                                            <div className="validation-bar">
                                                <div 
                                                    className="validation-progress"
                                                    style={{ width: `${zeroTouchStatus.validationProgress}%` }}
                                                />
                                            </div>
                                            <span className="validation-text">
                                                {zeroTouchStatus.filesValidated || 0} files validated
                                            </span>
                                        </div>
                                    )}
                                </div>
                            )}
                            
                            {/* v3.0: Dead Man's Switch Status */}
                            {dmsDisplay && (
                                <div className={`dms-status-panel ${dmsDisplay.isHealthy ? 'healthy' : 'warning'}`}>
                                    <div className="dms-header">
                                        <span className="dms-icon">🎯</span>
                                        <span className="dms-title">Stability Monitor</span>
                                    </div>
                                    
                                    <div className="dms-stats">
                                        <div className="dms-stat">
                                            <span className="stat-label">Health:</span>
                                            <span className={`stat-value ${dmsDisplay.isHealthy ? 'good' : 'warning'}`}>
                                                {dmsDisplay.healthPercent}%
                                            </span>
                                        </div>
                                        <div className="dms-stat">
                                            <span className="stat-label">Probation:</span>
                                            <span className="stat-value">{dmsDisplay.timeRemaining}s</span>
                                        </div>
                                        {dmsDisplay.failures > 0 && (
                                            <div className="dms-stat failures">
                                                <span className="stat-label">Failures:</span>
                                                <span className="stat-value warning">{dmsDisplay.failures}/3</span>
                                            </div>
                                        )}
                                    </div>
                                    
                                    {/* Probation Progress Bar */}
                                    <div className="dms-progress">
                                        <div 
                                            className={`dms-progress-bar ${dmsDisplay.isHealthy ? 'healthy' : 'warning'}`}
                                            style={{ width: `${100 - (dmsDisplay.timeRemaining / 30 * 100)}%` }}
                                        />
                                    </div>
                                </div>
                            )}
                            
                            {/* v3.0: Update Classification Badge */}
                            {updateClassification && !isZeroTouchUpdate && (
                                <div className="update-classification-badge" style={{ borderColor: updateClassification.color }}>
                                    <span className="classification-icon">{updateClassification.icon}</span>
                                    <span className="classification-label" style={{ color: updateClassification.color }}>
                                        {updateClassification.label} Update
                                    </span>
                                </div>
                            )}

                            {/* Stats - Remote Update */}
                            {isRemoteUpdate && !isZeroTouchUpdate && (
                                <div className="update-stats">
                                    <span className="stat-label">Commits behind:</span>
                                    <span className="stat-value">{updateInfo.commits_behind}</span>
                                </div>
                            )}

                            {/* Stats - Local Changes */}
                            {isLocalChange && (
                                <>
                                    {localChangeInfo.commits_since_start > 0 && (
                                        <div className="update-stats">
                                            <span className="stat-label">New commits:</span>
                                            <span className="stat-value">{localChangeInfo.commits_since_start}</span>
                                        </div>
                                    )}
                                    {localChangeInfo.uncommitted_files > 0 && (
                                        <div className="update-stats">
                                            <span className="stat-label">Uncommitted files:</span>
                                            <span className="stat-value">{localChangeInfo.uncommitted_files}</span>
                                        </div>
                                    )}
                                </>
                            )}

                            {/* Priority indicators - Remote Update */}
                            {isRemoteUpdate && updateInfo.security_update && (
                                <div className="update-alert security-alert">
                                    🔒 This update includes security fixes
                                </div>
                            )}
                            {isRemoteUpdate && updateInfo.breaking_changes && (
                                <div className="update-alert breaking-alert">
                                    ⚠️ This update includes breaking changes
                                </div>
                            )}

                            {/* Restart reason - Local Changes */}
                            {isLocalChange && localChangeInfo.restart_recommended && (
                                <div className="update-alert restart-alert">
                                    🔄 {localChangeInfo.restart_reason || 'Code changes require a restart'}
                                </div>
                            )}

                            {/* Countdown warning */}
                            {restartCountdown !== null && restartCountdown > 0 && (
                                <div className="update-alert countdown-alert">
                                    ⏱️ Auto-restarting in {restartCountdown} seconds...
                                </div>
                            )}

                            {/* Modified files preview */}
                            {isLocalChange && localChangeInfo.modified_files?.length > 0 && (
                                <div className="update-highlights">
                                    <h3>Modified files:</h3>
                                    <ul>
                                        {localChangeInfo.modified_files.slice(0, 5).map((file, index) => (
                                            <li key={index}>{file}</li>
                                        ))}
                                        {localChangeInfo.modified_files.length > 5 && (
                                            <li className="more-files">
                                                +{localChangeInfo.modified_files.length - 5} more files
                                            </li>
                                        )}
                                    </ul>
                                </div>
                            )}

                            {/* Highlights - Remote Update */}
                            {isRemoteUpdate && updateInfo.highlights?.length > 0 && (
                                <div className="update-highlights">
                                    <h3>What's new:</h3>
                                    <ul>
                                        {updateInfo.highlights.map((highlight, index) => (
                                            <li key={index}>{highlight}</li>
                                        ))}
                                    </ul>
                                </div>
                            )}

                            {/* Voice command hint */}
                            <p className="voice-hint">
                                💬 You can also say: "{
                                    isZeroTouchUpdate 
                                        ? 'JARVIS, show update status'
                                        : isLocalChange 
                                            ? 'JARVIS, restart now' 
                                            : 'JARVIS, update to the latest version'
                                }"
                            </p>
                            
                            {/* v3.0: Zero-Touch autonomous info */}
                            {updateInfo?.zeroTouchEligible && !isZeroTouchUpdate && (
                                <div className="zero-touch-hint">
                                    <span className="zt-hint-icon">🤖</span>
                                    <span className="zt-hint-text">
                                        This update can be applied automatically when you're idle
                                    </span>
                                </div>
                            )}
                        </div>

                        {/* Actions */}
                        <div className="update-modal-actions">
                            {/* v3.0: Zero-Touch updates have different actions */}
                            {isZeroTouchUpdate ? (
                                <>
                                    <button
                                        className="btn-pause-zt"
                                        onClick={() => sendReliable({ type: 'command', command: 'pause_zero_touch' }, 'general', 3000)}
                                        disabled={updating || zeroTouchStatus?.state === 'applying'}
                                    >
                                        Pause Auto-Update
                                    </button>
                                    <button
                                        className="btn-view-details"
                                        onClick={() => setShowZeroTouchDetails(true)}
                                    >
                                        View Details
                                    </button>
                                </>
                            ) : (
                                <>
                                    <button
                                        className="btn-later"
                                        onClick={handleLater}
                                        disabled={updating}
                                    >
                                        {restartCountdown !== null ? 'Cancel' : 'Later'}
                                    </button>
                                    <button
                                        className={isLocalChange ? 'btn-restart' : 'btn-update'}
                                        onClick={isLocalChange ? handleRestartNow : handleUpdateNow}
                                        disabled={updating}
                                    >
                                        {updating
                                            ? (isLocalChange ? 'Restarting...' : 'Updating...')
                                            : (isLocalChange ? 'Restart Now' : 'Update Now')
                                        }
                                    </button>
                                </>
                            )}
                        </div>
                    </div>
                </div>
            )}
            
            {/* v3.0: Zero-Touch Details Modal */}
            {showZeroTouchDetails && zeroTouchStatus && (
                <div className="zt-details-overlay" onClick={() => setShowZeroTouchDetails(false)}>
                    <div className="zt-details-modal" onClick={(e) => e.stopPropagation()}>
                        <div className="zt-details-header">
                            <h3>Zero-Touch Update Details</h3>
                            <button onClick={() => setShowZeroTouchDetails(false)}>×</button>
                        </div>
                        <div className="zt-details-content">
                            <div className="zt-detail-row">
                                <span className="detail-label">State:</span>
                                <span className="detail-value">{zeroTouchStatus.state}</span>
                            </div>
                            <div className="zt-detail-row">
                                <span className="detail-label">Classification:</span>
                                <span className="detail-value">{zeroTouchStatus.classification || 'N/A'}</span>
                            </div>
                            <div className="zt-detail-row">
                                <span className="detail-label">Files Changed:</span>
                                <span className="detail-value">{zeroTouchStatus.filesChanged || 0}</span>
                            </div>
                            <div className="zt-detail-row">
                                <span className="detail-label">Commits:</span>
                                <span className="detail-value">{zeroTouchStatus.commits || 0}</span>
                            </div>
                            {zeroTouchStatus.validationReport && (
                                <>
                                    <div className="zt-detail-row">
                                        <span className="detail-label">Syntax Errors:</span>
                                        <span className={`detail-value ${zeroTouchStatus.validationReport.syntaxErrors > 0 ? 'error' : 'success'}`}>
                                            {zeroTouchStatus.validationReport.syntaxErrors}
                                        </span>
                                    </div>
                                    <div className="zt-detail-row">
                                        <span className="detail-label">Import Errors:</span>
                                        <span className={`detail-value ${zeroTouchStatus.validationReport.importErrors > 0 ? 'error' : 'success'}`}>
                                            {zeroTouchStatus.validationReport.importErrors}
                                        </span>
                                    </div>
                                </>
                            )}
                        </div>
                    </div>
                </div>
            )}
        </>
    );
};

export default UpdateNotificationBadge;


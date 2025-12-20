/**
 * UpdateNotificationBadge Component v2.0
 * =======================================
 *
 * Displays notification badges/modals for:
 * - Remote updates available (new version on GitHub)
 * - Local changes detected (your commits, pushes, code changes)
 * - Restart recommendations (when code changes require restart)
 *
 * Features:
 * - Animated badge that appears when updates/changes are available
 * - Priority-based styling (normal, security, breaking changes, local)
 * - Rich information display (commits behind, summary, highlights)
 * - "Update Now" / "Restart Now" buttons for immediate action
 * - "Later" button to dismiss temporarily
 * - Voice command hint
 * - Local change awareness with auto-restart countdown
 *
 * Usage:
 *   <UpdateNotificationBadge />
 *
 * Place alongside MaintenanceOverlay in your app root.
 */

import React, { useState, useCallback, useEffect } from 'react';
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
        sendReliable
    } = useUnifiedWebSocket();

    const [showModal, setShowModal] = useState(false);
    const [updating, setUpdating] = useState(false);
    const [restartCountdown, setRestartCountdown] = useState(null);

    // Determine if we're showing remote update or local changes
    const isLocalChange = localChangesDetected && localChangeInfo;
    const isRemoteUpdate = updateAvailable && updateInfo;
    const hasNotification = isLocalChange || isRemoteUpdate;

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

    // Don't render if no notification
    if (!hasNotification) {
        return null;
    }

    // Determine badge style based on type and priority
    const getBadgeClass = () => {
        if (isLocalChange) {
            if (localChangeInfo.restart_recommended) return 'badge-restart';
            if (localChangeInfo.changeType === 'push') return 'badge-push';
            return 'badge-local';
        }
        if (updateInfo?.security_update) return 'badge-security';
        if (updateInfo?.breaking_changes) return 'badge-breaking';
        if (updateInfo?.priority === 'high') return 'badge-high';
        return 'badge-normal';
    };

    // Get badge icon
    const getBadgeIcon = () => {
        if (isLocalChange) {
            if (localChangeInfo.restart_recommended) return '🔄';
            if (localChangeInfo.changeType === 'push') return '📤';
            if (localChangeInfo.changeType === 'commit') return '📝';
            return '💻';
        }
        if (updateInfo?.security_update) return '🔒';
        if (updateInfo?.breaking_changes) return '⚠️';
        return '📦';
    };

    // Get badge text
    const getBadgeText = () => {
        if (isLocalChange) {
            if (localChangeInfo.restart_recommended) return 'Restart Recommended';
            if (localChangeInfo.changeType === 'push') return 'Code Pushed';
            if (localChangeInfo.changeType === 'commit') return 'New Commit';
            return 'Changes Detected';
        }
        return 'Update Available';
    };

    // Get modal title
    const getModalTitle = () => {
        if (isLocalChange) {
            if (localChangeInfo.restart_recommended) return 'Restart Recommended';
            return 'Local Changes Detected';
        }
        return 'System Update Available';
    };

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
                                {isLocalChange ? localChangeInfo.summary : updateInfo?.summary}
                            </p>

                            {/* Stats - Remote Update */}
                            {isRemoteUpdate && (
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
                                💬 You can also say: "{isLocalChange ? 'JARVIS, restart now' : 'JARVIS, update to the latest version'}"
                            </p>
                        </div>

                        {/* Actions */}
                        <div className="update-modal-actions">
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
                        </div>
                    </div>
                </div>
            )}
        </>
    );
};

export default UpdateNotificationBadge;


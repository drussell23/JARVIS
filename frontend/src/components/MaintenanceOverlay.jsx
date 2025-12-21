/**
 * MaintenanceOverlay Component v3.0 - Zero-Touch Edition
 * =======================================================
 * 
 * Displays a premium overlay when JARVIS is in maintenance mode
 * (updating, restarting, rolling back, or Zero-Touch autonomous update).
 * 
 * Features:
 * - Matrix rain effect (canvas-based)
 * - Falling particles animation
 * - Arc reactor spinner
 * - Matches JARVIS green theme (#00ff41)
 * - Zero-Touch autonomous update progress (v3.0)
 * - Dead Man's Switch monitoring display (v3.0)
 * - Update validation progress (v3.0)
 * - Update classification badges (v3.0)
 * 
 * Shows status message instead of a "Connection Error" banner.
 * Automatically hides when the system comes back online.
 * 
 * Usage:
 *   <MaintenanceOverlay />
 * 
 * Place at the root of your app (e.g., in App.js).
 */

import React, { useEffect, useRef, useMemo } from 'react';
import { useUnifiedWebSocket } from '../services/UnifiedWebSocketService';
import './MaintenanceOverlay.css';

// Matrix rain characters
const MATRIX_CHARS = 'アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ';

const MaintenanceOverlay = () => {
    const { 
        maintenanceMode, 
        maintenanceReason, 
        maintenanceMessage,
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
    const canvasRef = useRef(null);
    const animationRef = useRef(null);
    
    // v3.0: Determine if this is a Zero-Touch update
    const isZeroTouchUpdate = maintenanceReason === 'zero_touch' || zeroTouchActive;
    
    // v5.0: Determine if this is a Hot Reload
    const isHotReload = maintenanceReason === 'hot_reload' || hotReloadActive;

    // Matrix rain effect
    useEffect(() => {
        if (!maintenanceMode || !canvasRef.current) return;

        const canvas = canvasRef.current;
        const ctx = canvas.getContext('2d');

        // Set canvas size
        const resizeCanvas = () => {
            canvas.width = window.innerWidth;
            canvas.height = window.innerHeight;
        };

        resizeCanvas();
        window.addEventListener('resize', resizeCanvas);

        // Matrix columns
        const fontSize = 14;
        const columns = Math.floor(canvas.width / fontSize);
        const drops = Array(columns).fill(1);

        // Animation loop
        const draw = () => {
            // Semi-transparent black to create trail effect
            ctx.fillStyle = 'rgba(0, 0, 0, 0.05)';
            ctx.fillRect(0, 0, canvas.width, canvas.height);

            // Green text
            ctx.fillStyle = '#00ff41';
            ctx.font = `${fontSize}px monospace`;

            for (let i = 0; i < drops.length; i++) {
                // Random character
                const char = MATRIX_CHARS[Math.floor(Math.random() * MATRIX_CHARS.length)];

                // Draw character
                ctx.fillText(char, i * fontSize, drops[i] * fontSize);

                // Reset drop to top randomly after reaching bottom
                if (drops[i] * fontSize > canvas.height && Math.random() > 0.975) {
                    drops[i] = 0;
                }

                drops[i]++;
            }

            animationRef.current = requestAnimationFrame(draw);
        };

        draw();

        return () => {
            window.removeEventListener('resize', resizeCanvas);
            if (animationRef.current) {
                cancelAnimationFrame(animationRef.current);
            }
        };
    }, [maintenanceMode]);

    if (!maintenanceMode) {
        return null;
    }

    // Determine icon based on reason
    const getIcon = () => {
        // v5.0: Hot Reload icons
        if (isHotReload) {
            switch (hotReloadStatus?.state) {
                case 'detected': return '👀';
                case 'restarting': return '🔥';
                case 'rebuilding': return '🔨';
                case 'complete': return '✅';
                case 'failed': return '❌';
                default: return '🔥';
            }
        }
        // v3.0: Zero-Touch phase icons
        if (isZeroTouchUpdate) {
            switch (zeroTouchStatus?.state) {
                case 'staging': return '📦';
                case 'validating': return '🔍';
                case 'applying': return '⚡';
                case 'dms_monitoring': return '🎯';
                case 'complete': return '✅';
                default: return '🤖';
            }
        }
        switch (maintenanceReason) {
            case 'updating':
                return '⬇️';
            case 'restarting':
                return '🔄';
            case 'rollback':
                return '⏪';
            case 'dms_rollback':
                return '🎯';
            default:
                return '⚙️';
        }
    };

    // Determine title based on reason
    const getTitle = () => {
        // v5.0: Hot Reload titles
        if (isHotReload) {
            switch (hotReloadStatus?.state) {
                case 'detected': return 'Code Changes Detected';
                case 'restarting': return 'Hot Reloading';
                case 'rebuilding': return 'Rebuilding Frontend';
                case 'complete': return 'Reload Complete';
                case 'failed': return 'Reload Failed';
                default: return 'Hot Reload';
            }
        }
        // v3.0: Zero-Touch phase titles
        if (isZeroTouchUpdate) {
            switch (zeroTouchStatus?.state) {
                case 'staging': return 'Staging Update';
                case 'validating': return 'Validating Code';
                case 'applying': return 'Applying Update';
                case 'dms_monitoring': return 'Monitoring Stability';
                case 'complete': return 'Update Complete';
                default: return 'Autonomous Update';
            }
        }
        switch (maintenanceReason) {
            case 'updating':
                return 'Updating JARVIS';
            case 'restarting':
                return 'Restarting';
            case 'rollback':
                return 'Rolling Back';
            case 'dms_rollback':
                return 'Stability Rollback';
            default:
                return 'Maintenance';
        }
    };
    
    // v3.0: Get Zero-Touch progress percentage
    const getZeroTouchProgress = () => {
        if (!zeroTouchStatus) return 0;
        switch (zeroTouchStatus.state) {
            case 'staging': return 20;
            case 'validating': return 40;
            case 'applying': return 60;
            case 'dms_monitoring': return 80;
            case 'complete': return 100;
            default: return 10;
        }
    };
    
    // v3.0: Get DMS progress
    const getDmsProgress = () => {
        if (!dmsStatus || !dmsStatus.probationRemaining) return 0;
        const total = dmsStatus.probationTotal || 30;
        return 100 - (dmsStatus.probationRemaining / total * 100);
    };
    
    // v3.0: Get classification color
    const getClassificationColor = () => {
        const classification = zeroTouchStatus?.classification;
        switch (classification) {
            case 'security': return '#ff4444';
            case 'critical': return '#ff8800';
            case 'minor': return '#00ff41';
            case 'major': return '#00aaff';
            default: return '#00ff41';
        }
    };

    // Generate particles for CSS animation
    const renderParticles = () => {
        const particles = [];
        for (let i = 1; i <= 20; i++) {
            particles.push(<div key={i} className="particle" />);
        }
        return particles;
    };

    return (
        <div className={`maintenance-overlay ${isZeroTouchUpdate ? 'zero-touch-mode' : ''} ${isHotReload ? 'hot-reload-mode' : ''}`}>
            {/* Matrix rain canvas */}
            <canvas ref={canvasRef} className="matrix-rain-canvas" />

            {/* Dark overlay for readability */}
            <div className="matrix-overlay-dark" />

            {/* Falling particles */}
            <div className="maintenance-particles">
                {renderParticles()}
            </div>

            {/* Main content */}
            <div className="maintenance-content">
                {/* Arc Reactor Style Spinner */}
                <div className="arc-reactor-spinner">
                    <div className="reactor-core"></div>
                    <div className="reactor-ring reactor-ring-1"></div>
                    <div className="reactor-ring reactor-ring-2"></div>
                    <div className="reactor-ring reactor-ring-3"></div>
                </div>

                {/* Status Icon */}
                <div className="maintenance-icon">{getIcon()}</div>

                {/* Title */}
                <h2 className="maintenance-title">{getTitle()}</h2>
                
                {/* v3.0: Zero-Touch Classification Badge */}
                {isZeroTouchUpdate && zeroTouchStatus?.classification && (
                    <div 
                        className="zt-classification-badge"
                        style={{ borderColor: getClassificationColor() }}
                    >
                        <span 
                            className="classification-text"
                            style={{ color: getClassificationColor() }}
                        >
                            {zeroTouchStatus.classification.toUpperCase()} UPDATE
                        </span>
                    </div>
                )}

                {/* Message */}
                <p className="maintenance-message">
                    {maintenanceMessage || (
                        isHotReload ? (hotReloadStatus?.message || 'Applying your changes...') :
                        isZeroTouchUpdate ? 'Autonomous update in progress...' : 
                        'Please wait...'
                    )}
                </p>
                
                {/* v5.0: Hot Reload Info Panel */}
                {isHotReload && hotReloadStatus && (
                    <div className="hot-reload-panel">
                        <div className="hr-header">
                            <span className="hr-icon">🔥</span>
                            <span className="hr-title">Dev Mode Hot Reload</span>
                        </div>
                        
                        {hotReloadStatus.fileTypes && hotReloadStatus.fileTypes.length > 0 && (
                            <div className="hr-file-types">
                                {hotReloadStatus.fileTypes.map((type, idx) => (
                                    <span key={idx} className="file-type-badge">
                                        {type === 'Python' && '🐍'}
                                        {type === 'Rust' && '🦀'}
                                        {type === 'Swift' && '🍎'}
                                        {type === 'JavaScript' && '📜'}
                                        {type === 'TypeScript' && '📘'}
                                        {' '}{type}
                                    </span>
                                ))}
                            </div>
                        )}
                        
                        {hotReloadStatus.fileCount > 0 && (
                            <div className="hr-stats">
                                <span className="hr-stat">
                                    {hotReloadStatus.fileCount} file{hotReloadStatus.fileCount > 1 ? 's' : ''} changed
                                </span>
                                {hotReloadStatus.target && (
                                    <span className="hr-stat hr-target">
                                        Target: {hotReloadStatus.target}
                                    </span>
                                )}
                            </div>
                        )}
                        
                        <div className="hr-progress-steps">
                            <div className={`hr-step ${hotReloadStatus.state === 'detected' ? 'active' : ['restarting', 'rebuilding', 'complete'].includes(hotReloadStatus.state) ? 'complete' : ''}`}>
                                <span className="step-icon">👀</span>
                                <span className="step-label">Detect</span>
                            </div>
                            <div className="phase-connector" />
                            <div className={`hr-step ${hotReloadStatus.state === 'restarting' || hotReloadStatus.state === 'rebuilding' ? 'active' : hotReloadStatus.state === 'complete' ? 'complete' : ''}`}>
                                <span className="step-icon">🔥</span>
                                <span className="step-label">Reload</span>
                            </div>
                            <div className="phase-connector" />
                            <div className={`hr-step ${hotReloadStatus.state === 'complete' ? 'active complete' : ''}`}>
                                <span className="step-icon">✅</span>
                                <span className="step-label">Ready</span>
                            </div>
                        </div>
                    </div>
                )}
                
                {/* v3.0: Zero-Touch Phase Progress */}
                {isZeroTouchUpdate && (
                    <div className="zt-phase-progress">
                        <div className="phase-steps">
                            <div className={`phase-step ${zeroTouchStatus?.state === 'staging' ? 'active' : getZeroTouchProgress() > 20 ? 'complete' : ''}`}>
                                <span className="step-icon">📦</span>
                                <span className="step-label">Stage</span>
                            </div>
                            <div className="phase-connector" />
                            <div className={`phase-step ${zeroTouchStatus?.state === 'validating' ? 'active' : getZeroTouchProgress() > 40 ? 'complete' : ''}`}>
                                <span className="step-icon">🔍</span>
                                <span className="step-label">Validate</span>
                            </div>
                            <div className="phase-connector" />
                            <div className={`phase-step ${zeroTouchStatus?.state === 'applying' ? 'active' : getZeroTouchProgress() > 60 ? 'complete' : ''}`}>
                                <span className="step-icon">⚡</span>
                                <span className="step-label">Apply</span>
                            </div>
                            <div className="phase-connector" />
                            <div className={`phase-step ${zeroTouchStatus?.state === 'dms_monitoring' ? 'active' : getZeroTouchProgress() > 80 ? 'complete' : ''}`}>
                                <span className="step-icon">🎯</span>
                                <span className="step-label">Monitor</span>
                            </div>
                            <div className="phase-connector" />
                            <div className={`phase-step ${zeroTouchStatus?.state === 'complete' ? 'active complete' : ''}`}>
                                <span className="step-icon">✅</span>
                                <span className="step-label">Done</span>
                            </div>
                        </div>
                    </div>
                )}
                
                {/* v3.0: DMS Monitoring Panel */}
                {(dmsActive || zeroTouchStatus?.state === 'dms_monitoring') && dmsStatus && (
                    <div className="dms-monitor-panel">
                        <div className="dms-header">
                            <span className="dms-icon">🎯</span>
                            <span className="dms-title">Dead Man's Switch Active</span>
                        </div>
                        <div className="dms-info">
                            <div className="dms-metric">
                                <span className="metric-label">Health</span>
                                <span className={`metric-value ${(dmsStatus.healthScore || 1) >= 0.8 ? 'good' : 'warning'}`}>
                                    {Math.round((dmsStatus.healthScore || 1) * 100)}%
                                </span>
                            </div>
                            <div className="dms-metric">
                                <span className="metric-label">Probation</span>
                                <span className="metric-value">
                                    {Math.ceil(dmsStatus.probationRemaining || 0)}s
                                </span>
                            </div>
                            {dmsStatus.consecutiveFailures > 0 && (
                                <div className="dms-metric warning">
                                    <span className="metric-label">Failures</span>
                                    <span className="metric-value warning">
                                        {dmsStatus.consecutiveFailures}/3
                                    </span>
                                </div>
                            )}
                        </div>
                        <div className="dms-progress-bar">
                            <div 
                                className="dms-progress-fill"
                                style={{ width: `${getDmsProgress()}%` }}
                            />
                        </div>
                    </div>
                )}
                
                {/* v3.0: Validation Progress */}
                {zeroTouchStatus?.state === 'validating' && zeroTouchStatus.validationProgress && (
                    <div className="validation-panel">
                        <div className="validation-info">
                            <span className="validation-files">
                                {zeroTouchStatus.filesValidated || 0} / {zeroTouchStatus.totalFiles || '?'} files
                            </span>
                            <span className="validation-percent">
                                {Math.round(zeroTouchStatus.validationProgress)}%
                            </span>
                        </div>
                        <div className="validation-progress-bar">
                            <div 
                                className="validation-progress-fill"
                                style={{ width: `${zeroTouchStatus.validationProgress}%` }}
                            />
                        </div>
                    </div>
                )}

                {/* Subtitle */}
                <p className="maintenance-subtitle">
                    {isZeroTouchUpdate 
                        ? 'Zero-Touch autonomous update - no action required.'
                        : "I'll be back shortly, sir."}
                </p>

                {/* Animated progress bar */}
                <div className="maintenance-progress">
                    <div 
                        className="maintenance-progress-bar"
                        style={isZeroTouchUpdate ? { width: `${getZeroTouchProgress()}%` } : {}}
                    />
                </div>
            </div>

            {/* Status strip at bottom */}
            <div className={`maintenance-status-strip ${isZeroTouchUpdate ? 'zero-touch' : ''}`} />
        </div>
    );
};

export default MaintenanceOverlay;

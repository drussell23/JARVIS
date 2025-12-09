/**
 * Unified WebSocket Service for Frontend
 * Uses the dynamic WebSocket client with automatic discovery and routing
 */

import { DynamicWebSocketClient } from '../../backend/websocket/DynamicWebSocketClient';

class UnifiedWebSocketService {
    constructor() {
        this.client = new DynamicWebSocketClient({
            autoDiscover: true,
            reconnectStrategy: 'exponential',
            maxReconnectAttempts: 10,
            heartbeatInterval: 30000,
            dynamicRouting: true,
            messageValidation: true
        });
        
        this.subscriptions = new Map();
        this.connectionState = 'disconnected';
        
        // Setup client event handlers
        this.setupClientHandlers();
    }
    
    setupClientHandlers() {
        // Handle connection state changes
        this.client.on('connected', (clientId) => {
            this.connectionState = 'connected';
            console.log('✅ Connected to unified WebSocket system:', clientId);
            this.notifySubscribers('connection', { state: 'connected', clientId });
        });
        
        this.client.on('disconnected', () => {
            this.connectionState = 'disconnected';
            console.log('🔌 Disconnected from WebSocket system');
            this.notifySubscribers('connection', { state: 'disconnected' });
        });
        
        this.client.on('error', (error) => {
            console.error('WebSocket error:', error);
            this.notifySubscribers('error', error);
        });
    }
    
    /**
     * Connect to a specific capability or general endpoint
     */
    async connect(capability = 'vision') {
        try {
            await this.client.connect(capability);
            return true;
        } catch (error) {
            console.error('Failed to connect:', error);
            return false;
        }
    }
    
    /**
     * Subscribe to a message type
     */
    subscribe(messageType, handler) {
        // Register with client
        this.client.on(messageType, handler);
        
        // Track subscription
        if (!this.subscriptions.has(messageType)) {
            this.subscriptions.set(messageType, new Set());
        }
        this.subscriptions.get(messageType).add(handler);
        
        return () => this.unsubscribe(messageType, handler);
    }
    
    /**
     * Unsubscribe from a message type
     */
    unsubscribe(messageType, handler) {
        const handlers = this.subscriptions.get(messageType);
        if (handlers) {
            handlers.delete(handler);
            if (handlers.size === 0) {
                this.subscriptions.delete(messageType);
            }
        }
    }
    
    /**
     * Send a message
     */
    async send(message, capability) {
        try {
            await this.client.send(message, capability);
        } catch (error) {
            console.error('Failed to send message:', error);
            throw error;
        }
    }

    /**
     * Send a reliable message (awaits ACK)
     */
    async sendReliable(message, capability, timeout = 5000) {
        try {
            await this.client.sendReliable(message, capability, timeout);
        } catch (error) {
            console.error('Failed to send reliable message:', error);
            throw error;
        }
    }
    
    /**
     * Request workspace analysis
     */
    async requestWorkspaceAnalysis() {
        return this.send({
            type: 'request_workspace_analysis',
            timestamp: new Date().toISOString()
        }, 'vision');
    }
    
    /**
     * Set monitoring interval
     */
    async setMonitoringInterval(interval) {
        return this.send({
            type: 'set_monitoring_interval',
            interval
        }, 'vision');
    }
    
    /**
     * Execute an action
     */
    async executeAction(action) {
        return this.send({
            type: 'execute_action',
            action
        }, 'vision');
    }
    
    /**
     * Send vision command
     */
    async sendVisionCommand(command) {
        return this.send({
            type: 'vision_command',
            command
        }, 'vision');
    }
    
    /**
     * Send Claude vision query
     */
    async sendClaudeVision(query) {
        return this.send({
            type: 'claude_vision',
            query
        }, 'vision');
    }
    
    /**
     * Enable/disable autonomous mode
     */
    async setAutonomousMode(enabled) {
        return this.send({
            type: 'set_autonomous_mode',
            enabled
        }, 'vision');
    }
    
    /**
     * Get connection statistics
     */
    getStats() {
        return this.client.getStats();
    }
    
    /**
     * Check if connected
     */
    isConnected() {
        return this.connectionState === 'connected';
    }
    
    /**
     * Disconnect from all endpoints
     */
    disconnect() {
        this.client.destroy();
        this.connectionState = 'disconnected';
    }
    
    /**
     * Notify all subscribers of an event
     */
    notifySubscribers(eventType, data) {
        const handlers = this.subscriptions.get(eventType);
        if (handlers) {
            handlers.forEach(handler => {
                try {
                    handler(data);
                } catch (error) {
                    console.error('Subscriber error:', error);
                }
            });
        }
    }
}

// Singleton instance
let serviceInstance = null;

export function getUnifiedWebSocketService() {
    if (!serviceInstance) {
        serviceInstance = new UnifiedWebSocketService();
    }
    return serviceInstance;
}

// React hook for using the service
export function useUnifiedWebSocket() {
    const [connected, setConnected] = React.useState(false);
    const [stats, setStats] = React.useState(null);
    const service = React.useMemo(() => getUnifiedWebSocketService(), []);
    
    React.useEffect(() => {
        // Subscribe to connection changes
        const unsubscribe = service.subscribe('connection', (data) => {
            setConnected(data.state === 'connected');
        });
        
        // Initial connection state
        setConnected(service.isConnected());
        
        // Update stats periodically
        const interval = setInterval(() => {
            setStats(service.getStats());
        }, 5000);
        
        return () => {
            unsubscribe();
            clearInterval(interval);
        };
    }, [service]);
    
    return {
        service,
        connected,
        stats,
        connect: (capability) => service.connect(capability),
        disconnect: () => service.disconnect(),
        send: (message, capability) => service.send(message, capability),
        sendReliable: (message, capability, timeout) => service.sendReliable(message, capability, timeout),
        subscribe: (messageType, handler) => service.subscribe(messageType, handler)
    };
}

export default UnifiedWebSocketService;
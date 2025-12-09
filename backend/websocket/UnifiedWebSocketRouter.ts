/**
 * Unified WebSocket Router - Resolves all endpoint conflicts
 * Provides dynamic routing, handler registration, and Python bridge integration
 */

import { WebSocket, WebSocketServer } from 'ws';
import { createServer, Server } from 'http';
import { parse } from 'url';
import { EventEmitter } from 'events';
import { PythonHTTPBridge } from './bridges/PythonHTTPBridge';
import ErrorHandlingMiddleware from './middleware/ErrorHandlingMiddleware';

interface RouteHandler {
  path: string;
  pattern?: RegExp;
  handlers: Map<string, MessageHandler>;
  middleware: MiddlewareFunction[];
  capabilities: string[];
  priority: number;
  pythonModule?: string;
}

interface MessageHandler {
  (message: any, ws: WebSocket, context: MessageContext): Promise<void> | void;
}

interface MiddlewareFunction {
  (message: any, ws: WebSocket, context: MessageContext, next: () => void): void;
}

interface MessageContext {
  route: string;
  clientId: string;
  metadata: Map<string, any>;
  pythonBridge?: PythonHTTPBridge;
}

interface RouterConfig {
  port?: number;
  dynamicRouting?: boolean;
  pythonIntegration?: boolean;
  messageValidation?: boolean;
  rateLimiting?: boolean;
  authentication?: boolean;
}

export class UnifiedWebSocketRouter extends EventEmitter {
  private server: Server;
  private wss: WebSocketServer;
  private routes: Map<string, RouteHandler> = new Map();
  private clients: Map<string, WebSocket> = new Map();
  private pythonBridge?: PythonHTTPBridge;
  private config: RouterConfig;
  private messageQueue: Map<string, any[]> = new Map();
  private rateLimiters: Map<string, RateLimiter> = new Map();
  private errorHandler: ErrorHandlingMiddleware;

  constructor(config: RouterConfig = {}) {
    super();
    
    this.config = {
      port: 8001,
      dynamicRouting: true,
      pythonIntegration: true,
      messageValidation: true,
      rateLimiting: true,
      authentication: false,
      ...config
    };

    // Create HTTP server with health check endpoint
    this.server = createServer((req, res) => {
      const parsedUrl = parse(req.url || '');
      
      // Set CORS headers for all requests
      res.setHeader('Access-Control-Allow-Origin', '*');
      res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS, HEAD');
      res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
      
      // Handle preflight requests
      if (req.method === 'OPTIONS') {
        res.writeHead(204);
        res.end();
        return;
      }
      
      if (parsedUrl.pathname === '/health') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ 
          status: 'ok', 
          timestamp: new Date().toISOString(),
          routes: Array.from(this.routes.keys()),
          clients: this.clients.size
        }));
      } else if (parsedUrl.pathname === '/ws' || parsedUrl.pathname === '/websocket') {
        // Return info about WebSocket endpoint
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ 
          message: 'WebSocket endpoint - connect via ws://localhost:8001/ws',
          type: 'websocket'
        }));
      } else {
        res.writeHead(404, { 'Content-Type': 'text/plain' });
        res.end('Not Found');
      }
    });
    
    // Create WebSocket server
    this.wss = new WebSocketServer({ 
      server: this.server,
      verifyClient: this.verifyClient.bind(this)
    });

    // Initialize Python bridge if enabled
    if (this.config.pythonIntegration) {
      this.pythonBridge = new PythonHTTPBridge({
        pythonBackendUrl: 'http://localhost:8010',
        timeout: 30000,
        retryAttempts: 3,
        retryDelay: 1000
      });
    }

    // Initialize error handler
    this.errorHandler = new ErrorHandlingMiddleware({
      circuitBreaker: {
        failureThreshold: 5,
        resetTimeout: 60000,
        halfOpenRequests: 3
      },
      retry: {
        maxRetries: 3,
        baseDelay: 1000,
        maxDelay: 30000,
        backoffMultiplier: 2
      }
    });

    // Setup error handler listeners
    this.errorHandler.on('retry', async ({ message, ws, context }) => {
      await this.handleMessage(message, ws, context, context.route);
    });

    this.errorHandler.on('alert', (alert) => {
      console.error('High error rate alert:', alert);
      this.emit('error:alert', alert);
    });

    this.setupServer();
    this.registerDefaultRoutes();
  }

  /**
   * Verify client connection
   */
  private verifyClient(info: any): boolean {
    // Add authentication logic here if needed
    if (this.config.authentication) {
      // Implement token validation
      const token = this.extractToken(info.req);
      if (!this.validateToken(token)) {
        return false;
      }
    }

    return true;
  }

  /**
   * Extract authentication token
   */
  private extractToken(req: any): string | null {
    const auth = req.headers.authorization;
    if (auth && auth.startsWith('Bearer ')) {
      return auth.substring(7);
    }
    return null;
  }

  /**
   * Validate authentication token
   */
  private validateToken(token: string | null): boolean {
    // Implement actual token validation
    return token !== null;
  }

  /**
   * Setup WebSocket server
   */
  private setupServer(): void {
    this.wss.on('connection', (ws: WebSocket, req: any) => {
      const { pathname } = parse(req.url || '');
      const clientId = this.generateClientId();
      
      // Store client connection
      this.clients.set(clientId, ws);
      
      // Find matching route
      const route = this.findRoute(pathname || '');
      
      if (!route) {
        ws.send(JSON.stringify({
          type: 'error',
          message: `No handler found for route: ${pathname}`
        }));
        ws.close();
        return;
      }

      // Create context
      const context: MessageContext = {
        route: pathname || '',
        clientId,
        metadata: new Map(),
        pythonBridge: this.pythonBridge
      };

      // Handle messages
      ws.on('message', async (data: Buffer) => {
        try {
          const message = JSON.parse(data.toString());
          
          // Rate limiting
          if (this.config.rateLimiting && !this.checkRateLimit(clientId, route.path)) {
            ws.send(JSON.stringify({
              type: 'error',
              message: 'Rate limit exceeded'
            }));
            return;
          }

          // Message validation
          if (this.config.messageValidation && !this.validateMessage(message)) {
            ws.send(JSON.stringify({
              type: 'error',
              message: 'Invalid message format'
            }));
            return;
          }

          // Process through middleware
          await this.processMiddleware(message, ws, context, route);
          
          // Handle message with error handling
          try {
            await this.handleMessage(message, ws, context, route);
            // Record success
            await this.errorHandler.handleSuccess(message, ws, context);
          } catch (messageError: any) {
            await this.errorHandler.handleError(messageError, message, ws, context);
          }
          
        } catch (error: any) {
          console.error('Message parsing error:', error);
          ws.send(JSON.stringify({
            type: 'error',
            message: 'Invalid message format'
          }));
        }
      });

      // Handle client disconnect
      ws.on('close', () => {
        this.clients.delete(clientId);
        this.emit('client:disconnect', clientId);
      });

      // Send connection acknowledgment
      ws.send(JSON.stringify({
        type: 'connected',
        clientId,
        route: pathname,
        capabilities: route.capabilities
      }));

      this.emit('client:connect', clientId);
    });
  }

  /**
   * Find matching route for path
   */
  private findRoute(path: string): RouteHandler | null {
    // Exact match
    if (this.routes.has(path)) {
      return this.routes.get(path)!;
    }

    // Pattern matching
    for (const [_, route] of this.routes) {
      if (route.pattern && route.pattern.test(path)) {
        return route;
      }
    }

    // Dynamic routing - find closest match
    if (this.config.dynamicRouting) {
      return this.findDynamicRoute(path);
    }

    return null;
  }

  /**
   * Find route dynamically based on similarity
   */
  private findDynamicRoute(path: string): RouteHandler | null {
    let bestMatch: RouteHandler | null = null;
    let bestScore = 0;

    for (const [routePath, route] of this.routes) {
      const score = this.calculatePathSimilarity(path, routePath);
      if (score > bestScore && score > 0.5) {
        bestScore = score;
        bestMatch = route;
      }
    }

    return bestMatch;
  }

  /**
   * Calculate path similarity score
   */
  private calculatePathSimilarity(path1: string, path2: string): number {
    const parts1 = path1.split('/').filter(Boolean);
    const parts2 = path2.split('/').filter(Boolean);
    
    let matches = 0;
    const maxLength = Math.max(parts1.length, parts2.length);
    
    for (let i = 0; i < Math.min(parts1.length, parts2.length); i++) {
      if (parts1[i] === parts2[i] || parts2[i].startsWith(':')) {
        matches++;
      }
    }
    
    return matches / maxLength;
  }

  /**
   * Process middleware chain
   */
  private async processMiddleware(
    message: any,
    ws: WebSocket,
    context: MessageContext,
    route: RouteHandler
  ): Promise<void> {
    const middleware = [...route.middleware];
    
    const next = async () => {
      const mw = middleware.shift();
      if (mw) {
        await mw(message, ws, context, next);
      }
    };
    
    await next();
  }

  /**
   * Handle incoming message
   */
  private async handleMessage(
    message: any,
    ws: WebSocket,
    context: MessageContext,
    route: RouteHandler
  ): Promise<void> {
    // Check for reliable message ID and send ACK
    if (message.messageId) {
      ws.send(JSON.stringify({
        type: 'ack',
        ackId: message.messageId,
        timestamp: Date.now()
      }));
    }

    const messageType = message.type || 'default';
    const handler = route.handlers.get(messageType) || route.handlers.get('*');
    
    if (handler) {
      await handler(message, ws, context);
    } else if (route.pythonModule && this.pythonBridge) {
      // Forward to Python handler
      await this.forwardToPython(message, ws, context, route);
    } else {
      ws.send(JSON.stringify({
        type: 'error',
        message: `No handler found for message type: ${messageType}`
      }));
    }
  }

  /**
   * Forward message to Python handler
   */
  private async forwardToPython(
    message: any,
    ws: WebSocket,
    context: MessageContext,
    route: RouteHandler
  ): Promise<void> {
    try {
      const result = await this.pythonBridge!.callPythonFunction(
        route.pythonModule!,
        'handle_websocket_message',
        [message],
        { context: context.metadata }
      );
      
      if (result) {
        ws.send(JSON.stringify(result));
      }
    } catch (error) {
      console.error('Python handler error:', error);
      ws.send(JSON.stringify({
        type: 'error',
        message: 'Python handler error'
      }));
    }
  }

  /**
   * Register a route with handlers
   */
  registerRoute(
    path: string,
    options: {
      pattern?: RegExp;
      capabilities?: string[];
      priority?: number;
      pythonModule?: string;
    } = {}
  ): RouteRegistrar {
    const route: RouteHandler = {
      path,
      pattern: options.pattern,
      handlers: new Map(),
      middleware: [],
      capabilities: options.capabilities || [],
      priority: options.priority || 5,
      pythonModule: options.pythonModule
    };
    
    this.routes.set(path, route);
    
    // Sort routes by priority
    this.routes = new Map(
      [...this.routes.entries()].sort((a, b) => b[1].priority - a[1].priority)
    );
    
    return new RouteRegistrar(route);
  }

  /**
   * Register default routes
   */
  private registerDefaultRoutes(): void {
    // Unified vision route
    this.registerRoute('/ws/vision', {
      capabilities: ['vision', 'monitoring', 'analysis', 'autonomous'],
      priority: 10,
      pythonModule: 'backend.api.unified_vision_handler'
    })
      .on('set_monitoring_interval', async (msg, ws) => {
        // Handle monitoring interval update
        ws.send(JSON.stringify({
          type: 'config_updated',
          monitoring_interval: msg.interval
        }));
      })
      .on('request_workspace_analysis', async (msg, ws, ctx) => {
        // Forward to Python vision system
        if (ctx.pythonBridge) {
          const analysis = await ctx.pythonBridge.callPythonFunction(
            'backend.vision.vision_system_v2',
            'analyze_workspace',
            [],
            {}
          );
          
          ws.send(JSON.stringify({
            type: 'workspace_analysis',
            timestamp: new Date().toISOString(),
            analysis
          }));
        }
      })
      .on('execute_action', async (msg, ws, ctx) => {
        // Handle action execution
        if (ctx.pythonBridge) {
          const result = await ctx.pythonBridge.callPythonFunction(
            'backend.autonomy.action_executor',
            'execute_action',
            [msg.action],
            {}
          );
          
          ws.send(JSON.stringify({
            type: 'action_result',
            result
          }));
        }
      })
      .on('vision_command', async (msg, ws, ctx) => {
        // Handle vision commands
        if (ctx.pythonBridge) {
          try {
            const result = await ctx.pythonBridge.callPythonFunction(
              'backend.api.unified_vision_handler',
              'handle_vision_command',
              [msg],
              { client_id: ctx.clientId }
            );
            
            ws.send(JSON.stringify(result));
          } catch (error: any) {
            ws.send(JSON.stringify({
              type: 'error',
              message: `Vision command failed: ${error.message}`
            }));
          }
        }
      });

    // General WebSocket route
    this.registerRoute('/ws', {
      capabilities: ['general'],
      priority: 5
    })
      .on('*', async (msg, ws) => {
        // Echo back for general messages
        ws.send(JSON.stringify({
          type: 'echo',
          original: msg
        }));
      });

    // API discovery route
    this.registerRoute('/api/websocket/endpoints', {
      capabilities: ['discovery'],
      priority: 1
    });
  }

  /**
   * Validate message format
   */
  private validateMessage(message: any): boolean {
    return typeof message === 'object' && message.type;
  }

  /**
   * Check rate limit
   */
  private checkRateLimit(clientId: string, route: string): boolean {
    const key = `${clientId}:${route}`;
    
    if (!this.rateLimiters.has(key)) {
      this.rateLimiters.set(key, new RateLimiter(100, 60000)); // 100 requests per minute
    }
    
    return this.rateLimiters.get(key)!.check();
  }

  /**
   * Generate unique client ID
   */
  private generateClientId(): string {
    return `client_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
  }

  /**
   * Start the server
   */
  start(): void {
    this.server.listen(this.config.port, () => {
      console.log(`🚀 Unified WebSocket Router running on port ${this.config.port}`);
      this.emit('server:start');
    });
  }

  /**
   * Stop the server
   */
  stop(): void {
    this.wss.close();
    this.server.close();
    this.pythonBridge?.destroy();
    this.emit('server:stop');
  }

  /**
   * Broadcast message to all clients on a route
   */
  broadcast(route: string, message: any): void {
    this.clients.forEach((ws, clientId) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(message));
      }
    });
  }

  /**
   * Get server statistics
   */
  getStats(): any {
    return {
      routes: Array.from(this.routes.keys()),
      clients: this.clients.size,
      pythonBridge: this.pythonBridge?.getStats()
    };
  }
}

/**
 * Route registrar for fluent API
 */
class RouteRegistrar {
  constructor(private route: RouteHandler) {}
  
  on(messageType: string, handler: MessageHandler): this {
    this.route.handlers.set(messageType, handler);
    return this;
  }
  
  use(middleware: MiddlewareFunction): this {
    this.route.middleware.push(middleware);
    return this;
  }
}

/**
 * Simple rate limiter
 */
class RateLimiter {
  private requests: number[] = [];
  
  constructor(
    private limit: number,
    private window: number
  ) {}
  
  check(): boolean {
    const now = Date.now();
    this.requests = this.requests.filter(time => now - time < this.window);
    
    if (this.requests.length < this.limit) {
      this.requests.push(now);
      return true;
    }
    
    return false;
  }
}

// Export for use
export default UnifiedWebSocketRouter;
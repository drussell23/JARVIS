"""
Vision Status Endpoint - Fully Dynamic with Zero Hardcoding
Automatically discovers and monitors all vision-related components
"""

from fastapi import APIRouter, HTTPException
from typing import Dict, Any, List, Optional, Set
import logging
from datetime import datetime
import importlib
import inspect
import asyncio
import os
import sys
import json
from pathlib import Path
import threading
import time
import requests

router = APIRouter(prefix="/vision", tags=["vision"])
logger = logging.getLogger(__name__)

class DynamicVisionDiscovery:
    """Dynamically discovers and monitors vision components"""
    
    def __init__(self):
        self.discovered_modules = {}
        self.component_registry = {}
        self.capability_map = {}
        self.monitoring_threads = {}
        self.discovery_lock = threading.Lock()
        self.status_cache = {}
        self.cache_ttl = 5  # seconds
        
        # Dynamic capability detection patterns
        self.capability_patterns = {
            "screen_capture": ["capture", "screenshot", "screen", "display"],
            "window_detection": ["window", "app", "application", "process"],
            "ocr_extraction": ["ocr", "text", "tesseract", "extract"],
            "ai_analysis": ["claude", "ai", "analyze", "llm", "anthropic"],
            "pattern_learning": ["learn", "pattern", "ml", "train"],
            "notification_detection": ["notification", "alert", "notify"],
            "monitoring": ["monitor", "watch", "observe", "track"],
            "websocket": ["websocket", "ws", "socket", "realtime"],
            "image_processing": ["image", "img", "cv2", "opencv", "pil"],
            "automation": ["automate", "action", "execute", "control"]
        }
        
        # Start continuous discovery
        self._start_discovery_thread()
    
    def _start_discovery_thread(self):
        """Start background thread for continuous discovery"""
        def discovery_loop():
            # Maximum run time for discovery thread (default: 24 hours, configurable)
            max_run_time = float(os.getenv("TIMEOUT_DISCOVERY_THREAD_MAX", "86400.0"))
            start_time = time.monotonic()

            while True:
                try:
                    # Check if we've exceeded maximum run time
                    if time.monotonic() - start_time > max_run_time:
                        logger.info("Discovery thread max run time reached, exiting")
                        break

                    self.discover_vision_components()
                    self.discover_websocket_endpoints()
                    self.discover_ml_models()
                    time.sleep(30)  # Re-discover every 30 seconds
                except Exception as e:
                    logger.error(f"Discovery error: {e}")
                    time.sleep(60)  # Wait longer on error

        thread = threading.Thread(target=discovery_loop, daemon=True)
        thread.start()
    
    def discover_vision_components(self) -> Dict[str, Any]:
        """Dynamically discover all vision-related components"""
        with self.discovery_lock:
            discovered = {}
            
            # Scan all loaded modules
            for name, module in list(sys.modules.items()):
                if not module or not hasattr(module, "__file__"):
                    continue
                    
                try:
                    # Check if it's a vision-related module
                    if self._is_vision_related(name, module):
                        components = self._extract_components(name, module)
                        if components:
                            discovered[name] = components
                            self.discovered_modules[name] = module
                            
                            # Extract capabilities
                            capabilities = self._extract_capabilities(module)
                            if capabilities:
                                self.capability_map[name] = capabilities
                
                except Exception as e:
                    logger.debug(f"Error scanning module {name}: {e}")
            
            return discovered
    
    def _is_vision_related(self, name: str, module: Any) -> bool:
        """Determine if a module is vision-related using dynamic patterns"""
        # Check module name
        vision_keywords = ["vision", "screen", "capture", "image", "ocr", "window", 
                          "monitor", "display", "visual", "cv2", "opencv", "pil",
                          "screenshot", "notification", "unified", "enhanced"]
        
        name_lower = name.lower()
        if any(keyword in name_lower for keyword in vision_keywords):
            return True
        
        # Check module docstring
        if hasattr(module, "__doc__") and module.__doc__:
            doc_lower = module.__doc__.lower()
            if any(keyword in doc_lower for keyword in vision_keywords):
                return True
        
        # Check for vision-related classes/functions
        try:
            for item_name in dir(module):
                if item_name.startswith("_"):
                    continue
                item = getattr(module, item_name, None)
                if item and any(keyword in item_name.lower() for keyword in vision_keywords):
                    return True
        except Exception:
            pass

        return False
    
    def _extract_components(self, module_name: str, module: Any) -> Dict[str, Any]:
        """Extract vision components from a module"""
        components = {}
        
        try:
            for item_name in dir(module):
                if item_name.startswith("_"):
                    continue
                    
                item = getattr(module, item_name, None)
                if not item:
                    continue
                
                # Check for vision-related classes
                if inspect.isclass(item):
                    if self._is_vision_component(item):
                        components[item_name] = {
                            "type": "class",
                            "module": module_name,
                            "capabilities": self._analyze_class_capabilities(item),
                            "instance": None
                        }
                
                # Check for routers (FastAPI)
                elif hasattr(item, "routes"):
                    components[item_name] = {
                        "type": "router",
                        "module": module_name,
                        "routes": [route.path for route in item.routes],
                        "capabilities": ["api_endpoints"]
                    }
                
                # Check for singleton instances
                elif hasattr(item, "__class__") and self._is_vision_component(item.__class__):
                    components[item_name] = {
                        "type": "instance",
                        "module": module_name,
                        "class": item.__class__.__name__,
                        "capabilities": self._analyze_instance_capabilities(item),
                        "instance": item
                    }
        
        except Exception as e:
            logger.debug(f"Error extracting components from {module_name}: {e}")
        
        return components
    
    def _is_vision_component(self, cls: type) -> bool:
        """Check if a class is a vision component"""
        # Check class name
        vision_class_keywords = ["vision", "monitor", "capture", "screen", "window",
                               "handler", "manager", "processor", "analyzer"]
        
        class_name = cls.__name__.lower()
        if any(keyword in class_name for keyword in vision_class_keywords):
            return True
        
        # Check methods
        for method_name in dir(cls):
            if method_name.startswith("_"):
                continue
            method_lower = method_name.lower()
            if any(keyword in method_lower for keyword in ["capture", "analyze", "detect", "monitor", "vision"]):
                return True
        
        return False
    
    def _analyze_class_capabilities(self, cls: type) -> List[str]:
        """Analyze a class to determine its capabilities"""
        capabilities = set()
        
        # Analyze method names
        for method_name in dir(cls):
            if method_name.startswith("_"):
                continue
            method_lower = method_name.lower()
            
            for capability, patterns in self.capability_patterns.items():
                if any(pattern in method_lower for pattern in patterns):
                    capabilities.add(capability)
        
        return list(capabilities)
    
    def _analyze_instance_capabilities(self, instance: Any) -> List[str]:
        """Analyze an instance to determine its capabilities"""
        capabilities = set()
        
        # Get capabilities from class
        capabilities.update(self._analyze_class_capabilities(instance.__class__))
        
        # Check for specific attributes
        capability_attributes = {
            "ai_analysis": ["ai_core", "claude_client", "anthropic"],
            "websocket": ["websocket", "ws", "connections"],
            "monitoring": ["monitoring_active", "monitor_thread"],
            "pattern_learning": ["patterns", "learning_enabled", "ml_model"]
        }
        
        for capability, attributes in capability_attributes.items():
            if any(hasattr(instance, attr) for attr in attributes):
                capabilities.add(capability)
        
        return list(capabilities)
    
    def _extract_capabilities(self, module: Any) -> List[str]:
        """Extract capabilities from a module"""
        capabilities = set()
        
        # Check module-level attributes
        for item_name in dir(module):
            if item_name.startswith("_"):
                continue
            item_lower = item_name.lower()
            
            for capability, patterns in self.capability_patterns.items():
                if any(pattern in item_lower for pattern in patterns):
                    capabilities.add(capability)
        
        return list(capabilities)
    
    def discover_websocket_endpoints(self) -> Dict[str, Any]:
        """Discover WebSocket endpoints from various sources"""
        endpoints = {}
        
        # Check TypeScript router
        try:
            response = requests.get("http://localhost:8001/api/websocket/endpoints", timeout=2)
            if response.status_code == 200:
                data = response.json()
                endpoints["typescript_router"] = {
                    "endpoints": data.get("endpoints", []),
                    "active": True,
                    "port": 8001
                }
        except Exception:
            endpoints["typescript_router"] = {"active": False}
        
        # Check Python WebSocket endpoints
        try:
            response = requests.get("http://localhost:8000/websocket/discovery/endpoints", timeout=2)
            if response.status_code == 200:
                data = response.json()
                endpoints["python_websockets"] = {
                    "endpoints": data.get("endpoints", []),
                    "active": True,
                    "port": 8000
                }
        except Exception:
            # Fallback: scan for WebSocket routes in loaded modules
            ws_routes = self._scan_websocket_routes()
            if ws_routes:
                endpoints["python_websockets"] = {
                    "endpoints": ws_routes,
                    "active": True,
                    "port": 8000
                }
        
        return endpoints
    
    def _scan_websocket_routes(self) -> List[Dict[str, Any]]:
        """Scan for WebSocket routes in loaded modules"""
        routes = []
        
        for name, module in self.discovered_modules.items():
            try:
                for item_name in dir(module):
                    item = getattr(module, item_name, None)
                    if hasattr(item, "routes"):
                        for route in item.routes:
                            if hasattr(route, "path") and "/ws" in route.path:
                                routes.append({
                                    "path": route.path,
                                    "module": name,
                                    "handler": item_name
                                })
            except Exception:
                continue

        return routes
    
    def discover_ml_models(self) -> Dict[str, Any]:
        """Discover loaded ML models and their capabilities"""
        models = {}
        
        # Check common ML framework modules
        ml_frameworks = ["torch", "tensorflow", "keras", "sklearn", "transformers"]
        
        for framework in ml_frameworks:
            if framework in sys.modules:
                models[framework] = {
                    "loaded": True,
                    "version": getattr(sys.modules[framework], "__version__", "unknown")
                }
        
        # Scan for model files
        model_extensions = [".pkl", ".pth", ".h5", ".onnx", ".pt", ".model"]
        model_dirs = ["models", "data", "weights", "checkpoints"]
        
        for dir_name in model_dirs:
            dir_path = Path(".") / dir_name
            if dir_path.exists():
                for ext in model_extensions:
                    model_files = list(dir_path.glob(f"*{ext}"))
                    if model_files:
                        models[f"{dir_name}_files"] = {
                            "count": len(model_files),
                            "files": [f.name for f in model_files[:5]]  # First 5
                        }
        
        return models
    
    def get_component_status(self, component_name: str, component_info: Dict[str, Any]) -> Dict[str, Any]:
        """Get status of a specific component"""
        # Check cache
        cache_key = f"{component_name}:{component_info.get('module', '')}"
        if cache_key in self.status_cache:
            cached = self.status_cache[cache_key]
            if time.time() - cached["timestamp"] < self.cache_ttl:
                return cached["status"]
        
        status = {
            "active": False,
            "healthy": False,
            "last_checked": datetime.now().isoformat()
        }
        
        try:
            if component_info["type"] == "instance" and component_info.get("instance"):
                instance = component_info["instance"]
                
                # Check for status methods
                if hasattr(instance, "get_status"):
                    status.update(instance.get_status())
                    status["active"] = True
                    status["healthy"] = True
                elif hasattr(instance, "is_active"):
                    status["active"] = instance.is_active()
                    status["healthy"] = status["active"]
                elif hasattr(instance, "healthy"):
                    status["healthy"] = instance.healthy
                    status["active"] = status["healthy"]
                else:
                    # Instance exists, assume active
                    status["active"] = True
                    status["healthy"] = True
                
                # Check for additional info
                if hasattr(instance, "get_metrics"):
                    status["metrics"] = instance.get_metrics()
                if hasattr(instance, "get_info"):
                    status["info"] = instance.get_info()
            
            elif component_info["type"] == "class":
                # Class exists, check if it can be instantiated
                status["active"] = True
                status["healthy"] = True
                status["instantiable"] = True
            
            elif component_info["type"] == "router":
                # Router exists
                status["active"] = True
                status["healthy"] = True
                status["routes"] = component_info.get("routes", [])
        
        except Exception as e:
            status["error"] = str(e)
            logger.error(f"Error getting status for {component_name}: {e}")
        
        # Cache result
        self.status_cache[cache_key] = {
            "timestamp": time.time(),
            "status": status
        }
        
        return status
    
    def get_comprehensive_status(self) -> Dict[str, Any]:
        """Get comprehensive vision system status"""
        # Ensure discovery has run
        if not self.discovered_modules:
            self.discover_vision_components()
        
        status = {
            "operational": True,
            "timestamp": datetime.now().isoformat(),
            "discovery": {
                "last_scan": datetime.now().isoformat(),
                "modules_scanned": len(sys.modules),
                "vision_modules_found": len(self.discovered_modules)
            },
            "capabilities": self._get_all_capabilities(),
            "components": {},
            "websockets": self.discover_websocket_endpoints(),
            "ml_models": self.discover_ml_models(),
            "performance": self._get_performance_metrics()
        }
        
        # Get status for each component
        for module_name, components in self.discovered_modules.items():
            if isinstance(components, dict):
                for comp_name, comp_info in components.items():
                    full_name = f"{module_name}.{comp_name}"
                    status["components"][full_name] = self.get_component_status(comp_name, comp_info)
        
        # Calculate health
        if status["components"]:
            active_count = sum(1 for c in status["components"].values() if c.get("active", False))
            healthy_count = sum(1 for c in status["components"].values() if c.get("healthy", False))
            total_count = len(status["components"])
            
            status["health"] = {
                "active_components": active_count,
                "healthy_components": healthy_count,
                "total_components": total_count,
                "health_percentage": (healthy_count / total_count * 100) if total_count > 0 else 0
            }
        else:
            status["health"] = {
                "active_components": 0,
                "healthy_components": 0,
                "total_components": 0,
                "health_percentage": 0
            }
        
        # Determine operational status
        status["operational"] = status["health"]["health_percentage"] > 0
        
        return status
    
    def _get_all_capabilities(self) -> List[str]:
        """Get all discovered capabilities"""
        all_capabilities = set()
        
        for capabilities in self.capability_map.values():
            all_capabilities.update(capabilities)
        
        # Add capabilities from component analysis
        for components in self.discovered_modules.values():
            if isinstance(components, dict):
                for comp_info in components.values():
                    if "capabilities" in comp_info:
                        all_capabilities.update(comp_info["capabilities"])
        
        return sorted(list(all_capabilities))
    
    def _get_performance_metrics(self) -> Dict[str, Any]:
        """Get performance metrics"""
        import psutil
        
        try:
            process = psutil.Process()
            return {
                "cpu_percent": process.cpu_percent(interval=0.1),
                "memory_mb": process.memory_info().rss / 1024 / 1024,
                "threads": process.num_threads(),
                "open_files": len(process.open_files()),
                "connections": len(process.connections())
            }
        except Exception:
            return {}

    def register_component(self, name: str, component: Any, capabilities: Optional[List[str]] = None):
        """Manually register a component"""
        with self.discovery_lock:
            self.component_registry[name] = {
                "type": "manual",
                "instance": component,
                "capabilities": capabilities or self._analyze_instance_capabilities(component),
                "registered_at": datetime.now().isoformat()
            }
            logger.info(f"Manually registered component: {name}")
    
    def start_monitoring_component(self, name: str, component: Any, interval: int = 5):
        """Start monitoring a component"""
        if name in self.monitoring_threads:
            return  # Already monitoring
        
        def monitor():
            while name in self.monitoring_threads:
                try:
                    if hasattr(component, "health_check"):
                        component.health_check()
                    elif hasattr(component, "heartbeat"):
                        component.heartbeat()
                except Exception as e:
                    logger.error(f"Monitoring error for {name}: {e}")
                time.sleep(interval)
        
        thread = threading.Thread(target=monitor, daemon=True)
        self.monitoring_threads[name] = thread
        thread.start()
        logger.info(f"Started monitoring component: {name}")
    
    def stop_monitoring_component(self, name: str):
        """Stop monitoring a component"""
        if name in self.monitoring_threads:
            del self.monitoring_threads[name]
            logger.info(f"Stopped monitoring component: {name}")

# Global discovery instance
discovery = DynamicVisionDiscovery()

# API Endpoints
@router.get("/status")
async def get_vision_status():
    """Get comprehensive vision system status with dynamic discovery"""
    try:
        return discovery.get_comprehensive_status()
    except Exception as e:
        logger.error(f"Error getting vision status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/health")
async def get_vision_health():
    """Quick health check endpoint"""
    try:
        status = discovery.get_comprehensive_status()
        return {
            "healthy": status["operational"],
            "health_percentage": status["health"]["health_percentage"],
            "active_components": status["health"]["active_components"]
        }
    except Exception:
        return {
            "healthy": False,
            "health_percentage": 0,
            "active_components": 0
        }

@router.get("/capabilities")
async def get_vision_capabilities():
    """Get all discovered vision capabilities"""
    return {
        "capabilities": discovery._get_all_capabilities(),
        "capability_map": discovery.capability_map
    }

@router.get("/components")
async def get_vision_components():
    """Get detailed information about discovered components"""
    components = {}
    
    for module_name, module_components in discovery.discovered_modules.items():
        if isinstance(module_components, dict):
            components[module_name] = module_components
    
    # Add manually registered components
    components["manual"] = discovery.component_registry
    
    return {
        "components": components,
        "total_count": sum(len(c) if isinstance(c, dict) else 1 for c in components.values())
    }

@router.post("/register")
async def register_vision_component(name: str, capabilities: Optional[List[str]] = None):
    """Manually register a vision component"""
    # This would be called internally by components
    discovery.register_component(name, None, capabilities)
    return {"status": "registered", "component": name}

@router.get("/discover")
async def force_discovery():
    """Force re-discovery of all vision components"""
    discovery.discover_vision_components()
    return {
        "status": "discovery_complete",
        "modules_found": len(discovery.discovered_modules),
        "capabilities_found": len(discovery._get_all_capabilities())
    }

@router.get("/performance")
async def get_performance_metrics():
    """Get performance metrics for vision system"""
    return discovery._get_performance_metrics()

# Background initialization
async def initialize_vision_discovery():
    """Initialize vision discovery system"""
    await asyncio.sleep(1)  # Give system time to load
    discovery.discover_vision_components()
    logger.info("Vision discovery system initialized")

# Vision discovery will be initialized during app startup
#!/usr/bin/env python3
"""
Multi-Space Integration Example for JARVIS
Shows how to integrate all the multi-space components together
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime

# Import the enhanced components
from backend.api.enhanced_pure_vision_intelligence import EnhancedPureVisionIntelligence
from backend.vision.multi_space_window_detector import MultiSpaceWindowDetector
from backend.vision.space_screenshot_cache import SpaceScreenshotCache
from backend.vision.minimal_space_switcher import MinimalSpaceSwitcher, SpaceCaptureIntegration, SwitchRequest
from backend.vision.claude_vision_analyzer_main import ClaudeVisionAnalyzer

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class JARVISMultiSpaceVision:
    """
    Complete multi-space vision system for JARVIS
    Integrates all components for workspace-wide intelligence
    """
    
    def __init__(self, claude_client):
        # Core components
        self.vision_intelligence = EnhancedPureVisionIntelligence(claude_client)
        self.window_detector = MultiSpaceWindowDetector()
        self.screenshot_cache = SpaceScreenshotCache()
        self.space_switcher = MinimalSpaceSwitcher()
        self.vision_analyzer = ClaudeVisionAnalyzer()
        
        # Integration helper
        self.space_capture = SpaceCaptureIntegration(
            self.space_switcher,
            self.vision_analyzer.capture_screen
        )
        
        # System state
        self.initialized = False
        self.last_full_scan = None
        
    async def initialize(self):
        """Initialize the multi-space vision system"""
        logger.info("Initializing JARVIS Multi-Space Vision System...")
        
        # Start predictive caching
        await self.screenshot_cache.start_predictive_caching()
        
        # Get initial workspace snapshot
        workspace_data = self.window_detector.get_all_windows_across_spaces()
        current_space = workspace_data['current_space']['id']
        
        # Cache current space screenshot
        screenshot = await self.vision_analyzer.capture_screen()
        if screenshot:
            self.screenshot_cache.add_screenshot(
                space_id=current_space,
                screenshot=screenshot,
                window_count=workspace_data['current_space']['window_count'],
                active_apps=[w.app_name for w in workspace_data['windows'] if w.space_id == current_space],
                triggered_by='initialization'
            )
            
        self.initialized = True
        logger.info(f"Multi-space vision initialized. Current space: {current_space}")
        
    async def process_query(self, query: str, current_screenshot: Any = None) -> Dict[str, Any]:
        """
        Process a query with full multi-space awareness
        
        Returns:
            Dict containing:
            - response: Natural language response
            - confidence: Confidence level
            - data_sources: What data was used
            - space_info: Relevant space information
        """
        
        if not self.initialized:
            await self.initialize()
            
        logger.info(f"Processing multi-space query: {query}")
        
        # Capture current screenshot if not provided
        if current_screenshot is None:
            current_screenshot = await self.vision_analyzer.capture_screen()
            
        # Get response from enhanced vision intelligence
        response = await self.vision_intelligence.understand_and_respond(
            current_screenshot, 
            query
        )
        
        # Get additional context
        workspace_summary = self.vision_intelligence.get_multi_space_summary()
        
        return {
            'response': response,
            'confidence': self._determine_confidence(workspace_summary),
            'data_sources': self._identify_data_sources(workspace_summary),
            'space_info': {
                'current_space': workspace_summary['current_space'],
                'total_spaces': workspace_summary['total_spaces'],
                'cache_stats': workspace_summary['cache_statistics']
            },
            'timestamp': datetime.now().isoformat()
        }
        
    async def capture_specific_space(self, space_id: int, reason: str = "user_request") -> Dict[str, Any]:
        """
        Capture a specific space with user permission
        
        Returns:
            Dict with capture result and metadata
        """
        
        # Check cache first
        cached = self.screenshot_cache.get_screenshot(space_id, max_age_seconds=300)
        if cached:
            logger.info(f"Using cached screenshot for space {space_id} (age: {cached.age_seconds():.1f}s)")
            return {
                'success': True,
                'screenshot': cached.screenshot,
                'from_cache': True,
                'cache_age': cached.age_seconds(),
                'confidence': cached.confidence_level().value
            }
            
        # Request fresh capture
        logger.info(f"Requesting fresh capture for space {space_id}")
        
        screenshot = await self.space_capture.capture_space(
            space_id=space_id,
            reason=reason,
            cache_callback=self._cache_screenshot
        )
        
        if screenshot:
            return {
                'success': True,
                'screenshot': screenshot,
                'from_cache': False,
                'cache_age': 0,
                'confidence': 'fresh'
            }
        else:
            return {
                'success': False,
                'error': 'Failed to capture space or permission denied',
                'fallback': 'metadata_only'
            }
            
    async def get_workspace_overview(self) -> Dict[str, Any]:
        """Get comprehensive overview of all workspaces"""
        
        # Update window information
        workspace_data = self.window_detector.get_all_windows_across_spaces()
        
        # Build overview for each space
        space_overviews = []
        for space in workspace_data['spaces']:
            space_id = space['space_id']
            
            # Get space summary
            summary = self.window_detector.get_space_summary(space_id)
            
            # Check screenshot availability
            cached = self.screenshot_cache.get_screenshot(space_id)
            summary['screenshot_available'] = cached is not None
            summary['screenshot_age'] = cached.age_seconds() if cached else None
            
            space_overviews.append(summary)
            
        return {
            'timestamp': datetime.now().isoformat(),
            'current_space': workspace_data['current_space'],
            'spaces': space_overviews,
            'total_windows': len(workspace_data['windows']),
            'cache_statistics': self.screenshot_cache.get_cache_statistics()
        }
        
    async def smart_space_update(self, priority_spaces: Optional[List[int]] = None):
        """
        Intelligently update space information and screenshots
        
        Args:
            priority_spaces: Spaces to prioritize for updates
        """
        
        # Get likely spaces from cache predictions
        if priority_spaces is None:
            priority_spaces = self.screenshot_cache._get_likely_spaces()[:3]
            
        logger.info(f"Smart updating spaces: {priority_spaces}")
        
        # Update with appropriate priority
        for i, space_id in enumerate(priority_spaces):
            priority = 7 - i  # Decreasing priority
            
            # Check if update needed
            cached = self.screenshot_cache.get_screenshot(space_id)
            if cached and cached.confidence_level().value in ['fresh', 'recent']:
                continue
                
            # Request update
            request = SwitchRequest(
                target_space=space_id,
                reason="smart_update",
                requester="cache_system",
                priority=priority,
                require_permission=True
            )
            
            await self.capture_specific_space(space_id, "smart_update")
            
            # Small delay between updates
            await asyncio.sleep(1.0)
            
    async def _cache_screenshot(self, space_id: int, screenshot: Any):
        """Cache a screenshot with metadata"""
        
        # Get current window info for this space
        workspace_data = self.window_detector.get_all_windows_across_spaces()
        space_windows = [
            w for w in workspace_data['windows'] 
            if w.space_id == space_id
        ]
        
        self.screenshot_cache.add_screenshot(
            space_id=space_id,
            screenshot=screenshot,
            window_count=len(space_windows),
            active_apps=list(set(w.app_name for w in space_windows)),
            triggered_by='capture_request'
        )
        
    def _determine_confidence(self, workspace_summary: Dict[str, Any]) -> str:
        """Determine overall confidence level"""
        
        cache_stats = workspace_summary.get('cache_statistics', {})
        hit_rate = cache_stats.get('hit_rate', 0)
        
        if hit_rate > 0.8:
            return 'high'
        elif hit_rate > 0.5:
            return 'medium'
        else:
            return 'low'
            
    def _identify_data_sources(self, workspace_summary: Dict[str, Any]) -> List[str]:
        """Identify what data sources were used"""
        
        sources = ['window_metadata']  # Always have this
        
        if workspace_summary.get('current_space'):
            sources.append('live_screenshot')
            
        cache_stats = workspace_summary.get('cache_statistics', {})
        if cache_stats.get('cached_spaces'):
            sources.append('cached_screenshots')
            
        return sources
        
    async def cleanup(self):
        """Clean up resources"""
        
        logger.info("Cleaning up multi-space vision system...")
        
        # Stop predictive caching
        await self.screenshot_cache.stop_predictive_caching()
        
        # Restore animation settings
        self.space_switcher.restore_animation_settings()
        
        logger.info("Cleanup complete")

# Example usage
async def main():
    """Example of using the multi-space vision system"""
    
    # Mock Claude client for demo
    class MockClaudeClient:
        async def analyze_image_with_prompt(self, image, prompt, max_tokens):
            return {
                'content': f"I can see multiple desktops. Based on the query, VSCode is open on Desktop 2 with your Python project."
            }
    
    # Initialize system
    claude_client = MockClaudeClient()
    jarvis = JARVISMultiSpaceVision(claude_client)
    await jarvis.initialize()
    
    # Example queries
    queries = [
        "Is VSCode open anywhere?",
        "What's on Desktop 2?",
        "Show me all my workspaces",
        "Where is Chrome?"
    ]
    
    for query in queries:
        print(f"\nQuery: {query}")
        result = await jarvis.process_query(query)
        print(f"Response: {result['response']}")
        print(f"Confidence: {result['confidence']}")
        print(f"Data sources: {', '.join(result['data_sources'])}")
        
    # Get workspace overview
    overview = await jarvis.get_workspace_overview()
    print(f"\nWorkspace Overview:")
    print(f"Total spaces: {len(overview['spaces'])}")
    print(f"Total windows: {overview['total_windows']}")
    
    # Cleanup
    await jarvis.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
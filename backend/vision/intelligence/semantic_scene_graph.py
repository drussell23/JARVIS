"""
Semantic Scene Graph - Understanding relationships between visual elements
Builds a comprehensive graph structure to represent visual scene semantics
Memory-optimized for 100MB allocation
"""

import numpy as np
from typing import Dict, List, Optional, Set, Tuple, Any, Union
from dataclasses import dataclass, field
from enum import Enum, auto
import uuid
import cv2
import networkx as nx
from collections import defaultdict, deque
import json
import logging
from datetime import datetime
import mmap
from pathlib import Path

logger = logging.getLogger(__name__)

# Memory allocation constants
MEMORY_LIMITS = {
    'graph_structure': 40 * 1024 * 1024,   # 40MB
    'node_properties': 30 * 1024 * 1024,   # 30MB
    'relationship_index': 30 * 1024 * 1024 # 30MB
}


class NodeType(Enum):
    """Types of nodes in the scene graph"""
    APPLICATION = auto()
    CONTENT = auto()
    UI_ELEMENT = auto()
    INFORMATION = auto()
    CONTAINER = auto()
    UNKNOWN = auto()


class RelationshipType(Enum):
    """Types of relationships between nodes"""
    # Spatial relationships
    CONTAINS = auto()
    CONTAINED_BY = auto()
    ADJACENT_TO = auto()
    OVERLAPS = auto()
    ABOVE = auto()
    BELOW = auto()
    LEFT_OF = auto()
    RIGHT_OF = auto()
    
    # Functional relationships
    CONTROLS = auto()
    CONTROLLED_BY = auto()
    TRIGGERS = auto()
    TRIGGERED_BY = auto()
    DISPLAYS = auto()
    DISPLAYED_BY = auto()
    
    # Semantic relationships
    DESCRIBES = auto()
    DESCRIBED_BY = auto()
    BELONGS_TO = auto()
    REFERENCES = auto()
    MODIFIES = auto()
    MODIFIED_BY = auto()
    
    # Temporal relationships
    BEFORE = auto()
    AFTER = auto()
    CAUSES = auto()
    CAUSED_BY = auto()
    
    # Hierarchical relationships
    PARENT_OF = auto()
    CHILD_OF = auto()
    SIBLING_OF = auto()


@dataclass
class Bounds:
    """Bounding box for visual elements"""
    x: int
    y: int
    width: int
    height: int
    
    @property
    def center(self) -> Tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)
    
    @property
    def area(self) -> int:
        return self.width * self.height
    
    def contains(self, other: 'Bounds') -> bool:
        """Check if this bounds contains another"""
        return (self.x <= other.x and 
                self.y <= other.y and
                self.x + self.width >= other.x + other.width and
                self.y + self.height >= other.y + other.height)
    
    def overlaps(self, other: 'Bounds') -> bool:
        """Check if this bounds overlaps with another"""
        return not (self.x + self.width < other.x or 
                   other.x + other.width < self.x or
                   self.y + self.height < other.y or
                   other.y + other.height < self.y)
    
    def distance_to(self, other: 'Bounds') -> float:
        """Calculate distance between centers"""
        dx = self.center[0] - other.center[0]
        dy = self.center[1] - other.center[1]
        return np.sqrt(dx*dx + dy*dy)


@dataclass
class SceneNode:
    """Base class for all scene graph nodes"""
    node_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    node_type: NodeType = NodeType.UNKNOWN
    bounds: Optional[Bounds] = None
    properties: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    timestamp: datetime = field(default_factory=datetime.now)
    
    def get_property(self, key: str, default: Any = None) -> Any:
        """Get a property value"""
        return self.properties.get(key, default)
    
    def set_property(self, key: str, value: Any):
        """Set a property value"""
        self.properties[key] = value
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            'node_id': self.node_id,
            'node_type': self.node_type.name,
            'bounds': {
                'x': self.bounds.x,
                'y': self.bounds.y,
                'width': self.bounds.width,
                'height': self.bounds.height
            } if self.bounds else None,
            'properties': self.properties,
            'confidence': self.confidence,
            'timestamp': self.timestamp.isoformat()
        }


@dataclass
class ApplicationNode(SceneNode):
    """Node representing an application window"""
    def __init__(self, app_id: str, name: str, bounds: Bounds, **kwargs):
        super().__init__(node_type=NodeType.APPLICATION, bounds=bounds)
        self.properties.update({
            'app_id': app_id,
            'name': name,
            'z_order': kwargs.get('z_order', 0),
            'has_focus': kwargs.get('has_focus', False),
            'is_active': kwargs.get('is_active', True),
            'window_state': kwargs.get('window_state', 'normal')
        })


@dataclass
class ContentNode(SceneNode):
    """Node representing content within an application"""
    def __init__(self, content_type: str, bounds: Optional[Bounds] = None, **kwargs):
        super().__init__(node_type=NodeType.CONTENT, bounds=bounds)
        self.properties.update({
            'content_type': content_type,
            'value': kwargs.get('value'),
            'state': kwargs.get('state', 'idle'),
            'is_modified': kwargs.get('is_modified', False),
            'metadata': kwargs.get('metadata', {})
        })


@dataclass
class UIElementNode(SceneNode):
    """Node representing a UI element"""
    def __init__(self, element_type: str, bounds: Bounds, **kwargs):
        super().__init__(node_type=NodeType.UI_ELEMENT, bounds=bounds)
        self.properties.update({
            'element_type': element_type,
            'is_interactive': kwargs.get('is_interactive', True),
            'is_enabled': kwargs.get('is_enabled', True),
            'value': kwargs.get('value'),
            'label': kwargs.get('label'),
            'tooltip': kwargs.get('tooltip'),
            'keyboard_shortcut': kwargs.get('keyboard_shortcut')
        })


@dataclass
class InformationNode(SceneNode):
    """Node representing information/text"""
    def __init__(self, text: str, bounds: Optional[Bounds] = None, **kwargs):
        super().__init__(node_type=NodeType.INFORMATION, bounds=bounds)
        self.properties.update({
            'text': text,
            'format': kwargs.get('format', 'plain'),
            'language': kwargs.get('language', 'en'),
            'is_editable': kwargs.get('is_editable', False),
            'font_properties': kwargs.get('font_properties', {})
        })


@dataclass
class Relationship:
    """Represents a relationship between nodes"""
    source_id: str
    target_id: str
    relationship_type: RelationshipType
    confidence: float = 1.0
    properties: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            'source_id': self.source_id,
            'target_id': self.target_id,
            'type': self.relationship_type.name,
            'confidence': self.confidence,
            'properties': self.properties
        }


class SceneGraphBuilder:
    """Builds scene graphs from visual data"""
    
    def __init__(self):
        self.current_graph = nx.DiGraph()
        self.nodes: Dict[str, SceneNode] = {}
        self.relationships: List[Relationship] = []
        self.spatial_index = defaultdict(list)  # Grid-based spatial index
        
    async def build_graph(self, screenshot: np.ndarray, 
                         detected_elements: List[Dict[str, Any]]) -> nx.DiGraph:
        """Build a scene graph from screenshot and detected elements"""
        # Clear previous graph
        self.current_graph.clear()
        self.nodes.clear()
        self.relationships.clear()
        
        # Step 1: Extract nodes
        await self._extract_nodes(screenshot, detected_elements)
        
        # Step 2: Discover relationships
        await self._discover_relationships()
        
        # Step 3: Build NetworkX graph
        self._build_networkx_graph()
        
        return self.current_graph
    
    async def _extract_nodes(self, screenshot: np.ndarray, 
                           detected_elements: List[Dict[str, Any]]):
        """Extract nodes from detected elements"""
        # Group elements by type
        for element in detected_elements:
            node = await self._create_node_from_element(element)
            if node:
                self.nodes[node.node_id] = node
                # Update spatial index
                if node.bounds:
                    grid_key = self._get_grid_key(node.bounds)
                    self.spatial_index[grid_key].append(node.node_id)
    
    async def _create_node_from_element(self, element: Dict[str, Any]) -> Optional[SceneNode]:
        """Create appropriate node type from element data"""
        element_type = element.get('type', 'unknown')
        bounds_data = element.get('bounds')
        
        if not bounds_data:
            return None
            
        bounds = Bounds(
            x=bounds_data['x'],
            y=bounds_data['y'],
            width=bounds_data['width'],
            height=bounds_data['height']
        )
        
        # Create appropriate node type
        if element_type == 'application' or element_type == 'window':
            return ApplicationNode(
                app_id=element.get('app_id', 'unknown'),
                name=element.get('name', 'Unknown App'),
                bounds=bounds,
                z_order=element.get('z_order', 0),
                has_focus=element.get('has_focus', False)
            )
        
        elif element_type in ['button', 'input', 'dropdown', 'checkbox', 'slider']:
            return UIElementNode(
                element_type=element_type,
                bounds=bounds,
                is_interactive=element.get('is_interactive', True),
                is_enabled=element.get('is_enabled', True),
                value=element.get('value'),
                label=element.get('label')
            )
        
        elif element_type in ['text', 'label', 'heading']:
            return InformationNode(
                text=element.get('text', ''),
                bounds=bounds,
                format=element.get('format', 'plain'),
                is_editable=element.get('is_editable', False)
            )
        
        elif element_type in ['image', 'video', 'document', 'content']:
            return ContentNode(
                content_type=element_type,
                bounds=bounds,
                value=element.get('value'),
                state=element.get('state', 'idle'),
                is_modified=element.get('is_modified', False)
            )
        
        else:
            # Generic scene node
            node = SceneNode(node_type=NodeType.UNKNOWN, bounds=bounds)
            node.properties.update(element)
            return node
    
    async def _discover_relationships(self):
        """Discover relationships between nodes"""
        # Discover spatial relationships
        await self._discover_spatial_relationships()
        
        # Discover functional relationships
        await self._discover_functional_relationships()
        
        # Discover semantic relationships
        await self._discover_semantic_relationships()
        
        # Discover temporal relationships (if historical data available)
        await self._discover_temporal_relationships()
    
    async def _discover_spatial_relationships(self):
        """Discover spatial relationships between nodes"""
        nodes_list = list(self.nodes.values())
        
        for i, node1 in enumerate(nodes_list):
            if not node1.bounds:
                continue
                
            for j, node2 in enumerate(nodes_list[i+1:], i+1):
                if not node2.bounds:
                    continue
                
                # Check containment
                if node1.bounds.contains(node2.bounds):
                    self.relationships.append(Relationship(
                        source_id=node1.node_id,
                        target_id=node2.node_id,
                        relationship_type=RelationshipType.CONTAINS
                    ))
                    self.relationships.append(Relationship(
                        source_id=node2.node_id,
                        target_id=node1.node_id,
                        relationship_type=RelationshipType.CONTAINED_BY
                    ))
                
                elif node2.bounds.contains(node1.bounds):
                    self.relationships.append(Relationship(
                        source_id=node2.node_id,
                        target_id=node1.node_id,
                        relationship_type=RelationshipType.CONTAINS
                    ))
                    self.relationships.append(Relationship(
                        source_id=node1.node_id,
                        target_id=node2.node_id,
                        relationship_type=RelationshipType.CONTAINED_BY
                    ))
                
                # Check overlap
                elif node1.bounds.overlaps(node2.bounds):
                    self.relationships.append(Relationship(
                        source_id=node1.node_id,
                        target_id=node2.node_id,
                        relationship_type=RelationshipType.OVERLAPS
                    ))
                
                # Check relative positions
                else:
                    dx = node2.bounds.center[0] - node1.bounds.center[0]
                    dy = node2.bounds.center[1] - node1.bounds.center[1]
                    
                    # Horizontal relationships
                    if abs(dy) < min(node1.bounds.height, node2.bounds.height) / 2:
                        if dx > 0:
                            self.relationships.append(Relationship(
                                source_id=node1.node_id,
                                target_id=node2.node_id,
                                relationship_type=RelationshipType.LEFT_OF
                            ))
                        else:
                            self.relationships.append(Relationship(
                                source_id=node1.node_id,
                                target_id=node2.node_id,
                                relationship_type=RelationshipType.RIGHT_OF
                            ))
                    
                    # Vertical relationships
                    if abs(dx) < min(node1.bounds.width, node2.bounds.width) / 2:
                        if dy > 0:
                            self.relationships.append(Relationship(
                                source_id=node1.node_id,
                                target_id=node2.node_id,
                                relationship_type=RelationshipType.ABOVE
                            ))
                        else:
                            self.relationships.append(Relationship(
                                source_id=node1.node_id,
                                target_id=node2.node_id,
                                relationship_type=RelationshipType.BELOW
                            ))
                    
                    # Check adjacency
                    distance = node1.bounds.distance_to(node2.bounds)
                    if distance < 50:  # Threshold for adjacency
                        self.relationships.append(Relationship(
                            source_id=node1.node_id,
                            target_id=node2.node_id,
                            relationship_type=RelationshipType.ADJACENT_TO
                        ))
    
    async def _discover_functional_relationships(self):
        """Discover functional relationships between nodes"""
        # Find UI elements and their potential targets
        ui_elements = [n for n in self.nodes.values() if n.node_type == NodeType.UI_ELEMENT]
        
        for ui_node in ui_elements:
            element_type = ui_node.get_property('element_type')
            
            # Find nearby nodes that might be controlled
            nearby_nodes = self._find_nearby_nodes(ui_node, radius=100)
            
            for nearby_node in nearby_nodes:
                # Buttons might trigger/control nearby elements
                if element_type == 'button':
                    if nearby_node.node_type in [NodeType.CONTENT, NodeType.UI_ELEMENT]:
                        self.relationships.append(Relationship(
                            source_id=ui_node.node_id,
                            target_id=nearby_node.node_id,
                            relationship_type=RelationshipType.CONTROLS,
                            confidence=0.7
                        ))
                
                # Input fields might modify information nodes
                elif element_type in ['input', 'textarea']:
                    if nearby_node.node_type == NodeType.INFORMATION:
                        self.relationships.append(Relationship(
                            source_id=ui_node.node_id,
                            target_id=nearby_node.node_id,
                            relationship_type=RelationshipType.MODIFIES,
                            confidence=0.8
                        ))
    
    async def _discover_semantic_relationships(self):
        """Discover semantic relationships between nodes"""
        # Find information nodes and what they might describe
        info_nodes = [n for n in self.nodes.values() if n.node_type == NodeType.INFORMATION]
        
        for info_node in info_nodes:
            text = info_node.get_property('text', '').lower()
            
            # Find nodes this text might describe
            for node in self.nodes.values():
                if node.node_id == info_node.node_id:
                    continue
                
                # Check if text describes a UI element
                if node.node_type == NodeType.UI_ELEMENT:
                    label = node.get_property('label', '').lower()
                    if label and (label in text or text in label):
                        self.relationships.append(Relationship(
                            source_id=info_node.node_id,
                            target_id=node.node_id,
                            relationship_type=RelationshipType.DESCRIBES,
                            confidence=0.9
                        ))
                
                # Check if nodes are semantically related by proximity
                if info_node.bounds and node.bounds:
                    distance = info_node.bounds.distance_to(node.bounds)
                    if distance < 30:  # Very close proximity
                        if node.node_type == NodeType.CONTENT:
                            self.relationships.append(Relationship(
                                source_id=info_node.node_id,
                                target_id=node.node_id,
                                relationship_type=RelationshipType.DESCRIBES,
                                confidence=0.8
                            ))
    
    async def _discover_temporal_relationships(self):
        """Discover temporal relationships (placeholder for historical analysis)"""
        # This would analyze historical scene graphs to find temporal patterns
        pass
    
    def _find_nearby_nodes(self, node: SceneNode, radius: float) -> List[SceneNode]:
        """Find nodes within a certain radius"""
        if not node.bounds:
            return []
        
        nearby = []
        for other_id, other_node in self.nodes.items():
            if other_id == node.node_id or not other_node.bounds:
                continue
            
            distance = node.bounds.distance_to(other_node.bounds)
            if distance <= radius:
                nearby.append(other_node)
        
        return nearby
    
    def _get_grid_key(self, bounds: Bounds, grid_size: int = 100) -> Tuple[int, int]:
        """Get grid key for spatial indexing"""
        return (bounds.x // grid_size, bounds.y // grid_size)
    
    def _build_networkx_graph(self):
        """Build NetworkX graph from nodes and relationships"""
        # Add nodes
        for node_id, node in self.nodes.items():
            self.current_graph.add_node(
                node_id,
                node_object=node,
                **node.to_dict()
            )
        
        # Add edges
        for rel in self.relationships:
            self.current_graph.add_edge(
                rel.source_id,
                rel.target_id,
                relationship=rel,
                type=rel.relationship_type.name,
                confidence=rel.confidence,
                **rel.properties
            )


class SceneGraphIntelligence:
    """Analyzes scene graphs for patterns and insights"""
    
    def __init__(self):
        self.key_nodes_cache = {}
        self.pattern_library = defaultdict(list)
        self.interaction_maps = {}
    
    def analyze_graph(self, graph: nx.DiGraph) -> Dict[str, Any]:
        """Analyze a scene graph for intelligence insights"""
        analysis = {
            'key_nodes': self._identify_key_nodes(graph),
            'information_flow': self._trace_information_flow(graph),
            'interaction_patterns': self._detect_interaction_patterns(graph),
            'anomalies': self._detect_anomalies(graph),
            'graph_metrics': self._calculate_graph_metrics(graph)
        }
        
        return analysis
    
    def _identify_key_nodes(self, graph: nx.DiGraph) -> List[Dict[str, Any]]:
        """Identify key nodes based on connectivity and importance"""
        key_nodes = []
        
        # Calculate various centrality measures
        degree_centrality = nx.degree_centrality(graph)
        betweenness_centrality = nx.betweenness_centrality(graph)
        
        # Try to calculate eigenvector centrality (may fail on some graphs)
        try:
            eigenvector_centrality = nx.eigenvector_centrality(graph, max_iter=100)
        except Exception:
            eigenvector_centrality = {}
        
        # Combine scores
        for node_id in graph.nodes():
            node = graph.nodes[node_id].get('node_object')
            if not node:
                continue
            
            importance_score = (
                degree_centrality.get(node_id, 0) * 0.3 +
                betweenness_centrality.get(node_id, 0) * 0.4 +
                eigenvector_centrality.get(node_id, 0) * 0.3
            )
            
            # Boost importance for certain node types
            if node.node_type == NodeType.APPLICATION:
                importance_score *= 1.5
            elif node.node_type == NodeType.UI_ELEMENT and node.get_property('is_interactive'):
                importance_score *= 1.2
            
            key_nodes.append({
                'node_id': node_id,
                'node_type': node.node_type.name,
                'importance_score': importance_score,
                'degree': graph.degree(node_id),
                'properties': node.properties
            })
        
        # Sort by importance
        key_nodes.sort(key=lambda x: x['importance_score'], reverse=True)
        
        return key_nodes[:10]  # Top 10 key nodes
    
    def _trace_information_flow(self, graph: nx.DiGraph) -> List[Dict[str, Any]]:
        """Trace information flow paths through the graph"""
        flows = []
        
        # Find information source nodes
        info_sources = [
            n for n in graph.nodes() 
            if graph.nodes[n].get('node_type') == NodeType.INFORMATION.name
        ]
        
        # Find UI elements that might be sinks
        ui_sinks = [
            n for n in graph.nodes()
            if graph.nodes[n].get('node_type') == NodeType.UI_ELEMENT.name
        ]
        
        # Trace paths from sources to sinks
        for source in info_sources[:5]:  # Limit to avoid too many paths
            for sink in ui_sinks[:5]:
                try:
                    # Find all simple paths
                    paths = list(nx.all_simple_paths(graph, source, sink, cutoff=4))
                    if paths:
                        flows.append({
                            'source': source,
                            'sink': sink,
                            'paths': paths[:3],  # Limit paths
                            'shortest_path_length': nx.shortest_path_length(graph, source, sink)
                        })
                except nx.NetworkXNoPath:
                    continue
        
        return flows
    
    def _detect_interaction_patterns(self, graph: nx.DiGraph) -> Dict[str, Any]:
        """Detect common interaction patterns"""
        patterns = {
            'control_clusters': self._find_control_clusters(graph),
            'information_hubs': self._find_information_hubs(graph),
            'ui_workflows': self._find_ui_workflows(graph)
        }
        
        return patterns
    
    def _find_control_clusters(self, graph: nx.DiGraph) -> List[Dict[str, Any]]:
        """Find clusters of UI elements that work together"""
        clusters = []
        
        # Find subgraphs of UI elements
        ui_nodes = [
            n for n in graph.nodes()
            if graph.nodes[n].get('node_type') == NodeType.UI_ELEMENT.name
        ]
        
        if len(ui_nodes) > 1:
            # Create subgraph of UI elements
            ui_subgraph = graph.subgraph(ui_nodes)
            
            # Find connected components
            components = list(nx.weakly_connected_components(ui_subgraph))
            
            for component in components:
                if len(component) > 1:
                    clusters.append({
                        'nodes': list(component),
                        'size': len(component),
                        'density': nx.density(ui_subgraph.subgraph(component))
                    })
        
        return clusters
    
    def _find_information_hubs(self, graph: nx.DiGraph) -> List[str]:
        """Find nodes that are hubs for information flow"""
        hubs = []
        
        # Find nodes with high in/out degree for information relationships
        for node in graph.nodes():
            info_in = sum(
                1 for _, _, data in graph.in_edges(node, data=True)
                if data.get('type') in ['DESCRIBES', 'REFERENCES', 'MODIFIES']
            )
            info_out = sum(
                1 for _, _, data in graph.out_edges(node, data=True)
                if data.get('type') in ['DESCRIBES', 'REFERENCES', 'MODIFIES']
            )
            
            if info_in + info_out > 3:
                hubs.append(node)
        
        return hubs
    
    def _find_ui_workflows(self, graph: nx.DiGraph) -> List[List[str]]:
        """Find potential UI workflows"""
        workflows = []
        
        # Find sequences of UI elements connected by control relationships
        ui_nodes = [
            n for n in graph.nodes()
            if graph.nodes[n].get('node_type') == NodeType.UI_ELEMENT.name
        ]
        
        for start_node in ui_nodes:
            # Try to build workflow from this node
            workflow = [start_node]
            current = start_node
            
            for _ in range(5):  # Max workflow length
                # Find next UI element in control chain
                next_nodes = [
                    target for _, target, data in graph.out_edges(current, data=True)
                    if data.get('type') == 'CONTROLS' and target in ui_nodes
                ]
                
                if next_nodes:
                    current = next_nodes[0]
                    workflow.append(current)
                else:
                    break
            
            if len(workflow) > 2:
                workflows.append(workflow)
        
        return workflows
    
    def _detect_anomalies(self, graph: nx.DiGraph) -> List[Dict[str, Any]]:
        """Detect unusual patterns in the graph"""
        anomalies = []
        
        # Detect isolated nodes
        isolated = list(nx.isolates(graph))
        if isolated:
            anomalies.append({
                'type': 'isolated_nodes',
                'nodes': isolated,
                'severity': 'low'
            })
        
        # Detect unusually high connectivity
        avg_degree = sum(dict(graph.degree()).values()) / len(graph) if len(graph) > 0 else 0
        
        for node, degree in graph.degree():
            if degree > avg_degree * 3:
                anomalies.append({
                    'type': 'high_connectivity',
                    'node': node,
                    'degree': degree,
                    'average': avg_degree,
                    'severity': 'medium'
                })
        
        # Detect circular dependencies
        try:
            cycles = list(nx.simple_cycles(graph))
            if cycles:
                anomalies.append({
                    'type': 'circular_dependencies',
                    'cycles': cycles[:5],  # Limit to 5
                    'severity': 'high'
                })
        except Exception:
            pass
        
        return anomalies
    
    def _calculate_graph_metrics(self, graph: nx.DiGraph) -> Dict[str, Any]:
        """Calculate overall graph metrics"""
        metrics = {
            'node_count': len(graph),
            'edge_count': graph.number_of_edges(),
            'density': nx.density(graph),
            'average_degree': sum(dict(graph.degree()).values()) / len(graph) if len(graph) > 0 else 0,
            'is_connected': nx.is_weakly_connected(graph),
            'component_count': nx.number_weakly_connected_components(graph)
        }
        
        # Node type distribution
        node_types = defaultdict(int)
        for node in graph.nodes(data=True):
            node_type = node[1].get('node_type', 'UNKNOWN')
            node_types[node_type] += 1
        
        metrics['node_type_distribution'] = dict(node_types)
        
        # Relationship type distribution
        rel_types = defaultdict(int)
        for _, _, data in graph.edges(data=True):
            rel_type = data.get('type', 'UNKNOWN')
            rel_types[rel_type] += 1
        
        metrics['relationship_type_distribution'] = dict(rel_types)
        
        return metrics


class SemanticSceneGraph:
    """Main class for Semantic Scene Graph functionality"""
    
    def __init__(self):
        self.builder = SceneGraphBuilder()
        self.intelligence = SceneGraphIntelligence()
        self.graph_history = deque(maxlen=10)  # Keep last 10 graphs
        self.current_graph = None
        self.memory_usage = {
            'graph_structure': 0,
            'node_properties': 0,
            'relationship_index': 0
        }
    
    async def process_scene(self, screenshot: np.ndarray, 
                          detected_elements: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Process a visual scene and build semantic graph"""
        # Build the graph
        graph = await self.builder.build_graph(screenshot, detected_elements)
        self.current_graph = graph
        
        # Add to history
        self.graph_history.append({
            'timestamp': datetime.now(),
            'graph': graph.copy()
        })
        
        # Analyze the graph
        analysis = self.intelligence.analyze_graph(graph)
        
        # Update memory usage
        self._update_memory_usage()
        
        return {
            'graph_metrics': analysis['graph_metrics'],
            'key_nodes': analysis['key_nodes'],
            'information_flow': analysis['information_flow'],
            'interaction_patterns': analysis['interaction_patterns'],
            'anomalies': analysis['anomalies'],
            'node_count': len(self.builder.nodes),
            'relationship_count': len(self.builder.relationships),
            'memory_usage': self.memory_usage
        }
    
    def get_node(self, node_id: str) -> Optional[SceneNode]:
        """Get a specific node by ID"""
        return self.builder.nodes.get(node_id)
    
    def get_relationships(self, node_id: str, 
                        relationship_type: Optional[RelationshipType] = None) -> List[Relationship]:
        """Get relationships for a specific node"""
        relationships = []
        
        for rel in self.builder.relationships:
            if rel.source_id == node_id or rel.target_id == node_id:
                if relationship_type is None or rel.relationship_type == relationship_type:
                    relationships.append(rel)
        
        return relationships
    
    def find_nodes_by_type(self, node_type: NodeType) -> List[SceneNode]:
        """Find all nodes of a specific type"""
        return [
            node for node in self.builder.nodes.values()
            if node.node_type == node_type
        ]
    
    def find_path(self, source_id: str, target_id: str) -> Optional[List[str]]:
        """Find path between two nodes"""
        if not self.current_graph or source_id not in self.current_graph or target_id not in self.current_graph:
            return None
        
        try:
            return nx.shortest_path(self.current_graph, source_id, target_id)
        except nx.NetworkXNoPath:
            return None
    
    def get_subgraph(self, node_ids: List[str]) -> nx.DiGraph:
        """Get subgraph containing specific nodes"""
        if not self.current_graph:
            return nx.DiGraph()
        
        return self.current_graph.subgraph(node_ids)
    
    def _update_memory_usage(self):
        """Update memory usage tracking"""
        import sys
        
        # Estimate graph structure size
        self.memory_usage['graph_structure'] = sys.getsizeof(self.current_graph) if self.current_graph else 0
        
        # Estimate node properties size
        self.memory_usage['node_properties'] = sum(
            sys.getsizeof(node) + sys.getsizeof(node.properties)
            for node in self.builder.nodes.values()
        )
        
        # Estimate relationship index size
        self.memory_usage['relationship_index'] = sum(
            sys.getsizeof(rel) + sys.getsizeof(rel.properties)
            for rel in self.builder.relationships
        )
    
    def export_graph(self, format: str = 'json') -> Union[str, Dict[str, Any]]:
        """Export the current graph in specified format"""
        if format == 'json':
            return {
                'nodes': [node.to_dict() for node in self.builder.nodes.values()],
                'relationships': [rel.to_dict() for rel in self.builder.relationships],
                'metadata': {
                    'timestamp': datetime.now().isoformat(),
                    'node_count': len(self.builder.nodes),
                    'relationship_count': len(self.builder.relationships)
                }
            }
        elif format == 'graphml':
            # Export as GraphML for visualization tools
            from io import StringIO
            output = StringIO()
            nx.write_graphml(self.current_graph, output)
            return output.getvalue()
        else:
            raise ValueError(f"Unsupported export format: {format}")


# Global instance
_scene_graph_instance = None

def get_scene_graph() -> SemanticSceneGraph:
    """Get or create the global scene graph instance"""
    global _scene_graph_instance
    if _scene_graph_instance is None:
        _scene_graph_instance = SemanticSceneGraph()
    return _scene_graph_instance
from typing import Any, Dict, List, Optional, Tuple, Type, Union

from langflow.graph import Edge, Vertex
from langflow.graph.graph.constants import VERTEX_TYPE_MAP
from langflow.graph.vertex.types import (
    FileToolVertex,
    LLMVertex,
    ToolkitVertex,
)
from langflow.graph.vertex.types import ConnectorVertex
from langflow.interface.tools.constants import FILE_TOOLS
from langflow.utils import payload


class Graph:
    def __init__(
        self,
        *,
        graph_data: Optional[Dict] = None,
        nodes: Optional[List[Vertex]] = None,
        edges: Optional[List[Edge]] = None,
    ) -> None:
        self.has_connectors = False

        if graph_data:
            _nodes = graph_data["nodes"]
            _edges = graph_data["edges"]
            self._nodes = _nodes
            self._edges = _edges
            self.nodes = []
            self.edges = []
            self._build_nodes_and_edges()
        elif nodes and edges:
            self.nodes = nodes
            self.edges = edges

    @classmethod
    def from_root_node(cls, root_node: Vertex):
        # Starting at the root node
        # Iterate all of its edges to find
        # all nodes and edges
        nodes, edges = cls.traverse_graph(root_node)
        return cls(nodes=nodes, edges=edges)

    @staticmethod
    def traverse_graph(root_node: Vertex) -> Tuple[List[Vertex], List[Edge]]:
        """
        Traverses the graph from the root_node using depth-first search (DFS) and returns all the nodes and edges.

        Args:
            root_node (Vertex): The root node to start traversal from.

        Returns:
            tuple: A tuple containing a set of all nodes and all edges visited in the graph.
        """
        # Initialize empty sets for visited nodes and edges.
        visited_nodes = set()
        visited_edges = set()

        # Initialize a stack with the root node.
        stack = [root_node]

        # Continue while there are nodes to be visited in the stack.
        while stack:
            # Pop a node from the stack.
            node = stack.pop()

            # If this node has not been visited, add it to visited_nodes.
            if node not in visited_nodes:
                visited_nodes.add(node)

                # Iterate over the edges of the current node.
                for edge in node.edges:
                    # If this edge has not been visited, add it to visited_edges.
                    if edge not in visited_edges:
                        visited_edges.add(edge)

                        # Add the adjacent node (the one that's not the current node)
                        #  to the stack for future exploration.
                        stack.append(
                            edge.source if edge.source != node else edge.target
                        )

        # Return the sets of visited nodes and edges.
        return list(visited_nodes), list(visited_edges)

    def _build_nodes_and_edges(self) -> None:
        self.nodes += self._build_nodes()
        self.edges += self._build_edges()
        for edge in self.edges:
            try:
                edge.source.add_edge(edge)
                edge.target.add_edge(edge)
            except AttributeError as e:
                print(e)
                pass

        # This is a hack to make sure that the LLM node is sent to
        # the toolkit node
        llm_node = None
        for node in self.nodes:
            node._build_params()

            if isinstance(node, LLMVertex):
                llm_node = node

        for node in self.nodes:
            if isinstance(node, ToolkitVertex):
                node.params["llm"] = llm_node
        # remove invalid nodes
        self.nodes = [
            node
            for node in self.nodes
            if self._validate_node(node)
            or (len(self.nodes) == 1 and len(self.edges) == 0)
        ]

    def _validate_node(self, node: Vertex) -> bool:
        # All nodes that do not have edges are invalid
        return len(node.edges) > 0

    def get_node(self, node_id: str) -> Union[None, Vertex]:
        return next((node for node in self.nodes if node.id == node_id), None)

    def get_nodes_with_target(self, node: Vertex) -> List[Vertex]:
        connected_nodes: List[Vertex] = [
            edge.source for edge in self.edges if edge.target == node
        ]
        return connected_nodes

    def build(self) -> Any:
        # Get root node
        root_node = payload.get_root_node(self)
        if root_node is None:
            raise ValueError("No root node found")
        return root_node.build()

    @property
    def root_node(self) -> Union[None, Vertex]:
        return payload.get_root_node(self)

    def get_node_neighbors(self, node: Vertex) -> Dict[Vertex, int]:
        neighbors: Dict[Vertex, int] = {}
        for edge in self.edges:
            if edge.source == node:
                neighbor = edge.target
                if neighbor not in neighbors:
                    neighbors[neighbor] = 0
                neighbors[neighbor] += 1
            elif edge.target == node:
                neighbor = edge.source
                if neighbor not in neighbors:
                    neighbors[neighbor] = 0
                neighbors[neighbor] += 1
        return neighbors

    def _build_edges(self) -> List[Edge]:
        # Edge takes two nodes as arguments, so we need to build the nodes first
        # and then build the edges
        # if we can't find a node, we raise an error

        edges: List[Edge] = []
        for edge in self._edges:
            source = self.get_node(edge["source"])
            target = self.get_node(edge["target"])
            if source is None:
                raise ValueError(f"Source node {edge['source']} not found")
            if target is None:
                raise ValueError(f"Target node {edge['target']} not found")
            edges.append(Edge(source, target))
        return edges

    def _get_node_class(self, node_type: str, node_lc_type: str) -> Type[Vertex]:
        if node_type in FILE_TOOLS:
            return FileToolVertex
        node_class = VERTEX_TYPE_MAP.get(node_type)
        if node_class is None:
            node_class = VERTEX_TYPE_MAP.get(node_lc_type, Vertex)

        return node_class

    def _build_nodes(self) -> List[Vertex]:
        nodes: List[Vertex] = []

        self.expand_flow_nodes(self._nodes)
        for node in self._nodes:
            node_data = node["data"]
            node_type: str = node_data["type"]  # type: ignore
            if node_type == "flow":
                continue
            node_lc_type: str = node_data["node"]["template"].get("_type")  # type: ignore

            # Some nodes are a bit special and need to be handled differently
            # node_type is "flow" and node_data["node"] contains a "flow" key which
            # is itself a graph.

            VertexClass = self._get_node_class(node_type, node_lc_type)
            nodes.append(VertexClass(node))
            if VertexClass == ConnectorVertex:
                self.has_connectors = True

        return nodes

    def expand_flow_nodes(self, nodes: list):
        # Certain nodes are actually graphs themselves, so we need to expand them
        # and add their nodes and edges to the current graph
        # The problem is that the node has an id, and the inner nodes also have an id
        # and the edges also have an id
        # The id is what is used to connect the nodes and edges together
        # So the node id needs to replace the inner node id for the node that has
        # has a root_field in the ["node"]["template"] dict
        # The edges need to be updated to use the new node id
        # The inner nodes need to be updated to use the new node id
        for node in nodes.copy():
            node_data = node["data"]["node"]
            if "flow" in node_data:
                self.expand_flow_node(node)
                nodes.remove(node)

    def expand_flow_node(self, flow_node):
        # Get the subgraph data from the flow node
        subgraph_data = flow_node["data"]["node"]["flow"]["data"]

        # Build the subgraph Graph object
        subgraph = Graph(graph_data=subgraph_data)

        # Set the ID of the subgraph root node to the flow node ID
        subgraph_root = subgraph.root_node
        old_id = subgraph_root.id
        if subgraph_root is None:
            raise ValueError("No root node found")
        subgraph_root.id = flow_node["id"]

        # Get all edges in the subgraph graph that have the subgraph root as the source or target
        edges_to_update = [
            edge
            for edge in subgraph.edges
            if edge.source == subgraph_root or edge.target == subgraph_root
        ]

        # Update all such edges to use the flow node ID instead
        for edge in edges_to_update:
            # The root node shouldn't be the source of any edges, but just in case
            if edge.source.id == old_id:
                edge.source = subgraph_root
            if edge.target.id == old_id:
                edge.target = subgraph_root

        # Add subgraph nodes and edges to the main graph
        self.nodes.extend(subgraph.nodes)
        self.edges.extend(subgraph.edges)

    def get_children_by_node_type(self, node: Vertex, node_type: str) -> List[Vertex]:
        children = []
        node_types = [node.data["type"]]
        if "node" in node.data:
            node_types += node.data["node"]["base_classes"]
        if node_type in node_types:
            children.append(node)
        return children

    def __hash__(self):
        nodes_hash = hash(tuple(self.nodes))
        edges_hash = hash(tuple(self.edges))
        return hash((nodes_hash, edges_hash))

    def __eq__(self, other):
        if isinstance(other, Graph):
            return self.nodes == other.nodes and self.edges == other.edges
        return False
import ReactFlow, {
  Controls,
  Background,
  useNodesState,
  useEdgesState,
  MarkerType,
} from "reactflow";
import "reactflow/dist/style.css";
import { visualizationGraph } from "./visualizationGraph";
import "./WorkflowVisualization.css";

function WorkflowGraph({ workflow, onSelectNode }) {
  const graph = visualizationGraph(workflow);
  const [nodes, , onNodesChange] = useNodesState(graph.nodes);
  const [edges, , onEdgesChange] = useEdgesState(graph.edges.map((edge) => ({
    ...edge,
    markerEnd: { type: MarkerType.ArrowClosed },
  })));

  return (
    <div className="workflow-visualization-container">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        onNodeClick={(_, node) => onSelectNode?.(node.id)}
        fitView
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

export function WorkflowVisualization({ workflow, onSelectNode }) {
  if (!Array.isArray(workflow?.nodes) || !Array.isArray(workflow?.edges)) {
    return (
      <div className="workflow-visualization-empty">
        <p>No workflow graph available for this session.</p>
      </div>
    );
  }
  if (workflow.nodes.length === 0) {
    return (
      <div className="workflow-visualization-empty">
        <p>This workflow has no nodes.</p>
      </div>
    );
  }
  const graphKey = JSON.stringify([
    workflow.nodes,
    workflow.edges,
    workflow.active_node_id,
    workflow.active_node_ids,
    workflow.node_statuses,
    workflow.agent_conversations,
  ]);
  return <WorkflowGraph key={graphKey} workflow={workflow} onSelectNode={onSelectNode} />;
}

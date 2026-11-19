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

function WorkflowGraph({ workflow }) {
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
        fitView
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

export function WorkflowVisualization({ workflow }) {
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
  const graphKey = JSON.stringify([workflow.nodes, workflow.edges]);
  return <WorkflowGraph key={graphKey} workflow={workflow} />;
}

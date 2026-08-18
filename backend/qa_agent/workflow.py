from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .ai_client import AIClient
from .config import Settings, settings


class WorkflowService:
    """Optional static workflow context. It never determines test pass/fail."""

    def __init__(self, ai: AIClient | None = None, config: Settings = settings):
        self.ai = ai or AIClient(config)
        self.config = config

    def summarize(self, workflow: Dict[str, Any]) -> Dict[str, Any]:
        nodes = workflow.get("nodes") if isinstance(workflow.get("nodes"), list) else []
        connections = workflow.get("connections") if isinstance(workflow.get("connections"), dict) else {}
        compact_nodes = []
        for node in nodes[:250]:
            if not isinstance(node, dict):
                continue
            compact_nodes.append({"name": node.get("name", ""), "type": node.get("type", ""), "disabled": bool(node.get("disabled", False))})

        # Keep only topology, not node credentials/parameters. The edge list gives
        # failure diagnostics useful path context while avoiding the cost and risk
        # of storing the full workflow payload in the project record.
        edges: List[Dict[str, str]] = []
        for source, groups in connections.items():
            if len(edges) >= 300:
                break
            if not isinstance(groups, dict):
                continue
            for output_name, branches in groups.items():
                if not isinstance(branches, list):
                    continue
                for branch_index, branch in enumerate(branches):
                    if not isinstance(branch, list):
                        continue
                    for connection in branch:
                        if not isinstance(connection, dict):
                            continue
                        target = str(connection.get("node") or "").strip()
                        if target:
                            edges.append({
                                "source": str(source),
                                "target": target,
                                "output": str(output_name),
                                "branch": str(branch_index),
                            })
                            if len(edges) >= 300:
                                break
                    if len(edges) >= 300:
                        break
                if len(edges) >= 300:
                    break
        return {
            "format": "n8n" if nodes and connections else "generic-json",
            "node_count": len(nodes),
            "connection_groups": len(connections),
            "nodes": compact_nodes,
            "edges": edges,
            "topology_truncated": len(edges) >= 300,
        }

    def advisory_review(self, project: Dict[str, Any], requirements: List[Dict[str, Any]]) -> Dict[str, Any]:
        workflow = project.get("workflow") or {}
        summary = workflow.get("summary") if isinstance(workflow, dict) else None
        if not summary:
            return {"available": False, "summary": "No workflow definition attached.", "findings": []}
        result = self.ai.structured(
            model=self.config.diagnosis_model,
            system=(
                "You are reviewing a workflow definition against documented requirements. This is static advisory analysis only. "
                "Do not claim a requirement definitely fails from structure alone. Identify plausible gaps or areas needing verification."
            ),
            user=f"REQUIREMENTS\n{json.dumps(requirements, ensure_ascii=False)}\n\nWORKFLOW SUMMARY\n{json.dumps(summary, ensure_ascii=False)}",
            schema_name="workflow_advisory_review",
            schema={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "summary": {"type": "string"},
                    "findings": {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {
                        "requirement_id": {"type": "string"}, "severity": {"type": "string", "enum": ["High", "Medium", "Low"]},
                        "possible_gap": {"type": "string"}, "workflow_area": {"type": "string"}, "recommended_check": {"type": "string"}
                    }, "required": ["requirement_id", "severity", "possible_gap", "workflow_area", "recommended_check"]}},
                },
                "required": ["summary", "findings"],
            },
        )
        return {"available": True, **result.value, "ai_usage": result.usage.to_dict()}

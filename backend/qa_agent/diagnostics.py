from __future__ import annotations

import json
from typing import Any, Dict, List

from .ai_client import AIClient
from .config import Settings, settings


class FailureDiagnoser:
    """Advisory failure diagnosis grounded in observed evidence.

    Without an attached workflow definition, this component deliberately avoids
    inventing implementation component names. With a workflow summary it may
    suggest exact areas from that summary, but never treats static workflow
    inspection as proof of the functional root cause.
    """

    def __init__(self, ai: AIClient | None = None, config: Settings = settings):
        self.ai = ai or AIClient(config)
        self.config = config

    def diagnose(
        self,
        project: Dict[str, Any],
        test_case: Dict[str, Any],
        conversation: Dict[str, Any],
        evaluation: Dict[str, Any],
        requirements: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not self.config.diagnosis_enabled:
            return {
                "failure_category": "Unclassified",
                "observed_problem": evaluation.get("summary", ""),
                "evidence": [],
                "likely_causes": [],
                "recommended_checks": [],
                "suspected_components": [],
                "confidence": "Low",
                "ai_usage": {},
            }

        requirement_map = {item["id"]: item for item in requirements}
        relevant = [
            requirement_map[requirement_id]
            for requirement_id in test_case.get("requirement_ids", [])
            if requirement_id in requirement_map
        ]
        workflow_summary = ((project.get("workflow") or {}).get("summary") or {})
        workflow_available = bool(workflow_summary)
        transcript = "\n".join(
            f"{turn.get('role', '').upper()}: {turn.get('content', '')}"
            for turn in conversation.get("turns", [])
            if turn.get("role") in {"user", "assistant"}
        )
        human_evidence = [
            {
                "title": str((item.get("action") or {}).get("title") or "Human action"),
                "status": str(item.get("status") or ""),
                "observation": str(item.get("note") or "").strip(),
            }
            for item in conversation.get("human_actions", []) or []
        ]

        workflow_instruction = (
            "A workflow summary IS available. suspected_components may contain only exact or clearly identifiable areas from the supplied workflow summary. Treat them as places to inspect, not proven causes."
            if workflow_available
            else "No workflow definition is available. suspected_components MUST be an empty array. likely_causes must use implementation-agnostic categories such as state persistence, validation, integration handling, or conversation progression; do not invent module/node/component names."
        )

        result = self.ai.structured(
            model=self.config.diagnosis_model,
            system=(
                "You are a senior QA failure analyst. Separate OBSERVED EVIDENCE from INFERRED CAUSES. "
                "Use only the transcript, evaluation and documented requirements. Be explicit about uncertainty. "
                "Never claim hidden implementation behavior as fact."
            ),
            user=(
                f"TEST\n{json.dumps({key: test_case.get(key) for key in ['id','title','user_goal','objectives','expected_result','scenario_data']}, ensure_ascii=False)}\n\n"
                f"REQUIREMENTS\n{json.dumps(relevant, ensure_ascii=False)}\n\n"
                f"EVALUATION\n{json.dumps(evaluation, ensure_ascii=False)}\n\n"
                f"CONVERSATION\n{transcript}\n\n"
                f"RUNTIME HUMAN ACTION EVIDENCE\n{json.dumps(human_evidence, ensure_ascii=False)}\n\n"
                f"WORKFLOW AVAILABLE\n{workflow_available}\n"
                f"OPTIONAL WORKFLOW SUMMARY\n{json.dumps(workflow_summary, ensure_ascii=False)}\n\n"
                f"DIAGNOSIS RULE\n{workflow_instruction}"
            ),
            schema_name="qa_failure_diagnosis",
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "failure_category": {"type": "string"},
                    "observed_problem": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "likely_causes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "cause": {"type": "string"},
                                "confidence": {"type": "string", "enum": ["High", "Medium", "Low"]},
                            },
                            "required": ["cause", "confidence"],
                        },
                    },
                    "recommended_checks": {"type": "array", "items": {"type": "string"}},
                    "suspected_components": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["High", "Medium", "Low"]},
                },
                "required": [
                    "failure_category",
                    "observed_problem",
                    "evidence",
                    "likely_causes",
                    "recommended_checks",
                    "suspected_components",
                    "confidence",
                ],
            },
        )
        value = dict(result.value)
        if not workflow_available:
            value["suspected_components"] = []
        value["workflow_evidence_available"] = workflow_available
        value["ai_usage"] = result.usage.to_dict()
        return value


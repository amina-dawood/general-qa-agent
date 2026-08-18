from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .ai_client import AIClient, UsageCollector
from .config import Settings, settings
from .rules import RuleValidator


class DeepEvalAdapter:
    """One focused multi-turn DeepEval judge.

    DeepEval is lazy-imported only when a completed conversation needs semantic
    evaluation. Conversational G-Eval is kept as the single semantic judge to
    avoid metric explosion while still covering task completion, context and
    recovery in one bounded evaluation pass.
    """

    def __init__(self, ai: AIClient, config: Settings = settings):
        self.ai = ai
        self.config = config

    def evaluate(
        self,
        test_case: Dict[str, Any],
        conversation: Dict[str, Any],
        requirement_context: List[str],
        usage: UsageCollector,
    ) -> Dict[str, Any]:
        from deepeval.metrics import ConversationalGEval
        try:
            from deepeval.models import DeepEvalBaseLLM
        except ImportError:  # compatibility with early DeepEval 4 builds
            from deepeval.models.base_model import DeepEvalBaseLLM
        from deepeval.test_case import ConversationalTestCase, MultiTurnParams, Turn

        outer = self

        class Model(DeepEvalBaseLLM):
            def load_model(self):
                return outer.ai

            def generate(self, prompt: str, schema=None):
                system = (
                    "You are a strict production QA evaluator. Judge only observable evidence and documented expectations. "
                    "Do not infer hidden backend success. Follow the requested output schema exactly."
                )
                if schema is not None:
                    result = outer.ai.pydantic_json(
                        model=outer.config.evaluation_model,
                        system=system,
                        user=prompt,
                        schema_type=schema,
                    )
                else:
                    result = outer.ai.text(
                        model=outer.config.evaluation_model,
                        system=system,
                        user=prompt,
                    )
                usage.add(result.usage)
                return result.value

            async def a_generate(self, prompt: str, schema=None):
                return self.generate(prompt, schema=schema)

            def get_model_name(self):
                return outer.config.evaluation_model

        turns = [
            Turn(
                role="user" if turn.get("role") == "user" else "assistant",
                content=str(turn.get("content", "")),
            )
            for turn in conversation.get("turns", [])
            if turn.get("role") in {"user", "assistant"} and str(turn.get("content", "")).strip()
        ]
        if not turns:
            raise RuntimeError("No conversation turns were available for semantic evaluation.")

        human_evidence = []
        for item in conversation.get("human_actions", []) or []:
            if str(item.get("status") or "") != "completed":
                continue
            action = item.get("action") or {}
            title = str(action.get("title") or "Human action").strip()
            note = str(item.get("note") or "").strip()
            if note:
                human_evidence.append(f"Runtime human observation - {title}: {note}")

        case = ConversationalTestCase(
            scenario=(
                f"{test_case.get('title', '')}. User goal: {test_case.get('user_goal', '')}. "
                f"QA objectives: {'; '.join(test_case.get('objectives', []))}"
            ),
            expected_outcome=str(test_case.get("expected_result", "")),
            user_description=str(test_case.get("persona", "")),
            context=(requirement_context + human_evidence) or None,
            turns=turns,
        )
        metric = ConversationalGEval(
            name="Production requirement adherence",
            evaluation_steps=[
                "Judge whether the assistant achieved the documented expected observable outcome; never infer hidden backend success without evidence. Completed runtime human observations in context are valid external evidence for browser/account effects that the chat transcript cannot directly observe.",
                "Check consistency with mapped requirements and acceptance criteria, including validation and recovery behavior when required.",
                "Check context retention: facts already supplied by the user should be remembered unless re-confirmation is clearly justified.",
                "Check progression and completion: repeated questions, backwards state transitions, contradictory state, loops, or stalled flows reduce the score.",
                "Ignore transport latency here because performance is evaluated separately by deterministic operational thresholds.",
                "Do not fail functionally correct behavior for minor wording, tone, punctuation, or equivalent phrasing differences.",
            ],
            evaluation_params=[MultiTurnParams.CONTENT],
            model=Model(),
            threshold=self.config.evaluation_threshold,
            async_mode=False,
            verbose_mode=False,
        )
        metric.measure(case)
        score = max(0.0, min(float(metric.score or 0.0), 1.0))
        passed = bool(getattr(metric, "success", score >= self.config.evaluation_threshold))
        return {
            "engine": "deepeval",
            "metric": "ConversationalGEval",
            "passed": passed and score >= self.config.evaluation_threshold,
            "score": round(score, 4),
            "threshold": self.config.evaluation_threshold,
            "reason": str(metric.reason or "No evaluator reason returned."),
            "model": self.config.evaluation_model,
        }


class HybridEvaluator:
    def __init__(self, ai: AIClient | None = None, config: Settings = settings):
        self.ai = ai or AIClient(config)
        self.config = config
        self.rules = RuleValidator(config)
        self.deepeval = DeepEvalAdapter(self.ai, config)

    def evaluate(
        self,
        test_case: Dict[str, Any],
        conversation: Dict[str, Any],
        requirements: List[Dict[str, Any]],
        target_config: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        usage = UsageCollector()
        rule_checks = self.rules.validate(test_case, conversation)
        performance = self.rules.performance_summary(test_case, conversation, target_config)

        hard_failures = [
            check
            for check in rule_checks
            if not check["passed"] and check.get("severity") == "error"
        ]
        performance_failure = bool(performance.get("blocking"))

        semantic: Optional[Dict[str, Any]] = None
        evaluation_error = ""
        if self.config.enable_deepeval:
            requirement_map = {item["id"]: item for item in requirements}
            context: List[str] = []
            for requirement_id in test_case.get("requirement_ids", []):
                item = requirement_map.get(requirement_id)
                if item:
                    context.append(
                        f"{item['id']}: {item['description']} Acceptance: "
                        + "; ".join(item.get("acceptance_criteria", []))
                    )
            try:
                semantic = self.deepeval.evaluate(test_case, conversation, context, usage)
            except Exception as exc:
                evaluation_error = str(exc)
        else:
            deterministic_checks = [
                check for check in rule_checks if check.get("severity") == "error"
            ]
            rule_score = (
                sum(1 for check in deterministic_checks if check["passed"]) / len(deterministic_checks)
                if deterministic_checks
                else 1.0
            )
            semantic = {
                "engine": "rules-only",
                "metric": "deterministic",
                "passed": not hard_failures,
                "score": round(rule_score, 4),
                "threshold": self.config.evaluation_threshold,
                "reason": "DeepEval disabled; deterministic functional checks were used.",
                "model": "",
            }

        if evaluation_error:
            passed = False
            score = 0.0
            status = "error"
            summary = f"Semantic evaluation failed: {evaluation_error}"
        else:
            assert semantic is not None
            passed = not hard_failures and not performance_failure and bool(semantic["passed"])
            score = float(semantic["score"])
            status = "passed" if passed else "failed"
            parts: List[str] = []
            if hard_failures:
                parts.append("Deterministic failure: " + "; ".join(check["message"] for check in hard_failures))
            if performance_failure:
                parts.append("Performance failure: " + str(performance.get("message") or "Hard latency threshold exceeded."))
            elif performance.get("status") in {"warning", "critical"}:
                parts.append("Performance advisory: " + str(performance.get("message") or ""))
            parts.append(str(semantic["reason"]))
            summary = " | ".join(part for part in parts if part)

        return {
            "status": status,
            "passed": passed,
            "score": round(score, 4),
            "summary": summary,
            "rule_checks": rule_checks,
            "performance": performance,
            "semantic": semantic,
            "evaluation_error": evaluation_error,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "ai_usage": usage.snapshot(),
        }


from __future__ import annotations

import copy
import inspect
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from .adapters.base import TargetAdapter, TargetBlockedError, TargetError
from .adapters.factory import build_adapter
from .ai_client import AIClient
from .config import Settings, settings
from .db import Database, database
from .diagnostics import FailureDiagnoser
from .evaluator import HybridEvaluator
from .generator import DISCLOSURE_STYLES, STATE_MODES, TEST_TYPES, TestGenerator
from .simulator import UserSimulator
from .utils import new_id, normalize_text, utc_now

FIXTURE_PATTERN = re.compile(r"^\{FIXTURE:([A-Za-z0-9_.-]+)\}$")
URL_PATTERN = re.compile(r"https?://[^\s<>\]\[\"']+", re.IGNORECASE)
RESOURCE_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:url|link|ics|webhook|invite|calendar_export|export_link|authorization_url|oauth_url|file_path|account_id|external_id|otp|token|verification_code|access_code|invite_code|auth_code)(?:$|_)",
    re.IGNORECASE,
)
USAGE_COUNTERS = (
    "requests",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
)


def empty_usage() -> Dict[str, Any]:
    return {
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
        "models": {},
    }


def merge_usage_dict(target: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    for key in USAGE_COUNTERS:
        target[key] = int(target.get(key, 0) or 0) + int(source.get(key, 0) or 0)
    target["cost_usd"] = round(
        float(target.get("cost_usd", 0.0) or 0.0) + float(source.get("cost_usd", 0.0) or 0.0),
        8,
    )
    models = target.setdefault("models", {})
    for model, count in (source.get("models", {}) or {}).items():
        models[model] = int(models.get(model, 0)) + int(count or 0)
    return target


def usage_delta(after: Dict[str, Any], before: Dict[str, Any]) -> Dict[str, Any]:
    """Return non-negative AI usage added after a paused case was resumed."""

    delta = empty_usage()
    for key in USAGE_COUNTERS:
        delta[key] = max(0, int(after.get(key, 0) or 0) - int(before.get(key, 0) or 0))
    delta["cost_usd"] = round(
        max(0.0, float(after.get("cost_usd", 0.0) or 0.0) - float(before.get("cost_usd", 0.0) or 0.0)),
        8,
    )
    before_models = before.get("models", {}) or {}
    for model, count in (after.get("models", {}) or {}).items():
        change = int(count or 0) - int(before_models.get(model, 0) or 0)
        if change > 0:
            delta["models"][model] = change
    return delta


def resolve_fixtures(value: Any, fixtures: Dict[str, Any]) -> Any:
    if isinstance(value, str):
        match = FIXTURE_PATTERN.match(value.strip())
        if match:
            key = match.group(1)
            if key not in fixtures or fixtures.get(key) in {None, ""}:
                raise TargetBlockedError(
                    f"Required test resource '{key}' is not configured. Add it in Projects -> Test resources, then run the case again."
                )
            return fixtures[key]
        return value
    if isinstance(value, dict):
        return {key: resolve_fixtures(item, fixtures) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_fixtures(item, fixtures) for item in value]
    return value


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "auto", "enabled"}


class QAEngine:
    def __init__(
        self,
        db: Database = database,
        ai: AIClient | None = None,
        config: Settings = settings,
        documents=None,
    ):
        from .documents import DocumentService

        self.db = db
        self.ai = ai or AIClient(config)
        self.config = config
        self.documents = documents or DocumentService(db, self.ai, config)
        self.generator = TestGenerator(self.documents, self.ai, config)
        self.simulator = UserSimulator(self.ai, config)
        self.evaluator = HybridEvaluator(self.ai, config)
        self.diagnoser = FailureDiagnoser(self.ai, config)

    def generate_suite(
        self,
        project_id: str,
        feature: str,
        query: str,
        progress=None,
        generation_prompt: str | None = None,
    ) -> Dict[str, Any]:
        project = self._project(project_id)
        suite, usage = self.generator.generate(
            project,
            feature,
            query,
            progress,
            generation_prompt=generation_prompt,
        )
        suite["generation_ai_usage"] = usage
        return self.db.save_suite(suite)

    def execute_suite(
        self,
        project_id: str,
        suite_id: str,
        priority: str = "All",
        limit: int = 20,
        test_case_ids: Optional[List[str]] = None,
        progress=None,
    ) -> Dict[str, Any]:
        project = self._project(project_id)
        suite = self.db.get_suite(suite_id)
        if not suite or suite.get("project_id") != project_id:
            raise ValueError("Suite not found for this project.")
        if not suite.get("approved"):
            raise ValueError("Suite must be approved before execution.")

        cases = [
            copy.deepcopy(case)
            for case in suite.get("test_cases", [])
            if case.get("approved") and case.get("review_status") == "approved"
        ]
        if priority and priority != "All":
            cases = [case for case in cases if case.get("priority") == priority]
        if test_case_ids:
            selected = set(test_case_ids)
            cases = [case for case in cases if case.get("id") in selected]
        cases = cases[: max(1, int(limit))]
        if not cases:
            raise ValueError("No approved test cases match the execution filters.")

        target_config = project.get("target") or {}
        isolation_warnings: List[str] = []
        has_fresh = any(str(case.get("state_mode") or "fresh_user") == "fresh_user" for case in cases)
        if (
            str(target_config.get("adapter") or "").strip().lower() == "twilio_webhook"
            and has_fresh
            and not str(target_config.get("reset_url") or "").strip()
        ):
            auto_isolation = _as_bool(target_config.get("isolate_fresh_users"), True)
            if auto_isolation:
                isolation_warnings.append(
                    "Fresh-user isolation is active. Each fresh-user case uses a unique synthetic E.164 sender derived "
                    "from the configured test sender, while the tested endpoint and To number remain unchanged. "
                    "Returning/continuation cases keep the configured sender."
                )
            else:
                isolation_warnings.append(
                    "Fresh-user isolation is disabled and no reset URL is configured. If the target stores state by "
                    "sender identity, a fresh-user test can inherit earlier test state. Enable fresh-user isolation or "
                    "configure a real reset endpoint before trusting fresh-user results."
                )

        run_number = self.db.next_run_number(project_id)
        run = {
            "id": new_id("run"),
            "project_id": project_id,
            "run_number": run_number,
            "display_id": f"Run {run_number}",
            "suite_id": suite_id,
            "suite_name": suite.get("name", ""),
            "status": "running",
            "is_baseline": False,
            "started_at": utc_now(),
            "ended_at": "",
            "duration_ms": 0,
            "active_duration_ms": 0,
            "results": [],
            "execution_cases": cases,
            "next_case_index": 0,
            "current_result_index": None,
            "pending_human_action": None,
            "passed_count": 0,
            "failed_count": 0,
            "blocked_count": 0,
            "error_count": 0,
            "pass_rate": 0.0,
            "ai_usage": empty_usage(),
            "warnings": isolation_warnings,
        }
        self.db.save_run(run)
        return self._continue_run(project, suite, run, progress)

    def resume_run(
        self,
        run_id: str,
        completed: bool,
        note: str = "",
        progress=None,
    ) -> Dict[str, Any]:
        """Resume a run that paused for a real human/browser/account action.

        No worker is held while the tester is completing the external step. The
        previous background job has already finished and this method is called by
        a new background job when the tester chooses Continue/Unable to complete.
        """

        run = self.db.get_run(run_id)
        if not run:
            raise ValueError("Run not found.")
        if run.get("status") != "awaiting_human":
            raise ValueError("This run is not waiting for a human action.")

        project = self._project(run["project_id"])
        suite = self.db.get_suite(run.get("suite_id", ""))
        if not suite:
            raise ValueError("Suite not found for this run.")

        result_index = run.get("current_result_index")
        if result_index is None:
            raise ValueError("Paused run does not contain a resumable test state.")
        result_index = int(result_index)
        results = run.get("results", [])
        if result_index < 0 or result_index >= len(results):
            raise ValueError("Paused test result could not be located.")

        paused_result = results[result_index]
        pending = dict(run.get("pending_human_action") or paused_result.get("pending_human_action") or {})
        if not pending:
            raise ValueError("The pending human action could not be located.")

        if not completed:
            self._mark_human_action_unavailable(paused_result, pending, note)
            run["results"][result_index] = paused_result
            run["next_case_index"] = int(run.get("next_case_index", 0) or 0) + 1
            run["current_result_index"] = None
            run["pending_human_action"] = None
            run["status"] = "running"
            self._recount(run)
            self.db.save_run(run)
            return self._continue_run(project, suite, run, progress)

        previous_usage = copy.deepcopy(paused_result.get("ai_usage") or empty_usage())
        previous_duration = int(paused_result.get("duration_ms", 0) or 0)
        adapter: TargetAdapter | None = None
        try:
            if progress:
                progress(8, f"Resuming {paused_result.get('test_case_id', 'test case')} after human action...")
            adapter = build_adapter(project.get("target") or {})
            raw_case = self._case_for_paused_result(run, paused_result)
            resumed = self.execute_case(
                project,
                suite,
                raw_case,
                run["id"],
                adapter=adapter,
                existing_result=paused_result,
                human_resolution={
                    "completed": True,
                    "note": note.strip(),
                    "action": pending,
                },
            )
            run["results"][result_index] = resumed
            merge_usage_dict(run["ai_usage"], usage_delta(resumed.get("ai_usage", {}), previous_usage))
            run["active_duration_ms"] = int(run.get("active_duration_ms", 0) or 0) + max(
                0,
                int(resumed.get("duration_ms", 0) or 0) - previous_duration,
            )
            run["duration_ms"] = int(run.get("active_duration_ms", 0) or 0)

            if resumed.get("outcome") == "awaiting_human":
                run["status"] = "awaiting_human"
                run["pending_human_action"] = resumed.get("pending_human_action")
                run["current_result_index"] = result_index
                self._recount(run)
                self.db.save_run(run)
                if progress:
                    progress(96, "Another human action is required before this run can continue.")
                return run

            run["next_case_index"] = int(run.get("next_case_index", 0) or 0) + 1
            run["current_result_index"] = None
            run["pending_human_action"] = None
            run["status"] = "running"
            self._recount(run)
            self.db.save_run(run)
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    pass

        return self._continue_run(project, suite, run, progress)

    def _continue_run(
        self,
        project: Dict[str, Any],
        suite: Dict[str, Any],
        run: Dict[str, Any],
        progress=None,
    ) -> Dict[str, Any]:
        cases = run.get("execution_cases") or []
        index = int(run.get("next_case_index", 0) or 0)
        adapter: TargetAdapter | None = None
        run_error: Exception | None = None
        try:
            # One small HTTP pool for a continuous execution segment minimizes
            # TCP/TLS overhead. A paused HITL run releases this pool immediately.
            adapter = build_adapter(project.get("target") or {})
            while index < len(cases):
                case = cases[index]
                if progress:
                    progress(
                        int(5 + index / max(1, len(cases)) * 85),
                        f"Running {case['id']} ({index + 1}/{len(cases)})...",
                    )

                result = self.execute_case(project, suite, case, run["id"], adapter=adapter)
                run["results"].append(result)
                result_index = len(run["results"]) - 1
                merge_usage_dict(run["ai_usage"], result.get("ai_usage", {}))
                run["active_duration_ms"] = int(run.get("active_duration_ms", 0) or 0) + int(
                    result.get("duration_ms", 0) or 0
                )
                run["duration_ms"] = int(run.get("active_duration_ms", 0) or 0)
                self._recount(run)

                if result.get("outcome") == "awaiting_human":
                    run["status"] = "awaiting_human"
                    run["pending_human_action"] = result.get("pending_human_action")
                    run["current_result_index"] = result_index
                    run["next_case_index"] = index
                    run["ended_at"] = ""
                    self.db.save_run(run)
                    if progress:
                        progress(96, "Human action required. The run is safely paused.")
                    return run

                index += 1
                run["next_case_index"] = index
                run["current_result_index"] = None
                run["pending_human_action"] = None
                self.db.save_run(run)

        except Exception as exc:
            run_error = exc
            run["status"] = "error"
            run["error"] = str(exc)
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    pass

        if run_error is not None:
            run["ended_at"] = utc_now()
            run["duration_ms"] = int(run.get("active_duration_ms", 0) or 0)
            self._recount(run)
            self.db.save_run(run)
            raise run_error

        run["ended_at"] = utc_now()
        run["duration_ms"] = int(run.get("active_duration_ms", 0) or 0)
        self._recount(run)
        run["status"] = (
            "completed"
            if run["failed_count"] == 0 and run["blocked_count"] == 0 and run["error_count"] == 0
            else "completed_with_issues"
        )
        run["pending_human_action"] = None
        run["current_result_index"] = None
        self.db.save_run(run)
        if progress:
            progress(96, f"{run['display_id']} completed.")
        return run

    def execute_case(
        self,
        project: Dict[str, Any],
        suite: Dict[str, Any],
        raw_case: Dict[str, Any],
        run_id: str,
        adapter: TargetAdapter | None = None,
        existing_result: Dict[str, Any] | None = None,
        human_resolution: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        resumed = existing_result is not None
        started_segment = time.perf_counter()
        previous_duration = int((existing_result or {}).get("duration_ms", 0) or 0)
        owns_adapter = adapter is None
        active_adapter = adapter
        diagnosis: Dict[str, Any] = {}
        evaluation: Dict[str, Any] = {}
        blocked_reason = ""
        pending_human_action: Dict[str, Any] | None = None

        if resumed:
            execution_state = copy.deepcopy((existing_result or {}).get("execution_state") or {})
            case = copy.deepcopy(execution_state.get("resolved_case") or raw_case)
            usage = copy.deepcopy((existing_result or {}).get("ai_usage") or empty_usage())
            conversation = copy.deepcopy((existing_result or {}).get("conversation") or {})
            session_id = str(conversation.get("session_id") or "")
            if not session_id:
                raise ValueError("Paused test is missing its conversation session id.")
            conversation.setdefault("turns", [])
            conversation.setdefault("human_actions", [])
            conversation["status"] = "running"
            conversation["stop_reason"] = ""
            conversation["error"] = ""
            conversation["ended_at"] = ""
            if human_resolution:
                conversation["human_actions"].append(
                    {
                        "action": copy.deepcopy(human_resolution.get("action") or {}),
                        "status": "completed" if human_resolution.get("completed") else "not_completed",
                        "note": str(human_resolution.get("note") or "").strip(),
                        "completed_at": utc_now(),
                    }
                )
        else:
            case = copy.deepcopy(raw_case)
            usage = empty_usage()
            session_id = f"qa-{uuid.uuid4().hex[:16]}"
            conversation = {
                "status": "running",
                "stop_reason": "",
                "error": "",
                "session_id": session_id,
                "sender_identity": "",
                "turns": [],
                "human_actions": [],
                "started_at": utc_now(),
                "ended_at": "",
            }

        outcome = "error"
        try:
            fixtures = project.get("fixtures") or {}
            self._prepare_case_resources(case, fixtures)

            # Compile a stable runtime compatibility snapshot once per case.
            # Stored suites remain untouched; execution can understand both old
            # and new scenario-key conventions without changing test meaning.
            prepare_runtime = getattr(self.simulator, "prepare_case_for_execution", None)
            if callable(prepare_runtime):
                case = prepare_runtime(case)
                conversation["simulator_runtime_version"] = str(
                    case.get("_simulator_runtime_version") or ""
                )
                conversation["runtime_fact_snapshot"] = copy.deepcopy(
                    case.get("_runtime_facts") or {}
                )
                if case.get("_runtime_fact_conflicts"):
                    conversation["runtime_fact_conflicts"] = copy.deepcopy(
                        case.get("_runtime_fact_conflicts") or {}
                    )

            if resumed and human_resolution and human_resolution.get("completed"):
                action_info = human_resolution.get("action") or {}
                if str(action_info.get("kind") or "") == "resource_input":
                    supplied = str(human_resolution.get("note") or "").strip()
                    if supplied:
                        resource_key = str(action_info.get("resource_key") or "human_supplied_resource").strip()
                        case.setdefault("_runtime_resources", {})[resource_key] = supplied
                        case.setdefault("_expected_resource_keys", [])
                        if resource_key not in case["_expected_resource_keys"]:
                            case["_expected_resource_keys"].append(resource_key)

            if not resumed:
                if active_adapter is None:
                    active_adapter = build_adapter(project.get("target") or {})
                active_adapter.start_case(case, run_id, session_id)
            elif active_adapter is None:
                active_adapter = build_adapter(project.get("target") or {})

            max_turns = min(
                int(case.get("max_turns") or self.config.max_conversation_turns),
                self.config.max_conversation_turns,
            )
            assistant_turn_count = sum(
                1 for turn in conversation.get("turns", []) if turn.get("role") == "assistant"
            )
            remaining_turns = max(0, max_turns - assistant_turn_count)
            assistant_history = [
                normalize_text(str(turn.get("content", "")))
                for turn in conversation.get("turns", [])
                if turn.get("role") == "assistant" and normalize_text(str(turn.get("content", "")))
            ]
            previous_assistant = assistant_history[-1] if assistant_history else ""
            repeat_streak = 0
            if previous_assistant:
                for prior in reversed(assistant_history[:-1]):
                    if prior != previous_assistant:
                        break
                    repeat_streak += 1

            for _ in range(remaining_turns):
                simulator_method = self.simulator.next_action_with_guard
                parameter_count = len(inspect.signature(simulator_method).parameters)
                if parameter_count >= 3:
                    action = simulator_method(
                        case,
                        conversation["turns"],
                        conversation.get("human_actions") or [],
                    )
                else:
                    # Compatibility with a custom/legacy simulator adapter that
                    # implements the pre-HITL two-argument method.
                    action = simulator_method(case, conversation["turns"])
                merge_usage_dict(usage, action.usage.to_dict())

                if action.requires_human:
                    pending_human_action = {
                        **copy.deepcopy(action.human_action),
                        "reason": action.reason,
                        "test_case_id": case.get("id", ""),
                        "requested_at": utc_now(),
                    }
                    conversation["status"] = "awaiting_human"
                    conversation["stop_reason"] = "human_action_required"
                    outcome = "awaiting_human"
                    evaluation = self._simple_evaluation(
                        "awaiting_human",
                        "Execution is paused until the tester completes the required external human action.",
                    )
                    break

                if action.done:
                    conversation["stop_reason"] = action.reason or "user_goal_completed"
                    break

                conversation["turns"].append(
                    {"role": "user", "content": action.message, "timestamp": utc_now(), "latency_ms": 0}
                )
                target_started = time.perf_counter()
                reply = active_adapter.send(action.message, case, run_id, session_id)
                latency_ms = int((time.perf_counter() - target_started) * 1000)
                if not conversation.get("sender_identity"):
                    conversation["sender_identity"] = str(reply.metadata.get("sender_identity") or "")
                conversation["turns"].append(
                    {
                        "role": "assistant",
                        "content": reply.text,
                        "timestamp": utc_now(),
                        "latency_ms": latency_ms,
                        "metadata": reply.metadata,
                    }
                )
                current = normalize_text(reply.text)
                if current and current == previous_assistant:
                    repeat_streak += 1
                    # One validation re-prompt can be legitimate (for example,
                    # an incomplete/un-geocodable address). Stop only when the
                    # same assistant response appears three times in a row,
                    # which is much stronger evidence of a real loop.
                    if repeat_streak >= 2:
                        conversation["stop_reason"] = "repeated_assistant_response"
                        break
                else:
                    repeat_streak = 0
                previous_assistant = current
            else:
                if outcome != "awaiting_human":
                    conversation["stop_reason"] = "maximum_turns_reached"

            if outcome != "awaiting_human":
                if not conversation.get("stop_reason"):
                    conversation["stop_reason"] = "maximum_turns_reached"
                conversation["status"] = "completed"
                evaluation = self.evaluator.evaluate(
                    case,
                    conversation,
                    suite.get("requirements", []),
                    project.get("target") or {},
                )
                merge_usage_dict(usage, evaluation.get("ai_usage", {}))
                outcome = evaluation.get("status", "error")

        except TargetBlockedError as exc:
            outcome = "blocked"
            blocked_reason = str(exc)
            conversation["status"] = "blocked"
            conversation["error"] = str(exc)
            conversation["stop_reason"] = "external_dependency_blocked"
            evaluation = self._simple_evaluation("blocked", str(exc))
        except TargetError as exc:
            outcome = "failed"
            conversation["status"] = "error"
            conversation["error"] = str(exc)
            conversation["stop_reason"] = "target_error"
            evaluation = self._simple_evaluation("failed", str(exc))
        except Exception as exc:
            outcome = "error"
            conversation["status"] = "error"
            conversation["error"] = str(exc)
            conversation["stop_reason"] = "execution_error"
            evaluation = self._simple_evaluation("error", str(exc))
        finally:
            if outcome != "awaiting_human":
                conversation["ended_at"] = utc_now()
            if owns_adapter and active_adapter is not None:
                try:
                    active_adapter.close()
                except Exception:
                    pass

        if outcome == "failed":
            diagnosis = self._diagnose_safely(
                project,
                case,
                conversation,
                evaluation,
                suite.get("requirements", []),
            )
            merge_usage_dict(usage, diagnosis.get("ai_usage", {}))

        duration_ms = previous_duration + int((time.perf_counter() - started_segment) * 1000)
        result = {
            "test_case_id": case["id"],
            "title": case["title"],
            "feature": case.get("feature", ""),
            "priority": case.get("priority", "Medium"),
            "test_type": case.get("test_type", ""),
            "requirement_ids": list(case.get("requirement_ids", [])),
            "test_case_snapshot": {
                "version": int(case.get("version", 1) or 1),
                "persona": case.get("persona", ""),
                "user_goal": case.get("user_goal", ""),
                "state_mode": case.get("state_mode", "fresh_user"),
                "disclosure_style": case.get("disclosure_style", "progressive"),
                "scenario_data": case.get("scenario_data", {}),
                "runtime_facts": copy.deepcopy(case.get("_runtime_facts") or {}),
                "simulator_runtime_version": str(case.get("_simulator_runtime_version") or ""),
                "required_fixture_keys": case.get("required_fixture_keys", []),
                "preconditions": case.get("preconditions", ""),
                "objectives": case.get("objectives", []),
                "expected_result": case.get("expected_result", ""),
                "rule_assertions": case.get("rule_assertions", {}),
            },
            "outcome": outcome,
            "passed": outcome == "passed",
            "score": float(evaluation.get("score", 0.0) or 0.0),
            "duration_ms": duration_ms,
            "blocked_reason": blocked_reason,
            "conversation": conversation,
            "evaluation": evaluation,
            "diagnosis": diagnosis,
            "ai_usage": usage,
        }
        if outcome == "awaiting_human":
            result["pending_human_action"] = pending_human_action
            result["execution_state"] = {
                "resolved_case": self._persistable_case(case),
            }
        return result

    def improve_test_case(
        self,
        suite_id: str,
        test_case_id: str,
        reviewer_note: str,
        progress=None,
    ) -> Dict[str, Any]:
        note = reviewer_note.strip()
        if not note:
            raise ValueError("Reviewer instructions are required for AI revision.")
        suite = self.db.get_suite(suite_id)
        if not suite:
            raise ValueError("Suite not found.")
        project = self._project(suite["project_id"])
        case = next((item for item in suite.get("test_cases", []) if item.get("id") == test_case_id), None)
        if not case:
            raise ValueError("Test case not found.")

        if progress:
            progress(18, "Retrieving supporting requirements...")
        query = f"{case['title']} {case.get('user_goal','')} {' '.join(case.get('objectives', []))} {note}"
        context, refs = self.documents.retrieve(project["id"], query, top_k=10)
        req_map = {item["id"]: item for item in suite.get("requirements", [])}
        relevant = [req_map[rid] for rid in case.get("requirement_ids", []) if rid in req_map]
        fixture_keys = sorted(str(key) for key in (project.get("fixtures") or {}).keys())

        if progress:
            progress(48, "Improving the selected test with AI...")
        result = self.ai.structured(
            model=self.config.generation_model,
            system=(
                "Revise exactly one production QA test case. Preserve its ID and requirement mappings. Change only what "
                "the reviewer requested or what supporting evidence justifies. Keep user goal separate from expected "
                "system behavior. Normal human cases use progressive disclosure. Never invent a URL, ICS/calendar link, "
                "OAuth/invite link, external account identifier, token, OTP or code that must actually work. Use a supplied "
                "{FIXTURE:key} resource or declare a required fixture key. Missing real resources are resolved lazily only "
                "when the application asks for them. Semantic behaviors must not be encoded as literal "
                "substring assertions."
            ),
            user=(
                f"REVIEWER INSTRUCTIONS\n{note}\n\n"
                f"CURRENT CASE\n{case}\n\n"
                f"MAPPED REQUIREMENTS\n{relevant}\n\n"
                f"AVAILABLE TEST RESOURCE KEYS\n{fixture_keys}\n\n"
                f"SUPPORTING EVIDENCE\n{context}"
            ),
            schema_name="revised_test_case",
            schema=self._revision_schema(),
        )

        previous_snapshot = {
            "version": int(case.get("version", 1)),
            "reviewer_note": note,
            "title": case.get("title", ""),
            "priority": case.get("priority", "Medium"),
            "test_type": case.get("test_type", ""),
            "persona": case.get("persona", ""),
            "user_goal": case.get("user_goal", ""),
            "state_mode": case.get("state_mode", "fresh_user"),
            "disclosure_style": case.get("disclosure_style", "progressive"),
            "scenario_data": copy.deepcopy(case.get("scenario_data", {})),
            "required_fixture_keys": copy.deepcopy(case.get("required_fixture_keys", [])),
            "objectives": copy.deepcopy(case.get("objectives", [])),
            "expected_result": case.get("expected_result", ""),
            "rule_assertions": copy.deepcopy(case.get("rule_assertions", {})),
            "saved_at": utc_now(),
        }
        history = list(case.get("revision_history") or [])[-9:]
        history.append(previous_snapshot)

        value = result.value
        revised = dict(case)
        revised.update(value)
        raw_scenario = {
            str(item["key"]): str(item["value"])
            for item in value.get("scenario_data", [])
            if str(item.get("key") or "").strip()
        }
        fixture_value_to_key = {
            str(resource_value): str(resource_key)
            for resource_key, resource_value in (project.get("fixtures") or {}).items()
            if isinstance(resource_value, (str, int, float))
        }
        required_fixture_keys = {
            str(key).strip()
            for key in value.get("required_fixture_keys", []) or []
            if str(key).strip()
        }
        normalized_scenario: Dict[str, str] = {}
        for key, raw_value in raw_scenario.items():
            normalized_value, resource_key = self.generator._normalize_resource_value(
                key,
                raw_value,
                fixture_value_to_key,
            )
            normalized_scenario[key] = normalized_value
            if resource_key:
                required_fixture_keys.add(resource_key)

        revised["scenario_data"] = normalized_scenario
        revised["required_fixture_keys"] = sorted(required_fixture_keys)
        disclosure_style = str(value.get("disclosure_style") or "progressive")
        revised["disclosure_style"] = (
            disclosure_style if disclosure_style in DISCLOSURE_STYLES else "progressive"
        )
        revised["version"] = int(case.get("version", 1)) + 1
        revised["review_status"] = "draft"
        revised["approved"] = False
        revised["review_note"] = note
        revised["source_refs"] = refs
        revised["revision_history"] = history

        suite["test_cases"] = [
            revised if item.get("id") == test_case_id else item
            for item in suite.get("test_cases", [])
        ]
        all_required = sorted(
            {
                key
                for item in suite.get("test_cases", [])
                for key in item.get("required_fixture_keys", []) or []
                if str(key).strip()
            }
        )
        fixtures = project.get("fixtures") or {}
        suite.setdefault("generation_summary", {})["required_fixture_keys"] = all_required
        suite["generation_summary"]["missing_fixture_keys"] = [key for key in all_required if key not in fixtures]
        suite["approved"] = False
        suite["status"] = "draft"
        self.db.save_suite(suite)
        self.db.save_usage(
            f"revision:{suite_id}:{test_case_id}:v{revised['version']}",
            suite["project_id"],
            "test_revision",
            result.usage.to_dict(),
        )
        if progress:
            progress(95, "Revised test is ready for review.")
        return suite

    def _revision_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                "test_type": {"type": "string", "enum": TEST_TYPES},
                "risk_tags": {"type": "array", "items": {"type": "string"}},
                "preconditions": {"type": "string"},
                "persona": {"type": "string"},
                "user_goal": {"type": "string"},
                "state_mode": {"type": "string", "enum": STATE_MODES},
                "disclosure_style": {"type": "string", "enum": DISCLOSURE_STYLES},
                "scenario_data": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                        "required": ["key", "value"],
                    },
                },
                "required_fixture_keys": {"type": "array", "items": {"type": "string"}},
                "objectives": {"type": "array", "items": {"type": "string"}},
                "initial_message_hint": {"type": "string"},
                "expected_result": {"type": "string"},
                "max_turns": {"type": "integer", "minimum": 2, "maximum": 40},
                "rule_assertions": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "literal_required_any": {"type": "array", "items": {"type": "string"}},
                        "literal_required_all": {"type": "array", "items": {"type": "string"}},
                        "literal_forbidden": {"type": "array", "items": {"type": "string"}},
                        "final_response_regex": {"type": "string"},
                        "enforce_final_response_regex": {"type": "boolean"},
                        "max_assistant_chars": {"type": "integer"},
                        "min_user_turns": {"type": "integer"},
                        "enforce_min_user_turns": {"type": "boolean"},
                        "max_response_ms": {"type": "integer"},
                    },
                    "required": [
                        "literal_required_any",
                        "literal_required_all",
                        "literal_forbidden",
                        "final_response_regex",
                        "enforce_final_response_regex",
                        "max_assistant_chars",
                        "min_user_turns",
                        "enforce_min_user_turns",
                        "max_response_ms",
                    ],
                },
            },
            "required": [
                "title",
                "priority",
                "test_type",
                "risk_tags",
                "preconditions",
                "persona",
                "user_goal",
                "state_mode",
                "disclosure_style",
                "scenario_data",
                "required_fixture_keys",
                "objectives",
                "initial_message_hint",
                "expected_result",
                "max_turns",
                "rule_assertions",
            ],
        }

    def _simple_evaluation(self, status: str, summary: str) -> Dict[str, Any]:
        return {
            "status": status,
            "passed": False,
            "score": 0.0,
            "summary": summary,
            "rule_checks": [],
            "performance": {
                "status": "not_measured",
                "blocking": False,
                "message": "Performance was not evaluated for this outcome.",
                "average_ms": 0,
                "p95_ms": 0,
                "max_ms": 0,
                "turn_count": 0,
                "warning_count": 0,
                "critical_count": 0,
                "failed_count": 0,
                "thresholds": {},
                "documented_target_ms": 0,
                "documented_target_exceeded_count": 0,
                "documented_sla_enforced": False,
            },
            "semantic": None,
            "evaluation_error": "",
            "duration_ms": 0,
            "ai_usage": empty_usage(),
        }

    def _diagnose_safely(
        self,
        project: Dict[str, Any],
        case: Dict[str, Any],
        conversation: Dict[str, Any],
        evaluation: Dict[str, Any],
        requirements: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        try:
            return self.diagnoser.diagnose(project, case, conversation, evaluation, requirements)
        except Exception as exc:
            return {
                "failure_category": "Unclassified",
                "observed_problem": evaluation.get("summary") or conversation.get("error") or "Test failed.",
                "evidence": [],
                "likely_causes": [],
                "recommended_checks": [
                    "Review the conversation and target logs; automated diagnosis was unavailable."
                ],
                "suspected_components": [],
                "workflow_evidence_available": False,
                "confidence": "Low",
                "diagnosis_error": str(exc),
                "ai_usage": {},
            }

    def _prepare_case_resources(self, case: Dict[str, Any], fixtures: Dict[str, Any]) -> None:
        """Attach tester-controlled resources lazily for the active conversation.

        Missing resources must not block the case before the application asks
        for them. This lets onboarding progress naturally. If the target later
        requests a missing real URL/code, the simulator pauses exactly at that
        turn and asks the tester. Existing project resources are available to
        the simulator immediately without requiring suite regeneration.
        """

        runtime_resources = {
            str(key): value
            for key, value in (fixtures or {}).items()
            if str(key).strip() and value is not None and value != ""
        }
        fixture_values = {str(value) for value in runtime_resources.values()}
        expected_keys = {
            str(key).strip()
            for key in case.get("required_fixture_keys", []) or []
            if str(key).strip()
        }
        missing_keys: set[str] = set()
        clean_scenario: Dict[str, Any] = {}

        for key, value in (case.get("scenario_data") or {}).items():
            text = str(value or "").strip() if isinstance(value, str) else value
            if isinstance(text, str):
                fixture_match = FIXTURE_PATTERN.match(text)
                if fixture_match:
                    fixture_key = fixture_match.group(1)
                    expected_keys.add(fixture_key)
                    if fixture_key in runtime_resources:
                        clean_scenario[key] = runtime_resources[fixture_key]
                    else:
                        missing_keys.add(fixture_key)
                    continue

                # Legacy suites may contain an AI-invented URL inline. Do not
                # send or trust it. Treat the field as an expected real resource
                # and wait until the product actually asks for that value.
                if URL_PATTERN.search(text) and RESOURCE_KEY_PATTERN.search(str(key)) and text not in fixture_values:
                    expected_keys.add(str(key))
                    missing_keys.add(str(key))
                    continue

            clean_scenario[key] = value

        for key in expected_keys:
            if key not in runtime_resources:
                missing_keys.add(key)

        case["scenario_data"] = clean_scenario
        case["_runtime_resources"] = runtime_resources
        case["_expected_resource_keys"] = sorted(expected_keys)
        case["_missing_resource_keys"] = sorted(missing_keys)

    @staticmethod
    def _persistable_case(case: Dict[str, Any]) -> Dict[str, Any]:
        # Project resources are runtime configuration. Do not duplicate them in
        # every paused run record; they are reloaded from the project on resume.
        return {
            key: copy.deepcopy(value)
            for key, value in case.items()
            if not str(key).startswith("_runtime_")
        }

    def _case_for_paused_result(self, run: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        state_case = ((result.get("execution_state") or {}).get("resolved_case") or {})
        if state_case:
            return copy.deepcopy(state_case)
        case_id = result.get("test_case_id")
        for case in run.get("execution_cases", []) or []:
            if case.get("id") == case_id:
                return copy.deepcopy(case)
        raise ValueError("Paused test case snapshot is unavailable.")

    def _mark_human_action_unavailable(
        self,
        result: Dict[str, Any],
        action: Dict[str, Any],
        note: str,
    ) -> None:
        conversation = result.setdefault("conversation", {})
        conversation.setdefault("human_actions", []).append(
            {
                "action": copy.deepcopy(action),
                "status": "not_completed",
                "note": note.strip(),
                "completed_at": utc_now(),
            }
        )
        reason = note.strip() or "The tester could not complete the required external human action."
        conversation["status"] = "blocked"
        conversation["stop_reason"] = "human_action_not_completed"
        conversation["error"] = reason
        conversation["ended_at"] = utc_now()
        result["outcome"] = "blocked"
        result["passed"] = False
        result["score"] = 0.0
        result["blocked_reason"] = reason
        result["evaluation"] = self._simple_evaluation("blocked", reason)
        result["diagnosis"] = {}
        result.pop("pending_human_action", None)
        result.pop("execution_state", None)

    def _project(self, project_id: str) -> Dict[str, Any]:
        project = self.db.get_project(project_id)
        if not project:
            raise ValueError("Project not found.")
        return project

    def _recount(self, run: Dict[str, Any]) -> None:
        results = run.get("results", [])
        run["passed_count"] = sum(1 for result in results if result.get("outcome") == "passed")
        run["failed_count"] = sum(1 for result in results if result.get("outcome") == "failed")
        run["blocked_count"] = sum(1 for result in results if result.get("outcome") == "blocked")
        run["error_count"] = sum(1 for result in results if result.get("outcome") == "error")
        completed = run["passed_count"] + run["failed_count"]
        run["pass_rate"] = round(run["passed_count"] / completed * 100, 2) if completed else 0.0


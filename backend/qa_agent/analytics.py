from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import settings
from .db import Database, database

FINAL_RUN_STATUSES = {"completed", "completed_with_issues", "error", "cancelled"}
FINAL_RESULT_OUTCOMES = {"passed", "failed", "blocked", "error", "cancelled"}
CURRENT_RESULT_OUTCOMES = {"passed", "failed", "blocked", "error"}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _percentile(values: Iterable[int], percentile: float) -> int:
    ordered = sorted(int(value) for value in values if int(value) > 0)
    if not ordered:
        return 0
    if len(ordered) == 1:
        return ordered[0]
    percentile = max(0.0, min(1.0, float(percentile)))
    index = int(round((len(ordered) - 1) * percentile))
    return ordered[index]


def _result_version(result: Dict[str, Any]) -> int:
    snapshot = result.get("test_case_snapshot") or {}
    return max(1, _as_int(snapshot.get("version"), 1))


def _run_signature(run: Dict[str, Any]) -> Tuple[Tuple[str, int], ...]:
    """Comparable-run signature.

    Two runs are only compared when they executed the same test IDs at the same
    test-case versions. This prevents a targeted one-case run from being compared
    with an unrelated regression run and prevents a revised case from being
    compared with its pre-revision result.
    """

    items: List[Tuple[str, int]] = []
    for result in run.get("results", []) or []:
        if result.get("outcome") not in FINAL_RESULT_OUTCOMES:
            continue
        test_id = str(result.get("test_case_id") or "").strip()
        if test_id:
            items.append((test_id, _result_version(result)))
    return tuple(sorted(set(items)))


def _run_summary(run: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": run.get("id", ""),
        "display_id": run.get("display_id", f"Run {run.get('run_number', '')}"),
        "passed": _as_int(run.get("passed_count")),
        "failed": _as_int(run.get("failed_count")),
        "blocked": _as_int(run.get("blocked_count")),
        "errors": _as_int(run.get("error_count")),
        "pass_rate": float(run.get("pass_rate", 0) or 0),
        "duration_ms": _as_int(run.get("duration_ms")),
    }


def project_analytics(project_id: str, db: Database = database) -> Dict[str, Any]:
    suites = db.list_suites(project_id, 100)
    all_runs = db.list_runs(project_id, 100)

    # Paused HITL runs are incomplete evidence and must not affect pass/fail trend.
    final_runs = [run for run in all_runs if run.get("status") in FINAL_RUN_STATUSES]

    approved_suites = [suite for suite in suites if suite.get("approved")]
    current_suite = approved_suites[0] if approved_suites else (suites[0] if suites else None)
    current_suite_id = str((current_suite or {}).get("id") or "")

    cases = (current_suite or {}).get("test_cases", []) or []
    active_cases = [case for case in cases if case.get("review_status") not in {"rejected", "deprecated"}]
    approved_cases = [
        case
        for case in active_cases
        if case.get("approved") and case.get("review_status") == "approved"
    ]
    case_by_id = {str(case.get("id")): case for case in approved_cases if case.get("id")}
    approved_ids = set(case_by_id)

    requirements = (current_suite or {}).get("requirements", []) or []
    requirement_ids = {str(item.get("id")) for item in requirements if item.get("id")}
    designed_requirement_ids = {
        str(requirement_id)
        for case in approved_cases
        for requirement_id in case.get("requirement_ids", []) or []
        if str(requirement_id) in requirement_ids
    }
    requirement_coverage = (
        round(len(designed_requirement_ids) / len(requirement_ids) * 100, 2)
        if requirement_ids
        else None
    )

    suite_all_runs = [run for run in all_runs if str(run.get("suite_id") or "") == current_suite_id]
    suite_final_runs = [run for run in final_runs if str(run.get("suite_id") or "") == current_suite_id]

    # list_runs is newest-first. Keep only the newest result for each currently
    # approved test. A result from an older test-case version is intentionally not
    # treated as current evidence after the reviewer revises that case.
    latest_records: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
    for run in suite_final_runs:
        for result in run.get("results", []) or []:
            test_id = str(result.get("test_case_id") or "")
            if test_id not in approved_ids or test_id in latest_records:
                continue
            if result.get("outcome") not in FINAL_RESULT_OUTCOMES:
                continue
            latest_records[test_id] = (result, run)

    current_results: Dict[str, Dict[str, Any]] = {}
    stale_test_ids: List[str] = []
    for test_id, (result, _run) in latest_records.items():
        current_case_version = max(1, _as_int(case_by_id[test_id].get("version"), 1))
        if _result_version(result) != current_case_version:
            stale_test_ids.append(test_id)
            continue
        if result.get("outcome") in CURRENT_RESULT_OUTCOMES:
            current_results[test_id] = result

    passed = sum(1 for result in current_results.values() if result.get("outcome") == "passed")
    failed = sum(1 for result in current_results.values() if result.get("outcome") == "failed")
    blocked = sum(1 for result in current_results.values() if result.get("outcome") == "blocked")
    errors = sum(1 for result in current_results.values() if result.get("outcome") == "error")
    executed = len(current_results)
    stale_count = len(stale_test_ids)
    not_run = max(0, len(approved_cases) - executed - stale_count)
    pass_fail_total = passed + failed
    pass_rate = round(passed / pass_fail_total * 100, 2) if pass_fail_total else 0.0
    execution_coverage = round(executed / len(approved_cases) * 100, 2) if approved_cases else 0.0

    exercised_requirement_ids = {
        str(requirement_id)
        for test_id in current_results
        for requirement_id in case_by_id[test_id].get("requirement_ids", []) or []
        if str(requirement_id) in requirement_ids
    }
    requirement_execution_coverage = (
        round(len(exercised_requirement_ids) / len(requirement_ids) * 100, 2)
        if requirement_ids
        else None
    )

    durations = [
        _as_int(result.get("duration_ms"))
        for result in current_results.values()
        if _as_int(result.get("duration_ms")) > 0
    ]
    latencies = [
        _as_int(turn.get("latency_ms"))
        for result in current_results.values()
        for turn in (result.get("conversation") or {}).get("turns", []) or []
        if turn.get("role") == "assistant" and _as_int(turn.get("latency_ms")) > 0
    ]

    failure_categories = Counter()
    suspected_components = Counter()
    for result in current_results.values():
        if result.get("outcome") == "passed":
            continue
        diagnosis = result.get("diagnosis") or {}
        category = diagnosis.get("failure_category") or (
            "Blocked / Dependency" if result.get("outcome") == "blocked" else "Unclassified"
        )
        failure_categories[str(category)] += 1
        if diagnosis.get("workflow_evidence_available"):
            for component in diagnosis.get("suspected_components", []) or []:
                suspected_components[str(component)] += 1

    priorities = Counter(case.get("priority", "Unknown") for case in approved_cases)
    types = Counter(case.get("test_type", "Unclassified") for case in approved_cases)
    high_priority_failures = sum(
        1
        for test_id, result in current_results.items()
        if result.get("outcome") in {"failed", "error"}
        and case_by_id[test_id].get("priority", "Medium") == "High"
    )

    performance_states = Counter()
    for result in current_results.values():
        performance = (result.get("evaluation") or {}).get("performance") or {}
        status = str(performance.get("status") or "not_measured")
        if status != "not_measured":
            performance_states[status] += 1

    usage = db.aggregate_usage(project_id)

    trend = [
        {
            "id": run["id"],
            "display_id": run.get("display_id", f"Run {run.get('run_number', '')}"),
            "started_at": run.get("started_at", ""),
            "passed": run.get("passed_count", 0),
            "failed": run.get("failed_count", 0),
            "blocked": run.get("blocked_count", 0),
            "errors": run.get("error_count", 0),
            "pass_rate": run.get("pass_rate", 0),
            "case_count": len(_run_signature(run)),
        }
        for run in reversed(suite_final_runs[:12])
    ]

    comparison = None
    comparison_note = "A comparable previous run appears after the same case set is executed more than once."
    if suite_final_runs:
        current_run = suite_final_runs[0]
        current_signature = _run_signature(current_run)
        if current_signature:
            previous = next(
                (
                    run
                    for run in suite_final_runs[1:]
                    if _run_signature(run) == current_signature
                ),
                None,
            )
            baseline = next(
                (
                    run
                    for run in suite_final_runs
                    if run.get("is_baseline")
                    and run.get("id") != current_run.get("id")
                    and _run_signature(run) == current_signature
                ),
                None,
            )
            if previous:
                comparison = {
                    "current": _run_summary(current_run),
                    "previous": _run_summary(previous),
                    "baseline": _run_summary(baseline) if baseline else None,
                    "case_count": len(current_signature),
                    "pass_rate_delta": round(
                        float(current_run.get("pass_rate", 0) or 0)
                        - float(previous.get("pass_rate", 0) or 0),
                        2,
                    ),
                }
                comparison_note = "Compared only with the most recent run of the same test IDs and versions."
            else:
                comparison_note = "No earlier completed run used the same test IDs and versions as the latest run."

    validation_status = [
        {"name": "Passed", "value": passed},
        {"name": "Failed", "value": failed},
        {"name": "Blocked", "value": blocked},
        {"name": "Errors", "value": errors},
        {"name": "Needs retest", "value": stale_count},
        {"name": "Not run", "value": not_run},
    ]

    performance_status = [
        {"name": name.title(), "value": performance_states.get(name, 0)}
        for name in ("healthy", "warning", "critical", "failed")
        if performance_states.get(name, 0)
    ]

    current_suite_completed_runs = len(suite_final_runs)
    current_suite_total_runs = len(suite_all_runs)
    current_suite_waiting_runs = sum(1 for run in suite_all_runs if run.get("status") == "awaiting_human")

    return {
        "scope": {
            "suite_id": current_suite_id,
            "suite_name": (current_suite or {}).get("name", ""),
            "suite_version": _as_int((current_suite or {}).get("version"), 0),
            "active_test_cases": len(active_cases),
            "approved_test_cases": len(approved_cases),
        },
        "summary": {
            # Backward-compatible field. It now explicitly means runnable/approved tests.
            "total_test_cases": len(approved_cases),
            "active_test_cases": len(active_cases),
            "approved_test_cases": len(approved_cases),
            "executed_tests": executed,
            "untested_tests": not_run,
            "needs_retest_tests": stale_count,
            "passed_tests": passed,
            "failed_tests": failed,
            "blocked_tests": blocked,
            "error_tests": errors,
            "pass_rate": pass_rate,
            "execution_coverage": execution_coverage,
            "requirement_coverage": requirement_coverage,
            "requirement_execution_coverage": requirement_execution_coverage,
            "workflow_node_coverage": None,
            "branch_coverage": None,
            "average_execution_ms": round(sum(durations) / len(durations), 2) if durations else 0,
            "total_test_runs": db.count_runs(project_id),
            "current_suite_runs": current_suite_total_runs,
            "completed_runs": current_suite_completed_runs,
            "awaiting_human_runs": current_suite_waiting_runs,
            "api_response_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0,
            "p95_api_response_ms": _percentile(latencies, 0.95),
            "ai_token_usage": usage["total_tokens"],
            "estimated_test_cost": usage["cost_usd"],
            "pricing_configured": bool(settings.model_pricing),
            "high_risk_issues_count": high_priority_failures,
            "high_priority_failures": high_priority_failures,
            "most_failed_workflow_node": None,
        },
        "failure_categories": [
            {"name": key, "value": value}
            for key, value in failure_categories.most_common(8)
        ],
        # Kept for API compatibility; the Overview intentionally no longer renders this card.
        "tests_by_priority": [
            {"name": key, "value": value}
            for key, value in priorities.most_common()
        ],
        "tests_by_type": [
            {"name": key, "value": value}
            for key, value in types.most_common(10)
        ],
        "validation_status": validation_status,
        "performance_status": performance_status,
        "trend": trend,
        "current_vs_previous": comparison,
        "comparison_note": comparison_note,
        "ai_usage": usage,
        "coverage": {
            "total": len(requirement_ids),
            "covered": len(designed_requirement_ids),
            "uncovered": max(0, len(requirement_ids) - len(designed_requirement_ids)),
            "executed": len(exercised_requirement_ids),
            "not_executed": max(0, len(requirement_ids) - len(exercised_requirement_ids)),
        },
        "suspected_failure_areas": [
            {"name": key, "value": value}
            for key, value in suspected_components.most_common(5)
        ],
    }

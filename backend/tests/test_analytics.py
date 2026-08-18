from pathlib import Path

from qa_agent.analytics import project_analytics
from qa_agent.db import Database


def _result(test_id: str, outcome: str, version: int, requirement_id: str, duration_ms: int = 1000):
    return {
        "test_case_id": test_id,
        "title": test_id,
        "priority": "High" if test_id == "TC-1" else "Medium",
        "test_type": "happy-path",
        "requirement_ids": [requirement_id],
        "test_case_snapshot": {"version": version},
        "outcome": outcome,
        "duration_ms": duration_ms,
        "conversation": {
            "turns": [
                {"role": "assistant", "latency_ms": 5000},
                {"role": "assistant", "latency_ms": 10000},
            ]
        },
        "evaluation": {
            "performance": {
                "status": "critical" if outcome != "passed" else "warning",
            }
        },
        "diagnosis": {
            "failure_category": "Conversation Progression Failure" if outcome == "failed" else "",
            "workflow_evidence_available": False,
        },
    }


def _run(run_number: int, results, status="completed", pass_rate=0, is_baseline=False):
    passed = sum(1 for item in results if item["outcome"] == "passed")
    failed = sum(1 for item in results if item["outcome"] == "failed")
    blocked = sum(1 for item in results if item["outcome"] == "blocked")
    errors = sum(1 for item in results if item["outcome"] == "error")
    return {
        "id": f"run-{run_number}",
        "project_id": "project-1",
        "run_number": run_number,
        "display_id": f"Run {run_number}",
        "suite_id": "suite-1",
        "suite_name": "Current suite",
        "status": status,
        "is_baseline": is_baseline,
        "started_at": f"2026-08-0{run_number}T00:00:00+00:00",
        "ended_at": f"2026-08-0{run_number}T00:01:00+00:00",
        "duration_ms": 60000,
        "results": results,
        "passed_count": passed,
        "failed_count": failed,
        "blocked_count": blocked,
        "error_count": errors,
        "pass_rate": pass_rate,
        "ai_usage": {},
    }


def test_analytics_scopes_current_suite_tracks_execution_and_stale_revisions(tmp_path: Path):
    db = Database(tmp_path / "qa.db")
    db.initialize()
    db.save_project(
        {
            "id": "project-1",
            "name": "Demo",
            "slug": "demo",
            "status": "active",
            "target": {"adapter": "mock"},
            "fixtures": {},
        }
    )
    db.save_suite(
        {
            "id": "suite-1",
            "project_id": "project-1",
            "version": 1,
            "name": "Current suite",
            "feature": "Full product",
            "status": "approved",
            "approved": True,
            "requirements": [
                {"id": "REQ-1", "title": "One"},
                {"id": "REQ-2", "title": "Two"},
            ],
            "test_cases": [
                {
                    "id": "TC-1",
                    "version": 1,
                    "priority": "High",
                    "test_type": "happy-path",
                    "requirement_ids": ["REQ-1"],
                    "review_status": "approved",
                    "approved": True,
                },
                {
                    "id": "TC-2",
                    "version": 2,
                    "priority": "Medium",
                    "test_type": "validation",
                    "requirement_ids": ["REQ-2"],
                    "review_status": "approved",
                    "approved": True,
                },
                {
                    "id": "TC-3",
                    "version": 1,
                    "priority": "Low",
                    "test_type": "negative",
                    "requirement_ids": ["REQ-2"],
                    "review_status": "draft",
                    "approved": False,
                },
            ],
        }
    )

    # Older result for TC-2 is version 1. Since the current approved case is now
    # version 2, that historical outcome must be classified as needs-retest.
    db.save_run(_run(1, [
        _result("TC-1", "failed", 1, "REQ-1"),
        _result("TC-2", "passed", 1, "REQ-2"),
    ], pass_rate=50))
    db.save_run(_run(2, [_result("TC-1", "failed", 1, "REQ-1")], pass_rate=0))
    db.save_run(_run(3, [_result("TC-1", "passed", 1, "REQ-1")], pass_rate=100))
    db.save_run(_run(4, [], status="awaiting_human", pass_rate=0))

    data = project_analytics("project-1", db)
    summary = data["summary"]

    assert summary["active_test_cases"] == 3
    assert summary["approved_test_cases"] == 2
    assert summary["executed_tests"] == 1
    assert summary["needs_retest_tests"] == 1
    assert summary["untested_tests"] == 0
    assert summary["execution_coverage"] == 50.0
    assert summary["passed_tests"] == 1
    assert summary["failed_tests"] == 0
    assert summary["pass_rate"] == 100.0
    assert summary["requirement_coverage"] == 100.0
    assert summary["requirement_execution_coverage"] == 50.0
    assert summary["completed_runs"] == 3
    assert summary["current_suite_runs"] == 4
    assert summary["awaiting_human_runs"] == 1
    assert summary["api_response_ms"] == 7500.0
    assert summary["p95_api_response_ms"] == 10000

    status = {item["name"]: item["value"] for item in data["validation_status"]}
    assert status["Passed"] == 1
    assert status["Needs retest"] == 1

    # Run 3 and Run 2 executed the same test ID/version, so they are comparable.
    assert data["current_vs_previous"]["current"]["display_id"] == "Run 3"
    assert data["current_vs_previous"]["previous"]["display_id"] == "Run 2"
    assert data["current_vs_previous"]["pass_rate_delta"] == 100.0
    assert all(item["display_id"] != "Run 4" for item in data["trend"])

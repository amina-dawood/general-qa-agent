from qa_agent.db import Database


def _project(project_id: str):
    return {
        "id": project_id,
        "name": "Delete Run Test",
        "slug": "delete-run-test",
        "status": "active",
        "target": {"adapter": "mock"},
        "fixtures": {},
        "workflow": None,
    }


def _run(project_id: str, run_id: str, number: int, tokens: int):
    return {
        "id": run_id,
        "project_id": project_id,
        "run_number": number,
        "display_id": f"Run {number}",
        "suite_id": None,
        "suite_name": "Suite",
        "status": "completed",
        "passed_count": 1,
        "failed_count": 0,
        "blocked_count": 0,
        "error_count": 0,
        "pass_rate": 100,
        "duration_ms": 1000,
        "started_at": "2026-08-12T00:00:00+00:00",
        "ended_at": "2026-08-12T00:00:01+00:00",
        "is_baseline": False,
        "results": [],
        "ai_usage": {"requests": 1, "total_tokens": tokens},
    }


def test_delete_run_removes_run_usage_and_preserves_monotonic_numbers(tmp_path):
    db = Database(tmp_path / "qa.db")
    project_id = "project-delete-test"
    db.save_project(_project(project_id))

    first_number = db.next_run_number(project_id)
    second_number = db.next_run_number(project_id)
    assert (first_number, second_number) == (1, 2)

    db.save_run(_run(project_id, "run-one", first_number, 100))
    db.save_run(_run(project_id, "run-two", second_number, 200))
    assert db.count_runs(project_id) == 2
    assert db.aggregate_usage(project_id)["total_tokens"] == 300

    assert db.delete_run("run-one") is True
    assert db.get_run("run-one") is None
    assert db.count_runs(project_id) == 1
    assert db.aggregate_usage(project_id)["total_tokens"] == 200

    # Deleted friendly IDs are not recycled. This keeps historical references stable.
    assert db.next_run_number(project_id) == 3
    assert db.delete_run("missing-run") is False

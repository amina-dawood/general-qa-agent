from pathlib import Path

from qa_agent.ai_client import AIResult, AIUsage
from qa_agent.db import Database
from qa_agent.generator import TestGenerator
from qa_agent.simulator import UserSimulator


class FakeConfig:
    generation_model = "fake-gen"
    simulation_model = "fake-sim"
    max_generated_cases = 8
    max_conversation_turns = 12
    max_prompt_chars = 12000


class FakeDocs:
    def retrieve_many(self, project_id, queries, top_k=None):
        return (
            "The assistant must collect a user's preferred name and confirm completion.",
            [{"document": "spec.txt", "chunk_index": 0}],
        )


class FakeGenerationAI:
    def __init__(self):
        self.calls = 0

    def structured(self, **kwargs):
        self.calls += 1
        usage = AIUsage(
            requests=1,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            models={kwargs["model"]: 1},
        )
        if self.calls == 1:
            return AIResult(
                {
                    "suite_name": "Profile suite",
                    "requirements": [
                        {
                            "title": "Collect name",
                            "description": "Collect the user's preferred name and confirm it.",
                            "risk": "High",
                            "acceptance_criteria": ["Name is accepted", "Completion is confirmed"],
                        }
                    ],
                    "risk_areas": ["missing name"],
                    "applicable_test_types": ["happy-path", "validation"],
                    "documentation_conflicts": [],
                },
                usage,
                kwargs["model"],
            )
        if self.calls == 2:
            return AIResult(
                {
                    "suite_name": "Profile suite",
                    "test_cases": [
                        {
                            "title": "User completes profile naturally",
                            "priority": "High",
                            "test_type": "happy-path",
                            "requirement_ids": ["REQ-PROF-001"],
                            "risk_tags": ["completion"],
                            "preconditions": "New user",
                            "persona": "A concise first-time user",
                            "user_goal": "Finish profile setup using a preferred name.",
                            "state_mode": "fresh_user",
                            "scenario_data": [{"key": "preferred_name", "value": "Jordan"}],
                            "objectives": ["Provide the requested preferred name"],
                            "initial_message_hint": "Open the conversation naturally",
                            "expected_result": "The assistant accepts the name and confirms completion.",
                            "max_turns": 8,
                            "rule_assertions": {
                                "required_any": [],
                                "required_all": [],
                                "forbidden": [],
                                "final_response_regex": "",
                                "max_assistant_chars": 1500,
                                "min_user_turns": 1,
                                "max_response_ms": 0,
                            },
                        }
                    ],
                },
                usage,
                kwargs["model"],
            )
        return AIResult(
            {"missing_cases": [], "duplicate_case_titles": [], "notes": []},
            usage,
            kwargs["model"],
        )


class CapturingSimulationAI:
    def __init__(self):
        self.last_user_prompt = ""

    def structured(self, **kwargs):
        self.last_user_prompt = kwargs["user"]
        return AIResult(
            {"message": "Hey, I’m trying to get set up.", "done": False, "reason": ""},
            AIUsage(requests=1, total_tokens=10, models={kwargs["model"]: 1}),
            kwargs["model"],
        )


def test_database_run_numbers_are_short_and_sequential(tmp_path: Path):
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
    assert db.next_run_number("project-1") == 1
    db.save_run(
        {
            "id": "run-uuid",
            "project_id": "project-1",
            "run_number": 1,
            "display_id": "Run 1",
            "suite_id": "suite-1",
            "status": "completed",
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:00:01+00:00",
            "results": [],
            "passed_count": 0,
            "failed_count": 0,
            "blocked_count": 0,
            "error_count": 0,
            "pass_rate": 0,
            "duration_ms": 1000,
            "ai_usage": {},
        }
    )
    assert db.next_run_number("project-1") == 2


def test_generator_uses_three_bounded_ai_stages_and_maps_requirements():
    ai = FakeGenerationAI()
    generator = TestGenerator(FakeDocs(), ai=ai, config=FakeConfig())
    suite, usage = generator.generate(
        {"id": "project-1", "name": "Demo", "fixtures": {}},
        "profile",
        "profile setup",
    )
    assert ai.calls == 3
    assert suite["requirements"][0]["id"] == "REQ-PROF-001"
    case = suite["test_cases"][0]
    assert case["requirement_ids"] == ["REQ-PROF-001"]
    assert case["scenario_data"]["preferred_name"] == "Jordan"
    assert case["user_goal"] == "Finish profile setup using a preferred name."
    assert case["state_mode"] == "fresh_user"
    assert usage["requests"] == 3


def test_user_simulator_is_ai_generated_and_blind_to_expected_result():
    ai = CapturingSimulationAI()
    simulator = UserSimulator(ai=ai, config=FakeConfig())
    action = simulator.next_action(
        {
            "title": "Setup",
            "persona": "A busy user",
            "user_goal": "Get set up",
            "state_mode": "fresh_user",
            "scenario_data": {"name": "Jordan"},
            "objectives": ["Set up"],
            "expected_result": "SECRET_EXPECTED_QA_OUTCOME",
            "initial_message_hint": "Ask for help",
        },
        [],
    )
    assert action.message == "Hey, I’m trying to get set up."
    assert "SECRET_EXPECTED_QA_OUTCOME" not in ai.last_user_prompt
    assert "User goal: Get set up" in ai.last_user_prompt

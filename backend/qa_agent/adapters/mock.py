from __future__ import annotations

from typing import Any, Dict

from .base import TargetReply


class MockAdapter:
    """Deterministic adapter for installation verification only."""

    def start_case(self, test_case: Dict[str, Any], run_id: str, session_id: str) -> None:
        return None

    def close(self) -> None:
        return None

    def send(self, message: str, test_case: Dict[str, Any], run_id: str, session_id: str) -> TargetReply:
        count = int(test_case.setdefault("_mock_turn", 0))
        test_case["_mock_turn"] = count + 1
        if count == 0:
            return TargetReply("Thanks. What information would you like to provide next?", {"mock": True})
        return TargetReply("Thanks, I have that. Your request is complete.", {"mock": True})

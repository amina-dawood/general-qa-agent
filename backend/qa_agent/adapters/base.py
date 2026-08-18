from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Protocol


class TargetError(RuntimeError):
    pass


class TargetBlockedError(TargetError):
    """External/configuration condition prevented meaningful test execution."""


@dataclass
class TargetReply:
    text: str
    metadata: Dict[str, Any]


class TargetAdapter(Protocol):
    def start_case(self, test_case: Dict[str, Any], run_id: str, session_id: str) -> None: ...
    def send(self, message: str, test_case: Dict[str, Any], run_id: str, session_id: str) -> TargetReply: ...
    def close(self) -> None: ...

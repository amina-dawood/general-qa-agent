from __future__ import annotations

import math
import re
from typing import Any, Dict, List

from .config import Settings, settings
from .utils import normalize_text


class RuleValidator:
    """Deterministic guardrails plus non-blocking performance classification.

    Semantic ideas such as "asked for the parent's name" are intentionally NOT
    evaluated with substring matching. Those belong to the semantic evaluator.
    Only assertions explicitly marked as literal/strict can fail a case here.
    """

    def __init__(self, config: Settings = settings):
        self.config = config

    def validate(
        self,
        test_case: Dict[str, Any],
        conversation: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        assertions = test_case.get("rule_assertions") or {}
        turns = conversation.get("turns") or []
        assistant_turns = [turn for turn in turns if turn.get("role") == "assistant"]
        assistant_text = "\n".join(str(turn.get("content", "")) for turn in assistant_turns)
        user_turns = [turn for turn in turns if turn.get("role") == "user"]
        checks: List[Dict[str, Any]] = []

        def add(
            name: str,
            passed: bool,
            message: str,
            severity: str = "error",
            evidence: str = "",
        ) -> None:
            checks.append(
                {
                    "name": name,
                    "passed": passed,
                    "message": message,
                    "severity": severity,
                    "evidence": evidence,
                }
            )

        normalized_assistant = normalize_text(assistant_text)

        # New explicit literal checks. These are safe to use as hard failures.
        literal_all = [
            str(item).strip()
            for item in assertions.get("literal_required_all", []) or []
            if str(item).strip()
        ]
        for phrase in literal_all:
            passed = normalize_text(phrase) in normalized_assistant
            add(
                f"literal_required_all:{phrase}",
                passed,
                f"Required literal text {'found' if passed else 'not found'}: {phrase}",
            )

        literal_any = [
            str(item).strip()
            for item in assertions.get("literal_required_any", []) or []
            if str(item).strip()
        ]
        if literal_any:
            passed = any(normalize_text(item) in normalized_assistant for item in literal_any)
            add(
                "literal_required_any",
                passed,
                "At least one required literal indicator was present."
                if passed
                else "None of the required literal indicators were present.",
            )

        literal_forbidden = [
            str(item).strip()
            for item in assertions.get("literal_forbidden", []) or []
            if str(item).strip()
        ]
        for phrase in literal_forbidden:
            found = normalize_text(phrase) in normalized_assistant
            add(
                f"literal_forbidden:{phrase}",
                not found,
                f"Forbidden literal text {'was found' if found else 'was not found'}: {phrase}",
            )

        # Backward compatibility for suites generated before the semantic/literal
        # separation. These values may contain concepts such as "parent name
        # requested", so they are retained as advisory evidence only and cannot
        # create false product failures.
        legacy_all = [
            str(item).strip()
            for item in assertions.get("required_all", []) or []
            if str(item).strip()
        ]
        if legacy_all:
            add(
                "legacy_required_all",
                True,
                "Legacy semantic indicators are evaluated by DeepEval, not by literal substring matching: "
                + "; ".join(legacy_all),
                severity="info",
            )

        legacy_any = [
            str(item).strip()
            for item in assertions.get("required_any", []) or []
            if str(item).strip()
        ]
        if legacy_any:
            add(
                "legacy_required_any",
                True,
                "Legacy semantic alternatives are evaluated by DeepEval, not by literal substring matching: "
                + "; ".join(legacy_any),
                severity="info",
            )

        legacy_forbidden = [
            str(item).strip()
            for item in assertions.get("forbidden", []) or []
            if str(item).strip()
        ]
        if legacy_forbidden:
            add(
                "legacy_forbidden",
                True,
                "Legacy semantic forbidden behaviors are advisory; exact forbidden wording must use literal_forbidden: "
                + "; ".join(legacy_forbidden),
                severity="info",
            )

        regex = str(assertions.get("final_response_regex") or "").strip()
        if regex:
            last = str(assistant_turns[-1].get("content", "")) if assistant_turns else ""
            try:
                matched = bool(re.search(regex, last, re.IGNORECASE | re.DOTALL))
                valid_regex = True
            except re.error:
                matched = False
                valid_regex = False

            strict = bool(assertions.get("enforce_final_response_regex", False))
            if strict:
                add(
                    "final_response_regex",
                    matched and valid_regex,
                    "Final response matched the required pattern."
                    if matched and valid_regex
                    else "Final response did not match the required pattern."
                    if valid_regex
                    else "Configured final-response regex is invalid.",
                    evidence=last,
                )
            else:
                add(
                    "final_response_regex_advisory",
                    True,
                    "Generated/legacy final-response regex is advisory unless explicitly marked strict. "
                    + ("It matched." if matched else "It did not match." if valid_regex else "The regex is invalid."),
                    severity="warning" if not matched else "info",
                    evidence=last,
                )

        min_user_turns = max(
            1,
            int(assertions.get("min_user_turns", assertions.get("min_parent_turns", 1)) or 1),
        )
        enough_turns = len(user_turns) >= min_user_turns
        min_turns_strict = bool(assertions.get("enforce_min_user_turns", False))
        add(
            "minimum_user_turns" if min_turns_strict else "minimum_user_turns_advisory",
            enough_turns if min_turns_strict else True,
            f"Observed {len(user_turns)} user turns; generated minimum is {min_user_turns}."
            + ("" if min_turns_strict else " This is advisory unless explicitly marked strict."),
            severity="error" if min_turns_strict else ("warning" if not enough_turns else "info"),
        )

        documented_response_ms = max(0, int(assertions.get("max_response_ms", 0) or 0))
        if documented_response_ms:
            exceeded = [
                turn
                for turn in assistant_turns
                if int(turn.get("latency_ms", 0) or 0) > documented_response_ms
            ]
            add(
                "response_time",
                not exceeded,
                f"{len(exceeded)} assistant response(s) exceeded the documented {documented_response_ms} ms target."
                if exceeded
                else f"Assistant responses met the documented {documented_response_ms} ms target.",
                # The operational performance policy decides whether latency is
                # blocking. This legacy/documented check is visible evidence,
                # not an automatic functional failure.
                severity="warning" if exceeded else "info",
            )

        max_chars = max(0, int(assertions.get("max_assistant_chars", 0) or 0))
        if max_chars:
            oversized = [
                turn
                for turn in assistant_turns
                if len(str(turn.get("content", ""))) > max_chars
            ]
            add(
                "assistant_length",
                True,
                f"Assistant responses respected the {max_chars}-character guideline."
                if not oversized
                else f"{len(oversized)} assistant response(s) exceeded the {max_chars}-character guideline.",
                severity="warning" if oversized else "info",
            )

        longest_repeat_run = 0
        current_run = 0
        previous = ""
        for turn in assistant_turns:
            current = normalize_text(turn.get("content"))
            if current and current == previous:
                current_run += 1
                longest_repeat_run = max(longest_repeat_run, current_run)
            else:
                current_run = 0
            previous = current

        # One exact re-prompt can be valid validation behavior. Treat it as a
        # warning, not a failure. Three identical assistant messages in a row
        # (two consecutive repeats) is strong enough to classify as a loop.
        if longest_repeat_run == 1:
            add(
                "assistant_repeat_warning",
                True,
                "The assistant repeated one response once. This can be valid after incomplete or invalid input; review context if needed.",
                severity="warning",
            )

        hard_loop = longest_repeat_run >= 2
        add(
            "no_exact_assistant_loop",
            not hard_loop,
            "No persistent exact assistant-response loop was detected."
            if not hard_loop
            else "The same assistant response appeared at least three times in a row, indicating a persistent loop.",
        )

        stop_reason = str(conversation.get("stop_reason") or "")
        if stop_reason == "maximum_turns_reached":
            add(
                "conversation_completion",
                False,
                "The user goal was not completed before the maximum conversation-turn limit was reached.",
            )
        elif stop_reason == "repeated_assistant_response":
            add(
                "conversation_progression",
                False,
                "Execution stopped because the assistant repeated the same response and the flow was no longer progressing.",
            )
        elif stop_reason == "resource_not_accepted":
            add(
                "controlled_resource_acceptance",
                False,
                "The application requested the same tester-controlled resource again after the exact configured value had already been supplied twice. Execution stopped to avoid repeatedly resending the same valid test input.",
            )

        if conversation.get("status") == "error":
            add(
                "conversation_execution",
                False,
                conversation.get("error") or "Conversation execution failed.",
            )

        return checks

    def performance_summary(
        self,
        test_case: Dict[str, Any],
        conversation: Dict[str, Any],
        target_config: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        target = target_config or {}
        latencies = sorted(
            int(turn.get("latency_ms", 0) or 0)
            for turn in (conversation.get("turns") or [])
            if turn.get("role") == "assistant" and int(turn.get("latency_ms", 0) or 0) > 0
        )

        warning_ms = self._positive_int(
            target.get("performance_warning_ms"), self.config.performance_warning_ms
        )
        critical_ms = self._positive_int(
            target.get("performance_critical_ms"), self.config.performance_critical_ms
        )
        fail_ms = self._positive_int(
            target.get("performance_fail_ms"), self.config.performance_fail_ms
        )
        critical_ms = max(warning_ms + 1, critical_ms)
        fail_ms = max(critical_ms + 1, fail_ms)

        assertions = test_case.get("rule_assertions") or {}
        documented_target_ms = max(0, int(assertions.get("max_response_ms", 0) or 0))
        enforce_documented = self._as_bool(
            target.get("enforce_documented_response_sla"),
            self.config.enforce_documented_response_sla,
        )

        if not latencies:
            return {
                "status": "not_measured",
                "blocking": False,
                "message": "No assistant response latency was recorded.",
                "average_ms": 0,
                "p95_ms": 0,
                "max_ms": 0,
                "turn_count": 0,
                "warning_count": 0,
                "critical_count": 0,
                "failed_count": 0,
                "thresholds": {
                    "warning_ms": warning_ms,
                    "critical_ms": critical_ms,
                    "fail_ms": fail_ms,
                },
                "documented_target_ms": documented_target_ms,
                "documented_target_exceeded_count": 0,
                "documented_sla_enforced": enforce_documented,
            }

        average_ms = int(round(sum(latencies) / len(latencies)))
        p95_index = max(0, min(len(latencies) - 1, math.ceil(len(latencies) * 0.95) - 1))
        p95_ms = latencies[p95_index]
        max_ms = latencies[-1]

        failed_count = sum(1 for value in latencies if value >= fail_ms)
        critical_count = sum(1 for value in latencies if critical_ms <= value < fail_ms)
        warning_count = sum(1 for value in latencies if warning_ms < value < critical_ms)
        documented_exceeded = (
            sum(1 for value in latencies if value > documented_target_ms)
            if documented_target_ms
            else 0
        )

        blocking = failed_count > 0 or (
            enforce_documented and documented_target_ms > 0 and documented_exceeded > 0
        )
        if blocking:
            status = "failed"
        elif critical_count:
            status = "critical"
        elif warning_count:
            status = "warning"
        else:
            status = "healthy"

        if blocking and failed_count:
            message = (
                f"{failed_count} response(s) reached the hard performance-failure threshold of {fail_ms} ms."
            )
        elif blocking:
            message = (
                f"The documented {documented_target_ms} ms response SLA is configured as a hard requirement and was exceeded."
            )
        elif status == "critical":
            message = (
                f"Performance is critical but non-blocking: {critical_count} response(s) were at least {critical_ms} ms."
            )
        elif status == "warning":
            message = (
                f"Performance warning: {warning_count} response(s) were slower than {warning_ms} ms."
            )
        else:
            message = f"All measured assistant responses were within {warning_ms} ms."

        if documented_target_ms and not enforce_documented:
            message += (
                f" Documented target: {documented_target_ms} ms; it is reported for visibility but is not a hard failure policy."
            )

        return {
            "status": status,
            "blocking": blocking,
            "message": message,
            "average_ms": average_ms,
            "p95_ms": p95_ms,
            "max_ms": max_ms,
            "turn_count": len(latencies),
            "warning_count": warning_count,
            "critical_count": critical_count,
            "failed_count": failed_count,
            "thresholds": {
                "warning_ms": warning_ms,
                "critical_ms": critical_ms,
                "fail_ms": fail_ms,
            },
            "documented_target_ms": documented_target_ms,
            "documented_target_exceeded_count": documented_exceeded,
            "documented_sla_enforced": enforce_documented,
        }

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return int(default)
        return parsed if parsed > 0 else int(default)

    @staticmethod
    def _as_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}


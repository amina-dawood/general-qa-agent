from qa_agent.rules import RuleValidator


def test_single_exact_assistant_reprompt_is_warning_not_hard_loop():
    case = {"rule_assertions": {"min_user_turns": 1}}
    conversation = {
        "status": "completed",
        "turns": [
            {"role": "user", "content": "I already sent it"},
            {"role": "assistant", "content": "Please send the link"},
            {"role": "user", "content": "I already sent it"},
            {"role": "assistant", "content": "Please send the link"},
        ],
    }
    checks = RuleValidator().validate(case, conversation)
    loop = next(item for item in checks if item["name"] == "no_exact_assistant_loop")
    warning = next(item for item in checks if item["name"] == "assistant_repeat_warning")
    assert loop["passed"] is True
    assert warning["severity"] == "warning"


def test_documented_response_time_is_checked_deterministically():
    case = {"rule_assertions": {"min_user_turns": 1, "max_response_ms": 1000}}
    conversation = {
        "status": "completed",
        "turns": [
            {"role": "user", "content": "help"},
            {"role": "assistant", "content": "Sure", "latency_ms": 1250},
        ],
    }
    checks = RuleValidator().validate(case, conversation)
    latency = next(item for item in checks if item["name"] == "response_time")
    assert latency["passed"] is False

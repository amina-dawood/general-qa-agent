from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List

from .ai_client import AIClient, AIUsage
from .config import Settings, settings
from .utils import normalize_text

SIMULATOR_RUNTIME_VERSION = "2026.08-compat-v2"

URL_PATTERN = re.compile(r"https?://[^\s<>\]\[\"']+", re.IGNORECASE)
EXTERNAL_RESOURCE_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:url|link|ics|webhook|invite|calendar_export|export_link|authorization_url|oauth_url|account_id|external_id|otp|token|verification_code|access_code|invite_code|auth_code)(?:$|_)",
    re.IGNORECASE,
)
TEMPLATE_PLACEHOLDER_PATTERN = re.compile(
    r"(?:\[[^\]\r\n]{1,80}\]|<[^<>\r\n]{1,80}>|\b(?:your|insert|enter|replace)\s+(?:full\s+)?(?:name|child\s+name|email|phone|address)\b)",
    re.IGNORECASE,
)
FORMAL_OPENING_PATTERN = re.compile(
    r"\b(?:onboarding process|looking forward to working together|reach out and start|dear sir|dear team|sincerely)\b",
    re.IGNORECASE,
)
ADDRESS_RESOURCE_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:address|home_address|street_address|physical_address|mailing_address|service_address|origin_address|destination_address|geocodable_address)(?:$|_)",
    re.IGNORECASE,
)
ENTRY_JOURNEY_SCOPE_PATTERN = re.compile(
    r"\b(?:onboarding|initial\s+conversation|initial\s+setup|first[-\s]?time|new\s+user|new\s+parent|registration|sign[-\s]?up|activation|setup\s+journey)\b",
    re.IGNORECASE,
)

FRESH_ENTRY_FORBIDDEN_PATTERN = re.compile(
    r"\b(?:it(?:'|’)s me|checking in|see how you(?:'|’)re doing|how have you been|good to (?:talk|see) you again|talk to you again|ready to get started|ready to get things set up|setting things up|start onboarding|begin onboarding|connect my google calendar)\b",
    re.IGNORECASE,
)

LATER_STAGE_CLAIM_PATTERN = re.compile(
    r"\b(?:already|just\s+(?:finished|completed|confirmed)|finished\s+confirming|completed\s+(?:the\s+)?(?:ics|calendar)|"
    r"confirmed\s+(?:the\s+)?(?:ics|calendar)|connect(?:ing)?\s+(?:my\s+)?google\s+calendar|sync(?:ing)?\s+(?:my\s+)?google\s+calendar|"
    r"calendar\s+(?:export\s+)?link|ics\s+link|home\s+address)\b",
    re.IGNORECASE,
)

DIRECT_FACT_KEY_STOPWORDS = {
    "a", "an", "and", "are", "do", "does", "for", "give", "i", "in", "is", "it",
    "me", "my", "of", "on", "please", "the", "to", "what", "what's", "whats",
    "which", "who", "your",
}
ENTRY_GOAL_STOPWORDS = {
    "a", "an", "and", "app", "assistant", "be", "behavior", "case", "complete", "correct",
    "correctly", "do", "for", "from", "get", "goal", "help", "i", "in", "initial", "is",
    "it", "me", "message", "messages", "my", "of", "on", "onboarding", "or", "please",
    "process", "product", "set", "setup", "smoothly", "start", "successfully", "test",
    "the", "this", "through", "to", "user", "want", "with", "working", "would", "up",
}


@dataclass(init=False)
class SimulatedAction:
    """One simulator decision.

    `done=` is kept as an initializer alias for backward compatibility with
    older tests/integrations that constructed SimulatedAction directly.
    New code should use `action=message|done|human_required`.
    """

    action: str
    message: str
    reason: str
    usage: AIUsage
    human_action: Dict[str, Any]

    def __init__(
        self,
        action: str = "message",
        message: str = "",
        reason: str = "",
        usage: AIUsage | None = None,
        human_action: Dict[str, Any] | None = None,
        done: bool | None = None,
    ) -> None:
        if done is not None:
            action = "done" if done else (action or "message")

        normalized_action = str(action or "message").strip().lower()
        if normalized_action not in {"message", "done", "human_required"}:
            normalized_action = "message"

        self.action = normalized_action
        self.message = str(message or "")
        self.reason = str(reason or "")
        self.usage = usage or AIUsage()
        self.human_action = dict(human_action or {})

    @property
    def done(self) -> bool:
        return self.action == "done"

    @property
    def requires_human(self) -> bool:
        return self.action == "human_required"


class UserSimulator:
    """Adaptive AI human simulator with guarded progressive disclosure.

    The simulator never sees expected_result. It knows only the human persona,
    goal, starting state, scenario facts and the conversation. Real external
    resources must come from scenario fixtures or from a human-in-the-loop step;
    generated URLs/tokens are not allowed.
    """

    def __init__(self, ai: AIClient | None = None, config: Settings = settings):
        self.ai = ai or AIClient(config)
        self.config = config

    @staticmethod
    def _raw_scenario_facts(test_case: Dict[str, Any]) -> Dict[str, Any]:
        """Merge stored baseline + case facts without changing stored data."""
        facts: Dict[str, Any] = {}
        baseline = test_case.get("journey_baseline_facts")
        if isinstance(baseline, dict):
            facts.update({str(k): v for k, v in baseline.items() if str(v or "").strip()})
        scenario = test_case.get("scenario_data")
        if isinstance(scenario, dict):
            facts.update({str(k): v for k, v in scenario.items() if str(v or "").strip()})
        return facts

    @classmethod
    def _canonical_fact_key(cls, key: str) -> str:
        """Map legacy/generated aliases onto the runtime slot contract.

        This is intentionally a runtime compatibility layer. It does not rewrite
        the suite stored in SQLite. Older suites may use keys such as
        ``number_of_kids`` or ``child1_name`` while newer suites use slightly
        different aliases. The simulator should understand both consistently.
        """
        raw = str(key or "").strip()
        normalized = normalize_text(raw.replace("_", " ").replace("-", " "))

        # Resource-shaped keys are not ordinary conversation slots. Preserve
        # their identity exactly so compatibility normalization never turns a
        # URL/code fixture key into a provider/name/etc. fact by accident.
        if EXTERNAL_RESOURCE_KEY_PATTERN.search(raw) or ADDRESS_RESOURCE_KEY_PATTERN.search(raw):
            return raw

        child_index = cls._scenario_key_child_index(raw)
        category = cls._scenario_key_category(raw)

        if category == "parent_name":
            return "parent_name"
        if category == "child_count":
            return "child_count"
        if category == "child_name":
            return f"child_{child_index or 1}_name"
        if category == "sport":
            return f"child_{child_index or 1}_sport"
        if category == "provider":
            # An unindexed app/provider is a shared journey fact. Preserve that
            # distinction; only explicitly indexed provider keys become child-specific.
            return "sports_app" if child_index is None else f"child_{child_index}_sports_app"
        if category == "team_name":
            return f"child_{child_index or 1}_team_name"
        if category in {"email", "phone"}:
            return category

        # Keep real-resource and intentionally test-specific keys intact. They
        # are resolved by the resource layer rather than by ordinary slot logic.
        if "address" in normalized:
            return raw
        return raw

    @classmethod
    def _canonicalize_facts(cls, facts: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, List[str]]]:
        grouped: Dict[str, List[tuple[str, Any]]] = {}
        passthrough: Dict[str, Any] = {}
        for key, value in facts.items():
            if not str(value or "").strip():
                continue
            canonical = cls._canonical_fact_key(str(key))
            category = cls._scenario_key_category(str(key))
            if category in {"generic", "address"} or EXTERNAL_RESOURCE_KEY_PATTERN.search(str(key)):
                passthrough[str(key)] = value
            else:
                # Group canonical keys and legacy aliases together. This makes
                # conflicting values visible instead of allowing an alias to
                # silently overwrite an already-canonical fact.
                grouped.setdefault(canonical, []).append((str(key), value))

        canonical_facts = dict(passthrough)
        conflicts: Dict[str, List[str]] = {}
        for canonical, items in grouped.items():
            normalized_values = {normalize_text(str(value)) for _, value in items}
            if len(normalized_values) == 1:
                canonical_facts[canonical] = items[-1][1]
            else:
                # Do not silently choose between conflicting legacy aliases.
                # Preserve them for diagnostics and let a direct-slot request
                # surface a clear test-data problem instead of changing meaning.
                conflicts[canonical] = [key for key, _ in items]
                for key, value in items:
                    canonical_facts[key] = value
        return canonical_facts, conflicts

    @classmethod
    def prepare_case_for_execution(cls, test_case: Dict[str, Any]) -> Dict[str, Any]:
        """Compile a stable runtime fact snapshot without rewriting the suite.

        The snapshot remains attached to the resolved execution case, so HITL
        pause/resume and later turns use exactly the same facts even if generator
        conventions evolve. This is the backward-compatibility boundary between
        stored suites and the current simulator implementation.
        """
        if isinstance(test_case.get("_runtime_facts"), dict):
            test_case.setdefault("_simulator_runtime_version", SIMULATOR_RUNTIME_VERSION)
            test_case.setdefault("_runtime_fact_conflicts", {})
            return test_case

        raw = cls._raw_scenario_facts(test_case)
        canonical, conflicts = cls._canonicalize_facts(raw)
        test_case["_runtime_facts"] = canonical
        test_case["_runtime_fact_conflicts"] = conflicts
        test_case["_simulator_runtime_version"] = SIMULATOR_RUNTIME_VERSION
        return test_case

    @classmethod
    def _scenario_facts(cls, test_case: Dict[str, Any]) -> Dict[str, Any]:
        """Return the stable canonical runtime facts for old and new suites."""
        runtime = test_case.get("_runtime_facts")
        if isinstance(runtime, dict):
            return dict(runtime)
        raw = cls._raw_scenario_facts(test_case)
        canonical, _ = cls._canonicalize_facts(raw)
        return canonical

    def next_action(
        self,
        test_case: Dict[str, Any],
        turns: List[Dict[str, Any]],
        human_actions: List[Dict[str, Any]] | None = None,
        retry_note: str = "",
    ) -> SimulatedAction:
        human_actions = human_actions or []
        transcript = self._compact_transcript(turns)
        scenario_facts = self._scenario_facts(test_case)
        disclosure = self._disclosure_status(scenario_facts, turns)
        human_history = self._human_history(human_actions)
        retry_section = f"\nRETRY NOTE\n{retry_note}\n" if retry_note else ""
        disclosure_style = str(test_case.get("disclosure_style") or "progressive").lower()
        opening_hint = self._safe_opening_hint(test_case.get("initial_message_hint"))
        entry_journey = self._is_entry_journey_case(test_case)
        effective_state_mode = "fresh_user" if entry_journey else str(test_case.get("state_mode") or "fresh_user")
        journey_rule = (
            "- This is an onboarding/initial-entry journey. The user's goal may describe a later step, but it is the eventual target, "
            "not the opening line. Start from the normal beginning, let the assistant drive the sequence, and reach the target step "
            "through prerequisite questions. The first turn must not ask directly for a later integration/action or reveal scenario facts.\n"
            if entry_journey
            else ""
        )

        prompt = (
            "USER SCENARIO\n"
            f"Persona: {test_case.get('persona', '')}\n"
            f"User goal: {test_case.get('user_goal', '')}\n"
            f"Starting state: {effective_state_mode}\n"
            f"Journey entry mode: {'from_start' if entry_journey else test_case.get('journey_entry_mode', 'scenario_defined')}\n"
            f"Disclosure style: {disclosure_style}\n"
            f"Known scenario facts: {json.dumps(scenario_facts, ensure_ascii=False)}\n"
            f"Fact disclosure status: {json.dumps(disclosure, ensure_ascii=False)}\n"
            f"Preconditions visible to the user: {test_case.get('preconditions', '')}\n"
            f"Opening behavior hint: {opening_hint}\n\n"
            f"HUMAN ACTION HISTORY\n{human_history}\n\n"
            f"CONVERSATION SO FAR\n{transcript}\n"
            f"{retry_section}\n"
            "BEHAVIOR RULES\n"
            "- You are the human user, not a tester. Never mention tests, requirements, prompts, automation or evaluation.\n"
            "- There is no fixed script. Produce only the next realistic action based on the assistant's latest reply.\n"
            + journey_rule +
            "- For progressive disclosure, the first message should normally be a short SMS-style greeting or simple statement of intent, not name + child + sport + app + links together.\n"
            "- Never output template placeholders such as [Your Name], [Child Name], <name>, INSERT NAME, or similar tokens. If a concrete fact is not needed yet, omit it rather than inventing or templating it.\n"
            "- Do not talk like a business email or QA script. Avoid phrases such as 'start the onboarding process' or 'looking forward to working together' unless the persona explicitly requires formal business language.\n"
            "- After the conversation starts, answer the assistant's CURRENT direct question first. If it asks for your name, give the supplied name; if it asks for a child count, give the supplied count; do not reply with another generic question such as 'what do you need from me first?'.\n"
            "- Treat lists of future onboarding steps as context only. Respond to the final/current actionable question, not to a resource or field merely mentioned earlier in the same assistant message.\n"
            "- If the assistant asks which app/platform/provider/service the user uses, answer only that requested provider fact (for example TeamSnap). Do not send the associated URL until the assistant explicitly asks for the URL/link/export value.\n"
            "- Do not volunteer future onboarding answers merely because they exist in scenario facts.\n"
            "- Never invent or alter a scenario fact. In particular, NEVER fabricate URLs, calendar links, invite links, tokens, account IDs, OTPs or codes.\n"
            "- In positive flows, do not invent a physical address that the product may geocode or use for timezone/distance/weather/travel. Use a tester-controlled address resource when the product asks.\n"
            "- If the assistant asks the user to provide a real external resource that is not present in scenario facts or completed human-action input, choose human_required and ask the tester to provide that resource.\n"
            "- If the assistant gives an OAuth/authorization/sign-in/consent link or requires a browser/account/device action that cannot be completed through text, choose human_required. Copy a URL only if it appears exactly in the assistant's latest message.\n"
            "- Do NOT choose human_required for an ordinary text answer that you already know from scenario facts.\n"
            "- If a completed human action supplied a value/note, use it naturally when the assistant needs that value.\n"
            "- If the assistant repeats a question already answered, react naturally and mention that you already provided the information when appropriate.\n"
            "- Short SMS/chat messages are normal. Avoid formal, over-complete paragraphs unless the persona specifically calls for them.\n"
            "- Choose done only when the USER'S goal is clearly complete, the conversation is terminal, or there is no realistic next action.\n"
            "- For action=message, message must be the next human utterance. For action=done or human_required, message may be empty."
        )

        result = self.ai.structured(
            model=self.config.simulation_model,
            system=(
                "Role-play the human user described by the scenario. React to the application like a real person. "
                "Use only supplied facts and completed human inputs. You are intentionally unaware of the application's expected QA result."
            ),
            user=prompt,
            schema_name="simulated_user_action",
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {"type": "string", "enum": ["message", "done", "human_required"]},
                    "message": {"type": "string"},
                    "reason": {"type": "string"},
                    "human_action": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "kind": {"type": "string", "enum": ["resource_input", "browser_action", "other"]},
                            "resource_key": {"type": "string"},
                            "title": {"type": "string"},
                            "instructions": {"type": "string"},
                            "url": {"type": "string"},
                            "requires_input": {"type": "boolean"},
                            "input_label": {"type": "string"},
                        },
                        "required": [
                            "kind", "resource_key", "title", "instructions", "url", "requires_input", "input_label"
                        ],
                    },
                },
                "required": ["action", "message", "reason", "human_action"],
            },
            temperature=0.35,
        )

        value = result.value
        if "action" in value:
            action = str(value.get("action") or "message").strip().lower()
        else:
            # Backward compatibility with suites/tests created around the older
            # simulator contract that returned a boolean `done` field.
            action = "done" if bool(value.get("done")) else "message"
        if action not in {"message", "done", "human_required"}:
            action = "message"
        message = str(value.get("message") or "").strip()
        reason = str(value.get("reason") or "").strip()
        human_action = dict(value.get("human_action") or {})

        if action == "message" and not message:
            raise RuntimeError("User simulator returned an empty message.")

        if action == "human_required":
            human_action = self._sanitize_human_action(human_action, turns)
            if not human_action.get("title"):
                human_action["title"] = "Human action required"
            if not human_action.get("instructions"):
                human_action["instructions"] = reason or "Complete the required external step, then continue the run."

        return SimulatedAction(
            action=action,
            message=message,
            reason=reason,
            usage=result.usage,
            human_action=human_action,
        )

    def next_action_with_guard(
        self,
        test_case: Dict[str, Any],
        turns: List[Dict[str, Any]],
        human_actions: List[Dict[str, Any]] | None = None,
    ) -> SimulatedAction:
        human_actions = human_actions or []

        # Entry-journey openings are safety controlled. The rest of the conversation
        # remains adaptive, but a brand-new user must never start with wording that
        # implies prior history ("it's me"), dumps facts, or jumps to a later step.
        fresh_opening = self._fresh_entry_opening_action(test_case, turns)
        if fresh_opening:
            return fresh_opening

        # Strong deterministic guard for the most important HITL case: a real
        # browser/account authorization step. This does not rely only on the LLM
        # noticing OAuth wording, and it will not re-trigger after the tester has
        # completed the same link.
        forced_human = self._forced_human_action(turns, human_actions)
        if forced_human:
            return SimulatedAction(
                action="human_required",
                message="",
                reason="The next required step must be completed by a real person in a browser/account flow.",
                usage=AIUsage(models={}),
                human_action=forced_human,
            )

        # Choice questions such as "which app do you use - TeamSnap or
        # LeagueApps?" are ordinary onboarding questions, not requests for the
        # calendar URL mentioned in an earlier sentence. Answer the provider
        # fact first and wait until the product explicitly asks for the resource.
        provider_choice = self._controlled_provider_choice_action(test_case, turns)
        if provider_choice:
            return provider_choice

        # If the exact same tester-controlled resource has already been sent
        # twice and the product is still asking for it, stop wasting turns and
        # tokens. This is strong evidence that the target did not accept/persist
        # the resource, not a reason for the simulated parent to spam it forever.
        repeated_resource_failure = self._repeated_controlled_resource_failure(test_case, turns)
        if repeated_resource_failure:
            return repeated_resource_failure

        # When the product actually asks for a real external value and the
        # tester already saved a matching project resource, send that exact
        # value now. Machine-readable resources are sent raw (no surrounding
        # prose) so the target receives exactly what the tester configured.
        controlled_resource = self._controlled_external_resource_action(test_case, turns)
        if controlled_resource:
            return controlled_resource

        # Direct slot/fact questions are simple deterministic work, not a nuanced
        # conversation decision. If the assistant asks for a supplied scenario
        # fact (name, child count, child name, sport, email, etc.), answer only
        # that current question. This prevents the model from replying with
        # "what do you need from me?" or volunteering a later fact.
        controlled_fact = self._controlled_scenario_fact_action(test_case, turns)
        if controlled_fact:
            return controlled_fact

        forced_resource = self._forced_external_resource_input(test_case, turns, human_actions)
        if forced_resource:
            return SimulatedAction(
                action="human_required",
                message="",
                reason="The product requested a real external test resource that is not available in the scenario.",
                usage=AIUsage(models={}),
                human_action=forced_resource,
            )

        # If an entry/onboarding assistant sends an informational message without
        # asking the next prerequisite question, keep the human on the natural
        # journey instead of allowing the model to jump to the later test goal.
        progression = self._entry_progression_action(test_case, turns)
        if progression:
            return progression

        accumulated = AIUsage(models={})
        retry_note = ""
        action: SimulatedAction | None = None
        for attempt in range(3):
            action = self.next_action(test_case, turns, human_actions, retry_note)
            accumulated.add(action.usage)

            if not turns and action.done:
                retry_note = (
                    "The conversation has not started. Send a short, natural opening message for the user's goal; "
                    "do not finish yet and do not dump scenario facts."
                )
                continue

            if action.action != "message":
                # The AI fallback may independently ask for the same browser/account
                # action again even though the tester already completed it. Treat a
                # matching completed action as satisfied and continue the conversation
                # instead of reopening the same HITL pause indefinitely.
                if action.action == "human_required":
                    completed_followup = self._completed_human_action_followup(
                        action.human_action, human_actions
                    )
                    if completed_followup:
                        return SimulatedAction(
                            action="message",
                            message=completed_followup,
                            reason="The requested external action was already completed by the tester.",
                            usage=accumulated,
                        )
                action.usage = accumulated
                return action

            violation = self._message_guard_reason(test_case, turns, human_actions, action.message)
            if not violation:
                action.usage = accumulated
                return action
            retry_note = violation

        assert action is not None
        invented = self._invented_urls(test_case, turns, human_actions, action.message)
        if invented:
            return SimulatedAction(
                action="human_required",
                message="",
                reason="A real external test resource is required and the simulator is not allowed to invent one.",
                usage=accumulated,
                human_action={
                    "kind": "resource_input",
                    "resource_key": "external_resource",
                    "title": "Provide a real test resource",
                    "instructions": "Enter the real URL/code/value requested by the application. The simulator will use your supplied value when the run resumes.",
                    "url": "",
                    "requires_input": True,
                    "input_label": "Real test resource value",
                },
            )

        # Do not send a known-bad simulator message to the product under test.
        # After three bounded retries, treat persistent progressive-disclosure
        # violations as a QA-agent execution error instead of polluting the test
        # with an unrealistic parent message.
        raise RuntimeError(
            "User simulator could not produce a safe progressive-disclosure message after three attempts."
        )

    def _fresh_entry_opening_action(
        self,
        test_case: Dict[str, Any],
        turns: List[Dict[str, Any]],
    ) -> SimulatedAction | None:
        """Return a safe first SMS for independent onboarding/entry tests.

        The first turn is deliberately constrained because it establishes the
        target's state machine. Later turns remain adaptive. If a test explicitly
        supplies a safe inbound_message/first_message scenario value, preserve it;
        otherwise choose a short neutral opener deterministically from the case ID.
        """
        if turns or not self._is_entry_journey_case(test_case):
            return None
        if str(test_case.get("state_mode") or "fresh_user").lower() != "fresh_user":
            return None

        scenario = self._scenario_facts(test_case)
        for key in ("inbound_message", "first_message", "opening_message", "initial_message"):
            value = str(scenario.get(key) or "").strip()
            if value and self._is_safe_fresh_opening(test_case, value):
                return SimulatedAction(
                    action="message",
                    message=value,
                    reason=f"Using explicit safe first-message scenario fact '{key}'.",
                    usage=AIUsage(models={}),
                )

        openers = (
            "Hey, who is this?",
            "Hi, who am I texting?",
            "Hey, who's this?",
            "Hi there, who is this?",
        )
        seed = str(test_case.get("id") or test_case.get("title") or test_case.get("user_goal") or "fresh")
        index = hashlib.sha256(seed.encode("utf-8")).digest()[0] % len(openers)
        return SimulatedAction(
            action="message",
            message=openers[index],
            reason="Starting an independent fresh-user journey with a short neutral SMS opener.",
            usage=AIUsage(models={}),
        )

    def _is_safe_fresh_opening(self, test_case: Dict[str, Any], message: str) -> bool:
        text = str(message or "").strip()
        if not text or TEMPLATE_PLACEHOLDER_PATTERN.search(text) or FORMAL_OPENING_PATTERN.search(text):
            return False
        if FRESH_ENTRY_FORBIDDEN_PATTERN.search(text):
            return False
        if URL_PATTERN.search(text):
            return False
        if len(re.findall(r"\b\w+[\w'-]*\b", text)) > 14:
            return False
        if self._newly_disclosed_keys(self._scenario_facts(test_case), [], text):
            return False
        if self._entry_target_overlap(test_case, text):
            return False
        return True

    def _entry_required_categories(self, test_case: Dict[str, Any]) -> List[str]:
        configured = [
            str(item).strip()
            for item in (test_case.get("journey_required_fact_categories") or [])
            if str(item).strip()
        ]
        if configured:
            return configured
        categories: List[str] = []
        for key, value in self._scenario_facts(test_case).items():
            if not str(value or "").strip():
                continue
            category = self._scenario_key_category(str(key))
            if category in {"parent_name", "child_count", "child_name", "sport", "provider", "team_name", "email", "phone"} and category not in categories:
                categories.append(category)
        return categories

    def _entry_disclosed_categories(
        self,
        turns: List[Dict[str, Any]],
    ) -> set[str]:
        """Infer completed ordinary slots from actual assistant->user turn pairs."""
        disclosed: set[str] = set()
        for index, turn in enumerate(turns):
            if turn.get("role") != "assistant":
                continue
            category = self._requested_fact_category(str(turn.get("content") or ""))
            if category not in {"parent_name", "child_count", "child_name", "sport", "provider", "team_name", "email", "phone"}:
                continue
            has_user_reply = any(
                later.get("role") == "user"
                for later in turns[index + 1 : index + 3]
            )
            if has_user_reply:
                disclosed.add(category)
        return disclosed

    def _entry_pending_categories(
        self,
        test_case: Dict[str, Any],
        turns: List[Dict[str, Any]],
    ) -> List[str]:
        required = self._entry_required_categories(test_case)
        disclosed = self._entry_disclosed_categories(turns)
        return [category for category in required if category not in disclosed]

    def _entry_progression_action(
        self,
        test_case: Dict[str, Any],
        turns: List[Dict[str, Any]],
    ) -> SimulatedAction | None:
        if not turns or not self._is_entry_journey_case(test_case):
            return None
        pending = self._entry_pending_categories(test_case, turns)
        if not pending:
            return None
        latest = self._latest_assistant(turns)
        if not latest:
            return None
        category = self._requested_fact_category(latest)
        # Any actual question belongs to either a deterministic slot/resource
        # guard or the adaptive AI fallback.  Progression prompts are only for
        # informational assistant turns; never answer an unclassified question
        # with another "what do you need next?" message.
        if category:
            return None
        normalized = normalize_text(latest)
        if re.search(r"\b(?:all set|you(?:'|’)re done|setup is complete|onboarding is complete|successfully completed)\b", normalized):
            return None
        if re.search(r"\b(?:how can i help|what can i help|what do you need help with|how may i help)\b", normalized):
            message = "I need some help getting set up."
        else:
            prompts = (
                "Okay, what do you need from me next?",
                "Got it. What do you need from me?",
                "Okay. What's the next step?",
            )
            seed = f"{test_case.get('id','')}|{len(turns)}|{latest}"
            message = prompts[hashlib.sha256(seed.encode("utf-8")).digest()[0] % len(prompts)]
        return SimulatedAction(
            action="message",
            message=message,
            reason=(
                "Keeping the fresh-user onboarding journey on prerequisite collection because the assistant "
                "did not ask a concrete next slot and required onboarding facts are still pending."
            ),
            usage=AIUsage(models={}),
        )

    def _controlled_scenario_fact_action(
        self,
        test_case: Dict[str, Any],
        turns: List[Dict[str, Any]],
    ) -> SimulatedAction | None:
        latest = self._latest_assistant(turns)
        if not latest:
            return None
        focus = self._latest_actionable_segment(latest)
        category = self._requested_fact_category(focus)
        scenario = self._scenario_facts(test_case)

        # Provider/app and real-resource requests have dedicated guards that run
        # before this method, so do not compete with them here. Unclassified
        # questions also fall through to the adaptive AI instead of fuzzy-matching
        # several unrelated scenario facts.
        if not category or category in {"provider", "address", "external_resource", "generic"}:
            return None

        child_index = self._requested_child_index(category, focus, turns, scenario)
        candidates: List[tuple[int, str, Any]] = []
        for key, value in scenario.items():
            value_text = str(value or "").strip()
            if not value_text or URL_PATTERN.search(value_text):
                continue
            key_category = self._scenario_key_category(str(key))
            key_child_index = self._scenario_key_child_index(str(key))

            # For child-specific slots, prefer the fact for the child currently
            # being discussed. A generic first-child value may satisfy child #1,
            # but it must never be reused for child #2 or later.
            if child_index and category in {"child_name", "sport", "team_name"}:
                if key_child_index is None:
                    if child_index != 1:
                        continue
                elif key_child_index != child_index:
                    continue

            # For recognized onboarding slots, category matching is strict.
            # Never let a generic word overlap such as "name" cause parent_name
            # to be reused as child_name, or another unrelated fact to answer a
            # direct slot question. Generic matching is reserved for genuinely
            # unclassified questions only.
            if category != "generic" and key_category != category:
                continue

            score = 20 if key_category == category else 0
            score += self._generic_key_question_score(str(key), focus)
            if score > 0:
                candidates.append((score, str(key), value))

        if not candidates:
            if category != "generic" and self._is_entry_journey_case(test_case):
                raise RuntimeError(
                    "Invalid test case data: the current onboarding question requests "
                    f"'{category}', but scenario_data has no matching fact. "
                    "The simulator will not substitute an unrelated value; regenerate or revise this test case."
                )
            return None

        candidates.sort(key=lambda item: (-item[0], item[1]))
        top_score = candidates[0][0]
        top = [item for item in candidates if item[0] == top_score]
        distinct_values = {normalize_text(str(item[2])) for item in top}
        if len(distinct_values) > 1:
            raise RuntimeError(
                "Invalid test case data: multiple conflicting scenario facts match the current "
                f"'{category}' question. Revise the test case so the requested slot has one clear value."
            )

        _, key, value = candidates[0]
        message = self._format_scenario_fact_answer(category, value)
        return SimulatedAction(
            action="message",
            message=message,
            reason=f"Answering the application's current direct question with supplied scenario fact '{key}'.",
            usage=AIUsage(models={}),
        )

    @classmethod
    def _requested_fact_category(cls, message: str) -> str:
        focus_raw = cls._latest_actionable_segment(message)
        focus = normalize_text(focus_raw)
        if not focus or not cls._is_high_confidence_direct_request(focus_raw):
            return ""
        if cls._is_provider_choice_request(focus_raw):
            return "provider"
        if cls._is_external_resource_request(focus_raw):
            return "external_resource"
        if cls._is_address_request(focus_raw):
            return "address"
        if re.search(r"\bhow many\b.{0,50}\b(?:kids?|children|child)\b", focus, re.IGNORECASE):
            return "child_count"
        if re.search(
            r"\b(?:kid|kid's|kids|child|child's|children|son|daughter)\b.{0,45}\bname\b|"
            r"\bname\b.{0,45}\b(?:kid|child|son|daughter)\b",
            focus,
            re.IGNORECASE,
        ):
            return "child_name"
        if re.search(
            r"\b(?:what(?:'s| is)?|tell me|give me|may i have|can i have|could i get)\b.{0,55}\byour name\b|"
            r"\bwho am i speaking with\b|\bwhat should i call you\b|"
            r"\bwhat name would you like(?: me| the assistant)? to use(?: for you)?\b|"
            r"\bwhich name should (?:i|we|the assistant) use\b|\bname would you like (?:me|the assistant) to use\b",
            focus,
            re.IGNORECASE,
        ):
            return "parent_name"
        if re.search(
            r"\b(?:what|which|tell me|give me|provide|share)\b.{0,45}\bsport\b|"
            r"\bsport\b.{0,45}\b(?:play|plays|playing|in)\b|^sport\s*\?$",
            focus,
            re.IGNORECASE,
        ):
            return "sport"
        if re.search(r"\b(?:email|e-mail)\b", focus, re.IGNORECASE):
            return "email"
        if re.search(r"\b(?:phone|mobile)\b", focus, re.IGNORECASE):
            return "phone"
        if re.search(r"\bteam\b.{0,30}\bname\b|\bwhich team\b|^team name\s*\?$", focus, re.IGNORECASE):
            return "team_name"
        return "generic"

    @staticmethod
    def _scenario_key_child_index(key: str) -> int | None:
        normalized = normalize_text(str(key).replace("_", " ").replace("-", " "))
        match = re.search(r"\bchild\s*([1-5])\b", normalized)
        if match:
            return int(match.group(1))
        ordinals = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
        for word, index in ordinals.items():
            if re.search(
                rf"(?:\b{word}\b.{{0,18}}\b(?:child|kid)\b|\b(?:child|kid)\b.{{0,18}}\b{word}\b)",
                normalized,
                re.IGNORECASE,
            ):
                return index
        return None

    @classmethod
    def _requested_child_index(
        cls,
        category: str,
        focus: str,
        turns: List[Dict[str, Any]],
        scenario: Dict[str, Any],
    ) -> int | None:
        if category not in {"child_name", "sport", "provider", "team_name"}:
            return None

        explicit = cls._scenario_key_child_index(focus)
        if explicit:
            return explicit

        normalized_focus = normalize_text(focus)
        # If the assistant mentions a known child by name, bind the requested
        # sport/app/team fact to that child's indexed slot.
        for key, value in scenario.items():
            if cls._scenario_key_category(str(key)) != "child_name":
                continue
            value_text = normalize_text(str(value or ""))
            if not value_text or value_text not in normalized_focus:
                continue
            index = cls._scenario_key_child_index(str(key))
            return index or 1

        # For sequential child-name/sport/provider collection, infer the next
        # child from how many same-slot prompts have already received user replies.
        completed = 0
        for i, turn in enumerate(turns):
            if turn.get("role") != "assistant":
                continue
            if cls._requested_fact_category(str(turn.get("content") or "")) != category:
                continue
            if any(later.get("role") == "user" for later in turns[i + 1 : i + 3]):
                completed += 1
        return completed + 1

    @staticmethod
    def _scenario_key_category(key: str) -> str:
        normalized = normalize_text(str(key).replace("_", " ").replace("-", " "))
        if "address" in normalized:
            return "address"
        if any(token in normalized for token in ("app", "platform", "provider", "service")):
            return "provider"
        if "name" in normalized and any(token in normalized for token in ("child", "kid")):
            return "child_name"
        if "name" in normalized and any(token in normalized for token in ("parent", "user", "adult")):
            return "parent_name"
        if normalized in {"name", "full name", "first name"}:
            return "parent_name"
        if any(token in normalized for token in ("number of kids", "number of children", "child count", "kid count", "children count")):
            return "child_count"
        if "sport" in normalized:
            return "sport"
        if "email" in normalized:
            return "email"
        if "phone" in normalized or "mobile" in normalized:
            return "phone"
        if "team" in normalized and "name" in normalized:
            return "team_name"
        return "generic"

    @staticmethod
    def _generic_key_question_score(key: str, question: str) -> int:
        key_words = [
            token
            for token in re.findall(r"[a-z0-9]+", normalize_text(key.replace("_", " ").replace("-", " ")))
            if len(token) >= 3 and token not in DIRECT_FACT_KEY_STOPWORDS
        ]
        focus = normalize_text(question)
        matches = sum(1 for token in key_words if token in focus)
        return matches * 3

    @staticmethod
    def _format_scenario_fact_answer(category: str, value: Any) -> str:
        text = str(value or "").strip()
        if category == "child_count":
            number_words = {"1": "Just one.", "2": "Two.", "3": "Three.", "4": "Four.", "5": "Five."}
            return number_words.get(text, f"{text}.")
        if category in {"parent_name", "child_name", "sport", "team_name"}:
            return text if text.endswith((".", "!", "?")) else f"{text}."
        return text

    def _controlled_provider_choice_action(
        self,
        test_case: Dict[str, Any],
        turns: List[Dict[str, Any]],
    ) -> SimulatedAction | None:
        """Answer an explicit app/platform/provider question from scenario facts.

        A target may mention that it eventually needs a calendar link and then
        ask only which app the user uses. That is *not* permission to send the
        URL yet. Keeping this step deterministic prevents the simulator from
        skipping an onboarding state or confusing strict target parsers.
        """
        latest = self._latest_assistant(turns)
        if not latest or not self._is_provider_choice_request(latest):
            return None

        scenario = self._scenario_facts(test_case)
        focus = self._latest_actionable_segment(latest)
        child_index = self._requested_child_index("provider", focus, turns, scenario)
        preferred_keys = (
            "sports_app",
            "team_app",
            "calendar_app",
            "app",
            "platform",
            "provider",
            "service",
        )

        candidates: List[tuple[int, str, Any]] = []
        for key, value in scenario.items():
            value_text = str(value or "").strip()
            if not value_text or URL_PATTERN.search(value_text):
                continue
            key_category = self._scenario_key_category(str(key))
            if key_category != "provider":
                continue
            key_child_index = self._scenario_key_child_index(str(key))
            if child_index and key_child_index is not None and key_child_index != child_index:
                continue
            # A shared sports_app/provider is allowed for multiple children.
            key_norm = normalize_text(str(key).replace("_", " ").replace("-", " "))
            score = 20
            if str(key) in preferred_keys:
                score += 10
            if any(token in key_norm for token in ("app", "platform", "provider", "service")):
                score += 6
            candidates.append((score, str(key), value))

        if candidates:
            candidates.sort(key=lambda item: (-item[0], item[1]))
            top_score = candidates[0][0]
            top = [item for item in candidates if item[0] == top_score]
            distinct_values = {normalize_text(str(item[2])) for item in top}
            if len(distinct_values) > 1:
                raise RuntimeError(
                    "Invalid test case data: multiple conflicting provider/app facts are present. "
                    "Revise the test case so the provider choice has one clear value."
                )
            _, key, value = candidates[0]
            return SimulatedAction(
                action="message",
                message=str(value).strip(),
                reason=f"Answering the application's provider/app question with scenario fact '{key}'.",
                usage=AIUsage(models={}),
            )

        inferred = self._provider_from_test_resources(test_case, latest)
        if inferred:
            return SimulatedAction(
                action="message",
                message=inferred,
                reason="Answering the provider/app question from the tester-controlled resource configured for this case.",
                usage=AIUsage(models={}),
            )

        if self._is_entry_journey_case(test_case):
            raise RuntimeError(
                "Invalid test case data: the current onboarding question asks for an app/provider, "
                "but the case has no provider scenario fact and no matching configured test resource. "
                "The simulator will not reuse a person's name or another unrelated value."
            )
        return None

    def _provider_from_test_resources(
        self,
        test_case: Dict[str, Any],
        latest_assistant: str,
    ) -> str:
        """Infer a provider choice only from tester-controlled resource identity.

        Example: a saved key such as ``teamsnap_calendar_url`` may safely tell
        us that the provider choice is TeamSnap when the target asks
        "TeamSnap or LeagueApps?". This never invents a provider and never uses
        unrelated scenario values such as a person's name.
        """
        sources: List[str] = []
        sources.extend(str(key) for key in (test_case.get("_runtime_resources") or {}).keys())
        sources.extend(str(key) for key in (test_case.get("_expected_resource_keys") or []))
        sources.extend(str(key) for key in (test_case.get("required_fixture_keys") or []))
        for value in (test_case.get("_runtime_resources") or {}).values():
            sources.append(str(value or ""))

        generic_tokens = {
            "url", "link", "calendar", "calender", "ics", "export", "schedule", "team",
            "test", "resource", "valid", "external", "webhook", "oauth", "auth", "code",
            "token", "account", "http", "https", "www", "com", "cdn", "ical",
        }
        question = str(latest_assistant or "")
        matches: Dict[str, str] = {}
        for source in sources:
            for token in re.findall(r"[a-z][a-z0-9]{2,}", normalize_text(source)):
                if token in generic_tokens or len(token) < 4:
                    continue
                match = re.search(rf"\b{re.escape(token)}\b", question, re.IGNORECASE)
                if match:
                    matches[normalize_text(match.group(0))] = match.group(0)

        if not matches:
            return ""
        labels = [matches[key] for key in sorted(matches)]
        if len(labels) == 1:
            return labels[0]

        seed = str(test_case.get("id") or test_case.get("title") or "provider")
        index = hashlib.sha256(seed.encode("utf-8")).digest()[0] % len(labels)
        return labels[index]

    def _repeated_controlled_resource_failure(
        self,
        test_case: Dict[str, Any],
        turns: List[Dict[str, Any]],
    ) -> SimulatedAction | None:
        latest = self._latest_assistant(turns)
        if not latest:
            return None
        is_external = self._is_external_resource_request(latest)
        is_address = self._is_address_request(latest) and self._requires_real_address(test_case)
        if not (is_external or is_address):
            return None
        selected = self._select_runtime_resource(test_case, latest)
        if not selected:
            return None
        key, value = selected
        expected = normalize_text(str(value or ""))
        if not expected:
            return None
        deliveries = sum(
            1
            for turn in turns
            if turn.get("role") == "user" and normalize_text(str(turn.get("content") or "")) == expected
        )
        if deliveries < 2:
            return None
        return SimulatedAction(
            action="done",
            message="",
            reason="resource_not_accepted",
            usage=AIUsage(models={}),
            human_action={},
        )

    def _controlled_external_resource_action(
        self,
        test_case: Dict[str, Any],
        turns: List[Dict[str, Any]],
    ) -> SimulatedAction | None:
        latest = self._latest_assistant(turns)
        if not latest:
            return None

        is_external = self._is_external_resource_request(latest)
        is_address = self._is_address_request(latest) and self._requires_real_address(test_case)
        if not (is_external or is_address):
            return None

        selected = self._select_runtime_resource(test_case, latest)
        if not selected:
            return None

        key, value = selected
        value_text = str(value).strip()
        if not value_text:
            return None

        user_history = " ".join(
            str(turn.get("content", "")) for turn in turns if turn.get("role") == "user"
        )
        repeated = normalize_text(value_text) in normalize_text(user_history)
        key_text = normalize_text(key.replace("_", " "))

        if ADDRESS_RESOURCE_KEY_PATTERN.search(key):
            # Geocoders and address parsers should receive the complete tester-
            # controlled value, not an LLM-shortened or conversational variant.
            message = value_text
        elif URL_PATTERN.search(value_text):
            # A "paste/send the link" step is an integration boundary. Send the
            # exact URL alone so strict downstream parsers see the tester's value
            # byte-for-byte (apart from transport encoding).
            message = value_text
        elif any(token in key_text for token in ("otp", "code", "token", "verification")):
            message = value_text
        else:
            message = f"Sure, here it is: {value_text}"

        return SimulatedAction(
            action="message",
            message=message,
            reason=f"Using tester-controlled project resource '{key}' after the application requested it.",
            usage=AIUsage(models={}),
        )

    def _forced_external_resource_input(
        self,
        test_case: Dict[str, Any],
        turns: List[Dict[str, Any]],
        human_actions: List[Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        latest_assistant = self._latest_assistant(turns)
        if not latest_assistant:
            return None

        is_external = self._is_external_resource_request(latest_assistant)
        is_address = self._is_address_request(latest_assistant) and self._requires_real_address(test_case)
        if not (is_external or is_address):
            return None

        # A saved project resource always wins over HITL. The user should only
        # be interrupted when the product asks for a real value that the QA
        # project does not already know.
        if self._select_runtime_resource(test_case, latest_assistant):
            return None

        # Legacy/generated cases may already contain a resolved real resource in
        # scenario data. In that case the normal AI parent can disclose it.
        for key, value in self._scenario_facts(test_case).items():
            if EXTERNAL_RESOURCE_KEY_PATTERN.search(str(key)) and str(value or "").strip():
                return None

        # Backward compatibility for a paused run created before runtime
        # resources were injected into the case. Only a completed resource-input
        # action counts here; unrelated OAuth notes must not suppress a later
        # missing-resource pause.
        for item in human_actions:
            action = item.get("action") or {}
            if (
                str(item.get("status") or "") == "completed"
                and str(action.get("kind") or "") == "resource_input"
                and str(item.get("note") or "").strip()
            ):
                return None

        resource_key = self._select_expected_resource_key(test_case, latest_assistant)
        if is_address:
            return {
                "kind": "resource_input",
                "resource_key": resource_key,
                "title": "Provide a valid test address",
                "instructions": (
                    "The product requested a physical address that may be geocoded or used for timezone, distance, weather, or travel calculations. "
                    "Enter a tester-approved complete address (street, city, state, postal code, country). The AI user will use that exact address."
                ),
                "url": "",
                "requires_input": True,
                "input_label": "Valid test address",
            }
        return {
            "kind": "resource_input",
            "resource_key": resource_key,
            "title": "Provide a real test resource",
            "instructions": (
                "The product requested a real external value (for example a TeamSnap/LeagueApps/ICS link or verification code). "
                "Enter the tester-controlled value here. The AI user will use that exact value when this same test resumes; "
                "it will not invent a substitute."
            ),
            "url": "",
            "requires_input": True,
            "input_label": "Real external test resource",
        }

    @staticmethod
    def _latest_assistant(turns: List[Dict[str, Any]]) -> str:
        return next(
            (str(turn.get("content", "")) for turn in reversed(turns) if turn.get("role") == "assistant"),
            "",
        )

    @classmethod
    def _is_address_request(cls, message: str) -> bool:
        # Physical addresses are tester-controlled resources. Only inject one
        # when the CURRENT segment is a high-confidence request for the address,
        # never because an address is merely described as a later requirement.
        focus = cls._latest_actionable_segment(message)
        normalized = normalize_text(focus)
        if not normalized or not cls._is_high_confidence_direct_request(focus):
            return False
        patterns = (
            r"\b(?:what is|what's|whats|send|provide|share|enter|give me|need)\b.{0,80}\b(?:home address|street address|physical address|mailing address|full address|address)\b",
            r"\bwhat\b.{0,35}\b(?:home address|street address|physical address|mailing address|full address|address)\b.{0,45}\b(?:should|can|do)\b.{0,20}\buse\b",
            r"^(?:your\s+)?(?:home address|street address|physical address|mailing address|full address|address)\s*(?:please)?\??$",
        )
        return any(re.search(pattern, normalized, re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _requires_real_address(test_case: Dict[str, Any]) -> bool:
        test_type = normalize_text(str(test_case.get("test_type") or ""))
        required_keys = [
            str(key)
            for key in (test_case.get("_expected_resource_keys") or test_case.get("required_fixture_keys") or [])
        ]
        if any(ADDRESS_RESOURCE_KEY_PATTERN.search(key) for key in required_keys):
            return True

        # Only address-focused negative/validation/boundary cases are allowed to
        # use intentionally bad address data. A voice/tone validation case is
        # still a normal positive address flow and must use the tester-controlled
        # real address rather than an AI-invented location.
        case_text = normalize_text(
            " ".join(
                [
                    str(test_case.get("title") or ""),
                    str(test_case.get("user_goal") or ""),
                    str(test_case.get("expected_result") or ""),
                    " ".join(str(item) for item in (test_case.get("objectives") or [])),
                    " ".join(str(key) for key in (test_case.get("scenario_data") or {}).keys()),
                ]
            )
        )
        intentionally_bad_address = bool(
            re.search(
                r"\b(?:invalid|malformed|incomplete|bad|unrecognized|unrecognised|unsupported|missing|partial)\b.{0,40}\baddress\b|"
                r"\baddress\b.{0,40}\b(?:invalid|malformed|incomplete|bad|unrecognized|unrecognised|unsupported|missing|partial)\b",
                case_text,
                re.IGNORECASE,
            )
        )
        if test_type in {"negative", "validation", "boundary"} and intentionally_bad_address:
            return False

        # For ordinary positive flows, any physical address requested by the
        # product should be a tester-controlled, valid location rather than a
        # plausible-looking AI invention.
        return True

    def _active_provider_for_resource(
        self,
        test_case: Dict[str, Any],
        latest_assistant: str,
    ) -> str:
        """Resolve the provider relevant to the current calendar request.

        Prefer a provider explicitly named by the target. Otherwise bind the
        request to the child currently being discussed and finally fall back to
        a unique shared provider fact. This keeps old suites compatible while
        preventing a TeamSnap fixture from being sent to a GameChanger case.
        """
        scenario = self._scenario_facts(test_case)
        focus = self._latest_actionable_segment(latest_assistant)
        normalized_focus = normalize_text(focus)

        provider_items: List[tuple[int | None, str]] = []
        for key, value in scenario.items():
            if self._scenario_key_category(str(key)) != "provider":
                continue
            value_text = str(value or "").strip()
            if not value_text or URL_PATTERN.search(value_text):
                continue
            provider_items.append((self._scenario_key_child_index(str(key)), value_text))

        # Target wording is strongest evidence when it repeats the provider.
        explicit = []
        for _, value in provider_items:
            if normalize_text(value) and normalize_text(value) in normalized_focus:
                explicit.append(value)
        if len({normalize_text(value) for value in explicit}) == 1 and explicit:
            return explicit[0]

        child_index = self._requested_child_index("provider", focus, [], scenario)
        if child_index:
            child_specific = [
                value
                for index, value in provider_items
                if index == child_index
            ]
            if len({normalize_text(value) for value in child_specific}) == 1 and child_specific:
                return child_specific[0]

        shared = [value for index, value in provider_items if index in {None, 1}]
        distinct = {normalize_text(value): value for value in shared}
        if len(distinct) == 1:
            return next(iter(distinct.values()))
        return ""

    @staticmethod
    def _resource_mentions_provider(key: str, value: Any, provider: str) -> bool:
        provider_token = re.sub(r"[^a-z0-9]+", "", normalize_text(provider))
        if not provider_token:
            return False
        resource_text = re.sub(
            r"[^a-z0-9]+",
            "",
            normalize_text(f"{key} {value or ''}").replace("calender", "calendar"),
        )
        return provider_token in resource_text

    @staticmethod
    def _is_deferred_action_segment(message: str) -> bool:
        """True when a segment clearly describes a future/later requirement.

        This is intentionally narrow. It is used only to stop deterministic
        slot/resource guards from treating planning language as the current ask.
        Ambiguous wording falls through to the AI parent instead.
        """
        normalized = normalize_text(str(message or ""))
        if not normalized:
            return False
        if re.search(r"\b(?:right now|for now|now please|need .* now|send .* now|provide .* now)\b", normalized):
            return False
        deferred_patterns = (
            r"\b(?:later|eventually|not yet|down the road|in a later step|at a later step|when we get there)\b",
            r"\b(?:i|we)\s+(?:will|'ll)\s+(?:ask|need|request|collect)\b",
            r"\b(?:after that|after this)\b.{0,60}\b(?:ask|need|request|collect)\b",
        )
        return any(re.search(pattern, normalized, re.IGNORECASE) for pattern in deferred_patterns)

    @classmethod
    def _is_high_confidence_direct_request(cls, message: str) -> bool:
        """Recognize only clear current questions/commands.

        Deterministic logic should bind facts/resources only for unambiguous
        requests. Descriptive or future-looking mentions deliberately return
        False so the adaptive AI parent keeps control of the conversation.
        """
        raw = str(message or "").strip()
        normalized = normalize_text(raw)
        if not normalized or cls._is_deferred_action_segment(raw):
            return False
        if "?" in raw:
            return True
        patterns = (
            r"\b(?:please\s+)?(?:tell me|give me|send|provide|share|enter|paste|drop|confirm|choose|select)\b",
            r"\b(?:can|could|would|will)\s+you\b",
            r"\b(?:i|we)\s+need\s+(?:you\s+to|your|the|a)\b",
            r"^need\s+(?:your|the|a)\b",
            r"\b(?:waiting for|still waiting for)\b",
            r"^(?:what|what's|whats|which|who|how many)\b",
        )
        return any(re.search(pattern, normalized, re.IGNORECASE) for pattern in patterns)

    @classmethod
    def _latest_actionable_segment(cls, message: str) -> str:
        """Return the most recent high-confidence question/command.

        Earlier planning statements must never outrank a later current request.
        For example, "We'll need your calendar link later. For now, tell me your
        child's name." focuses on the child-name request. If no segment is a
        clear request, return the final segment and let the AI parent reason.
        """
        raw = str(message or "").strip()
        if not raw:
            return ""
        segments = [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+|[\r\n]+", raw)
            if part.strip()
        ]
        if not segments:
            return raw

        for segment in reversed(segments):
            if cls._is_high_confidence_direct_request(segment):
                return segment
        return segments[-1]

    @classmethod
    def _is_provider_choice_request(cls, message: str) -> bool:
        focus_raw = cls._latest_actionable_segment(message)
        focus = normalize_text(focus_raw)
        if not focus or not cls._is_high_confidence_direct_request(focus_raw):
            return False
        patterns = (
            r"\bwhich\b.{0,45}\b(?:app|platform|provider|service)\b",
            r"\bwhat\b.{0,45}\b(?:app|platform|provider|service)\b.{0,45}\b(?:use|using|on)\b",
            r"\b(?:app|platform|provider|service)\b.{0,45}\b(?:do you use|are you using|does .{0,30} use)\b",
        )
        if any(re.search(pattern, focus, re.IGNORECASE) for pattern in patterns):
            return True

        # Some assistants format a provider question as two sentences, e.g.
        # "Which team app do you use? TeamSnap or LeagueApps?". The final
        # actionable segment is then only the option list. In that narrow case,
        # inspect the full message so the choice is still handled as one slot.
        if re.search(r"\b[^?]{2,40}\s+or\s+[^?]{2,40}\?*$", focus_raw, re.IGNORECASE):
            whole = normalize_text(message)
            return any(re.search(pattern, whole, re.IGNORECASE) for pattern in patterns)
        return False

    @classmethod
    def _is_external_resource_request(cls, message: str) -> bool:
        # Only inspect a high-confidence CURRENT request. Earlier sentences may
        # explain that a link will be needed later while the actual current
        # question asks for something else (for example the app/provider name).
        focus = cls._latest_actionable_segment(message)
        normalized = normalize_text(focus)
        if (
            not normalized
            or not cls._is_high_confidence_direct_request(focus)
            or cls._is_provider_choice_request(focus)
        ):
            return False
        patterns = (
            r"\b(?:paste|send|provide|share|enter|drop|give me)\b.{0,100}\b(?:link|url|ics|calendar export|calendar link|verification code|otp|token|access code|invite code|auth code)\b",
            r"\b(?:what is|what's|whats)\b.{0,80}\b(?:link|url|calendar export link|calendar link|verification code|otp|access code|invite code|auth code)\b",
            r"\b(?:need|waiting for|still waiting for)\b.{0,100}\b(?:calendar export link|team calendar link|calendar link|ics link|invite link|verification code|otp|access code|auth code)\b",
        )
        return any(re.search(pattern, normalized, re.IGNORECASE) for pattern in patterns)

    def _select_runtime_resource(
        self,
        test_case: Dict[str, Any],
        latest_assistant: str,
    ) -> tuple[str, Any] | None:
        resources = {
            str(key): value
            for key, value in (test_case.get("_runtime_resources") or {}).items()
            if str(key).strip() and value is not None and value != ""
        }
        if not resources:
            return None

        # Resource selection must follow the CURRENT actionable request, not
        # keywords mentioned earlier in the same assistant response. Example:
        # "TeamSnap calendar processed successfully. What home address should
        # I use?" must select the address fixture, never the TeamSnap URL.
        request_kind = self._resource_request_kind(latest_assistant)
        candidates = list(resources.keys())
        if request_kind == "address":
            candidates = [key for key in candidates if ADDRESS_RESOURCE_KEY_PATTERN.search(key)]
        elif request_kind == "calendar":
            candidates = [
                key for key in candidates
                if not ADDRESS_RESOURCE_KEY_PATTERN.search(key)
                and any(
                    token in normalize_text(key.replace("_", " ").replace("-", " ")).replace("calender", "calendar")
                    for token in ("calendar", "ics", "export", "schedule", "link", "url")
                )
            ]
        elif request_kind in {"code", "authorization", "external"}:
            candidates = [key for key in candidates if not ADDRESS_RESOURCE_KEY_PATTERN.search(key)]

        if not candidates:
            return None

        # Calendar resources are provider-sensitive. If the current case is for
        # GameChanger, never fall back to a TeamSnap URL merely because it is the
        # only calendar fixture configured. A generic fixture is still allowed
        # when the case explicitly references it via required/expected keys.
        if request_kind == "calendar":
            provider = self._active_provider_for_resource(test_case, latest_assistant)
            if provider:
                provider_matches = [
                    key
                    for key in candidates
                    if self._resource_mentions_provider(key, resources.get(key), provider)
                ]
                if provider_matches:
                    candidates = provider_matches
                else:
                    expected = {
                        str(key)
                        for key in (
                            test_case.get("_expected_resource_keys")
                            or test_case.get("required_fixture_keys")
                            or []
                        )
                    }
                    generic_expected = [
                        key
                        for key in candidates
                        if key in expected
                        and not any(
                            self._resource_mentions_provider(key, resources.get(key), other_provider)
                            for other_provider in {
                                str(value or "").strip()
                                for fact_key, value in self._scenario_facts(test_case).items()
                                if self._scenario_key_category(str(fact_key)) == "provider"
                                and str(value or "").strip()
                                and normalize_text(str(value)) != normalize_text(provider)
                            }
                        )
                    ]
                    if generic_expected:
                        candidates = generic_expected
                    else:
                        # Missing provider-specific resource: fall through to
                        # HITL rather than sending a different provider's value.
                        return None

        selected_key = self._best_resource_key(
            candidates,
            test_case,
            latest_assistant,
        )
        if not selected_key:
            return None
        return selected_key, resources[selected_key]

    def _select_expected_resource_key(
        self,
        test_case: Dict[str, Any],
        latest_assistant: str,
    ) -> str:
        candidates = [
            str(key)
            for key in (
                test_case.get("_expected_resource_keys")
                or test_case.get("required_fixture_keys")
                or []
            )
            if str(key).strip()
        ]
        selected = self._best_resource_key(candidates, test_case, latest_assistant)
        if selected:
            return selected

        focus = self._latest_actionable_segment(latest_assistant)
        normalized = normalize_text(focus)
        provider = ""
        for value in self._scenario_facts(test_case).values():
            token = normalize_text(str(value or ""))
            if token and token in normalized and len(token) >= 4:
                provider = re.sub(r"[^a-z0-9]+", "_", token).strip("_")
                break
        if self._is_address_request(latest_assistant):
            return "valid_us_home_address"
        if "calendar" in normalized or "ics" in normalized:
            return f"{provider + '_' if provider else ''}calendar_url"
        if any(word in normalized for word in ("otp", "verification code", "access code", "auth code")):
            return f"{provider + '_' if provider else ''}verification_code"
        return f"{provider + '_' if provider else ''}external_resource"

    @classmethod
    def _resource_request_kind(cls, message: str) -> str:
        """Classify only the current actionable resource request.

        This intentionally ignores descriptive/history text from earlier in the
        same assistant message so prior resources cannot hijack the next reply.
        """
        if cls._is_address_request(message):
            return "address"

        focus = normalize_text(cls._latest_actionable_segment(message))
        if not focus or not cls._is_external_resource_request(message):
            return ""
        if any(token in focus for token in ("calendar", "ics", "export", "schedule link")):
            return "calendar"
        if any(token in focus for token in ("otp", "verification code", "access code", "auth code", "token")):
            return "code"
        if any(token in focus for token in ("oauth", "authorization", "authorisation", "sign in", "login")):
            return "authorization"
        return "external"

    def _best_resource_key(
        self,
        candidates: List[str],
        test_case: Dict[str, Any],
        latest_assistant: str,
    ) -> str:
        if not candidates:
            return ""

        focus = self._latest_actionable_segment(latest_assistant)
        normalized = normalize_text(focus)
        request_kind = self._resource_request_kind(latest_assistant)
        scenario_values = [
            normalize_text(str(value or ""))
            for value in self._scenario_facts(test_case).values()
            if str(value or "").strip()
        ]
        expected = {
            str(key)
            for key in (test_case.get("_expected_resource_keys") or [])
        }

        scored: List[tuple[int, str]] = []
        for key in candidates:
            normalized_key = normalize_text(key.replace("_", " ").replace("-", " ")).replace("calender", "calendar")
            key_words = [word for word in re.findall(r"[a-z0-9]+", normalized_key) if len(word) >= 3]
            score = 0
            key_is_address = bool(ADDRESS_RESOURCE_KEY_PATTERN.search(key))
            key_is_calendar = any(token in normalized_key for token in ("calendar", "ics", "export", "schedule", "link", "url"))
            key_is_code = any(token in normalized_key for token in ("otp", "verification", "code", "token", "auth"))

            # Hard type preference: the current question wins over all historical
            # words in the assistant message. This prevents a previously mentioned
            # TeamSnap link from outranking a newly requested home address.
            if request_kind == "address":
                score += 100 if key_is_address else -100
            elif request_kind == "calendar":
                score += 100 if key_is_calendar and not key_is_address else -100
            elif request_kind == "code":
                score += 100 if key_is_code and not key_is_address else -100
            elif request_kind in {"authorization", "external"} and key_is_address:
                score -= 100

            for word in key_words:
                if word in normalized:
                    score += 4

            for value in scenario_values:
                if len(value) >= 4 and value in normalized_key:
                    # Scenario facts such as sports_app=TeamSnap disambiguate a
                    # generic "send the calendar export link" request even when
                    # the assistant does not repeat the provider name.
                    score += 6
                    if value in normalized:
                        score += 4

            if key in expected:
                score += 2
            if any(token in normalized for token in ("calendar", "ics", "export")) and any(
                token in normalized_key for token in ("calendar", "ics", "export")
            ):
                score += 5
            if any(token in normalized for token in ("otp", "verification", "code", "token")) and any(
                token in normalized_key for token in ("otp", "verification", "code", "token")
            ):
                score += 5
            scored.append((score, key))

        scored.sort(key=lambda item: (-item[0], item[1]))
        best_score = scored[0][0]
        if len(scored) == 1:
            return scored[0][1] if best_score > 0 else ""
        if best_score <= 0:
            return ""
        tied = [key for score, key in scored if score == best_score]
        return tied[0] if len(tied) == 1 else ""

    def _completed_human_action_followup(
        self,
        requested_action: Dict[str, Any],
        human_actions: List[Dict[str, Any]],
    ) -> str:
        """Return a natural continuation when the same HITL action is already done.

        This is deliberately narrow: only completed browser/account actions are
        de-duplicated here. Resource-input actions continue through the existing
        runtime-resource path so exact tester-controlled values are preserved.
        """
        if str(requested_action.get("kind") or "").strip().lower() != "browser_action":
            return ""

        requested_url = self._clean_url(str(requested_action.get("url") or ""))
        requested_title = normalize_text(str(requested_action.get("title") or ""))

        for item in reversed(human_actions):
            if str(item.get("status") or "") != "completed":
                continue
            completed = item.get("action") or {}
            if str(completed.get("kind") or "").strip().lower() != "browser_action":
                continue

            completed_url = self._clean_url(str(completed.get("url") or ""))
            completed_title = normalize_text(str(completed.get("title") or ""))
            same_url = bool(requested_url and completed_url and requested_url == completed_url)
            same_title = bool(requested_title and completed_title and requested_title == completed_title)
            if not (same_url or same_title):
                continue

            note = str(item.get("note") or "").strip()
            if note:
                return note[:500]
            title = str(completed.get("title") or requested_action.get("title") or "that step").strip()
            return f"Done, I completed {title}."[:500]

        return ""

    def _forced_human_action(
        self,
        turns: List[Dict[str, Any]],
        human_actions: List[Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        latest_assistant = next(
            (str(turn.get("content", "")) for turn in reversed(turns) if turn.get("role") == "assistant"),
            "",
        )
        if not latest_assistant:
            return None

        urls = [self._clean_url(url) for url in URL_PATTERN.findall(latest_assistant)]
        urls = [url for url in urls if url]

        normalized = normalize_text(latest_assistant)
        strong_markers = (
            "oauth",
            "authorize",
            "authorization",
            "grant access",
            "consent",
            "sign in",
            "log in",
            "login",
            "choose your account",
            "choose a google account",
            "select your account",
            "connect your google",
            "connect google calendar",
            "verify your account",
        )
        if not any(marker in normalized for marker in strong_markers):
            return None

        # A browser/account step is still human-only when the message says to
        # click/open a link or choose/sign in to an account but the webhook
        # response does not expose the URL itself (for example, the link is sent
        # through another channel). In that case the dashboard pauses without
        # inventing a URL and asks the tester to complete the visible external
        # step on the real device/account.
        browser_action_markers = (
            "click",
            "open",
            "follow the link",
            "choose your account",
            "choose a google account",
            "select your account",
            "sign in",
            "log in",
            "login",
            "grant access",
            "consent",
        )
        if not urls and any(marker in normalized for marker in browser_action_markers):
            completed_without_url = any(
                str(item.get("status") or "") == "completed"
                and not str((item.get("action") or {}).get("url") or "").strip()
                and normalize_text(str((item.get("action") or {}).get("title") or ""))
                == "complete external authorization"
                for item in human_actions
            )
            if completed_without_url:
                return None
            return {
                "kind": "browser_action",
                "resource_key": "",
                "title": "Complete external authorization",
                "instructions": (
                    "Complete the browser/account authorization step shown by the product on the real device/account, "
                    "then return here and continue the same test. No URL was exposed in the captured assistant response."
                ),
                "url": "",
                "requires_input": True,
                "input_label": "What happened after authorization?",
            }

        if not urls:
            return None

        completed_urls = {
            self._clean_url(str((item.get("action") or {}).get("url") or ""))
            for item in human_actions
            if str(item.get("status") or "") == "completed"
        }
        target_url = next((url for url in urls if url not in completed_urls), "")
        if not target_url:
            return None

        return {
            "kind": "browser_action",
            "resource_key": "",
            "title": "Complete external authorization",
            "instructions": (
                "Open the link and complete the account/browser authorization exactly as a real user would. "
                "Return here afterward and continue the same test."
            ),
            "url": target_url,
            "requires_input": True,
            "input_label": "What happened after authorization?",
        }

    @staticmethod
    def _is_entry_journey_case(test_case: Dict[str, Any]) -> bool:
        if str(test_case.get("journey_entry_mode") or "").lower() == "from_start":
            return True
        scope_text = " ".join(
            str(test_case.get(key) or "")
            for key in ("feature", "scope")
        )
        return bool(ENTRY_JOURNEY_SCOPE_PATTERN.search(scope_text))

    @staticmethod
    def _entry_target_overlap(test_case: Dict[str, Any], message: str) -> List[str]:
        source = " ".join(
            str(test_case.get(key) or "")
            for key in ("title", "user_goal")
        ).lower()
        target_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", source)
            if len(token) >= 4 and token not in ENTRY_GOAL_STOPWORDS
        }
        message_tokens = set(re.findall(r"[a-z0-9]+", str(message or "").lower()))
        return sorted(target_tokens & message_tokens)

    @staticmethod
    def _safe_opening_hint(value: Any) -> str:
        """Return behavior guidance, never an unsafe literal/template opener.

        Generated suites may contain old or model-produced hints such as
        "Hi, this is [Your Name]...".  Feeding those hints back into the
        simulator strongly encourages it to copy the template.  Runtime safety
        therefore treats an opening hint as guidance only and replaces unsafe
        template/formal hints with a neutral instruction.
        """
        text = str(value or "").strip()
        if (
            not text
            or TEMPLATE_PLACEHOLDER_PATTERN.search(text)
            or FORMAL_OPENING_PATTERN.search(text)
        ):
            return (
                "Start with a short, natural SMS greeting or a simple statement of intent. "
                "Do not use placeholders, mention onboarding/testing, or volunteer personal/domain facts before they are asked for."
            )
        return text[:240]

    def _message_guard_reason(
        self,
        test_case: Dict[str, Any],
        turns: List[Dict[str, Any]],
        human_actions: List[Dict[str, Any]],
        message: str,
    ) -> str:
        previous_user = next(
            (str(turn.get("content", "")) for turn in reversed(turns) if turn.get("role") == "user"),
            "",
        )
        if previous_user and normalize_text(message) == normalize_text(previous_user):
            return (
                "Your proposed message exactly repeated your previous user message. Respond naturally to the latest assistant turn without copying yourself verbatim."
            )

        if TEMPLATE_PLACEHOLDER_PATTERN.search(message):
            return (
                "Your proposed message contains a template placeholder such as [Your Name] or <name>. "
                "Never send placeholders to the application. Use a real supplied scenario fact only if the application asked for it; otherwise omit that fact."
            )

        invented = self._invented_urls(test_case, turns, human_actions, message)
        if invented:
            return (
                "You invented an external URL/resource that was not supplied by the scenario, the assistant, or a completed human action. "
                "Do not fabricate it. If the application requires a real missing resource, choose human_required instead."
            )

        disclosure_style = str(test_case.get("disclosure_style") or "progressive").lower()
        if disclosure_style == "progressive":
            latest_assistant = self._latest_assistant(turns)
            normalized_message = normalize_text(message)

            if turns and self._is_entry_journey_case(test_case):
                pending = self._entry_pending_categories(test_case, turns)
                latest_category = self._requested_fact_category(latest_assistant) if latest_assistant else ""
                if (
                    pending
                    and latest_category in {"", "generic"}
                    and (LATER_STAGE_CLAIM_PATTERN.search(message) or self._entry_target_overlap(test_case, message))
                ):
                    return (
                        "Do not jump ahead or claim that a later onboarding step has already happened while prerequisite "
                        "facts are still pending. Stay in the current journey and ask/answer only what is needed next."
                    )

            if not turns and self._is_entry_journey_case(test_case):
                if FRESH_ENTRY_FORBIDDEN_PATTERN.search(message):
                    return (
                        "This is a brand-new user. Do not imply prior familiarity ('it's me', 'checking in', 'again') and do not "
                        "announce later setup/onboarding actions in the first SMS. Use a short neutral greeting or identity check."
                    )
                word_count = len(re.findall(r"\b\w+[\w'-]*\b", message))
                if word_count > 14:
                    return (
                        "This onboarding/initial-entry test must begin like a normal new user. Keep the first SMS very short "
                        "and let the assistant lead the onboarding steps instead of describing the eventual test goal."
                    )

                new_keys = self._newly_disclosed_keys(self._scenario_facts(test_case), turns, message)
                if new_keys:
                    return (
                        "Do not reveal scenario facts in the first message of this onboarding/initial-entry test. "
                        "Start with a short greeting or identity check, then provide name, child/entity, category, provider, links, "
                        "address, or other facts only when the assistant asks for them."
                    )

                target_overlap = self._entry_target_overlap(test_case, message)
                if target_overlap:
                    return (
                        "Do not jump directly to the later behavior under test in the opening message. "
                        "The user goal is the eventual destination, not a literal first-turn request. Start normally and let the "
                        "assistant guide the conversation through prerequisite onboarding steps."
                    )

            for resource_key, resource_value in (test_case.get("_runtime_resources") or {}).items():
                resource_text = str(resource_value or "").strip()
                if not resource_text or normalize_text(resource_text) not in normalized_message:
                    continue
                if URL_PATTERN.search(resource_text) and not self._is_external_resource_request(latest_assistant):
                    return (
                        "Do not send the saved URL yet. The assistant has not explicitly asked for the link/value in its latest actionable question. "
                        "If it asked which app/platform/provider is used, answer only that provider fact now."
                    )
                if ADDRESS_RESOURCE_KEY_PATTERN.search(str(resource_key)) and not self._is_address_request(latest_assistant):
                    return (
                        "Do not volunteer the saved physical address before the assistant asks for the address. "
                        "Reveal only the information requested in the current onboarding step."
                    )

            if not turns and str(test_case.get("state_mode") or "fresh_user") == "fresh_user":
                word_count = len(re.findall(r"\b\w+[\w'-]*\b", message))
                if FORMAL_OPENING_PATTERN.search(message) or word_count > 24:
                    return (
                        "This is the first message of a fresh-user progressive conversation. Keep it short and natural like an SMS greeting or simple intent. "
                        "Do not use business-email language, announce an 'onboarding process', or provide a long introduction."
                    )

            new_keys = self._newly_disclosed_keys(self._scenario_facts(test_case), turns, message)
            if not turns and len(new_keys) > 1:
                return (
                    "This is a progressive-disclosure scenario. Your opening message revealed too many scenario facts at once. "
                    "State the goal naturally and reveal at most one scenario fact unless absolutely necessary."
                )
            if turns and len(new_keys) > 2:
                return (
                    "You are volunteering several new scenario facts at once. Answer the assistant's latest question naturally and reveal only the information needed now."
                )
        return ""

    def _invented_urls(
        self,
        test_case: Dict[str, Any],
        turns: List[Dict[str, Any]],
        human_actions: List[Dict[str, Any]],
        message: str,
    ) -> List[str]:
        proposed = {self._clean_url(url) for url in URL_PATTERN.findall(message)}
        if not proposed:
            return []

        allowed: set[str] = set()
        for value in self._scenario_facts(test_case).values():
            if isinstance(value, str):
                allowed.update(self._clean_url(url) for url in URL_PATTERN.findall(value))
        for value in (test_case.get("_runtime_resources") or {}).values():
            if isinstance(value, str):
                allowed.update(self._clean_url(url) for url in URL_PATTERN.findall(value))
        for turn in turns:
            allowed.update(self._clean_url(url) for url in URL_PATTERN.findall(str(turn.get("content", ""))))
        for item in human_actions:
            allowed.update(self._clean_url(url) for url in URL_PATTERN.findall(str(item.get("note", ""))))
            action = item.get("action") or {}
            allowed.update(self._clean_url(url) for url in URL_PATTERN.findall(str(action.get("url", ""))))

        return sorted(url for url in proposed if url and url not in allowed)

    def _sanitize_human_action(
        self,
        action: Dict[str, Any],
        turns: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        latest_assistant = next(
            (str(turn.get("content", "")) for turn in reversed(turns) if turn.get("role") == "assistant"),
            "",
        )
        allowed_urls = {self._clean_url(url) for url in URL_PATTERN.findall(latest_assistant)}
        requested_url = self._clean_url(str(action.get("url") or ""))
        if requested_url not in allowed_urls:
            requested_url = ""
        return {
            "kind": str(action.get("kind") or "other").strip().lower()
            if str(action.get("kind") or "").strip().lower() in {"resource_input", "browser_action", "other"}
            else "other",
            "resource_key": str(action.get("resource_key") or "").strip()[:120],
            "title": str(action.get("title") or "").strip()[:160],
            "instructions": str(action.get("instructions") or "").strip()[:1200],
            "url": requested_url,
            "requires_input": bool(action.get("requires_input")),
            "input_label": str(action.get("input_label") or "").strip()[:120],
        }

    def _disclosure_status(
        self,
        scenario_data: Dict[str, Any],
        turns: List[Dict[str, Any]],
    ) -> Dict[str, List[str]]:
        user_text = normalize_text(
            " ".join(str(turn.get("content", "")) for turn in turns if turn.get("role") == "user")
        )
        disclosed: List[str] = []
        undisclosed: List[str] = []
        for key, value in scenario_data.items():
            text = str(value or "").strip()
            token = normalize_text(text)
            if token and len(token) >= 3 and token in user_text:
                disclosed.append(str(key))
            else:
                undisclosed.append(str(key))
        return {"disclosed": disclosed, "undisclosed": undisclosed}

    def _newly_disclosed_keys(
        self,
        scenario_data: Dict[str, Any],
        turns: List[Dict[str, Any]],
        proposed_message: str,
    ) -> List[str]:
        before = set(self._disclosure_status(scenario_data, turns)["disclosed"])
        proposed = normalize_text(proposed_message)
        result: List[str] = []
        for key, value in scenario_data.items():
            if str(key) in before:
                continue
            text = str(value or "").strip()
            token = normalize_text(text)
            if len(token) >= 3 and token in proposed:
                result.append(str(key))
        return result

    def _human_history(self, human_actions: List[Dict[str, Any]]) -> str:
        if not human_actions:
            return "(No human actions have been completed.)"
        lines: List[str] = []
        for item in human_actions[-6:]:
            status = str(item.get("status") or "")
            action = item.get("action") or {}
            title = str(action.get("title") or "Human action")
            note = str(item.get("note") or "").strip()
            lines.append(f"- {title}: {status}" + (f"; tester input: {note}" if note else ""))
        return "\n".join(lines)

    def _compact_transcript(self, turns: List[Dict[str, Any]]) -> str:
        if not turns:
            return "(No messages yet.)"
        lines = [
            f"{'USER' if turn.get('role') == 'user' else 'ASSISTANT'}: {turn.get('content', '')}"
            for turn in turns
        ]
        return "\n".join(lines)[-self.config.max_prompt_chars :]

    @staticmethod
    def _clean_url(value: str) -> str:
        return value.rstrip(".,;:!?)>\"")


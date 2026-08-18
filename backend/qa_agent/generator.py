from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Dict, List, Tuple

from .ai_client import AIClient, UsageCollector
from .config import Settings, settings
from .documents import DocumentService
from .utils import new_id, normalize_text, slugify, utc_now

TEST_TYPES = [
    "happy-path",
    "negative",
    "validation",
    "boundary",
    "recovery",
    "context-retention",
    "interruption",
    "integration",
    "idempotency",
    "data-integrity",
    "persona-variation",
]
STATE_MODES = ["fresh_user", "returning_user", "continuation"]
DISCLOSURE_STYLES = ["progressive", "concise", "verbose"]
FIXTURE_PATTERN = re.compile(r"^\{FIXTURE:([A-Za-z0-9_.-]+)\}$")
URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)
CONTROLLED_RESOURCE_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:url|link|ics|webhook|invite|calendar_export|export_link|authorization_url|oauth_url|file_path|account_id|external_id)(?:$|_)",
    re.IGNORECASE,
)
SENSITIVE_RESOURCE_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:otp|token|verification_code|access_code|invite_code|auth_code)(?:$|_)",
    re.IGNORECASE,
)
PHYSICAL_ADDRESS_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:address|home_address|street_address|physical_address|mailing_address|service_address|origin_address|destination_address|geocodable_address)(?:$|_)",
    re.IGNORECASE,
)
ENTRY_OPENING_LATER_STEP_PATTERN = re.compile(
    r"\b(?:calendar|ics|oauth|authorization|invite|export|url|link|address|token|otp|verification\s+code|upload)\b",
    re.IGNORECASE,
)
ENTRY_JOURNEY_SCOPE_PATTERN = re.compile(
    r"\b(?:onboarding|initial\s+conversation|initial\s+setup|first[-\s]?time|new\s+user|new\s+parent|registration|sign[-\s]?up|activation|setup\s+journey)\b",
    re.IGNORECASE,
)

ENTRY_FACT_CATEGORIES = [
    "parent_name",
    "child_count",
    "child_name",
    "sport",
    "provider",
    "team_name",
    "email",
    "phone",
]

ENTRY_CANONICAL_KEYS = {
    "parent_name": "parent_name",
    "child_count": "child_count",
    "child_name": "child_name",
    "sport": "sport",
    "provider": "sports_app",
    "team_name": "team_name",
    "email": "email",
    "phone": "phone",
}

CASE_COUNT_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}


class TestGenerator:
    """Three-stage production generator: requirements -> inventory -> one audit."""

    __test__ = False

    def __init__(
        self,
        documents: DocumentService,
        ai: AIClient | None = None,
        config: Settings = settings,
    ):
        self.documents = documents
        self.ai = ai or AIClient(config)
        self.config = config

    def generate(
        self,
        project: Dict[str, Any],
        feature: str,
        query: str,
        progress=None,
        generation_prompt: str | None = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        usage = UsageCollector()
        scope = feature.strip() or "Full product"
        focus = query.strip()
        # Direct/legacy callers historically used `query` as the tester's focus.
        # The API now passes generation_prompt explicitly so an empty dashboard prompt
        # can still use a broad retrieval query without pretending the tester asked for it.
        manual_request = focus if generation_prompt is None else generation_prompt.strip()
        prompt_targeted_generation = bool(manual_request)
        requested_case_count = self._extract_requested_case_count(manual_request)
        if requested_case_count is not None and requested_case_count > self.config.max_generated_cases:
            raise ValueError(
                f"The generation prompt requests {requested_case_count} test cases, but this project is configured "
                f"to generate at most {self.config.max_generated_cases} per suite. Request "
                f"{self.config.max_generated_cases} or fewer test cases."
            )
        case_limit = requested_case_count or self.config.max_generated_cases
        exact_case_count_guidance = (
            f"EXACT CASE COUNT RULE: The tester explicitly requested exactly {requested_case_count} test cases. "
            f"Return exactly {requested_case_count} distinct, runnable, documentation-grounded test_cases. "
            "Do not silently reduce the requested count because several cases exercise the same requirement. "
            "When the requested family is narrow (for example happy paths), create legitimate supported variations "
            "using different safe user facts, personas, starting-message guidance, or documented paths while keeping "
            "the requested case family intact. Do not invent unsupported behavior merely to reach the count. "
            if requested_case_count is not None
            else f"Create up to {self.config.max_generated_cases} non-duplicative production cases. "
        )
        coverage_gate_guidance = (
            "A USER TEST GENERATION REQUEST is present. Treat this as a targeted suite: cover the documented "
            "requirements needed for the requested case families and their execution prerequisites, but do not add or "
            "force unrelated requirements merely because they are High risk elsewhere in the selected product scope. "
            "Every requirement explicitly exercised by a generated case must still be documentation-grounded."
            if prompt_targeted_generation
            else "No USER TEST GENERATION REQUEST is present. This is broad scope generation: every extracted High-risk requirement must be covered."
        )
        prompt_contract_guidance = (
            "DASHBOARD PROMPT CONTRACT: The USER TEST GENERATION REQUEST is authoritative for the requested suite shape. "
            "Honor its explicit case count, positive/negative/recovery mix, fresh/returning state, opening-message variations, "
            "partial/invalid/corrected input behavior, multi-entity variations, same/different resource relationships, and any "
            "other requested scenario constraints whenever the documentation supports the underlying product behavior. "
            "Documentation constrains what the PRODUCT is expected to do; it must not be used to silently rewrite a requested "
            "invalid-user-input case into a happy path or to replace a requested early ordinary fact with a greeting-only opener. "
            "Invalid or partial USER INPUT is valid test data when the request asks for it. The test-case STRUCTURE must still be "
            "runnable, independent, and grounded.\n"
            if prompt_targeted_generation
            else ""
        )
        entry_journey_scope = self._is_entry_journey_scope(scope)
        entry_journey_guidance = (
            (
                "ENTRY-JOURNEY EXECUTION RULE: This scope represents onboarding/initial setup. Every test case must be "
                "independently executable from a brand-new user identity. Fresh user means no prior stored onboarding state; "
                "it does NOT mean the first message must always be greeting-only. When the dashboard request explicitly asks "
                "for a parent/user to volunteer safe ordinary facts early (for example their name, child count, or another "
                "non-resource onboarding fact), preserve that requested natural behavior. Still do not create direct-entry "
                "later-step openers that claim a downstream action already happened or that volunteer real URLs, calendar/ICS "
                "links, OAuth tokens, addresses, OTPs, or other controlled resources before the product actually requests them. "
                "Even when a case targets a later onboarding step, start from fresh_user and progress through the journey. "
                "Keep eventual valid prerequisite facts available in scenario_data so recovery cases remain runnable.\n"
            )
            if entry_journey_scope and prompt_targeted_generation
            else (
                "ENTRY-JOURNEY EXECUTION RULE: This scope represents onboarding/initial setup. Every test case must be "
                "independently executable from a brand-new user identity. Even when a case targets a later onboarding step, "
                "the simulated user must enter through the normal beginning of the conversation and naturally progress through "
                "all prerequisite questions before exercising the target behavior. Do not create direct-entry later-step openers "
                "such as asking to connect a calendar, upload a link, change a downstream setting, or complete a later action in "
                "the first message. Do not use returning_user or continuation for these independent onboarding cases. Include in "
                "scenario_data all ordinary prerequisite facts needed to reach the target step (for example names, counts, categories, "
                "provider choices), while keeping real external resources as fixture references. The first user turn should only be a "
                "short natural greeting/identity check/simple request for help; the product should drive the onboarding sequence.\n"
            )
            if entry_journey_scope
            else ""
        )

        if progress:
            progress(8, "Retrieving requirements and risk evidence...")
        retrieval_queries = [
            focus or f"{scope} product requirements behavior acceptance criteria",
            f"{scope} successful user journeys state transitions required behavior acceptance criteria",
            f"{scope} invalid missing ambiguous corrected repeated out-of-order input recovery context retention edge cases",
            f"{scope} integration failures retries timeouts duplicate actions idempotency data integrity boundaries human authorization consent",
        ]
        context, source_refs = self.documents.retrieve_many(project["id"], retrieval_queries)
        context = context[: self.config.max_prompt_chars]

        if progress:
            progress(20, "Extracting atomic requirements and risks...")
        requirement_result = self.ai.structured(
            model=self.config.generation_model,
            system=(
                "You are a principal production QA architect. Extract atomic, observable, documentation-grounded "
                "requirements and risks. Do not invent behavior. The evidence may contain planning, POC, beta, or "
                "launch documents from different stages. Do not silently reconcile conflicting statements: record "
                "meaningful conflicts so a reviewer can decide which source is authoritative. "
                "For onboarding/initial-entry scopes, also identify the ordinary user facts the product collects before "
                "downstream actions and provide safe synthetic DEFAULT test values for those facts. These defaults are "
                "test data only, never opening-message scripts. Do not put real URLs, physical addresses, secrets, tokens, "
                "OTP values, or account-specific identifiers in the defaults. Provider/category values must be grounded in "
                "the supplied evidence or configured test-resource names; ordinary names/counts may be synthetic."
            ),
            user=(
                f"PROJECT\n{project['name']}\n\n"
                f"SCOPE\n{scope}\n\n"
                f"USER TEST GENERATION REQUEST\n{manual_request or '(none - cover the documented scope broadly)'}\n\n"
                f"DOCUMENT EVIDENCE\n{context}\n\n"
                "Extract only behavior that can be tested from observable system responses or externally visible effects. "
                "When a USER TEST GENERATION REQUEST is present, treat it as a strong selection/emphasis instruction: "
                "extract the documented requirements needed for those requested scenarios plus any prerequisites required to "
                "execute them correctly. Do not invent unsupported product behavior merely because the tester requested it. "
                "When no request is present, cover the documented scope broadly. "
                "Risk must be High, Medium, or Low. Choose only applicable test types from: "
                + ", ".join(TEST_TYPES)
                + "\n\nFor entry/onboarding scopes, entry_journey_defaults should use canonical categories only: "
                + ", ".join(ENTRY_FACT_CATEGORIES)
                + ". Return [] when a category is not documented or the scope is not an entry journey."
            ),
            schema_name="qa_requirements",
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "suite_name": {"type": "string"},
                    "requirements": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                                "risk": {"type": "string", "enum": ["High", "Medium", "Low"]},
                                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["title", "description", "risk", "acceptance_criteria"],
                        },
                    },
                    "risk_areas": {"type": "array", "items": {"type": "string"}},
                    "applicable_test_types": {"type": "array", "items": {"type": "string", "enum": TEST_TYPES}},
                    "documentation_conflicts": {"type": "array", "items": {"type": "string"}},
                    "entry_journey_defaults": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "category": {"type": "string", "enum": ENTRY_FACT_CATEGORIES},
                                "value": {"type": "string"},
                            },
                            "required": ["category", "value"],
                        },
                    },
                },
                "required": [
                    "suite_name",
                    "requirements",
                    "risk_areas",
                    "applicable_test_types",
                    "documentation_conflicts",
                    "entry_journey_defaults",
                ],
            },
        )
        usage.add(requirement_result.usage)

        raw_requirements = requirement_result.value.get("requirements", [])
        if not raw_requirements:
            raise ValueError("No testable requirements could be extracted from the indexed documentation.")

        prefix = re.sub(r"[^A-Za-z0-9]", "", scope.upper())[:4] or "GEN"
        requirements: List[Dict[str, Any]] = []
        for index, item in enumerate(raw_requirements, start=1):
            acceptance = [str(x).strip() for x in item.get("acceptance_criteria", []) if str(x).strip()]
            requirements.append(
                {
                    "id": f"REQ-{prefix}-{index:03d}",
                    "title": str(item.get("title") or "").strip(),
                    "description": str(item.get("description") or "").strip(),
                    "risk": item.get("risk", "Medium"),
                    "acceptance_criteria": acceptance,
                }
            )

        fixtures = project.get("fixtures") or {}
        fixture_keys = sorted(str(key) for key in fixtures.keys())
        entry_required_fact_categories = (
            self._entry_fact_categories_from_evidence(
                requirements,
                context,
                fixture_keys,
                requirement_result.value.get("entry_journey_defaults", []),
            )
            if entry_journey_scope
            else []
        )
        entry_default_facts = (
            self._normalize_entry_defaults(
                requirement_result.value.get("entry_journey_defaults", []),
                entry_required_fact_categories,
            )
            if entry_journey_scope
            else {}
        )
        entry_fact_guidance = (
            "ENTRY-JOURNEY FACT COMPLETENESS RULE: Documentation for this scope indicates these ordinary "
            "prerequisite fact categories: "
            + json.dumps(entry_required_fact_categories)
            + ". Suite-level safe baseline facts available for filling prerequisites are: "
            + json.dumps(entry_default_facts, ensure_ascii=False)
            + ". Every independent entry/onboarding case must include one concrete scenario_data value for each "
            "listed category, even when the case's main objective is a later behavior such as tone, recovery, "
            "integration, or validation. These facts are test data, not opening-message scripts; progressive "
            "disclosure still applies. Never reuse a value from a different category (for example a person's name "
            "as an app/provider answer). Real URLs, addresses, tokens, and other controlled resources remain fixture "
            "references rather than invented scenario values.\n"
            if entry_required_fact_categories
            else ""
        )

        if progress:
            progress(40, "Designing production test cases...")
        generation_result = self.ai.structured(
            model=self.config.generation_model,
            system=(
                "You are a senior black-box QA engineer. Design independent, high-value conversational test cases. "
                "The inventory must be reusable across domains. Never assume sports, parents, ecommerce, healthcare, "
                "or another domain unless the supplied requirements support it. Personas describe human behavior/style; "
                "identity and domain facts belong in scenario_data."
            ),
            user=(
                f"PROJECT\n{project['name']}\n\n"
                f"SCOPE\n{scope}\n\n"
                f"USER TEST GENERATION REQUEST\n{manual_request or '(none - use the normal production coverage strategy)'}\n\n"
                f"REQUIREMENTS\n{json.dumps(requirements, ensure_ascii=False)}\n\n"
                f"RISK AREAS\n{json.dumps(requirement_result.value.get('risk_areas', []), ensure_ascii=False)}\n\n"
                f"APPLICABLE TEST TYPES\n{json.dumps(requirement_result.value.get('applicable_test_types', []))}\n\n"
                f"AVAILABLE TEST RESOURCE KEYS\n{json.dumps(fixture_keys)}\n\n"
                f"EVIDENCE\n{context}\n\n"
                + exact_case_count_guidance
                + prompt_contract_guidance
                + "If the USER TEST GENERATION REQUEST is present, honor the requested scenario mix, user state, personas, "
                "data variations, edge conditions, and emphasis wherever they are supported by the documented requirements. "
                "The tester's request controls WHAT KIND of cases to prioritize; the documentation remains the source of truth "
                "for expected product behavior. Do not add unrelated case families merely to broaden the suite. "
                + coverage_gate_guidance
                + " As many relevant documented Medium/Low requirements as practical may be covered when they fit the requested suite. Include happy paths and only "
                "applicable negative, invalid, missing, ambiguous, corrected, out-of-order, repeated-message, recovery, "
                "context-retention, interruption/resume, integration, boundary, idempotency and data-integrity behavior.\n"
                "Separate USER GOAL from EXPECTED SYSTEM RESULT. user_goal is only what the human wants to accomplish. "
                "expected_result is only what the product should observably do. Never leak expected behavior into persona, "
                "user_goal, opening hint or scenario facts.\n"
                "For normal cases use disclosure_style=progressive. That means the simulated human should usually reveal facts as "
                "the product asks for them rather than dumping the entire scenario in the opening message. However, an explicit "
                "dashboard request overrides that DEFAULT for safe ordinary facts: if the tester asks for openings where the user "
                "volunteers their name, count, or other non-resource onboarding information, generate those variations. Use verbose "
                "when the requested test intentionally validates oversharing or multi-fact input. When the tester asks for varied "
                "starting messages, make initial_message_hint meaningfully distinct across cases while keeping each opener natural. "
                "Never volunteer later-step controlled resources such as URLs, addresses, OAuth values, tokens, OTPs, or external IDs.\n"
                "OPENING HINTS are behavior guidance, not literal scripts. Never put template placeholders such as [Your Name], "
                "[Child Name], <name>, INSERT NAME, TBD, or similar tokens in the opening hint or scenario values. For fresh-user "
                "progressive cases, the opening should be a short natural SMS greeting/simple intent; do not write formal phrases like "
                "'start the onboarding process' or 'looking forward to working together'.\n"
                "Keep functional test titles focused on observable behavior. If documentation contains a response-time/SLA target, "
                "record it in rule_assertions.max_response_ms; do not mix latency wording into a normal functional test title.\n"
                "state_mode should normally be fresh_user; use returning_user or continuation only when the requirement "
                "specifically needs existing state/history.\n"
                + entry_journey_guidance
                + entry_fact_guidance
                + "CANONICAL ONBOARDING FACT KEYS: when these categories apply, prefer parent_name, child_count, child_name, "
                "sport, sports_app, team_name, email, and phone. Do not use ambiguous keys such as first_name when it is unclear "
                "whether it belongs to the parent or child. For negative/validation/recovery cases, distinguish the eventual "
                "stable fact from the user's intentionally bad attempt. Keep eventual valid facts available so the case can recover "
                "and finish; describe the natural bad-first/correct-later behavior clearly in persona/preconditions and, where useful, "
                "store auxiliary values under explicit keys such as invalid_attempt_* or partial_attempt_*. Do not omit every usable "
                "fact and expect the simulator to invent one. A later-stage test objective does not mean the user may claim that "
                "prerequisite steps already happened; fresh-user cases must reach the target step naturally.\n"
                "MULTI-CHILD CONTRACT: if child_count is 2 or more, include explicit per-child facts for every child. Use child_1_name, "
                "child_1_sport, child_2_name, child_2_sport, etc. Child names must be distinct. If providers differ by child, use "
                "child_1_sports_app, child_2_sports_app, etc.; otherwise a shared sports_app may be used. Never create a multi-child "
                "test with only one child name or one child's sport.\n"
                + "REAL TEST RESOURCES: URLs, calendar/ICS links, OAuth/invite links, account-specific identifiers, tokens, OTPs, "
                "codes and other external values that must actually work MUST NOT be invented. Physical addresses used by the product "
                "for geocoding, timezone, distance, travel, weather, maps, delivery, or similar real-world calculations are also tester-controlled "
                "resources in positive flows; do not invent a plausible-looking address. Negative/validation cases may intentionally use malformed "
                "or incomplete addresses when that is the behavior under test. If a suitable resource key "
                "exists, put exactly {FIXTURE:key} in scenario_data and list the key in required_fixture_keys. If no suitable "
                "resource key exists but a positive test may need one, create a clear lowercase snake_case resource key, use "
                "{FIXTURE:new_key}, and list it. Missing resources do NOT block test startup: execution continues naturally "
                "until the application actually asks for the value, then uses a matching saved project resource or pauses "
                "for tester input at that exact turn.\n"
                "Human-in-the-loop browser/account actions such as Google OAuth consent may be part of a valid test. Do not "
                "fake completion in scenario data; the runtime will pause when the target actually asks for such an action.\n"
                "DETERMINISTIC ASSERTIONS: only use literal_required_all/literal_required_any/literal_forbidden when exact text "
                "or a literal token is truly required. Concepts like 'asked for parent name', 'gave app instructions', task "
                "completion, context retention and recovery are semantic and MUST NOT be placed in literal arrays. Use a final "
                "regex only for an actual documented format and set enforce_final_response_regex accordingly. min_user_turns is "
                "normally advisory. max_response_ms records a documented response-time target if one exists; the runtime "
                "performance policy decides whether it is advisory or enforced."
            ),
            schema_name="qa_test_inventory",
            schema=self._inventory_schema(),
        )
        usage.add(generation_result.usage)
        raw_inventory_cases = generation_result.value.get("test_cases", []) or []
        entry_baseline_facts = (
            self._build_entry_baseline_facts(
                entry_default_facts,
                raw_inventory_cases,
                entry_required_fact_categories,
                fixture_keys,
                context,
            )
            if entry_journey_scope
            else {}
        )
        generation_rejections: List[Dict[str, Any]] = []
        cases = self._normalize_cases(
            scope,
            raw_inventory_cases,
            requirements,
            fixtures,
            required_entry_fact_categories=entry_required_fact_categories,
            entry_baseline_facts=entry_baseline_facts,
            rejection_sink=generation_rejections,
            prompt_targeted_generation=prompt_targeted_generation,
        )
        if requested_case_count is not None:
            # An explicit tester count is a hard contract. Count only distinct runnable cases so
            # duplicate model output cannot make a 5-case request look satisfied.
            cases = self._deduplicate(cases)[:case_limit]

        remaining = max(0, case_limit - len(cases))
        configured_audit_timeout = float(getattr(self.config, "generation_audit_timeout_seconds", 60.0) or 60.0)
        general_ai_timeout = float(getattr(self.config, "ai_timeout_seconds", 120.0) or 120.0)
        audit_timeout = max(15.0, min(configured_audit_timeout, general_ai_timeout))
        prompt_budget = int(getattr(self.config, "max_prompt_chars", 28000) or 28000)
        audit_evidence_chars = min(7000, max(2000, prompt_budget // 4))
        audit_evidence = context[:audit_evidence_chars]
        audit_value: Dict[str, Any] = {
            "missing_cases": [],
            "duplicate_case_titles": [],
            "noncompliant_case_titles": [],
            "notes": [],
        }
        audit_status = "completed"
        audit_error = ""

        if progress:
            progress(70, f"Final AI audit (bounded to {int(audit_timeout)} seconds)...")
        try:
            audit_result = self.ai.structured(
                model=self.config.generation_model,
                system=(
                    "You are an independent production QA reviewer. Audit the inventory for missing requirement coverage, "
                    "missing serious edge/recovery risks, unsafe invented external resources, semantic assertions incorrectly "
                    "encoded as literal checks, and duplicates. Return only genuinely missing cases. The extracted requirements "
                    "are the authoritative documentation-grounded contract for this audit."
                ),
                user=(
                    f"USER TEST GENERATION REQUEST\n{manual_request or '(none - normal production coverage)'}\n\n"
                    f"REQUIREMENTS\n{json.dumps(requirements, ensure_ascii=False)}\n\n"
                    f"AVAILABLE TEST RESOURCE KEYS\n{json.dumps(fixture_keys)}\n\n"
                    f"EXISTING CASES\n{json.dumps(self._audit_projection(cases), ensure_ascii=False)}\n\n"
                    f"ENTRY CONTRACT\nrequired categories={json.dumps(entry_required_fact_categories)}; "
                    f"baseline facts={json.dumps(entry_baseline_facts, ensure_ascii=False)}; "
                    f"initial candidates rejected by scenario-quality gate={max(0, len(raw_inventory_cases) - len(cases))}\n\n"
                    f"REJECTED CANDIDATES AND EXACT VALIDATION REASONS\n"
                    f"{json.dumps(generation_rejections[:30], ensure_ascii=False)}\n\n"
                    f"EVIDENCE EXCERPT\n{audit_evidence}\n\n"
                    +(
                        f"The tester requested exactly {requested_case_count} test cases and the current validated inventory "
                        f"contains {len(cases)}. First audit every existing case against the dashboard request. Put the exact titles "
                        "of materially noncompliant cases in noncompliant_case_titles. Then return enough distinct missing_cases "
                        f"to replace those cases AND fill any count shortfall so the final inventory contains exactly {requested_case_count}. "
                        "A materially noncompliant case is one that violates an explicit requested family/state/input/opening/data "
                        "constraint, not merely a stylistic preference. Preserve the requested scenario family and vary only "
                        "documentation-supported paths, safe facts, personas, or natural opening-message guidance. "
                        if requested_case_count is not None
                        else f"Return at most {remaining} genuinely missing cases. The final inventory cannot exceed "
                        f"{self.config.max_generated_cases} cases. "
                    )
                    + "When a USER TEST GENERATION REQUEST is present, audit "
                    "against that requested scenario emphasis as well as the documented requirements; do not broaden the "
                    "suite with unrelated cases. Treat the dashboard request as the authoritative suite-design contract. "
                    "If candidates were rejected, use the exact validation reasons above to REPAIR or REPLACE them rather "
                    "than changing the requested negative/partial/correction scenario into a happy path. Invalid USER INPUT "
                    "is allowed when it is the behavior under test; malformed TEST STRUCTURE is not. Apply the same "
                    "progressive-disclosure, real-resource, human-action, "
                    "canonical-slot, and literal-vs-semantic rules used in generation. Any replacement onboarding case must "
                    "be independently executable from fresh_user state and must carry every required ordinary prerequisite fact. "
                    "For child_count >= 2, include distinct per-child names and per-child sports using child_1_name/child_1_sport, "
                    "child_2_name/child_2_sport, etc. Do not create cases that claim an ICS/calendar/address/OAuth step has "
                    "already happened before the product reaches it. "
                    + entry_journey_guidance
                    + entry_fact_guidance
                ),
                schema_name="qa_test_audit",
                schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "missing_cases": self._inventory_schema()["properties"]["test_cases"],
                        "duplicate_case_titles": {"type": "array", "items": {"type": "string"}},
                        "noncompliant_case_titles": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["missing_cases", "duplicate_case_titles", "noncompliant_case_titles", "notes"],
                },
                timeout_seconds=audit_timeout,
                max_retries=0,
            )
            usage.add(audit_result.usage)
            audit_value = audit_result.value
        except Exception as exc:
            # The final audit improves coverage but must not hold a valid suite at 70% for multiple
            # global timeout/retry cycles. Deterministic normalization still runs below. Broad generation
            # keeps the full-scope High-risk gate; a manual prompt uses the targeted coverage policy.
            audit_status = "skipped_after_error"
            audit_error = str(exc).strip()[:240] or exc.__class__.__name__
            audit_value = {
                "missing_cases": [],
                "duplicate_case_titles": [],
                "noncompliant_case_titles": [],
                "notes": [
                    "Final AI audit was unavailable after its bounded request; the initial inventory was "
                    + (
                        "kept only if it passed deterministic validation and the targeted-prompt coverage policy."
                        if prompt_targeted_generation
                        else "kept only if it passed deterministic validation and the full-scope High-risk coverage gate."
                    )
                ],
            }
            if progress:
                progress(84, "Final audit unavailable; validating the generated inventory safely...")

        audit_rejections: List[Dict[str, Any]] = []
        if prompt_targeted_generation:
            noncompliant_titles = {
                normalize_text(str(title))
                for title in audit_value.get("noncompliant_case_titles", []) or []
                if str(title).strip()
            }
            if noncompliant_titles:
                cases = [
                    case for case in cases
                    if normalize_text(str(case.get("title") or "")) not in noncompliant_titles
                ]
        remaining = max(0, case_limit - len(cases))
        if remaining:
            raw_additions = audit_value.get("missing_cases", [])[:remaining] or []
            if entry_journey_scope and raw_additions:
                repaired_baseline = self._build_entry_baseline_facts(
                    entry_baseline_facts,
                    list(raw_inventory_cases) + list(raw_additions),
                    entry_required_fact_categories,
                    fixture_keys,
                    context,
                )
                entry_baseline_facts.update(repaired_baseline)
            additions = self._normalize_cases(
                scope,
                raw_additions,
                requirements,
                fixtures,
                start=len(cases) + 1,
                required_entry_fact_categories=entry_required_fact_categories,
                entry_baseline_facts=entry_baseline_facts,
                rejection_sink=audit_rejections,
                prompt_targeted_generation=prompt_targeted_generation,
            )
            cases = self._deduplicate(cases + additions)[:case_limit]

        if requested_case_count is not None and len(cases) != requested_case_count:
            reason_counts = Counter(
                reason
                for item in (generation_rejections + audit_rejections)
                for reason in item.get("reasons", [])
                if str(reason).strip()
            )
            reason_detail = ""
            if reason_counts:
                reason_detail = " Most common validation reasons: " + "; ".join(
                    f"{reason} ({count})" for reason, count in reason_counts.most_common(6)
                ) + "."
            raise ValueError(
                f"Generation count requirement was not satisfied. The prompt requested exactly "
                f"{requested_case_count} distinct runnable test cases, but {len(cases)} passed generation validation. "
                "No partial suite was saved."
                + reason_detail
                + " The dashboard prompt remains the requested suite contract; retrying should not require weakening it."
            )

        if not cases:
            detail = (
                " Required ordinary entry facts: " + ", ".join(entry_required_fact_categories) + "."
                if entry_required_fact_categories
                else ""
            )
            raise ValueError(
                "The generator did not produce any runnable documentation-grounded test cases with complete scenario data."
                + detail
            )

        covered = {req_id for case in cases for req_id in case.get("requirement_ids", [])}
        uncovered = [r["id"] for r in requirements if r["id"] not in covered]
        high_gaps = [r["id"] for r in requirements if r["risk"] == "High" and r["id"] not in covered]
        # A manual generation prompt intentionally defines a narrower suite. In that mode, uncovered
        # requirements outside the requested case family are reported but do not block generation.
        # Blank-prompt/documentation-driven generation keeps the original strict full-scope High-risk gate.
        if high_gaps and not prompt_targeted_generation:
            raise ValueError(
                "Generation quality gate failed. Uncovered High-risk requirements: " + ", ".join(high_gaps)
            )

        required_fixture_keys = sorted(
            {
                key
                for case in cases
                for key in case.get("required_fixture_keys", [])
                if str(key).strip()
            }
        )
        missing_fixture_keys = [key for key in required_fixture_keys if key not in fixtures]

        suite = {
            "id": new_id("suite"),
            "project_id": project["id"],
            "name": generation_result.value.get("suite_name")
            or requirement_result.value.get("suite_name")
            or f"{scope} QA suite",
            "feature": scope,
            "version": 1,
            "status": "draft",
            "approved": False,
            "review_note": "",
            "requirements": requirements,
            "test_cases": cases,
            "source_query": focus,
            "generation_request": manual_request,
            "source_refs": source_refs,
            "generation_summary": {
                "strategy": "requirements-generation-single-audit",
                "model": self.config.generation_model,
                "requirement_count": len(requirements),
                "covered_requirement_count": len(covered),
                "uncovered_requirement_ids": uncovered,
                "coverage_percent": round(len(covered) / len(requirements) * 100, 2) if requirements else 0.0,
                "coverage_gate_mode": "prompt-targeted" if prompt_targeted_generation else "full-scope",
                "requested_case_count": requested_case_count,
                "generated_case_count": len(cases),
                "uncovered_high_risk_requirement_ids": high_gaps,
                "test_type_counts": dict(Counter(case["test_type"] for case in cases)),
                "audit_status": audit_status,
                "audit_timeout_seconds": audit_timeout,
                "audit_error": audit_error,
                "audit_notes": audit_value.get("notes", []),
                "duplicate_case_titles": audit_value.get("duplicate_case_titles", []),
                "noncompliant_case_titles": audit_value.get("noncompliant_case_titles", []),
                "candidate_validation_rejections": (generation_rejections + audit_rejections)[:30],
                "documentation_conflicts": requirement_result.value.get("documentation_conflicts", []),
                "retrieved_chunk_count": len(source_refs),
                "required_fixture_keys": required_fixture_keys,
                "missing_fixture_keys": missing_fixture_keys,
                "entry_journey_required_fact_categories": entry_required_fact_categories,
                "entry_journey_baseline_facts": entry_baseline_facts,
            },
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        if progress:
            progress(94, f"Generated {len(cases)} production test cases.")
        return suite, usage.snapshot()

    @staticmethod
    def _extract_requested_case_count(prompt: str) -> int | None:
        """Return an explicit test-case count without confusing domain quantities for case counts."""
        value = str(prompt or "").strip().lower()
        if not value:
            return None

        number_token = r"(?:\d{1,3}|" + "|".join(CASE_COUNT_WORDS) + r")"
        patterns = [
            # "generate 5 different test cases", "create exactly five happy-path cases"
            rf"\b(?:generate|create|produce|make|write|design|build)?\s*(?:exactly\s+)?({number_token})\s+"
            rf"(?:(?:different|distinct|unique|separate|independent|fresh|new|happy[- ]?path|negative|positive|validation|recovery)\s+)*"
            rf"(?:test\s+cases?|cases?|scenarios?)\b",
            # "number of test cases: 5" / "test case count = five"
            rf"\b(?:number|count)\s+of\s+(?:test\s+)?cases?\s*(?:is|=|:)?\s*({number_token})\b",
            rf"\btest\s+case\s+count\s*(?:is|=|:)?\s*({number_token})\b",
            # "test cases: 5"
            rf"\b(?:test\s+cases?|cases?)\s*[:=]\s*({number_token})\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, value, re.IGNORECASE)
            if not match:
                continue
            token = match.group(1).lower()
            count = int(token) if token.isdigit() else CASE_COUNT_WORDS.get(token)
            if count is not None and count > 0:
                return count
        return None

    def _inventory_schema(self) -> Dict[str, Any]:
        rule_schema = {
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
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "suite_name": {"type": "string"},
                "test_cases": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "title": {"type": "string"},
                            "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                            "test_type": {"type": "string", "enum": TEST_TYPES},
                            "requirement_ids": {"type": "array", "items": {"type": "string"}},
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
                            "rule_assertions": rule_schema,
                        },
                        "required": [
                            "title",
                            "priority",
                            "test_type",
                            "requirement_ids",
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
                    },
                },
            },
            "required": ["suite_name", "test_cases"],
        }

    def _normalize_cases(
        self,
        feature: str,
        raw_cases: List[Dict[str, Any]],
        requirements: List[Dict[str, Any]],
        fixtures: Dict[str, Any],
        start: int = 1,
        required_entry_fact_categories: List[str] | None = None,
        entry_baseline_facts: Dict[str, str] | None = None,
        rejection_sink: List[Dict[str, Any]] | None = None,
        prompt_targeted_generation: bool = False,
    ) -> List[Dict[str, Any]]:
        valid_req_ids = {item["id"] for item in requirements}
        prefix = re.sub(r"[^A-Za-z0-9]", "", feature.upper())[:4] or "GEN"
        entry_journey_scope = self._is_entry_journey_scope(feature)
        fixture_value_to_key = {
            str(value): str(key)
            for key, value in fixtures.items()
            if isinstance(value, (str, int, float))
        }
        fixture_keys = sorted(str(key) for key in fixtures.keys())
        cases: List[Dict[str, Any]] = []

        def reject(raw_case: Dict[str, Any], reasons: List[str]) -> None:
            if rejection_sink is None or not reasons:
                return
            rejection_sink.append(
                {
                    "title": self._clean_case_title(str(raw_case.get("title") or "").strip()) or "(untitled candidate)",
                    "test_type": str(raw_case.get("test_type") or "").strip().lower(),
                    "reasons": [str(reason) for reason in reasons if str(reason).strip()][:12],
                }
            )

        for offset, raw in enumerate(raw_cases, start=start):
            title = self._clean_case_title(str(raw.get("title") or "").strip())
            objectives = [str(x).strip() for x in raw.get("objectives", []) if str(x).strip()]
            expected = str(raw.get("expected_result") or "").strip()
            user_goal = str(raw.get("user_goal") or "").strip()
            missing_core = []
            if not title:
                missing_core.append("missing_title")
            if not objectives:
                missing_core.append("missing_objectives")
            if not expected:
                missing_core.append("missing_expected_result")
            if not user_goal:
                missing_core.append("missing_user_goal")
            if missing_core:
                reject(raw, missing_core)
                continue

            req_ids = [str(x) for x in raw.get("requirement_ids", []) if str(x) in valid_req_ids]
            if not req_ids:
                reject(raw, ["no_valid_requirement_ids"])
                continue

            state_mode = str(raw.get("state_mode") or "fresh_user")
            if state_mode not in STATE_MODES:
                state_mode = "fresh_user"
            test_type = str(raw.get("test_type") or "negative").strip().lower()
            case_intent = normalize_text(
                " ".join([title, user_goal, expected, *objectives, str(raw.get("preconditions") or "")])
            )
            intentionally_invalid_resource = bool(
                test_type in {"negative", "validation", "boundary", "recovery"}
                and re.search(
                    r"\b(?:invalid|malformed|incomplete|partial|bad|wrong|unsupported|missing)\b.{0,55}"
                    r"\b(?:url|link|calendar|ics|address|token|code|otp|resource)\b|"
                    r"\b(?:url|link|calendar|ics|address|token|code|otp|resource)\b.{0,55}"
                    r"\b(?:invalid|malformed|incomplete|partial|bad|wrong|unsupported|missing)\b",
                    case_intent,
                    re.IGNORECASE,
                )
            )
            disclosure_style = str(raw.get("disclosure_style") or "progressive")
            if disclosure_style not in DISCLOSURE_STYLES:
                disclosure_style = "progressive"

            # Onboarding/initial-conversation suites are intentionally independent.
            # A later-stage behavior (calendar offer, voice rule, validation, recovery, etc.)
            # must be reached through the normal onboarding journey instead of pretending
            # a brand-new synthetic sender already has downstream state.
            if entry_journey_scope:
                state_mode = "fresh_user"
                if disclosure_style != "verbose":
                    disclosure_style = "progressive"

            required_fixture_keys = {
                str(key).strip()
                for key in raw.get("required_fixture_keys", []) or []
                if str(key).strip()
            }
            scenario_data: Dict[str, str] = {}
            for item in raw.get("scenario_data") or []:
                if not isinstance(item, dict):
                    continue
                raw_key = str(item.get("key") or "").strip()
                if not raw_key:
                    continue
                key = self._canonical_scenario_key(raw_key) if entry_journey_scope else raw_key
                value = str(item.get("value") or "").strip()
                value, fixture_key = self._normalize_resource_value(
                    key,
                    value,
                    fixture_value_to_key,
                    fixture_keys=fixture_keys,
                    test_type=test_type,
                    allow_invalid_literal=intentionally_invalid_resource,
                )
                if fixture_key:
                    required_fixture_keys.add(fixture_key)
                # Prefer an explicit canonical case value over aliases that normalize
                # to the same slot. Conflicting values are rejected below.
                if key in scenario_data and normalize_text(scenario_data[key]) != normalize_text(value):
                    # Recovery/correction cases intentionally may contain an initial wrong value
                    # followed by the corrected canonical value. For prompt-targeted negative
                    # scenarios, keep the latest value as the eventual truth instead of treating
                    # the test definition itself as malformed.
                    if prompt_targeted_generation and test_type in {"negative", "validation", "boundary", "recovery"}:
                        scenario_data[key] = value
                    else:
                        scenario_data[f"__conflict__{key}"] = value
                else:
                    scenario_data[key] = value

            if entry_journey_scope:
                conflicts = [key for key in scenario_data if key.startswith("__conflict__")]
                if conflicts:
                    reject(raw, ["conflicting_scenario_fact:" + key.replace("__conflict__", "") for key in conflicts])
                    continue
                baseline = dict(entry_baseline_facts or {})
                for category in required_entry_fact_categories or []:
                    canonical_key = ENTRY_CANONICAL_KEYS.get(category, category)
                    if self._scenario_has_category(scenario_data, category):
                        continue
                    baseline_value = str(baseline.get(canonical_key) or baseline.get(category) or "").strip()
                    if baseline_value:
                        scenario_data[canonical_key] = baseline_value

                # A case that explicitly targets a second/multiple child journey must
                # actually carry a multi-child count. Do not let the suite baseline of
                # one child silently turn a multi-child objective into a one-child test.
                if re.search(
                    r"\b(?:second\s+(?:child|kid)|(?:child|kid)\s*#?\s*2|two\s+(?:children|kids)|2\s+(?:children|kids)|multiple\s+(?:children|kids))\b",
                    case_intent,
                    re.IGNORECASE,
                ):
                    current_count = self._parse_child_count(scenario_data.get("child_count"))
                    if current_count < 2:
                        scenario_data["child_count"] = "2"

                if prompt_targeted_generation:
                    self._complete_prompt_targeted_entry_facts(
                        scenario_data,
                        required_entry_fact_categories or [],
                        case_intent=case_intent,
                    )

                missing_fact_categories = self._missing_entry_fact_categories(
                    scenario_data,
                    required_entry_fact_categories or [],
                )
                multi_child_issues = self._multi_child_quality_issues(
                    scenario_data,
                    required_entry_fact_categories or [],
                )
                # Production safety gate: malformed independent onboarding tests are
                # never saved. The audit stage can replace them in the same three-call
                # generation pipeline. Runtime should not have to guess missing slots.
                if missing_fact_categories or multi_child_issues:
                    reasons = [f"missing_entry_fact:{item}" for item in missing_fact_categories]
                    reasons.extend(f"multi_child_issue:{item}" for item in multi_child_issues)
                    reject(raw, reasons)
                    continue

            rule_assertions = raw.get("rule_assertions") if isinstance(raw.get("rule_assertions"), dict) else {}
            normalized_rules = {
                "literal_required_any": self._strings(rule_assertions.get("literal_required_any")),
                "literal_required_all": self._strings(rule_assertions.get("literal_required_all")),
                "literal_forbidden": self._strings(rule_assertions.get("literal_forbidden")),
                "final_response_regex": str(rule_assertions.get("final_response_regex") or ""),
                "enforce_final_response_regex": bool(rule_assertions.get("enforce_final_response_regex", False)),
                "max_assistant_chars": max(0, int(rule_assertions.get("max_assistant_chars", 0) or 0)),
                "min_user_turns": max(1, int(rule_assertions.get("min_user_turns", 1) or 1)),
                "enforce_min_user_turns": bool(rule_assertions.get("enforce_min_user_turns", False)),
                # Captures a documented target but is advisory unless the target
                # config explicitly opts into enforcing documented SLAs.
                "max_response_ms": max(0, int(rule_assertions.get("max_response_ms", 0) or 0)),
            }

            cases.append(
                {
                    "id": f"TC-{prefix}-{offset:03d}",
                    "title": title,
                    "feature": feature.strip(),
                    "priority": raw.get("priority", "Medium"),
                    "test_type": test_type,
                    "requirement_ids": req_ids,
                    "risk_tags": sorted(
                        {str(x).strip().lower() for x in raw.get("risk_tags", []) if str(x).strip()}
                    ),
                    "preconditions": str(raw.get("preconditions") or "").strip(),
                    "persona": str(raw.get("persona") or "A realistic user with ordinary conversational habits.").strip(),
                    "user_goal": user_goal,
                    "state_mode": state_mode,
                    "disclosure_style": disclosure_style,
                    "scenario_data": scenario_data,
                    "journey_required_fact_categories": list(required_entry_fact_categories or []),
                    "journey_baseline_facts": dict(entry_baseline_facts or {}) if entry_journey_scope else {},
                    "scenario_contract_version": 2 if entry_journey_scope else 1,
                    "required_fixture_keys": sorted(required_fixture_keys),
                    "objectives": objectives,
                    "initial_message_hint": (
                        self._safe_entry_opening_hint(raw.get("initial_message_hint"))
                        if entry_journey_scope
                        else self._safe_opening_hint(raw.get("initial_message_hint"))
                    ),
                    "journey_entry_mode": "from_start" if entry_journey_scope else "scenario_defined",
                    "expected_result": expected,
                    "max_turns": max(
                        2,
                        min(int(raw.get("max_turns") or self.config.max_conversation_turns), 40),
                    ),
                    "rule_assertions": normalized_rules,
                    "review_status": "draft",
                    "review_note": "",
                    "approved": False,
                    "version": 1,
                    "revision_history": [],
                }
            )
        return cases

    @staticmethod
    def _is_entry_journey_scope(value: Any) -> bool:
        return bool(ENTRY_JOURNEY_SCOPE_PATTERN.search(str(value or "")))

    @classmethod
    def _entry_fact_categories_from_requirements(
        cls,
        requirements: List[Dict[str, Any]],
    ) -> List[str]:
        """Backward-compatible wrapper used by older tests/helpers."""
        return cls._entry_fact_categories_from_evidence(requirements, "", [], [])

    @classmethod
    def _entry_fact_categories_from_evidence(
        cls,
        requirements: List[Dict[str, Any]],
        context: str,
        fixture_keys: List[str],
        entry_defaults: List[Dict[str, Any]] | None = None,
    ) -> List[str]:
        """Infer ordinary onboarding slots from the full evidence set.

        Requirement extraction can omit a prerequisite even when the source docs
        clearly describe it.  Production generation therefore looks at the
        extracted requirements *and* retrieved document evidence, configured
        resource names, and the requirement-stage default-fact contract.
        """
        evidence_parts: List[str] = [str(context or "")]
        for requirement in requirements:
            evidence_parts.extend(
                [
                    str(requirement.get("title") or ""),
                    str(requirement.get("description") or ""),
                    " ".join(str(x) for x in requirement.get("acceptance_criteria", []) or []),
                ]
            )
        evidence_parts.extend(str(key) for key in fixture_keys or [])
        text = normalize_text(" ".join(evidence_parts))

        checks = [
            (
                "parent_name",
                r"\b(?:parent|user|adult|guardian)\b.{0,55}\bname\b|"
                r"\b(?:your name|who am i speaking with|who are you|what should i call you|name should .* use for you)\b|"
                r"\bwho\b.{0,40}\bspeaking with\b",
            ),
            (
                "child_count",
                r"\b(?:how many|number of|count of)\b.{0,60}\b(?:kids?|children|child|schedules?)\b|"
                r"\b(?:kids?|children|child)\b.{0,40}\b(?:count|how many|schedules?)\b",
            ),
            (
                "child_name",
                r"\b(?:kid|kid's|kids|child|child's|children)\b.{0,55}\bname\b|"
                r"\bname\b.{0,55}\b(?:kid|child)\b",
            ),
            ("sport", r"\bsport(?:s)?\b|\bwhat .* (?:play|playing)\b"),
            (
                "provider",
                r"\b(?:team app|sports app|calendar app|app provider|platform|provider|teamsnap|leagueapps|sportsengine)\b",
            ),
            ("team_name", r"\bteam\b.{0,35}\bname\b"),
            ("email", r"\b(?:email|e-mail)\b"),
            ("phone", r"\b(?:phone|mobile)\b.{0,30}\b(?:number|contact)\b"),
        ]
        categories = [category for category, pattern in checks if re.search(pattern, text, re.IGNORECASE)]

        for item in entry_defaults or []:
            category = str((item or {}).get("category") or "").strip()
            if category in ENTRY_FACT_CATEGORIES and category not in categories:
                categories.append(category)
        return categories

    @classmethod
    def _normalize_entry_defaults(
        cls,
        values: List[Dict[str, Any]] | None,
        required_categories: List[str],
    ) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for item in values or []:
            category = str((item or {}).get("category") or "").strip()
            value = str((item or {}).get("value") or "").strip()
            if category not in required_categories or not value:
                continue
            if FIXTURE_PATTERN.match(value) or URL_PATTERN.match(value):
                continue
            key = ENTRY_CANONICAL_KEYS.get(category, category)
            result.setdefault(key, value)
        return result

    @classmethod
    def _build_entry_baseline_facts(
        cls,
        defaults: Dict[str, str],
        raw_cases: List[Dict[str, Any]],
        required_categories: List[str],
        fixture_keys: List[str],
        context: str,
    ) -> Dict[str, str]:
        """Compile a suite-level safe baseline for independent onboarding cases.

        Case-specific facts always override this baseline.  It exists so a test
        focused on a later behavior (tone, address, OAuth, recovery, etc.) still
        has the ordinary prerequisite data needed to reach that behavior.
        """
        baseline = {str(k): str(v) for k, v in (defaults or {}).items() if str(v).strip()}
        candidates: Dict[str, Counter[str]] = {category: Counter() for category in required_categories}
        original_values: Dict[tuple[str, str], str] = {}

        for raw in raw_cases or []:
            for item in (raw or {}).get("scenario_data") or []:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "").strip()
                value = str(item.get("value") or "").strip()
                if not key or not value or FIXTURE_PATTERN.match(value) or URL_PATTERN.match(value):
                    continue
                category = cls._scenario_fact_category(key)
                if category not in candidates:
                    continue
                normalized_value = normalize_text(value)
                if not normalized_value:
                    continue
                candidates[category][normalized_value] += 1
                original_values.setdefault((category, normalized_value), value)

        for category in required_categories:
            canonical_key = ENTRY_CANONICAL_KEYS.get(category, category)
            if str(baseline.get(canonical_key) or "").strip():
                continue
            if candidates.get(category):
                normalized_value, _count = candidates[category].most_common(1)[0]
                baseline[canonical_key] = original_values[(category, normalized_value)]

        # A single-child positive baseline is safe and domain-neutral.  Cases
        # that test multiple children or invalid counts provide their own value
        # and therefore override this baseline.
        if "child_count" in required_categories and not str(baseline.get("child_count") or "").strip():
            baseline["child_count"] = "1"

        # Provider may be inferable from a tester-controlled resource key even
        # when a model omitted the ordinary sports_app fact.
        if "provider" in required_categories and not str(baseline.get("sports_app") or "").strip():
            provider = cls._provider_from_fixture_evidence(fixture_keys, context)
            if provider:
                baseline["sports_app"] = provider
        return baseline

    @staticmethod
    def _provider_from_fixture_evidence(fixture_keys: List[str], context: str) -> str:
        generic = {
            "url", "link", "calendar", "calender", "ics", "export", "schedule", "team",
            "test", "resource", "valid", "external", "webhook", "oauth", "auth", "code",
        }
        context_text = str(context or "")
        for key in fixture_keys or []:
            for token in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", str(key)):
                if normalize_text(token) in generic:
                    continue
                match = re.search(rf"\b{re.escape(token)}\b", context_text, re.IGNORECASE)
                if match:
                    return match.group(0)
        return ""

    @staticmethod
    def _indexed_child_key(key: str) -> str:
        """Return a canonical per-child slot such as child_2_name when explicit.

        Multi-child onboarding must not collapse ``second_child_name`` and
        ``child_name`` into one slot.  Keeping the child index is what lets the
        runtime answer "what is kid #2's name?" with a distinct value.
        """
        raw = normalize_text(str(key).replace("_", " ").replace("-", " "))
        if not re.search(r"\b(?:child|kid)\b", raw):
            return ""

        ordinals = {
            "first": 1, "1": 1, "1st": 1,
            "second": 2, "2": 2, "2nd": 2,
            "third": 3, "3": 3, "3rd": 3,
            "fourth": 4, "4": 4, "4th": 4,
            "fifth": 5, "5": 5, "5th": 5,
        }
        index = 0
        for token, number in ordinals.items():
            if re.search(
                rf"(?:\b{re.escape(token)}\b.{{0,18}}\b(?:child|kid)\b|"
                rf"\b(?:child|kid)\b.{{0,18}}\b{re.escape(token)}\b)",
                raw,
                re.IGNORECASE,
            ):
                index = number
                break
        if not index:
            compact = re.sub(r"[^a-z0-9]+", " ", raw)
            match = re.search(r"\b(?:child|kid)\s*#?\s*([1-5])\b", compact)
            if match:
                index = int(match.group(1))
        if not index:
            return ""

        if "name" in raw:
            field = "name"
        elif "sport" in raw:
            field = "sport"
        elif any(token in raw for token in ("app", "platform", "provider", "service")):
            field = "sports_app"
        elif "team" in raw:
            field = "team_name"
        else:
            return ""
        return f"child_{index}_{field}"

    @classmethod
    def _canonical_scenario_key(cls, key: str) -> str:
        indexed = cls._indexed_child_key(key)
        if indexed:
            return indexed
        category = cls._scenario_fact_category(key)
        return ENTRY_CANONICAL_KEYS.get(category, str(key).strip()) if category != "generic" else str(key).strip()

    @classmethod
    def _scenario_has_category(cls, scenario_data: Dict[str, Any], category: str) -> bool:
        for key, value in scenario_data.items():
            if str(key).startswith("__conflict__"):
                continue
            if not str(value or "").strip():
                continue
            if FIXTURE_PATTERN.match(str(value).strip()) or URL_PATTERN.match(str(value).strip()):
                continue
            if cls._scenario_fact_category(str(key)) == category:
                return True
        return False

    @classmethod
    def _scenario_fact_category(cls, key: str) -> str:
        indexed = cls._indexed_child_key(key)
        if indexed:
            if indexed.endswith("_name"):
                return "child_name"
            if indexed.endswith("_sport"):
                return "sport"
            if indexed.endswith("_sports_app"):
                return "provider"
            if indexed.endswith("_team_name"):
                return "team_name"

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
        if any(
            token in normalized
            for token in (
                "number of kids",
                "number of children",
                "child count",
                "kid count",
                "children count",
            )
        ):
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

    @classmethod
    def _complete_prompt_targeted_entry_facts(
        cls,
        scenario_data: Dict[str, Any],
        required_categories: List[str],
        *,
        case_intent: str = "",
    ) -> None:
        """Complete safe eventual facts for developer-requested onboarding scenarios.

        The dashboard prompt defines behavior under test. Missing/invalid *attempts* are valid
        scenario behavior, but the simulator still needs eventual truth facts so the case can
        continue after recovery. Only domain-neutral synthetic identity/contact values are
        invented here; domain facts such as a different sport/provider are never fabricated.
        """
        safe_defaults = {
            "parent_name": "Taylor",
            "email": "qa.parent@example.com",
            "phone": "202-555-0147",
        }
        for category, value in safe_defaults.items():
            if category in required_categories and not cls._scenario_has_category(scenario_data, category):
                scenario_data[ENTRY_CANONICAL_KEYS.get(category, category)] = value

        count = cls._parse_child_count(scenario_data.get("child_count"))
        if count <= 0 and "child_count" in required_categories:
            count = 1
            scenario_data["child_count"] = "1"

        if count <= 0:
            return

        # Promote generic first-child facts to indexed canonical facts for multi-child cases.
        if count > 1:
            if not str(scenario_data.get("child_1_name") or "").strip() and str(scenario_data.get("child_name") or "").strip():
                scenario_data["child_1_name"] = str(scenario_data["child_name"]).strip()
            if not str(scenario_data.get("child_1_sport") or "").strip() and str(scenario_data.get("sport") or "").strip():
                scenario_data["child_1_sport"] = str(scenario_data["sport"]).strip()

        # Names are domain-neutral test data and may be safely synthesized.
        default_names = ["Jamie", "Riley", "Jordan", "Casey", "Morgan"]
        used_names = {
            normalize_text(str(v))
            for k, v in scenario_data.items()
            if cls._scenario_fact_category(str(k)) == "child_name" and str(v or "").strip()
        }
        if "child_name" in required_categories:
            for index in range(1, count + 1):
                key = f"child_{index}_name" if count > 1 else "child_name"
                existing = str(scenario_data.get(key) or "").strip()
                if existing:
                    continue
                for candidate in default_names:
                    if normalize_text(candidate) not in used_names:
                        scenario_data[key] = candidate
                        used_names.add(normalize_text(candidate))
                        break

        if "sport" in required_categories and count > 1:
            first_sport = str(scenario_data.get("child_1_sport") or scenario_data.get("sport") or "").strip()
            explicitly_different = bool(re.search(
                r"\b(?:different|distinct|separate)\b.{0,35}\bsports?\b|\bsports?\b.{0,35}\b(?:different|distinct|separate)\b",
                case_intent,
                re.IGNORECASE,
            ))
            # Same-sport or unspecified multi-child scenarios can safely share the grounded
            # first sport. Different-sport cases must carry a second grounded value from AI.
            if first_sport and not explicitly_different:
                for index in range(2, count + 1):
                    scenario_data.setdefault(f"child_{index}_sport", first_sport)

    @classmethod
    def _missing_entry_fact_categories(
        cls,
        scenario_data: Dict[str, Any],
        required_categories: List[str],
    ) -> List[str]:
        available = {
            cls._scenario_fact_category(str(key))
            for key, value in scenario_data.items()
            if str(value or "").strip()
            and not FIXTURE_PATTERN.match(str(value or "").strip())
            and not URL_PATTERN.match(str(value or "").strip())
        }
        return [category for category in required_categories if category not in available]

    @staticmethod
    def _parse_child_count(value: Any) -> int:
        text = normalize_text(str(value or "").strip())
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
        if text in words:
            return words[text]
        match = re.search(r"\b([1-5])\b", text)
        return int(match.group(1)) if match else 0

    @classmethod
    def _multi_child_quality_issues(
        cls,
        scenario_data: Dict[str, Any],
        required_categories: List[str],
    ) -> List[str]:
        """Validate per-child facts when a case says more than one child exists."""
        count = cls._parse_child_count(scenario_data.get("child_count"))
        if count <= 1:
            return []

        issues: List[str] = []
        names: List[str] = []
        for index in range(1, count + 1):
            name = str(
                scenario_data.get(f"child_{index}_name")
                or (scenario_data.get("child_name") if index == 1 else "")
                or ""
            ).strip()
            if not name:
                issues.append(f"child_{index}_name")
            else:
                names.append(normalize_text(name))

            if "sport" in required_categories:
                sport = str(
                    scenario_data.get(f"child_{index}_sport")
                    or (scenario_data.get("sport") if index == 1 else "")
                    or ""
                ).strip()
                if not sport:
                    issues.append(f"child_{index}_sport")

        if names and len(set(names)) != len(names):
            issues.append("distinct_child_names")
        return issues


    @classmethod
    def _safe_entry_opening_hint(cls, value: Any) -> str:
        """Preserve safe per-case opening variety without leaking later onboarding steps."""
        text = str(value or "").strip()
        if not text or ENTRY_OPENING_LATER_STEP_PATTERN.search(text):
            return (
                "Start with a short natural greeting, identity check, or simple request for help. "
                "Do not mention later onboarding steps, resources, or personal/domain facts before they are asked for."
            )
        return cls._safe_opening_hint(text)

    @staticmethod
    def _safe_opening_hint(value: Any) -> str:
        text = str(value or "").strip()
        unsafe_placeholder = re.search(
            r"(?:\[[^\]\r\n]{1,80}\]|<[^<>\r\n]{1,80}>|\b(?:your|insert|enter|replace)\s+(?:full\s+)?(?:name|child\s+name|email|phone|address)\b)",
            text,
            re.IGNORECASE,
        )
        unsafe_formal = re.search(
            r"\b(?:onboarding process|looking forward to working together|reach out and start|dear sir|dear team|sincerely)\b",
            text,
            re.IGNORECASE,
        )
        if not text or unsafe_placeholder or unsafe_formal:
            return (
                "Start with a short, natural SMS greeting or simple statement of intent. "
                "Do not use placeholders or volunteer personal/domain facts before they are asked for."
            )
        return text[:240]

    @staticmethod
    def _clean_case_title(title: str) -> str:
        """Keep latency/SLA wording out of ordinary functional titles.

        The documented value remains in rule_assertions.max_response_ms and is
        evaluated by the performance policy.  This keeps suite titles readable
        and avoids teaching the simulator to treat an SLA as conversational data.
        """
        if not title:
            return title
        cleaned = re.sub(
            r"\s+(?:with|and)\s+(?:a\s+)?(?:response|reply)\s+(?:latency|time)\s+"
            r"(?:under|below|within|of|<=?)\s*\d+(?:\.\d+)?\s*(?:ms|milliseconds?|s|sec(?:onds?)?)\b.*$",
            "",
            title,
            flags=re.IGNORECASE,
        ).strip(" -:;,.")
        return cleaned or title

    def _normalize_resource_value(
        self,
        key: str,
        value: str,
        fixture_value_to_key: Dict[str, str],
        fixture_keys: List[str] | None = None,
        test_type: str = "",
        allow_invalid_literal: bool = False,
    ) -> Tuple[str, str]:
        match = FIXTURE_PATTERN.match(value)
        if match:
            return value, match.group(1)

        existing_key = fixture_value_to_key.get(value)
        if existing_key:
            return f"{{FIXTURE:{existing_key}}}", existing_key

        normalized_test_type = normalize_text(test_type)
        requires_real_address = bool(
            test_type
            and PHYSICAL_ADDRESS_KEY_PATTERN.search(key)
            and normalized_test_type not in {"negative", "validation", "boundary"}
        )
        if requires_real_address:
            candidates = [
                str(candidate)
                for candidate in (fixture_keys or [])
                if PHYSICAL_ADDRESS_KEY_PATTERN.search(str(candidate))
            ]
            if candidates:
                key_words = set(re.findall(r"[a-z0-9]+", normalize_text(key.replace("_", " "))))
                ranked = []
                for candidate in candidates:
                    candidate_words = set(
                        re.findall(r"[a-z0-9]+", normalize_text(candidate.replace("_", " ")))
                    )
                    ranked.append((len(key_words & candidate_words), candidate))
                ranked.sort(key=lambda item: (-item[0], item[1]))
                selected = ranked[0][1]
                return f"{{FIXTURE:{selected}}}", selected

            proposed = "valid_us_home_address" if "home" in normalize_text(key) else (
                slugify(key).replace("-", "_") or "valid_physical_address"
            )
            return f"{{FIXTURE:{proposed}}}", proposed

        controlled_key = bool(
            CONTROLLED_RESOURCE_KEY_PATTERN.search(key)
            or SENSITIVE_RESOURCE_KEY_PATTERN.search(key)
        )
        looks_like_url = bool(URL_PATTERN.match(value))
        if not controlled_key and not looks_like_url:
            return value, ""

        # A malformed/partial resource can itself be the user input under test.
        # Preserve an explicitly non-URL bad attempt for negative-style cases instead
        # of silently turning it into a valid fixture. A syntactically real URL remains
        # tester-controlled even in a negative case; the generator must never invent it.
        if allow_invalid_literal and controlled_key and value and not looks_like_url:
            return value, ""

        proposed = slugify(key).replace("-", "_") or "external_test_resource"
        return f"{{FIXTURE:{proposed}}}", proposed

    def _deduplicate(self, cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        result = []
        for case in cases:
            key = (
                normalize_text(case.get("title"))
                + "|"
                + "|".join(sorted(case.get("requirement_ids", [])))
                + "|"
                + case.get("test_type", "")
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(case)
        for index, case in enumerate(result, start=1):
            prefix = case["id"].split("-")[1] if "-" in case["id"] else "GEN"
            case["id"] = f"TC-{prefix}-{index:03d}"
        return result

    def _audit_projection(self, cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "title": case["title"],
                "type": case["test_type"],
                "requirements": case["requirement_ids"],
                "user_goal": case.get("user_goal", ""),
                "state_mode": case.get("state_mode", "fresh_user"),
                "disclosure_style": case.get("disclosure_style", "progressive"),
                "scenario_data": case.get("scenario_data", {}),
                "journey_required_fact_categories": case.get("journey_required_fact_categories", []),
                "objectives": case["objectives"],
                "expected_result": case["expected_result"],
                "required_fixture_keys": case.get("required_fixture_keys", []),
                "rule_assertions": case.get("rule_assertions", {}),
            }
            for case in cases
        ]

    @staticmethod
    def _strings(value: Any) -> List[str]:
        return [str(item).strip() for item in (value or []) if str(item).strip()]


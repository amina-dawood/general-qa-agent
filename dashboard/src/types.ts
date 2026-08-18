export type Page = 'overview' | 'projects' | 'suites' | 'runs';
export type Outcome =
  | 'passed'
  | 'failed'
  | 'blocked'
  | 'error'
  | 'cancelled'
  | 'running'
  | 'awaiting_human';

export interface Project {
  id: string;
  name: string;
  slug: string;
  description: string;
  status: string;
  target: Record<string, any>;
  fixtures: Record<string, any>;
  workflow?: { filename: string; summary: any; attached_at: string } | null;
  created_at: string;
  updated_at: string;
  document_count?: number;
  documents?: DocumentInfo[];
}

export interface DocumentInfo {
  id: string;
  project_id: string;
  name: string;
  path: string;
  checksum: string;
  status: string;
  chunk_count: number;
  created_at: string;
}

export interface Requirement {
  id: string;
  title: string;
  description: string;
  risk: 'High' | 'Medium' | 'Low';
  acceptance_criteria: string[];
}

export interface TestCase {
  id: string;
  title: string;
  feature: string;
  priority: 'High' | 'Medium' | 'Low' | string;
  test_type: string;
  requirement_ids: string[];
  risk_tags: string[];
  preconditions: string;
  persona: string;
  user_goal: string;
  state_mode: 'fresh_user' | 'returning_user' | 'continuation' | string;
  disclosure_style?: 'progressive' | 'concise' | 'verbose' | string;
  scenario_data: Record<string, any>;
  required_fixture_keys?: string[];
  objectives: string[];
  initial_message_hint: string;
  expected_result: string;
  max_turns: number;
  rule_assertions: Record<string, any>;
  review_status: string;
  review_note: string;
  approved: boolean;
  version: number;
  revision_history?: any[];
}

export interface WorkflowReviewFinding {
  requirement_id: string;
  severity: 'High' | 'Medium' | 'Low';
  possible_gap: string;
  workflow_area: string;
  recommended_check: string;
}

export interface WorkflowReview {
  available: boolean;
  summary: string;
  findings: WorkflowReviewFinding[];
  reviewed_at?: string;
  ai_usage?: AiUsage;
}

export interface Suite {
  id: string;
  project_id: string;
  name: string;
  feature: string;
  version: number;
  status: string;
  approved: boolean;
  review_note: string;
  requirements: Requirement[];
  test_cases: TestCase[];
  source_query: string;
  source_refs: any[];
  generation_summary: Record<string, any>;
  generation_ai_usage?: AiUsage;
  workflow_review?: WorkflowReview;
  created_at: string;
  updated_at: string;
}

export interface Turn {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  latency_ms: number;
  metadata?: Record<string, any>;
}

export interface HumanAction {
  kind?: 'resource_input' | 'browser_action' | 'other' | string;
  resource_key?: string;
  title: string;
  instructions: string;
  url?: string;
  requires_input?: boolean;
  input_label?: string;
  reason?: string;
  test_case_id?: string;
  requested_at?: string;
}

export interface HumanActionRecord {
  action: HumanAction;
  status: 'completed' | 'not_completed' | string;
  note?: string;
  completed_at?: string;
}

export interface SemanticEvaluation {
  engine: string;
  metric?: string;
  passed: boolean;
  score: number;
  threshold: number;
  reason: string;
  model: string;
}

export interface PerformanceEvaluation {
  status: 'healthy' | 'warning' | 'critical' | 'failed' | 'not_measured' | string;
  blocking: boolean;
  message: string;
  average_ms: number;
  p95_ms: number;
  max_ms: number;
  turn_count: number;
  warning_count: number;
  critical_count: number;
  failed_count: number;
  thresholds: {
    warning_ms?: number;
    critical_ms?: number;
    fail_ms?: number;
  };
  documented_target_ms?: number;
  documented_target_exceeded_count?: number;
  documented_sla_enforced?: boolean;
}

export interface Evaluation {
  status: Outcome;
  passed: boolean;
  score: number;
  summary: string;
  rule_checks: any[];
  performance?: PerformanceEvaluation;
  semantic?: SemanticEvaluation | null;
  evaluation_error?: string;
  duration_ms: number;
  ai_usage: AiUsage;
}

export interface Diagnosis {
  failure_category?: string;
  observed_problem?: string;
  evidence?: string[];
  likely_causes?: { cause: string; confidence: string }[];
  recommended_checks?: string[];
  suspected_components?: string[];
  workflow_evidence_available?: boolean;
  confidence?: string;
  diagnosis_error?: string;
}

export interface TestCaseSnapshot {
  version?: number;
  persona?: string;
  user_goal?: string;
  state_mode?: string;
  disclosure_style?: string;
  scenario_data?: Record<string, any>;
  required_fixture_keys?: string[];
  preconditions?: string;
  objectives?: string[];
  expected_result?: string;
  rule_assertions?: Record<string, any>;
}

export interface TestResult {
  test_case_id: string;
  title: string;
  feature: string;
  priority: string;
  test_type: string;
  requirement_ids?: string[];
  test_case_snapshot?: TestCaseSnapshot;
  outcome: Outcome;
  passed: boolean;
  score: number;
  duration_ms: number;
  blocked_reason: string;
  pending_human_action?: HumanAction | null;
  execution_state?: Record<string, any>;
  conversation: {
    status: string;
    stop_reason: string;
    error: string;
    session_id?: string;
    sender_identity?: string;
    turns: Turn[];
    human_actions?: HumanActionRecord[];
    started_at: string;
    ended_at: string;
  };
  evaluation: Evaluation;
  diagnosis: Diagnosis;
  ai_usage: AiUsage;
}

export interface AiUsage {
  requests?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  cached_tokens?: number;
  reasoning_tokens?: number;
  cost_usd?: number;
  models?: Record<string, number>;
}

export interface Run {
  id: string;
  project_id: string;
  run_number: number;
  display_id: string;
  suite_id: string;
  suite_name: string;
  status: string;
  is_baseline: boolean;
  started_at: string;
  ended_at: string;
  duration_ms: number;
  active_duration_ms?: number;
  passed_count: number;
  failed_count: number;
  blocked_count: number;
  error_count: number;
  pass_rate: number;
  results: TestResult[];
  ai_usage: AiUsage;
  pending_human_action?: HumanAction | null;
  next_case_index?: number;
  current_result_index?: number | null;
  execution_cases?: TestCase[];
  reports?: { json: string; html: string };
  warnings?: string[];
}

export interface RunPreselection {
  suite_id: string;
  test_case_ids: string[];
  nonce: number;
}

export interface Analytics {
  scope?: {
    suite_id: string;
    suite_name: string;
    suite_version: number;
    active_test_cases: number;
    approved_test_cases: number;
  };
  summary: {
    total_test_cases: number;
    active_test_cases?: number;
    approved_test_cases?: number;
    executed_tests?: number;
    untested_tests?: number;
    needs_retest_tests?: number;
    passed_tests: number;
    failed_tests: number;
    blocked_tests: number;
    error_tests?: number;
    pass_rate: number;
    execution_coverage?: number;
    requirement_coverage: number | null;
    requirement_execution_coverage?: number | null;
    workflow_node_coverage: number | null;
    branch_coverage: number | null;
    average_execution_ms: number;
    total_test_runs: number;
    current_suite_runs?: number;
    completed_runs?: number;
    awaiting_human_runs?: number;
    api_response_ms: number;
    p95_api_response_ms?: number;
    ai_token_usage: number;
    estimated_test_cost: number;
    pricing_configured?: boolean;
    high_risk_issues_count: number;
    high_priority_failures?: number;
    most_failed_workflow_node: string | null;
  };
  failure_categories: { name: string; value: number }[];
  tests_by_priority: { name: string; value: number }[];
  tests_by_type: { name: string; value: number }[];
  validation_status?: { name: string; value: number }[];
  performance_status?: { name: string; value: number }[];
  trend: { id: string; display_id: string; started_at: string; passed: number; failed: number; blocked: number; errors?: number; pass_rate: number; case_count?: number }[];
  current_vs_previous: null | {
    current: { id?: string; display_id: string; passed: number; failed: number; blocked: number; errors?: number; pass_rate: number; duration_ms: number };
    previous: { id?: string; display_id: string; passed: number; failed: number; blocked: number; errors?: number; pass_rate: number; duration_ms: number };
    baseline?: { id?: string; display_id: string; passed: number; failed: number; blocked: number; errors?: number; pass_rate: number; duration_ms: number } | null;
    case_count?: number;
    pass_rate_delta?: number;
  };
  comparison_note?: string;
  ai_usage: AiUsage;
  coverage: { total: number; covered: number; uncovered: number; executed?: number; not_executed?: number };
  suspected_failure_areas: { name: string; value: number }[];
}

export interface Job<T = any> {
  id: string;
  project_id?: string;
  kind: string;
  status: 'queued' | 'running' | 'completed' | 'failed';
  progress: number;
  message: string;
  result: T | null;
  error: string;
  created_at: string;
  updated_at: string;
}


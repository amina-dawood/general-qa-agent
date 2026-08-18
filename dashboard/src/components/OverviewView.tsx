import { useEffect, useState } from 'react';
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  Clock3,
  FileCheck2,
  Gauge,
  PlayCircle,
  TestTube2,
  Timer,
  XCircle,
} from 'lucide-react';
import { api } from '../api';
import type { Analytics, Project } from '../types';

const fmt = new Intl.NumberFormat('en-US');
const duration = (ms:number) => ms >= 60000
  ? `${Math.floor(ms / 60000)}m ${Math.round(ms % 60000 / 1000)}s`
  : ms > 0 ? `${(ms / 1000).toFixed(1)}s` : '—';
const money = (value:number) => `$${value.toFixed(value < 1 ? 4 : 2)}`;

function Card({ label, value, icon:Icon, tone='', hint='' }:{
  label:string;
  value:string|number;
  icon:any;
  tone?:string;
  hint?:string;
}) {
  return <article className={`metric-card ${tone}`}>
    <div className="metric-head"><span>{label}</span><Icon size={18}/></div>
    <strong>{value}</strong>
    {hint && <small>{hint}</small>}
  </article>;
}

function Bars({ items }:{ items:{name:string;value:number}[] }) {
  const visible = items.filter(item => item.value > 0);
  const max = Math.max(1, ...visible.map(item => item.value));
  return <div className="bars">
    {visible.length
      ? visible.map(item => <div className="bar-row" key={item.name}>
          <div className="bar-label"><span>{item.name}</span><b>{item.value}</b></div>
          <div className="bar-track"><i style={{ width: `${item.value / max * 100}%` }}/></div>
        </div>)
      : <div className="empty-small">No data yet.</div>}
  </div>;
}

function Trend({ data }:{ data:Analytics['trend'] }) {
  if (!data.length) return <div className="empty-small">Run trend will appear after completed execution.</div>;
  const points = data.map((item, index) => {
    const x = data.length === 1 ? 50 : (index / (data.length - 1)) * 100;
    const y = 100 - Math.max(0, Math.min(100, item.pass_rate));
    return `${x},${y}`;
  }).join(' ');
  return <div className="trend-chart">
    <svg viewBox="0 0 100 100" preserveAspectRatio="none">
      <line x1="0" y1="100" x2="100" y2="100"/>
      <line x1="0" y1="50" x2="100" y2="50"/>
      <polyline points={points}/>
    </svg>
    <div className="trend-labels">
      {data.map(item => <span key={item.id}>{item.display_id}<b>{item.pass_rate}%</b><small>{item.case_count ?? 0} test{(item.case_count ?? 0) === 1 ? '' : 's'}</small></span>)}
    </div>
  </div>;
}

export default function OverviewView({ project }:{project:Project|null}) {
  const [data, setData] = useState<Analytics|null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!project) { setData(null); return; }
    let active = true;
    setLoading(true);
    setError('');
    api.analytics(project.id)
      .then(value => { if (active) setData(value); })
      .catch((err:any) => { if (active) setError(err.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [project?.id]);

  if (!project) return <div className="empty-state"><h3>Create or select a project first.</h3><p>Projects hold requirements, target configuration, suites and run history.</p></div>;
  if (loading && !data) return <div className="loading-panel"><div className="spinner"/><p>Loading analytics…</p></div>;
  if (!data) return <>{error && <div className="alert error">{error}</div>}<div className="empty-state"><h3>Analytics are not available yet.</h3></div></>;

  const s = data.summary;
  const approved = s.approved_test_cases ?? s.total_test_cases;
  const active = s.active_test_cases ?? approved;
  const executed = s.executed_tests ?? (s.passed_tests + s.failed_tests + s.blocked_tests + (s.error_tests ?? 0));
  const executionCoverage = s.execution_coverage ?? (approved ? Math.round(executed / approved * 10000) / 100 : 0);
  const errors = s.error_tests ?? 0;
  const requirementHint = data.coverage.total
    ? `${data.coverage.covered}/${data.coverage.total} mapped · ${data.coverage.executed ?? 0}/${data.coverage.total} exercised`
    : 'No requirements in the current suite';
  const completedRuns = s.completed_runs ?? data.trend.length;
  const currentSuiteRuns = s.current_suite_runs ?? completedRuns;
  const waitingRuns = s.awaiting_human_runs ?? 0;

  return <>
    {data.scope?.suite_name && <div className="analytics-scope-note">
      <b>{data.scope.suite_name}</b>
      <span>Analytics below describe the current runnable suite. Historical runs from other suites do not change current test-health counts.</span>
    </div>}

    <div className="metrics-grid">
      <Card label="Approved tests" value={fmt.format(approved)} icon={TestTube2} hint={`${approved} of ${active} active suite cases are runnable`}/>
      <Card label="Execution coverage" value={`${executionCoverage}%`} icon={Gauge} hint={`${executed}/${approved || 0} current-version approved tests executed`}/>
      <Card label="Passed" value={fmt.format(s.passed_tests)} icon={CheckCircle2} tone="good"/>
      <Card label="Failed" value={fmt.format(s.failed_tests)} icon={XCircle} tone="bad"/>
      <Card label="Blocked" value={fmt.format(s.blocked_tests)} icon={Ban} tone="warning"/>
      <Card label="Errors" value={fmt.format(errors)} icon={AlertTriangle} tone={errors ? 'bad' : ''}/>
      <Card label="Pass rate" value={`${s.pass_rate}%`} icon={Gauge} hint="Passed / (passed + failed); blocked and errors stay separate"/>
      <Card label="Requirement design coverage" value={s.requirement_coverage == null ? '—' : `${s.requirement_coverage}%`} icon={FileCheck2} hint={requirementHint}/>
    </div>

    <section className="panel compact-kpis analytics-kpis">
      <div><span>Avg test duration</span><b>{duration(s.average_execution_ms)}</b><small>Latest current-version result per test</small></div>
      <div><span>Avg API response</span><b>{duration(s.api_response_ms)}</b><small>Assistant turns in current test health</small></div>
      <div><span>P95 API response</span><b>{duration(s.p95_api_response_ms ?? 0)}</b><small>Highlights slow-tail behavior</small></div>
      <div><span>Completed runs</span><b>{completedRuns}</b><small>{currentSuiteRuns} current-suite total{waitingRuns ? ` · ${waitingRuns} awaiting human` : ''}</small></div>
      <div><span>AI tokens</span><b>{fmt.format(s.ai_token_usage)}</b><small>Project total: generation, revision and execution</small></div>
      <div><span>AI cost</span><b>{s.pricing_configured ? money(s.estimated_test_cost) : 'Not configured'}</b><small>{s.pricing_configured ? 'Based on configured model pricing' : 'Set MODEL_PRICING_JSON to calculate cost'}</small></div>
      <div><span>High-priority failures</span><b>{s.high_priority_failures ?? s.high_risk_issues_count}</b><small>Current-version failed/error high-priority tests</small></div>
    </section>

    <div className="analytics-grid two">
      <section className="panel">
        <div className="panel-head"><div><span className="eyebrow">Regression signal</span><h3>Recent run pass-rate trend</h3><p>Current suite only; paused human-action runs are excluded.</p></div><Timer size={18}/></div>
        <Trend data={data.trend}/>
      </section>
      <section className="panel">
        <div className="panel-head"><div><span className="eyebrow">Diagnosis</span><h3>Failures by category</h3><p>Uses the latest current-version result for each approved test.</p></div><AlertTriangle size={18}/></div>
        <Bars items={data.failure_categories}/>
      </section>
    </div>

    <div className="analytics-grid three analytics-secondary-grid">
      <section className="panel">
        <div className="panel-head"><div><h3>Validation status</h3><p>Shows what is actually tested, not only what is designed.</p></div></div>
        <Bars items={data.validation_status ?? []}/>
      </section>
      <section className="panel">
        <div className="panel-head"><div><h3>Approved tests by type</h3><p>Useful suite-shape signal without duplicating priority filters.</p></div></div>
        <Bars items={data.tests_by_type}/>
      </section>
      <section className="panel">
        <div className="panel-head"><div><h3>Comparable regression</h3><p>Only compares the same test IDs and test versions.</p></div></div>
        {data.current_vs_previous
          ? <div className="comparison comparison-stacked">
              <div><span>{data.current_vs_previous.current.display_id}</span><b>{data.current_vs_previous.current.pass_rate}%</b><small>{data.current_vs_previous.current.failed} failed · {data.current_vs_previous.current.blocked} blocked · {data.current_vs_previous.case_count ?? 0} tests</small></div>
              <div><span>{data.current_vs_previous.previous.display_id}</span><b>{data.current_vs_previous.previous.pass_rate}%</b><small>{data.current_vs_previous.previous.failed} failed · {data.current_vs_previous.previous.blocked} blocked</small></div>
              <p className={`comparison-delta ${(data.current_vs_previous.pass_rate_delta ?? 0) < 0 ? 'bad' : (data.current_vs_previous.pass_rate_delta ?? 0) > 0 ? 'good' : ''}`}>
                Pass-rate change: {(data.current_vs_previous.pass_rate_delta ?? 0) > 0 ? '+' : ''}{data.current_vs_previous.pass_rate_delta ?? 0}%
              </p>
              {data.current_vs_previous.baseline && <small className="comparison-baseline">Comparable baseline: {data.current_vs_previous.baseline.display_id} · {data.current_vs_previous.baseline.pass_rate}%</small>}
            </div>
          : <div className="empty-small">{data.comparison_note || 'A comparable run appears after the same test set is executed more than once.'}</div>}
      </section>
    </div>

    {!!data.performance_status?.length && <section className="panel overview-performance-panel">
      <div className="panel-head"><div><span className="eyebrow">Performance health</span><h3>Latest test performance classification</h3></div><Clock3 size={18}/></div>
      <Bars items={data.performance_status}/>
    </section>}

    {!!data.suspected_failure_areas.length && <section className="panel instrumentation-note">
      <div><AlertTriangle size={17}/><div><b>Workflow failure areas</b><p>{data.suspected_failure_areas.map(item => `${item.name} (${item.value})`).join(' · ')}</p></div></div>
      <div><b>Evidence source</b><span>Attached workflow analysis only</span></div>
    </section>}

    {error && <div className="alert error">{error}</div>}
  </>;
}

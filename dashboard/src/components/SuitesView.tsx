import { useEffect, useMemo, useState } from 'react';
import {
  Check, ChevronDown, ChevronRight, FileSearch, KeyRound, Play, RefreshCw, Sparkles, X, RotateCcw,
} from 'lucide-react';
import { api } from '../api';
import type { Job, Project, Suite, TestCase } from '../types';

type Props = {
  project: Project | null;
  onRunTestCase?: (suiteId: string, testCaseId: string) => void;
};
type Filter = 'active' | 'all' | 'rejected';

export default function SuitesView({ project, onRunTestCase }: Props) {
  const [suites, setSuites] = useState<Suite[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [scope, setScope] = useState('Full product');
  const [generationPrompt, setGenerationPrompt] = useState('');
  const [job, setJob] = useState<Job | null>(null);
  const [filter, setFilter] = useState<Filter>('active');
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  const load = async () => {
    if (!project) return;
    setError('');
    try {
      const items = await api.suites(project.id);
      setSuites(items);
      setSelectedId(current => items.some(item => item.id === current) ? current : (items[0]?.id || ''));
    } catch (err: any) { setError(err.message); }
  };

  useEffect(() => { setSelectedId(''); setSuites([]); void load(); }, [project?.id]);

  const visibleSuites = useMemo(() => suites.filter(suite => {
    if (filter === 'all') return true;
    if (filter === 'rejected') return suite.status === 'rejected';
    return !['rejected', 'deprecated'].includes(suite.status);
  }), [suites, filter]);

  const selected = suites.find(item => item.id === selectedId) || null;
  const configuredResourceKeys = new Set(Object.keys(project?.fixtures || {}));
  const requiredResourceKeys = Array.from(new Set(
    (selected?.test_cases || []).flatMap(test => test.required_fixture_keys || []),
  )).sort();
  const missingResourceKeys = requiredResourceKeys.filter(key => !configuredResourceKeys.has(key));

  const replaceSuite = (suite: Suite) => {
    setSuites(current => [suite, ...current.filter(item => item.id !== suite.id)]);
    setSelectedId(suite.id);
  };

  const generate = async () => {
    if (!project) return;
    setError(''); setSuccess('');
    try {
      const suite = await api.generate(project.id, { feature: scope.trim() || 'Full product', generation_prompt: generationPrompt.trim() }, setJob);
      setJob(null); replaceSuite(suite);
      setSuccess(`Generated ${suite.test_cases.length} documentation-grounded test cases.`);
    } catch (err: any) { setError(err.message); setJob(null); }
  };

  const approveSuite = async () => {
    if (!selected) return;
    setError(''); setSuccess('');
    try {
      const suite = await api.approveSuite(selected.id);
      replaceSuite(suite);
      setSuccess(missingResourceKeys.length
        ? 'Suite approved. Missing real resources no longer block test startup; the run will pause only if the application actually requests one and no matching saved project resource is available.'
        : 'Suite approved for execution.');
    } catch (err: any) { setError(err.message); }
  };

  const rejectSuite = async () => {
    if (!selected) return;
    const note = window.prompt('Why should this suite be rejected?');
    if (!note?.trim()) return;
    try { replaceSuite(await api.rejectSuite(selected.id, note.trim())); setSuccess('Suite rejected and retained in history.'); }
    catch (err: any) { setError(err.message); }
  };

  const restoreSuite = async () => {
    if (!selected) return;
    try { replaceSuite(await api.restoreSuite(selected.id)); setSuccess('Suite restored to draft.'); }
    catch (err: any) { setError(err.message); }
  };

  const reviewCase = async (test: TestCase, status: string) => {
    if (!selected) return;
    let note = '';
    if (status === 'rejected' || status === 'needs_revision') {
      note = window.prompt(status === 'rejected' ? 'Reason for rejection:' : 'What should be improved?')?.trim() || '';
      if (!note) return;
    }
    try { replaceSuite(await api.reviewCase(selected.id, test.id, status, note)); }
    catch (err: any) { setError(err.message); }
  };

  const improveCase = async (test: TestCase) => {
    if (!selected) return;
    const defaultNote = test.review_note || '';
    const note = window.prompt('Tell the AI exactly what should be improved in this test:', defaultNote)?.trim();
    if (!note) return;
    setError(''); setSuccess('');
    try {
      const suite = await api.improveCase(selected.id, test.id, note, setJob);
      setJob(null); replaceSuite(suite); setSuccess(`${test.id} revised and returned to draft review.`);
    } catch (err: any) { setError(err.message); setJob(null); }
  };

  const workflowReview = async () => {
    if (!selected || !project?.workflow) return;
    try {
      const suite = await api.reviewWorkflow(selected.id, setJob);
      setJob(null); replaceSuite(suite); setSuccess('Workflow advisory review completed.');
    } catch (err: any) { setError(err.message); setJob(null); }
  };

  if (!project) return <div className="empty-state"><h3>Create or select a project first.</h3></div>;

  return <>
    <section className="panel generator-panel">
      <div className="panel-head"><div><span className="eyebrow">Documentation-driven generation</span><h3>Generate production test suite</h3></div><Sparkles size={18}/></div>
      <div className="form-grid generate-grid">
        <label>Scope<input value={scope} onChange={e => setScope(e.target.value)} placeholder="Full product"/><small>Use “Full product” for complete document coverage, or name a specific feature.</small></label>
        <label>Test case generation prompt
          <textarea
            value={generationPrompt}
            onChange={e => setGenerationPrompt(e.target.value)}
            placeholder="Example: Generate fresh-parent onboarding cases for multiple kids, different sports, corrections, interruptions, and recovery."
            rows={3}
            maxLength={6000}
          />
          <small>Describe the kinds of test cases you want in normal language. Your prompt controls scenario emphasis; indexed documentation remains the source of truth for expected behavior.</small>
        </label>
        <button className="primary generate-submit" onClick={generate} disabled={!!job}><Sparkles size={16}/> Generate suite</button>
      </div>
      {job && <div className="job-progress"><div><span>{job.message}</span><b>{job.progress}%</b></div><progress value={job.progress} max="100"/></div>}
    </section>

    <div className="suites-layout">
      <aside className="suite-list panel">
        <div className="panel-head"><h3>Test suites</h3><select className="compact-select" value={filter} onChange={e => setFilter(e.target.value as Filter)}><option value="active">Active</option><option value="rejected">Rejected</option><option value="all">All</option></select></div>
        {visibleSuites.map(suite => <button key={suite.id} className={suite.id === selectedId ? 'selected' : ''} onClick={() => setSelectedId(suite.id)}>
          <div><strong>{suite.name}</strong><span className={`badge ${suite.status}`}>{suite.status}</span></div>
          <span>{suite.test_cases.length} tests · {suite.requirements.length} requirements</span>
        </button>)}
        {!visibleSuites.length && <div className="empty-small">No suites in this view.</div>}
      </aside>

      <div className="suite-detail">
        {!selected ? <div className="empty-state"><h3>Generate a suite to begin.</h3></div> : <>
          <section className="panel">
            <div className="panel-head">
              <div><span className="eyebrow">{selected.status} · v{selected.version}</span><h2>{selected.name}</h2><p>{selected.feature}</p></div>
              <div className="actions wrap">
                {selected.status === 'rejected' ? <button className="secondary" onClick={restoreSuite}><RotateCcw size={15}/> Restore</button> : <>
                  {!selected.approved && <button className="primary" onClick={approveSuite}><Check size={15}/> Approve suite</button>}
                  <button className="secondary danger-text" onClick={rejectSuite}><X size={15}/> Reject suite</button>
                </>}
                {project.workflow && selected.status !== 'rejected' && <button className="secondary" onClick={workflowReview} disabled={!!job}><FileSearch size={15}/> Workflow advisory</button>}
              </div>
            </div>
            <div className="suite-metrics">
              <div><small>Tests</small><b>{selected.test_cases.length}</b></div>
              <div><small>Requirements</small><b>{selected.requirements.length}</b></div>
              <div><small>Generated coverage</small><b>{selected.generation_summary?.coverage_percent ?? 0}%</b></div>
              <div><small>Approved tests</small><b>{selected.test_cases.filter(c => c.approved && c.review_status === 'approved').length}</b></div>
            </div>

            {!!missingResourceKeys.length && <div className="alert warning resource-warning">
              <KeyRound size={16}/><div><b>Some generated cases may need a real external value later.</b><p>Generated resource keys without an exact saved match: {missingResourceKeys.join(', ')}. Tests still start normally. When the application actually asks for a link/code, the QA Agent first uses a matching saved project resource; only if none is available will it pause for tester input at that exact turn.</p></div>
            </div>}

            <details className="compact-details">
              <summary>Generation review</summary>
              {selected.generation_summary?.audit_status === 'skipped_after_error' && <div className="alert warning"><b>Final AI audit did not complete.</b><p>The generated inventory was still required to pass the normal deterministic validation and High-risk requirement coverage gate. Review the audit note below before approval.</p></div>}
              {!!selected.generation_summary?.documentation_conflicts?.length && <div className="alert warning"><b>Documentation conflicts need reviewer attention.</b><ul>{selected.generation_summary.documentation_conflicts.map((item:string,i:number)=><li key={i}>{item}</li>)}</ul></div>}
              {!!selected.generation_summary?.uncovered_requirement_ids?.length && <p><b>Uncovered requirements:</b> {selected.generation_summary.uncovered_requirement_ids.join(', ')}</p>}
              {!!selected.generation_summary?.audit_notes?.length && <><b>Audit notes</b><ul>{selected.generation_summary.audit_notes.map((item:string,i:number)=><li key={i}>{item}</li>)}</ul></>}
              {!!requiredResourceKeys.length && <p><b>Required test resources:</b> {requiredResourceKeys.join(', ')}</p>}
              {selected.generation_summary?.test_type_counts && <p><b>Types:</b> {Object.entries(selected.generation_summary.test_type_counts).map(([k,v])=>`${k}: ${v}`).join(' · ')}</p>}
            </details>

            <details className="compact-details">
              <summary>Requirements ({selected.requirements.length})</summary>
              <div className="requirement-list">{selected.requirements.map(req => <article key={req.id}><div><b>{req.id}</b><span className={`badge risk-${req.risk.toLowerCase()}`}>{req.risk}</span></div><strong>{req.title}</strong><p>{req.description}</p></article>)}</div>
            </details>
          </section>

          {selected.workflow_review?.available && <section className="panel"><div className="panel-head"><h3>Workflow advisory</h3></div><p>{selected.workflow_review.summary}</p>{selected.workflow_review.findings?.length ? <div className="advisory-list">{selected.workflow_review.findings.map((finding,i)=><article key={i}><div><b>{finding.requirement_id}</b><span className={`badge risk-${finding.severity.toLowerCase()}`}>{finding.severity}</span></div><p>{finding.possible_gap}</p><small><b>Possible area:</b> {finding.workflow_area}</small><small><b>Check:</b> {finding.recommended_check}</small></article>)}</div> : <div className="empty-small">No structural concerns were suggested.</div>}</section>}

          <section className="panel">
            <div className="panel-head"><h3>Test cases</h3><span className="muted">AI revises only the selected case.</span></div>
            <div className="test-case-list">
              {selected.test_cases.map(test => <TestCaseCard
                key={test.id}
                test={test}
                configuredResourceKeys={configuredResourceKeys}
                suiteApproved={selected.approved}
                open={!!expanded[test.id]}
                toggle={() => setExpanded(current => ({...current,[test.id]:!current[test.id]}))}
                review={reviewCase}
                improve={improveCase}
                run={() => onRunTestCase?.(selected.id, test.id)}
              />) }
            </div>
          </section>
        </>}
      </div>
    </div>

    {success && <div className="alert success">{success}</div>}
    {error && <div className="alert error">{error}</div>}
  </>;
}

function TestCaseCard({ test, configuredResourceKeys, suiteApproved, open, toggle, review, improve, run }:{
  test:TestCase;
  configuredResourceKeys:Set<string>;
  suiteApproved:boolean;
  open:boolean;
  toggle:()=>void;
  review:(test:TestCase,status:string)=>void;
  improve:(test:TestCase)=>void;
  run:()=>void;
}) {
  const requiredResources = test.required_fixture_keys || [];
  const missing = requiredResources.filter(key => !configuredResourceKeys.has(key));
  return <article className={`test-card ${test.review_status}`}>
    <button className="test-card-head" onClick={toggle}>
      {open ? <ChevronDown size={17}/> : <ChevronRight size={17}/>}<div className="test-title"><div><b>{test.id}</b><span className={`badge ${test.review_status}`}>{test.review_status.replace('_',' ')}</span><span className="badge neutral">{test.priority}</span><span className="badge neutral">{test.test_type}</span>{!!missing.length&&<span className="badge warning">may pause for resource</span>}</div><h4>{test.title}</h4></div>
    </button>
    {open && <div className="test-card-body">
      <div className="scenario-grid">
        <div><small>Persona</small><p>{test.persona}</p></div>
        <div><small>User goal</small><p>{test.user_goal || '—'}</p></div>
        <div><small>Starting state</small><p>{test.state_mode || 'fresh_user'}</p></div>
        <div><small>Disclosure</small><p>{test.disclosure_style || 'progressive'}</p></div>
        <div><small>Requirements</small><p>{test.requirement_ids.join(', ')}</p></div>
      </div>
      {!!requiredResources.length && <div className={`resource-case-box ${missing.length ? 'missing' : 'ready'}`}><b>Potential real test resources</b><p>{requiredResources.map(key => `${key}${configuredResourceKeys.has(key) ? ' ✓ exact saved key' : ' — resolved lazily when requested'}`).join(' · ')}</p></div>}
      {!!Object.keys(test.scenario_data || {}).length && <details className="compact-details"><summary>Scenario facts</summary><dl className="fact-list">{Object.entries(test.scenario_data).map(([key,value])=><div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>)}</dl></details>}
      <div className="case-copy"><b>Expected observable result</b><p>{test.expected_result}</p></div>
      <div className="case-copy"><b>QA objectives</b><ul>{test.objectives.map((item,i)=><li key={i}>{item}</li>)}</ul></div>
      {test.review_note && <div className="alert warning"><b>Reviewer note:</b> {test.review_note}</div>}
      <div className="actions wrap">
        {suiteApproved && test.approved && test.review_status === 'approved' && <button className="secondary small run-case-button" onClick={run}><Play size={14}/> Run this test</button>}
        {test.review_status !== 'approved' && test.review_status !== 'rejected' && <button className="primary small" onClick={() => review(test,'approved')}><Check size={14}/> Approve</button>}
        {test.review_status !== 'needs_revision' && test.review_status !== 'rejected' && <button className="secondary small" onClick={() => review(test,'needs_revision')}><RefreshCw size={14}/> Needs revision</button>}
        {test.review_status !== 'rejected' && <button className="secondary small" onClick={() => improve(test)}><Sparkles size={14}/> AI revise</button>}
        {test.review_status !== 'rejected' ? <button className="secondary small danger-text" onClick={() => review(test,'rejected')}><X size={14}/> Reject</button> : <button className="secondary small" onClick={() => review(test,'draft')}><RotateCcw size={14}/> Restore to draft</button>}
      </div>
    </div>}
  </article>;
}


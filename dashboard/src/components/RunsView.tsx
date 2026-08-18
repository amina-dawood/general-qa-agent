import { useEffect, useMemo, useState } from 'react';
import {
  Bot, CheckCircle2, ChevronDown, ChevronRight, ExternalLink, FileJson2, FileText,
  ListChecks, Play, Search, ShieldAlert, SlidersHorizontal, Trash2, User, UserCheck, XCircle,
} from 'lucide-react';
import { api } from '../api';
import type { Job, Project, Run, RunPreselection, Suite, TestResult } from '../types';

const duration = (ms:number) => ms >= 60000
  ? `${Math.floor(ms/60000)}m ${Math.round(ms%60000/1000)}s`
  : `${(ms/1000).toFixed(1)}s`;

const latency = (ms:number|undefined) => !ms ? '-' : ms >= 1000 ? `${(ms/1000).toFixed(2)}s` : `${Math.round(ms)}ms`;

const humanize = (value?:string) => String(value || '-').replaceAll('_', ' ');

function safeExternalUrl(value?: string) {
  if (!value) return '';
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.toString() : '';
  } catch { return ''; }
}

function shortText(value:string|undefined, max=220) {
  const text=String(value||'').trim();
  if(!text) return '-';
  return text.length>max ? `${text.slice(0,max-1).trim()}...` : text;
}

function primaryReason(result:TestResult) {
  if(result.outcome==='passed') return result.evaluation?.summary || 'The observed conversation satisfied the evaluated behavior.';
  if(result.outcome==='awaiting_human') return result.pending_human_action?.instructions || 'The test is paused for a real human/external action.';
  if(result.outcome==='blocked') return result.blocked_reason || result.evaluation?.summary || 'The test could not continue because a required dependency was unavailable.';
  return result.diagnosis?.observed_problem || result.evaluation?.summary || result.conversation?.error || humanize(result.conversation?.stop_reason);
}

function checkCounts(result:TestResult) {
  const checks=(result.evaluation?.rule_checks||[]).filter((check:any)=>!String(check.name||'').startsWith('legacy_'));
  return {
    fail: checks.filter((check:any)=>String(check.severity||'error')==='error' && !check.passed).length,
    warn: checks.filter((check:any)=>String(check.severity||'error')==='warning').length,
    pass: checks.filter((check:any)=>check.passed && !['warning','info'].includes(String(check.severity||'error'))).length,
    checks,
  };
}

type RunMode = 'filter' | 'selected';

export default function RunsView({ project, preselection, onPreselectionConsumed }:{
  project:Project|null;
  preselection?:RunPreselection|null;
  onPreselectionConsumed?:()=>void;
}) {
  const [runs,setRuns]=useState<Run[]>([]);
  const [suites,setSuites]=useState<Suite[]>([]);
  const [details,setDetails]=useState<Record<string,Run>>({});
  const [selectedId,setSelectedId]=useState('');
  const [suiteId,setSuiteId]=useState('');
  const [priority,setPriority]=useState('All');
  const [limit,setLimit]=useState(10);
  const [runMode,setRunMode]=useState<RunMode>('filter');
  const [selectedCaseIds,setSelectedCaseIds]=useState<string[]>([]);
  const [caseSearch,setCaseSearch]=useState('');
  const [casePriority,setCasePriority]=useState('All');
  const [job,setJob]=useState<Job|null>(null);
  const [detailLoading,setDetailLoading]=useState(false);
  const [deletingRunId,setDeletingRunId]=useState('');
  const [error,setError]=useState('');
  const [expanded,setExpanded]=useState<Record<string,boolean>>({});

  const load=async()=>{
    if(!project)return;
    setError('');
    try{
      const [r,s]=await Promise.all([api.runs(project.id),api.suites(project.id)]);
      setRuns(r); setSuites(s);
      setSelectedId(current=>r.some(item=>item.id===current)?current:(r[0]?.id||''));
      const approved=s.find(item=>item.approved);
      setSuiteId(current=>s.some(item=>item.id===current&&item.approved)?current:(approved?.id||''));
    }catch(err:any){setError(err.message)}
  };

  useEffect(()=>{
    setSelectedId('');
    setSuiteId('');
    setDetails({});
    setSelectedCaseIds([]);
    setCaseSearch('');
    setCasePriority('All');
    setRunMode('filter');
    void load();
  },[project?.id]);

  useEffect(()=>{
    if(!preselection)return;
    setSuiteId(preselection.suite_id);
    setRunMode('selected');
    setSelectedCaseIds(preselection.test_case_ids);
    setCaseSearch('');
    setCasePriority('All');
    onPreselectionConsumed?.();
  },[preselection?.nonce]);

  useEffect(()=>{
    if(!selectedId||details[selectedId])return;
    let active=true; setDetailLoading(true);
    api.run(selectedId).then(run=>{if(active)setDetails(current=>({...current,[run.id]:run}))})
      .catch((err:any)=>active&&setError(err.message)).finally(()=>{if(active)setDetailLoading(false)});
    return()=>{active=false};
  },[selectedId,details]);

  const approvedSuites=useMemo(()=>suites.filter(s=>s.approved),[suites]);
  const selectedSuite=useMemo(()=>approvedSuites.find(suite=>suite.id===suiteId)||null,[approvedSuites,suiteId]);
  const runnableCases=useMemo(()=>
    (selectedSuite?.test_cases||[]).filter(test=>test.approved&&test.review_status==='approved'),
    [selectedSuite],
  );
  const visibleRunnableCases=useMemo(()=>{
    const query=caseSearch.trim().toLowerCase();
    return runnableCases.filter(test=>{
      if(casePriority!=='All'&&test.priority!==casePriority)return false;
      if(!query)return true;
      return `${test.id} ${test.title} ${test.test_type}`.toLowerCase().includes(query);
    });
  },[runnableCases,caseSearch,casePriority]);
  const validSelectedCaseIds=useMemo(()=>{
    const runnable=new Set(runnableCases.map(test=>test.id));
    return selectedCaseIds.filter(id=>runnable.has(id));
  },[selectedCaseIds,runnableCases]);
  const selectedSummary=runs.find(r=>r.id===selectedId)||runs[0]||null;
  const selected=selectedSummary?(details[selectedSummary.id]||null):null;

  const updateRun = (run: Run) => {
    setDetails(current=>({...current,[run.id]:run}));
    setRuns(current=>[
      run,
      ...current.filter(item=>item.id!==run.id),
    ].sort((a,b)=>(b.run_number||0)-(a.run_number||0)));
    setSelectedId(run.id);
  };

  const changeSuite=(id:string)=>{
    setSuiteId(id);
    setSelectedCaseIds([]);
    setCaseSearch('');
    setCasePriority('All');
  };

  const toggleSelectedCase=(id:string)=>{
    setSelectedCaseIds(current=>current.includes(id)?current.filter(item=>item!==id):[...current,id]);
  };

  const selectVisibleCases=()=>{
    const visibleIds=visibleRunnableCases.map(test=>test.id);
    setSelectedCaseIds(current=>Array.from(new Set([...current,...visibleIds])));
  };

  const execute=async()=>{
    if(!project||!suiteId)return;
    setError('');
    if(runMode==='selected'&&!validSelectedCaseIds.length){
      setError('Select at least one approved test case to run.');
      return;
    }
    try{
      const payload=runMode==='selected'
        ? {suite_id:suiteId,priority:'All',limit:validSelectedCaseIds.length,test_case_ids:validSelectedCaseIds}
        : {suite_id:suiteId,priority,limit};
      const run=await api.execute(project.id,payload,setJob);
      setJob(null); updateRun(run);
    }catch(err:any){setError(err.message);setJob(null)}
  };

  const resumeHuman = async (run:Run, completed:boolean, note:string) => {
    setError('');
    try {
      const resumed=await api.resumeHumanAction(run.id,{completed,note},setJob);
      setJob(null); updateRun(resumed);
      if(project) {
        const summaries=await api.runs(project.id);
        setRuns(summaries.map(summary=>summary.id===resumed.id?{...summary,...resumed}:summary));
      }
    } catch(err:any) { setError(err.message); setJob(null); }
  };

  const deleteRun = async (run:Run) => {
    const status=String(run.status||'').toLowerCase();
    if(status==='running'||status==='awaiting_human'){
      setError('Finish or cancel the active run before deleting it.');
      return;
    }
    const baselineNote=run.is_baseline?' This run is the current baseline; deleting it will leave the project without that baseline.':'';
    const confirmed=window.confirm(`Delete ${run.display_id}?\n\nThis permanently removes this run, its run-scoped AI usage, and generated HTML/JSON reports.${baselineNote}\n\nTest suites, documents, project settings, and other runs are not changed. Run numbers are not renumbered.`);
    if(!confirmed)return;

    setDeletingRunId(run.id);
    setError('');
    try{
      await api.deleteRun(run.id);
      const remaining=runs.filter(item=>item.id!==run.id);
      setRuns(remaining);
      setDetails(current=>{const next={...current};delete next[run.id];return next;});
      setExpanded({});
      if(selectedId===run.id)setSelectedId(remaining[0]?.id||'');
    }catch(err:any){
      setError(err.message);
    }finally{
      setDeletingRunId('');
    }
  };

  if(!project)return <div className="empty-state"><h3>Select or create a project first.</h3></div>;

  return <>
    <section className="panel execute-panel">
      <div className="panel-head"><div><span className="eyebrow">Live execution</span><h3>Run approved tests</h3><p>Use filters for regression batches or select exact cases for targeted verification.</p></div><Play size={18}/></div>

      <div className="run-mode-switch" role="tablist" aria-label="Run mode">
        <button type="button" className={runMode==='filter'?'active':''} onClick={()=>setRunMode('filter')}><SlidersHorizontal size={15}/> Filter & limit</button>
        <button type="button" className={runMode==='selected'?'active':''} onClick={()=>setRunMode('selected')}><ListChecks size={15}/> Select test cases</button>
      </div>

      {runMode==='filter'?<div className="form-grid four run-controls">
        <label>Approved suite<select value={suiteId} onChange={e=>changeSuite(e.target.value)}>{approvedSuites.map(s=><option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
        <label>Priority<select value={priority} onChange={e=>setPriority(e.target.value)}><option>All</option><option>High</option><option>Medium</option><option>Low</option></select></label>
        <label>Maximum tests<input type="number" min="1" max="100" value={limit} onChange={e=>setLimit(Math.max(1,Number(e.target.value)||1))}/></label>
        <button className="primary" onClick={execute} disabled={!suiteId||!!job}><Play size={16}/> Execute</button>
      </div>:<div className="targeted-run">
        <div className="form-grid four targeted-run-controls">
          <label>Approved suite<select value={suiteId} onChange={e=>changeSuite(e.target.value)}>{approvedSuites.map(s=><option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
          <label>Find test case<div className="input-with-icon"><Search size={14}/><input value={caseSearch} onChange={e=>setCaseSearch(e.target.value)} placeholder="ID, title or type"/></div></label>
          <label>Priority<select value={casePriority} onChange={e=>setCasePriority(e.target.value)}><option>All</option><option>High</option><option>Medium</option><option>Low</option></select></label>
          <button className="primary" onClick={execute} disabled={!suiteId||!validSelectedCaseIds.length||!!job}><Play size={16}/> Run selected ({validSelectedCaseIds.length})</button>
        </div>
        <div className="case-selector-toolbar">
          <div><b>{validSelectedCaseIds.length} selected</b><span>{runnableCases.length} approved runnable tests</span></div>
          <div className="actions wrap"><button type="button" className="secondary small" onClick={selectVisibleCases} disabled={!visibleRunnableCases.length}>Select visible</button><button type="button" className="secondary small" onClick={()=>setSelectedCaseIds([])} disabled={!selectedCaseIds.length}>Clear</button></div>
        </div>
        <div className="case-selector-list">
          {visibleRunnableCases.map(test=>{const checked=validSelectedCaseIds.includes(test.id);return <label key={test.id} className={`case-selector-row ${checked?'selected':''}`}>
            <input type="checkbox" checked={checked} onChange={()=>toggleSelectedCase(test.id)}/>
            <div className="case-selector-copy"><div><b>{test.id}</b><span className={`badge risk-${test.priority.toLowerCase()}`}>{test.priority}</span><span className="badge neutral">{test.test_type}</span></div><span>{test.title}</span></div>
          </label>})}
          {!visibleRunnableCases.length&&<div className="empty-small">No approved test cases match this search/filter.</div>}
        </div>
        <p className="targeted-run-note">Selected cases execute sequentially. Fresh-user cases keep their existing isolated test identity behavior.</p>
      </div>}

      {job&&<div className="job-progress"><div><span>{job.message}</span><b>{job.progress}%</b></div><progress value={job.progress} max="100"/></div>}
      {!approvedSuites.length&&<div className="alert warning">Approve a suite before execution.</div>}
    </section>

    <div className="runs-layout">
      <aside className="run-list panel">
        <div className="panel-head"><h3>Run history</h3></div>
        {runs.map(run=>{
          const active=run.status==='running'||run.status==='awaiting_human';
          return <div key={run.id} className={`run-history-row ${selectedSummary?.id===run.id?'selected':''}`}>
            <button className="run-history-select" onClick={()=>setSelectedId(run.id)}>
              <div><strong>{run.display_id}</strong>{run.is_baseline&&<span className="badge neutral">baseline</span>}{run.status==='awaiting_human'&&<span className="badge awaiting_human">human action</span>}</div>
              <span>{run.pass_rate}% pass - {humanize(run.status)}</span>
            </button>
            <button
              type="button"
              className="icon-button danger run-delete-button"
              title={active?'Finish or cancel this run before deleting it':`Delete ${run.display_id}`}
              aria-label={`Delete ${run.display_id}`}
              disabled={active||deletingRunId===run.id||!!job}
              onClick={()=>void deleteRun(run)}
            >
              <Trash2 size={14}/>
            </button>
          </div>;
        })}
        {!runs.length&&<div className="empty-small">No runs yet.</div>}
      </aside>
      <div className="run-detail">
        {selected?<RunDetail
          key={selected.id}
          run={selected}
          expanded={expanded}
          setExpanded={setExpanded}
          busy={!!job}
          job={job}
          onResume={(completed,note)=>resumeHuman(selected,completed,note)}
          onBaseline={async()=>{
            await api.baseline(selected.id);
            setRuns(current=>current.map(run=>({...run,is_baseline:run.id===selected.id})));
            setDetails(current=>Object.fromEntries(Object.entries(current).map(([id,run])=>[id,{...run,is_baseline:id===selected.id}])) as Record<string,Run>);
          }}
        />:detailLoading?<div className="loading-panel"><div className="spinner"/><p>Loading run details...</p></div>:<div className="empty-state"><h3>Run data will appear here.</h3></div>}
      </div>
    </div>
    {error&&<div className="alert error">{error}</div>}
  </>;
}

function RunDetail({run,expanded,setExpanded,onBaseline,onResume,busy,job}:{
  run:Run;
  expanded:Record<string,boolean>;
  setExpanded:React.Dispatch<React.SetStateAction<Record<string,boolean>>>;
  onBaseline:()=>void;
  onResume:(completed:boolean,note:string)=>Promise<void>;
  busy:boolean;
  job:Job|null;
}){
  const [humanNote,setHumanNote]=useState('');
  const pending=run.pending_human_action || run.results.find(item=>item.outcome==='awaiting_human')?.pending_human_action || null;
  const actionUrl=safeExternalUrl(pending?.url);

  useEffect(()=>{ setHumanNote(''); },[pending?.requested_at,pending?.test_case_id,pending?.title,pending?.url]);

  const unable = async () => {
    if(!window.confirm('Mark this human step as unable to complete? The current test will be BLOCKED and the run will continue with remaining tests.')) return;
    await onResume(false,humanNote);
  };

  return <>
    <section className="panel">
      <div className="panel-head">
        <div><span className="eyebrow">{run.display_id}</span><h2>{run.suite_name}</h2><p>{new Date(run.started_at).toLocaleString()} - active test time {duration(run.duration_ms)}</p></div>
        <div className="actions wrap">
          {run.reports?.html&&<a className="secondary button-link" href={`/api/runs/${run.id}/reports/html`} target="_blank" rel="noreferrer"><FileText size={15}/> HTML report</a>}
          {run.reports?.json&&<a className="secondary button-link" href={`/api/runs/${run.id}/reports/json`} target="_blank" rel="noreferrer"><FileJson2 size={15}/> JSON report</a>}
          {run.status!=='awaiting_human'&&<button className="secondary" onClick={onBaseline}>{run.is_baseline?'Baseline':'Set baseline'}</button>}
        </div>
      </div>
      <div className="run-metrics">
        <div><span>Passed</span><b>{run.passed_count}</b></div><div><span>Failed</span><b>{run.failed_count}</b></div><div><span>Blocked</span><b>{run.blocked_count}</b></div><div><span>Errors</span><b>{run.error_count}</b></div><div><span>Pass rate</span><b>{run.pass_rate}%</b></div><div><span>AI tokens</span><b>{run.ai_usage?.total_tokens||0}</b></div>
      </div>
    </section>

    {!!run.warnings?.length&&<details className="panel run-notice"><summary>Run notices ({run.warnings.length})</summary><div className="alert warning"><ul>{run.warnings.map((warning,i)=><li key={i}>{warning}</li>)}</ul></div></details>}

    {run.status==='awaiting_human'&&pending&&<section className="panel human-action-box">
      <div className="human-action-head"><UserCheck size={20}/><div><span className="eyebrow">Execution safely paused</span><h3>{pending.title || 'Human action required'}</h3></div></div>
      <p>{pending.instructions || pending.reason || 'Complete the external action, then resume the same test.'}</p>
      {actionUrl&&<a className="primary button-link" href={actionUrl} target="_blank" rel="noreferrer"><ExternalLink size={15}/> Open required link</a>}
      <label className="human-note">
        {pending.requires_input ? (pending.input_label || 'Required tester value') : 'Optional note'}
        <input value={humanNote} onChange={e=>setHumanNote(e.target.value)} placeholder={pending.requires_input?'Enter the real value after completing the step':'Optional note for the run evidence'}/>
        <small>The run is stored while paused. Resume keeps the same test session and sender identity.</small>
      </label>
      <div className="actions wrap">
        <button className="primary" disabled={busy || (!!pending.requires_input && !humanNote.trim())} onClick={()=>void onResume(true,humanNote)}><CheckCircle2 size={15}/> {busy?'Resuming test...':'I completed this step'}</button>
        <button className="secondary danger-text" disabled={busy} onClick={()=>void unable()}><XCircle size={15}/> Cannot complete</button>
      </div>
      {busy&&job&&<div className="job-progress inline-resume-progress"><div><span>{job.message || 'Resuming the same test conversation...'}</span><b>{job.progress}%</b></div><progress value={job.progress} max="100"/></div>}
    </section>}

    <section className="panel"><div className="panel-head"><h3>Test results</h3><span className="muted">Open a case for the short verdict first; raw evidence stays collapsed.</span></div><div className="result-list">{run.results.map((result,index)=><ResultCard key={`${result.test_case_id}-${index}`} result={result} open={!!expanded[`${result.test_case_id}-${index}`]} toggle={()=>setExpanded(current=>({...current,[`${result.test_case_id}-${index}`]:!current[`${result.test_case_id}-${index}`]}))}/>)}</div></section>
  </>;
}

function ResultCard({result,open,toggle}:{result:TestResult;open:boolean;toggle:()=>void}){
  const icon=result.outcome==='passed'?<CheckCircle2 size={17}/>:result.outcome==='awaiting_human'?<UserCheck size={17}/>:result.outcome==='blocked'?<ShieldAlert size={17}/>:<XCircle size={17}/>;
  const semantic=result.evaluation?.semantic;
  const performance=result.evaluation?.performance;
  const counts=checkCounts(result);
  const problematicChecks=counts.checks.filter((check:any)=>!check.passed || String(check.severity||'error')==='warning');
  const turnCount=result.conversation?.turns?.length||0;

  return <article className={`result-card ${result.outcome}`}>
    <button className="result-main" onClick={toggle}>{open?<ChevronDown size={17}/>:<ChevronRight size={17}/>}<span className="outcome-icon">{icon}</span><div className="result-name"><div><b>{result.test_case_id}</b><span className={`badge ${result.outcome}`}>{humanize(result.outcome)}</span><span className="badge neutral">{result.priority}</span>{performance&&performance.status!=='not_measured'&&<span className={`badge perf-${performance.status}`}>perf {performance.status}</span>}</div><h4>{result.title}</h4></div><div className="result-right"><span>{duration(result.duration_ms)}</span><b>{result.outcome==='awaiting_human'?'-':`${Math.round(result.score*100)}%`}</b></div></button>

    {open&&<div className="result-details">
      <section className={`verdict-card ${result.outcome}`}>
        <div className="verdict-head"><div><span className="eyebrow">Quick verdict</span><h4>{result.outcome==='passed'?'Test passed':result.outcome==='awaiting_human'?'Waiting for human action':result.outcome==='blocked'?'Test blocked':'Test failed'}</h4></div><span className={`badge ${result.outcome}`}>{humanize(result.outcome)}</span></div>
        <p>{shortText(primaryReason(result),360)}</p>
        <div className="verdict-grid">
          <div><small>Stop reason</small><b>{humanize(result.conversation.stop_reason)}</b></div>
          <div><small>DeepEval</small><b>{semantic?`${Math.round(semantic.score*100)}%`:'-'}</b></div>
          <div><small>Performance</small><b>{performance&&performance.status!=='not_measured'?`${performance.status} - ${latency(performance.average_ms)} avg`:'-'}</b></div>
          <div><small>Conversation</small><b>{turnCount} messages</b></div>
          <div><small>Test sender</small><b>{result.conversation.sender_identity||'-'}</b></div>
          <div><small>Checks</small><b>{counts.fail} fail / {counts.warn} warn</b></div>
        </div>
      </section>

      {performance&&performance.status!=='not_measured'&&<PerformanceBox performance={performance}/>} 

      {result.outcome!=='passed'&&result.outcome!=='awaiting_human'&&<Failure result={result}/>} 

      {!!problematicChecks.length&&result.outcome!=='awaiting_human'&&<div className="signal-box">
        <div className="signal-head"><b>Important evaluator signals</b><span>{problematicChecks.length} item{problematicChecks.length===1?'':'s'}</span></div>
        <div className="signal-list">{problematicChecks.slice(0,4).map((check:any,i:number)=><div key={i} className={!check.passed?'fail':'warning'}><span>{!check.passed?'x':'!'}</span><p><b>{humanize(check.name)}</b> - {check.message}</p></div>)}</div>
        {problematicChecks.length>4&&<small className="muted">{problematicChecks.length-4} more evaluator signal(s) are available under Evaluation details.</small>}
      </div>}

      <details className="compact-details"><summary>Test setup and expected behavior</summary><div className="scenario-grid">
        <div><small>Persona</small><p>{result.test_case_snapshot?.persona||'-'}</p></div><div><small>User goal</small><p>{result.test_case_snapshot?.user_goal||'-'}</p></div><div><small>Starting state</small><p>{result.test_case_snapshot?.state_mode||'-'}</p></div><div><small>Disclosure</small><p>{result.test_case_snapshot?.disclosure_style||'progressive'}</p></div><div><small>Requirements</small><p>{result.requirement_ids?.join(', ')||'-'}</p></div><div><small>Type</small><p>{result.test_type||'-'}</p></div>
      </div>{result.test_case_snapshot?.scenario_data&&Object.keys(result.test_case_snapshot.scenario_data).length>0&&<dl className="fact-list">{Object.entries(result.test_case_snapshot.scenario_data).map(([k,v])=><div key={k}><dt>{k}</dt><dd>{String(v)}</dd></div>)}</dl>}{!!result.test_case_snapshot?.required_fixture_keys?.length&&<p><b>Test resources:</b> {result.test_case_snapshot.required_fixture_keys.join(', ')}</p>}<p><b>Expected:</b> {result.test_case_snapshot?.expected_result||'-'}</p></details>

      {result.evaluation&&result.outcome!=='awaiting_human'&&<details className="compact-details"><summary>Evaluation details</summary>
        {semantic&&<div className={`semantic-box ${semantic.passed?'passed':'failed'}`}><div><b>{semantic.metric||'DeepEval semantic judge'}</b><span>{Math.round(semantic.score*100)}% - threshold {Math.round(semantic.threshold*100)}%</span></div><p>{semantic.reason}</p></div>}
        {!!result.evaluation.rule_checks?.length&&<div className="rule-list">{result.evaluation.rule_checks.map((check:any,i:number)=>{
          const severity=String(check.severity||'error');
          const marker=severity==='info'?'-':severity==='warning'?'!':check.passed?'OK':'x';
          const cls=severity==='info'?'info':severity==='warning'?'warning':check.passed?'pass':'fail';
          return <div key={i} className={cls}><span>{marker}</span><div><b>{humanize(check.name)}</b><p>{check.message}</p>{check.evidence&&<small>{check.evidence}</small>}</div></div>;
        })}</div>}
        {result.evaluation.evaluation_error&&<div className="alert error">{result.evaluation.evaluation_error}</div>}
      </details>}

      {!!result.conversation.human_actions?.length&&<details className="compact-details"><summary>Human actions ({result.conversation.human_actions.length})</summary><div className="human-history">{result.conversation.human_actions.map((item,i)=><div key={i}><b>{item.action?.title||'Human action'}</b><span className={`badge ${item.status==='completed'?'ready':'blocked'}`}>{humanize(item.status)}</span>{item.note&&<p>{item.note}</p>}</div>)}</div></details>}

      <details className="compact-details conversation-details"><summary>Full conversation ({turnCount} messages)</summary><div className="conversation">{result.conversation.turns.map((turn,i)=><div key={i} className={`message ${turn.role}`}><div className="message-avatar">{turn.role==='user'?<User size={14}/>:<Bot size={14}/>}</div><div className="bubble"><div className="message-head"><b>{turn.role==='user'?'User':'Assistant'}</b><span>{turn.latency_ms?`${(turn.latency_ms/1000).toFixed(2)}s`:''}</span></div><p>{turn.content}</p></div></div>)}</div></details>
    </div>}
  </article>;
}

function PerformanceBox({performance}:{performance:NonNullable<TestResult['evaluation']['performance']>}) {
  const thresholds=performance.thresholds||{};
  const documented=Number(performance.documented_target_ms||0);
  return <div className={`performance-box compact-performance ${performance.status}`}>
    <div className="performance-title"><div><b>Response performance</b><span className={`badge perf-${performance.status}`}>{performance.status}</span></div><p>{performance.message}</p></div>
    <div className="performance-metrics"><div><small>Average</small><b>{latency(performance.average_ms)}</b></div><div><small>P95</small><b>{latency(performance.p95_ms)}</b></div><div><small>Maximum</small><b>{latency(performance.max_ms)}</b></div><div><small>Critical</small><b>{performance.critical_count||0}</b></div></div>
    <details className="performance-policy"><summary>Performance policy</summary><p className="muted">Warning above {latency(thresholds.warning_ms)}, critical above {latency(thresholds.critical_ms)}, hard fail above {latency(thresholds.fail_ms)}.</p>{!!documented&&<p className="muted"><b>Documented target:</b> {latency(documented)} - exceeded on {performance.documented_target_exceeded_count||0} response(s) - <b>{performance.documented_sla_enforced?'enforced':'advisory'}</b>.</p>}</details>
  </div>;
}

function Failure({result}:{result:TestResult}){
  const diagnosis=result.diagnosis||{};
  return <div className="failure-box compact-failure"><div><ShieldAlert size={16}/><b>{diagnosis.failure_category||humanize(result.outcome)}</b></div><p>{shortText(diagnosis.observed_problem||result.evaluation?.summary||result.blocked_reason||result.conversation.error,420)}</p>
    <details><summary>Diagnosis details</summary>
      {!!diagnosis.evidence?.length&&<><h6>Observed evidence</h6><ul>{diagnosis.evidence.map((item,i)=><li key={i}>{item}</li>)}</ul></>}
      {!!diagnosis.likely_causes?.length&&<><h6>Possible causes (inference)</h6><ul>{diagnosis.likely_causes.map((item,i)=><li key={i}>{item.cause} <small>({item.confidence})</small></li>)}</ul></>}
      {!!diagnosis.recommended_checks?.length&&<><h6>Recommended checks</h6><ul>{diagnosis.recommended_checks.map((item,i)=><li key={i}>{item}</li>)}</ul></>}
      {diagnosis.workflow_evidence_available&&!!diagnosis.suspected_components?.length&&<p className="muted"><b>Workflow areas to inspect:</b> {diagnosis.suspected_components.join(', ')}</p>}
    </details>
  </div>;
}

import { useEffect, useMemo, useState } from 'react';
import {
  CheckCircle2, FileJson, FileText, KeyRound, Plus, RefreshCw, Save, Settings2, Trash2, Upload,
} from 'lucide-react';
import { api, waitForJob } from '../api';
import type { DocumentIndexResult } from '../api';
import type { DocumentInfo, Job, Project } from '../types';

type Props = {
  projects: Project[];
  selected: Project | null;
  onProject: (project: Project) => void;
  onReload: () => Promise<void>;
};

type ResourceRow = { id: string; key: string; value: string };

const prettyJson = (value: unknown) => JSON.stringify(value ?? {}, null, 2);
const adapterLabel = (adapter: string) =>
  adapter === 'twilio_webhook' ? 'Twilio-style webhook' :
  adapter === 'generic_webhook' ? 'Generic webhook' : 'Mock / installation test';

function configured(target: Record<string, any>) {
  const adapter = target.adapter || 'mock';
  if (adapter === 'mock' || !String(target.url || '').trim()) return false;
  if (adapter === 'twilio_webhook') {
    return Boolean(String(target.from_number || '').trim() && String(target.to_number || '').trim());
  }
  return true;
}

function indexError(result: DocumentIndexResult) {
  if (!result.failed_count) return '';
  const details = result.failed.slice(0, 2).map(item => `${item.name}: ${item.error}`).join(' | ');
  const extra = result.failed_count > 2 ? ` (+${result.failed_count - 2} more)` : '';
  return `${result.failed_count} document(s) failed to index. ${details}${extra}`;
}

function fixtureRows(fixtures: Record<string, any> | undefined): ResourceRow[] {
  return Object.entries(fixtures || {}).map(([key, value], index) => ({
    id: `${Date.now()}-${index}-${key}`,
    key,
    value: typeof value === 'string' ? value : JSON.stringify(value),
  }));
}

function normalizeResourceKey(value: string) {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_.-]+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^[_\-.]+|[_\-.]+$/g, '');
}

export default function ProjectsView({ projects, selected, onProject, onReload }: Props) {
  const [detail, setDetail] = useState<Project | null>(selected);
  const [target, setTarget] = useState<Record<string, any>>(selected?.target || { adapter: 'mock' });
  const [resources, setResources] = useState<ResourceRow[]>(fixtureRows(selected?.fixtures));
  const [headerEnv, setHeaderEnv] = useState(prettyJson(selected?.target?.header_env));
  const [staticPayload, setStaticPayload] = useState(prettyJson(selected?.target?.static_payload));
  const [editingTarget, setEditingTarget] = useState(true);
  const [newName, setNewName] = useState('');
  const [newDescription, setNewDescription] = useState('');
  const [busy, setBusy] = useState(false);
  const [savingResources, setSavingResources] = useState(false);
  const [resourceSaveState, setResourceSaveState] = useState<'idle'|'dirty'|'saved'|'error'>('idle');
  const [resourceMessage, setResourceMessage] = useState('');
  const [job, setJob] = useState<Job | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  const documents = detail?.documents || [];
  const pending = useMemo(
    () => documents.filter(doc => doc.status !== 'ready' || Number(doc.chunk_count || 0) <= 0),
    [documents],
  );

  const applyProject = (project: Project) => {
    setDetail(project);
    setTarget(project.target || { adapter: 'mock' });
    setResources(fixtureRows(project.fixtures));
    setHeaderEnv(prettyJson(project.target?.header_env));
    setStaticPayload(prettyJson(project.target?.static_payload));
  };

  useEffect(() => {
    setError(''); setSuccess('');
    setResourceSaveState('idle'); setResourceMessage('');
    if (!selected) { setDetail(null); return; }
    let active = true;
    api.project(selected.id)
      .then(project => {
        if (!active) return;
        applyProject(project);
        setEditingTarget(!configured(project.target || {}));
      })
      .catch(err => active && setError(err.message));
    return () => { active = false; };
  }, [selected?.id]);

  const refreshProject = async () => {
    if (!detail) return null;
    const project = await api.project(detail.id);
    applyProject(project);
    await onReload();
    return project;
  };

  const create = async () => {
    const name = newName.trim();
    if (!name) { setError('Project name is required.'); return; }
    setBusy(true); setError(''); setSuccess('');
    try {
      const project = await api.createProject({
        name, description: newDescription.trim(), target: { adapter: 'mock' }, fixtures: {},
      });
      onProject(project);
      setNewName(''); setNewDescription('');
      await onReload();
      setSuccess('Project created. Configure its target and upload requirements.');
    } catch (err: any) { setError(err.message); }
    finally { setBusy(false); }
  };

  const saveConnection = async () => {
    if (!detail) return;
    setBusy(true); setError(''); setSuccess('');
    try {
      let parsedHeaderEnv: Record<string, unknown>;
      let parsedStatic: Record<string, unknown>;
      try {
        parsedHeaderEnv = JSON.parse(headerEnv || '{}');
        parsedStatic = JSON.parse(staticPayload || '{}');
      } catch { throw new Error('Advanced connection JSON fields must contain valid JSON objects.'); }

      const adapter = target.adapter || 'mock';
      if (adapter !== 'mock' && !String(target.url || '').trim()) throw new Error('Webhook URL is required.');
      if (adapter === 'twilio_webhook') {
        if (!String(target.from_number || '').trim()) throw new Error('From number is required.');
        if (!String(target.to_number || '').trim()) throw new Error('To number is required.');
      }

      // Only target configuration is written here. Test resources are stored by
      // their own Save button, so editing resources can never overwrite the
      // already-tested target connection.
      await api.updateProject(detail.id, {
        target: { ...target, header_env: parsedHeaderEnv, static_payload: parsedStatic },
      });
      // PATCH returns project configuration without the document list. Reload
      // the full detail so saving a connection never makes READY documents
      // disappear until the next browser refresh.
      const refreshed = await api.project(detail.id);
      applyProject(refreshed);
      onProject(refreshed);
      await onReload();
      setEditingTarget(!configured(refreshed.target || {}));
      setSuccess('Connection configuration saved.');
    } catch (err: any) { setError(err.message); }
    finally { setBusy(false); }
  };

  const addResource = () => {
    setResources(current => [...current, { id: `${Date.now()}-${Math.random()}`, key: '', value: '' }]);
    setResourceSaveState('dirty'); setResourceMessage('Unsaved changes.');
  };

  const updateResource = (id: string, patch: Partial<ResourceRow>) => {
    setResources(current => current.map(row => row.id === id ? { ...row, ...patch } : row));
    setResourceSaveState('dirty'); setResourceMessage('Unsaved changes.');
  };

  const removeResource = (id: string) => {
    setResources(current => current.filter(row => row.id !== id));
    setResourceSaveState('dirty'); setResourceMessage('Unsaved changes.');
  };

  const saveResources = async () => {
    if (!detail) return;
    setSavingResources(true); setError(''); setSuccess(''); setResourceMessage('');
    try {
      const output: Record<string, string> = {};
      for (const row of resources) {
        const rawKey = row.key.trim();
        const key = normalizeResourceKey(rawKey);
        const value = row.value.trim();
        if (!rawKey && !value) continue;
        if (!key) throw new Error('Every test resource needs a name/key.');
        if (!value) throw new Error(`Test resource "${key}" needs a value.`);
        if (Object.prototype.hasOwnProperty.call(output, key)) throw new Error(`Duplicate test resource key: ${key}`);
        output[key] = value;
      }
      const saved = await api.saveProjectResources(detail.id, output);
      const expected = JSON.stringify(output);
      const returned = JSON.stringify(saved.resources || {});
      if (expected !== returned) throw new Error('The server did not confirm the saved test resources. Please retry.');

      const refreshed = await api.project(detail.id);
      if (JSON.stringify(refreshed.fixtures || {}) !== expected) {
        throw new Error('The test resources did not persist after reload. Nothing was changed in the target connection.');
      }
      applyProject(refreshed);
      onProject(refreshed);
      await onReload();
      setResourceSaveState('saved');
      setResourceMessage(`Saved ${Object.keys(output).length} test resource${Object.keys(output).length===1?'':'s'}. The AI parent will use matching values only after the target assistant asks for them.`);
    } catch (err: any) {
      setResourceSaveState('error'); setResourceMessage(err.message); setError(err.message);
    }
    finally { setSavingResources(false); }
  };

  const finishIndex = async (result: DocumentIndexResult, successText: string) => {
    await refreshProject();
    const message = indexError(result);
    if (message) { setError(message); setSuccess(''); }
    else { setSuccess(successText); setError(''); }
  };

  const uploadDocs = async (files: FileList | null) => {
    if (!detail || !files?.length) return;
    setBusy(true); setError(''); setSuccess('');
    try {
      const queued = await api.uploadDocuments(detail.id, Array.from(files));
      const result = await waitForJob(queued, setJob);
      await finishIndex(result, 'Documents indexed successfully.');
    } catch (err: any) {
      setError(err.message);
      try { await refreshProject(); } catch { /* preserve original error */ }
    } finally { setJob(null); setBusy(false); }
  };

  const reindex = async () => {
    if (!detail || !pending.length) return;
    setBusy(true); setError(''); setSuccess('');
    try {
      const result = await waitForJob(await api.reindexDocuments(detail.id), setJob);
      await finishIndex(result, 'Pending documents indexed successfully.');
    } catch (err: any) {
      setError(err.message);
      try { await refreshProject(); } catch { /* preserve original error */ }
    } finally { setJob(null); setBusy(false); }
  };

  const removeDocument = async (document: DocumentInfo) => {
    if (!detail || busy || removing) return;
    if (!window.confirm(`Remove "${document.name}"?\n\nIts indexed chunks will also be removed. Existing suites and historical runs are kept.`)) return;
    setRemoving(document.id); setError(''); setSuccess('');
    try {
      await api.removeDocument(detail.id, document.id);
      await refreshProject();
      setSuccess(`Removed ${document.name}.`);
    } catch (err: any) { setError(err.message); }
    finally { setRemoving(null); }
  };

  const uploadWorkflow = async (file: File | null) => {
    if (!detail || !file) return;
    setBusy(true); setError(''); setSuccess('');
    try { await api.uploadWorkflow(detail.id, file); await refreshProject(); setSuccess('Workflow attached for advisory analysis.'); }
    catch (err: any) { setError(err.message); }
    finally { setBusy(false); }
  };

  const removeWorkflow = async () => {
    if (!detail || !detail.workflow) return;
    if (!window.confirm('Remove the attached workflow definition? Functional black-box testing will continue normally.')) return;
    setBusy(true); setError(''); setSuccess('');
    try { await api.removeWorkflow(detail.id); await refreshProject(); setSuccess('Workflow removed.'); }
    catch (err: any) { setError(err.message); }
    finally { setBusy(false); }
  };

  const adapter = target.adapter || 'mock';
  const isTwilio = adapter === 'twilio_webhook';
  const isGeneric = adapter === 'generic_webhook';
  const usesWebhook = isTwilio || isGeneric;
  const isConfigured = configured(target);

  return <div className="project-layout">
    <aside className="project-list panel">
      <div className="panel-head"><h3>Projects</h3></div>
      {projects.map(project => <button key={project.id} className={project.id === selected?.id ? 'selected' : ''} onClick={() => onProject(project)}>
        <strong>{project.name}</strong><span>{project.document_count || 0} documents</span>
      </button>)}
      {!projects.length && <div className="empty-small">No projects yet.</div>}
    </aside>

    <div className="project-main">
      <details className="panel create-project">
        <summary><span><b>New project</b><small>Create another testing target without changing Python code.</small></span><Plus size={17}/></summary>
        <div className="form-grid two compact-form">
          <label>Project name<input value={newName} onChange={e => setNewName(e.target.value)} placeholder="Customer Support Bot"/></label>
          <label>Description<input value={newDescription} onChange={e => setNewDescription(e.target.value)} placeholder="Optional"/></label>
        </div>
        <button className="primary" onClick={create} disabled={busy || !newName.trim()}><Plus size={15}/> Create project</button>
      </details>

      {detail && <>
        <section className="panel">
          <div className="panel-head">
            <div><span className="eyebrow">Target connection</span><h3>{detail.name}</h3></div>
            {!editingTarget && isConfigured && <button className="secondary small" onClick={() => setEditingTarget(true)}>Edit connection</button>}
          </div>

          {!editingTarget && isConfigured ? <>
            <div className="target-summary">
              <div className="target-summary-card"><small>Connection</small><b>{adapterLabel(adapter)}</b></div>
              <div className="target-summary-card wide"><small>Endpoint</small><b>{target.url}</b></div>
              {isTwilio && <div className="target-summary-card"><small>Test route</small><b>{target.from_number} → {target.to_number}</b></div>}
            </div>
            <div className="ready-line"><CheckCircle2 size={15}/> Saved and ready for test execution.</div>
          </> : <>
            <div className="form-grid two">
              <label>Connection type<select value={adapter} onChange={e => setTarget({...target, adapter:e.target.value, timeout_seconds:target.timeout_seconds || 45})}>
                <option value="mock">Mock / installation test</option>
                <option value="generic_webhook">Generic webhook</option>
                <option value="twilio_webhook">Twilio-style webhook</option>
              </select></label>
              {usesWebhook && <label>Webhook URL<input value={target.url || ''} onChange={e => setTarget({...target,url:e.target.value})} placeholder="https://..."/></label>}
            </div>

            {isTwilio && <div className="form-grid three">
              <label>From number<input value={target.from_number || ''} onChange={e => setTarget({...target,from_number:e.target.value})} placeholder="+1..."/></label>
              <label>To number<input value={target.to_number || ''} onChange={e => setTarget({...target,to_number:e.target.value})} placeholder="+1..."/></label>
              <label>Timeout seconds<input type="number" min="5" max="300" value={target.timeout_seconds || 45} onChange={e => setTarget({...target,timeout_seconds:Number(e.target.value)})}/></label>
            </div>}

            {isGeneric && <div className="form-grid three">
              <label>Payload<select value={target.payload_mode || 'json'} onChange={e => setTarget({...target,payload_mode:e.target.value})}><option value="json">JSON</option><option value="form">Form</option></select></label>
              <label>Message field<input value={target.message_field || 'message'} onChange={e => setTarget({...target,message_field:e.target.value})}/></label>
              <label>Session field<input value={target.session_field || 'session_id'} onChange={e => setTarget({...target,session_field:e.target.value})}/></label>
            </div>}

            {usesWebhook && <details className="advanced">
              <summary><Settings2 size={15}/> Advanced settings</summary>
              <div className="form-grid three">
                <label>Response path<input value={target.response_path || ''} onChange={e => setTarget({...target,response_path:e.target.value})} placeholder="Optional"/></label>
                <label>Reset URL<input value={target.reset_url || ''} onChange={e => setTarget({...target,reset_url:e.target.value})} placeholder="Optional state-reset endpoint"/><small>Called before each test case only when configured.</small></label>
                {isTwilio && <label>Fresh-user isolation<select value={String(target.isolate_fresh_users ?? true)} onChange={e => setTarget({...target,isolate_fresh_users:e.target.value==='true'})}>
                  <option value="true">Automatic unique test sender (recommended)</option>
                  <option value="false">Reuse configured sender</option>
                </select><small>Automatic mode keeps the saved endpoint and To number unchanged, but gives each fresh-user case a unique synthetic From identity so old target-system state is not reused. Returning/continuation tests keep the configured sender.</small></label>}
              </div>
              {isTwilio && <div className="form-grid two">
                <label>Account SID<input value={target.account_sid || ''} onChange={e => setTarget({...target,account_sid:e.target.value})} placeholder="Optional / .env"/></label>
                <label>Messaging Service SID<input value={target.messaging_service_sid || ''} onChange={e => setTarget({...target,messaging_service_sid:e.target.value})} placeholder="Optional / .env"/></label>
              </div>}
              <div className="form-grid two">
                <label>Header environment mapping<textarea rows={4} value={headerEnv} onChange={e => setHeaderEnv(e.target.value)}/><small>Maps HTTP headers to environment-variable names. Secrets remain in .env.</small></label>
                {isGeneric && <label>Static payload JSON<textarea rows={4} value={staticPayload} onChange={e => setStaticPayload(e.target.value)}/></label>}
              </div>
            </details>}
            {adapter === 'mock' && <p className="muted">Mock mode is for installation verification only.</p>}
            <div className="actions"><button className="primary" onClick={saveConnection} disabled={busy}><Save size={15}/> {busy?'Saving...':'Save connection'}</button>{isConfigured && <button className="secondary" onClick={() => setEditingTarget(false)} disabled={busy}>Cancel</button>}</div>
          </>}
        </section>

        <section className="panel test-resources-panel">
          <div className="panel-head">
            <div><span className="eyebrow">Reusable test data</span><h3>Test resources</h3></div><KeyRound size={18}/>
          </div>
          <p className="muted">Put tester-controlled values here when a test must use something real - for example a TeamSnap/LeagueApps calendar export URL, an ICS link, a dedicated test account ID, or a complete address used for geocoding/timezone/distance. The AI parent uses matching values only when the application asks. Do not use production credentials or a private personal address.</p>
          <div className="resource-list">
            {resources.map(row => <div className="resource-row" key={row.id}>
              <input aria-label="Resource key" value={row.key} onChange={e => updateResource(row.id, { key: e.target.value })} placeholder="teamsnap_calendar_url or valid_us_home_address"/>
              <input aria-label="Resource value" value={row.value} onChange={e => updateResource(row.id, { value: e.target.value })} placeholder="https://... or complete tester-approved value"/>
              <button className="icon-button danger" title="Remove resource" onClick={() => removeResource(row.id)} disabled={savingResources}><Trash2 size={14}/></button>
            </div>)}
            {!resources.length && <div className="empty-small">No tester-controlled resources configured yet. You can still generate suites; when the application actually asks for a missing real link/code/address, the run will pause safely at that turn.</div>}
          </div>
          <div className="actions wrap">
            <button className="secondary" onClick={addResource} disabled={savingResources}><Plus size={15}/> Add resource</button>
            <button className="primary" onClick={saveResources} disabled={savingResources}><Save size={15}/> {savingResources ? 'Saving...' : 'Save test resources'}</button>
            {resourceSaveState==='saved'&&<span className="resource-save-status saved"><CheckCircle2 size={15}/> {resourceMessage}</span>}
            {resourceSaveState==='dirty'&&<span className="resource-save-status dirty">{resourceMessage}</span>}
            {resourceSaveState==='error'&&<span className="resource-save-status error">{resourceMessage}</span>}
          </div>
        </section>

        <section className="analytics-grid two">
          <div className="panel">
            <div className="panel-head"><div><span className="eyebrow">Requirements</span><h3>Documents</h3></div><FileText size={18}/></div>
            <p className="muted">Upload PDF, DOCX, TXT or MD. Exact duplicates are reused; indexing runs in the background.</p>
            <div className="actions wrap">
              <label className="upload-button"><Upload size={15}/> Upload documents<input type="file" multiple accept=".pdf,.docx,.txt,.md" disabled={busy} onChange={e => { const input=e.currentTarget; void uploadDocs(input.files).finally(()=>{input.value='';}); }}/></label>
              {!!pending.length && <button className="secondary" onClick={reindex} disabled={busy}><RefreshCw size={15}/> {job?'Indexing...':`Index pending (${pending.length})`}</button>}
            </div>
            {job && <div className="job-progress"><div><span>{job.message}</span><b>{job.progress}%</b></div><progress value={job.progress} max="100"/></div>}
            <div className="document-list">
              {documents.map(document => <div key={document.id}>
                <FileText size={15}/><span title={document.name}>{document.name}</span>
                <span className="doc-status"><b className={`badge ${document.status}`}>{document.status}</b><small>{document.chunk_count} chunks</small></span>
                <button className="icon-button danger" title={`Remove ${document.name}`} onClick={() => removeDocument(document)} disabled={busy || removing===document.id}><Trash2 size={14}/></button>
              </div>)}
              {!documents.length && <div className="empty-small">No requirements uploaded yet.</div>}
            </div>
          </div>

          <div className="panel">
            <div className="panel-head"><div><span className="eyebrow">Optional</span><h3>Workflow definition</h3></div><FileJson size={18}/></div>
            <p className="muted">Attach n8n JSON only when available. It enriches diagnosis and advisory review; it never decides functional pass/fail by itself.</p>
            {!detail.workflow ? <label className="upload-button secondary"><Upload size={15}/> Attach workflow JSON<input type="file" accept=".json" onChange={e => { const input=e.currentTarget; void uploadWorkflow(input.files?.[0] || null).finally(()=>{input.value='';}); }}/></label> : <div className="workflow-summary">
              <div><b>{detail.workflow.filename}</b><span>{detail.workflow.summary?.node_count || 0} nodes · advisory context enabled</span></div>
              <button className="secondary small" onClick={removeWorkflow} disabled={busy}><Trash2 size={14}/> Remove</button>
            </div>}
            {!detail.workflow && <div className="empty-small">No workflow attached — black-box testing works normally.</div>}
          </div>
        </section>
      </>}

      {success && <div className="alert success">{success}</div>}
      {error && <div className="alert error">{error}</div>}
    </div>
  </div>;
}


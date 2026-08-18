import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { Activity, BarChart3, FolderKanban, FlaskConical, PlayCircle } from 'lucide-react';
import { api } from './api';
import type { Page, Project, RunPreselection } from './types';

const OverviewView = lazy(() => import('./components/OverviewView'));
const ProjectsView = lazy(() => import('./components/ProjectsView'));
const SuitesView = lazy(() => import('./components/SuitesView'));
const RunsView = lazy(() => import('./components/RunsView'));

const nav = [
  { id: 'overview' as Page, label: 'Overview', icon: BarChart3 },
  { id: 'projects' as Page, label: 'Projects', icon: FolderKanban },
  { id: 'suites' as Page, label: 'Test Suites', icon: FlaskConical },
  { id: 'runs' as Page, label: 'Runs', icon: PlayCircle },
];

export default function App() {
  const [page, setPage] = useState<Page>('overview');
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState(localStorage.getItem('qa-project-id') || '');
  const [runPreselection, setRunPreselection] = useState<RunPreselection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const loadProjects = async () => {
    setError('');
    try {
      const items = await api.projects();
      setProjects(items);
      const stillExists = items.some(project => project.id === projectId);
      const selected = stillExists ? projectId : (items[0]?.id || '');
      setProjectId(selected);
      if (selected) localStorage.setItem('qa-project-id', selected);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to load projects.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void loadProjects(); }, []);

  const selectedProject = useMemo(
    () => projects.find(project => project.id === projectId) || null,
    [projects, projectId],
  );

  const chooseProject = (id: string) => {
    setProjectId(id);
    setRunPreselection(null);
    localStorage.setItem('qa-project-id', id);
  };

  const upsertProject = (project: Project) => {
    setProjects(current => [project, ...current.filter(item => item.id !== project.id)]);
    chooseProject(project.id);
  };

  const runSpecificCase = (suiteId: string, testCaseId: string) => {
    setRunPreselection({
      suite_id: suiteId,
      test_case_ids: [testCaseId],
      nonce: Date.now(),
    });
    setPage('runs');
  };

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark"><Activity size={20}/></div>
        <div><strong>QA Agent</strong><span>Production Testing</span></div>
      </div>
      <nav>
        {nav.map(item => {
          const Icon = item.icon;
          return <button key={item.id} className={page === item.id ? 'active' : ''} onClick={() => setPage(item.id)}>
            <Icon size={18}/><span>{item.label}</span>
          </button>;
        })}
      </nav>
      <div className="sidebar-foot"><span className="live-dot"/> Local-first runtime<small>FastAPI · React · SQLite</small></div>
    </aside>

    <main className="main-area">
      <header className="topbar">
        <div><h1>{nav.find(item => item.id === page)?.label}</h1><p>General-purpose conversational QA with AI simulation and DeepEval</p></div>
        <div className="project-switcher">
          <label>Project</label>
          <select value={projectId} onChange={event => chooseProject(event.target.value)} disabled={!projects.length}>
            {!projects.length && <option value="">No project yet</option>}
            {projects.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}
          </select>
        </div>
      </header>

      <section className="content">
        {error && <div className="alert error">{error}</div>}
        {loading
          ? <div className="loading-panel"><div className="spinner"/><p>Loading workspace…</p></div>
          : <Suspense fallback={<div className="loading-panel"><div className="spinner"/></div>}>
              {page === 'projects' && <ProjectsView projects={projects} selected={selectedProject} onProject={upsertProject} onReload={loadProjects}/>} 
              {page === 'overview' && <OverviewView project={selectedProject}/>} 
              {page === 'suites' && <SuitesView project={selectedProject} onRunTestCase={runSpecificCase}/>} 
              {page === 'runs' && <RunsView
                project={selectedProject}
                preselection={runPreselection}
                onPreselectionConsumed={() => setRunPreselection(null)}
              />} 
            </Suspense>}
      </section>
    </main>
  </div>;
}

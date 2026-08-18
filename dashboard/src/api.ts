import type { Analytics, Job, Project, Run, Suite } from './types';

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    let payload: any = {};
    try { payload = await response.json(); } catch { payload = {}; }
    throw new Error(payload.detail || payload.error || `Request failed (${response.status})`);
  }
  return (await response.json()) as T;
}

async function jsonRequest<T>(url: string, method: string, body?: unknown): Promise<T> {
  return request<T>(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export interface DocumentIndexResult {
  total: number;
  ready_count: number;
  failed_count: number;
  ready: any[];
  failed: { id: string; name: string; error: string }[];
}

export async function waitForJob<T>(job: Job<T>, onProgress?: (job: Job<T>) => void): Promise<T> {
  let current = job;
  onProgress?.(current);
  while (current.status === 'queued' || current.status === 'running') {
    await new Promise<void>(resolve => window.setTimeout(resolve, 900));
    current = await request<Job<T>>(`/api/jobs/${current.id}`);
    onProgress?.(current);
  }
  if (current.status === 'failed') throw new Error(current.error || 'Background job failed.');
  if (current.result == null) throw new Error('Job completed without a result.');
  return current.result;
}

export const api = {
  projects: () => request<Project[]>('/api/projects'),
  project: (id: string) => request<Project>(`/api/projects/${id}`),
  createProject: (payload: Partial<Project>) => jsonRequest<Project>('/api/projects', 'POST', payload),
  updateProject: (id: string, payload: unknown) => jsonRequest<Project>(`/api/projects/${id}`, 'PATCH', payload),
  saveProjectResources: (id: string, resources: Record<string, string>) =>
    jsonRequest<{ ok: boolean; resources: Record<string, string>; saved_at: string }>(
      `/api/projects/${id}/resources`,
      'PUT',
      { resources },
    ),
  analytics: (id: string) => request<Analytics>(`/api/projects/${id}/analytics`),
  suites: (id: string) => request<Suite[]>(`/api/projects/${id}/suites`),
  runs: (id: string) => request<Run[]>(`/api/projects/${id}/runs?limit=50`),
  run: (id: string) => request<Run>(`/api/runs/${id}`),
  deleteRun: (id: string) => request<{ ok: boolean; run_id: string; display_id: string }>(`/api/runs/${id}`, { method: 'DELETE' }),

  uploadDocuments: async (id: string, files: File[]) => {
    const form = new FormData();
    files.forEach(file => form.append('files', file));
    return request<Job<DocumentIndexResult>>(`/api/projects/${id}/documents`, { method: 'POST', body: form });
  },
  reindexDocuments: (id: string) => request<Job<DocumentIndexResult>>(`/api/projects/${id}/documents/reindex`, { method: 'POST' }),
  removeDocument: (projectId: string, documentId: string) =>
    request<{ ok: boolean; document_id: string }>(`/api/projects/${projectId}/documents/${documentId}`, { method: 'DELETE' }),

  uploadWorkflow: async (id: string, file: File) => {
    const form = new FormData();
    form.append('file', file);
    return request<any>(`/api/projects/${id}/workflow`, { method: 'POST', body: form });
  },
  removeWorkflow: (id: string) => request<{ ok: boolean }>(`/api/projects/${id}/workflow`, { method: 'DELETE' }),

  generate: async (
    id: string,
    payload: { feature?: string; query?: string; generation_prompt?: string },
    onProgress?: (job: Job<Suite>) => void,
  ) => waitForJob(
    await jsonRequest<Job<Suite>>(`/api/projects/${id}/jobs/generate`, 'POST', {
      feature: payload.feature || 'Full product',
      query: payload.query || '',
      generation_prompt: payload.generation_prompt || '',
    }),
    onProgress,
  ),

  approveSuite: (id: string) => jsonRequest<Suite>(`/api/suites/${id}/approve`, 'POST', { approve_all_cases: true }),
  rejectSuite: (id: string, note: string) => jsonRequest<Suite>(`/api/suites/${id}/reject`, 'POST', { status: 'rejected', note }),
  restoreSuite: (id: string) => jsonRequest<Suite>(`/api/suites/${id}/restore`, 'POST', {}),
  reviewCase: (suiteId: string, caseId: string, status: string, note = '') =>
    jsonRequest<Suite>(`/api/suites/${suiteId}/cases/${caseId}`, 'PATCH', { status, note }),
  improveCase: async (
    suiteId: string,
    caseId: string,
    note: string,
    onProgress?: (job: Job<Suite>) => void,
  ) => waitForJob(
    await jsonRequest<Job<Suite>>(`/api/suites/${suiteId}/cases/${caseId}/jobs/improve`, 'POST', { note }),
    onProgress,
  ),
  reviewWorkflow: async (suiteId: string, onProgress?: (job: Job<Suite>) => void) => waitForJob(
    await jsonRequest<Job<Suite>>(`/api/suites/${suiteId}/jobs/workflow-review`, 'POST', {}),
    onProgress,
  ),

  execute: async (
    projectId: string,
    payload: { suite_id: string; priority: string; limit: number; test_case_ids?: string[] },
    onProgress?: (job: Job<Run>) => void,
  ) => waitForJob(
    await jsonRequest<Job<Run>>(`/api/projects/${projectId}/jobs/execute`, 'POST', payload),
    onProgress,
  ),

  resumeHumanAction: async (
    runId: string,
    payload: { completed: boolean; note?: string },
    onProgress?: (job: Job<Run>) => void,
  ) => waitForJob(
    await jsonRequest<Job<Run>>(`/api/runs/${runId}/jobs/resume-human`, 'POST', payload),
    onProgress,
  ),

  baseline: (runId: string) => jsonRequest<{ ok: boolean; run_id: string }>(`/api/runs/${runId}/baseline`, 'POST', {}),
};


const config = window.__APP_CONFIG__ || {}
export const projectId = config.projectId || import.meta.env.VITE_PROJECT_ID || import.meta.env.PROJECT_ID || ''

export class ApiError extends Error {
  constructor(message, status, detail, url) { super(message); this.name = 'ApiError'; this.status = status; this.detail = detail; this.url = url }
}

async function request(method, path, body, signal) {
  let response
  try {
    response = await fetch(path, {
      method, signal,
      headers: { Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch (error) {
    if (error.name === 'AbortError') throw error
    throw new ApiError('Could not connect to the backend', null, error.message, path)
  }
  const text = await response.text()
  let data = {}; try { data = text ? JSON.parse(text) : {} } catch { data = { raw: text } }
  if (!response.ok) throw new ApiError(`Backend request failed: ${response.status} ${response.statusText}`, response.status, data.detail || data.message || text, path)
  return data
}

export const api = {
  health: signal => request('GET', '/health', null, signal),
  createContext: description => request('POST', '/api/projects/context', { project_id: projectId, project_description: description }),
  commits: limit => request('GET', `/api/projects/${encodeURIComponent(projectId)}/commits${limit ? `?limit=${limit}` : ''}`),
  prReview: number => request('GET', `/api/projects/${encodeURIComponent(projectId)}/prs/${number}`),
  approve: (number, approved, selected_messages) => request('POST', `/api/projects/${encodeURIComponent(projectId)}/prs/${number}/approve`, { approved, selected_messages }),
  context: () => request('GET', `/api/projects/${encodeURIComponent(projectId)}/context`),
  updateMappings: (default_slack_channel_id, stakeholders) => request('PUT', `/api/projects/${encodeURIComponent(projectId)}/slack-mappings`, { default_slack_channel_id, stakeholders }),
}

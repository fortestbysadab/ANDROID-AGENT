/* All calls to the agent. Every state-changing request carries the CSRF
   header the server requires; GETs that the browser may navigate to (file
   downloads) deliberately do not, because a navigation cannot set one. */

const HEADERS = { 'Content-Type': 'application/json', 'X-Android-Agent': '1' };

async function request(path, body) {
  const options = body === undefined
    ? { headers: { 'X-Android-Agent': '1' } }
    : { method: 'POST', headers: HEADERS, body: JSON.stringify(body) };

  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error('The agent is not reachable. Is it still running?');
  }
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    /* A non-JSON body only matters if the status was bad. */
  }
  if (!response.ok) {
    const error = new Error(payload.error || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

export const api = {
  state: () => request('/api/state'),
  login: (token) => request('/api/login', { token }),
  logout: () => request('/api/logout', {}),
  send: (text) => request('/api/message', { text }),
  approve: (id, approve) => request('/api/approval', { approval_id: id, approve }),
  newChat: () => request('/api/new', {}),
  openChat: (id) => request('/api/conversations/open', { id }),
  deleteChat: (id) => request('/api/conversations/delete', { id }),
  clearChats: () => request('/api/conversations/clear', {}),
  files: () => request('/api/files'),
  tools: () => request('/api/tools'),
  schedule: () => request('/api/schedule'),
};

export const downloadUrl = (name) =>
  `/api/files/download?name=${encodeURIComponent(name)}`;

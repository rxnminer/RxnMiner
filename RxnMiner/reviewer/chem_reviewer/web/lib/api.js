// Thin fetch wrapper. Lives in lib/ so components don't fetch directly.

function endpoint(url) {
  const base = typeof globalThis.CHEMRXN_REVIEWER_API_BASE === "string"
    ? globalThis.CHEMRXN_REVIEWER_API_BASE.replace(/\/+$/, "") : "";
  return `${base}${url}`;
}

async function get(url) {
  const target = endpoint(url);
  const r = await fetch(target);
  if (!r.ok) {
    const txt = await r.text().catch(() => r.statusText);
    throw new Error(`GET ${target} → ${r.status}: ${txt.slice(0, 160)}`);
  }
  return r.json();
}

async function post(url, body) {
  const target = endpoint(url);
  const r = await fetch(target, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const txt = await r.text().catch(() => r.statusText);
    const error = new Error(`POST ${target} → ${r.status}: ${txt.slice(0, 160)}`);
    error.status = r.status;
    throw error;
  }
  return r.json();
}

export const api = {
  papers: () => get("/api/papers"),
  manifest: (slug) => get(`/api/paper/${encodeURIComponent(slug)}/manifest`),
  task: (slug, tid) =>
    get(`/api/paper/${encodeURIComponent(slug)}/task/${encodeURIComponent(tid)}`),
  saveTask: (slug, tid, data, proposalId, agentProposalId = null) =>
    post(
      `/api/paper/${encodeURIComponent(slug)}/task/${encodeURIComponent(tid)}/save`,
      {
        data,
        proposal_id: proposalId,
        ...(agentProposalId ? { agent_proposal_id: agentProposalId } : {}),
      }
    ),
  previewTask: (slug, tid, data) =>
    post(
      `/api/paper/${encodeURIComponent(slug)}/task/${encodeURIComponent(tid)}/preview`,
      { data }
    ),
  taskVersions: (slug, tid) =>
    get(
      `/api/paper/${encodeURIComponent(slug)}/task/${encodeURIComponent(tid)}/versions`
    ),
  taskVersionDiff: (slug, tid, version) =>
    get(
      `/api/paper/${encodeURIComponent(slug)}/task/${encodeURIComponent(tid)}/versions/${encodeURIComponent(version)}/diff`
    ),
  restoreTaskVersion: (slug, tid, version) =>
    post(
      `/api/paper/${encodeURIComponent(slug)}/task/${encodeURIComponent(tid)}/versions/${encodeURIComponent(version)}/restore`,
      {}
    ),
  undoTask: (slug, tid) =>
    post(
      `/api/paper/${encodeURIComponent(slug)}/task/${encodeURIComponent(tid)}/undo`,
      {}
    ),
  diffTask: (slug, tid) =>
    get(
      `/api/paper/${encodeURIComponent(slug)}/task/${encodeURIComponent(tid)}/diff`
    ),
  molparser: (slug, tid) =>
    get(
      `/api/paper/${encodeURIComponent(slug)}/molparser/${encodeURIComponent(tid)}`
    ),
  anchorUrl: (slug, tid) =>
    endpoint(`/api/paper/${encodeURIComponent(slug)}/anchor/${encodeURIComponent(tid)}`),
  pageUrl: (slug, n) =>
    endpoint(`/api/paper/${encodeURIComponent(slug)}/page/${n}`),
  pdfUrl: (slug) => endpoint(`/api/paper/${encodeURIComponent(slug)}/pdf`),
};

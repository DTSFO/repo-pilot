const $ = (id) => document.getElementById(id);
const TASK_STATUS_CLASSES = new Set(['pending','running','completed','guarded','failed','cancelled']);
let currentTask = null;
let currentRepository = null;
let streamController = null;
let apiToken = null;

function requestApiToken() {
  const supplied = window.prompt('RepoPilot API Token（仅保存在当前页面内存）');
  if (supplied === null || !supplied.trim()) return false;
  apiToken = supplied.trim();
  return true;
}

async function request(path, options = {}, allowTokenPrompt = true) {
  const headers = new Headers(options.headers || {});
  if (apiToken) headers.set('Authorization', `Bearer ${apiToken}`);
  const response = await fetch(path, {...options, headers});
  if (response.status === 401 && allowTokenPrompt && requestApiToken())
    return request(path, options, false);
  return response;
}

async function responseError(response) {
  try {
    const payload = await response.json();
    return payload.error?.message || String(response.status);
  } catch (_) { return String(response.status); }
}

async function api(path, options = {}) {
  const response = await request(path, options);
  if (!response.ok) throw new Error(await responseError(response));
  return response.json();
}

function setStatus(node, status) {
  node.className = 'status';
  if (TASK_STATUS_CLASSES.has(status) || ['ready','indexing','archived','error'].includes(status))
    node.classList.add(status);
  node.textContent = String(status);
}

async function refreshRepositories() {
  const repositories = await api('/api/repositories');
  if (!currentRepository && repositories.length) currentRepository = repositories[0].id;
  const fragment = document.createDocumentFragment();
  for (const repo of repositories) {
    const li = document.createElement('li');
    li.dataset.id = String(repo.id);
    if (repo.id === currentRepository) li.classList.add('active');
    const name = document.createElement('div'); name.className = 'goal'; name.textContent = repo.name;
    const status = document.createElement('span'); setStatus(status, repo.status);
    const revision = document.createElement('div'); revision.className = 'muted';
    revision.textContent = repo.indexed_revision ? `revision ${repo.indexed_revision.slice(0, 12)}` : '未索引';
    const actions = document.createElement('div'); actions.className = 'row repository-actions';
    const select = document.createElement('button'); select.type = 'button'; select.className = 'secondary';
    select.textContent = '选择';
    select.onclick = async (event) => {
      event.stopPropagation(); currentRepository = li.dataset.id; currentTask = null;
      await refreshRepositories(); await refreshTasks();
    };
    actions.append(select);
    if (repo.id !== '00000000-0000-0000-0000-000000000001') {
      const archive = document.createElement('button'); archive.type = 'button'; archive.className = 'secondary';
      archive.textContent = '归档';
      archive.onclick = async (event) => {
        event.stopPropagation();
        if (!window.confirm(`确认归档“${repo.name}”？历史任务和报告仍会保留。`)) return;
        try {
          const response = await request(`/api/repositories/${encodeURIComponent(repo.id)}`, {method: 'DELETE'});
          if (!response.ok) throw new Error(await responseError(response));
          if (currentRepository === repo.id) currentRepository = null;
          await refreshRepositories(); await refreshTasks();
        } catch (error) { appendEventLabel(`归档失败: ${error.message}`); }
      };
      actions.append(archive);
    }
    li.append(name, status, revision, actions);
    li.onclick = async () => { currentRepository = li.dataset.id; currentTask = null; await refreshRepositories(); await refreshTasks(); };
    fragment.append(li);
  }
  $('repositories').replaceChildren(fragment);
  const selected = repositories.find((item) => item.id === currentRepository);
  $('repository-meta').textContent = selected ? `${selected.name} · ${selected.source_location}` : '未选择仓库';
}

async function refreshTasks() {
  const suffix = currentRepository ? `?repository_id=${encodeURIComponent(currentRepository)}` : '';
  const tasks = await api(`/api/tasks${suffix}`);
  const fragment = document.createDocumentFragment();
  for (const task of tasks) {
    const li = document.createElement('li'); li.dataset.id = String(task.id);
    if (task.id === currentTask) li.classList.add('active');
    const goal = document.createElement('div'); goal.className = 'goal'; goal.textContent = String(task.goal);
    const status = document.createElement('span'); setStatus(status, task.status);
    if (task.degraded) status.textContent += ' · degraded';
    li.append(goal, status); li.onclick = () => selectTask(li.dataset.id); fragment.append(li);
  }
  $('tasks').replaceChildren(fragment);
}

function renderEvidence(evidence) {
  const container = $('evidence');
  if (!evidence.length) { container.textContent = '—'; return; }
  const fragment = document.createDocumentFragment();
  for (const item of evidence) {
    const block = document.createElement('div');
    if (item.review_status === 'rejected') block.classList.add('rejected');
    const citation = document.createElement('code'); citation.textContent = String(item.citation);
    const quote = document.createElement('span'); quote.textContent = ` · ${String(item.quote).slice(0, 180)}`;
    block.append(citation, quote); fragment.append(block);
  }
  container.replaceChildren(fragment);
}

function renderTrustedMarkdownFragment(html) {
  const parsed = new DOMParser().parseFromString(html, 'text/html');
  $('report').replaceChildren(...Array.from(parsed.body.childNodes).map((node) => document.importNode(node, true)));
}

async function renderTaskReport(task) {
  if (!task.final_report) { $('report').textContent = '(运行中…)'; return; }
  try {
    const report = await api(`/api/tasks/${task.id}/report`);
    renderTrustedMarkdownFragment(report.html);
  } catch (error) {
    $('report').textContent = '报告渲染失败，请重试或使用下载功能获取原始报告。';
    appendEventLabel(`report render error: ${error.message}`);
  }
}

async function selectTask(id) {
  currentTask = id; await refreshTasks();
  const task = await api(`/api/tasks/${id}`);
  $('task-meta').textContent = `${task.status} · v${task.version}`;
  await renderTaskReport(task);
  renderEvidence(await api(`/api/tasks/${id}/evidence`));
  follow(id);
}

function appendEventLabel(label) { $('events').textContent += `${String(label)}\n`; $('events').scrollTop = $('events').scrollHeight; }

function logTaskEvent(type, data) {
  let label = type;
  if (type.startsWith('provider.request.')) {
    try {
      const event = JSON.parse(data); const payload = event.payload || {};
      const phase = type.replace('provider.request.', '');
      label = `${payload.purpose || 'model'}: ${phase}`;
    } catch (_) {}
  }
  appendEventLabel(label);
}

async function consumeTaskStream(response, id, cursor) {
  if (!response.body) throw new Error('Streaming response body is unavailable.');
  const reader = response.body.getReader(); const decoder = new TextDecoder();
  let buffer = ''; let eventType = 'message'; let eventId = ''; let dataLines = [];
  const dispatch = () => {
    if (!dataLines.length) { eventType = 'message'; eventId = ''; return false; }
    const dispatchedType = eventType || 'message';
    logTaskEvent(dispatchedType, dataLines.join('\n'));
    if (/^\d+$/.test(eventId)) cursor.value = Number(eventId);
    const terminal = dispatchedType === 'stream.end';
    eventType = 'message'; eventId = ''; dataLines = [];
    if (dispatchedType.startsWith('task.') || terminal) void selectTaskSilently(id);
    return terminal;
  };
  while (true) {
    const {value, done} = await reader.read(); buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
    let newline;
    while ((newline = buffer.indexOf('\n')) >= 0) {
      let line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
      if (line.endsWith('\r')) line = line.slice(0, -1);
      if (!line) { if (dispatch()) { await reader.cancel(); return true; } continue; }
      if (line.startsWith(':')) continue;
      const separator = line.indexOf(':'); const field = separator < 0 ? line : line.slice(0, separator);
      let valueText = separator < 0 ? '' : line.slice(separator + 1); if (valueText.startsWith(' ')) valueText = valueText.slice(1);
      if (field === 'event') eventType = valueText; else if (field === 'id') eventId = valueText; else if (field === 'data') dataLines.push(valueText);
    }
    if (done) { if (buffer) dataLines.push(buffer); return dispatch(); }
  }
}

function follow(id) {
  if (streamController) streamController.abort(); $('events').textContent = '';
  const controller = new AbortController(); streamController = controller;
  void streamTask(id, controller).catch((error) => { if (!controller.signal.aborted) appendEventLabel(error.message); });
}

async function streamTask(id, controller) {
  const cursor = {value: 0};
  while (!controller.signal.aborted && currentTask === id) {
    try {
      const suffix = cursor.value ? `?after=${encodeURIComponent(cursor.value)}` : '';
      const response = await request(`/api/tasks/${id}/stream${suffix}`, {headers:{'Accept':'text/event-stream'}, signal:controller.signal});
      if (!response.ok) throw new Error(await responseError(response));
      if (await consumeTaskStream(response, id, cursor)) return;
    } catch (error) { if (controller.signal.aborted) return; appendEventLabel(`reconnecting: ${error.message}`); }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

async function selectTaskSilently(id) {
  const task = await api(`/api/tasks/${id}`); $('task-meta').textContent = `${task.status} · v${task.version}`;
  if (task.final_report) await renderTaskReport(task);
  renderEvidence(await api(`/api/tasks/${id}/evidence`)); await refreshTasks();
}

async function downloadReport(format) {
  if (!currentTask) return;
  const response = await request(`/api/tasks/${currentTask}/exports/${format}`);
  if (!response.ok) { appendEventLabel(await responseError(response)); return; }
  const blob = await response.blob(); const url = URL.createObjectURL(blob); const link = document.createElement('a');
  link.href = url; link.download = `repopilot-${currentTask}.${format}`; link.click(); URL.revokeObjectURL(url);
}

$('create').onclick = async () => {
  const goal = $('goal').value.trim(); if (!goal || !currentRepository) return;
  const task = await api('/api/tasks', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({goal, repository_id:currentRepository})});
  $('goal').value = ''; await selectTask(task.id);
};

$('ingest').onclick = async () => {
  if (!currentRepository) return; $('ingest').disabled = true;
  try { const report = await api('/api/ingest', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({repository_id:currentRepository})});
    $('ingest-info').textContent = `已索引 ${report.ingested_documents} 文档 / ${report.chunks} chunks`; await refreshRepositories();
  } catch (error) { $('ingest-info').textContent = error.message; } finally { $('ingest').disabled = false; }
};

for (const button of document.querySelectorAll('[data-export]')) button.onclick = () => downloadReport(button.dataset.export);
$('add-repository').onclick = () => $('repository-dialog').showModal();
$('cancel-repository').onclick = () => $('repository-dialog').close();
$('repository-form').onsubmit = async (event) => {
  event.preventDefault(); const form = new FormData(event.target); const payload = {name:String(form.get('name') || '') || null};
  const local = String(form.get('local_path') || '').trim(); const git = String(form.get('git_url') || '').trim();
  if (local) payload.local_path = local; else if (git) payload.git_url = git; else return;
  try { const repo = await api('/api/repositories', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(payload)});
    currentRepository = repo.id; $('repository-dialog').close(); event.target.reset(); await refreshRepositories(); await refreshTasks();
  } catch (error) { $('repository-error').textContent = error.message; }
};

void Promise.all([refreshRepositories(), refreshTasks()]).catch((error) => appendEventLabel(`API error: ${error.message}`));

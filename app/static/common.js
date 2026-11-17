// Shared by the conveyor (app.js) and packing (packing.js) screens.
const $ = (id) => document.getElementById(id);

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) { /* not JSON */ }
    throw new Error(msg);
  }
  return res.json();
}

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Live state from /api/stream (server-sent events).
function connect(onState) {
  const es = new EventSource('/api/stream');
  es.onmessage = (e) => { $('conn').classList.add('on'); onState(JSON.parse(e.data)); };
  es.onerror = () => $('conn').classList.remove('on');
}

function startClock() {
  const tick = () => { $('clock').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); };
  tick();
  setInterval(tick, 10000);
}

function openDialog(id) { $(id).classList.remove('hidden'); }
function closeDialogs() { document.querySelectorAll('.dialog').forEach((d) => d.classList.add('hidden')); }
document.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', closeDialogs));

// ---- job picker: numeric keypad (no keyboard on the touchscreen) + ERP search ----

const picker = { query: '', job: null };

function renderQuery() {
  const q = $('query');
  q.textContent = picker.query || 'Type job no.';
  q.classList.toggle('empty', !picker.query);
  if ($('btn-manual')) $('btn-manual').classList.toggle('hidden', picker.query.length < 3);
}

function renderJobs(jobs, hint) {
  $('job-hint').textContent = hint;
  const list = $('job-list');
  list.innerHTML = '';
  for (const job of jobs) {
    const li = document.createElement('li');
    li.innerHTML = `<div class="no">${esc(job.job_no)}</div><div class="name">${esc(job.job_name)}</div>` +
      `<div class="client">${esc(job.client)}${job.order_qty ? ` · qty ${job.order_qty}` : ''}</div>`;
    li.addEventListener('click', () => {
      picker.job = job;
      list.querySelectorAll('li').forEach((x) => x.classList.remove('on'));
      li.classList.add('on');
      $('btn-go').disabled = false;
    });
    list.appendChild(li);
  }
}

async function openJobPicker() {
  picker.query = ''; picker.job = null;
  $('btn-go').disabled = true;
  renderQuery();
  openDialog('dlg-start');
  const { jobs } = await api('/api/recent-jobs');
  renderJobs(jobs, jobs.length ? 'Recent jobs' : 'Type part of the job number and tap Find');
}

async function searchJobs() {
  if (picker.query.length < 2) return;
  renderJobs([], 'Searching ERP…');
  const { jobs, error } = await api(`/api/jobs?q=${encodeURIComponent(picker.query)}`);
  picker.job = null; $('btn-go').disabled = true;
  renderJobs(jobs, error || (jobs.length ? `${jobs.length} open jobs` : 'No open job found'));
}

document.querySelectorAll('.keypad button').forEach((b) => b.addEventListener('click', () => {
  const key = b.dataset.key;
  if (key === 'del') picker.query = picker.query.slice(0, -1);
  else if (key === 'search') { searchJobs(); return; }
  else if (picker.query.length < 12) picker.query += b.textContent;
  renderQuery();
}));

// ---- settings: +/- steppers ----

let settings = [];

function fmtSetting(s) {
  const digits = s.step < 1 ? Math.max(0, -Math.floor(Math.log10(s.step))) : 0;
  return `${Number(s.value).toFixed(digits)} ${s.unit}`.trim();
}

function renderSettings() {
  const rows = $('settings-rows');
  rows.innerHTML = '';
  settings.forEach((s) => {
    const row = document.createElement('div');
    row.className = 'setting';
    row.innerHTML = `<span>${esc(s.label)}</span><button data-d="-1">−</button><span class="val">${esc(fmtSetting(s))}</span><button data-d="1">+</button>`;
    row.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
      const v = s.value + Number(b.dataset.d) * s.step;
      s.value = Math.min(s.max, Math.max(s.min, Math.round(v / s.step) * s.step));
      row.querySelector('.val').textContent = fmtSetting(s);
    }));
    rows.appendChild(row);
  });
}

async function openSettings() {
  settings = (await api('/api/settings')).settings;
  renderSettings();
  $('photo-info').textContent = '';
  openDialog('dlg-settings');
}

$('btn-save').addEventListener('click', async () => {
  const body = {};
  for (const s of settings) (body[s.section] ??= {})[s.key] = s.value;
  settings = (await api('/api/settings', body)).settings;
  closeDialogs();
});

$('btn-test').addEventListener('click', async () => {
  const st = await api('/api/test-photo', {});
  $('photo-info').textContent = `Sharpness ${st.last.sharpness} · brightness ${st.last.brightness}`;
});

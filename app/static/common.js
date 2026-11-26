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
function closeDialogs() {
  const settingsOpen = $('dlg-settings') && !$('dlg-settings').classList.contains('hidden');
  document.querySelectorAll('.dialog').forEach((d) => d.classList.add('hidden'));
  if (settingsOpen) settingsClosed();
}
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
      if (s.section === 'camera') tryCamera();
    }));
    rows.appendChild(row);
  });
}

async function openSettings() {
  settings = (await api('/api/settings')).settings;
  renderSettings();
  $('photo-info').textContent = '';
  cam.tried = false;
  showPhoto(null);
  openDialog('dlg-settings');
}

$('btn-save').addEventListener('click', async () => {
  const body = {};
  for (const s of settings) (body[s.section] ??= {})[s.key] = s.value;
  settings = (await api('/api/settings', body)).settings;
  cam.tried = false;   // now saved: closing must not undo it
  closeDialogs();
});

// ---- restart / shut down the Pi (Settings) ----

const POWER = {
  reboot: {
    ask: 'Restart the Pi?\n\nA running lot is stopped and saved first. The station comes back by itself in about a minute.',
    title: 'Restarting…', text: 'The station comes back by itself in about a minute.',
  },
  poweroff: {
    ask: 'Shut down the Pi?\n\nA running lot is stopped and saved first. To start again you must switch the power off and on.',
    title: 'Shutting down…',
    text: 'Wait until the screen goes dark and the ACT light on the Pi board stops blinking (about 20 seconds). Then you can switch off the power.',
  },
};

async function power(action) {
  const p = POWER[action];
  if (!confirm(p.ask)) return;
  try {
    const res = await api(`/api/system/${action}`, {});
    closeDialogs();
    const msg = $('power-msg');
    msg.innerHTML = `<h2>${esc(p.title)}</h2><p>${esc(p.text)}</p>` +
      (res.mock ? '<p>(Mock run: nothing was restarted.) <button onclick="location.reload()">Back</button></p>' : '');
    msg.classList.remove('hidden');
  } catch (e) {
    $('photo-info').textContent = e.message;
  }
}

$('btn-reboot').addEventListener('click', () => power('reboot'));
$('btn-poweroff').addEventListener('click', () => power('poweroff'));

// ---- camera check in Settings: live preview, test photo, zoom ----

const cam = { live: false, timer: null, tryTimer: null, tried: false, best: 0, zoom: 1, tx: 0, ty: 0, drag: null };
const ZOOMS = [1, 1.5, 2, 3, 4, 6, 8];

function settingValue(section, key) {
  const s = settings.find((x) => x.section === section && x.key === key);
  return s ? s.value : null;
}

// Exposure / gain changes show in the preview at once; only Save keeps them.
function tryCamera() {
  clearTimeout(cam.tryTimer);
  cam.tryTimer = setTimeout(async () => {
    await api('/api/camera/try', { exposure_us: settingValue('camera', 'exposure_us'), gain: settingValue('camera', 'gain') });
    cam.tried = true;
    $('photo-info').textContent = 'Not saved yet: tap Save to keep it';
  }, 250);
}

function settingsClosed() {
  stopPreview();
  clearTimeout(cam.tryTimer);
  if (cam.tried) api('/api/camera/revert', {});   // closed without Save: back to the saved camera setting
  cam.tried = false;
}

function showPhoto(src, caption, live) {
  const img = $('cam-img');
  if (src) img.src = src; else img.removeAttribute('src');
  $('cam-empty').classList.toggle('hidden', !!src);
  $('cam-cap').classList.toggle('hidden', !caption);
  $('cam-cap').classList.toggle('live', !!live);
  $('cam-cap').textContent = caption || '';
  if (!src) $('cam-stats').innerHTML = '';
}

function renderStats(st) {
  if (!st) { $('cam-stats').innerHTML = ''; return; }
  const b = st.brightness, g = st.glare ?? 0;
  const light = b < 60 ? 'Too dark' : b > 200 ? 'Too bright' : 'OK';
  const sharp = cam.live && cam.best ? `best ${cam.best}` : 'higher = sharper';
  const glare = g > 2 ? 'Glare on cover' : 'OK';
  $('cam-stats').innerHTML =
    `<div class="${light === 'OK' ? '' : 'bad'}"><span>Brightness (need 60–200)</span><b>${b}</b><span class="verdict">${light}</span></div>` +
    `<div><span>Sharpness (focus)</span><b>${st.sharpness}</b><span>${sharp}</span></div>` +
    `<div class="${glare === 'OK' ? '' : 'bad'}"><span>Burnt-out white</span><b>${g} %</b><span class="verdict">${glare}</span></div>`;
}

async function startPreview() {
  const { blocked } = await api('/api/preview/stats');
  if (blocked) { $('photo-info').textContent = blocked; return; }
  cam.live = true; cam.best = 0;
  $('btn-live').textContent = 'Stop preview';
  $('btn-live').classList.add('primary');
  showPhoto(`/api/preview.mjpg?t=${Date.now()}`, 'Live', true);
  let lastFrame = null, stale = 0;
  cam.timer = setInterval(async () => {
    try {
      const { blocked: why, stats } = await api('/api/preview/stats');
      if (why) { stopPreview(); $('photo-info').textContent = why; return; }
      if (!stats) return;
      // The Pi ends the stream after 5 minutes; a frame number that stops moving means it ended.
      stale = stats.frame === lastFrame ? stale + 1 : 0;
      lastFrame = stats.frame;
      if (stale >= 6) { stopPreview(); $('photo-info').textContent = 'Preview stopped: tap Live preview again'; return; }
      cam.best = Math.max(cam.best, stats.sharpness);
      renderStats(stats);
    } catch (e) { /* next tick */ }
  }, 500);
}

function stopPreview() {
  if (!cam.live) return;
  cam.live = false;
  clearInterval(cam.timer);
  $('btn-live').textContent = 'Live preview';
  $('btn-live').classList.remove('primary');
  showPhoto(null);   // dropping the src closes the stream on the Pi
}

$('btn-live').addEventListener('click', () => (cam.live ? stopPreview() : startPreview()));

$('cam-img').addEventListener('error', () => {
  if (cam.live) { stopPreview(); $('photo-info').textContent = 'Preview stopped: tap Live preview again'; }
});

$('btn-test').addEventListener('click', async () => {
  stopPreview();
  const st = await api('/api/test-photo', {});
  showPhoto(`/api/photo/last.jpg?n=${st.photo_seq}`, `Test photo ${new Date().toLocaleTimeString()}`);
  renderStats(st.last);
  $('photo-info').textContent = cam.tried ? 'Not saved yet: tap Save to keep it' : '';
});

// Zoom: buttons and mouse wheel; drag to move around a zoomed photo.
function clampPan() {
  const view = $('cam-view').getBoundingClientRect(), img = $('cam-img');
  const ratio = (img.naturalWidth || 16) / (img.naturalHeight || 10);
  let w = view.width, h = view.width / ratio;
  if (h > view.height) { h = view.height; w = h * ratio; }
  const mx = Math.max(0, (w * cam.zoom - view.width) / 2), my = Math.max(0, (h * cam.zoom - view.height) / 2);
  cam.tx = Math.min(mx, Math.max(-mx, cam.tx));
  cam.ty = Math.min(my, Math.max(-my, cam.ty));
}

function applyZoom() {
  clampPan();
  $('cam-img').style.transform = `translate(${cam.tx}px, ${cam.ty}px) scale(${cam.zoom})`;
  $('zoom-val').textContent = `${cam.zoom}×`;
  $('cam-view').classList.toggle('zoomed', cam.zoom > 1);
}

function setZoom(z) {
  const k = z / cam.zoom;
  cam.tx *= k; cam.ty *= k; cam.zoom = z;
  applyZoom();
}

function stepZoom(d) {
  const i = ZOOMS.indexOf(cam.zoom) + d;
  if (i >= 0 && i < ZOOMS.length) setZoom(ZOOMS[i]);
}

$('zoom-in').addEventListener('click', () => stepZoom(1));
$('zoom-out').addEventListener('click', () => stepZoom(-1));
$('zoom-fit').addEventListener('click', () => { cam.tx = 0; cam.ty = 0; setZoom(1); });
$('cam-view').addEventListener('wheel', (e) => { e.preventDefault(); stepZoom(e.deltaY < 0 ? 1 : -1); }, { passive: false });
$('cam-view').addEventListener('pointerdown', (e) => {
  if (e.target.closest('.zoom') || cam.zoom === 1) return;
  cam.drag = { x: e.clientX - cam.tx, y: e.clientY - cam.ty };
  $('cam-view').setPointerCapture(e.pointerId);
});
$('cam-view').addEventListener('pointermove', (e) => {
  if (!cam.drag) return;
  cam.tx = e.clientX - cam.drag.x; cam.ty = e.clientY - cam.drag.y;
  applyZoom();
});
['pointerup', 'pointercancel'].forEach((t) => $('cam-view').addEventListener(t, () => { cam.drag = null; }));

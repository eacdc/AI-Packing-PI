// Conveyor station screen. Shared helpers (api, job picker, settings) are in common.js.
let S = null;
let shownPhoto = null;
let shownRef = null;
let startMode = 'page';

function render() {
  const lot = S.lot;
  $('station').textContent = S.station;
  $('mode').textContent = S.mode_label || '';
  $('mode').classList.toggle('hidden', !lot);
  $('rec').classList.toggle('hidden', !S.recording);
  $('job').textContent = lot ? `${lot.job_no} · ${lot.job_name}` : '';
  $('ok').textContent = lot ? lot.ok : 0;
  $('alerts').textContent = lot ? lot.alerts : 0;
  $('rate').textContent = S.rate;

  let cls = 'idle', title = 'No lot running', msg = S.message;
  if (S.alert) {
    cls = S.alert.kind === 'gap' ? 'warn' : 'alert';
    title = S.alert.title;
    msg = `${S.alert.message} (${S.alert.time})` + (S.alert.count > 1 ? ` · ${S.alert.count} alerts not checked` : '');
  } else if (S.state === 'teaching') {
    cls = 'teaching'; title = `Learning ${S.teach.have} / ${S.teach.need}`;
  } else if (S.state === 'running') {
    cls = 'ok'; title = 'OK';
  } else if (S.state === 'teach_failed') {
    cls = 'warn'; title = 'Re-teach needed';
  }
  $('status').className = `status ${cls}`;
  $('status-title').textContent = title;
  $('status-msg').textContent = msg;

  $('btn-ack').classList.toggle('hidden', !S.alert);
  $('btn-stop').disabled = !lot;
  $('btn-reteach').disabled = !lot;

  // Left photo: the odd book while an alert is open, otherwise the latest photo.
  let src, caption = 'Last book', detail = '';
  if (S.alert && S.alert.image) {
    src = `/files/${S.alert.image}`;
    caption = 'Odd book';
  } else if (S.photo_seq !== null) {
    src = `/api/photo/last.jpg?n=${S.photo_seq}`;
    const l = S.last;
    if (!l) detail = '';
    else if (l.sharpness !== undefined) detail = `sharpness ${l.sharpness} · brightness ${l.brightness}`;
    else if (l.ncc !== null && l.ncc !== undefined) detail = `match ${l.ncc} · ${l.ms} ms`;
    else if (l.inliers !== undefined) detail = `points ${l.inliers}/${l.inlier_threshold} · patch ${l.diff_patch}/${l.diff_threshold} · ${l.ms} ms`;
  }
  if (src && src !== shownPhoto) { $('img-last').src = src; shownPhoto = src; }
  if (!src && shownPhoto) { $('img-last').removeAttribute('src'); shownPhoto = null; }
  $('cap-last').textContent = caption;
  $('cap-detail').textContent = detail;

  const ref = S.ref_version === null ? null : `/api/photo/ref.jpg?v=${S.ref_version}`;
  if (ref !== shownRef) {
    if (ref) $('img-ref').src = ref; else $('img-ref').removeAttribute('src');
    shownRef = ref;
  }
}

// Start lot: mode + job.
document.querySelectorAll('.seg button').forEach((b) => b.addEventListener('click', () => {
  startMode = b.dataset.mode;
  document.querySelectorAll('.seg button').forEach((x) => x.classList.toggle('on', x === b));
}));

$('btn-manual').addEventListener('click', () => {
  picker.job = { job_no: picker.query, job_name: '', client: '' };
  renderJobs([picker.job], 'Using the typed number (ERP not used)');
  $('job-list').firstChild.click();
});

$('btn-go').addEventListener('click', async () => {
  if (!picker.job) return;
  await api('/api/lot/start', { ...picker.job, mode: startMode, reteach: $('chk-reteach').checked });
  closeDialogs();
});

// History: lots, then the alerts of one lot.
async function openHistory() {
  $('history-title').textContent = 'History';
  const { lots } = await api('/api/lots');
  const rows = lots.map((l) => `<tr data-lot="${l.id}"><td>${esc(l.started.replace('T', ' '))}</td>` +
    `<td>${esc(l.job_no)}<br><span class="hint">${esc(l.job_name)}</span></td><td>${l.mode === 'cover' ? 'With cover' : 'Without cover'}</td>` +
    `<td class="num">${l.ok}</td><td class="num">${l.alerts}</td></tr>`).join('');
  $('history-body').innerHTML = `<table><tr><th>Started</th><th>Job</th><th>Mode</th><th class="num">Good</th><th class="num">Alerts</th></tr>${rows}</table>`;
  $('history-body').querySelectorAll('tr[data-lot]').forEach((tr) => tr.addEventListener('click', () => showLot(tr.dataset.lot)));
  openDialog('dlg-history');
}

async function showLot(id) {
  const { alerts } = await api(`/api/lots/${id}/alerts`);
  $('history-title').textContent = `Lot ${id}: ${alerts.length} alerts`;
  $('history-body').innerHTML = alerts.length ? '<div class="thumbs">' + alerts.map((a) =>
    `<figure>${a.image ? `<img src="/files/${esc(a.image)}" alt="">` : ''}<figcaption><span>${esc(a.ts.slice(11))}</span><span>${esc(a.message)}</span></figcaption></figure>`).join('') + '</div>'
    : '<p class="hint">No alerts in this lot.</p>';
}

$('btn-start').addEventListener('click', () => { $('chk-reteach').checked = false; openJobPicker(); });
$('btn-stop').addEventListener('click', () => { if (confirm('Stop this lot?')) api('/api/lot/stop', {}); });
$('btn-reteach').addEventListener('click', () => { if (confirm('Learn this title again from the next good books?')) api('/api/lot/reteach', {}); });
$('btn-ack').addEventListener('click', () => api('/api/alert/ack', {}));
$('btn-settings').addEventListener('click', openSettings);
$('btn-history').addEventListener('click', openHistory);

connect((state) => { S = state; render(); });
startClock();

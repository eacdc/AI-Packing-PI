// Packing station screen. Shared helpers (api, job picker, settings) are in common.js.
let S = null;
let shownPhoto = null;

// Titles of one series share a long prefix ("Meus amigos Um Livro Recortado ..."); show what differs.
function shortNames(names) {
  if (names.length < 2) return names;
  let prefix = names[0];
  for (const n of names) while (!n.startsWith(prefix)) prefix = prefix.slice(0, -1);
  prefix = prefix.slice(0, prefix.lastIndexOf(' ') + 1);
  return names.map((n) => n.slice(prefix.length) || n);
}

function render() {
  const session = S.session;
  $('station').textContent = S.station;
  $('rec').classList.toggle('hidden', !S.recording);
  $('job').textContent = session ? `${session.job_no} · ${session.job_name}` : '';
  $('labels').textContent = S.labels;
  $('done').textContent = S.boxes_done.complete || 0;
  $('short').textContent = S.boxes_done.short || 0;

  // Status panel
  let cls = 'idle', title = 'Not packing', msg = S.message;
  const box = S.box;
  if (S.alert) {
    cls = ['short_box', 'packed_label'].includes(S.alert.kind) ? 'warn' : 'alert';
    title = S.alert.title;
    msg = `${S.alert.message} (${S.alert.time})`;
  } else if (S.teach) {
    cls = 'teaching'; title = `Teaching ${S.teach.have} / ${S.teach.need}`;
  } else if (box) {
    const packed = box.items.reduce((a, i) => a + i.counted, 0);
    const total = box.items.reduce((a, i) => a + i.qty, 0);
    cls = 'ok'; title = `Box ${String(box.barcode).padStart(7, '0')}  ${packed} / ${total}`;
  } else if (session) {
    cls = 'teaching'; title = 'Show box label';
  }
  $('status').className = `status ${cls}`;
  $('status-title').textContent = title;
  $('status-msg').textContent = msg;

  // Title rows: the open box's titles with counts, or all titles of the session.
  const rows = box ? box.items : S.titles;
  const names = shortNames(rows.map((r) => r.job_name));
  $('box-caption').textContent = box ? 'In this box' : (session ? 'Titles in these boxes' : 'Titles');
  $('box-total').textContent = box && box.again ? 'opened again' : '';
  const list = $('items');
  list.innerHTML = '';
  rows.forEach((r, i) => {
    const li = document.createElement('li');
    const teaching = S.teach && S.teach.job_no === r.job_no;
    li.className = teaching ? 'teaching' : '';
    let right = '';
    if (box) {
      const pct = Math.min(100, (r.counted / r.qty) * 100);
      const state = r.counted > r.qty ? 'over' : (r.counted === r.qty ? 'full' : '');
      right = `<span class="bar ${state}"><i style="width:${pct}%"></i></span><b class="cnt ${state}">${r.counted}/${r.qty}</b>`;
    }
    const taught = r.taught;
    li.innerHTML = `<span class="nm" title="${esc(r.job_name)}">${esc(names[i])}</span>${right}` +
      (taught ? '' : `<button data-job="${esc(r.job_no)}" class="teach">${teaching ? '…' : 'Teach'}</button>`);
    list.appendChild(li);
  });
  list.querySelectorAll('button.teach').forEach((b) => b.addEventListener('click', () => api('/api/teach', { job_no: b.dataset.job })));

  $('btn-ack').classList.toggle('hidden', !S.alert);
  $('btn-reopen').classList.toggle('hidden', !(S.alert && S.alert.kind === 'packed_label'));
  $('btn-cancel-teach').classList.toggle('hidden', !S.teach);
  $('btn-stop').disabled = !session;

  // Photo: the problem book while an alert is open, otherwise the latest photo.
  let src = null, caption = 'Camera', detail = '';
  if (S.alert && S.alert.image) {
    src = `/files/${S.alert.image}`; caption = 'This book';
  } else if (S.photo_seq !== null) {
    src = `/api/photo/last.jpg?n=${S.photo_seq}`;
    const l = S.last;
    if (l && l.title && l.reason === 'ok') { caption = l.title; detail = `${l.ms} ms`; }
    else if (l && l.sharpness !== undefined) detail = `sharpness ${l.sharpness} · brightness ${l.brightness}`;
  }
  if (src !== shownPhoto) {
    if (src) $('img-last').src = src; else $('img-last').removeAttribute('src');
    shownPhoto = src;
  }
  $('cap-last').textContent = caption;
  $('cap-detail').textContent = detail;
}

async function openBoxes() {
  const { boxes } = await api('/api/boxes');
  const rows = boxes.map((b) => {
    const packed = b.items.reduce((a, i) => a + i.counted, 0);
    const total = b.items.reduce((a, i) => a + i.qty, 0);
    return `<tr><td>${String(b.barcode).padStart(7, '0')}</td><td>${esc(b.ended.replace('T', ' ').slice(0, 16))}</td>` +
      `<td>${b.status === 'complete' ? 'Complete' : '<b style="color:#f87171">Short</b>'}</td><td class="num">${packed} / ${total}</td></tr>`;
  }).join('');
  $('boxes-body').innerHTML = `<table><tr><th>Label</th><th>Closed</th><th>Status</th><th class="num">Books</th></tr>${rows}</table>`;
  openDialog('dlg-boxes');
}

$('btn-go').addEventListener('click', async () => {
  if (!picker.job) return;
  $('start-error').textContent = 'Loading label list and title photos… (a few seconds)';
  $('btn-go').disabled = true;
  try {
    await api('/api/session/start', picker.job);
    $('start-error').textContent = '';
    closeDialogs();
  } catch (e) {
    $('start-error').textContent = e.message;
  }
});

$('btn-start').addEventListener('click', () => { $('start-error').textContent = ''; openJobPicker(); });
$('btn-stop').addEventListener('click', () => { if (confirm('Stop packing this job?')) api('/api/session/stop', {}); });
$('btn-ack').addEventListener('click', () => api('/api/alert/ack', {}));
$('btn-reopen').addEventListener('click', () => api('/api/box/open', { barcode: S.alert.barcode }));
$('btn-cancel-teach').addEventListener('click', () => api('/api/teach/cancel', {}));
$('btn-boxes').addEventListener('click', openBoxes);
$('btn-settings').addEventListener('click', openSettings);

connect((state) => { S = state; render(); });
startClock();

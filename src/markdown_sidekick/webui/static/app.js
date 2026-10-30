// Markdown Sidekick web UI. Python (webui/bridge.py) owns all state and
// sends snapshots to app.receive(); this file only draws them and forwards
// clicks to window.pywebview.api. No framework, no build step.
'use strict';

const ICON = {
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
  plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
  doc: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/>',
  image: '<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
  audio: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
  warn: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  copy: '<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  retry: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  save: '<path d="M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"/><path d="M7 3v4a1 1 0 0 0 1 1h7"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2m-7.07-17.07 1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  help: '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
  sliders: '<path d="M20 7h-9"/><path d="M14 17H5"/><circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/>',
  cup: '<path d="M10 2v2"/><path d="M14 2v2"/><path d="M16 8a1 1 0 0 1 1 1v8a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4V9a1 1 0 0 1 1-1h14a4 4 0 1 1 0 8h-1"/><path d="M6 2v2"/>',
};
const icon = (name, size = 16, width = 2) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${width}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name]}</svg>`;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const $ = (sel) => document.querySelector(sel);
const api = () => window.pywebview.api;

const S = {
  boot: null, seq: 0, files: [], busy: false, status: '', overall: 0, canSave: false,
  selId: null, view: 'rendered', clean: true, ocr: true, exportStyle: 'single', aiTarget: 'Claude',
  theme: 'system', preview: null, previewKey: '', hiddenWarn: new Set(), showTech: false,
  dialog: null, help: null, helpTab: 0, probe: false,
};
const sysDark = window.matchMedia('(prefers-color-scheme: dark)');

function applyTheme() {
  const dark = S.theme === 'dark' || (S.theme === 'system' && sysDark.matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  const b = $('#themeBtn');
  b.innerHTML = icon(dark ? 'sun' : 'moon');
  b.title = dark ? 'Switch to light theme' : 'Switch to dark theme';
}

// ---- Python -> page ----------------------------------------------------
window.app = {
  receive(snap) {
    // Snapshots are sent from several Python threads: an older one can arrive
    // after a newer one, and one can arrive before the page has booted.
    if (!S.boot || snap.seq <= S.seq) return;
    S.seq = snap.seq;
    const before = new Map(S.files.map((f) => [f.id, f.status]));
    Object.assign(S, { files: snap.files, busy: snap.busy, status: snap.status, overall: snap.overall, canSave: snap.canSave, ocr: snap.ocr });
    if (S.selId && !S.files.some((f) => f.id === S.selId)) S.selId = null;
    // Nothing selected yet: show the first file that finishes.
    if (!S.selId) {
      const first = S.files.find((f) => f.status === 'done' || f.status === 'warn');
      if (first) S.selId = first.id;
    }
    drawMain();
    drawFooter();
    $('#status').textContent = S.status;
    const sel = S.files.find((f) => f.id === S.selId);
    const changed = sel && before.get(sel.id) !== sel.status;
    if (!sel) drawPreview(null);
    else if (changed || !S.preview || S.preview.id !== sel.id) loadPreview();
    else if (sel.status === 'converting') drawConverting(sel);
  },
  previewReady(id) {
    if (id === S.selId) loadPreview();
  },
};

// ---- drawing -----------------------------------------------------------
function drawMain() {
  const main = $('#main');
  if (!S.files.length) {
    if (!main.querySelector('.empty')) {
      main.innerHTML = `
        <div class="empty"><div class="dropzone">
          <div class="hero">
            <span style="color:var(--color-accent)">${icon('upload', 40, 2)}</span>
            <h1>Drop files here to turn them into clean Markdown.</h1>
            <p>PDFs, Office documents, images, audio and video. Every engine runs on this computer — nothing is uploaded.</p>
            <div class="hero-actions">
              <button class="btn btn-primary btn-lg" data-act="add">${icon('plus', 18, 2.4)}Add files…</button>
              <span class="muted" style="font-size:13px">or press Ctrl+O</span>
            </div>
          </div>
          <div class="formats">${S.boot.formats.map((f) => `<div><div class="label">${esc(f.cat)}</div><div class="list">${esc(f.list)}</div></div>`).join('')}</div>
        </div></div>`;
      S.preview = null;
    }
    return;
  }
  if (!main.querySelector('.split')) {
    main.innerHTML = `
      <div class="split">
        <section class="files" aria-label="Files">
          <div class="files-head">
            <span class="label">Files</span><span class="count" id="count"></span>
            <button class="btn btn-secondary" data-act="add" title="Ctrl+O">${icon('plus', 15, 2.4)}Add files…</button>
          </div>
          <div class="filelist" id="filelist" role="listbox" tabindex="0" aria-label="Files to convert"></div>
          <div class="files-foot">
            <button class="switch" role="switch" id="ocrSw" data-act="ocr"><span class="track"></span>OCR images &amp; scanned PDFs</button>
            <div class="actions">
              <button class="btn btn-ghost" data-act="remove" id="removeBtn" title="Delete">Remove selected</button>
              <button class="btn btn-ghost" data-act="clear">Clear all</button>
            </div>
          </div>
        </section>
        <section class="preview" aria-label="Preview">
          <div class="pv-head" id="pvHead"></div>
          <div id="warnbar"></div>
          <div class="pv-body" id="pvBody"></div>
          <div id="stats"></div>
        </section>
      </div>`;
    S.preview = null;
  }
  $('#count').textContent = S.files.length;
  $('#ocrSw').setAttribute('aria-checked', String(S.ocr));
  $('#removeBtn').disabled = !S.selId;
  const list = $('#filelist');
  const scroll = list.scrollTop;
  list.innerHTML = S.files.map(rowHtml).join('') +
    `<div class="dropmore">${icon('upload', 18)}Drop more files here — they convert automatically</div>`;
  list.scrollTop = scroll;
}

function rowHtml(f) {
  const ico = f.kind === 'image' ? 'image' : f.kind === 'audio' ? 'audio' : 'doc';
  let right = '';
  if (f.status === 'done') right = `<span class="badge">${esc(f.engine)}</span>`;
  else if (f.status === 'warn') right = `<span class="badge badge-warn">${icon('warn', 12, 2.4)}${esc(f.engine)}</span>`;
  else if (f.status === 'error') right = `<span class="badge badge-err">${icon('x', 12, 2.6)}${esc(f.errCode)}</span>`;
  else if (f.status === 'converting') right = `<span class="pct">${f.pct ? f.pct + '%' : '…'}</span>`;
  else right = '<span class="muted" style="font-size:12px">Queued</span>';
  const bar = f.status === 'converting' && f.pct ? `<div class="bar" style="width:${f.pct}%"></div>` : '';
  return `<div class="row" role="option" data-id="${esc(f.id)}" aria-selected="${f.id === S.selId}">
    <span class="ico">${icon(ico, 20, 1.8)}</span>
    <div style="min-width:0"><div class="name" title="${esc(f.name)}">${esc(f.name)}</div><div class="meta">${esc(f.meta)}</div></div>
    <div>${right}</div>${bar}</div>`;
}

function drawFooter() {
  const bar = $('#exportbar');
  bar.hidden = !S.files.length;
  if (bar.hidden) return;
  const ai = S.exportStyle === 'ai';
  bar.innerHTML = `
    ${S.busy ? `<div class="overall" style="width:${S.overall}%"></div>` : ''}
    <span class="label">Output</span>
    <div class="seg">${S.boot.styles.map((o) => `<button data-style="${o.id}" title="${esc(o.desc)}" aria-pressed="${S.exportStyle === o.id}">${esc(o.label)}</button>`).join('')}</div>
    <span class="label" style="margin-left:8px;opacity:${ai ? 1 : 0.45}">Optimize for</span>
    <select class="input" id="target" ${ai ? '' : 'disabled'}>${S.boot.targets.map((t) => `<option ${t === S.aiTarget ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>
    <button class="btn btn-primary save" data-act="save" title="Ctrl+S" ${S.canSave ? '' : 'disabled'}>${icon('save')}Save Markdown…</button>`;
}

function drawPreview(p) {
  const head = $('#pvHead'), body = $('#pvBody');
  if (!head) return;
  const sel = S.files.find((f) => f.id === S.selId);
  const done = p && p.state === 'done';
  head.innerHTML = `
    <div class="pv-title"><span class="nm" title="${esc(sel ? sel.name : '')}">${esc(sel ? sel.name : 'No file selected')}</span>${done ? `<span class="badge">${esc(p.engine)}</span>` : ''}</div>
    <div class="seg">${[['rendered', 'Rendered'], ['raw', 'Raw']].map(([v, l]) => `<button data-view="${v}" aria-pressed="${S.view === v}">${l}</button>`).join('')}</div>
    <button class="switch" role="switch" data-act="clean" aria-checked="${S.clean}"><span class="track"></span>Clean output</button>
    <button class="btn btn-secondary" data-act="copy" title="Ctrl+Shift+C" ${done ? '' : 'disabled'}>${icon('copy', 15)}Copy</button>`;
  const w = done && p.warning && !S.hiddenWarn.has(S.selId) ? p.warning : null;
  $('#warnbar').innerHTML = w ? `<div class="warnbar">
      <span style="color:var(--color-accent)">${icon('warn', 18, 2.2)}</span>
      <div class="txt"><strong>${esc(w.title)}</strong> ${esc(w.next)} <span class="muted">(${esc(w.code)}${w.more ? `, +${w.more} more` : ''})</span></div>
      ${w.action ? `<button class="btn btn-secondary" data-fix="${esc(w.action)}">${esc(w.actionLabel)}</button>` : ''}
      <button class="btn btn-icon" data-act="hideWarn" title="Dismiss">${icon('x', 14, 2.4)}</button></div>` : '';
  $('#stats').innerHTML = done ? `<div class="stats">${p.stats.map((s) => `<div class="${s.alert ? 'alert' : ''}"><div class="k">${esc(s.label)}</div><div class="v">${esc(s.value)}</div><div class="n" title="${esc(s.note)}">${esc(s.note)}</div></div>`).join('')}</div>` : '';
  if (!sel) { body.innerHTML = '<div class="pv-state muted">Select a file to preview its Markdown.</div>'; return; }
  if (!p || p.state === 'converting') { drawConverting(sel); return; }
  if (p.state === 'queued') { body.innerHTML = '<div class="pv-state"><div class="label">Queued</div><div class="big">Waiting for the files above it.</div></div>'; return; }
  if (p.state === 'preparing') { body.innerHTML = '<div class="pv-state"><div class="label" style="color:var(--ms-accent-text)">Converted</div><div class="big">Cleaning up and preparing the preview…</div><div class="progress indeterminate"><div></div></div></div>'; return; }
  if (p.state === 'error') { body.innerHTML = errorHtml(p); body.scrollTop = 0; return; }
  if (p.state !== 'done') { body.innerHTML = ''; return; }
  const note = p.truncated ? `Preview shows the first ${p.limitK}k characters — Copy and Save always use the full document.` : '';
  if (p.html !== undefined) {
    body.innerHTML = `<article class="doc">${note ? `<p class="notice">${esc(note)}</p>` : ''}${p.html}</article>`;
  } else {
    body.innerHTML = '<pre class="raw"></pre>';
    body.firstChild.textContent = (note ? note + '\n\n' : '') + p.raw;
  }
  body.scrollTop = 0;
}

function drawConverting(sel) {
  const body = $('#pvBody');
  if (!body) return;
  const known = sel.pct > 0;
  body.innerHTML = `<div class="pv-state">
    <div class="label" style="color:var(--ms-accent-text)">Converting${known ? ' · ' + sel.pct + '%' : ''}</div>
    <div class="big">${esc(sel.meta && sel.meta !== 'Converting…' ? sel.meta : 'Converting ' + sel.name + '…')}</div>
    <div class="progress ${known ? '' : 'indeterminate'}"><div style="${known ? `width:${sel.pct}%` : ''}"></div></div>
    <div class="muted" style="font-size:14px">The preview appears when this file finishes. You can keep adding files meanwhile.</div></div>`;
}

function errorHtml(p) {
  const fix = p.action && p.action !== 'retry' ? `<button class="btn btn-secondary" data-fix="${esc(p.action)}">${esc(p.actionLabel)}</button>` : '';
  return `<div class="errpage">
    <div class="head"><span class="mark">${icon('x', 20, 2.6)}</span><span class="code">${esc(p.code)}</span><span class="muted" style="font-size:13px">This file didn’t convert</span></div>
    <h2>${esc(p.title)}</h2>
    <div class="todo"><div class="label">What to do</div><p>${esc(p.next)}</p></div>
    <div class="acts">
      <button class="btn btn-primary" data-fix="retry">${icon('retry', 15, 2.4)}Retry</button>${fix}
      <button class="btn btn-secondary" data-act="copyDetails">Copy details</button>
      <button class="btn btn-ghost" data-act="remove">Remove from list</button>
    </div>
    <div class="ref">Ref ${esc(p.ref)} · the full details are in the error log.</div>
    <div><button class="tech-btn" data-act="tech">${S.showTech ? '▾  Hide technical details' : '▸  Technical details'}</button>
    ${S.showTech ? `<pre>${esc(p.detail || '(none)')}</pre>` : ''}</div></div>`;
}

async function loadPreview() {
  const id = S.selId;
  if (!id) return drawPreview(null);
  const key = `${id}|${S.view}|${S.clean}`;
  S.previewKey = key;
  const t0 = performance.now();
  const p = await api().preview(id, S.view);
  if (S.previewKey !== key) return; // a newer request superseded this one
  const t1 = performance.now();
  S.preview = { ...p, id };
  drawPreview(p);
  if (S.probe && p.state === 'done') probe(p, t1 - t0, t1);
}

function drawDialog() {
  const root = $('#dialog');
  if (S.dialog !== 'help' || !S.help) { root.innerHTML = ''; return; }
  const tab = S.help[S.helpTab] || S.help[0];
  root.innerHTML = `<div class="backdrop" data-act="closeDlg"><div class="dlg help" role="dialog" aria-modal="true" aria-label="User guide">
    <div class="dlg-head"><span class="t">User guide</span><button class="btn btn-icon" data-act="closeDlg" title="Esc">${icon('x', 16, 2.4)}</button></div>
    <div class="tabs"><nav>${S.help.map((s, i) => `<button data-tab="${i}" aria-selected="${i === S.helpTab}">${esc(s.title)}</button>`).join('')}</nav>
      <div class="pane"><h2 class="title">${esc(tab.title)}</h2><div class="doc">${tab.html}</div></div></div>
    <div class="dlg-foot"><button class="btn btn-secondary" data-act="kofi">${icon('cup', 15)}Support on Ko-fi</button><button class="btn btn-primary right" data-act="closeDlg">Close</button></div>
  </div></div>`;
}

// ---- actions -----------------------------------------------------------
function select(id) {
  if (id === S.selId) return;
  S.selId = id; S.showTech = false;
  document.querySelectorAll('.row').forEach((r) => r.setAttribute('aria-selected', String(r.dataset.id === id)));
  $('#removeBtn').disabled = !id;
  loadPreview();
}

function moveSelection(delta) {
  const i = S.files.findIndex((f) => f.id === S.selId);
  const next = S.files[Math.max(0, Math.min(S.files.length - 1, (i < 0 ? 0 : i + delta)))];
  if (next) { select(next.id); document.querySelector(`.row[data-id="${next.id}"]`)?.scrollIntoView({ block: 'nearest' }); }
}

async function act(name, el) {
  switch (name) {
    case 'add': return api().pick_files();
    case 'remove': if (S.selId) { const id = S.selId; S.selId = null; return api().remove(id); } return;
    case 'clear': return api().clear();
    case 'ocr': return api().set_option('ocr', !S.ocr); // the next snapshot redraws the switch
    case 'clean': S.clean = !S.clean; await api().set_option('clean', S.clean); return loadPreview();
    case 'copy': return S.selId && api().copy(S.selId);
    case 'copyDetails': if (S.preview?.details) { const ok = await api().copy_text(S.preview.details); el.textContent = ok ? 'Copied ✓' : 'Clipboard busy'; } return;
    case 'tech': S.showTech = !S.showTech; return drawPreview(S.preview);
    case 'hideWarn': S.hiddenWarn.add(S.selId); return drawPreview(S.preview);
    case 'save': return api().save(); // Python opens the dialog and greys the button meanwhile
    case 'settings': return api().not_yet('Settings');
    case 'help': S.help = S.help || await api().help(); S.dialog = 'help'; return drawDialog();
    case 'closeDlg': S.dialog = null; return drawDialog();
    case 'kofi': return api().kofi();
    case 'theme': {
      const dark = document.documentElement.dataset.theme === 'dark';
      S.theme = dark ? 'light' : 'dark';
      return applyTheme();
    }
  }
}

document.addEventListener('click', (e) => {
  const t = e.target;
  const link = t.closest('a[href]');
  if (link) { e.preventDefault(); return api().open_url(link.getAttribute('href')); }
  const tab = t.closest('[data-tab]');
  if (tab) { S.helpTab = Number(tab.dataset.tab); drawDialog(); return; }
  const a = t.closest('[data-act]');
  if (a) {
    // The backdrop closes only when the click lands on it, not the dialog.
    if (a.classList.contains('backdrop') && t !== a) return;
    return act(a.dataset.act, a);
  }
  const fix = t.closest('[data-fix]');
  if (fix) {
    if (fix.dataset.fix === 'raw_preview') { S.view = 'raw'; return loadPreview(); }
    return api().fix(fix.dataset.fix, S.selId || '');
  }
  const view = t.closest('[data-view]');
  if (view) { S.view = view.dataset.view; api().set_option('rendered', S.view === 'rendered'); return loadPreview(); }
  const style = t.closest('[data-style]');
  if (style) { S.exportStyle = style.dataset.style; api().set_option('exportStyle', S.exportStyle); return drawFooter(); }
  const row = t.closest('.row');
  if (row) select(row.dataset.id);
});
// A middle-click would ask WebView2 for a new window, which pywebview hands to
// Windows whatever the scheme: send it through open_url's web-only check too.
document.addEventListener('auxclick', (e) => {
  const link = e.target.closest('a');
  if (!link || e.button !== 1) return;
  e.preventDefault();
  if (link.hasAttribute('href')) api().open_url(link.getAttribute('href'));
});
document.addEventListener('change', (e) => {
  if (e.target.id === 'target') { S.aiTarget = e.target.value; api().set_option('aiTarget', S.aiTarget); }
});
document.addEventListener('keydown', (e) => {
  const k = e.key.toLowerCase();
  const inList = !!document.activeElement.closest?.('#filelist');
  if (e.key === 'Escape' && S.dialog) { S.dialog = null; drawDialog(); return; }
  if (e.ctrlKey && k === 'o') { e.preventDefault(); act('add'); }
  else if (e.ctrlKey && !e.shiftKey && k === 's') { e.preventDefault(); if (S.canSave) act('save'); }
  else if (e.ctrlKey && e.shiftKey && k === 'c') { e.preventDefault(); act('copy'); }
  // Delete acts on the list only (as in the classic window): pressed while
  // reading the preview it must not throw a converted file away.
  else if (e.key === 'Delete' && !S.dialog && inList) act('remove');
  else if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && inList) {
    e.preventDefault(); moveSelection(e.key === 'ArrowDown' ? 1 : -1);
  }
});
// Drop paths arrive in Python (pywebview adds the full path); the page only
// draws the highlight and must allow the drop.
let dragDepth = 0;
document.addEventListener('dragenter', (e) => { e.preventDefault(); dragDepth++; document.body.classList.add('drag'); });
document.addEventListener('dragover', (e) => { e.preventDefault(); });
document.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove('drag'); } });
document.addEventListener('drop', (e) => { e.preventDefault(); dragDepth = 0; document.body.classList.remove('drag'); });
sysDark.addEventListener('change', applyTheme);

window.addEventListener('pywebviewready', async () => {
  const boot = await api().ready();
  Object.assign(S, { boot, clean: boot.clean, view: boot.rendered ? 'rendered' : 'raw', exportStyle: boot.exportStyle, aiTarget: boot.aiTarget, probe: boot.probe });
  $('#helpBtn').innerHTML = `${icon('help')}Help`;
  $('#helpBtn').dataset.act = 'help';
  $('#settingsBtn').innerHTML = `${icon('sliders')}Settings`;
  $('#settingsBtn').dataset.act = 'settings';
  $('#themeBtn').dataset.act = 'theme';
  $('#kofiBtn').innerHTML = `${icon('cup', 14)}Support Markdown Sidekick`;
  $('#kofiBtn').dataset.act = 'kofi';
  applyTheme();
  window.app.receive(boot);
});

// ---- probe mode: the spike's measurements (MARKDOWN_SIDEKICK_WEBUI_PROBE) -
let probed = false;
function probe(p, bridgeMs, gotAt) {
  if (probed) return;
  probed = true;
  requestAnimationFrame(() => requestAnimationFrame(async () => {
    const paintMs = performance.now() - gotAt;
    const body = $('#pvBody');
    const frames = [];
    let last = performance.now();
    const end = last + 4000;
    const step = (now) => {
      frames.push(now - last); last = now;
      body.scrollTop = ((now - (end - 4000)) / 4000) * (body.scrollHeight - body.clientHeight);
      if (now < end) requestAnimationFrame(step); else finish();
    };
    const finish = async () => {
      frames.sort((a, b) => a - b);
      const q = (x) => Math.round(frames[Math.floor(x * (frames.length - 1))] * 10) / 10;
      await api().probe_result('preview', {
        chars: (p.html || p.raw || '').length, truncated: p.truncated, bridgeMs: Math.round(bridgeMs), paintMs: Math.round(paintMs),
        scrollHeight: body.scrollHeight, frames: frames.length, p50: q(0.5), p95: q(0.95), max: q(1),
        over33ms: frames.filter((f) => f > 33.4).length,
      });
      await api().probe_done();
    };
    requestAnimationFrame(step);
  }));
}
document.addEventListener('securitypolicyviolation', (e) => {
  if (S.probe) api().probe_result('csp_' + Date.now(), { directive: e.violatedDirective, blocked: e.blockedURI });
});

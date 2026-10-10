/* rGoVoice UI — vanilla JS, talks to /api/* */
'use strict';

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...children) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) n.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) n.append(c.nodeType ? c : document.createTextNode(c));
  return n;
};

const LANG_NAMES = {
  en: 'English', es: 'Spanish', fr: 'French', de: 'German', it: 'Italian', pt: 'Portuguese', pl: 'Polish', tr: 'Turkish',
  ru: 'Russian', nl: 'Dutch', cs: 'Czech', ar: 'Arabic', 'zh-cn': 'Chinese', zh: 'Chinese', ja: 'Japanese', hu: 'Hungarian',
  ko: 'Korean', hi: 'Hindi', sv: 'Swedish', da: 'Danish', fi: 'Finnish', no: 'Norwegian', el: 'Greek', he: 'Hebrew',
  id: 'Indonesian', ms: 'Malay', ro: 'Romanian', sk: 'Slovak', uk: 'Ukrainian', vi: 'Vietnamese', bg: 'Bulgarian',
  hr: 'Croatian', ta: 'Tamil', fil: 'Filipino',
};

const state = { engines: [], voices: [], current: null, outputs: [], pollTimer: null, optionValues: {} };

// ------------------------------------------------------------------ helpers
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (res.status === 204) return null;
  const ct = res.headers.get('content-type') || '';
  const body = ct.includes('json') ? await res.json() : await res.text();
  if (!res.ok) {
    const msg = body && body.detail ? (typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)) : res.statusText;
    throw new Error(msg);
  }
  return body;
}
const jsonReq = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

let toastTimer;
function toast(msg, isError = false) {
  const t = $('#toast');
  t.textContent = msg; t.className = 'toast' + (isError ? ' error' : '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add('hidden'), isError ? 7000 : 3500);
}
const fmtSec = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
const langName = (c) => LANG_NAMES[c] || c;
function fillLangSelect(select, codes, value) {
  select.innerHTML = '';
  for (const c of codes) select.append(el('option', { value: c }, `${langName(c)} (${c})`));
  if (value && codes.includes(value)) select.value = value;
}
function allLanguages() {
  const set = new Set();
  for (const e of state.engines) for (const l of e.languages) set.add(l === 'zh-cn' ? 'zh' : l);
  return [...set].sort((a, b) => (a === 'en' ? -1 : b === 'en' ? 1 : langName(a).localeCompare(langName(b))));
}
function engineLangs(e) { return e.languages.map((l) => (l === 'zh-cn' ? 'zh' : l)); }

// ------------------------------------------------------------------ engines / settings
async function loadEngines() {
  state.engines = await api('/api/engines');
  const badges = $('#engine-badges');
  badges.innerHTML = '';
  for (const e of state.engines) {
    badges.append(el('span', { class: `pill ${e.kind} ${e.available ? '' : 'err'}`, title: e.available ? e.description : e.reason },
      `${e.kind === 'free' ? 'FREE' : 'PAID'} · ${e.name.split(' (')[0]}${e.available ? '' : ' (off)'}`));
  }
  const x = state.engines.find((e) => e.key === 'xtts');
  $('#xtts-status').textContent = x ? (x.available ? x.description : x.reason) : 'not installed';
}

async function loadSettingsUI() {
  const s = await api('/api/settings');
  $('#el-key-status').textContent = s.elevenlabs_api_key_set
    ? `Key set (${s.elevenlabs_api_key_hint})${s.elevenlabs_from_env ? ' from ELEVENLABS_API_KEY env' : ''}`
    : 'No key saved. The ElevenLabs engine is disabled.';
}

$('#btn-settings').onclick = async () => { await loadSettingsUI(); $('#el-check').textContent = ''; $('#dlg-settings').showModal(); };
$('#btn-save-key').onclick = async () => {
  const key = $('#el-key').value.trim();
  if (!key) return toast('Paste a key first', true);
  try { await api('/api/settings', jsonReq('PUT', { elevenlabs_api_key: key })); $('#el-key').value = ''; toast('ElevenLabs key saved'); await loadSettingsUI(); await loadEngines(); if (state.current) renderVoice(); }
  catch (e) { toast(e.message, true); }
};
$('#btn-clear-key').onclick = async () => {
  try { await api('/api/settings', jsonReq('PUT', { elevenlabs_api_key: '' })); toast('Key removed'); await loadSettingsUI(); await loadEngines(); if (state.current) renderVoice(); }
  catch (e) { toast(e.message, true); }
};
$('#btn-check-el').onclick = async () => {
  $('#el-check').textContent = 'Checking…';
  try {
    const r = await api('/api/settings/elevenlabs/check');
    if (!r.ok) { $('#el-check').textContent = r.error; return; }
    const s = r.subscription;
    $('#el-check').textContent = `Plan: ${s.tier}\nCharacters used: ${s.character_count} / ${s.character_limit}\nVoice slots: ${s.voice_slots_used} / ${s.voice_limit}\nInstant cloning: ${s.can_use_instant_voice_cloning ? 'yes' : 'NO (upgrade plan)'}\nModels: ${r.models.map((m) => m.id).join(', ')}`;
    await loadEngines();
  } catch (e) { $('#el-check').textContent = e.message; }
};

// ------------------------------------------------------------------ voices list
async function loadVoices(selectId) {
  state.voices = await api('/api/voices');
  const list = $('#voice-list');
  list.innerHTML = '';
  $('#voice-empty').classList.toggle('hidden', state.voices.length > 0);
  for (const v of state.voices) {
    const ready = Object.entries(v.engines).filter(([, s]) => s.status === 'ready').map(([k]) => k);
    const li = el('li', { class: v.id === (selectId || state.current?.id) ? 'active' : '', onclick: () => selectVoice(v.id) },
      el('span', { class: 'vname' }, v.name),
      el('span', { class: 'vsub' }, `${langName(v.language)} · ${v.total_sample_seconds}s`,
        ...ready.map((k) => el('span', { class: `pill ${k === 'xtts' ? 'free' : 'paid'}` }, k === 'xtts' ? 'XTTS' : '11L'))));
    list.append(li);
  }
  if (selectId) await selectVoice(selectId);
  else if (state.current) {
    const fresh = state.voices.find((v) => v.id === state.current.id);
    if (fresh) { state.current = fresh; renderVoice(); } else { state.current = null; showWelcome(); }
  }
}

function showWelcome() { $('#voice-panel').classList.add('hidden'); $('#welcome').classList.remove('hidden'); }

async function selectVoice(id) {
  state.current = await api(`/api/voices/${id}`);
  state.outputs = await api(`/api/voices/${id}/outputs`);
  state.optionValues = {};
  history.replaceState(null, '', `#${id}`);
  await loadVoices(); // refresh badges/active state
  $('#welcome').classList.add('hidden'); $('#voice-panel').classList.remove('hidden');
  renderVoice(); renderOutputs();
  $('#speak-result').classList.add('hidden');
}

$('#btn-new-voice').onclick = () => { fillLangSelect($('#new-language'), allLanguages(), 'en'); $('#new-name').value = ''; $('#new-description').value = ''; $('#dlg-new').showModal(); $('#new-name').focus(); };
$('#form-new').onsubmit = async (ev) => {
  ev.preventDefault();
  try {
    const v = await api('/api/voices', jsonReq('POST', { name: $('#new-name').value, language: $('#new-language').value, description: $('#new-description').value }));
    $('#dlg-new').close(); await loadVoices(v.id); toast('Voice created. Now add samples.');
  } catch (e) { toast(e.message, true); }
};

// ------------------------------------------------------------------ voice panel
async function saveVoiceMeta() {
  const v = state.current; if (!v) return;
  const body = { name: $('#v-name').textContent.trim() || v.name, language: $('#v-language').value, description: $('#v-description').value };
  if (body.name === v.name && body.language === v.language && body.description === v.description) return;
  try { state.current = await api(`/api/voices/${v.id}`, jsonReq('PATCH', body)); await loadVoices(); }
  catch (e) { toast(e.message, true); }
}
$('#v-name').onblur = saveVoiceMeta; $('#v-name').onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); } };
$('#v-language').onchange = () => { saveVoiceMeta(); $('#speak-language').value = $('#v-language').value; };
$('#v-description').onchange = saveVoiceMeta;
$('#btn-delete-voice').onclick = async () => {
  const v = state.current;
  if (!confirm(`Delete voice "${v.name}", its samples, trained data and history? This also removes the ElevenLabs clone if one exists.`)) return;
  try { await api(`/api/voices/${v.id}`, { method: 'DELETE' }); state.current = null; await loadVoices(); showWelcome(); toast('Voice deleted'); }
  catch (e) { toast(e.message, true); }
};

function renderVoice() {
  const v = state.current;
  $('#v-name').textContent = v.name;
  fillLangSelect($('#v-language'), allLanguages(), v.language);
  $('#v-description').value = v.description || '';
  $('#v-created').textContent = `created ${v.created_at.replace('T', ' ')}`;
  renderSamples(); renderEngines(); renderSpeakControls();
  const training = Object.values(v.engines).some((s) => s.status === 'training' || s.training);
  clearTimeout(state.pollTimer);
  if (training) state.pollTimer = setTimeout(async () => { if (state.current?.id === v.id) { state.current = await api(`/api/voices/${v.id}`); renderVoice(); await loadVoices(); } }, 2500);
}

function renderSamples() {
  const v = state.current; const ul = $('#sample-list'); ul.innerHTML = '';
  const total = v.total_sample_seconds;
  const pill = $('#sample-total');
  pill.textContent = `${total}s total`;
  pill.className = 'pill ' + (total >= 30 ? 'ok' : total >= 6 ? 'warn' : 'err');
  v.samples.forEach((s, i) => {
    ul.append(el('li', {},
      el('span', { class: 'name' }, s.label || `sample ${i + 1}`),
      el('span', { class: 'muted' }, `${s.duration}s`),
      el('audio', { controls: '', preload: 'none', src: `/api/voices/${v.id}/samples/${i}` }),
      el('button', { class: 'btn ghost small danger', onclick: () => deleteSample(i) }, '✕')));
  });
  if (!v.samples.length) ul.append(el('li', { class: 'muted' }, 'No samples yet. Record or upload at least 6 seconds (30 s+ recommended).'));
}

async function deleteSample(i) {
  try { state.current = await api(`/api/voices/${state.current.id}/samples/${i}`, { method: 'DELETE' }); renderVoice(); await loadVoices(); }
  catch (e) { toast(e.message, true); }
}

async function uploadSample(blob, label) {
  const fd = new FormData();
  fd.append('file', blob, (label || 'sample').replace(/\.[^.]+$/, '') + '.wav');
  fd.append('label', label || 'sample');
  state.current = await api(`/api/voices/${state.current.id}/samples`, { method: 'POST', body: fd });
  renderVoice(); await loadVoices();
}

// ---- file upload: decode anything the browser can (mp3, m4a, webm, ogg, wav) and send 16-bit WAV
$('#file-input').onchange = async (ev) => {
  const files = [...ev.target.files]; ev.target.value = '';
  for (const f of files) {
    try {
      toast(`Processing ${f.name}…`);
      const wav = await toWav(await f.arrayBuffer());
      await uploadSample(wav, f.name);
      toast(`Added ${f.name}`);
    } catch (e) { toast(`${f.name}: ${e.message}`, true); }
  }
};

async function toWav(arrayBuffer) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  try {
    const buf = await ctx.decodeAudioData(arrayBuffer.slice(0));
    return encodeWav(buf);
  } finally { ctx.close(); }
}

function encodeWav(audioBuffer) {
  const n = audioBuffer.length, ch = audioBuffer.numberOfChannels, sr = audioBuffer.sampleRate;
  const mono = new Float32Array(n);
  for (let c = 0; c < ch; c++) { const d = audioBuffer.getChannelData(c); for (let i = 0; i < n; i++) mono[i] += d[i] / ch; }
  const out = new ArrayBuffer(44 + n * 2); const dv = new DataView(out);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); dv.setUint32(4, 36 + n * 2, true); str(8, 'WAVE'); str(12, 'fmt '); dv.setUint32(16, 16, true);
  dv.setUint16(20, 1, true); dv.setUint16(22, 1, true); dv.setUint32(24, sr, true); dv.setUint32(28, sr * 2, true);
  dv.setUint16(32, 2, true); dv.setUint16(34, 16, true); str(36, 'data'); dv.setUint32(40, n * 2, true);
  for (let i = 0, o = 44; i < n; i++, o += 2) { const s = Math.max(-1, Math.min(1, mono[i])); dv.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true); }
  return new Blob([out], { type: 'audio/wav' });
}

// ---- microphone recording
const rec = { mr: null, chunks: [], stream: null, start: 0, timer: null, raf: null, analyser: null };
$('#btn-record').onclick = async () => (rec.mr ? stopRecording() : startRecording());

async function startRecording() {
  try {
    rec.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
  } catch (e) { return toast('Microphone access denied: ' + e.message, true); }
  rec.chunks = [];
  const mime = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus', ''].find((m) => !m || MediaRecorder.isTypeSupported(m));
  rec.mr = new MediaRecorder(rec.stream, mime ? { mimeType: mime } : undefined);
  rec.mr.ondataavailable = (e) => e.data.size && rec.chunks.push(e.data);
  rec.mr.onstop = finishRecording;
  rec.mr.start(250);
  rec.start = Date.now();
  $('#btn-record').textContent = '■ Stop'; $('#btn-record').classList.add('on');
  rec.timer = setInterval(() => { $('#rec-timer').textContent = fmtSec((Date.now() - rec.start) / 1000); }, 200);
  // level meter
  const actx = new AudioContext(); const src = actx.createMediaStreamSource(rec.stream); rec.analyser = actx.createAnalyser(); rec.analyser.fftSize = 512; src.connect(rec.analyser);
  rec.actx = actx; const data = new Uint8Array(rec.analyser.fftSize);
  const tick = () => { rec.analyser.getByteTimeDomainData(data); let peak = 0; for (const x of data) peak = Math.max(peak, Math.abs(x - 128) / 128); $('#rec-level').style.width = `${Math.min(100, peak * 160)}%`; rec.raf = requestAnimationFrame(tick); };
  tick();
}

function stopRecording() { rec.mr.stop(); }

async function finishRecording() {
  clearInterval(rec.timer); cancelAnimationFrame(rec.raf); $('#rec-level').style.width = '0';
  rec.stream.getTracks().forEach((t) => t.stop()); rec.actx?.close();
  const secs = (Date.now() - rec.start) / 1000;
  const blob = new Blob(rec.chunks, { type: rec.mr.mimeType }); rec.mr = null;
  $('#btn-record').textContent = '● Record'; $('#btn-record').classList.remove('on'); $('#rec-timer').textContent = '0:00';
  if (secs < 1) return toast('Recording too short', true);
  try {
    const wav = await toWav(await blob.arrayBuffer());
    await uploadSample(wav, `recording ${new Date().toLocaleTimeString()}`);
    toast(`Added ${secs.toFixed(0)}s recording`);
  } catch (e) { toast('Could not process recording: ' + e.message, true); }
}

// ------------------------------------------------------------------ engines (train)
function renderEngines() {
  const v = state.current; const wrap = $('#engine-cards'); wrap.innerHTML = '';
  for (const e of state.engines) {
    const st = v.engines[e.key] || { status: 'untrained' };
    const busy = st.status === 'training' || st.training;
    const statusPill = busy ? el('span', { class: 'status' }, el('span', { class: 'spinner' }), ' training…')
      : st.status === 'ready' ? el('span', { class: `pill ${st.stale ? 'warn' : 'ok'}` }, st.stale ? 'ready (samples changed, retrain suggested)' : 'ready')
      : st.status === 'error' ? el('span', { class: 'pill err' }, 'error')
      : el('span', { class: 'pill' }, 'not trained');
    const details = [];
    if (st.trained_at) details.push(`trained ${st.trained_at.replace('T', ' ')}`);
    if (st.data?.samples_used) details.push(`${st.data.samples_used} sample(s)`);
    if (st.data?.reference_seconds) details.push(`${st.data.reference_seconds}s reference`);
    if (st.data?.voice_id) details.push(`ElevenLabs id ${st.data.voice_id}`);
    const canTrain = e.available && !busy && v.total_sample_seconds >= 6;
    const card = el('div', { class: 'engine-card' },
      el('div', { class: 'title' }, el('span', {}, e.name), el('span', { class: `pill ${e.kind}` }, e.kind === 'free' ? 'FREE · local' : 'PAID · cloud')),
      el('div', { class: 'muted' }, e.available ? e.description : e.reason),
      el('div', { class: 'status' }, statusPill, el('span', { class: 'muted' }, details.join(' · '))),
      st.error ? el('div', { class: 'err' }, st.error) : null,
      el('div', { class: 'actions' },
        el('button', { class: 'btn primary small', disabled: canTrain ? null : '', onclick: () => train(e.key),
          title: !e.available ? e.reason : v.total_sample_seconds < 6 ? 'Add at least 6 s of samples' : '' },
          st.status === 'ready' ? '↻ Retrain' : '⚡ Train'),
        st.status === 'ready' || st.status === 'error' ? el('button', { class: 'btn ghost small danger', onclick: () => untrain(e.key) }, 'Remove') : null));
    wrap.append(card);
  }
}

async function train(key) {
  try {
    state.current = await api(`/api/voices/${state.current.id}/train/${key}`, { method: 'POST' });
    renderVoice();
    toast(key === 'xtts' ? 'Training locally. First run loads the model (~1 min, downloads weights if missing).' : 'Cloning on ElevenLabs…');
  } catch (e) { toast(e.message, true); }
}
async function untrain(key) {
  if (!confirm('Remove the trained data for this engine?')) return;
  try { state.current = await api(`/api/voices/${state.current.id}/train/${key}`, { method: 'DELETE' }); renderVoice(); await loadVoices(); }
  catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ speak
function readyEngines() { return state.engines.filter((e) => state.current.engines[e.key]?.status === 'ready'); }

function renderSpeakControls() {
  const sel = $('#speak-engine'); const prev = sel.value; sel.innerHTML = '';
  const ready = readyEngines();
  for (const e of ready) sel.append(el('option', { value: e.key }, `${e.name} · ${e.kind}`));
  if (!ready.length) sel.append(el('option', { value: '' }, 'Train the voice first'));
  if (ready.some((e) => e.key === prev)) sel.value = prev;
  $('#btn-speak').disabled = !ready.length;
  renderEngineOptions();
}
$('#speak-engine').onchange = renderEngineOptions;

function renderEngineOptions() {
  const e = state.engines.find((x) => x.key === $('#speak-engine').value);
  const box = $('#speak-options'); box.innerHTML = '';
  const langSel = $('#speak-language');
  const prevLang = langSel.value || state.current.language;
  fillLangSelect(langSel, e ? engineLangs(e) : allLanguages(), prevLang);
  if (!e) return;
  const vals = (state.optionValues[e.key] ||= {});
  for (const o of e.options) {
    const cur = vals[o.key] ?? o.default; vals[o.key] = cur;
    if (o.type === 'select') {
      const s = el('select', { onchange: (ev) => { vals[o.key] = ev.target.value; } });
      for (const c of o.choices) s.append(el('option', { value: c.value }, c.label));
      s.value = cur;
      box.append(el('div', { class: 'opt' }, el('div', { class: 'lab' }, el('span', {}, o.label)), s));
    } else {
      const valSpan = el('span', {}, String(cur));
      const r = el('input', { type: 'range', min: o.min, max: o.max, step: o.step, value: cur,
        oninput: (ev) => { vals[o.key] = parseFloat(ev.target.value); valSpan.textContent = ev.target.value; } });
      box.append(el('div', { class: 'opt' }, el('div', { class: 'lab' }, el('span', {}, o.label), valSpan), r));
    }
  }
  box.append(el('button', { class: 'btn ghost small', onclick: () => { state.optionValues[e.key] = {}; renderEngineOptions(); } }, 'Reset to defaults'));
}

$('#speak-text').oninput = (ev) => { $('#speak-count').textContent = `${ev.target.value.length} / 5000`; };

$('#btn-speak').onclick = async () => {
  const text = $('#speak-text').value.trim(); const engine = $('#speak-engine').value;
  if (!text) return toast('Type some text first', true);
  const btn = $('#btn-speak'); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Generating…';
  const t0 = Date.now();
  try {
    const r = await api(`/api/voices/${state.current.id}/speak`, jsonReq('POST', { text, engine, language: $('#speak-language').value, options: state.optionValues[engine] || {} }));
    showResult(r, (Date.now() - t0) / 1000);
    state.outputs.unshift(r); renderOutputs();
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.textContent = '▶ Generate speech'; }
};

function showResult(r, secs) {
  $('#speak-result').classList.remove('hidden');
  const a = $('#speak-audio'); a.src = r.url + `?t=${Date.now()}`; a.play().catch(() => {});
  $('#speak-download').href = r.url + '?download=1';
  const parts = [r.engine, r.audio_seconds ? `${r.audio_seconds}s audio` : null, secs ? `generated in ${secs.toFixed(1)}s` : null, r.device ? `on ${r.device}` : null, r.model_id || null].filter(Boolean);
  $('#speak-info').textContent = parts.join(' · ');
}

function renderOutputs() {
  const ul = $('#output-list'); ul.innerHTML = '';
  $('#output-empty').classList.toggle('hidden', state.outputs.length > 0);
  for (const o of state.outputs) {
    ul.append(el('li', {},
      el('div', {}, el('div', { class: 'otext', title: o.text }, o.text), el('div', { class: 'ometa' }, `${o.created_at.replace('T', ' ')} · ${o.engine} · ${langName(o.language)}${o.model_id ? ' · ' + o.model_id : ''}${o.seconds_to_generate ? ' · ' + o.seconds_to_generate + 's' : ''}`)),
      el('audio', { controls: '', preload: 'none', src: o.url }),
      el('a', { class: 'btn ghost small', href: o.url + '?download=1', download: '' }, '⬇'),
      el('button', { class: 'btn ghost small danger', onclick: async () => { await api(`/api/voices/${o.voice_id}/outputs/${o.id}`, { method: 'DELETE' }); state.outputs = state.outputs.filter((x) => x.id !== o.id); renderOutputs(); } }, '✕')));
  }
}

// ------------------------------------------------------------------ boot
(async function boot() {
  try { await loadEngines(); await loadVoices(); }
  catch (e) { toast('Failed to load: ' + e.message, true); }
  const first = location.hash.slice(1);
  if (first && state.voices.some((v) => v.id === first)) selectVoice(first);
})();

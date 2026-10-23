(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = { info: null, selected: null, kind: 'video', job: null, stream: null, poll: null, seq: 0 };
  const iconDownload = '<svg class="icon icon-sm" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m-4-4 4 4 4-4M4 17v3h16v-3"/></svg>';
  const iconSearch = '<svg class="icon icon-sm" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14m-6-6 6 6-6 6"/></svg>';
  const params = job => `?token=${encodeURIComponent(job.token)}`;
  let toastTimer;

  function msg(value, target = 'error') {
    $(target === 'error' ? 'error-text' : 'prep-title').textContent = value;
    if (target === 'error') {
      $('error').classList.remove('hide');
      $('url-input').setAttribute('aria-invalid', 'true');
    }
  }
  function clearError() { $('error').classList.add('hide'); $('url-input').removeAttribute('aria-invalid'); }
  function toast(value) { $('toast').textContent = value; $('toast').classList.add('visible'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.remove('visible'), 4000); }
  async function api(path, init = {}) {
    let response;
    try { response = await fetch(path, { cache: 'no-store', ...init }); }
    catch { throw new Error('Não foi possível conectar ao servidor. Tente novamente.'); }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || 'Algo deu errado. Tente novamente.');
    return data;
  }
  function safeLink(value) {
    try {
      const u = new URL(value);
      return u.protocol === 'https:' && ['youtube.com', 'www.youtube.com', 'youtu.be', 'vimeo.com'].includes(u.hostname) ? u.href : null;
    } catch { return null; }
  }
  function safeThumb(value) {
    try {
      const u = new URL(value);
      if (u.protocol !== 'https:') return '';
      const host = u.hostname.toLowerCase();
      if (['i.ytimg.com', 'img.youtube.com', 'i.vimeocdn.com'].includes(host) || host.endsWith('.vimeocdn.com')) return u.href;
    } catch { /* fallback artwork */ }
    return '';
  }
  function formatDuration(s) {
    if (!Number.isFinite(s)) return '';
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}` : `${m}:${String(sec).padStart(2, '0')}`;
  }
  function setKind(kind) {
    if (!state.info?.profiles.some(p => p.kind === kind)) return;
    state.kind = kind;
    state.selected = state.info.profiles.find(p => p.kind === kind)?.id;
    ['video', 'audio'].forEach(k => {
      $('kind-' + k).classList.toggle('selected', k === kind);
      $('kind-' + k).setAttribute('aria-pressed', String(k === kind));
      $('kind-' + k).disabled = !state.info.profiles.some(p => p.kind === k);
    });
    renderOptions();
  }
  function renderOptions() {
    const container = $('format-list'); container.replaceChildren();
    for (const p of (state.info?.profiles || []).filter(p => p.kind === state.kind)) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'option' + (p.id === state.selected ? ' selected' : '');
      button.setAttribute('aria-pressed', String(p.id === state.selected));
      const left = document.createElement('span'); left.className = 'option-left';
      const dot = document.createElement('span'); dot.className = 'radio-dot'; dot.setAttribute('aria-hidden', 'true');
      const content = document.createElement('span'); const title = document.createElement('strong');
      title.textContent = p.label;
      const detail = document.createElement('small'); detail.textContent = p.ext;
      content.append(title, detail); left.append(dot, content); button.append(left);
      button.addEventListener('click', () => { state.selected = p.id; renderOptions(); resetProgress(); });
      container.append(button);
    }
  }
  function showResult(info) {
    state.info = info;
    state.selected = null;
    $('results').classList.remove('hide');
    $('media-platform').textContent = info.source;
    $('media-title').textContent = info.title;
    $('media-duration').textContent = info.kind === 'playlist'
      ? [info.author, `${info.count} vídeos`].filter(Boolean).join(' · ')
      : [info.author, formatDuration(info.duration)].filter(Boolean).join(' · ');
    $('playlist-preview').classList.toggle('hide', info.kind !== 'playlist');
    if (info.kind === 'playlist') {
      $('playlist-count').textContent = `${info.count} vídeos · Em sequência`;
      const host = $('playlist-entries'); host.replaceChildren();
      for (const [i, entry] of info.entries.entries()) {
        const line = document.createElement('div'); line.className = 'playlist-entry';
        const index = document.createElement('span'); index.className = 'playlist-index'; index.textContent = String(i + 1).padStart(2, '0');
        const title = document.createElement('span'); title.className = 'playlist-entry-title'; title.textContent = entry.title;
        line.append(index, title); host.append(line);
      }
    }
    try { sessionStorage.setItem('lastInspection', JSON.stringify(info)); } catch { /* storage disabled */ }
    $('public-history').disabled = info.publicEligible === false;
    if (info.publicEligible === false) $('public-history').checked = false;
    const thumb = safeThumb(info.thumbnail);
    $('poster').classList.toggle('has-thumb', Boolean(thumb));
    $('poster').style.backgroundImage = thumb ? `url(${JSON.stringify(thumb)})` : '';
    setKind(info.profiles.some(p => p.kind === 'video') ? 'video' : 'audio');
  }
  async function inspect() {
    if (state.job && !state.job.terminal) { toast('Aguarde ou cancele o processamento atual.'); return; }
    const url = $('url-input').value.trim(); const seq = ++state.seq;
    try { sessionStorage.removeItem('lastInspection'); } catch { /* disabled */ }
    clearError(); resetProgress(); state.info = null; $('results').classList.add('hide');
    if (!url) { msg('Cole um link de vídeo para continuar.'); $('url-input').focus(); return; }
    try {
      const u = new URL(url);
      if (u.protocol !== 'https:') throw new Error();
    } catch { msg('Insira um link HTTPS válido do YouTube ou Vimeo.'); return; }
    $('loading').classList.remove('hide'); $('inspect-btn').disabled = true; $('inspect-btn').textContent = 'Analisando…';
    try {
      const result = await api('/api/inspect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
      if (seq === state.seq) showResult(result);
    } catch (error) { if (seq === state.seq) msg(error.message); }
    finally {
      if (seq === state.seq) {
        $('loading').classList.add('hide'); $('inspect-btn').disabled = false;
        $('inspect-btn').innerHTML = 'Analisar ' + iconSearch;
      }
    }
  }
  function stopListening() { if (state.stream) state.stream.close(); state.stream = null; clearInterval(state.poll); state.poll = null; }
  function resetProgress() {
    if (state.job && !state.job.terminal) return;
    stopListening(); $('prep-panel').classList.add('hide'); $('prepare-btn').disabled = false;
    $('prepare-btn').innerHTML = iconDownload + 'Preparar arquivo';
    const actions = $('result-actions'); if (actions) actions.replaceChildren();
    $('prep-percent').textContent = ''; $('prep-fill').style.width = '0%';
    $('prep-fill').classList.remove('indeterminate'); $('prep-progress').removeAttribute('aria-valuenow');
    $('playlist-progress').classList.add('hide'); $('playlist-progress-items').replaceChildren();
    state.job = null; try { sessionStorage.removeItem('activeJob'); } catch { /* storage disabled */ }
  }
  function ensureActions() {
    let el = $('result-actions');
    if (!el) { el = document.createElement('div'); el.id = 'result-actions'; el.className = 'bottom-actions'; $('prep-panel').after(el); }
    return el;
  }
  function renderPlaylistStatus(playlist) {
    $('playlist-progress').classList.toggle('hide', !playlist);
    if (!playlist) return;
    const host = $('playlist-progress-items'); host.replaceChildren();
    for (const entry of playlist.items) {
      const line = document.createElement('div'); line.className = 'playlist-entry';
      const index = document.createElement('span'); index.className = 'playlist-index';
      index.textContent = String(entry.position).padStart(2, '0');
      const title = document.createElement('span'); title.className = 'playlist-entry-title'; title.textContent = entry.title;
      const status = document.createElement('span'); status.className = 'playlist-entry-state';
      status.textContent = entry.status === 'completed' ? 'Concluído' : entry.status === 'failed'
        ? 'Falhou' : entry.status === 'running'
          ? (entry.percent == null ? entry.stage : `${entry.percent}%`) : entry.status === 'canceled' ? 'Cancelado' : 'Na fila';
      if (entry.error) status.title = entry.error;
      line.append(index, title, status); host.append(line);
    }
  }
  function renderJob(data) {
    const active = !['completed','failed','canceled','expired'].includes(data.status);
    $('url-input').disabled = active;
    $('inspect-btn').disabled = active;
    $('example-btn').disabled = active;
    $('prep-panel').classList.remove('hide');
    $('prep-title').textContent = data.error?.message || data.stage || 'Preparando';
    renderPlaylistStatus(data.playlist || null);
    const percent = typeof data.percent === 'number' ? Math.max(0, Math.min(100, data.percent)) : null;
    $('prep-percent').textContent = percent !== null ? `${Number(percent.toFixed(1))}%` : '';
    $('prep-fill').classList.toggle('indeterminate', percent === null && !['completed','failed','canceled','expired'].includes(data.status));
    $('prep-fill').style.width = percent === null ? '35%' : percent + '%';
    if (percent === null) $('prep-progress').removeAttribute('aria-valuenow');
    else $('prep-progress').setAttribute('aria-valuenow', String(percent));
    const actions = ensureActions();
    actions.replaceChildren();
    if (data.status === 'completed') {
      state.job.terminal = true; stopListening();
      $('prepare-btn').disabled = false; $('prepare-btn').innerHTML = iconDownload + 'Preparar outro arquivo';
      const a = document.createElement('a'); a.className = 'primary'; a.textContent = 'Baixar arquivo';
      a.href = `/api/downloads/${encodeURIComponent(state.job.id)}/file${params(state.job)}`;
      if (data.kind === 'playlist') {
        a.textContent = 'Baixar playlist (.zip)';
        if (data.playlist?.failed) toast(`${data.playlist.failed} vídeo(s) não puderam ser preparados. Os demais estão no ZIP.`);
      }
      actions.append(a);
    } else if (['failed', 'canceled', 'expired'].includes(data.status)) {
      state.job.terminal = true; stopListening(); $('prepare-btn').disabled = false;
      $('prepare-btn').textContent = 'Tentar novamente';
    } else {
      $('prepare-btn').disabled = true; $('prepare-btn').textContent = 'Processando…';
      const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'secondary'; cancel.textContent = 'Cancelar';
      cancel.addEventListener('click', async () => {
        cancel.disabled = true;
        try { await api(`/api/downloads/${state.job.id}/cancel${params(state.job)}`, {method:'POST'}); }
        catch (error) { toast(error.message); cancel.disabled = false; }
      });
      actions.append(cancel);
    }
  }
  async function poll() {
    if (!state.job || state.job.terminal) return;
    try { renderJob(await api(`/api/downloads/${state.job.id}${params(state.job)}`)); }
    catch (error) { toast(error.message); stopListening(); }
  }
  function watch(job) {
    stopListening(); state.job = {...job, terminal:false};
    try { sessionStorage.setItem('activeJob', JSON.stringify(state.job)); } catch { /* storage disabled */ }
    renderJob({ status: 'queued', stage: 'Na fila', percent: null });
    const es = new EventSource(`/api/downloads/${job.id}/events${params(job)}`);
    state.stream = es;
    es.onmessage = event => { try { renderJob(JSON.parse(event.data)); } catch { /* ignore malformed progress */ } };
    es.onerror = () => { es.close(); if (state.stream === es) state.stream = null; if (!state.poll && !state.job.terminal) state.poll = setInterval(poll, 2500); };
  }
  async function prepare() {
    if (!state.info || !state.selected) return;
    if (state.job && !state.job.terminal) return;
    resetProgress(); $('prepare-btn').disabled = true; $('prepare-btn').textContent = 'Adicionando à fila…';
    try {
      const job = await api('/api/downloads', { method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ inspectionId: state.info.inspectionId, profileId: state.selected,
          metadata: $('metadata').checked, publish: $('public-history').checked }) });
      watch(job);
    } catch (error) { toast(error.message); $('prepare-btn').disabled = false; $('prepare-btn').innerHTML = iconDownload + 'Preparar arquivo'; }
  }
  async function renderHistory() {
    const host = $('history-list'); host.replaceChildren();
    try {
      const records = await api('/api/history');
      const q = $('history-search').value.trim().toLowerCase();
      const items = records.items.filter(item => (item.title + ' ' + item.profile.ext + ' ' + item.profile.label).toLowerCase().includes(q));
      if (!items.length) { const empty = document.createElement('div'); empty.className = 'empty-history'; empty.textContent = 'Nenhum registro encontrado.'; host.append(empty); return; }
      for (const item of items) {
        const row = document.createElement('div'); row.className = 'history-item';
        const symbol = document.createElement('span'); symbol.className = 'history-thumb'; symbol.textContent = item.profile.kind === 'video' ? '▶' : '♫';
        const info = document.createElement('div'); const title = document.createElement('div'); title.className = 'history-name';
        const link = safeLink(item.url);
        if (link) { const a = document.createElement('a'); a.href = link; a.rel = 'noopener noreferrer'; a.target = '_blank'; a.textContent = item.title; title.append(a); }
        else title.textContent = item.title;
        const sub = document.createElement('div'); sub.className = 'history-info'; sub.textContent = `${item.source} · ${item.profile.ext} · ${item.profile.label}`;
        info.append(title, sub);
        const date = document.createElement('span'); date.className = 'history-time'; date.textContent = new Date(item.createdAt * 1000).toLocaleDateString('pt-BR');
        row.append(symbol, info, date); host.append(row);
      }
    } catch (error) { const empty = document.createElement('div'); empty.className = 'empty-history'; empty.textContent = error.message; host.append(empty); }
  }
  function showPage(page) {
    $('download-page').classList.toggle('hide', page !== 'download'); $('history-page').classList.toggle('hide', page !== 'history');
    ['download','history'].forEach(p => { $('nav-' + p).classList.toggle('active', p === page); $('nav-' + p).setAttribute('aria-current', p === page ? 'page':'false'); });
    if (page === 'history') renderHistory();
    window.scrollTo({top:0,behavior:'smooth'});
  }
  $('url-form').addEventListener('submit', event => { event.preventDefault(); inspect(); });
  $('url-input').addEventListener('input', () => { if (state.job && !state.job.terminal) return; ++state.seq; clearError(); $('loading').classList.add('hide'); $('inspect-btn').disabled = false; $('inspect-btn').innerHTML = 'Analisar ' + iconSearch; $('results').classList.add('hide'); });
  $('example-btn').addEventListener('click', () => { $('url-input').value = 'https://www.youtube.com/watch?v=aqz-KE-bpKQ'; inspect(); });
  $('kind-video').addEventListener('click', () => setKind('video'));
  $('kind-audio').addEventListener('click', () => setKind('audio'));
  $('prepare-btn').addEventListener('click', prepare);
  $('nav-download').addEventListener('click', () => showPage('download'));
  $('nav-history').addEventListener('click', () => showPage('history'));
  $('history-search').addEventListener('input', renderHistory);
  $('theme-button').addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme !== 'dark'; document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    $('theme-moon').classList.toggle('hide', dark); $('theme-sun').classList.toggle('hide', !dark);
  });
  try {
    const saved = JSON.parse(sessionStorage.getItem('activeJob') || 'null');
    const inspection = JSON.parse(sessionStorage.getItem('lastInspection') || 'null');
    if (inspection?.inspectionId && inspection?.profiles) showResult(inspection);
    if (saved?.id && saved?.token) watch(saved);
  } catch { /* no resumable session */ }
})();
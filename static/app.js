const $ = id => document.getElementById(id);
let config, selectedFile, result, activeTab = 'portuguese', busy = false;
let pollGeneration = 0, toastTimer;
let activeRecord = null, creating = false, nextHistoryOffset = null;
const storage = {
  get(key) { try { return sessionStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { value === null ? sessionStorage.removeItem(key) : sessionStorage.setItem(key, value); } catch {} }
};
let theme = 'light';
try { theme = localStorage.getItem('transcribe-theme') || 'light'; } catch {}
function applyTheme() {
  document.documentElement.dataset.theme = theme;
  $('theme').setAttribute('aria-label', theme === 'light' ? 'Ativar modo escuro' : 'Ativar modo claro');
  $('theme').setAttribute('aria-pressed', String(theme === 'dark'));
}
applyTheme();
$('theme').onclick = () => { theme = theme === 'dark' ? 'light' : 'dark'; applyTheme(); try { localStorage.setItem('transcribe-theme', theme); } catch {} };
function friendlyMessage(error) { return error instanceof TypeError || error instanceof SyntaxError ? 'Não foi possível conectar ao aplicativo. Abra novamente e tente outra vez.' : error.message || 'Não foi possível concluir. Tente novamente.'; }
function showError(message) { $('error').textContent = message; $('error').hidden = false; }
function setBusy(value) {
  busy = value;
  for (const id of ['url', 'submit', 'upload', 'file-send', 'file-clear']) $(id).disabled = value;
  $('form').setAttribute('aria-busy', String(value));
  $('submit').textContent = value ? 'Processando…' : 'Transcrever vídeo ↗';
}
async function refreshConfig() {
  const response = await fetch('/api/config');
  if (!response.ok) throw new Error('Não foi possível conectar ao aplicativo. Abra novamente e tente outra vez.');
  config = await response.json();
  $('config-notice').hidden = config.ready;
  return config;
}
$('setup-open').onclick = () => $('setup').showModal();
$('setup-close').onclick = () => $('setup').close();
$('setup-check').onclick = async () => {
  try { await refreshConfig(); $('setup-status').textContent = config.ready ? 'Chave encontrada. Você já pode iniciar uma transcrição; a validade será conferida ao processar.' : 'Ainda não encontramos a chave. Salve o arquivo .env e tente novamente.'; }
  catch(e) { $('setup-status').textContent = friendlyMessage(e); }
};
$('upload').onclick = () => { $('file').click(); };
$('file').onchange = () => {
  const file = $('file').files[0];
  if (!file) return;
  if (!/\.(mp4|mov|m4v|webm|mp3|m4a|wav)$/i.test(file.name)) { showError('Envie MP4, MOV, M4V, WEBM, MP3, M4A ou WAV.'); $('file').value = ''; return; }
  if (file.size > (config?.maxMB || 300) * 1024 * 1024 || !file.size) { showError(file.size ? 'O arquivo ultrapassa 300 MB. Envie uma versão menor.' : 'O arquivo está vazio. Escolha outro arquivo.'); $('file').value = ''; return; }
  selectedFile = file;
  $('file-name').textContent = `${file.name} · ${(file.size / 1024 / 1024).toLocaleString('pt-BR', {maximumFractionDigits:1})} MB`;
  $('file-selection').hidden = false;
  $('error').hidden = true;
};
function clearFile() { selectedFile = null; $('file').value = ''; $('file-selection').hidden = true; }
$('file-clear').onclick = clearFile;
$('file-send').onclick = () => selectedFile && start({file:selectedFile});
$('form').onsubmit = e => {
  e.preventDefault();
  if (busy || creating) return;
  const raw = $('url').value.trim();
  try {
    const url = new URL(raw);
    if (url.protocol !== 'https:' || !url.hostname || url.username || url.password || /\s/.test(raw)) throw new Error();
    start({url:raw});
  } catch { showError('Não conseguimos identificar esse link. Use uma URL https válida e tente novamente.'); $('url').focus(); }
};
function updateStage(status, detail) {
  $('progress').hidden = false;
  $('stage').textContent = detail;
  $('stage-detail').textContent = status === 'receiving' ? 'Enviando o conteúdo para processamento.' : 'Cada etapa acompanha o processamento real do seu vídeo.';
  const order = ['preparing', 'extracting', 'transcribing', 'language', 'translating', 'done'];
  const index = order.indexOf(status);
  document.querySelectorAll('.steps li').forEach((item, i) => { item.classList.toggle('current', i === index); item.classList.toggle('complete', i < index); });
}
async function start(input) {
  if (busy || creating) return;
  $('error').hidden = true;
  setBusy(true);
  try {
    await refreshConfig();
    const headers = {'x-transcribe-token':config.token};
    let body;
    if (input.file) { headers['Content-Type'] = 'application/octet-stream'; headers['x-file-name'] = encodeURIComponent(input.file.name); body = input.file; }
    else { headers['Content-Type'] = 'application/json'; body = JSON.stringify({url:input.url}); }
    updateStage(input.file ? 'receiving' : 'preparing', input.file ? 'Enviando arquivo…' : 'Preparando vídeo…');
    const response = await fetch('/api/jobs', {method:'POST', headers, body});
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || 'Não foi possível iniciar a transcrição. Tente novamente.');
    storage.set('transcribe-job', data.id);
    result = null; activeRecord = null; $('creative').hidden = true;
    $('result').hidden = true;
    $('empty').hidden = true;
    clearFile();
    await poll(data.id);
  } catch(e) { showError(friendlyMessage(e)); $('progress').hidden = true; setBusy(false); }
}
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function poll(id) {
  const generation = ++pollGeneration;
  let failures = 0;
  setBusy(true);
  $('empty').hidden = true;
  while (generation === pollGeneration) {
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(id)}`);
      const data = await response.json();
      if (!response.ok) {
        storage.set('transcribe-job', null);
        showError(data.message || 'Não foi possível recuperar a transcrição.');
        break;
      }
      failures = 0;
      updateStage(data.status, data.detail);
      if (data.status === 'done' || data.status === 'error') {
        if (data.result?.original) { result = data.result; activeTab = result.translation ? 'portuguese' : 'original'; renderResult(); }
        if (data.status === 'error') {
          showError(data.message || 'Não foi possível concluir a transcrição. Tente novamente.');
          if (result) { $('result-warning').textContent = result.partial ? 'Transcrição parcial: o processamento foi interrompido. Este texto não contém necessariamente o vídeo inteiro.' : 'A fala foi transcrita, mas o processamento posterior não terminou.'; $('result-warning').hidden = false; }
        } else toast(result.warning ? 'Transcrição pronta. Confira o aviso sobre a tradução.' : 'Tudo pronto ✓');
        if (data.result?.original) { await loadRecord(id); await refreshHistory(); }
        // Keep completed ID for refresh recovery, but no transcript in browser storage.
        break;
      }
    } catch {
      failures++;
      updateStage('preparing', 'Reconectando ao processamento…');
      if (failures >= 10) { showError('A conexão foi interrompida. Atualize a página para recuperar o resultado; o processamento pode continuar no servidor.'); break; }
    }
    await pause(failures ? 3000 : 1100);
  }
  if (generation === pollGeneration) { setBusy(false); $('progress').hidden = true; $('empty').hidden = !!result; }
}
function currentText() { return activeTab === 'portuguese' && result.translation ? result.translation : result.original; }
function allText() {
  const heading = result.partial ? 'TRANSCRIÇÃO PARCIAL' : 'TRANSCRIÇÃO';
  return `Idioma detectado: ${result.language || 'Ainda não identificado'}\n\n${heading}${result.translation ? ' ORIGINAL' : ''}\n\n${result.original}${result.translation ? '\n\nTRADUÇÃO PARA PORTUGUÊS\n\n' + result.translation : ''}`;
}
function renderResult() {
  const translated = Boolean(result.translation);
  $('result').hidden = false;
  $('empty').hidden = true;
  $('tab-portuguese').hidden = !translated;
  $('tab-original').textContent = translated ? 'Original' : 'Transcrição';
  for (const name of ['portuguese', 'original']) {
    $('tab-'+name).setAttribute('aria-selected', String(activeTab === name));
    $('tab-'+name).tabIndex = activeTab === name ? 0 : -1;
  }
  const text = currentText();
  $('transcript').textContent = text;
  $('transcript').setAttribute('aria-labelledby', 'tab-'+activeTab);
  $('transcript').lang = activeTab === 'portuguese' ? 'pt-BR' : result.language_code || '';
  $('result-title').textContent = result.partial ? 'Transcrição parcial' : translated ? (activeTab === 'portuguese' ? 'Tradução para português' : 'Transcrição original') : 'Transcrição';
  $('copy').textContent = translated ? (activeTab === 'portuguese' ? 'Copiar tradução' : 'Copiar original') : 'Copiar';
  const count = text.trim().split(/\s+/u).filter(Boolean).length;
  $('word-count').textContent = `${count.toLocaleString('pt-BR')} ${count === 1 ? 'palavra' : 'palavras'}`;
  const time = result.duration ? ` · ${Math.floor(result.duration/60)}:${String(result.duration%60).padStart(2,'0')} de vídeo` : '';
  $('language').textContent = `Idioma detectado: ${result.language || 'Ainda não identificado'}${time}`;
  $('result-warning').hidden = !result.warning;
  $('result-warning').textContent = result.warning || '';
}
for (const name of ['portuguese', 'original']) {
  $('tab-'+name).onclick = () => { activeTab = name; renderResult(); };
  $('tab-'+name).onkeydown = e => {
    if (result.translation && ['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) {
      e.preventDefault(); activeTab = e.key === 'Home' ? 'portuguese' : e.key === 'End' ? 'original' : activeTab === 'original' ? 'portuguese' : 'original'; renderResult(); $('tab-'+activeTab).focus();
    }
  };
}
function toast(message) { clearTimeout(toastTimer); $('toast').textContent = message; $('toast').hidden = false; toastTimer = setTimeout(() => $('toast').hidden = true, 2600); }
async function copyText(text, button) {
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
    else {
      const field = document.createElement('textarea'); field.value = text; field.setAttribute('readonly',''); document.body.appendChild(field); field.select(); const copied = document.execCommand('copy'); field.remove(); if (!copied) throw new Error();
    }
    const before = button.textContent; button.textContent = 'Copiado ✓'; toast('Copiado ✓'); setTimeout(() => { if (button.textContent === 'Copiado ✓') button.textContent = before; }, 2000);
  } catch { toast('Não foi possível copiar automaticamente. Selecione o texto e use Copiar.'); }
}
$('copy').onclick = () => result && copyText(currentText(), $('copy'));
$('copy-all').onclick = () => result && copyText(allText(), $('copy-all'));
$('download').onclick = () => {
  if (!result) return;
  const url = URL.createObjectURL(new Blob(['\uFEFF'+allText()], {type:'text/plain;charset=utf-8'}));
  const a = document.createElement('a'); a.href = url; a.download = `transcricao-${new Date().toISOString().slice(0,10)}.txt`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$('new').onclick = () => { if (creating) return; activeRecord = null; $('creative').hidden = true; pollGeneration++; storage.set('transcribe-job', null); result = null; $('url').value = ''; clearFile(); $('result').hidden = true; $('empty').hidden = false; $('error').hidden = true; $('progress').hidden = true; setBusy(false); $('url').focus(); };
refreshConfig().then(() => { const id = storage.get('transcribe-job'); if (id) poll(id); }).catch(e => showError(friendlyMessage(e)));

const kindNames = {copy:'Copy em português brasileiro', script:'Roteiro reescrito', hooks:'10 ganchos'};
function dateLabel(value) { return new Date(value).toLocaleString('pt-BR', {dateStyle:'short',timeStyle:'short'}); }
async function getJSON(url, options) {
  const response=await fetch(url,options); const data=await response.json();
  if (!response.ok) throw new Error(data.message || 'Não foi possível concluir. Tente novamente.');
  return data;
}
async function refreshHistory(more=false) {
  try {
    const data=await getJSON('/api/history?offset='+(more ? nextHistoryOffset || 0 : 0));
    if (!Array.isArray(data.items)) throw new Error('Não foi possível carregar o histórico.');
    if (!more) $('history-list').replaceChildren();
    for (const item of data.items) {
      const button=document.createElement('button'); button.type='button'; button.className='history-item';
      const title=document.createElement('strong'); title.textContent=item.title;
      const meta=document.createElement('span'); meta.textContent=dateLabel(item.created_at)+' · '+item.language+(item.partial ? ' · Parcial' : '');
      const preview=document.createElement('p'); preview.textContent=item.preview;
      button.append(title,meta,preview); button.onclick=()=>openRecord(item.id); $('history-list').append(button);
    }
    nextHistoryOffset=data.next_offset; $('history-more').hidden=nextHistoryOffset===null;
    $('history-status').textContent=data.total ? data.total+' transcrição'+(data.total===1 ? ' salva' : 's salvas') : 'As próximas transcrições aparecerão aqui automaticamente.';
  } catch(e) { $('history-status').textContent=friendlyMessage(e); }
}
async function loadRecord(id) {
  try { activeRecord=await getJSON('/api/history/'+encodeURIComponent(id)); $('creative-status').textContent=''; $('creative-error').hidden=true; renderCreative(); }
  catch { activeRecord=null; $('creative').hidden=true; }
}
async function openRecord(id) {
  if (busy || creating) { toast('Aguarde o processamento atual para abrir outra transcrição.'); return; }
  try {
    const record=await getJSON('/api/history/'+encodeURIComponent(id));
    $('creative-status').textContent=''; $('creative-error').hidden=true;
    activeRecord=record; result=record.result; activeTab=result.translation ? 'portuguese' : 'original';
    storage.set('transcribe-job',id); $('error').hidden=true; renderResult(); renderCreative();
    $('result').scrollIntoView?.({behavior:'smooth',block:'start'});
  } catch(e) { showError(friendlyMessage(e)); }
}
function renderCreative() {
  $('creative').hidden=!activeRecord || !!result?.partial;
  if (!activeRecord) return;
  const previous=$('creative-source').value;
  $('creative-source').replaceChildren(new Option('Transcrição original',''));
  $('versions').replaceChildren();
  for (const [i,version] of activeRecord.versions.entries()) {
    if (version.kind!=='hooks') $('creative-source').append(new Option(kindNames[version.kind]+' · versão '+(i+1), version.id));
    const details=document.createElement('details'); details.className='version';
    const summary=document.createElement('summary'); summary.textContent=kindNames[version.kind]+' · '+dateLabel(version.created_at);
    const meta=document.createElement('p'); meta.className='muted';
    const base=activeRecord.versions.find(v=>v.id===version.base_id);
    meta.textContent='Base: '+(base ? kindNames[base.kind]+' · '+dateLabel(base.created_at) : 'Transcrição original');
    const text=document.createElement('div'); text.className='version-text'; text.textContent=version.content;
    const copy=document.createElement('button'); copy.className='secondary'; copy.textContent='Copiar';copy.onclick=()=>copyText(version.content,copy);
    details.append(summary,meta,text,copy);$('versions').append(details);
  }
  if (Array.from($('creative-source').options).some(o=>o.value===previous)) $('creative-source').value=previous;
}
async function createText(kind) {
  if (!activeRecord || creating || busy) return;
  creating=true; const id=activeRecord.id;
  $('creative-error').hidden=true; $('creative-status').textContent='Criando '+kindNames[kind].toLowerCase()+'…';
  document.querySelectorAll('[data-create]').forEach(b=>b.disabled=true);
  $('creative-source').disabled=true; $('new').disabled=true;
  try {
    await refreshConfig();
    const version=await getJSON('/api/history/'+encodeURIComponent(id)+'/generate', {method:'POST',headers:{'Content-Type':'application/json','x-transcribe-token':config.token},body:JSON.stringify({kind,base_id:kind==='hooks' ? null : $('creative-source').value || null})});
    activeRecord.versions.push(version); renderCreative();
    
    $('versions').lastElementChild.open=true;
    $('creative-status').textContent='Pronto. Esta versão foi salva no histórico.';
  } catch(e) { $('creative-error').textContent=friendlyMessage(e);$('creative-error').hidden=false;$('creative-status').textContent=''; }
  finally { creating=false;document.querySelectorAll('[data-create]').forEach(b=>b.disabled=false);$('creative-source').disabled=false;$('new').disabled=false; }
}
document.querySelectorAll('[data-create]').forEach(b=>b.onclick=()=>createText(b.dataset.create));
$('history-refresh').onclick=()=>refreshHistory();
$('history-more').onclick=()=>refreshHistory(true);
refreshHistory();

let downloadPolling=false;
$('video-download-open').onclick=()=>$('video-download').showModal();
$('video-download-close').onclick=()=>$('video-download').close();
async function followDownload(id) {
  if(downloadPolling)return;
  downloadPolling=true;$('video-download-start').disabled=true;$('video-download-url').disabled=true;
  try {
    let failures=0;
    while(true){
      let data;
      try { data=await getJSON('/api/downloads/'+encodeURIComponent(id));failures=0; }
      catch(e){if(++failures>=5)throw e;await pause(2000);continue;}
      $('video-download-status').textContent=data.detail;
      if(data.status==='done'){
        $('video-download-save').href='/api/downloads/'+encodeURIComponent(id)+'/file';
        $('video-download-save').hidden=false;
        break;
      }
      if(data.status==='error'){storage.set('transcribe-download',null);break;}
      await pause(1100);
    }
  }catch(e){$('video-download-status').textContent=friendlyMessage(e);}
  finally{downloadPolling=false;$('video-download-start').disabled=false;$('video-download-url').disabled=false;}
}
$('video-download-form').onsubmit=async e=>{
  e.preventDefault();if(downloadPolling)return;
  $('video-download-start').disabled=true;$('video-download-save').hidden=true;
  $('video-download-status').textContent='Acessando o vídeo…';
  try{
    await refreshConfig();
    const data=await getJSON('/api/downloads',{method:'POST',headers:{'Content-Type':'application/json','x-transcribe-token':config.token},body:JSON.stringify({url:$('video-download-url').value.trim()})});
    storage.set('transcribe-download',data.id);await followDownload(data.id);
  }catch(e){$('video-download-status').textContent=friendlyMessage(e);}
  finally{$('video-download-start').disabled=false;}
};
if(storage.get('transcribe-download'))followDownload(storage.get('transcribe-download'));

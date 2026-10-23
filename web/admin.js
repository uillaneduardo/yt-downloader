(() => {
  'use strict';
  const $ = x => document.getElementById(x);
  const fmt = new Intl.NumberFormat('pt-BR');
  const formatBytes = v => v == null ? '—' : (v >= 1073741824 ? (v / 1073741824).toFixed(2) + ' GB' : v >= 1048576 ? (v / 1048576).toFixed(1) + ' MB' : (v / 1024).toFixed(1) + ' KB');
  const when = value => value ? new Date(value * 1000).toLocaleString('pt-BR') : '—';
  const titles = {
    MAX_FILE_MB:['Arquivo final (MB)','Limite máximo de cada vídeo ou áudio'],
    MAX_TEMP_MB:['Temporários por arquivo (MB)','Espaço durante conversão'],
    MAX_PLAYLIST_TOTAL_MB:['ZIP da playlist (MB)','Tamanho máximo do pacote final'],
    MAX_PLAYLIST_TEMP_MB:['Temporários de playlist (MB)','Inclui vídeo, áudio e ZIP'],
    MAX_PLAYLIST_ITEMS:['Vídeos por playlist','Número máximo de itens'],
    MAX_DURATION_SECONDS:['Duração máxima (s)','Por vídeo'],
    JOB_TIMEOUT_SECONDS:['Tempo de processamento (s)','Por vídeo'],
    FILE_TTL_SECONDS:['Expiração de arquivo (s)','Após conclusão'],
    IP_RETENTION_DAYS:['Retenção de IP (dias)','Limpeza automática'],
    JOB_RETENTION_DAYS:['Retenção de histórico (dias)','Registros administrativos']
  };
  let csrf = '', current = 'overview', offset = 0, total = 0;
  let toastTimer;
  const el = (tag, text, cls='') => { const e=document.createElement(tag); if (text !== undefined) e.textContent=String(text); if(cls)e.className=cls; return e; };
  function notice(s) { $('toast').textContent=s; $('toast').classList.remove('hide'); clearTimeout(toastTimer); toastTimer=setTimeout(()=> $('toast').classList.add('hide'), 4000); }
  async function api(path, init={}) {
    let response;
    try { response = await fetch(path, {credentials:'same-origin', cache:'no-store', ...init, headers:{...(init.method && init.method!=='GET'? {'Content-Type':'application/json','X-Admin-CSRF':csrf}:{}),...init.headers}}); }
    catch {throw new Error('Não foi possível conectar ao servidor.');}
    const data=await response.json().catch(()=>({}));
    if(!response.ok) { if(response.status===401 && !path.endsWith('/login')) showLogin(); throw new Error(data.message || `Erro ${response.status}`); }
    return data;
  }
  function showLogin(){ $('login').classList.remove('hide');$('dashboard').classList.add('hide');$('logout').classList.add('hide'); csrf=''; }
  function showDashboard(){ $('login').classList.add('hide');$('dashboard').classList.remove('hide');$('logout').classList.remove('hide'); load().catch(err=>notice(err.message)); }
  function tab(name){current=name; document.querySelectorAll('[data-tab]').forEach(n=>n.classList.toggle('active',n.dataset.tab===name));for(const t of ['overview','jobs','storage','limits'])$('tab-'+t).classList.toggle('hide', t!==name);load().catch(err=>notice(err.message));}
  async function load(){if(current==='overview')return overview();if(current==='jobs')return jobs();if(current==='storage')return storage();return limits();}
  async function overview(){const d=await api('/api/admin/overview');$('metric-recent').textContent=fmt.format(d.last24h);$('metric-failed').textContent=fmt.format(d.failed24h);$('metric-used').textContent=formatBytes(d.tempBytes);$('metric-free').textContent=formatBytes(d.freeBytes);$('storage-summary').textContent=`${d.folderCount} pastas · ${formatBytes(d.diskTotalBytes-d.freeBytes)} utilizados no volume`; $('storage-fill').style.width=Math.min(100, Math.round((d.diskTotalBytes-d.freeBytes)/d.diskTotalBytes*100))+'%';}
  function td(row, node){const cell=el('td'); cell.append(node instanceof Node?node:document.createTextNode(String(node??'—')));row.append(cell);return cell;}
  async function jobs(){const p=new URLSearchParams({limit:40,offset,status:$('status').value,q:$('search').value.trim()}); const d=await api('/api/admin/jobs?'+p);total=d.total;const body=$('jobs-rows');body.replaceChildren();for(const j of d.items){const tr=el('tr');const main=el('div');main.append(el('strong',j.title));const link=el('a',j.url,'small muted');link.href=j.url;link.target='_blank';link.rel='noopener noreferrer';main.append(link);td(tr,main);td(tr,when(j.created));td(tr,j.client_ip||'—');td(tr,el('span',j.status,'pill'));td(tr,`${j.profile?.ext||''} ${j.profile?.label||''}`);td(tr,formatBytes(j.filesize));td(tr,j.errorMessage||'—');body.append(tr);}if(!d.items.length){const tr=el('tr');const cell=td(tr,'Nenhum download encontrado.');cell.colSpan=7;cell.className='empty';body.append(tr);} $('count').textContent=`${total?offset+1:0}–${Math.min(total,offset+40)} de ${total}`;$('prev').disabled=offset===0;$('next').disabled=offset+40>=total;}
  async function storage(){const d=await api('/api/admin/storage');const body=$('files-rows');body.replaceChildren();for(const f of d.items){const tr=el('tr');td(tr,el('strong',f.title));td(tr,f.status);td(tr,f.files);td(tr,formatBytes(f.sizeBytes));td(tr,when(f.expires));const b=el('button','Excluir','button secondary');b.disabled=['running','queued','orphan'].includes(f.status);b.addEventListener('click',async()=>{if(!confirm(`Remover os arquivos de "${f.title}"?`))return;try{await api(`/api/admin/jobs/${encodeURIComponent(f.id)}/files`,{method:'DELETE'});notice('Arquivos removidos.');await storage();}catch(e){notice(e.message);}});td(tr,b);body.append(tr);}if(!d.items.length){const tr=el('tr');const cell=td(tr,'Sem arquivos armazenados.');cell.colSpan=6;cell.className='empty';body.append(tr);}}
  async function limits(){const d=await api('/api/admin/limits');const area=$('limit-fields');area.replaceChildren();for(const [key,value] of Object.entries(d.limits)){const bound=d.bounds[key]||[1,999999];const box=el('div',undefined,'field');const label=el('label',titles[key]?.[0]||key);label.htmlFor='limit-'+key;const input=el('input');input.type='number';input.id=label.htmlFor;input.name=key;input.required=true;input.min=bound[0];input.max=bound[1];input.step=1;input.value=value;const small=el('small',titles[key]?.[1]||'');box.append(label,input,small);area.append(box);}}
  $('login-form').addEventListener('submit',async e=>{e.preventDefault();const btn=$('login-btn');btn.disabled=true;try{const d=await api('/api/admin/login',{method:'POST',body:JSON.stringify({password:$('password').value})});csrf=d.csrf;$('password').value='';showDashboard();}catch(error){$('login-message').textContent=error.message;}finally{btn.disabled=false;}});
  $('logout').addEventListener('click',async()=>{try{await api('/api/admin/logout',{method:'POST',body:'{}'});}catch(e){notice(e.message);}showLogin();});
  document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>tab(b.dataset.tab)));
  $('refresh').addEventListener('click',()=>load().catch(e=>notice(e.message)));
  $('find').addEventListener('click',()=>{offset=0;jobs().catch(e=>notice(e.message));});
  $('search').addEventListener('keydown',e=>{if(e.key==='Enter'){$('find').click();}});
  $('status').addEventListener('change',()=>{$('find').click();});
  $('prev').addEventListener('click',()=>{offset=Math.max(0,offset-40);jobs().catch(e=>notice(e.message));});
  $('next').addEventListener('click',()=>{offset+=40;jobs().catch(e=>notice(e.message));});
  $('clean').addEventListener('click',async()=>{if(!confirm('Remover arquivos expirados e temporários de trabalhos encerrados?'))return;try{const d=await api('/api/admin/cleanup',{method:'POST',body:'{}'});notice(`${d.removed} pastas removidas.`);await storage();}catch(e){notice(e.message);}});
  $('limits-form').addEventListener('submit',async e=>{e.preventDefault();const values={};for(const input of $('limit-fields').querySelectorAll('input')){values[input.name]=Number(input.value);}const btn=$('save');btn.disabled=true;try{await api('/api/admin/limits',{method:'PUT',body:JSON.stringify({limits:values})});notice('Limites atualizados.');await limits();}catch(e){notice(e.message);}finally{btn.disabled=false;}});
  api('/api/admin/session').then(d=>{if(!d.enabled){showLogin();$('login-message').textContent='Defina ADMIN_PASSWORD (16 caracteres ou mais) no .env e reinicie o serviço.';}else if(d.authenticated){csrf=d.csrf;showDashboard();}else showLogin();}).catch(e=>{showLogin();$('login-message').textContent=e.message;});
})();

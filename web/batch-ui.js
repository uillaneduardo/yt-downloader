(()=>{
'use strict';
const $=id=>document.getElementById(id);
const groups=new Map(), rows=new Map(), selections=new Map();
let info=null,helpers=null,timer=null;
const done=s=>['completed','failed','canceled','expired'].includes(s);
const busy=()=>[...groups.values()].some(g=>!g.finished);
function el(tag,cls,txt){const n=document.createElement(tag);if(cls)n.className=cls;if(txt!==undefined)n.textContent=txt;return n;}
function save(){try{sessionStorage.setItem('batchCardJobs',JSON.stringify({inspectionId:info?.inspectionId,groups:[...groups.values()].map(g=>({id:g.id,token:g.token}))}));}catch{}}
function actions(){
 const checks=[...document.querySelectorAll('#batch-card-list .batch-check')];
 const selected=checks.filter(x=>x.checked&&!rows.get(Number(x.value))?.active);
 $('batch-prepare').disabled=!selected.length;
 $('batch-prepare').textContent=selected.length?'Preparar selecionados ('+selected.length+')':'Nenhum vídeo selecionado';
 $('batch-toggle').textContent=checks.length&&checks.every(x=>x.checked)?'Desmarcar todos':'Selecionar todos';
}
function card(entry,index){
 const position=index+1, article=el('article','batch-card'),top=el('div','batch-card-main');
 const check=el('input','batch-check');check.type='checkbox';check.checked=true;check.value=String(position);
 check.setAttribute('aria-label','Selecionar '+entry.title);check.onchange=actions;
 const image=el('img','batch-card-thumb');image.alt='';image.loading='lazy';
 const thumb=helpers.safeThumb(entry.thumbnail);if(thumb)image.src=thumb;
 const details=el('div','batch-card-details');
 details.append(el('div','batch-card-title',entry.title),el('div','batch-card-source',entry.source));
 top.append(check,image,details);
 const controls=el('div','batch-card-controls');
 const kind=el('select','batch-card-select'),quality=el('select','batch-card-select batch-quality');
 kind.setAttribute('aria-label','Tipo de arquivo: '+entry.title);
 quality.setAttribute('aria-label','Qualidade: '+entry.title);
 [['video','Vídeo'],['audio','Áudio']].forEach(pair=>{const option=el('option','',pair[1]);option.value=pair[0];kind.append(option);});
 const profiles=entry.profiles||info.profiles;
 const chosen={type:profiles.some(p=>p.kind==='video')?'video':'audio',id:''};
 kind.value=chosen.type;selections.set(position,chosen);
 function format(){
   quality.replaceChildren();
   profiles.filter(p=>p.kind===kind.value).forEach(p=>{const option=el('option','',p.ext+' · '+p.label);option.value=p.id;quality.append(option);});
   chosen.type=kind.value;chosen.id=quality.value;
 }
 kind.onchange=format;quality.onchange=()=>{chosen.id=quality.value;};format();
 const prepare=el('button','primary batch-card-prepare','Preparar arquivo');
 prepare.type='button';prepare.onclick=()=>submit([position],1);
 controls.append(kind,quality,prepare);
 const feedback=el('div','batch-card-feedback');feedback.setAttribute('aria-live','polite');
 const status=el('span','batch-card-state'),progress=el('progress','batch-card-progress hide');
 progress.max=100;progress.value=0;
 const cancel=el('button','secondary batch-card-cancel hide','Cancelar');
 cancel.type='button';
 cancel.onclick=async()=>{
  const row=rows.get(position);
  if(!row?.childId||!row?.group)return;
  cancel.disabled=true;
  try{await helpers.api('/api/downloads/'+encodeURIComponent(row.childId)+'/cancel'+helpers.params(row.group),{method:'POST'});}
  catch(e){cancel.disabled=false;helpers.toast(e.message);}
 };
 const download=el('a','batch-card-download hide','Baixar arquivo');
 feedback.append(status,progress,cancel,download);
 article.append(top,controls,feedback);
 rows.set(position,{check,kind,quality,prepare,status,progress,cancel,download,active:false});
 return article;
}
function update(data,group){
 if(!data.batch?.items)return;
 for(const item of data.batch.items){
   const c=rows.get(item.position);if(!c)continue;
   c.active=!done(item.status);c.kind.disabled=c.active;c.quality.disabled=c.active;c.prepare.disabled=c.active;
   c.childId=item.id;c.group=group;c.cancel.classList.toggle('hide',!c.active);c.cancel.disabled=false;
   c.prepare.textContent=c.active?'Preparando…':item.status==='completed'?'Preparar novamente':'Preparar arquivo';
   const pct=typeof item.percent==='number'?Math.max(0,Math.min(100,item.percent)):null;
   c.progress.classList.toggle('hide',!c.active||pct===null);if(pct!==null)c.progress.value=pct;
   c.status.textContent=item.status==='completed'?'Concluído':item.status==='failed'?item.error?.message||'Falha ao preparar'
     :item.status==='expired'?'Expirado':item.status==='canceled'?'Cancelado':pct===null?item.stage||'Na fila':pct+'%';
   c.status.classList.toggle('error-state',item.status==='failed');
   c.download.classList.toggle('hide',item.status!=='completed');
   if(item.status==='completed'){
     c.download.href='/api/downloads/'+encodeURIComponent(item.id)+'/file'+helpers.params(group);
     c.download.setAttribute('download',item.fileName||'arquivo');
   }else c.download.removeAttribute('href');
 }
 if(done(data.status)){group.finished=true;save();}
 actions();
 const count=[...rows.values()].filter(c=>!c.download.classList.contains('hide')).length;
 if(count)$('batch-summary').textContent=count+' arquivo(s) pronto(s) para baixar.';
}
async function poll(){
 await Promise.all([...groups.values()].filter(g=>!g.finished&&!g.polling).map(async g=>{
  g.polling=true;
  try{
   const data=await helpers.api('/api/downloads/'+encodeURIComponent(g.id)+helpers.params(g));
   update(data,g);g.errors=0;
  }catch(e){g.errors=(g.errors||0)+1;if(g.errors>=3){g.finished=true;helpers.toast(e.message);save();}}
  finally{g.polling=false;}
 }));
 if(!busy()){clearInterval(timer);timer=null;$('url-input').disabled=false;$('inspect-btn').disabled=false;}
}
function watch(){if(!timer)timer=setInterval(poll,1800);poll();}
async function submit(selected,concurrency){
 const positions=selected.filter(pos=>!rows.get(pos)?.active);
 if(!positions.length){helpers.toast('Os vídeos selecionados já estão sendo preparados.');return;}
 const itemProfiles=Object.fromEntries(positions.map(pos=>[String(pos),selections.get(pos)?.id]));
 if(Object.values(itemProfiles).some(x=>!x)){helpers.toast('Selecione um formato para cada vídeo.');return;}
 positions.forEach(pos=>{const c=rows.get(pos);c.active=true;c.kind.disabled=true;c.quality.disabled=true;c.prepare.disabled=true;c.prepare.textContent='Adicionando…';c.status.textContent='Na fila';});
 actions();$('url-input').disabled=true;$('inspect-btn').disabled=true;
 try{
  const job=await helpers.api('/api/downloads',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({inspectionId:info.inspectionId,selectedPositions:positions,itemProfiles,
     profileId:itemProfiles[String(positions[0])],concurrency,
     metadata:$('batch-metadata').checked,publish:$('batch-publish').checked})});
  groups.set(job.id,{...job,finished:false});save();watch();
 }catch(e){
  positions.forEach(pos=>{const c=rows.get(pos);c.active=false;c.kind.disabled=false;c.quality.disabled=false;c.prepare.disabled=false;c.prepare.textContent='Preparar arquivo';c.status.textContent=e.message;c.status.classList.add('error-state');});
  if(!busy()){$('url-input').disabled=false;$('inspect-btn').disabled=false;}
  actions();helpers.toast(e.message);
 }
}
function show(data,api){
 info=data;helpers=api;clearInterval(timer);timer=null;groups.clear();rows.clear();selections.clear();
 let section=$('batch-workspace');
 if(!section){section=el('section','batch-workspace');section.id='batch-workspace';$('results').after(section);}
 section.replaceChildren();section.classList.remove('hide');
 const bar=el('div','batch-toolbar'),heading=el('div','batch-toolbar-head');
 const h=el('h2','','Vídeos encontrados ('+info.entries.length+')');
 const toggle=el('button','secondary','Desmarcar todos');toggle.id='batch-toggle';toggle.type='button';
 heading.append(h,toggle);
 const options=el('div','batch-toolbar-actions'),conLabel=el('label','batch-concurrency-label','Simultâneos ');
 const concurrency=el('select','batch-concurrency');concurrency.id='batch-concurrency';
 concurrency.setAttribute('aria-label','Downloads simultâneos');
 for(let n=1;n<=Math.min(8,Math.max(1,Number(info.maxConcurrency)||2));n++){
  const o=el('option','',String(n));o.value=String(n);concurrency.append(o);
 }
 concurrency.value=String(Math.min(2,concurrency.options.length));
 conLabel.append(concurrency);
 const prepare=el('button','primary','Preparar selecionados');prepare.id='batch-prepare';prepare.type='button';
 options.append(conLabel,prepare);bar.append(heading,options);
 const list=el('div','batch-card-list');list.id='batch-card-list';
 info.entries.forEach((entry,i)=>list.append(card(entry,i)));
 const foot=el('div','batch-footer');
 for(const field of [['batch-metadata','Capa e metadados',true],['batch-publish','Publicar no histórico',false]]){
  const label=el('label','batch-checkbox',' '+field[1]),check=el('input');check.type='checkbox';check.id=field[0];check.checked=field[2];
  if(field[0]==='batch-publish')check.disabled=info.publicEligible===false;
  label.prepend(check);foot.append(label);
 }
 const summary=el('div','batch-summary',info.warnings?.length?String(info.warnings.length)+' link(s) indisponível(is).':'');
 summary.id='batch-summary';summary.setAttribute('aria-live','polite');
 section.append(bar,list,foot,summary);
 toggle.onclick=()=>{const checks=[...list.querySelectorAll('.batch-check')],all=checks.every(x=>x.checked);checks.forEach(x=>{x.checked=!all;});actions();};
 prepare.onclick=()=>submit([...list.querySelectorAll('.batch-check:checked')].map(x=>Number(x.value)),Number(concurrency.value));
 actions();
 try{
  const saved=JSON.parse(sessionStorage.getItem('batchCardJobs')||'null');
  if(saved?.inspectionId===info.inspectionId){for(const job of saved.groups||[])groups.set(job.id,{...job,finished:false});if(groups.size)watch();}
 }catch{}
}
function hide(){
 $('batch-workspace')?.classList.add('hide');clearInterval(timer);timer=null;info=null;groups.clear();
}
window.BatchUI={show,hide,busy};
})();
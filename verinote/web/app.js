'use strict';
const $ = (s, root = document) => root.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {pending:'검토 대기', approved:'승인', rejected:'반려', held:'보류', queued:'대기 중', running:'추출 중', success:'완료', error:'실패', interrupted:'중단'};
const badge = status => `<span class="badge ${esc(status)}">${esc(labels[status] || status)}</span>`;
let docs = [], config, selectedDoc, uploadTarget, reviewTarget;
async function api(path, method = 'GET', data) {
  const response = await fetch(path, {method, headers: data ? {'Content-Type':'application/json'} : {}, body: data ? JSON.stringify(data) : undefined});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
function notice(message, error = false) { $('#notice').textContent = message; $('#notice').className = error ? 'error' : ''; }
async function attempt(fn) { try { await fn(); } catch (error) { notice(error.message, true); } }
function date(value) { return new Date(value).toLocaleString('ko-KR'); }
function sourceLink(id, version, label='원문 보기') { return `<button data-source="${esc(id)}" data-version="${version}">${esc(label)} · v${version}</button>`; }
function showView(name) {
  for (const el of document.querySelectorAll('.view')) el.hidden = el.id !== name;
  for (const el of document.querySelectorAll('nav button')) el.classList.toggle('active', el.dataset.view === name);
  $('#breadcrumb').textContent = {library:'문서와 위키',query:'관계 탐색',activity:'실행 기록',settings:'에이전트 설정'}[name];
  if (name === 'activity') attempt(loadRuns);
  if (name === 'query') attempt(loadQueries);
}
async function loadLibrary() {
  docs = await api('/api/documents');
  $('#stats').innerHTML = `<div class="stat">등록 문서<strong>${docs.length}</strong></div><div class="stat">보존된 원문 버전<strong>${docs.reduce((n,d)=>n+d.current_version,0)}</strong></div><div class="stat">지식의 기준<strong>사람의 승인</strong></div>`;
  await renderDocs();
}
async function renderDocs() {
  const search = $('#search').value.trim().toLowerCase();
  let matches = docs;
  if (search) {
    const full = await Promise.all(docs.map(d => api(`/api/documents/${d.id}`)));
    matches = full.filter(d => (d.title+' '+d.text).toLowerCase().includes(search));
  }
  $('#documents').innerHTML = matches.length ? matches.map(d => `<button class="doc-card" data-doc="${d.id}"><span class="badge">DOCUMENT · v${d.current_version}</span><h3>${esc(d.title)}</h3><p>${esc(d.filename)}</p><div class="meta">${esc(date(d.created_at))} &nbsp; ↗ 열기</div></button>`).join('') : `<div class="empty"><h3>${search?'일치하는 문서가 없습니다.':'첫 문서에서 시작하세요.'}</h3><p>원문을 등록하면, 근거가 연결된 나만의 위키를 만들 수 있습니다.</p></div>`;
}
function providerSelect() {
  return `<label>추출 에이전트<select id="extract-agent">${config.providers.map(p=>`<option value="${p.id}" ${config.selected===p.id?'selected':''}>${esc(p.name)}${p.installed?'':' · 미설치'}</option>`).join('')}</select></label><label>이 작업의 모델 ID<input id="extract-model" value="${esc(config.models[config.selected]||'')}" placeholder="CLI 기본 모델"></label>`;
}
async function openDoc(id, version) {
  selectedDoc = await api(`/api/documents/${id}${version?'?version='+version:''}`);
  const d = selectedDoc, latest = d.version === d.current_version;
  showView('library');
  $('#document-detail').innerHTML = `<div class="panel"><div class="detail-title"><h2>${esc(d.title)}</h2><select id="version-select" aria-label="문서 버전">${d.versions.map(v=>`<option value="${v.version}" ${v.version===d.version?'selected':''}>v${v.version}${v.version===d.current_version?' · 현재':''}</option>`).join('')}</select><button id="add-version">＋ 새 버전 등록</button></div><p class="muted">${latest?'현재 문서 버전':'과거 문서 버전 · 현재 그래프에 포함되지 않습니다'} · ${esc(d.filename)}</p><p class="mono">SHA-256 ${esc(d.sha256)}</p><div class="actions"><a href="/api/documents/${d.id}/raw?version=${d.version}" download="${esc(d.filename)}">원본 다운로드</a><button id="show-diff">이전 버전과 비교</button></div><div id="version-diff"></div><details><summary>원문 전체 · v${d.version}</summary><pre class="source">${esc(d.text)}</pre></details>${latest&&!d.items.length?`<div class="form-grid">${providerSelect()}</div><button class="primary" id="extract">위키 초안 추출</button><p class="muted">원문이 선택한 CLI 모델 제공자에게 전달됩니다. 추출 결과는 검토 대기로 저장됩니다.</p>`:''}</div><h2>위키와 사실 검토</h2><p class="muted">위키 문장 승인과 관계 사실 승인은 별개입니다. 원문의 인용을 대조하여 각각 검토하세요.</p><div id="items">${d.items.length?d.items.map(renderItem).join(''):'<div class="empty">아직 생성된 초안이 없습니다. 추출을 시작해 주세요.</div>'}</div>`;
  $('#document-detail').scrollIntoView({behavior:'smooth',block:'start'});
  $('#version-select').addEventListener('change', e=>attempt(()=>openDoc(id,Number(e.target.value))));
  $('#add-version').onclick = ()=>openUpload(d);
  $('#show-diff').onclick = ()=>attempt(async()=>{const r=await api(`/api/documents/${id}/diff?version=${d.version}`);$('#version-diff').innerHTML=`<pre class="source">${esc(r.diff||'변경 없음')}</pre>`;});
  if ($('#extract-agent')) $('#extract-agent').onchange = e=>{$('#extract-model').value=config.models[e.target.value]||'';};
  if ($('#extract')) $('#extract').onclick = ()=>attempt(async()=>{
    const button=$('#extract'); button.disabled=true;
    try {const run=await api(`/api/documents/${id}/extract`,'POST',{agent:$('#extract-agent').value,model:$('#extract-model').value});notice('추출을 시작했습니다. 실행 기록에서 진행 상황을 볼 수 있습니다.'); watchRun(run.id,id);}
    catch(e){button.disabled=false;throw e;}
  });
}
function renderItem(item) {
  const p=item.payload;
  const body=item.kind==='page'?`<h3>${esc(p.title)}</h3>${p.claims.map(c=>`<h3>${esc(c.heading)}</h3><p>${esc(c.text)}</p><details><summary>원문 근거</summary><blockquote class="quote">${esc(c.quote)}</blockquote></details>`).join('')}`:`<h3 class="query-row">${esc(p.subject)} → <small>${esc(p.predicate)}</small> → ${esc(p.object)}</h3><blockquote class="quote">${esc(p.quote)}</blockquote>`;
  return `<article class="item">${badge(item.status)} <span class="muted">${item.kind==='page'?'위키 초안':'관계 사실'} · r${item.revision}</span>${body}<div class="actions">${['approved','held','rejected','pending',...(item.kind==='fact'?['revise']:[])].filter(a=>a!==item.status).map(a=>`<button data-review="${item.id}" data-action="${a}">${a==='revise'?'내용 수정':a==='pending'?'승인 철회 / 재검토':labels[a]}</button>`).join('')}<button data-history="${item.id}">이력</button></div><div id="history-${item.id}"></div></article>`;
}
async function watchRun(id, docId) {
  try {
    const r=await api('/api/runs/'+id);
    if (['queued','running'].includes(r.status)) {notice(`추출 중 · ${r.detail||'대기 중'}`);setTimeout(()=>watchRun(id,docId),1500);return;}
    notice(r.status==='success'?'위키 초안이 준비되었습니다. 원문과 대조해 승인하세요.':`추출 ${labels[r.status]}: ${r.detail}`,r.status!=='success');
    await loadLibrary();
    if(selectedDoc?.id===docId && !$('#library').hidden) await openDoc(docId);
  } catch(e){notice(e.message,true);}
}
function openUpload(doc=null) {
  uploadTarget=doc;$('#upload-form').reset();$('#upload-form').elements.title.value=doc?.title||'';$('#upload-form').elements.title.readOnly=Boolean(doc);
  $('#upload-heading').textContent=doc?`새 문서 버전 · v${doc.current_version+1}`:'문서 등록';
  $('#upload-dialog').showModal();
}
$('#new-document').onclick=()=>openUpload();$('#close-upload').onclick=()=>$('#upload-dialog').close();
$('#upload-form').onsubmit=e=>{e.preventDefault();attempt(async()=>{
  const form=e.target,file=form.elements.file.files[0],button=$('button.primary',form);button.disabled=true;
  try {
    const data={title:form.elements.title.value,filename:file?.name||'note.md'};
    if(file){if(file.size>4*1024*1024)throw new Error('최대 파일 크기는 4 MiB입니다.');const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));data.content_base64=btoa(binary);}
    else data.text=form.elements.text.value;
    if(uploadTarget){data.document_id=uploadTarget.id;data.expected_version=uploadTarget.current_version;}
    const d=await api('/api/documents','POST',data);$('#upload-dialog').close();await loadLibrary();await openDoc(d.id);notice('원문을 보존했습니다. 에이전트를 선택해 위키 초안을 만드세요.');
  } finally{button.disabled=false;}
});};
function openReview(id,action) {
  const item=selectedDoc.items.find(i=>i.id===id);reviewTarget={item,action};$('#review-form').reset();
  $('#review-heading').textContent=action==='revise'?'후보 수정 → 재검토':action==='pending'?'승인 철회 / 재검토':`${labels[action]} 기록`;
  $('#revision-fields').innerHTML=action==='revise'?['subject','predicate','object','quote'].map(k=>`<label>${k}<textarea name="${k}" required>${esc(item.payload[k])}</textarea></label>`).join(''):'';
  $('#review-dialog').showModal();
}
$('#cancel-review').onclick=()=>$('#review-dialog').close();
$('#review-form').onsubmit=e=>{e.preventDefault();attempt(async()=>{
  const {item,action}=reviewTarget,form=e.target;
  const data={revision:item.revision,action,actor:form.elements.actor.value,reason:form.elements.reason.value};
  if(action==='revise')data.payload=Object.fromEntries(['subject','predicate','object','quote'].map(k=>[k,form.elements[k].value]));
  await api(`/api/items/${item.id}/review`,'POST',data);$('#review-dialog').close();await openDoc(selectedDoc.id,selectedDoc.version);notice('검토 이력을 저장했습니다.');
});};
async function loadSettings() {
  config=await api('/api/settings');
  $('#provider-settings').innerHTML=config.providers.map(p=>`<div class="provider-row"><input type="radio" name="selected" value="${p.id}" ${config.selected===p.id?'checked':''} aria-label="${esc(p.name)} 기본 선택"><div><strong>${esc(p.name)}</strong><p class="muted">${p.installed?'● 설치됨':'○ 설치 필요'}</p></div><label>모델 ID<input name="model-${p.id}" value="${esc(config.models[p.id]||'')}" placeholder="CLI 기본 모델"></label></div>`).join('');
}
$('#settings-form').onsubmit=e=>{e.preventDefault();attempt(async()=>{
  const form=e.target;await api('/api/settings','PUT',{selected:form.elements.selected.value,models:Object.fromEntries(config.providers.map(p=>[p.id,form.elements['model-'+p.id].value]))});await loadSettings();if($('#extract-agent')){$('#extract-agent').value=config.selected;$('#extract-model').value=config.models[config.selected]||'';}notice('에이전트별 모델 설정을 저장했습니다.');
});};
$('#query-form').onsubmit=e=>{e.preventDefault();attempt(async()=>{
  const data=Object.fromEntries(new FormData(e.target));if(!data.snapshot)delete data.snapshot;
  const r=await api('/api/query','POST',data);renderQuery(r);await loadQueries();notice(`${r.rows.length}개 결과 · 실제 PyreWire 계산 완료`);
});};
function renderQuery(r) {
  $('#query-result').innerHTML=`<div class="panel"><h2>${r.rows.length}개의 관계 ${r.status==='empty'?'· 일치 결과 없음':''}</h2><p class="mono">스냅샷 ${esc(r.snapshot)}</p><p class="muted">${esc(r.engine)} ${esc(r.engine_version)} · ${esc(r.rule_id)}</p>${r.proofs.map(p=>`<div class="item"><h3 class="query-row">${p.row.map(esc).join(' → ')}</h3><details open><summary>증명 경로 · ${p.fact_ids.length}개 사실</summary>${p.fact_ids.map(id=>{const f=r.facts.find(f=>f.id===id);return `<p>${esc(f.payload.subject)} → ${esc(f.payload.predicate)} → ${esc(f.payload.object)}</p><blockquote class="quote">${esc(f.payload.quote)}</blockquote><p class="mono">사실 ${esc(f.id)} · r${f.revision}</p>${sourceLink(f.document_id,f.version)}`;}).join('')}</details></div>`).join('')}<p class="muted">${esc(r.scope)}</p><details><summary>규칙과 전체 결과 JSON</summary><pre class="source">${esc(JSON.stringify(r,null,2))}</pre></details><button id="download-query">결과 JSON 다운로드</button></div>`;
  $('#download-query').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(r,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='verinote-query.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
}
async function loadQueries() {
  const rows=await api('/api/queries');
  $('#query-history').innerHTML=rows.map(r=>`<div class="history-line"><button data-replay="${esc(r.id)}">과거 스냅샷 재조회</button> ${esc(r.payload.query.action)} · ${esc(r.payload.query.subject||'전체')} · ${esc(date(r.created_at))}</div>`).join('')||'<p>아직 실행한 질의가 없습니다.</p>';
  for(const button of document.querySelectorAll('[data-replay]'))button.onclick=()=>attempt(async()=>{const r=rows.find(r=>r.id===button.dataset.replay).payload;renderQuery(await api('/api/query','POST',{...r.query,snapshot:r.snapshot}));});
}
async function loadRuns() {
  const rows=await api('/api/runs');
  $('#runs').innerHTML=rows.map(r=>`<div class="panel">${badge(r.status)} <strong>${esc(r.agent)} / ${esc(r.model||'기본 모델')}</strong><p>${esc(r.detail)}</p><p class="muted">${esc(date(r.created_at))}</p>${sourceLink(r.document_id,r.version)}<details><summary>실행 메타데이터</summary><pre class="source">${esc(JSON.stringify(r,null,2))}</pre><p class="muted">프롬프트·stdout·stderr: data/runs/${esc(r.id)}/</p></details></div>`).join('')||'<div class="empty">아직 실행 기록이 없습니다.</div>';
}
document.addEventListener('click',e=>{
  const b=e.target.closest('button');if(!b)return;
  if(b.dataset.view)showView(b.dataset.view);
  if(b.dataset.doc)attempt(()=>openDoc(b.dataset.doc));
  if(b.dataset.source)attempt(()=>openDoc(b.dataset.source,Number(b.dataset.version)));
  if(b.dataset.review)openReview(b.dataset.review,b.dataset.action);
  if(b.dataset.history)attempt(async()=>{const rows=await api(`/api/items/${b.dataset.history}/history`);$('#history-'+b.dataset.history).innerHTML=rows.map(r=>`<div class="history-line"><strong>r${r.revision} · ${esc(r.action)}</strong> · ${esc(r.actor)}<br>${esc(r.reason)}<br><span class="muted">${esc(date(r.created_at))}</span><details><summary>당시 내용</summary><pre class="source">${esc(JSON.stringify(r.payload,null,2))}</pre></details></div>`).join('');});
});
let searchTimer;$('#search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>attempt(renderDocs),250);};
attempt(async()=>{await loadSettings();await loadLibrary();});

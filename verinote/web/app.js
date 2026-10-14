'use strict';
const $ = (s, root = document) => root.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {pending:'검토 대기', approved:'승인', rejected:'반려', held:'보류', queued:'대기 중', running:'처리 중', success:'완료', error:'실패', interrupted:'중단'};
const badge = status => `<span class="badge ${esc(status)}">${esc(labels[status] || status)}</span>`;
let docs = [], config, selectedDoc, uploadTarget, reviewTarget;
let documentRequest = 0, bulkReviewTarget = null, bulkSubmitting = false;
const MAX_BULK_APPROVAL = 2000;
async function api(path, method = 'GET', data) {
  const response = await fetch(path, {method, headers: data ? {'Content-Type':'application/json'} : {}, body: data ? JSON.stringify(data) : undefined});
  const result = await response.json();
  if (!response.ok) {const error=new Error(result.error || `HTTP ${response.status}`);error.status=response.status;throw error;}
  return result;
}
function notice(message, error = false) { $('#notice').textContent = message; $('#notice').className = error ? 'error' : ''; }
async function attempt(fn) { try { await fn(); } catch (error) { notice(error.message, true); } }
function date(value) { return new Date(value).toLocaleString('ko-KR'); }
function sourceLink(id, version, label='원문 보기') { return `<button data-source="${esc(id)}" data-version="${version}">${esc(label)} · v${version}</button>`; }
function showView(name) {
  if (name !== 'library') documentRequest++;
  for (const el of document.querySelectorAll('.view')) el.hidden = el.id !== name;
  for (const el of document.querySelectorAll('nav button')) el.classList.toggle('active', el.dataset.view === name);
  $('#breadcrumb').textContent = {library:'문서와 위키',query:'관계 탐색',activity:'실행 기록',settings:'에이전트 설정'}[name];
  if (name === 'activity') attempt(loadRuns);
  if (name === 'query') attempt(async()=>{await loadQueries();await loadQuestions();});
}
async function loadLibrary() {
  docs = await api('/api/documents');
  $('#stats').innerHTML = `<div class="stat">등록 문서<strong>${docs.length}</strong></div><div class="stat">보존된 원문 버전<strong>${docs.reduce((n,d)=>n+d.version_count,0)}</strong></div><div class="stat">지식의 기준<strong>사람의 승인</strong></div>`;
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
function extractionControls(d) {
  const dictionary=d.items.find(i=>i.kind==='ontology');
  return `<h3>1. 사전 생성 → 2. 사전 수정·승인 → 3. 위키·관계 추출</h3><div class="form-grid">${providerSelect()}</div><div class="actions"><button class="primary" id="ontology" ${dictionary?'disabled':''}>1. 개체·별칭·관계 사전 생성</button><button class="primary" id="extract" ${d.can_extract?'':'disabled'}>3. 승인 사전으로 추출</button></div><p class="muted">${!dictionary?'먼저 사전을 생성하세요.':dictionary.status!=='approved'?'아래 사전에서 이름·별칭·관계 방향과 타입을 확인하고 승인하세요.':d.can_extract?'사전이 승인되었습니다. 이 사전을 기준으로 추출할 수 있습니다.':'이 사전 revision의 추출이 완료되었습니다. 변경하려면 사전을 수정·재승인하세요.'} 원문이 선택한 모델 제공자에게 전달됩니다. 기존 결과는 보존하며 새 결과는 검토 대기로 추가합니다.</p>`;
}
function renderDictionary(p) {
  return `<h3>2. 추출 기준 사전 검토</h3><p>개체의 정규 ID, 별칭의 동일성, 관계의 의미·방향·타입을 원문과 대조하세요. 사전 승인 후에도 추출된 사실은 별도로 승인합니다.</p><h3>개체 · ${p.entities.length}</h3><p>각 항목이 하나의 개체입니다. <strong>개체 ID</strong>가 관계의 주어·목적어와 조회에 사용됩니다. <strong>원문 명칭</strong>은 문서에 쓰인 이름이고, <strong>개체 유형</strong>은 분류입니다. 같은 ID는 같은 개체로 연결되므로 원문 근거와 함께 확인하세요.</p>${p.entities.map(e=>`<section class="panel"><h4>원문 명칭: ${esc(e.name)}</h4><p class="query-row"><strong>개체 ID:</strong> <code>${esc(e.id)}</code></p><p><strong>개체 유형:</strong> ${esc(e.type)}</p><strong>원문 근거</strong><blockquote class="quote">${esc(e.quote)}</blockquote></section>`).join('')}<h3>별칭 · ${p.aliases.length}</h3>${p.aliases.map(a=>`<details><summary>${esc(a.alias)} → ${esc(a.entity)}</summary><blockquote class="quote">${esc(a.quote)}</blockquote></details>`).join('')}<h3>관계 · ${p.relations.length}</h3>${p.relations.map(r=>`<div><strong>${esc(r.id)}</strong><p>${r.subject_types.map(esc).join(', ')} → ${r.object_types.map(esc).join(', ')}<br>${esc(r.description)}</p><details><summary>원문 근거</summary><blockquote class="quote">${esc(r.quote)}</blockquote></details></div>`).join('')}`;
}
async function openDoc(id, version) {
  const request = ++documentRequest;
  const loaded = await api(`/api/documents/${id}${version?'?version='+version:''}`);
  if (request !== documentRequest) return;
  selectedDoc = loaded;
  const d = selectedDoc, latest = d.version === d.current_version;
  showView('library');
  $('#document-detail').innerHTML = `<div class="panel"><div class="detail-title"><h2>${esc(d.title)}</h2><select id="version-select" aria-label="문서 버전">${d.versions.map(v=>`<option value="${v.version}" ${v.version===d.version?'selected':''}>v${v.version}${v.version===d.current_version?' · 현재':''}</option>`).join('')}</select><button id="add-version">＋ 새 버전 등록</button></div><p class="muted">${latest?'현재 문서 버전':'과거 문서 버전 · 현재 그래프에 포함되지 않습니다'} · ${esc(d.filename)}</p><p class="mono">SHA-256 ${esc(d.sha256)}</p><div class="actions"><a href="/api/documents/${d.id}/raw?version=${d.version}" download="${esc(d.filename)}">원본 다운로드</a><button id="show-diff">이전 버전과 비교</button></div><div class="actions"><button id="delete-latest" class="danger" ${d.versions.length===1?'disabled':''}>최신 리비전 v${d.current_version}만 삭제</button><button id="delete-document" class="danger">문서 전체 삭제</button></div>${d.versions.length===1?'<p class="muted">리비전이 하나뿐이면 문서 전체 삭제를 사용하세요.</p>':''}<div id="version-diff"></div><details><summary>원문 전체 · v${d.version}</summary><pre class="source">${esc(d.text)}</pre></details>${latest?extractionControls(d):''}</div><h2>사전 · 위키 · 사실 검토</h2><p class="muted">위키 문장 승인과 관계 사실 승인은 별개입니다. 원문의 인용을 대조하여 각각 검토하세요.</p>${renderBulkControls(d)}<div id="items">${d.items.length?d.items.map(renderItem).join(''):'<div class="empty">아직 생성된 초안이 없습니다. 사전 생성을 시작해 주세요.</div>'}</div>`;
  $('#document-detail').scrollIntoView({behavior:'smooth',block:'start'});
  $('#bulk-select-all').onchange = e=>{for(const box of document.querySelectorAll('[data-bulk-item]')) box.checked=e.target.checked;updateBulkSelection();};
  $('#bulk-clear').onclick = ()=>{for(const box of document.querySelectorAll('[data-bulk-item]')) box.checked=false;updateBulkSelection();};
  $('#bulk-approve').onclick = ()=>attempt(openBulkReview);
  $('#version-select').addEventListener('change', e=>attempt(()=>openDoc(id,Number(e.target.value))));
  $('#add-version').onclick = ()=>openUpload(d);
  $('#delete-latest').onclick=()=>openDelete(d,true);
  $('#delete-document').onclick=()=>openDelete(d,false);
  $('#show-diff').onclick = ()=>attempt(async()=>{const r=await api(`/api/documents/${id}/diff?version=${d.version}`);$('#version-diff').innerHTML=`<pre class="source">${esc(r.diff||'변경 없음')}</pre>`;});
  if ($('#extract-agent')) $('#extract-agent').onchange = e=>{$('#extract-model').value=config.models[e.target.value]||'';};
  for (const stage of ['ontology','extract']) {
    const button=$('#'+stage);
    if(button) button.onclick=()=>attempt(async()=>{
      button.disabled=true;
      try {const run=await api(`/api/documents/${id}/${stage}`,'POST',{agent:$('#extract-agent').value,model:$('#extract-model').value,expected_version:d.version,expected_generation:d.generation});notice('작업을 시작했습니다. 실행 기록에서 진행 상황을 볼 수 있습니다.');watchRun(run.id,id);}
      catch(e){button.disabled=false;throw e;}
    });
  }
}
function renderItem(item) {
  const p=item.payload;
  const body=item.kind==='ontology'?renderDictionary(p):item.kind==='page'?`<h3>${esc(p.title)}</h3>${p.claims.map(c=>`<h3>${esc(c.heading)}</h3><p>${esc(c.text)}</p><details><summary>원문 근거</summary><blockquote class="quote">${esc(c.quote)}</blockquote></details>`).join('')}`:`<h3 class="query-row">${esc(p.subject)} → <small>${esc(p.predicate)}</small> → ${esc(p.object)}</h3><blockquote class="quote">${esc(p.quote)}</blockquote>`;
  return `<article class="item">${item.status==='pending'&&['page','fact'].includes(item.kind)?`<label class="bulk-choice"><input type="checkbox" data-bulk-item="${esc(item.id)}" aria-label="${esc(bulkItemLabel(item))} 일괄 승인에 포함">일괄 승인에 포함</label>`:''}${badge(item.status)} <span class="muted">${{ontology:'사전 초안',page:'위키 초안',fact:'관계 사실'}[item.kind]} · r${item.revision}</span>${item.dictionary?`<p class="muted">추출 기준 사전 r${item.dictionary.revision} · ${esc(item.dictionary.sha256.slice(0,12))}</p>`:''}${body}<div class="actions">${['approved','held','rejected','pending',...(['fact','ontology'].includes(item.kind)?['revise']:[])].filter(a=>a!==item.status).map(a=>`<button data-review="${item.id}" data-action="${a}">${a==='revise'?'내용 수정':a==='pending'?'승인 철회 / 재검토':labels[a]}</button>`).join('')}<button data-history="${item.id}">이력</button></div><div id="history-${item.id}"></div></article>`;
}
async function watchRun(id, docId) {
  try {
    const r=await api('/api/runs/'+id);
    if (['queued','running'].includes(r.status)) {notice(`추출 중 · ${r.detail||'대기 중'}`);setTimeout(()=>watchRun(id,docId),1500);return;}
    notice(r.status==='success'?((r.stage==='ontology'?'사전 초안이 준비되었습니다. 개체·별칭·관계 정의를 수정·승인한 다음 추출하세요.':'위키와 관계 후보가 준비되었습니다. 원문과 대조해 각각 승인하세요.')+(r.detail?` ${r.detail}`:'')):`추출 ${labels[r.status]}: ${r.detail}`,r.status!=='success');
    await loadLibrary();
    if(selectedDoc?.id===docId && !$('#library').hidden) await openDoc(docId);
  } catch(e){notice(e.message,true);}
}
function openUpload(doc=null) {
  uploadTarget=doc;$('#upload-form').reset();$('#upload-form').elements.title.value=doc?.title||'';$('#upload-form').elements.title.readOnly=Boolean(doc);
  $('#upload-heading').textContent=doc?`새 문서 버전 · v${doc.next_version}`:'문서 등록';
  $('#upload-dialog').showModal();
}
$('#new-document').onclick=()=>openUpload();$('#close-upload').onclick=()=>$('#upload-dialog').close();
$('#upload-form').onsubmit=e=>{e.preventDefault();attempt(async()=>{
  const form=e.target,file=form.elements.file.files[0],button=$('button.primary',form);button.disabled=true;
  try {
    const data={title:form.elements.title.value,filename:file?.name||'note.md'};
    if(file){if(file.size>4*1024*1024)throw new Error('최대 파일 크기는 4 MiB입니다.');const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));data.content_base64=btoa(binary);}
    else data.text=form.elements.text.value;
    if(uploadTarget){data.document_id=uploadTarget.id;data.expected_version=uploadTarget.current_version;data.expected_generation=uploadTarget.generation;}
    const d=await api('/api/documents','POST',data);$('#upload-dialog').close();await loadLibrary();await openDoc(d.id);notice('원문을 보존했습니다. 개체·별칭·관계 사전을 먼저 생성하세요.');
  } finally{button.disabled=false;}
});};
function bulkCandidates(doc) {
  return doc.items.filter(item=>item.status==='pending'&&['page','fact'].includes(item.kind));
}
function bulkItemLabel(item) {
  return item.kind==='page'?`위키: ${item.payload.title}`:`관계: ${item.payload.subject} → ${item.payload.predicate} → ${item.payload.object}`;
}
function captureBulkReview(doc, selectedIds) {
  const chosen=bulkCandidates(doc).filter(item=>selectedIds.includes(item.id));
  if(!chosen.length||chosen.length>MAX_BULK_APPROVAL) throw new Error(`일괄 승인은 1~${MAX_BULK_APPROVAL}개 항목을 선택하세요.`);
  return {documentId:doc.id,title:doc.title,version:doc.version,expected_version:doc.current_version,expected_generation:doc.generation,
    items:chosen.map(item=>({id:item.id,revision:item.revision})),pages:chosen.filter(item=>item.kind==='page').length,facts:chosen.filter(item=>item.kind==='fact').length};
}
function renderBulkControls(doc) {
  const count=bulkCandidates(doc).length;
  return `<section class="bulk-toolbar panel" aria-label="일괄 승인"><strong>검토 대기 위키·관계 일괄 승인</strong><p class="muted">현재 보고 있는 v${doc.version}의 항목을 선택하세요. 사전은 별도로 승인합니다.</p><div class="actions"><label class="bulk-choice"><input id="bulk-select-all" type="checkbox" ${count?'':'disabled'}>검토 대기 전체 선택 (${count}개)</label><button id="bulk-clear" ${count?'':'disabled'}>선택 해제</button><button id="bulk-approve" class="primary" disabled>선택 0개 승인</button></div><p id="bulk-count" role="status">선택 없음</p><p id="bulk-limit" role="alert" hidden>한 번에 최대 ${MAX_BULK_APPROVAL}개까지 승인할 수 있습니다. 일부 선택을 해제하세요.</p></section>`;
}
function updateBulkSelection() {
  const boxes=[...document.querySelectorAll('[data-bulk-item]')],selected=boxes.filter(box=>box.checked);
  const ids=new Set(selected.map(box=>box.dataset.bulkItem));
  const items=bulkCandidates(selectedDoc).filter(item=>ids.has(item.id));
  const pages=items.filter(item=>item.kind==='page').length,facts=items.length-pages;
  $('#bulk-count').textContent=items.length?`선택 ${items.length}개 · 위키 ${pages}개 · 관계 ${facts}개`:'선택 없음';
  $('#bulk-approve').textContent=`선택 ${items.length}개 승인`;
  $('#bulk-approve').disabled=!items.length||items.length>MAX_BULK_APPROVAL;
  $('#bulk-limit').hidden=items.length<=MAX_BULK_APPROVAL;
  $('#bulk-select-all').checked=boxes.length>0&&selected.length===boxes.length;
  $('#bulk-select-all').indeterminate=selected.length>0&&selected.length<boxes.length;
}
function openBulkReview() {
  if(bulkSubmitting) return;
  const ids=[...document.querySelectorAll('[data-bulk-item]:checked')].map(box=>box.dataset.bulkItem);
  const target=captureBulkReview(selectedDoc,ids);
  bulkReviewTarget=target;
  $('#bulk-review-form').reset();$('#bulk-review-error').textContent='';$('#bulk-refresh').hidden=true;
  $('#bulk-review-scope').textContent=`${target.title} · v${target.version} · 위키 ${target.pages}개 + 관계 ${target.facts}개 = 총 ${target.items.length}개`;
  $('#bulk-historical').hidden=target.version===target.expected_version;
  const selected=new Set(target.items.map(item=>item.id));
  $('#bulk-review-preview').innerHTML=bulkCandidates(selectedDoc).filter(item=>selected.has(item.id)).map(item=>`<details><summary>${esc(bulkItemLabel(item))}</summary>${item.kind==='page'?item.payload.claims.map(c=>`<p>${esc(c.text)}</p><blockquote class="quote">${esc(c.quote)}</blockquote>`).join(''):`<blockquote class="quote">${esc(item.payload.quote)}</blockquote>`}</details>`).join('');
  $('#bulk-confirm').textContent=`${target.items.length}개 승인 저장`;
  $('#bulk-review-dialog').showModal();
}
document.addEventListener('change',e=>{if(e.target.matches('[data-bulk-item]'))updateBulkSelection();});
$('#bulk-cancel').onclick=()=>{if(!bulkSubmitting)$('#bulk-review-dialog').close();};
$('#bulk-review-dialog').addEventListener('cancel',e=>{if(bulkSubmitting)e.preventDefault();});
$('#bulk-refresh').onclick=()=>attempt(async()=>{const target=bulkReviewTarget;$('#bulk-review-dialog').close();bulkReviewTarget=null;await openDoc(target.documentId);});
$('#bulk-review-form').onsubmit=e=>{e.preventDefault();attempt(async()=>{
  if(bulkSubmitting||!bulkReviewTarget) return;
  const target=bulkReviewTarget,form=e.target,viewRequest=documentRequest;
  const data={version:target.version,expected_version:target.expected_version,expected_generation:target.expected_generation,
    items:target.items,actor:form.elements.actor.value,reason:form.elements.reason.value};
  bulkSubmitting=true;$('#bulk-confirm').disabled=true;$('#bulk-cancel').disabled=true;
  $('#bulk-review-error').textContent='';$('#bulk-refresh').hidden=true;
  let result;
  try {result=await api(`/api/documents/${target.documentId}/approve-items`,'POST',data);}
  catch(error){$('#bulk-review-error').textContent=error.message;$('#bulk-refresh').hidden=error.status!==409;return;}
  finally {bulkSubmitting=false;$('#bulk-confirm').disabled=false;$('#bulk-cancel').disabled=false;}
  $('#bulk-review-dialog').close();bulkReviewTarget=null;
  notice(`${result.approved_count}개 항목을 승인하고 각 항목의 검토 이력을 저장했습니다.`);
  try {
    await loadLibrary();
    if(documentRequest===viewRequest&&selectedDoc?.id===target.documentId&&selectedDoc.version===target.version&&!$('#library').hidden) await openDoc(target.documentId,target.version);
  } catch(error){notice(`${result.approved_count}개 승인은 완료됐지만 목록 새로고침에 실패했습니다. ${error.message}`,true);}
});};
function openReview(id,action) {
  const item=selectedDoc.items.find(i=>i.id===id);reviewTarget={item,action};$('#review-form').reset();$('#review-error').textContent='';
  $('#review-heading').textContent=action==='revise'?'후보 수정 → 재검토':action==='pending'?'승인 철회 / 재검토':`${labels[action]} 기록`;
  $('#revision-fields').innerHTML=action==='revise'&&item.kind==='ontology'?`<label>개체·별칭·관계 사전 JSON<textarea name="dictionary" rows="18" required>${esc(JSON.stringify(item.payload,null,2))}</textarea></label><p>id는 문서 간에도 의미 있는 정규 명칭을 쓰세요. 개체·별칭의 원문 인용과 관계의 방향·타입·포함 및 제외 기준을 확인하세요. 수정하면 다시 승인이 필요합니다.</p>`:action==='revise'?['subject','predicate','object','quote'].map(k=>`<label>${k}<textarea name="${k}" required>${esc(item.payload[k])}</textarea></label>`).join(''):'';
  $('#review-dialog').showModal();
}
$('#cancel-review').onclick=()=>$('#review-dialog').close();
$('#review-form').onsubmit=e=>{e.preventDefault();attempt(async()=>{
  $('#review-error').textContent='';
  try {
  const {item,action}=reviewTarget,form=e.target;
  const data={revision:item.revision,action,actor:form.elements.actor.value,reason:form.elements.reason.value};
  if(action==='revise')data.payload=item.kind==='ontology'?JSON.parse(form.elements.dictionary.value):Object.fromEntries(['subject','predicate','object','quote'].map(k=>[k,form.elements[k].value]));
  await api(`/api/items/${item.id}/review`,'POST',data);$('#review-dialog').close();await openDoc(selectedDoc.id,selectedDoc.version);notice('검토 이력을 저장했습니다.');
  } catch(error) { $('#review-error').textContent=error.message; }
});};
async function loadSettings() {
  config=await api('/api/settings');
  $('#ask-agent').innerHTML=config.providers.map(p=>`<option value="${esc(p.id)}" ${p.id===config.selected?'selected':''}>${esc(p.name)}${p.installed?'':' · 미설치'}</option>`).join('');
  $('#ask-model').value=config.models[config.selected]||'';
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
  await loadCleanup();
  const rows=await api('/api/runs');
  $('#runs').innerHTML=rows.map(r=>`<div class="panel">${badge(r.status)} <strong>${esc({ontology:'사전 생성',extract:'관계 추출',legacy:'기존 추출'}[r.stage])} · ${esc(r.agent)} / ${esc(r.model||'기본 모델')}</strong>${r.ontology?`<p>기준 사전 r${r.ontology.revision} · ${esc(r.ontology.sha256.slice(0,12))}</p>`:''}<p>${esc(r.detail)}</p><p class="muted">${esc(date(r.created_at))}</p>${sourceLink(r.document_id,r.version)}<details><summary>실행 메타데이터</summary><pre class="source">${esc(JSON.stringify(r,null,2))}</pre><p class="muted">프롬프트·stdout·stderr: data/runs/${esc(r.id)}/</p></details></div>`).join('')||'<div class="empty">아직 실행 기록이 없습니다.</div>';
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

let activeQuestion;
$('#ask-agent').onchange=e=>{$('#ask-model').value=config.models[e.target.value]||'';};
$('#ask-form').onsubmit=e=>{e.preventDefault();attempt(async()=>{
  const button=$('button',e.target);button.disabled=true;
  try {
    const data={question:$('#question-text').value,agent:$('#ask-agent').value,model:$('#ask-model').value};
    if($('#ask-snapshot').value.trim())data.snapshot=$('#ask-snapshot').value.trim();
    const job=await api('/api/questions','POST',data);activeQuestion=job.id;renderAnswer(job);await loadQuestions();watchQuestion(job.id);
  } finally {button.disabled=false;}
});};
async function loadQuestions(){
  const rows=await api('/api/questions');
  $('#question-history').innerHTML=rows.map(r=>`<div class="history-line"><button data-question="${esc(r.id)}">${esc(r.question)}</button> ${badge(r.status)} <span class="muted">${esc(r.agent)} · ${esc(date(r.created_at))}</span></div>`).join('')||'<p class="muted">아직 질문이 없습니다.</p>';
  for(const b of document.querySelectorAll('[data-question]'))b.onclick=()=>{activeQuestion=b.dataset.question;attempt(()=>watchQuestion(activeQuestion));};
}
async function watchQuestion(id){
  try {
    const job=await api('/api/questions/'+id);
    if(activeQuestion!==id)return;
    renderAnswer(job);
    if(['queued','running'].includes(job.status))setTimeout(()=>watchQuestion(id),1500);
    else await loadQuestions();
  }catch(e){if(activeQuestion===id)notice(e.message,true);}
}
function renderEvidence(id,e){
  if(e.kind==='source')return `<details><summary>${esc(id)} · 미검토 원문 · v${e.version}</summary><blockquote class="quote">${esc(e.text)}</blockquote>${sourceLink(e.document_id,e.version)}<p class="mono">SHA-256 ${esc(e.sha256)} · offset ${e.offset}</p></details>`;
  const r=e.result;
  return `<details><summary>${esc(id)} · Wirelog ${esc(r.query.action)} · ${r.rows.length}개 결과</summary><p>${r.rows.map(row=>row.map(esc).join(' → ')).join('<br>')||'해당 질의에서 일치 결과 없음'}</p>${r.facts.map(f=>`<blockquote class="quote">${esc(f.payload.quote)}</blockquote>${sourceLink(f.document_id,f.version)}<p class="mono">사실 ${esc(f.id)} · r${f.revision}</p>`).join('')}<p class="muted">${esc(r.scope)}</p><pre class="source">${esc(JSON.stringify(r.proofs,null,2))}</pre></details>`;
}
function renderAnswer(job){
  $('#answer-result').innerHTML=`<article class="panel"><h2>${esc(job.question)}</h2>${badge(job.status)} <span>${esc(job.agent)} / ${esc(job.model||'기본 모델')}</span><p class="muted">질문 제출 시 고정한 승인 상태입니다. 이후 문서 변경·승인 철회는 반영되지 않습니다.</p><p class="mono">스냅샷 ${esc(job.snapshot)}</p>${job.error?`<p class="error">${esc(job.error)}</p>`:''}${job.answer?job.answer.claims.map(c=>`<div class="item"><span class="badge">${esc({graph:'승인 그래프',source:'미검토 원문',inference:'추론'}[c.kind])}</span><p>${esc(c.text)}</p>${c.citations.map(id=>renderEvidence(id,job.evidence[id])).join('')}</div>`).join('')+job.answer.unknowns.map(u=>`<p class="muted">확인 불가 · ${esc(u)}</p>`).join(''): (['queued','running'].includes(job.status)?'<p>모델의 질의 해석과 도구 실행을 기다리고 있습니다.</p>':'<p>답변이 생성되지 않았습니다.</p>')}<details><summary>실행 추적 · ${job.trace.length}개 도구 호출</summary>${job.trace.map(t=>`<p>${esc(t.name)} · ${esc(t.status)}</p><pre class="source">${esc(JSON.stringify(t.arguments,null,2))}</pre>${t.evidence_id?renderEvidence(t.evidence_id,job.evidence[t.evidence_id]):''}`).join('')}<pre class="source">${esc(JSON.stringify(job,null,2))}</pre></details><p class="muted">인용 검사는 의미적 정확성을 보증하지 않습니다. 모델 해석·답변은 비결정적이며, 같은 질의·스냅샷·규칙·엔진의 그래프 계산만 결정적입니다.</p><div class="actions"><button id="download-answer">답변 JSON 다운로드</button><button id="rerun-answer">같은 스냅샷으로 다시 질문 준비</button></div></article>`;
  $('#download-answer').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(job,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='verinote-answer.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  $('#rerun-answer').onclick=()=>{$('#question-text').value=job.question;$('#ask-agent').value=job.agent;$('#ask-model').value=job.model;$('#ask-snapshot').value=job.snapshot;$('#ask-form').scrollIntoView({behavior:'smooth'});};
}

let deleteTarget;
function openDelete(doc,latestOnly){
  deleteTarget={doc,latestOnly};
  const previous=doc.versions.find(v=>v.version<doc.current_version);
  $('#delete-heading').textContent=latestOnly?`최신 리비전 v${doc.current_version}만 삭제`:'문서 전체 삭제';
  $('#delete-scope').textContent=`「${doc.title}」 — `+(latestOnly?`현재 최신 v${doc.current_version}의 원문·위키·사실·검토 이력을 삭제합니다. v${previous.version}과 그 승인 상태가 다시 현재 기준이 됩니다.`:`모든 ${doc.versions.length}개 리비전의 원문·위키·사실·검토 이력과 문서 자체를 삭제합니다.`);
  $('#delete-error').textContent='';$('#confirm-delete').disabled=false;
  $('#delete-dialog').showModal();
}
$('#cancel-delete').onclick=()=>$('#delete-dialog').close();
$('#confirm-delete').onclick=()=>attempt(async()=>{
  const {doc,latestOnly}=deleteTarget;$('#confirm-delete').disabled=true;
  try{
    const result=await api(`/api/documents/${doc.id}/${latestOnly?'delete-latest':'delete'}`,'POST',{expected_version:doc.current_version,expected_generation:doc.generation});
    $('#delete-dialog').close();activeQuestion=null;selectedDoc=null;
    for(const selector of ['#document-detail','#query-result','#answer-result','#runs'])$(selector).innerHTML='';
    await loadLibrary();await loadQueries();await loadQuestions();await loadCleanup();
    if(result.current_version)await openDoc(doc.id,result.current_version);
    notice(result.cleanup_pending?'원문과 연결된 DB 기록을 삭제했습니다. 일부 로컬 로그 정리가 남았습니다. 실행 기록에서 재시도하세요.':(latestOnly?'최신 리비전을 삭제하고 이전 리비전으로 돌아갔습니다.':'문서와 모든 리비전을 삭제했습니다.'),Boolean(result.cleanup_pending));
  }catch(error){
    if(error.status===409){$('#delete-dialog').close();await loadLibrary();await openDoc(doc.id);notice(error.message+' 삭제하려면 다시 선택하세요.',true);}
    else{$('#delete-error').textContent=error.message;$('#confirm-delete').disabled=false;}
  }
});
async function loadCleanup(){
  const state=await api('/api/cleanup');
  $('#cleanup-status').innerHTML=state.cleanup_pending?`<div class="panel"><p>삭제한 문서의 로컬 로그 ${state.cleanup_pending}개 정리가 남았습니다. DB 원문·관련 이력은 이미 삭제되었습니다.</p><button id="retry-cleanup">로그 정리 재시도</button></div>`:'';
  if($('#retry-cleanup'))$('#retry-cleanup').onclick=()=>attempt(async()=>{const result=await api('/api/cleanup','POST',{});await loadCleanup();notice(result.cleanup_pending?'정리가 아직 완료되지 않았습니다. 로그 폴더 접근 권한을 확인하세요.':'남은 로그 정리를 완료했습니다.',Boolean(result.cleanup_pending));});
}

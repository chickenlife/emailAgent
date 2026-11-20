"use strict";
let selected=null, current=null, pollingStarted=false;
let multiSelected=new Set(), selectionAnchor=null, latestItemIds=[];
let referenceSummaryQueue=[], referenceSummaryBusy=false, referenceAutoReady=false;
let referenceSummaryCache=new Map();
let loadedDraftText='';
let showRequestGeneration=0;
const $=id=>document.getElementById(id);
const api=async(path,options={})=>{
  const res=await fetch(path,{...options,credentials:'same-origin',headers:{...(options.body?{'Content-Type':'application/json'}:{}),...(options.headers||{})}});
  if(!res.ok){let err;try{err=await res.json()}catch(_){err={detail:res.statusText}}throw Error(err.detail||res.statusText)}return res.json();
};
const txt=(tag,value,cls='')=>{const el=document.createElement(tag);if(cls)el.className=cls;el.textContent=value??'';return el;};
const clear=el=>el.replaceChildren();
const flash=m=>{$('toast').textContent=m;$('toast').classList.remove('hidden');setTimeout(()=>$('toast').classList.add('hidden'),3500);};
function normalizeText(input){
  let value=String(input??'').replace(/\r\n?/g,'\n');
  if(/<\/?(?:p|div|br|table|tr|td|html|body|li|ul|ol|span|a|strong|b)(?:\s|>|\/)/i.test(value)){
    // Render as text only. HTML is never inserted in the live page.
    const doc=new DOMParser().parseFromString(value,'text/html');
    doc.querySelectorAll('script,style,iframe,object,svg,form,template').forEach(el=>el.remove());
    const output=[];
    function walk(node){
      if(node.nodeType===3){output.push(node.nodeValue||'');return;}
      if(node.nodeType!==1)return;
      const tag=node.tagName.toLowerCase();
      if(tag==='br'){output.push('\n');return;}
      if(['p','div','table','tr','li','h1','h2','h3','blockquote'].includes(tag))output.push('\n\n');
      for(const child of node.childNodes)walk(child);
      if(['p','div','table','tr','li','h1','h2','h3','blockquote'].includes(tag))output.push('\n\n');
    }
    for(const child of doc.body.childNodes)walk(child);
    value=output.join('');
  }
  // Entity decoding of stored text; avoid turning markup into HTML.
  const el=document.createElement('textarea');el.innerHTML=value;
  value=el.value;
  return value.replace(/\u00a0/g,' ').replace(/[ \t]+\n/g,'\n').replace(/\n[ \t]+/g,'\n').replace(/\n{3,}/g,'\n\n').trim();
}
function formatMailDate(raw){
  const v=String(raw||'').trim();if(!v)return '—';
  const iso=v.match(/^(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d)(?::(\d\d))?/);
  if(iso)return iso[1]+' '+iso[2]+':'+(iso[3]||'00');
  const date=new Date(v);
  if(!Number.isFinite(date.getTime()))return v;
  const parts=new Intl.DateTimeFormat('en-CA',{
    timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit',
    hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'
  }).formatToParts(date);
  const field=t=>parts.find(x=>x.type===t)?.value||'00';
  return `${field('year')}-${field('month')}-${field('day')} ${field('hour')}:${field('minute')}:${field('second')}`;
}
function metadata(el,rows){const wrap=txt('div','','meta');for(const [k,v] of rows){wrap.append(txt('span',k),txt('b',normalizeText(v||'—')));}el.append(wrap);}
let referenceOffset=0,referenceHasMore=false,referenceLoading=false;
let referenceIds=new Set(), citedIds=new Set(), searchIds=new Set();
// Message-ID identifies the same RFC822 mail copied to multiple folders.
// Fallback is a conservative fingerprint only when Message-ID is unavailable.
let citedMailKeys=new Set(), searchMailKeys=new Set();
function uniqueMailKey(mail){
  const messageId=String(mail.msgid||'').trim().replace(/^<|>$/g,'').toLowerCase();
  if(messageId)return 'msgid:'+messageId;
  const subject=String(mail.subject||'').trim().replace(/\s+/g,' ').toLowerCase();
  const sender=String(mail.sender||'').trim().replace(/\s+/g,' ').toLowerCase();
  const date=String(mail.date_text||'').trim();
  // Don't collapse distinct emails without at least these identifying fields.
  return subject&&sender&&date ? `headers:${sender}|${subject}|${date}` : `id:${mail.id}`;
}
let referenceGeneration=0, referenceExpanded=false;
let citedGroup=null, searchGroup=null;
const SUMMARY_FIELDS=[['main','주요 내용'],['request','요청사항'],['schedule','일정·기한'],['action','후속 조치']];
function updateSelectionUI(){
  document.querySelectorAll('#items .item').forEach(el=>{
    const id=Number(el.dataset.id);
    el.classList.toggle('multi-selected',multiSelected.has(id));
    el.classList.toggle('active',id===selected);
    el.setAttribute('aria-selected',String(multiSelected.has(id)));
  });
  const amount=multiSelected.size;
  $('delete').disabled=amount===0;
  $('delete').textContent=amount?`선택 삭제 (${amount}건)`:'선택 삭제';
}
function selectRange(anchorId,toId){
  let a=latestItemIds.indexOf(anchorId),b=latestItemIds.indexOf(toId);
  if(a<0)a=b;if(b<0)return;
  multiSelected=new Set(latestItemIds.slice(Math.min(a,b),Math.max(a,b)+1));
  updateSelectionUI();
}
function selectMailFromList(id,evt){
  if(evt.shiftKey){
    if(selectionAnchor===null||!latestItemIds.includes(selectionAnchor))selectionAnchor=selected??id;
    selectRange(selectionAnchor,id);
  }else if(evt.ctrlKey||evt.metaKey){
    if(multiSelected.has(id))multiSelected.delete(id);else multiSelected.add(id);
    selectionAnchor=id;updateSelectionUI();
  }else{
    multiSelected=new Set([id]);selectionAnchor=id;updateSelectionUI();
  }
  if(selected!==id)show(id);
}
function onMailKeydown(evt,id){
  if(evt.key!=='ArrowDown'&&evt.key!=='ArrowUp')return;
  evt.preventDefault();
  const currentIndex=latestItemIds.indexOf(id);
  if(currentIndex<0)return;
  const nextIndex=Math.max(0,Math.min(latestItemIds.length-1,currentIndex+(evt.key==='ArrowDown'?1:-1)));
  const nextId=latestItemIds[nextIndex];
  if(evt.shiftKey){
    if(selectionAnchor===null||!latestItemIds.includes(selectionAnchor))selectionAnchor=id;
    selectRange(selectionAnchor,nextId);
  }else{
    selectionAnchor=nextId;multiSelected=new Set([nextId]);updateSelectionUI();
  }
  const nextButton=Array.from(document.querySelectorAll('#items .item')).find(x=>Number(x.dataset.id)===nextId);
  nextButton?.focus();
  if(selected!==nextId)show(nextId);
}
async function runReferenceSummaryQueue(){
  if(referenceSummaryBusy||!referenceAutoReady)return;
  referenceSummaryBusy=true;
  try{
    while(referenceSummaryQueue.length){
      const task=referenceSummaryQueue.shift();
      if(task.mailId!==selected||task.generation!==referenceGeneration)continue;
      await task.run();
    }
  }finally{referenceSummaryBusy=false;
    if(referenceSummaryQueue.length)runReferenceSummaryQueue();
  }
}

function structuredSummaryBox(summary,status,label,model,extraClass=''){
  const box=txt('section','','mail-summary-box '+extraClass);
  box.append(txt('div','핵심 요약','mail-summary-label'));
  const state=txt('div','','mail-summary-status');box.append(state);
  const nodes={};
  for(const [key,title] of SUMMARY_FIELDS){
    const row=txt('div','','mail-summary-row');row.append(txt('div',title,'mail-summary-field-label'));
    nodes[key]=txt('div','','mail-summary-field-value');row.append(nodes[key]);box.append(row);
  }
  function update(data,statusValue,statusText,modelValue){
    state.textContent=`${statusText||'추출 요약'} · 모델: ${modelValue||'미설정'}`;
    state.dataset.status=statusValue||'fallback';
    for(const [key] of SUMMARY_FIELDS)nodes[key].textContent=data?.[key]||'메일에 명시되지 않음';
  }
  update(summary,status,label,model);
  return {box,update};
}
function addReferenceCard(item,kind,generation){
  // The four-field summary stays visible in the collapsed card. Only the
  // original mail content (metadata + body) is shown when expanding it.
  const card=txt('article','','evidence-card evidence-summary-card');
  const mailId=Number(item.id);
  const idBanner=txt('div',`참고 메일 ID #${mailId}`,'reference-mail-id-banner reply-draft-title');
  const summaryHeader=txt('div','','reference-card-header');
  const title=txt('div',normalizeText(item.subject||'(제목 없음)'),'reference-card-subject');
  // This local DB message ID is the same ID referenced by [근거:ID] in the draft.
  const tag=txt('small',`${item.role==='sent'?'발신함':'수신/보관함'} · ${item.folder||''} · ${formatMailDate(item.date_text)}`,'reference-card-kind');
  summaryHeader.append(title,tag);

  const summaryPanel=txt('section','','reference-card-summary-panel');
  const heading=txt('div','핵심 요약','reference-card-summary-title reply-draft-title');
  const status=txt('div','원문 추출 요약 · AI 미분석','reference-card-summary-status');
  const fields={};
  for(const [key,label] of SUMMARY_FIELDS){
    const row=txt('div','','reference-card-summary-row');
    const labelEl=txt('div',label,'reference-card-field-label');
    const valueEl=txt('div','','reference-card-field-value');
    fields[key]=valueEl;
    row.append(labelEl,valueEl);summaryPanel.append(row);
  }
  function updateSummary(data,stateLabel,model){
    for(const [key] of SUMMARY_FIELDS){
      fields[key].textContent=normalizeText(data?.[key]||'메일에 명시되지 않음');
    }
    status.textContent=stateLabel+(model?' · 모델: '+model:'');
  }
  updateSummary(item.summary_structured,'원문 추출 요약 · AI 미분석','');

  const actions=txt('div','','reference-card-actions');
  const improve=txt('button','AI 요약 재시도','reference-analyze-btn hidden');
  improve.type='button';
  improve.setAttribute('aria-label','이 참고 메일을 AI로 요약');
  const toggle=txt('button','원본 메일 보기 ▾','reference-original-toggle');
  toggle.type='button';toggle.setAttribute('aria-expanded','false');
  actions.append(improve,toggle);
  const ratingControls=txt('div','','feedback-controls reference-feedback');
  const ratingLabel=txt('span','RAG 근거 평가','feedback-caption');
  const goodButton=txt('button','적합','feedback-good');
  const badButton=txt('button','부적합','feedback-bad');
  goodButton.type='button';badButton.type='button';
  function markRating(rating){
    goodButton.classList.toggle('rated',rating==='good');
    badButton.classList.toggle('rated',rating==='bad');
    goodButton.setAttribute('aria-pressed',String(rating==='good'));
    badButton.setAttribute('aria-pressed',String(rating==='bad'));
  }
  markRating(item.feedback_rating);
  async function rateReference(rating){
    if(!selected)return;
    const target=selected;
    goodButton.disabled=badButton.disabled=true;
    try{
      const res=await api(`/api/items/${target}/references/${mailId}/feedback`,{method:'POST',body:JSON.stringify({rating})});
      if(target===selected){markRating(res.rating);flash('참고 메일 평가를 저장했습니다.');}
    }catch(e){flash('참고 메일 평가 실패: '+e.message);}
    finally{goodButton.disabled=badButton.disabled=false;}
  }
  goodButton.onclick=()=>rateReference('good');
  badButton.onclick=()=>rateReference('bad');
  ratingControls.append(ratingLabel,goodButton,badButton);

  status.textContent='AI 자동 요약 대기 · 원문 추출 요약 표시 중';
  let analyzing=false;
  async function analyzeReference(){
    if(analyzing||!selected)return;
    analyzing=true;improve.disabled=true;improve.textContent='AI 분석 중…';
    const requested=selected;
    const cacheKey=String(item.id);
    status.textContent='AI 분석 중 · 요약 모델 처리 중';
    try{
      const r=referenceSummaryCache.get(cacheKey)||await api(`/api/items/${requested}/references/${item.id}/summary`);
      if(requested!==selected||generation!==referenceGeneration)return;
      if(r.status==='success')referenceSummaryCache.set(cacheKey,r);
      updateSummary(r.summary,r.status_label||'요약 결과',r.model);
      if(r.status==='success'){
        improve.classList.add('hidden');improve.textContent='AI 요약 완료';
      }else{
        improve.textContent='AI 요약 재시도';
        improve.classList.remove('hidden');improve.disabled=false;
      }
    }catch(e){
      if(requested!==selected||generation!==referenceGeneration)return;
      updateSummary(item.summary_structured,'AI 분석 요청 실패 · 원문 추출 요약','');
      improve.textContent='AI 요약 재시도';improve.classList.remove('hidden');improve.disabled=false;
    }finally{analyzing=false;}
  }
  improve.onclick=analyzeReference;
  // AI summaries run automatically for BOTH cited and searched mail cards.
  // One request at a time and only while this incoming mail remains selected.
  referenceSummaryQueue.push({mailId:selected,generation,run:analyzeReference});
  runReferenceSummaryQueue();

  const detail=txt('div','','reference-detail reference-original-content');detail.hidden=true;
  const head=txt('div','','evidence-title');head.append(txt('div',normalizeText(item.subject||'(제목 없음)')));detail.append(head);
  metadata(detail,[['메일 ID',String(mailId)],['발신자',item.sender],['수신자',item.recipients],['참조자',item.cc],['일시',formatMailDate(item.date_text)]]);
  detail.append(txt('div',item.body_display??normalizeText(item.body||'(본문 없음)'),'evidence-body'));
  toggle.onclick=()=>{
    detail.hidden=!detail.hidden;
    toggle.setAttribute('aria-expanded',String(!detail.hidden));
    toggle.textContent=detail.hidden?'원본 메일 보기 ▾':'원본 메일 접기 ▴';
  };
  // The full card can be clicked to open the original, while the separate
  // AI-summary action does not expand the original mail.
  card.onclick=e=>{
    if(detail.contains(e.target)||e.target.closest('button'))return;
    toggle.click();
  };
  summaryHeader.setAttribute('role','button');summaryHeader.tabIndex=0;
  summaryHeader.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle.click();}};
  card.append(idBanner,summaryHeader,heading,status,summaryPanel,ratingControls,actions,detail);
  (kind==='AI 인용'?citedGroup:searchGroup).append(card);
}
function createReferenceGroup(title,subtitle){
  const section=txt('section','','reference-group');
  const header=txt('div','','reference-group-heading');
  header.append(txt('strong',title),txt('small',subtitle));
  section.append(header);
  $('right-content').append(section);
  return section;
}
function referenceCounts(){
  // These are unique, actually loaded mail cards, NOT token/keyword hits.
  const cited=citedMailKeys.size, searched=searchMailKeys.size;
  $('source-count').textContent=`인용 메일 ${cited.toLocaleString()}건 · 검색 메일 ${searched.toLocaleString()}건`;
  $('reference-info').textContent=`인용 메일 ${cited.toLocaleString()}건 · 검색 메일 ${searched.toLocaleString()}건 (현재 불러온 고유 메일 · 인용과 중복 제외)`;
}
function showSearchRows(rows,generation,limit=Infinity){
  let count=0;
  for(const item of rows||[]){
    const id=Number(item.id);
    if(!Number.isSafeInteger(id)||citedIds.has(id)||searchIds.has(id))continue;
    const key=uniqueMailKey(item);
    if(citedMailKeys.has(key)||searchMailKeys.has(key))continue;
    if(count>=limit)break;
    searchIds.add(id);searchMailKeys.add(key);referenceIds.add(id);
    addReferenceCard(item,'관련 검색',generation);
    count++;
  }
  return count;
}
async function searchReferences(reset=true){
  if(!selected||(referenceLoading&&!reset))return;
  const requested=selected;
  if(reset){
    referenceGeneration++;referenceOffset=0;referenceExpanded=false;
    referenceHasMore=false;referenceIds=new Set();citedIds=new Set();searchIds=new Set();
    citedMailKeys=new Set();searchMailKeys=new Set();referenceCounts();
    referenceSummaryQueue=[];clear($('right-content'));
    citedGroup=createReferenceGroup('AI 초안 인용 메일','AI가 초안에 근거로 표시한 메일');
    searchGroup=createReferenceGroup('관련 검색 메일','검색 유사도 상위 10건부터 표시');
  }
  const generation=referenceGeneration;
  referenceLoading=true;$('load-more').disabled=true;
  try{
    if(reset){
      const [cited,all]=await Promise.all([
        api(`/api/items/${requested}/references?mode=cited&offset=0&page_size=100`),
        api(`/api/items/${requested}/references?mode=all&offset=0&page_size=40`)
      ]);
      if(requested!==selected||generation!==referenceGeneration)return;
      const citedRows=cited.results||[];
      for(const mail of citedRows){
        const id=Number(mail.id);
        if(!Number.isSafeInteger(id)||citedIds.has(id))continue;
        const key=uniqueMailKey(mail);
        if(citedMailKeys.has(key))continue;
        citedIds.add(id);citedMailKeys.add(key);referenceIds.add(id);
        addReferenceCard(mail,'AI 인용',generation);
      }
      if(!citedIds.size)citedGroup.append(txt('p','초안에 인용된 참고 메일이 없습니다. 아래 관련 검색 메일을 확인하세요.','reference-group-empty'));
      // The API combines ranked vector and lexical candidates on the first page.
      showSearchRows(all.results||[],generation,10);
      if(!searchIds.size)searchGroup.append(txt('p','일치하는 검색 결과가 없습니다.','reference-group-empty'));
      referenceOffset=0;
      referenceHasMore=Boolean(all.has_more)||(all.results||[]).filter(x=>!citedIds.has(Number(x.id))).length>searchIds.size;
      referenceCounts();
      $('load-more').textContent='검색 전체 보기';
      $('load-more').classList.toggle('hidden',!referenceHasMore);
    }else{
      // Expand to the full first page, then advance through additional FTS pages.
      const firstExpansion=!referenceExpanded;
      const offset=firstExpansion?0:referenceOffset;
      const data=await api(`/api/items/${requested}/references?mode=all&offset=${offset}&page_size=40`);
      if(requested!==selected||generation!==referenceGeneration)return;
      if(firstExpansion){
        referenceExpanded=true;searchIds.clear();searchMailKeys.clear();
        clear(searchGroup);
        const head=txt('div','','reference-group-heading');
        head.append(txt('strong','관련 검색 메일'),txt('small','일치하는 검색 결과를 순서대로 추가 표시'));
        searchGroup.append(head);
      }
      showSearchRows(data.results||[],generation);
      referenceOffset=Number(data.next_offset||offset+40);
      referenceHasMore=Boolean(data.has_more);
      $('load-more').textContent='검색 결과 더 보기';
      $('load-more').classList.toggle('hidden',!referenceHasMore);
      if(!searchIds.size)searchGroup.append(txt('p','일치하는 검색 결과가 없습니다.','reference-group-empty'));
    }
    referenceCounts();
    $('tool-status').textContent=`메일 #${requested} · 인용 메일 ${citedMailKeys.size}건 · 검색 메일 ${searchMailKeys.size}건`;
  }catch(e){
    if(requested===selected&&generation===referenceGeneration)flash('RAG 검색 실패: '+e.message);
  }finally{
    if(generation===referenceGeneration){referenceLoading=false;$('load-more').disabled=false;}
  }
}
function draftBox(){
  const box=txt('section','','reply-draft-box');
  box.append(txt('div','AI 회신 초안','reply-draft-title'));
  const status=txt('div','AI 초안 생성 대기','reply-draft-status');
  const body=txt('div','메일 핵심 요약 분석 후 RAG 참고 메일을 바탕으로 회신 초안을 작성합니다.','reply-draft-body');
  const note=txt('div','자동 발송되지 않습니다. 근거 메일과 내용 확인 후 사용하세요.','reply-draft-note');
  const ratingPanel=txt('div','','feedback-reply-box');
  const ratingHeader=txt('div','AI 초안 평가 · 적합으로 승인한 원본 초안은 다음 회신의 참고 사례가 됩니다.','feedback-caption');
  const controls=txt('div','','feedback-controls');
  const good=txt('button','적합','feedback-good');
  const bad=txt('button','부적합','feedback-bad');
  good.type='button';bad.type='button';
  const currentRating=txt('span','','feedback-saved');
  function markReply(rating){
    good.classList.toggle('rated',rating==='good');bad.classList.toggle('rated',rating==='bad');
    currentRating.textContent=rating==='good'?'원본 초안 승인됨':rating==='bad'?'부적합 평가 저장됨':'';
  }
  async function rateReply(rating){
    if(!selected)return;
    const target=selected;
    good.disabled=bad.disabled=true;
    try{
      // No editable text is accepted from the browser; server stores the original draft.
      const res=await api(`/api/items/${target}/feedback`,{method:'POST',body:JSON.stringify({rating})});
      if(target===selected){markReply(res.rating);flash('초안 평가를 저장했습니다.');}
    }catch(e){flash('초안 평가 실패: '+e.message);}
    finally{good.disabled=bad.disabled=false;}
  }
  good.onclick=()=>rateReply('good');bad.onclick=()=>rateReply('bad');
  controls.append(good,bad,currentRating);ratingPanel.append(ratingHeader,controls);
  box.append(status,body,note,ratingPanel);
  return {box,setFeedback(feedback){
    markReply(feedback?.rating);
  },update(data){
    loadedDraftText=(data.status==='success'&&data.draft)?data.draft:'';
    $('copy').disabled=!loadedDraftText;
    status.textContent=(data.status==='success'?'AI 초안 생성 완료':data.status==='pending'?'AI 초안 생성 중':'AI 초안 생성 실패')+' · 모델: '+(data.model||'미설정');
    status.dataset.status=data.status||'pending';
    body.textContent=data.draft|| (data.error||'초안이 생성되지 않았습니다.');
    good.disabled=bad.disabled=data.status!=='success';
  }};
}
async function generateReply(id,widget){
  if(selected!==id)return;
  try{
    const r=await api(`/api/items/${id}/reply`,{method:'POST'});
    if(selected!==id)return;
    widget.update(r);
    await searchReferences(true);
  }catch(e){
    if(selected!==id)return;
    widget.update({status:'error',model:'',error:'초안 작성에 실패했습니다: '+e.message});
    await searchReferences(true);
  }finally{
    if(selected===id){referenceAutoReady=true;runReferenceSummaryQueue();}
  }
}
async function show(id){
  const requestGeneration=++showRequestGeneration;
  try{
    const d=await api('/api/items/'+id);
    if(requestGeneration!==showRequestGeneration)return;
    selected=id;current=d;referenceAutoReady=false;
    document.querySelectorAll('.item').forEach(el=>el.classList.toggle('active',Number(el.dataset.id)===id));
    const left=$('left-content');clear(left);left.className='';
    loadedDraftText='';$('copy').disabled=true;
    const summaryWidget=structuredSummaryBox(d.summary_structured,d.summary_status,d.summary_status_label,d.summary_model);
    const draftWidget=draftBox();
    left.append(summaryWidget.box,draftWidget.box);
    left.append(txt('div',normalizeText(d.subject||'(제목 없음)'),'mail-subject'));
    metadata(left,[['발신',d.sender],['수신',d.recipients],['참조',d.cc],['일시',formatMailDate(d.date_text)]]);
    left.append(txt('div',d.body_display??normalizeText(d.body||'(본문 없음)'),'mail-body'));
    updateSelectionUI();$('left-scroll').scrollTop=0;$('right-scroll').scrollTop=0;
    history.replaceState(null,'',`/?mail=${id}`);
    if(d.reply_suggestion?.status==='success')draftWidget.update(d.reply_suggestion);
    else draftWidget.update({status:'pending',model:d.reply_model_configured,draft:''});
    draftWidget.setFeedback(d.reply_feedback);
    await searchReferences(true);
    // Background AI processes newly arrived mail; GET/POST are cached fallbacks.
    if(d.body && String(d.body).trim()){
      let summaryDone=Promise.resolve();
      summaryDone=api(`/api/items/${id}/summary`).then(r=>{
        if(selected===id)summaryWidget.update(r.summary,r.status,r.status_label,r.model);
      }).catch(()=>{
        if(selected===id)summaryWidget.update(d.summary_structured,'fallback','AI 분석 실패 · 추출 요약',d.summary_model);
      });
      if(!d.reply_suggestion || d.reply_suggestion.status!=='success')summaryDone.then(()=>generateReply(id,draftWidget));
      else summaryDone.finally(()=>{if(selected===id){referenceAutoReady=true;runReferenceSummaryQueue();}});
    }else if(d.reply_suggestion?.status==='success'){
      referenceAutoReady=true;runReferenceSummaryQueue();
    }
  }catch(e){flash(e.message);}
}
async function refreshItems(){try{
  const items=await api('/api/items');$('count').textContent=items.length;
  latestItemIds=items.map(i=>Number(i.id));
  multiSelected=new Set([...multiSelected].filter(id=>latestItemIds.includes(id)));
  if(selectionAnchor!==null&&!latestItemIds.includes(selectionAnchor))selectionAnchor=null;
  const focusedId=document.activeElement?.classList?.contains('item')?Number(document.activeElement.dataset.id):null;
  const list=$('items');clear(list);
  if(!items.length)list.append(txt('p','아직 수집된 신규 메일이 없습니다.','empty'));
  for(const item of items){
    const b=txt('button','','item');b.type='button';b.dataset.id=item.id;
    b.setAttribute('aria-selected',String(multiSelected.has(Number(item.id))));
    b.append(txt('strong',normalizeText(item.subject||'(제목 없음)')),
      txt('small',`${normalizeText(item.sender||'')} · ${item.created_at||''}`));
    b.addEventListener('click',evt=>selectMailFromList(Number(item.id),evt));
    b.addEventListener('keydown',evt=>onMailKeydown(evt,Number(item.id)));
    list.append(b);
  }
  const urlId=Number(new URLSearchParams(location.search).get('mail'));
  if(!selected&&items.length){
    const id=items.some(x=>x.id===urlId)?urlId:items[0].id;
    if(!multiSelected.size){multiSelected.add(id);selectionAnchor=id;}
    await show(id);
  }
  if(selected&&!latestItemIds.includes(selected)){
    selected=null;current=null;
    if(items.length)await show(items[0].id);
    else{
      $('left-content').replaceChildren(txt('p','다른 메일을 선택하세요.','empty-stage'));
      $('right-content').replaceChildren(txt('p','관련 메일을 선택하세요.','empty-stage'));
      loadedDraftText='';$('copy').disabled=true;
    }
  }
  updateSelectionUI();
  if(focusedId!==null){
    const focusButton=Array.from(list.querySelectorAll('.item')).find(b=>Number(b.dataset.id)===focusedId);
    focusButton?.focus({preventScroll:true});
  }
}catch(e){flash(e.message);}}
async function getStatus(){try{
  const s=await api('/api/status'),stage=s.phase||'idle',step=Number(s.processed||0),total=Number(s.total||0);
  const pct=total?Math.min(100,Math.round(step/total*100)):0;
  const phases={idle:'대기',connecting:'메일 서버 연결 중',indexing:'메일 색인 중',retrying:'누락 메일 재색인 중',warning:'일부 폴더 수집 실패',complete:'최근 수집 완료',error:'메일 수집 오류'};
  $('scan-phase').textContent=s.busy?(s.stalled?'처리 지연 · 서버 응답 확인 필요':phases[stage]||'메일 처리 중'):(s.scan_outcome==='partial'?'일부 폴더 수집 실패':s.last_error?'메일 수집 오류':s.history_remaining?'과거 메일 단계적 색인 중':s.last_check?'수집 완료 · 다음 점검 대기':'로그인 후 수집 대기');
  $('scan-dot').className='scan-dot'+(s.stalled?' error':s.busy?' busy':s.last_error?' error':s.last_check?' ok':'');
  $('scan-detail').textContent=s.busy?`${s.folder||'메일함'} · ${step.toLocaleString()} / ${total.toLocaleString()}건 (${pct}%) · UID ${s.active_uid||'-'}`:(s.last_error?'오류: '+s.last_error:(s.history_remaining?`남은 과거 메일 약 ${Number(s.history_remaining).toLocaleString()}건 · 다음 점검에서 계속 수집`:`점검 주기 ${s.poll_seconds||300}초 · 자동 모니터링`));
  $('scan-progress-bar').style.width=(s.busy&&total?pct:s.last_check&&!s.last_error?100:0)+'%';
  $('scan-metrics').textContent=s.busy?`현재 단계 실패 ${s.failed||0}건 · 재시도 대기 ${s.retry_pending||0}건 · 마지막 활동 ${s.idle_seconds||0}초 전`:`마지막 완료: ${s.last_check||'없음'} · 재시도 대기 ${s.retry_pending||0}건`;
  $('state').textContent=s.configured?`${s.username} · ${s.busy?'작업 중':s.last_error?'오류':'대기'}`:'메일 계정 미설정';
  $('state-dot').classList.toggle('ok',s.configured&&!s.last_error);
  $('subtitle').textContent=s.last_error?'최근 연결 오류: '+s.last_error:(s.last_check?`마지막 확인 ${s.last_check}`:'송·수신 메일의 RAG 근거를 검색합니다.');
  if(!s.configured)$('login-overlay').classList.remove('hidden');
}catch(e){flash(e.message);}}
$('login-form').addEventListener('submit',async ev=>{ev.preventDefault();$('login-error').textContent='';$('login-button').disabled=true;
try{await api('/api/login',{method:'POST',body:JSON.stringify({username:$('username').value.trim(),password:$('password').value})});$('password').value='';$('login-overlay').classList.add('hidden');flash('메일 로그인 성공 · 색인 시작');await getStatus();await refreshItems();startPolling();}
catch(e){$('login-error').textContent=e.message;}finally{$('login-button').disabled=false;}});
$('account').onclick=async()=>{try{await api('/api/logout',{method:'POST'});location.href='/';}catch(e){flash(e.message);}};
$('copy').onclick=async()=>{
  if(!loadedDraftText)return;
  try{
    if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(loadedDraftText);}
    else{
      // Local HTTP test deployments may lack the secure clipboard API.
      const field=document.createElement('textarea');field.value=loadedDraftText;
      field.style.cssText='position:fixed;left:-9999px;top:0';
      document.body.appendChild(field);field.select();
      const success=document.execCommand('copy');field.remove();
      if(!success)throw Error('브라우저 복사 권한이 없습니다. HTTPS 환경에서 다시 시도하세요.');
    }
    flash('AI 회신 초안을 복사했습니다.');
  }catch(e){flash('복사 실패: '+e.message);}
};
$('delete').onclick=async()=>{
  const ids=[...multiSelected].filter(id=>latestItemIds.includes(id));
  if(!ids.length)return;
  if(!confirm(`선택한 ${ids.length}건을 처리 목록에서 삭제하시겠습니까?\n메일 서버 원본은 삭제되지 않습니다.`))return;
  $('delete').disabled=true;
  try{
    const result=await api('/api/items/bulk-delete',{method:'POST',body:JSON.stringify({ids})});
    const deleted=new Set(result.deleted_ids||[]);
    for(const id of deleted)multiSelected.delete(id);
    if(deleted.has(selected)){
      showRequestGeneration++;selected=null;current=null;loadedDraftText='';$('copy').disabled=true;
      referenceGeneration++;referenceSummaryQueue=[];referenceSummaryCache.clear();
      history.replaceState(null,'','/');
    }
    await refreshItems();
    flash(`${result.deleted_count}건을 처리 목록에서 삭제했습니다.`);
  }catch(e){flash('선택 삭제 실패: '+e.message);updateSelectionUI();}
};
$('refresh').onclick=async()=>{try{const r=await api('/api/scan',{method:'POST'});flash(r.ok?'메일 확인을 시작했습니다.':'이미 작업 중입니다.');}catch(e){flash(e.message);}};
$('load-more').onclick=()=>searchReferences(false);

function startPolling(){if(pollingStarted)return;pollingStarted=true;setInterval(async()=>{try{const s=await api('/api/session');if(!s.authenticated){location.reload();return;}await getStatus();await refreshItems();}catch(e){flash(e.message);}},10000);}
(async()=>{try{const s=await api('/api/session');if(!s.authenticated){$('login-overlay').classList.remove('hidden');return;}await getStatus();await refreshItems();startPolling();}catch(e){flash(e.message);}})();

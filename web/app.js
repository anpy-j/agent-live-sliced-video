const $ = (selector, root=document) => root.querySelector(selector);
const $$ = (selector, root=document) => [...root.querySelectorAll(selector)];
const app = $('#app');
const state = { dashboard:null, job:null, poll:null, mcp:null, selectedStage:null };
const labels = {queued:'排队中',running:'执行中',waiting_input:'待恢复',review_required:'需人工复核',blocked:'已阻塞',completed:'已完成',failed:'自动恢复中',cancelled:'已取消',pending:'等待',succeeded:'完成'};
const stageLabels = {material_index:'素材索引',edit_plan:'AI 文本编排',validation:'文本校验与原声锁定',visual_mix:'多模态画面混剪',delivery:'一次高清渲染'};
const icons = {
  video:'<svg viewBox="0 0 24 24"><rect x="3" y="5" width="14" height="14" rx="2"/><path d="m17 10 4-2v8l-4-2z"/></svg>',
  file:'<svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h5M9 13h6M9 17h6"/></svg>',
  image:'<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m4 17 5-5 4 4 2-2 5 4"/></svg>',
  folder:'<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h9a1 1 0 0 1 1 1v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  empty:'<svg viewBox="0 0 24 24"><path d="M4 7h16v12H4zM8 4h8v3"/><path d="M9 12h6"/></svg>',
  arrow:'<svg viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></svg>'
};

async function api(url, options={}) {
  const response = await fetch(url, {headers:{'Content-Type':'application/json',...(options.headers||{})}, ...options});
  const data = await response.json().catch(()=>({}));
  if (!response.ok) throw new Error(data.error || `请求失败 ${response.status}`);
  return data;
}
function escapeHtml(value=''){return String(value).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
function formatTime(value){if(!value)return '—';const d=new Date(value);return new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(d);}
function formatExactTime(value){if(!value)return '—';const d=new Date(value);return new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(d);}
function durationText(seconds=0){seconds=Math.max(0,Math.floor(seconds));const h=Math.floor(seconds/3600),m=Math.floor(seconds%3600/60),s=seconds%60;return h?`${h} 小时 ${m} 分`:m?`${m} 分 ${s} 秒`:`${s} 秒`;}
function bytes(value=0){if(value<1024)return `${value} B`;if(value<1048576)return `${(value/1024).toFixed(1)} KB`;return `${(value/1048576).toFixed(1)} MB`;}
function clipTime(value=0){const seconds=Math.max(0,Number(value)||0),m=Math.floor(seconds/60),s=Math.floor(seconds%60);return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;}
function visualStatus(value){return value==='failed'?'running':value;}
function status(value){return `<span class="status ${visualStatus(value)}">${labels[value]||value}</span>`;}
function toast(message){const el=$('#toast');el.textContent=message;el.classList.add('show');clearTimeout(el.timer);el.timer=setTimeout(()=>el.classList.remove('show'),2400);}
function loading(){app.innerHTML='<div class="loading"><div class="spinner"></div>正在读取本地任务状态</div>';}
function setCrumb(text){$('#pageCrumb').textContent=text;$$('[data-nav]').forEach(x=>{const active=location.hash.includes(x.dataset.nav);x.classList.toggle('active',active);if(active)x.setAttribute('aria-current','page');else x.removeAttribute('aria-current')});}

function jobRows(jobs){
  if(!jobs.length)return `<div class="empty">${icons.empty}<h3>还没有剪辑任务</h3><p>添加第一段直播素材，流程节点、日志和产物会在这里持续更新。</p><button class="button primary" data-new-job>新建剪辑任务</button></div>`;
  return `<table class="jobs-table"><thead><tr><th>任务</th><th>当前节点</th><th>进度</th><th>状态</th><th>操作</th></tr></thead><tbody>${jobs.map(j=>`<tr>\n    <td><div class="job-name"><span class="job-thumb">${icons.video}</span><div><b>${escapeHtml(j.title)}</b><small>${escapeHtml(j.source_path)}</small></div></div></td>\n    <td><small>${escapeHtml(stageLabels[j.current_stage]||j.current_stage||"—")}</small></td>\n    <td><div class="progress"><div class="progress-line"><i style="width:${Math.max(2,j.progress||0)}%"></i></div><small>${Math.round(j.progress||0)}% · ${formatTime(j.updated_at)}</small></div></td>\n    <td>${status(j.status)}</td>\n    <td><div class="job-row-actions">\n      <button class="link-button" data-open-job="${j.id}">详情</button>\n      ${['completed','review_required'].includes(j.status)?`<button class="link-button" data-open-folder="${j.id}">打开文件夹</button>`:''}\n      <button class="link-button" data-restart-job="${j.id}" data-job-title="${escapeHtml(j.title)}">重新开始</button>\n      <button class="link-button text-danger" data-delete-job="${j.id}" data-job-title="${escapeHtml(j.title)}">删除</button>\n    </div></td></tr>`).join('')}</tbody></table>`;
}

async function renderDashboard(){
  setCrumb('任务总览');loading();const data=await api('/api/dashboard');state.dashboard=data;$('#queueBadge').textContent=data.active;
  const running=data.counts.running||0, waiting=data.counts.waiting_input||0, done=data.counts.completed||0;
  app.innerHTML=`<div class="hero"><div><span class="eyebrow">LIVE PRODUCTION CONTROL</span><h1>生产控制台</h1><p>统一管理素材索引、智能编排、质量校验、画面混剪与成片交付。</p></div><button class="button primary" data-new-job>${icons.video}添加直播素材</button></div>
  <div class="stat-grid"><div class="stat-card"><span>队列任务</span><strong>${data.active}</strong><small>等待或正在执行</small></div><div class="stat-card"><span>执行中</span><strong>${running}</strong><small>本地工作进程</small></div><div class="stat-card"><span>需要决策</span><strong>${waiting}</strong><small>等待外部 Agent</small></div><div class="stat-card"><span>已完成</span><strong>${done}</strong><small>可查看与播放</small></div></div>
  <div class="panel"><div class="panel-head"><div><h2>最近任务</h2><p>按最后创建时间排列</p></div><a class="link-button" href="#/queue">查看全部</a></div>${jobRows(data.jobs.slice(0,8))}</div>`;
  bindCommon();
}

async function renderQueue(){setCrumb('剪辑队列');loading();const data=await api('/api/jobs');app.innerHTML=`<div class="hero"><div><span class="eyebrow">PRODUCTION QUEUE</span><h1>剪辑队列</h1><p>重型任务串行调度，实时展示节点状态、进度和异常。</p></div><button class="button primary" data-new-job>添加任务</button></div><div class="panel"><div class="panel-head"><div><h2>全部任务</h2><p>${data.jobs.length} 个任务</p></div></div>${jobRows(data.jobs)}</div>`;bindCommon();}

function artifactCard(a){
  const url=`/api/artifacts/${a.id}/content`;let visual=icons.file;
  if((a.mime_type||'').startsWith('image/'))visual=`<img loading="lazy" src="${url}" alt="${escapeHtml(a.title)}">`;
  if((a.mime_type||'').startsWith('video/'))visual=`<video preload="metadata" src="${url}#t=0.1" aria-label="${escapeHtml(a.title)}"></video>`;
  return `<button class="artifact" data-preview="${a.id}" data-mime="${escapeHtml(a.mime_type||'')}" data-title="${escapeHtml(a.title)}"><span class="artifact-preview">${visual}</span><span class="artifact-meta"><b>${escapeHtml(a.title)}</b><small>${escapeHtml(a.kind)} · ${bytes(a.size)}</small></span></button>`;
}
function inferredProduct(title=''){
  const bracket=String(title).match(/【([^】]+)】/);if(bracket)return bracket[1];
  return String(title).replace(/\.[^.]+$/,'').replace(/[-_]?\d+(?:[-_]\d+)*$/,'').trim();
}
function candidateRole(category){return ({hook:'hook',result:'result',color:'color',craft:'craft',material:'material',fit:'fit',styling:'styling',scene:'scene',demo:'demo',close:'close',pain:'pain',proof:'proof',personality:'personality',story:'story',reaction:'reaction',visual:'visual'}[category]||'bridge');}
function categoryLabel(category){return ({hook:'开场',result:'效果',color:'颜色',craft:'工艺',material:'面料',fit:'版型',styling:'搭配',scene:'场景',demo:'展示',close:'收尾',pain:'痛点',proof:'佐证',personality:'人设',story:'故事',reaction:'反应',visual:'动作',other:'讲解'}[category]||category||'讲解');}
function providerLabel(id){return ({workbuddy:'WorkBuddy',antigravity:'Antigravity',codex:'Codex',opencode:'OpenCode',multica:'Multica',manual:'手动'}[id]||id||'手动');}
let modelPickerSequence=0;
function modelPickerGroups(providers,capability='text'){
  const groups=[];
  (providers||[]).forEach(provider=>{
    const models=capability==='vision'?(provider.vision_models||[]):(provider.models||[]);if(!models.length)return;
    if(provider.id!=='opencode')groups.push({label:provider.name,disabled:!provider.available,items:models.map(model=>({value:`${provider.id}:${model.id}`,label:`${model.name}${model.id==='auto'?' · 推荐':''}`,search:`${provider.id} ${provider.name} ${model.id} ${model.name}`}))});
    else {
      const split=new Map();
      models.forEach(model=>{const group=model.id==='auto'?'默认配置':model.id.split('/',1)[0];if(!split.has(group))split.set(group,[]);split.get(group).push({value:`opencode:${model.id}`,label:`${model.name}${model.id==='auto'?' · 推荐':''}`,search:`opencode ${group} ${model.id} ${model.name}`})});
      split.forEach((items,group)=>groups.push({label:`OpenCode · ${group==='默认配置'?group:group.toUpperCase()}`,disabled:!provider.available,items}));
    }
  });
  return groups;
}
function modelPickerHtml(providers,selected='workbuddy:auto',{name='',id='',capability='text'}={}){
  const listId=`modelPickerList${++modelPickerSequence}`,groups=modelPickerGroups(providers,capability),items=groups.flatMap(group=>group.items.map(item=>({...item,group:group.label,disabled:group.disabled}))),chosen=items.find(item=>item.value===selected&&!item.disabled)||items.find(item=>!item.disabled)||{value:'',label:'没有可用模型',group:''};
  return `<div class="model-picker" data-model-picker><input type="hidden" data-model-value ${name?`name="${escapeHtml(name)}"`:''} ${id?`id="${escapeHtml(id)}"`:''} value="${escapeHtml(chosen.value)}"><div class="model-picker-control"><input class="model-picker-search" type="search" role="combobox" aria-label="搜索并选择 AI 模型" aria-autocomplete="list" aria-expanded="false" aria-controls="${listId}" autocomplete="off" spellcheck="false" value="${escapeHtml(`${chosen.group} · ${chosen.label}`)}" placeholder="输入提供商或模型名称"><button class="model-picker-toggle" type="button" aria-label="展开模型列表" tabindex="-1"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg></button></div><div class="model-picker-menu" id="${listId}" role="listbox" hidden>${groups.map(group=>`<section class="model-picker-group" data-model-group><b>${escapeHtml(group.label)}${group.disabled?' · 不可用':''}</b>${group.items.map(item=>`<button type="button" role="option" data-model-option data-value="${escapeHtml(item.value)}" data-label="${escapeHtml(`${group.label} · ${item.label}`)}" data-search="${escapeHtml(item.search.toLowerCase())}" aria-selected="${item.value===chosen.value?'true':'false'}" ${group.disabled?'disabled':''}>${escapeHtml(item.label)}</button>`).join('')}</section>`).join('')}<p class="model-picker-empty" hidden>没有匹配的模型</p></div></div>`;
}
function bindModelPickers(root=document){
  $$('[data-model-picker]',root).forEach(picker=>{
    if(picker.dataset.bound)return;picker.dataset.bound='true';
    const input=$('.model-picker-search',picker),hidden=$('[data-model-value]',picker),menu=$('.model-picker-menu',picker),toggle=$('.model-picker-toggle',picker),empty=$('.model-picker-empty',picker);
    const options=()=>$$('[data-model-option]',menu).filter(option=>!option.hidden&&!option.disabled);
    const filter=query=>{const terms=query.toLowerCase().trim().split(/\s+/).filter(Boolean);let count=0;$$('[data-model-group]',menu).forEach(group=>{let groupCount=0;$$('[data-model-option]',group).forEach(option=>{const match=!terms.length||terms.every(term=>(`${option.dataset.search} ${option.dataset.label.toLowerCase()}`).includes(term));option.hidden=!match;if(match){groupCount++;count++}});group.hidden=!groupCount});empty.hidden=count!==0;activate(options()[0])};
    const open=()=>{menu.hidden=false;input.setAttribute('aria-expanded','true');const rect=picker.getBoundingClientRect(),height=Math.min(menu.scrollHeight,340);menu.classList.toggle('opens-up',rect.bottom+8+height>window.innerHeight&&rect.top-height-16>0);filter(input.value===input.dataset.selectedLabel?'':input.value)};
    const close=(restore=true)=>{menu.hidden=true;input.setAttribute('aria-expanded','false');input.removeAttribute('aria-activedescendant');if(restore)input.value=input.dataset.selectedLabel||'';$$('.active',menu).forEach(x=>x.classList.remove('active'))};
    const activate=option=>{$$('.active',menu).forEach(x=>x.classList.remove('active'));if(option){option.classList.add('active');option.scrollIntoView({block:'nearest'});input.setAttribute('aria-activedescendant',option.id||(option.id=`${menu.id}Option${[...menu.querySelectorAll('[data-model-option]')].indexOf(option)}`))}};
    const choose=option=>{if(!option||option.disabled)return;hidden.value=option.dataset.value;input.dataset.selectedLabel=option.dataset.label;input.value=option.dataset.label;$$('[data-model-option]',menu).forEach(x=>x.setAttribute('aria-selected',String(x===option)));hidden.dispatchEvent(new Event('change',{bubbles:true}));close(false)};
    const selected=$(`[data-model-option][data-value="${CSS.escape(hidden.value)}"]`,menu);input.dataset.selectedLabel=selected?.dataset.label||input.value;
    input.addEventListener('focus',()=>{open();input.select()});
    input.addEventListener('input',()=>{open();filter(input.value)});
    input.addEventListener('keydown',event=>{const visible=options(),active=$('.active',menu);if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();open();const index=visible.indexOf(active),next=event.key==='ArrowDown'?Math.min(visible.length-1,index+1):Math.max(0,index<0?visible.length-1:index-1);activate(visible[next])}else if(event.key==='Enter'&&!menu.hidden){event.preventDefault();choose(active||visible[0])}else if(event.key==='Escape'){event.preventDefault();close()}});
    input.addEventListener('blur',()=>setTimeout(()=>{if(!picker.contains(document.activeElement))close()},0));
    toggle.addEventListener('mousedown',event=>event.preventDefault());
    toggle.addEventListener('click',()=>{if(menu.hidden){input.focus();open()}else close()});
    menu.addEventListener('mousedown',event=>event.preventDefault());
    menu.addEventListener('click',event=>choose(event.target.closest('[data-model-option]')));
  });
}
function decisionPanel(job,packet,ai){
  const candidates=packet.candidate_digest||[];
  const limits=packet.editing_constraints||{},constraintText=limits.min_total?`成片 ${limits.min_total}–${limits.max_total} 秒 · ${limits.min_segments}–${limits.max_segments} 段 · 单段 1.2–5.0 秒 · 自然连续表达不超过 10 秒`:'单段 1.2–5.0 秒 · 自然连续表达不超过 10 秒';
  const providers=ai?.providers||[], selected=job.model_provider&&job.model_provider!=='manual'?`${job.model_provider}:${job.model_name||'auto'}`:(ai?.default||'workbuddy:auto'), available=providers.some(x=>x.available);
  const overview=(packet.artifacts||[]).find(a=>(a.mime_type||'').startsWith('image/')&&(a.title||'').includes('概览'));
  const prompt=`调用 live-slicer 的 get_stage_packet 读取任务 ${job.id}，按照任务包中的约束选择一个最强成片方案，并用 submit_stage_payload 提交 main_product 和 picks。每个 pick 同时标注 product 和 color；强开头可用 hook_A，没有合适钩子时全部使用 body；不要重复使用同一画面，不要新建任务。`;
  return `<section class="decision-panel" aria-labelledby="decisionTitle"><div class="decision-head"><div><span class="eyebrow">ACTION REQUIRED</span><h2 id="decisionTitle">需要完成音画编排</h2><p>素材分析已经完成。请选择一种方式提交方案，提交后流程才会继续。</p></div><span class="status waiting_input">等待你的决策</span></div>
    <div class="ai-orchestrator"><div><b>让 LiveCut 调用 AI</b><p>可输入提供商或模型关键词快速筛选。编排提交前会执行硬预检，不合格方案不会进入切片引擎。</p><small class="constraint-line">${escapeHtml(constraintText)}</small></div><label>提供方与模型${modelPickerHtml(providers,selected,{id:'aiPlanModel'})}</label><button class="button primary" id="startAiPlan" ${available?'':'disabled'}>${available?'开始 AI 编排':'没有可用的 AI CLI'}</button></div>
    <details class="external-agent"><summary>由外部 Agent 通过 MCP 提交</summary><div class="agent-handoff"><div><p>如果你希望 WorkBuddy、Codex、OpenCode 或 Multica 自己主导，也可以在对应客户端发送下面的指令。</p><code>${escapeHtml(prompt)}</code></div><div class="handoff-actions"><button class="button ghost" id="copyAgentPrompt">复制执行指令</button><a class="button ghost" href="#/mcp">查看 MCP 接入</a></div></div></details>
    <details class="manual-decision" open><summary><span><b>或在这里手动决定</b><small>适合你想亲自选开头和正文时使用</small></span><span class="selection-summary" id="selectionSummary">已选 0 段 · 0 秒</span></summary>
      <form id="editPlanForm"><div class="plan-toolbar"><label>主推款名称<input id="mainProduct" value="${escapeHtml(inferredProduct(job.title))}" required placeholder="例如：白山茶羊毛上衣"><small>用于字幕、文件记录和后续质检。</small></label><div class="plan-help"><b>怎么选</b><span>${escapeHtml(constraintText)}。同一画面只使用一次；连续时间点需要穿插其他片段形成真实切点。</span></div></div>
      ${overview?`<button class="overview-link" type="button" data-preview="${overview.id}" data-mime="${escapeHtml(overview.mime_type||'')}" data-title="${escapeHtml(overview.title)}"><img src="/api/artifacts/${overview.id}/content" alt="全场素材概览"><span>打开全场概览大图</span></button>`:''}
      <div class="candidate-list" aria-label="候选片段">${candidates.map(c=>`<label class="candidate-row"><input type="checkbox" data-candidate="${c.i}"><span class="candidate-time">${clipTime(c.s)}–${clipTime(c.e)}</span><span class="candidate-copy"><span class="candidate-tag ${escapeHtml(c.c)}">${escapeHtml(categoryLabel(c.c))}</span><span>${escapeHtml(c.t)}</span></span><select data-module="${c.i}" aria-label="片段用途" disabled><option value="${c.c==='hook'?'hook_A':'body'}">${c.c==='hook'?'开头':'正文'}</option><option value="${c.c==='hook'?'body':'hook_A'}">${c.c==='hook'?'正文':'开头'}</option></select></label>`).join('')}</div>
      <div class="plan-submit"><p class="form-error" id="editPlanError" role="alert"></p><button class="button primary" type="submit" id="submitEditPlan">提交编排并继续</button></div></form></details></section>`;
}
function jobFlags(job){return {active:['queued','running','failed'].includes(job.status),canRetry:['cancelled','review_required','blocked'].includes(job.status),canPlan:job.status==='waiting_input'&&job.current_stage==='edit_plan'};}
function jobControlsHtml(job){const {active,canRetry}=jobFlags(job),retryLabel=job.status==='review_required'?'修复后重新执行':job.current_stage==='validation'&&String(job.error||'').includes('校验未通过')?'退回 AI 重新编排':'重新排队';const folder=job.deliverables||{},showFolder=['completed','review_required'].includes(job.status)&&folder.exists;return `${status(job.status)}${showFolder?`${folder.folder?`<span class="folder-path" title="${escapeHtml(folder.folder)}">${escapeHtml(folder.folder)}</span>`:''}<button class="button ghost small" id="openDeliverableFolder">${icons.folder}打开成片文件夹</button>`:''}${canRetry?`<button class="button ghost small" id="retryJob">${retryLabel}</button>`:''}<button class="button ghost small" id="restartJob">重新开始</button>${active?'<button class="button danger small" id="cancelJob">取消任务</button>':''}<button class="button danger small" id="deleteJob">删除任务</button>`;}
function deliverablesControlsKey(job){return JSON.stringify([job.status,job.deliverables&&job.deliverables.exists,job.deliverables&&job.deliverables.folder]);}
function jobActionsHtml(job,packet,ai){
  const {canPlan}=jobFlags(job);
  let feedbackHtml='';
  if(job.status==='completed'){
    feedbackHtml=`<section class="decision-panel" aria-labelledby="feedbackTitle"><div class="decision-head"><div><span class="eyebrow">LEARNING CONTROL</span><h2 id="feedbackTitle">成片反馈与规则升级</h2><p>记录本次成片问题，并沉淀为可启停、可回滚的剪辑规则。</p></div><span class="status succeeded">已出片</span></div>
      <form id="jobFeedbackForm" class="feedback-form">
        <div class="form-grid"><label>问题类型<select id="feedbackProblem"><option value="content">内容结构</option><option value="duplicate">重复内容</option><option value="visual">画面替换</option><option value="render">导出渲染</option><option value="general">其他</option></select></label><label>作用域<select id="feedbackScope"><option value="text">文本编排</option><option value="vision">视觉混剪</option><option value="render">渲染交付</option></select></label></div>
        <div class="form-grid"><label>时间点（可选）<input id="feedbackTimestamp" placeholder="例如 00:32"></label><label>片段（可选）<input id="feedbackSegment" placeholder="例如 第 5 段"></label><label>商品（可选）<input id="feedbackProduct" placeholder="例如 阔腿裤"></label></div>
        <label><b>提出本次剪辑问题（提交后沉淀为新规则）</b>
          <textarea id="jobFeedbackText" rows="2" required placeholder="例如：上衣和裤子穿插了，请严格按商品集中讲；或者：讲面料的内容还是偏多，请进一步精炼"></textarea>
        </label>
        <div class="feedback-actions">
          <button class="button primary" type="submit" id="submitFeedbackBtn">提交问题并升级 Agent</button>
          <span id="feedbackNotice"></span>
        </div>
      </form></section>`;
  }
  return `${job.error?`<div class="danger-box"><b>自动恢复记录：</b> ${escapeHtml(job.error)}</div>`:''}${canPlan?decisionPanel(job,packet,ai):''}${feedbackHtml}`;
}
function selectedStageId(job){return job.stages.some(s=>s.stage_id===state.selectedStage)?state.selectedStage:(job.current_stage||job.stages[0]?.stage_id);}
function workflowHtml(job){const selected=selectedStageId(job);return job.stages.map((s,i)=>`<button type="button" class="stage ${visualStatus(s.status)} ${s.stage_id===selected?'selected':''}" data-stage-select="${escapeHtml(s.stage_id)}" aria-pressed="${s.stage_id===selected}"><span class="stage-dot">${s.status==='succeeded'?'✓':String(i+1).padStart(2,'0')}</span><b>${escapeHtml(s.name)}</b><small>${labels[s.status]||s.status}</small></button>`).join('');}
function payloadHtml(payload){if(!payload)return '';const value=JSON.stringify(payload,null,2);return `<details class="event-data"><summary>查看执行数据</summary><pre>${escapeHtml(value.length>6000?`${value.slice(0,6000)}\n……`:value)}</pre></details>`;}
function stageDetailHtml(job){
  const id=selectedStageId(job),stage=job.stages.find(s=>s.stage_id===id)||job.stages[0],events=job.events.filter(e=>e.stage_id===id),artifacts=job.artifacts.filter(a=>a.stage_id===id),isCurrent=job.current_stage===id&&['queued','running'].includes(job.status),runtime=job.runtime||{};
  const runState=isCurrent?(runtime.process_active?'本地子进程正在执行':runtime.worker_alive?'工作进程正在处理':'后台服务未运行'):(labels[stage.status]||stage.status);
  return `<div class="stage-detail-head"><div><span class="eyebrow">NODE ${String(job.stages.indexOf(stage)+1).padStart(2,'0')}</span><h2>${escapeHtml(stage.name)}</h2><p>${escapeHtml(stage.error||stage.message||'等待上游节点完成')}</p></div><span class="runtime-state ${isCurrent&&runtime.worker_alive?'live':''}"><i></i>${escapeHtml(runState)}</span></div>
  <div class="stage-metrics"><div><span>开始时间</span><b>${formatExactTime(stage.started_at)}</b></div><div><span>运行耗时</span><b data-elapsed-from="${escapeHtml(stage.started_at||'')}" data-elapsed-to="${escapeHtml(stage.finished_at||'')}">${stage.started_at?durationText((new Date(stage.finished_at||Date.now())-new Date(stage.started_at))/1000):'—'}</b></div><div><span>最后心跳</span><b data-relative-time="${escapeHtml(job.updated_at||'')}">刚刚</b></div><div><span>节点进度</span><b>${Math.round((stage.progress||0)*100)}%</b></div></div>
  ${isCurrent&&!runtime.worker_alive?'<div class="service-warning"><b>后台服务已停止</b><span>这不是正常等待；重启 LiveCut 后任务会恢复进队。</span></div>':''}
  <div class="node-section"><div class="node-section-title"><b>该节点执行记录</b><span>${events.length} 条</span></div><div class="node-events">${events.length?events.map(e=>`<article class="node-event ${e.level}"><span class="event-mark"></span><div><time>${formatExactTime(e.created_at)}</time><p>${escapeHtml(e.message)}</p>${payloadHtml(e.payload)}</div></article>`).join(''):'<p class="muted-empty">还没有执行记录。</p>'}</div></div>
  <div class="node-section"><div class="node-section-title"><b>该节点产物</b><span>${artifacts.length} 个</span></div>${artifacts.length?`<div class="node-artifacts">${artifacts.map(artifactCard).join('')}</div>`:'<p class="muted-empty">节点完成后，日志、报告或视频会出现在这里。</p>'}</div>`;
}
function stageDetailKey(job){const stage=job.stages.find(s=>s.stage_id===selectedStageId(job));return JSON.stringify([selectedStageId(job),job.status,job.current_stage,job.runtime,stage,job.events.filter(x=>x.stage_id===selectedStageId(job)).map(x=>x.id),job.artifacts.filter(x=>x.stage_id===selectedStageId(job)).map(x=>[x.id,x.size])]);}
function artifactsHtml(job){return job.artifacts.length?`<div class="artifacts">${job.artifacts.map(artifactCard).join('')}</div>`:`<div class="empty">${icons.empty}<h3>暂无产物</h3><p>节点完成后会自动登记产物。</p></div>`;}
function eventsHtml(job){return job.events.map(e=>`<div class="event ${e.level}"><time>${formatTime(e.created_at)} · ${escapeHtml(e.stage_id||'任务')}</time><p>${escapeHtml(e.message)}</p></div>`).join('')||'<div class="empty">暂无事件</div>';}
function patchJobRegion(selector,html,key){const region=$(selector);if(!region||region.dataset.renderKey===key)return false;region.innerHTML=html;region.dataset.renderKey=key;return true;}
function markJobDisconnected(){const indicator=$('#jobConnectionState');if(indicator){indicator.classList.add('offline');indicator.textContent='连接已中断 · 正在重试'}const runtime=$('.runtime-state.live');if(runtime){runtime.classList.remove('live');runtime.innerHTML='<i></i>无法连接后台服务';}}
function scheduleJobPoll(jobId,job){clearTimeout(state.poll);if(jobFlags(job).active)state.poll=setTimeout(()=>{if(location.hash===`#/jobs/${jobId}`)refreshJob(jobId).catch(()=>{markJobDisconnected();scheduleJobPoll(jobId,state.job||job)})},1800);}
function bindArtifactPreviews(root=document){$$('[data-preview]',root).forEach(x=>{if(x.dataset.previewBound)return;x.dataset.previewBound='true';x.addEventListener('click',()=>previewArtifact(x.dataset.preview,x.dataset.mime,x.dataset.title))});}
function bindJobControls(jobId){
  $('#openDeliverableFolder')?.addEventListener('click',async()=>{
    try{const res=await api(`/api/jobs/${jobId}/open-folder`,{method:'POST',body:'{}'});toast('已打开成片文件夹');}
    catch(err){toast(err.message);}
  });
  $('#cancelJob')?.addEventListener('click',async()=>{await api(`/api/jobs/${jobId}/cancel`,{method:'POST',body:'{}'});toast('任务已取消');await refreshJob(jobId)});
  $('#retryJob')?.addEventListener('click',async e=>{const returned=e.currentTarget.textContent.includes('重新编排');await api(`/api/jobs/${jobId}/retry`,{method:'POST',body:'{}'});toast(returned?'旧方案已退回编排节点':'任务已重新排队');await refreshJob(jobId)});
  $('#restartJob')?.addEventListener('click',async()=>{
    if(!confirm('确定要从头重新开始该任务吗？所有阶段执行进度和产物将被重置。'))return;
    try{
      await api(`/api/jobs/${jobId}/restart`,{method:'POST',body:'{}'});
      toast('任务已重新开始');
      await refreshJob(jobId);
    }catch(err){toast(err.message);}
  });
  $('#deleteJob')?.addEventListener('click',async()=>{
    if(!confirm('确定要彻底删除该任务吗？此操作将删除该任务的全部执行数据与产物，且不可恢复。'))return;
    try{
      await api(`/api/jobs/${jobId}/delete`,{method:'POST',body:'{}'});
      toast('任务已删除');
      location.hash='#/queue';
    }catch(err){toast(err.message);}
  });
}
function bindStageSelection(jobId){$$('[data-stage-select]').forEach(button=>button.addEventListener('click',()=>{state.selectedStage=button.dataset.stageSelect;patchJobRegion('#jobWorkflow',workflowHtml(state.job),`selected:${state.selectedStage}:${JSON.stringify(state.job.stages.map(x=>[x.stage_id,x.status]))}`);patchJobRegion('#jobStageDetail',stageDetailHtml(state.job),stageDetailKey(state.job));bindStageSelection(jobId);bindArtifactPreviews($('#jobStageDetail'));updateLiveTimes()}));}
function updateLiveTimes(){
  $$('[data-elapsed-from]').forEach(el=>{if(!el.dataset.elapsedFrom)return;const end=el.dataset.elapsedTo?new Date(el.dataset.elapsedTo):new Date();el.textContent=durationText((end-new Date(el.dataset.elapsedFrom))/1000)});
  $$('[data-relative-time]').forEach(el=>{if(!el.dataset.relativeTime)return;const seconds=Math.max(0,Math.floor((Date.now()-new Date(el.dataset.relativeTime))/1000));el.textContent=seconds<5?'刚刚':`${durationText(seconds)}前`});
}
function bindJobActions(jobId,job,packet){
  const fbForm=$('#jobFeedbackForm');
  if(fbForm){
    fbForm.addEventListener('submit',async e=>{
      e.preventDefault();
      const text=$('#jobFeedbackText').value.trim(),btn=$('#submitFeedbackBtn'),notice=$('#feedbackNotice');
      if(!text)return;
      btn.disabled=true;btn.textContent='正在沉淀规则…';
      try{
        const res=await api(`/api/jobs/${jobId}/feedback`,{method:'POST',body:JSON.stringify({feedback:text,upgrade:true,problem_type:$('#feedbackProblem').value,scope:$('#feedbackScope').value,kind:'soft',timestamp:$('#feedbackTimestamp').value,segment:$('#feedbackSegment').value,product:$('#feedbackProduct').value})});
        toast(res.message||'Agent 规则库已升级！');
        if(notice){notice.style.color='#4ade80';notice.textContent=res.message||'已成功升级！';}
        $('#jobFeedbackText').value='';
      }catch(err){
        if(notice){notice.style.color='#ef4444';notice.textContent=err.message;}
      }finally{
        btn.disabled=false;btn.textContent='提交问题并升级 Agent';
      }
    });
  }
  if(!jobFlags(job).canPlan)return;
  bindModelPickers($('#jobActions'));
  const candidates=new Map((packet.candidate_digest||[]).map(c=>[String(c.i),c]));
  const updateSelection=()=>{let count=0,duration=0;$$('[data-candidate]:checked').forEach(input=>{const c=candidates.get(input.dataset.candidate);count+=1;duration+=Number(c.e)-Number(c.s)});$('#selectionSummary').textContent=`已选 ${count} 段 · ${Math.round(duration)} 秒`;};
  $$('[data-candidate]').forEach(input=>input.addEventListener('change',()=>{const select=$(`[data-module="${input.dataset.candidate}"]`);select.disabled=!input.checked;input.closest('.candidate-row').classList.toggle('selected',input.checked);updateSelection()}));
  $('#startAiPlan').addEventListener('click',async e=>{const button=e.currentTarget,selection=$('#aiPlanModel').value;button.disabled=true;button.textContent='正在加入队列…';try{await api(`/api/jobs/${jobId}/ai-plan`,{method:'POST',body:JSON.stringify({ai_model:selection})});toast(`LiveCut 已开始调用 ${providerLabel(selection.split(':')[0])} AI`);await refreshJob(jobId)}catch(err){toast(err.message);button.disabled=false;button.textContent='开始 AI 编排'}});
  $('#copyAgentPrompt').addEventListener('click',async()=>{const text=$('.agent-handoff code').textContent;try{await navigator.clipboard.writeText(text);toast('执行指令已复制')}catch(_){toast('复制失败，请手动选择文字复制')}});
  $('#editPlanForm').addEventListener('submit',async e=>{e.preventDefault();const error=$('#editPlanError'),button=$('#submitEditPlan');error.textContent='';const mainProduct=$('#mainProduct').value.trim();const picks=$$('[data-candidate]:checked').map(input=>{const c=candidates.get(input.dataset.candidate);return {src:1,start:Number(c.s),end:Number(c.e),text:c.t,role:candidateRole(c.c),module:$(`[data-module="${input.dataset.candidate}"]`).value,product:mainProduct,color:''};});if(!mainProduct){error.textContent='请填写主推款名称';return}if(!picks.some(p=>p.module==='hook_A')){error.textContent='请至少选择一条片段作为开头';return}if(!picks.some(p=>p.module==='body')){error.textContent='请至少选择一条片段作为正文';return}button.disabled=true;button.textContent='正在提交…';try{await api(`/api/jobs/${jobId}/submit`,{method:'POST',body:JSON.stringify({main_product:mainProduct,picks})});toast('编排已提交，任务继续执行');await refreshJob(jobId)}catch(err){error.textContent=err.message;button.disabled=false;button.textContent='提交编排并继续'}});
  bindArtifactPreviews($('#jobActions'));
}
async function refreshJob(jobId){
  clearTimeout(state.poll);if(location.hash!==`#/jobs/${jobId}`)return;
  const job=await api(`/api/jobs/${jobId}`),flags=jobFlags(job),actionKey=JSON.stringify([job.status,job.current_stage,job.error,job.model_provider,job.model_name,job.visual_model_provider,job.visual_model_name]),actionsChanged=$('#jobActions')?.dataset.renderKey!==actionKey;
  let packet=null,ai=null;if(flags.canPlan&&actionsChanged)[packet,ai]=await Promise.all([api(`/api/jobs/${jobId}/packet`),api('/api/ai/providers')]);
  state.job=job;
  const connection=$('#jobConnectionState');if(connection){connection.classList.remove('offline');connection.textContent='实时连接正常';}
  if(patchJobRegion('#jobControls',jobControlsHtml(job),deliverablesControlsKey(job)))bindJobControls(jobId);
  const note=$('#jobModelNote');if(note){note.textContent=job.model_provider&&job.model_provider!=='manual'?`文本编排：${providerLabel(job.model_provider)} · ${job.model_name||'auto'} ｜ 画面混剪：${providerLabel(job.visual_model_provider)} · ${job.visual_model_name||'—'}`:'';note.hidden=!note.textContent;}
  const beforeActionScroll=window.scrollY;if(actionsChanged&&patchJobRegion('#jobActions',jobActionsHtml(job,packet,ai),actionKey)){bindJobActions(jobId,job,packet);window.scrollTo(0,Math.min(beforeActionScroll,Math.max(0,document.documentElement.scrollHeight-window.innerHeight)));}
  $('#jobProgressMeta').textContent=`${Math.round(job.progress||0)}% · 当前节点 ${stageLabels[job.current_stage]||job.current_stage||'—'}`;
  if(patchJobRegion('#jobWorkflow',workflowHtml(job),`${selectedStageId(job)}:${JSON.stringify(job.stages.map(x=>[x.stage_id,x.status]))}`))bindStageSelection(jobId);
  if(patchJobRegion('#jobStageDetail',stageDetailHtml(job),stageDetailKey(job))){bindArtifactPreviews($('#jobStageDetail'));updateLiveTimes();}
  const heartbeat=$('[data-relative-time]');if(heartbeat)heartbeat.dataset.relativeTime=job.updated_at||'';
  if(patchJobRegion('#jobArtifacts',artifactsHtml(job),JSON.stringify(job.artifacts.map(x=>[x.id,x.size,x.title]))))bindArtifactPreviews($('#jobArtifacts'));
  patchJobRegion('#jobEvents',eventsHtml(job),JSON.stringify(job.events.map(x=>x.id)));
  scheduleJobPoll(jobId,job);
}
async function renderJob(jobId){
  setCrumb('任务详情');loading();const job=await api(`/api/jobs/${jobId}`),flags=jobFlags(job);state.job=job;state.selectedStage=job.current_stage||job.stages[0]?.stage_id;
  const decisionData=flags.canPlan?await Promise.all([api(`/api/jobs/${jobId}/packet`),api('/api/ai/providers')]):[null,null],packet=decisionData[0],ai=decisionData[1],actionKey=JSON.stringify([job.status,job.current_stage,job.error,job.model_provider,job.model_name,job.visual_model_provider,job.visual_model_name]),stageKey=JSON.stringify(job.stages.map(x=>[x.stage_id,x.status,x.message,x.error,x.finished_at,x.started_at])),artifactKey=JSON.stringify(job.artifacts.map(x=>[x.id,x.size,x.title])),eventKey=JSON.stringify(job.events.map(x=>x.id));
  const metaInfo=[(job.products||[]).length?`<b>商品</b>${escapeHtml(job.products.join('、'))}`:'', (job.materials||[]).length?`<b>面料</b>${escapeHtml(job.materials.join('、'))}`:'', (job.colors||[]).length?`<b>颜色</b>${escapeHtml(job.colors.join('、'))}`:''].filter(Boolean).map(x=>`<span>${x}</span>`).join('');
  app.innerHTML=`<a href="#/queue" class="back-link">${icons.arrow}返回队列</a><div class="detail-head"><div class="detail-title"><span class="eyebrow">${escapeHtml(job.id)}</span><h1>${escapeHtml(job.title)}</h1><p>${escapeHtml(job.source_path)}</p>${metaInfo?`<div class="job-meta">${metaInfo}</div>`:''}<small class="model-note" id="jobModelNote" ${job.model_provider&&job.model_provider!=='manual'?'':'hidden'}>${job.model_provider&&job.model_provider!=='manual'?`文本编排：${escapeHtml(providerLabel(job.model_provider))} · ${escapeHtml(job.model_name||'auto')} ｜ 画面混剪：${escapeHtml(providerLabel(job.visual_model_provider))} · ${escapeHtml(job.visual_model_name||'—')}`:''}</small></div><div class="detail-actions" id="jobControls" data-render-key="${escapeHtml(deliverablesControlsKey(job))}">${jobControlsHtml(job)}</div></div>
  <div id="jobActions" data-render-key="${escapeHtml(actionKey)}">${jobActionsHtml(job,packet,ai)}</div>
  <div class="panel"><div class="panel-head"><div><h2>整体流程</h2><p id="jobProgressMeta">${Math.round(job.progress||0)}% · 当前节点 ${escapeHtml(stageLabels[job.current_stage]||job.current_stage||'—')}</p></div><div class="workflow-meta"><span class="connection-state" id="jobConnectionState"><i></i>实时连接正常</span><span class="panel-hint">点击节点查看详情</span></div></div><div class="workflow" id="jobWorkflow" data-render-key="${escapeHtml(`${selectedStageId(job)}:${JSON.stringify(job.stages.map(x=>[x.stage_id,x.status]))}`)}">${workflowHtml(job)}</div></div>
  <div class="detail-grid"><div><div class="panel stage-detail" id="jobStageDetail" data-render-key="${escapeHtml(stageDetailKey(job))}">${stageDetailHtml(job)}</div>
  <div class="panel"><div class="panel-head"><div><h2>任务产物</h2><p>图片、时间线、日志与视频均可打开</p></div></div><div id="jobArtifacts" data-render-key="${escapeHtml(artifactKey)}">${artifactsHtml(job)}</div></div></div>
  <div class="panel"><div class="panel-head"><div><h2>实时事件</h2><p>后台局部更新，不影响滚动和操作</p></div></div><div class="timeline" id="jobEvents" data-render-key="${escapeHtml(eventKey)}">${eventsHtml(job)}</div></div></div>`;
  bindJobControls(jobId);bindJobActions(jobId,job,packet);bindStageSelection(jobId);bindArtifactPreviews(app);updateLiveTimes();scheduleJobPoll(jobId,job);
}

async function previewArtifact(id,mime,title){
  const dialog=$('#previewDialog'),body=$('#previewBody'),url=`/api/artifacts/${id}/content`;body.innerHTML='<div class="loading"><div class="spinner"></div>加载产物</div>';dialog.showModal();
  if(mime.startsWith('image/'))body.innerHTML=`<img src="${url}" alt="${escapeHtml(title)}">`;
  else if(mime.startsWith('video/'))body.innerHTML=`<video src="${url}" controls autoplay></video>`;
  else {const text=await fetch(url).then(r=>r.text());body.innerHTML=`<pre>${escapeHtml(text)}</pre>`;}
}

async function renderSkill(){
  setCrumb('Skill 管理');loading();const data=await api('/api/skill');
  app.innerHTML=`<div class="hero"><div><span class="eyebrow">AGENT CONTRACT</span><h1>Skill 管理</h1><p>这份薄 Skill 只负责教第三方 Agent 如何调用 MCP，生产状态与规则由本地系统维护。</p></div><button class="button primary" id="saveSkill">保存版本</button></div><div class="editor-layout"><div class="panel"><div class="panel-head"><div><h2>SKILL.md</h2><p>${escapeHtml(data.path)}</p></div><span class="status ${data.exists?'completed':'failed'}">${data.exists?'有效':'不存在'}</span></div><textarea class="code-editor" id="skillEditor" spellcheck="false">${escapeHtml(data.content)}</textarea></div><div class="panel"><div class="panel-head"><div><h2>设计原则</h2><p>让 Skill 保持轻量</p></div></div><div class="info-list"><div class="info-item"><span>文件大小</span><b>${bytes(data.bytes)}</b></div><div class="info-item"><span>职责</span><b>触发、轮询、提交决策</b></div><div class="info-item"><span>不应包含</span><b>渲染实现、缓存、状态机、完整日志</b></div><div class="info-item"><span>版本保护</span><b>每次保存自动备份旧版</b></div></div></div></div>`;
  $('#saveSkill').addEventListener('click',async()=>{try{await api('/api/skill',{method:'PUT',body:JSON.stringify({content:$('#skillEditor').value})});toast('Skill 已保存并备份')}catch(e){toast(e.message)}});
}

async function renderMcp(){
  setCrumb('MCP 接入');loading();const data=await api('/api/mcp');state.mcp=data;const first=Object.keys(data.configs)[0];
  app.innerHTML=`<div class="hero"><div><span class="eyebrow">MODEL-AGNOSTIC BRIDGE</span><h1>MCP 接入</h1><p>WorkBuddy、Codex、Antigravity、OpenCode 等客户端共享同一套剪辑能力与任务状态。</p></div>${status(data.enabled?'completed':'failed')}</div><div class="detail-grid"><div><div class="panel"><div class="panel-head"><div><h2>客户端配置</h2><p>${escapeHtml(data.url)}</p></div><button class="button ghost small" id="rotateToken">轮换密钥</button></div><div class="connection-tabs">${Object.keys(data.configs).map((k,i)=>`<button class="tab-button ${i===0?'active':''}" data-config="${k}">${k}</button>`).join('')}</div><div class="config-box"><pre id="configText">${escapeHtml(JSON.stringify(data.configs[first],null,2))}</pre><button class="button ghost small copy-button" id="copyConfig">复制</button></div></div><div class="panel"><div class="panel-head"><div><h2>公开工具</h2><p>${data.tools.length} 个业务级动作</p></div></div><div class="tools-list">${data.tools.map(t=>`<div class="tool-card"><code>${escapeHtml(t.name)}</code><p>${escapeHtml(t.description)}</p></div>`).join('')}</div></div></div><div class="panel"><div class="panel-head"><div><h2>连接状态</h2><p>本地 Streamable HTTP</p></div></div><div class="info-list"><div class="info-item"><span>Endpoint</span><code>${escapeHtml(data.url)}</code></div><div class="info-item"><span>认证</span><b>Bearer Token</b></div><div class="info-item"><span>当前密钥</span><code>${escapeHtml(data.token)}</code></div><div class="info-item"><span>安全边界</span><b>默认只监听 127.0.0.1</b></div></div></div></div>`;
  $$('[data-config]').forEach(btn=>btn.addEventListener('click',()=>{$$('[data-config]').forEach(x=>x.classList.remove('active'));btn.classList.add('active');$('#configText').textContent=JSON.stringify(data.configs[btn.dataset.config],null,2)}));
  $('#copyConfig').addEventListener('click',()=>navigator.clipboard.writeText($('#configText').textContent).then(()=>toast('配置已复制')));
  $('#rotateToken').addEventListener('click',async()=>{if(!confirm('旧密钥会立即失效，继续吗？'))return;await api('/api/mcp/token',{method:'POST',body:'{}'});toast('密钥已轮换');renderMcp()});
}

async function renderSettings(){
  setCrumb('系统设置');loading();const [data,ai,rules]=await Promise.all([api('/api/settings'),api('/api/ai/providers'),api('/api/rules')]);
  const provider=id=>ai.providers.find(x=>x.id===id)||{};
  const ruleRows=(rules.rules||[]).map(rule=>`<div class="info-item"><span><b>${escapeHtml(rule.problem_type||'general')} · ${escapeHtml(rule.scope||'text')}</b><small>${escapeHtml(rule.expected_change||'')}${(rule.conflicts_with||[]).length?' · 存在冲突':''}</small></span><button class="button ghost small" data-rule-toggle="${escapeHtml(rule.id)}" data-enabled="${rule.enabled?'true':'false'}">${rule.enabled?'停用':'启用'}</button></div>`).join('')||'<p class="muted-empty">尚无反馈规则。</p>';
  app.innerHTML=`<div class="hero"><div><span class="eyebrow">SYSTEM CONFIGURATION</span><h1>系统设置</h1><p>配置执行内核、AI 提供方和本地接入策略。</p></div></div><div class="panel model-picker-panel"><div class="panel-head"><div><h2>运行配置</h2><p>内置引擎随应用版本一起升级</p></div></div><form class="settings-form" id="settingsForm"><label>内置切片内核<input value="${escapeHtml(data.engine_path||'')}" readonly><small>当前项目自带，不再依赖外部旧引擎目录。</small></label><label>引擎 Python<input name="engine_python" value="${escapeHtml(data.engine_python||'')}"><small>建议使用项目独立的 Python 3.13 环境。</small></label><div class="settings-section"><b>AI 提供方</b><small>文本编排和多模态混剪可分别指定模型；Multica 会通过所选 Agent 发起一次 Run。</small></div><label>WorkBuddy CLI<input name="workbuddy_cli_path" value="${escapeHtml(data.workbuddy_cli_path||'')}"><small>${provider('workbuddy').available?'已检测到可执行程序。':'当前路径不可用。'}</small></label><label>Antigravity CLI<input name="antigravity_cli_path" value="${escapeHtml(data.antigravity_cli_path||'')}"><small>${provider('antigravity').available?'已检测到可执行程序。':'当前路径不可用。'}</small></label><label>Codex CLI<input name="codex_cli_path" value="${escapeHtml(data.codex_cli_path||'')}"><small>${provider('codex').available?'已检测到可执行程序。':'当前路径不可用。'}</small></label><label>OpenCode CLI<input name="opencode_cli_path" value="${escapeHtml(data.opencode_cli_path||'')}"><small>${provider('opencode').available?'已检测到可执行程序。':'当前路径不可用。'}</small></label><label>Multica CLI<input name="multica_cli_path" value="${escapeHtml(data.multica_cli_path||'')}"><small>${provider('multica').available?'已检测到可执行程序；模型列表显示 Multica Agent。':'当前路径不可用，请先安装并运行 multica setup。'}</small></label><div class="form-grid"><label>Multica Profile<input name="multica_profile" value="${escapeHtml(data.multica_profile||'')}" placeholder="留空使用默认 Profile"></label><label>Multica Workspace ID<input name="multica_workspace_id" value="${escapeHtml(data.multica_workspace_id||'')}" placeholder="留空使用当前 Workspace"></label></div><label>默认文本编排模型${modelPickerHtml(ai.providers,data.ai_default_selection||ai.default,{name:'ai_default_selection'})}<small>输入关键词可模糊筛选，新建任务仍可单独修改。</small></label><label>默认多模态混剪模型${modelPickerHtml(ai.providers,data.visual_ai_default_selection||ai.visual_default,{name:'visual_ai_default_selection',capability:'vision'})}<small>只显示当前已接入图片输入的模型。</small></label><div class="settings-section"><b>TypeSafe AI（Jev 快速语义网关）</b><small>毫秒级智能原语初筛，支持置信度门禁与低置信度平滑回退 LLM。</small></div><label class="check-field"><input name="jev_enabled" type="checkbox" ${data.jev_enabled?'checked':''}><span>启用 TypeSafe AI (Jev) 语义初筛门禁</span></label><div class="form-grid"><label>API Key<input name="jev_api_key" type="password" value="${escapeHtml(data.jev_api_key||'')}" placeholder="${data.jev_api_key_configured?'已配置（留空或保持掩码不变）':'未配置，请输入 API Key'}"><small>${data.jev_api_key_configured?'已配置 Key（安全掩码保护）':'未配置 Key'}</small></label><label>Base URL<input name="jev_base_url" value="${escapeHtml(data.jev_base_url||'https://api.typesafe.ai/v1')}" placeholder="https://api.typesafe.ai/v1"></label></div><div class="form-grid"><label>默认模型<input name="jev_default_model" value="${escapeHtml(data.jev_default_model||'jev-latest')}" placeholder="jev-latest"></label><label>置信度阈值 (0.0-1.0)<input name="jev_min_confidence" type="number" step="0.05" min="0.1" max="1.0" value="${data.jev_min_confidence??0.6}"><small>低于此置信度或异常自动交由 LLM 兜底</small></label></div><div class="form-grid"><label>超时时间（秒）<input name="jev_timeout_seconds" type="number" min="1" max="60" value="${data.jev_timeout_seconds??15}"></label><label>并发请求数<input name="jev_concurrency" type="number" min="1" max="32" value="${data.jev_concurrency??8}"><small>多线程并发加速</small></label></div><label>Skill 文件路径<input name="skill_path" value="${escapeHtml(data.skill_path||'')}"></label><label>并行任务数<input name="max_parallel_jobs" type="number" min="1" max="1" value="1" disabled><small>视频转写和渲染固定串行，避免显存与编码资源争抢。</small></label><label class="check-field"><input name="mcp_enabled" type="checkbox" ${data.mcp_enabled?'checked':''}><span>启用 MCP HTTP 入口</span></label><div><button class="button primary" type="submit">保存设置</button></div></form></div><div class="panel"><div class="panel-head"><div><h2>自进化规则</h2><p>版本 ${rules.version||1} · 冲突规则默认禁用</p></div><form id="ruleRollbackForm" class="rollback-form"><input id="ruleRollbackVersion" type="number" min="1" placeholder="版本"><button class="button ghost small">回滚</button></form></div><div class="info-list">${ruleRows}</div></div>`;
  bindModelPickers(app);
  $('#settingsForm').addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(e.target);await api('/api/settings',{method:'PUT',body:JSON.stringify({engine_python:f.get('engine_python'),workbuddy_cli_path:f.get('workbuddy_cli_path'),antigravity_cli_path:f.get('antigravity_cli_path'),codex_cli_path:f.get('codex_cli_path'),opencode_cli_path:f.get('opencode_cli_path'),multica_cli_path:f.get('multica_cli_path'),multica_profile:f.get('multica_profile'),multica_workspace_id:f.get('multica_workspace_id'),ai_default_selection:f.get('ai_default_selection'),visual_ai_default_selection:f.get('visual_ai_default_selection'),jev_enabled:f.get('jev_enabled')==='on',jev_api_key:f.get('jev_api_key'),jev_base_url:f.get('jev_base_url'),jev_default_model:f.get('jev_default_model'),jev_min_confidence:f.get('jev_min_confidence'),jev_timeout_seconds:f.get('jev_timeout_seconds'),jev_concurrency:f.get('jev_concurrency'),skill_path:f.get('skill_path'),mcp_enabled:f.get('mcp_enabled')==='on'})});toast('设置已保存')});

  $$('[data-rule-toggle]').forEach(button=>button.addEventListener('click',async()=>{await api('/api/rules/enable',{method:'POST',body:JSON.stringify({rule_id:button.dataset.ruleToggle,enabled:button.dataset.enabled!=='true'})});toast('规则状态已更新');renderSettings()}));
  $('#ruleRollbackForm').addEventListener('submit',async e=>{e.preventDefault();const version=Number($('#ruleRollbackVersion').value);if(!version)return;await api('/api/rules/rollback',{method:'POST',body:JSON.stringify({version})});toast(`已回滚到版本 ${version}`);renderSettings()});
}

async function openNewJob(){
  $('#newJobError').textContent='';
  $('#newJobDialog').showModal();
  try{const ai=await api('/api/ai/providers'),textContainer=$('#newJobTextModelPicker'),visualContainer=$('#newJobVisualModelPicker'),available=ai.providers.filter(x=>x.available);textContainer.innerHTML=modelPickerHtml(ai.providers,ai.default,{name:'text_ai_model'});visualContainer.innerHTML=modelPickerHtml(ai.providers,ai.visual_default,{name:'visual_ai_model',capability:'vision'});bindModelPickers(textContainer);bindModelPickers(visualContainer);if(!available.length)$('#newJobError').textContent='未检测到可用的 AI CLI，可在系统设置中修正路径。';}catch(err){$('#newJobError').textContent=err.message}
}
function bindCommon(){
  $$('[data-new-job]').forEach(x=>x.addEventListener('click',openNewJob));
  $$('[data-open-job]').forEach(x=>x.addEventListener('click',()=>location.hash=`#/jobs/${x.dataset.openJob}`));
  $$('[data-open-folder]').forEach(x=>x.addEventListener('click',async e=>{
    e.stopPropagation();
    try{
      await api(`/api/jobs/${x.dataset.openFolder}/open-folder`,{method:'POST',body:'{}'});
      toast('已打开成片文件夹');
    }catch(err){toast(err.message);}
  }));
  $$('[data-restart-job]').forEach(x=>x.addEventListener('click',async e=>{
    e.stopPropagation();
    const jobId=x.dataset.restartJob,title=x.dataset.jobTitle||jobId;
    if(!confirm(`确定要从头重新开始任务“${title}”吗？`))return;
    try{
      await api(`/api/jobs/${jobId}/restart`,{method:'POST',body:'{}'});
      toast('任务已重新开始');
      route();
    }catch(err){toast(err.message);}
  }));
  $$('[data-delete-job]').forEach(x=>x.addEventListener('click',async e=>{
    e.stopPropagation();
    const jobId=x.dataset.deleteJob,title=x.dataset.jobTitle||jobId;
    if(!confirm(`确定要彻底删除任务“${title}”吗？此操作不可恢复。`))return;
    try{
      await api(`/api/jobs/${jobId}/delete`,{method:'POST',body:'{}'});
      toast('任务已删除');
      route();
    }catch(err){toast(err.message);}
  }));
}
async function route(){
  clearTimeout(state.poll);const hash=location.hash||'#/dashboard';
  try{if(hash.startsWith('#/jobs/'))return renderJob(hash.split('/')[2]);if(hash==='#/queue')return renderQueue();if(hash==='#/skill')return renderSkill();if(hash==='#/mcp')return renderMcp();if(hash==='#/settings')return renderSettings();return renderDashboard();}catch(e){app.innerHTML=`<div class="danger-box">${escapeHtml(e.message)}</div>`;}
}

$('#newJobButton').addEventListener('click',openNewJob);
$$('[data-close-new-job]').forEach(button=>button.addEventListener('click',()=>$('#newJobDialog').close()));
$('#newJobDialog').addEventListener('cancel',e=>{e.preventDefault();$('#newJobDialog').close()});
$('#newJobDialog').addEventListener('click',e=>{if(e.target===$('#newJobDialog'))$('#newJobDialog').close()});
$('#newJobForm [name="title"]').addEventListener('input',e=>{e.target.dataset.userEdited=e.target.value?'true':''});
$('#targetDuration')?.addEventListener('change',e=>{const el=$('#customDurationFields');if(el)el.hidden=e.target.value!=='custom'});
$('#pickSourceButton').addEventListener('click',async e=>{
  const button=e.currentTarget, source=$('#newJobForm [name="source_path"]'), title=$('#newJobForm [name="title"]');
  button.disabled=true;button.textContent='选择中…';$('#newJobError').textContent='';
  try{const result=await api('/api/files/pick',{method:'POST',body:'{}'});if(result.cancelled)return;source.value=result.path;if(title.dataset.userEdited!=='true')title.value=result.name;}
  catch(err){$('#newJobError').textContent=err.message}finally{button.disabled=false;button.textContent='浏览';}
});
$('#fileDrop').addEventListener('click',e=>{if(!e.target.closest('#pickSourceButton'))$('#pickSourceButton').click()});
$('#pickSubtitleButton')?.addEventListener('click',async e=>{
  const button=e.currentTarget, subtitle=$('#newJobForm [name="subtitle_path"]');
  button.disabled=true;button.textContent='选择中…';$('#newJobError').textContent='';
  try{const result=await api('/api/files/pick',{method:'POST',body:JSON.stringify({kind:'subtitle'})});if(result.cancelled)return;subtitle.value=result.path;}
  catch(err){$('#newJobError').textContent=err.message}finally{button.disabled=false;button.textContent='浏览';}
});
$('#newJobForm').addEventListener('submit',async e=>{
  e.preventDefault();const submit=$('#createJobSubmit');submit.disabled=true;submit.textContent='正在创建…';$('#newJobError').textContent='';
  try{const f=new FormData(e.target);const data=await api('/api/jobs',{method:'POST',body:JSON.stringify(Object.fromEntries(f.entries()))});$('#newJobDialog').close();e.target.reset();$('#newJobForm [name="title"]').dataset.userEdited='';location.hash=`#/jobs/${data.id}`;toast('任务已加入队列');}
  catch(err){$('#newJobError').textContent=err.message}finally{submit.disabled=false;submit.textContent='加入队列';}
});
$('#previewDialog .preview-close').addEventListener('click',()=>{$('#previewDialog video')?.pause();$('#previewDialog').close()});
$('#menuButton').addEventListener('click',()=>$('.sidebar').classList.toggle('open'));
window.addEventListener('hashchange',()=>{$('.sidebar').classList.remove('open');route()});
setInterval(()=>{$('#clock').textContent=new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date());updateLiveTimes()},1000);
api('/api/health').then(x=>$('#systemVersion').textContent=`v${x.version} · MCP online`).catch(()=>$('#systemVersion').textContent='连接失败');
route();

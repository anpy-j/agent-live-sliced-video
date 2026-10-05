const $ = (selector, root=document) => root.querySelector(selector);
const $$ = (selector, root=document) => [...root.querySelectorAll(selector)];
const app = $('#app');
const state = { dashboard:null, job:null, poll:null, mcp:null, selectedStage:null, jianying:null, jianyingAbort:null, label:{session:null,decisions:{},sel:{},patch:null}, clausesJob:null, clausesData:null, clausesFilter:'s2' };
const labels = {queued:'排队中',running:'执行中',waiting_input:'待继续',completed:'已完成',failed:'执行失败',cancelled:'已取消',pending:'等待',succeeded:'完成'};
const stageLabels = {asr:'语音转写与切分',filter:'规则粗筛',judge:'AI 可用性判定',order:'AI 排序编排',render:'渲染成片'};
const reasonLabels = {too_short:'文本过短',non_chinese:'中文占比低',duration_gate:'时长不足',hard_vocab:'违禁词',stage_chatter:'场控话术',malformed_speech:'病句/口误',duplicate:'重复',literal_duplicate:'字面重复',semantic_duplicate:'语义重复',invalid_bounds:'时间异常'};
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
  const contentType=response.headers.get('content-type')||'';
  if(!contentType.includes('application/json')){
    throw new Error('后台服务未加载当前页面所需接口，请完全退出并重启 LiveCut 后台服务');
  }
  const data = await response.json().catch(()=>({}));
  if (!response.ok) throw new Error(data.error || `请求失败 ${response.status}`);
  return data;
}
function escapeHtml(value=''){return String(value).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
function formatTime(value){if(!value)return '—';const d=new Date(value);return new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(d);}
function formatExactTime(value){if(!value)return '—';const d=new Date(value);return new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(d);}
function durationText(seconds=0){seconds=Math.max(0,Math.floor(seconds));const h=Math.floor(seconds/3600),m=Math.floor(seconds%3600/60),s=seconds%60;return h?`${h} 小时 ${m} 分`:m?`${m} 分 ${s} 秒`:`${s} 秒`;}
function bytes(value=0){if(value<1024)return `${value} B`;if(value<1048576)return `${(value/1024).toFixed(1)} KB`;return `${(value/1048576).toFixed(1)} MB`;}
function stageLabel(id){return id?(stageLabels[id]||id):'';}
function status(value){return `<span class="status ${escapeHtml(value||'')}">${labels[value]||value||'—'}</span>`;}
function editCountText(j){const n=Number(j.edit_count||0);return n>0?` ×${n}`:'';}
function statusCell(j){
  if(j.status==='completed'){
    if(j.delivered)return `<span class="status delivered">已剪辑${editCountText(j)}</span>`;
    return `<button type="button" class="status completed status-click" data-mark-delivered="${escapeHtml(j.id)}" title="点击标记为已剪辑">已完成${editCountText(j)}</button>`;
  }
  return status(j.status);
}
function toast(message){const el=$('#toast');el.textContent=message;el.classList.add('show');clearTimeout(el.timer);el.timer=setTimeout(()=>el.classList.remove('show'),2400);}
function loading(){app.innerHTML='<div class="loading"><div class="spinner"></div>正在读取本地任务状态</div>';}
function setCrumb(text){$('#pageCrumb').textContent=text;$$('[data-nav]').forEach(x=>{const active=location.hash.includes(x.dataset.nav);x.classList.toggle('active',active);if(active)x.setAttribute('aria-current','page');else x.removeAttribute('aria-current')});}
function jobFlags(job){return {active:['queued','running'].includes(job.status)};}
function jobStageLabel(job,id,stored=''){
  if(job?.job_type==='remix'){
    const remixLabels={asr:'片段文字识别',filter:'文字字面去重',judge:'语义去重与分类',order:'动态内容重排',render:'生成新成片'};
    return remixLabels[id]||stored||stageLabel(id);
  }
  return stored||stageLabel(id);
}
function jobTypeTag(job){
  if(job.job_type==='remix')return '<span class="timeline-tag">成片重组</span>';
  if(job.job_type==='timeline')return '<span class="timeline-tag">虚拟时间线</span>';
  return '';
}
function jobTargetText(job,prefix='目标'){
  if(job.job_type==='remix')return ' · 全部去重后片段';
  return job.target_seconds?` · ${prefix} ${escapeHtml(job.target_seconds)}s`:'';
}

function jobRows(jobs){
  if(!jobs.length)return `<div class="empty">${icons.empty}<h3>还没有剪辑任务</h3><p>添加第一段直播素材，系统会自动完成转写、粗筛、判定、排序与渲染。</p><button class="button primary" data-new-job>新建剪辑任务</button></div>`;
  return `<table class="jobs-table"><thead><tr><th>任务</th><th>当前节点</th><th>进度</th><th>状态</th><th>操作</th></tr></thead><tbody>${jobs.map(j=>{const folder=j.deliverables||{};return `<tr>
<td><div class="job-name"><span class="job-thumb">${icons.video}</span><div><b>${escapeHtml(j.title)}</b>${jobTypeTag(j)}<small>${escapeHtml(j.source_path)}${jobTargetText(j)}</small></div></div></td>
    <td><small>${escapeHtml(j.current_stage?jobStageLabel(j,j.current_stage):'—')}</small></td>
    <td><div class="progress"><div class="progress-line"><i style="width:${Math.max(2,Math.min(100,j.progress||0))}%"></i></div><small>${Math.round(j.progress||0)}% · ${formatTime(j.updated_at)}</small></div></td>
    <td>${statusCell(j)}</td>
    <td><div class="job-row-actions">
      <button class="link-button" data-open-job="${escapeHtml(j.id)}">详情</button>
      ${j.status==='completed'&&folder.exists?`<button class="link-button" data-open-folder="${escapeHtml(j.id)}">打开文件夹</button>`:''}
      <button class="link-button" data-restart-job="${escapeHtml(j.id)}" data-job-title="${escapeHtml(j.title)}">重置</button>
      <button class="link-button text-danger" data-delete-job="${escapeHtml(j.id)}" data-job-title="${escapeHtml(j.title)}">删除</button>
    </div></td></tr>`}).join('')}</tbody></table>`;
}

async function renderDashboard(){
  setCrumb('任务总览');loading();const data=await api('/api/dashboard');state.dashboard=data;
  $('#queueBadge').textContent=data.active;
  const counts=data.counts||{},running=counts.running||0,done=counts.completed||0,failed=counts.failed||0;
  app.innerHTML=`<div class="hero"><div><span class="eyebrow">LIVE PRODUCTION CONTROL</span><h1>生产控制台</h1><p>素材转写、规则粗筛、AI 判定与排序，一次渲染成片。</p></div><button class="button primary" data-new-job>${icons.video}添加直播素材</button></div>
  <div class="stat-grid"><div class="stat-card"><span>队列任务</span><strong>${data.active}</strong><small>等待或正在执行</small></div><div class="stat-card"><span>执行中</span><strong>${running}</strong><small>本地工作进程</small></div><div class="stat-card"><span>已完成</span><strong>${done}</strong><small>可查看与播放</small></div><div class="stat-card"><span>失败</span><strong>${failed}</strong><small>需要重新开始</small></div></div>
  <div class="panel"><div class="panel-head"><div><h2>最近任务</h2><p>按创建时间排列</p></div><a class="link-button" href="#/queue">查看全部</a></div>${jobRows(data.jobs.slice(0,8))}</div>`;
  bindCommon();
}

async function renderQueue(){
  setCrumb('剪辑队列');loading();const data=await api('/api/jobs');
  app.innerHTML=`<div class="hero"><div><span class="eyebrow">PRODUCTION QUEUE</span><h1>剪辑队列</h1><p>任务串行调度，实时展示节点状态、进度和异常。</p></div><button class="button primary" data-new-job>添加任务</button></div><div class="panel"><div class="panel-head"><div><h2>全部任务</h2><p>${data.jobs.length} 个任务</p></div></div>${jobRows(data.jobs)}</div>`;
  bindCommon();
}

function viralDnaSummary(reference){
  const dna=reference.dna||{};
  const hook=dna.hook||{};
  const distribution=dna.content_distribution||{};
  const distributionText=Object.entries(distribution)
    .sort((a,b)=>b[1]-a[1]).slice(0,3)
    .map(([name,value])=>`${escapeHtml(name)} ${Math.round(Number(value)*100)}%`).join(' · ');
  if(!reference.dna)return '<span class="muted">等待解析</span>';
  return `<div class="viral-dna"><b>${escapeHtml(dna.primary_focus||'综合表达')}</b><small>钩子：${escapeHtml(hook.mechanism||'未识别')} · ${distributionText||'暂无内容分布'}</small></div>`;
}

async function renderViralV2(){
  setCrumb('爆款学习剪辑');loading();
  const data=await api('/api/viral-v2/references');
  const rows=(data.references||[]).map(item=>`<tr>
    <td><div class="job-name"><span class="job-thumb">${icons.file}</span><div><b>${escapeHtml(item.title)}</b><small>${item.duration_seconds?`${Number(item.duration_seconds).toFixed(0)}秒 · `:''}${item.likes!=null?`${Number(item.likes).toLocaleString()}赞 · `:''}${formatTime(item.created_at)}</small></div></div></td>
    <td>${viralDnaSummary(item)}</td>
    <td>${status(item.status)}</td>
    <td><div class="job-row-actions"><button class="link-button" data-analyze-viral="${escapeHtml(item.id)}">重新解析</button><button class="link-button text-danger" data-delete-viral="${escapeHtml(item.id)}">删除</button></div></td>
  </tr>`).join('');
  app.innerHTML=`<div class="hero"><div><span class="eyebrow">VIRAL REFERENCE PIPELINE V2</span><h1>爆款学习剪辑</h1><p>独立于现有 S1–S5。样本、分析结果和未来 V2 任务均保存到独立数据库与目录。</p></div></div>
  <div class="stat-grid"><div class="stat-card"><span>爆款样本</span><strong>${data.total||0}</strong><small>仅收录真实爆款正样本</small></div><div class="stat-card"><span>已生成 DNA</span><strong>${data.ready||0}</strong><small>解析版本 ${escapeHtml(data.analyzer_version||'')}</small></div><div class="stat-card"><span>旧流程影响</span><strong>0</strong><small>不写入旧 jobs / stages</small></div></div>
  <div class="panel"><div class="panel-head"><div><h2>导入爆款文本</h2><p>第一阶段使用本地基线解析，不消耗 Token；后续可用强模型重新解析同一条样本。</p></div></div>
    <form id="viralReferenceForm" class="form-grid">
      <label class="field"><span>样本名称</span><input name="title" required maxlength="160" placeholder="例如：上身效果型爆款 01"></label>
      <label class="field"><span>点赞量</span><input name="likes" type="number" min="0" placeholder="可选"></label>
      <label class="field"><span>视频时长（秒）</span><input name="duration_seconds" type="number" min="1" step="0.1" placeholder="例如 92"></label>
      <label class="field"><span>发布时间</span><input name="published_at" type="date"></label>
      <label class="field full"><span>爆款视频文本</span><textarea name="transcript" required rows="9" placeholder="粘贴视频完整口播文本，至少20个字符"></textarea></label>
      <div class="form-actions full"><button class="button primary" type="submit">保存并解析</button></div>
    </form>
  </div>
  <div class="panel"><div class="panel-head"><div><h2>爆款样本库</h2><p>${data.total||0} 条样本；每条只解析一次并缓存结构 DNA</p></div></div>${rows?`<table class="jobs-table"><thead><tr><th>样本</th><th>结构 DNA</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table>`:`<div class="empty">${icons.empty}<h3>还没有爆款样本</h3><p>先粘贴一条真实爆款视频的完整文本。</p></div>`}</div>`;
  $('#viralReferenceForm')?.addEventListener('submit',async event=>{
    event.preventDefault();const form=new FormData(event.currentTarget);
    const payload={title:form.get('title'),transcript:form.get('transcript'),likes:form.get('likes'),duration_seconds:form.get('duration_seconds'),published_at:form.get('published_at'),analyze:true};
    try{await api('/api/viral-v2/references',{method:'POST',body:JSON.stringify(payload)});toast('爆款样本已保存并解析');await renderViralV2()}catch(err){toast(err.message)}
  });
  $$('[data-analyze-viral]').forEach(button=>button.addEventListener('click',async()=>{
    try{await api(`/api/viral-v2/references/${button.dataset.analyzeViral}/analyze`,{method:'POST',body:'{}'});toast('结构 DNA 已更新');await renderViralV2()}catch(err){toast(err.message)}
  }));
  $$('[data-delete-viral]').forEach(button=>button.addEventListener('click',async()=>{
    if(!confirm('删除这条爆款样本及其分析结果？'))return;
    try{await api(`/api/viral-v2/references/${button.dataset.deleteViral}`,{method:'DELETE'});toast('样本已删除');await renderViralV2()}catch(err){toast(err.message)}
  }));
}

async function renderJianyingDrafts(force=false){
  setCrumb('剪映草稿');
  if(force||!state.jianying){
    loading();
    if(state.jianyingAbort)state.jianyingAbort.abort();
    const controller=new AbortController();state.jianyingAbort=controller;
    const timeout=setTimeout(()=>controller.abort(),120000);
    try{
      state.jianying=await api('/api/jianying/drafts',{signal:controller.signal});
    }catch(err){
      // 切走页面导致的取消不算错误，也不要回写覆盖当前页面。
      if(controller.signal.aborted&&location.hash!=='#/jianying')return;
      throw err;
    }finally{
      clearTimeout(timeout);
      if(state.jianyingAbort===controller)state.jianyingAbort=null;
    }
  }
  const data=state.jianying;
  const drafts=data.drafts||[],defaults=data.defaults||{};
  const cards=drafts.map(draft=>{
    const timeline=draft.recommended_timeline;
    const recommendation=timeline?`最长时间线：${escapeHtml(timeline.name||timeline.title||timeline.timeline_id)} · ${durationText(timeline.timeline_duration)}`:(draft.timeline_error?'时间线解析失败':'未发现有效时间线');
    const activeCount=Number(draft.active_job_count||0);
    return `<article class="jianying-card"><div class="jianying-card-body"><div class="jianying-card-head"><h2>${escapeHtml(draft.name)}</h2><span>${formatTime(draft.modified_at)}</span></div><p>${recommendation}</p><div class="jianying-card-foot"><div class="jianying-title-preview"><span>成片名称</span><b>${escapeHtml(draft.suggested_title)}</b></div><div class="jianying-action">${activeCount?`<span class="jianying-running">剪辑中 ${activeCount}</span>`:''}<button type="button" class="button primary small" data-edit-jianying="${escapeHtml(draft.id)}" ${timeline?'':'disabled'}>剪辑</button></div></div></div></article>`;
  }).join('');
  app.innerHTML=`<div class="hero"><div><span class="eyebrow">JIANYING DRAFTS</span><h1>剪映草稿</h1><p>点击“剪辑”即按最长时间线、120–180 秒深度和分段版直接加入队列。</p></div><button class="button ghost" data-refresh-jianying>刷新草稿</button></div><div class="jianying-defaults"><span>默认导出目录</span><b>${escapeHtml(defaults.export_dir||'—')}</b></div>${drafts.length?`<div class="jianying-grid">${cards}</div>`:`<div class="panel"><div class="empty">${icons.empty}<h3>没有找到剪映草稿</h3><p>请确认本机已安装剪映专业版并保存过草稿。</p></div></div>`}`;
  $('[data-refresh-jianying]')?.addEventListener('click',()=>renderJianyingDrafts(true));
  $$('[data-edit-jianying]').forEach(button=>button.addEventListener('click',()=>{
    const draft=drafts.find(item=>item.id===button.dataset.editJianying);
    if(draft)enqueueJianyingDraft(draft,defaults,button);
  }));
}

function nextSuggestedTitle(title){const match=String(title).match(/-(\d+)$/);return match?`${title.slice(0,-match[0].length)}-${Number(match[1])+1}`:`${title}-1`;}
async function enqueueJianyingDraft(draft,defaults,button){
  const timeline=draft.recommended_timeline;
  if(!timeline)return;
  button.disabled=true;button.textContent='加入中…';
  try{
    const job=await api('/api/jobs',{method:'POST',body:JSON.stringify({job_type:'timeline',draft_path:timeline.path,title:draft.suggested_title,draft_name:draft.name,auto_title:true,product_name:'',export_mode:defaults.export_mode||'segments',export_dir:defaults.export_dir||'',target_min:defaults.target_min||120,target_max:defaults.target_max||180})});
    draft.active_job_count=Number(draft.active_job_count||0)+1;
    draft.suggested_title=nextSuggestedTitle(job.title||draft.suggested_title);
    renderJianyingDrafts();
    toast(`“${draft.name}”已加入剪辑队列`);
  }catch(err){button.disabled=false;button.textContent='剪辑';toast(err.message)}
}

function artifactCard(a){
  const url=`/api/artifacts/${a.id}/content`;let visual=icons.file;
  if((a.mime_type||'').startsWith('image/'))visual=`<img loading="lazy" src="${url}" alt="${escapeHtml(a.title)}">`;
  if((a.mime_type||'').startsWith('video/'))visual=`<video preload="metadata" src="${url}#t=0.1" aria-label="${escapeHtml(a.title)}"></video>`;
  return `<button class="artifact" data-preview="${escapeHtml(a.id)}" data-mime="${escapeHtml(a.mime_type||'')}" data-title="${escapeHtml(a.title)}"><span class="artifact-preview">${visual}</span><span class="artifact-meta"><b>${escapeHtml(a.title)}</b><small>${escapeHtml(a.kind)} · ${bytes(a.size)}</small></span></button>`;
}
function payloadHtml(payload){if(!payload)return '';const value=JSON.stringify(payload,null,2);return `<details class="event-data"><summary>查看执行数据</summary><pre>${escapeHtml(value.length>6000?`${value.slice(0,6000)}\n……`:value)}</pre></details>`;}

function selectedStageId(job){return job.stages.some(s=>s.stage_id===state.selectedStage)?state.selectedStage:(job.current_stage||job.stages[0]?.stage_id);}
function workflowHtml(job){const selected=selectedStageId(job);return job.stages.map((s,i)=>`<button type="button" class="stage ${escapeHtml(s.status)} ${s.stage_id===selected?'selected':''}" data-stage-select="${escapeHtml(s.stage_id)}" aria-pressed="${s.stage_id===selected}"><span class="stage-dot">${s.status==='succeeded'?'✓':String(i+1).padStart(2,'0')}</span><b>${escapeHtml(jobStageLabel(job,s.stage_id,s.name))}</b><small>${labels[s.status]||s.status}</small></button>`).join('');}
function workflowKey(job){return `${selectedStageId(job)}:${JSON.stringify(job.stages.map(x=>[x.stage_id,x.status]))}`;}
function stageDetailHtml(job){
  const id=selectedStageId(job),stage=job.stages.find(s=>s.stage_id===id)||job.stages[0];
  if(!stage)return `<div class="empty">${icons.empty}<h3>暂无节点</h3><p>任务尚未初始化流程节点。</p></div>`;
  const events=job.events.filter(e=>e.stage_id===id),artifacts=job.artifacts.filter(a=>a.stage_id===id),isCurrent=job.current_stage===id&&jobFlags(job).active,runtime=job.runtime||{};
  const runState=isCurrent?(runtime.process_active?'本地子进程正在执行':runtime.worker_alive?'工作进程正在处理':'后台服务未运行'):(labels[stage.status]||stage.status);
  const rerunnable=job.job_type!=='remix'&&['filter','judge','order','render'].includes(id),index=job.stages.indexOf(stage),upstreamReady=index>0&&job.stages[index-1].status==='succeeded';
  const canRerun=rerunnable&&upstreamReady&&!jobFlags(job).active;
  const actionLabel=id==='render'?(stage.status==='succeeded'?'重新渲染成片':'渲染成片'):(stage.status==='succeeded'?'重新执行此节点':'执行此节点');
  return `<div class="stage-detail-head"><div><span class="eyebrow">NODE ${String(index+1).padStart(2,'0')}</span><h2>${escapeHtml(jobStageLabel(job,stage.stage_id,stage.name))}</h2><p>${escapeHtml(stage.error||stage.message||'等待上游节点完成')}</p></div><div class="stage-detail-actions"><span class="runtime-state ${isCurrent&&runtime.worker_alive?'live':''}"><i></i>${escapeHtml(runState)}</span>${canRerun?`<button class="button ghost small" data-rerun-stage="${escapeHtml(id)}">${actionLabel}</button>`:''}</div></div>
  <div class="stage-metrics"><div><span>开始时间</span><b>${formatExactTime(stage.started_at)}</b></div><div><span>运行耗时</span><b data-elapsed-from="${escapeHtml(stage.started_at||'')}" data-elapsed-to="${escapeHtml(stage.finished_at||'')}">${stage.started_at?durationText((new Date(stage.finished_at||Date.now())-new Date(stage.started_at))/1000):'—'}</b></div><div><span>最后心跳</span><b data-relative-time="${escapeHtml(job.updated_at||'')}">刚刚</b></div><div><span>节点进度</span><b>${Math.round((stage.progress||0)*100)}%</b></div></div>
  ${isCurrent&&!runtime.worker_alive?'<div class="service-warning"><b>后台服务已停止</b><span>这不是正常等待；重启 LiveCut 后任务会恢复进队。</span></div>':''}
  <div class="node-section"><div class="node-section-title"><b>该节点执行记录</b><span>${events.length} 条</span></div><div class="node-events">${events.length?events.map(e=>`<article class="node-event ${escapeHtml(e.level)}"><span class="event-mark"></span><div><time>${formatExactTime(e.created_at)}</time><p>${escapeHtml(e.message)}</p>${payloadHtml(e.payload)}</div></article>`).join(''):'<p class="muted-empty">还没有执行记录。</p>'}</div></div>
  <div class="node-section"><div class="node-section-title"><b>该节点产物</b><span>${artifacts.length} 个</span></div>${artifacts.length?`<div class="node-artifacts">${artifacts.map(artifactCard).join('')}</div>`:'<p class="muted-empty">节点完成后，日志、报告或视频会出现在这里。</p>'}</div>`;
}
function stageDetailKey(job){const id=selectedStageId(job),stage=job.stages.find(s=>s.stage_id===id);return JSON.stringify([id,job.status,job.current_stage,job.runtime,stage,job.events.filter(x=>x.stage_id===id).map(x=>x.id),job.artifacts.filter(x=>x.stage_id===id).map(x=>[x.id,x.size])]);}
function artifactsHtml(job){return job.artifacts.length?`<div class="artifacts">${job.artifacts.map(artifactCard).join('')}</div>`:`<div class="empty">${icons.empty}<h3>暂无产物</h3><p>节点完成后会自动登记产物。</p></div>`;}
function artifactsKey(job){return JSON.stringify(job.artifacts.map(x=>[x.id,x.size,x.title]));}
function eventsHtml(job){return job.events.map(e=>`<div class="event ${escapeHtml(e.level)}"><time>${formatTime(e.created_at)} · ${escapeHtml(e.stage_id?stageLabel(e.stage_id):'任务')}</time><p>${escapeHtml(e.message)}</p></div>`).join('')||'<div class="empty">暂无事件</div>';}
function eventsKey(job){return JSON.stringify(job.events.map(x=>x.id));}

const clauseFilters = [
  {key:'s2', label:'S2 放行', countKey:'s2_passed'},
  {key:'s3', label:'S3 判可用', countKey:'usable'},
  {key:'s2rej', label:'S2 剔除', countKey:'s2_rejected'},
  {key:'all', label:'全部', countKey:'total'}
];
function reasonText(code){return reasonLabels[code]||code||'';}
function filterClauses(clauses,key){
  if(key==='s2')return clauses.filter(c=>c.s2_usable);
  if(key==='s3')return clauses.filter(c=>c.usable);
  if(key==='s2rej')return clauses.filter(c=>!c.s2_usable);
  return clauses;
}
function clauseRow(c,isRemix=false){
  let badge;
  if(c.usable)badge=`<span class="lb-changed">${isRemix?'保留':'AI 判可用'}</span>`;
  else if(!c.s2_usable)badge=`<span class="lb-reason">${isRemix?'字面去重':'S2'} · ${escapeHtml(reasonText(c.s2_reason))}</span>`;
  else badge=`<span class="lb-reason">${isRemix?'语义去重':'S3'} · ${escapeHtml(reasonText(c.reason)||'判为不可用')}</span>`;
  const order=c.order!=null?`<span class="lb-hit">成片第 ${c.order+1} 段</span>`:'';
  const category=isRemix&&c.category?`<span class="lb-hit">${escapeHtml(c.category)}</span>`:'';
  return `<article class="lb-row ${c.usable?'changed':'no'}"><header><span class="lb-time">${c.start.toFixed(1)}–${c.end.toFixed(1)}s · #${escapeHtml(String(c.id))}</span>${badge}${category}${order}</header><p class="lb-text">${escapeHtml(c.text||'（未识别到口播文字）')}</p></article>`;
}
function paintJobClauses(){
  const box=$('#jobClauses');const data=state.clausesData;if(!box||!data)return;
  const isRemix=data.job_type==='remix';
  if(!data.ready){box.innerHTML=`<p class="muted-empty">${isRemix?'尚未生成文字去重结果。':'尚未生成 S2/S3 结果（流程到达规则粗筛后可用）。'}</p>`;return;}
  const counts=data.counts||{},filter=state.clausesFilter||'s2';
  const rows=filterClauses(data.clauses,filter);
  const filters=isRemix?[
    {key:'s2',label:'字面去重后',countKey:'s2_passed'},
    {key:'s3',label:'最终保留',countKey:'usable'},
    {key:'s2rej',label:'字面重复',countKey:'s2_rejected'},
    {key:'all',label:'全部',countKey:'total'}
  ]:clauseFilters;
  const summary=isRemix?`原始 <b>${counts.total}</b> 段 · 字面去重后 <b>${counts.s2_passed}</b> · 最终保留 <b>${counts.usable}</b> · 语义重复 <b>${counts.rejected_by_s3}</b>`:`共 <b>${counts.total}</b> 条子句 · S2 放行 <b>${counts.s2_passed}</b> · S3 判可用 <b>${counts.usable}</b> · S3 淘汰 <b>${counts.rejected_by_s3}</b>`;
  box.innerHTML=`<div class="clause-tools">${filters.map(f=>`<button type="button" class="tab-button ${f.key===filter?'active':''}" data-clause-filter="${f.key}">${f.label} <em>${counts[f.countKey]??0}</em></button>`).join('')}<span class="lb-sel">${summary}</span></div>
  <div class="label-list clause-list">${rows.map(c=>clauseRow(c,isRemix)).join('')||'<p class="muted-empty">该分类下没有片段。</p>'}</div>`;
  $$('[data-clause-filter]',box).forEach(btn=>btn.addEventListener('click',()=>{state.clausesFilter=btn.dataset.clauseFilter;paintJobClauses()}));
}
async function loadJobClauses(jobId){
  const box=$('#jobClauses');if(!box)return;
  if(state.clausesJob===jobId&&state.clausesData){paintJobClauses();return;}
  box.innerHTML='<div class="loading"><div class="spinner"></div>读取子句判定结果</div>';
  try{
    const data=await api(`/api/jobs/${jobId}/clauses`);
    if(location.hash!==`#/jobs/${jobId}`)return;
    state.clausesJob=jobId;state.clausesData=data;state.clausesFilter='s2';
    paintJobClauses();
  }catch(err){box.innerHTML=`<p class="muted-empty">${escapeHtml(err.message)}</p>`;}
}

function jobControlsHtml(job){
  const folder=job.deliverables||{};
  return `${statusCell(job)}
    ${job.status==='completed'&&folder.exists?`${folder.folder?`<span class="folder-path" title="${escapeHtml(folder.folder)}">${escapeHtml(folder.folder)}</span>`:''}<button class="button ghost small" id="openDeliverableFolder">${icons.folder}打开成片文件夹</button>`:''}
    ${job.status==='failed'?`<button class="button ghost small" id="retryJob">重新开始</button>`:''}
    <button class="button ghost small" id="restartJob">重置任务</button>
    ${jobFlags(job).active?'<button class="button danger small" id="cancelJob">取消任务</button>':''}
    <button class="button danger small" id="deleteJob">删除任务</button>`;
}
function deliverablesControlsKey(job){return JSON.stringify([job.status,job.deliverables&&job.deliverables.exists,job.deliverables&&job.deliverables.folder]);}

function patchJobRegion(selector,html,key){const region=$(selector);if(!region||region.dataset.renderKey===key)return false;region.innerHTML=html;region.dataset.renderKey=key;return true;}
function markJobDisconnected(){const indicator=$('#jobConnectionState');if(indicator){indicator.classList.add('offline');indicator.textContent='连接已中断 · 正在重试'}const runtime=$('.runtime-state.live');if(runtime){runtime.classList.remove('live');runtime.innerHTML='<i></i>无法连接后台服务';}}
function scheduleJobPoll(jobId,job){clearTimeout(state.poll);if(jobFlags(job).active)state.poll=setTimeout(()=>{if(location.hash===`#/jobs/${jobId}`)refreshJob(jobId).catch(()=>{markJobDisconnected();scheduleJobPoll(jobId,state.job||job)})},1800);}
function bindArtifactPreviews(root=document){$$('[data-preview]',root).forEach(x=>{if(x.dataset.previewBound)return;x.dataset.previewBound='true';x.addEventListener('click',()=>previewArtifact(x.dataset.preview,x.dataset.mime,x.dataset.title))});}

function bindJobControls(jobId){
  const open=$('#openDeliverableFolder');
  if(open)open.addEventListener('click',async()=>{try{await api(`/api/jobs/${jobId}/open-folder`,{method:'POST',body:'{}'});toast('已打开成片文件夹')}catch(err){toast(err.message)}});
  const cancel=$('#cancelJob');
  if(cancel)cancel.addEventListener('click',async()=>{try{await api(`/api/jobs/${jobId}/cancel`,{method:'POST',body:'{}'});toast('任务已取消');await refreshJob(jobId)}catch(err){toast(err.message)}});
  const retry=$('#retryJob');
  if(retry)retry.addEventListener('click',async()=>{try{await api(`/api/jobs/${jobId}/retry`,{method:'POST',body:'{}'});toast('任务已重新开始');await refreshJob(jobId)}catch(err){toast(err.message)}});
  const restart=$('#restartJob');
  if(restart)restart.addEventListener('click',async()=>{
    if(!confirm('确定要重置该任务吗？所有阶段执行进度和产物将被清除，并从头重新开始。'))return;
    try{await api(`/api/jobs/${jobId}/restart`,{method:'POST',body:'{}'});toast('任务已重置');await refreshJob(jobId)}catch(err){toast(err.message)}
  });
  const del=$('#deleteJob');
  if(del)del.addEventListener('click',async()=>{
    if(!confirm('确定要彻底删除该任务吗？此操作将删除全部执行数据与产物，且不可恢复。'))return;
    try{await api(`/api/jobs/${jobId}/delete`,{method:'POST',body:'{}'});toast('任务已删除');location.hash='#/queue'}catch(err){toast(err.message)}
  });
  $$('[data-mark-delivered]').forEach(x=>x.addEventListener('click',async()=>{
    try{await api(`/api/jobs/${x.dataset.markDelivered}/delivered`,{method:'POST',body:'{}'});toast('已标记为已剪辑');await refreshJob(jobId)}catch(err){toast(err.message)}
  }));
}
function bindStageSelection(jobId){
  $$('[data-stage-select]').forEach(button=>{
    if(button.dataset.stageBound)return;button.dataset.stageBound='true';
    button.addEventListener('click',()=>{
      state.selectedStage=button.dataset.stageSelect;
      patchJobRegion('#jobWorkflow',workflowHtml(state.job),workflowKey(state.job));
      patchJobRegion('#jobStageDetail',stageDetailHtml(state.job),stageDetailKey(state.job));
      bindStageSelection(jobId);bindStageRerun(jobId);bindArtifactPreviews($('#jobStageDetail'));updateLiveTimes();
    });
  });
}
function bindStageRerun(jobId){
  $$('[data-rerun-stage]').forEach(button=>{
    if(button.dataset.rerunBound)return;button.dataset.rerunBound='true';
    button.addEventListener('click',async()=>{
      const stageId=button.dataset.rerunStage,label=stageLabel(stageId);
      const impact=stageId==='render'?'将复用现有 S3、S4 结果，仅重新渲染成片。':stageId==='filter'?'将重跑 S2，并自动继续 S3、S4、渲染。':stageId==='judge'?'将重跑 S3，并自动继续 S4、渲染。':'将重跑 S4，并自动继续渲染。';
      if(!confirm(`确定从“${label}”开始重跑吗？${impact}会一直执行到出片；历史成片文件不会删除。`))return;
      button.disabled=true;
      try{
        await api(`/api/jobs/${jobId}/rerun-stage`,{method:'POST',body:JSON.stringify({stage_id:stageId})});
        state.clausesJob=null;state.clausesData=null;toast(`${label}已加入队列`);await refreshJob(jobId);
      }catch(err){button.disabled=false;toast(err.message)}
    });
  });
}
function updateLiveTimes(){
  $$('[data-elapsed-from]').forEach(el=>{if(!el.dataset.elapsedFrom)return;const end=el.dataset.elapsedTo?new Date(el.dataset.elapsedTo):new Date();el.textContent=durationText((end-new Date(el.dataset.elapsedFrom))/1000)});
  $$('[data-relative-time]').forEach(el=>{if(!el.dataset.relativeTime)return;const seconds=Math.max(0,Math.floor((Date.now()-new Date(el.dataset.relativeTime))/1000));el.textContent=seconds<5?'刚刚':`${durationText(seconds)}前`});
}

async function refreshJob(jobId){
  clearTimeout(state.poll);if(location.hash!==`#/jobs/${jobId}`)return;
  const job=await api(`/api/jobs/${jobId}`);state.job=job;
  const connection=$('#jobConnectionState');if(connection){connection.classList.remove('offline');connection.innerHTML='<i></i>实时连接正常';}
  if(patchJobRegion('#jobControls',jobControlsHtml(job),deliverablesControlsKey(job)))bindJobControls(jobId);
  const meta=$('#jobProgressMeta');if(meta)meta.textContent=`${Math.round(job.progress||0)}% · 当前节点 ${jobStageLabel(job,job.current_stage)||'—'}`;
  if(patchJobRegion('#jobWorkflow',workflowHtml(job),workflowKey(job)))bindStageSelection(jobId);
  if(patchJobRegion('#jobStageDetail',stageDetailHtml(job),stageDetailKey(job))){bindStageRerun(jobId);bindArtifactPreviews($('#jobStageDetail'));updateLiveTimes();}
  const heartbeat=$('[data-relative-time]');if(heartbeat)heartbeat.dataset.relativeTime=job.updated_at||'';
  if(patchJobRegion('#jobArtifacts',artifactsHtml(job),artifactsKey(job)))bindArtifactPreviews($('#jobArtifacts'));
  patchJobRegion('#jobEvents',eventsHtml(job),eventsKey(job));
  const clauseSig=(job.stages||[]).map(s=>`${s.stage_id}:${s.status}`).join(',');
  if(state.clausesJob!==jobId||(state.clausesData&&state.clausesStageSig!==clauseSig)){state.clausesData=null}
  state.clausesStageSig=clauseSig;
  loadJobClauses(jobId);
  scheduleJobPoll(jobId,job);
}
async function renderJob(jobId){
  setCrumb('任务详情');loading();const job=await api(`/api/jobs/${jobId}`);
  state.job=job;state.selectedStage=job.current_stage||job.stages[0]?.stage_id;
  app.innerHTML=`<a href="#/queue" class="back-link">${icons.arrow}返回队列</a>
<div class="detail-head"><div class="detail-title"><span class="eyebrow">${escapeHtml(job.id)}</span><h1>${escapeHtml(job.title)}${jobTypeTag(job)}</h1><p>${escapeHtml(job.source_path)}${jobTargetText(job,'目标时长')}</p></div><div class="detail-actions" id="jobControls" data-render-key="${escapeHtml(deliverablesControlsKey(job))}">${jobControlsHtml(job)}</div></div>
  <div class="panel"><div class="panel-head"><div><h2>整体流程</h2><p id="jobProgressMeta">${Math.round(job.progress||0)}% · 当前节点 ${escapeHtml(jobStageLabel(job,job.current_stage)||'—')}</p></div><div class="workflow-meta"><span class="connection-state" id="jobConnectionState"><i></i>实时连接正常</span><span class="panel-hint">点击节点查看详情</span></div></div><div class="workflow" id="jobWorkflow" data-render-key="${escapeHtml(workflowKey(job))}">${workflowHtml(job)}</div></div>
  <div class="detail-grid"><div><div class="panel stage-detail" id="jobStageDetail" data-render-key="${escapeHtml(stageDetailKey(job))}">${stageDetailHtml(job)}</div>
  <div class="panel"><div class="panel-head"><div><h2>任务产物</h2><p>图片、时间线、日志与视频均可打开</p></div></div><div id="jobArtifacts" data-render-key="${escapeHtml(artifactsKey(job))}">${artifactsHtml(job)}</div></div></div>
  <div class="panel"><div class="panel-head"><div><h2>${job.job_type==='remix'?'片段去重与分组':'子句核验'}</h2><p>${job.job_type==='remix'?'展示集合时间线全部片段、文字重复项、动态分类与最终顺序':'展示 S1 全部子句、S2 规则放行与 S3 AI 判定结果，供人工逐条核对'}</p></div></div><div id="jobClauses"></div></div>
  <div class="panel"><div class="panel-head"><div><h2>实时事件</h2><p>后台局部更新，不影响滚动和操作</p></div></div><div class="timeline" id="jobEvents" data-render-key="${escapeHtml(eventsKey(job))}">${eventsHtml(job)}</div></div></div>`;
  bindJobControls(jobId);bindStageSelection(jobId);bindStageRerun(jobId);bindArtifactPreviews(app);updateLiveTimes();scheduleJobPoll(jobId,job);loadJobClauses(jobId);
}

async function previewArtifact(id,mime,title){
  const dialog=$('#previewDialog'),body=$('#previewBody'),url=`/api/artifacts/${id}/content`;body.innerHTML='<div class="loading"><div class="spinner"></div>加载产物</div>';dialog.showModal();
  if(mime.startsWith('image/'))body.innerHTML=`<img src="${url}" alt="${escapeHtml(title)}">`;
  else if(mime.startsWith('video/'))body.innerHTML=`<video src="${url}" controls autoplay></video>`;
  else {const text=await fetch(url).then(r=>r.text());body.innerHTML=`<pre>${escapeHtml(text)}</pre>`;}
}

async function renderSkill(){
  setCrumb('Skill 管理');loading();const data=await api('/api/skill');
  app.innerHTML=`<div class="hero"><div><span class="eyebrow">AGENT CONTRACT</span><h1>Skill 管理</h1><p>这份薄 Skill 只负责教第三方 Agent 如何调用 MCP，生产状态与规则由本地系统维护。</p></div><button class="button primary" id="saveSkill">保存版本</button></div>
  <div class="editor-layout"><div class="panel"><div class="panel-head"><div><h2>SKILL.md</h2><p>${escapeHtml(data.path)}</p></div><span class="status ${data.exists?'completed':'failed'}">${data.exists?'有效':'不存在'}</span></div><textarea class="code-editor" id="skillEditor" spellcheck="false">${escapeHtml(data.content)}</textarea></div>
  <div class="panel"><div class="panel-head"><div><h2>设计原则</h2><p>让 Skill 保持轻量</p></div></div><div class="info-list"><div class="info-item"><span>文件大小</span><b>${bytes(data.bytes)}</b></div><div class="info-item"><span>职责</span><b>触发、轮询、提交决策</b></div><div class="info-item"><span>不应包含</span><b>渲染实现、缓存、状态机、完整日志</b></div><div class="info-item"><span>版本保护</span><b>每次保存自动备份旧版</b></div></div></div></div>`;
  $('#saveSkill').addEventListener('click',async()=>{try{await api('/api/skill',{method:'PUT',body:JSON.stringify({content:$('#skillEditor').value})});toast('Skill 已保存并备份')}catch(e){toast(e.message)}});
}

async function renderMcp(){
  setCrumb('MCP 接入');loading();const data=await api('/api/mcp');state.mcp=data;const first=Object.keys(data.configs)[0];
  app.innerHTML=`<div class="hero"><div><span class="eyebrow">MODEL-AGNOSTIC BRIDGE</span><h1>MCP 接入</h1><p>WorkBuddy、Codex、Antigravity、OpenCode 等客户端共享同一套剪辑能力与任务状态。</p></div>${status(data.enabled?'completed':'failed')}</div><div class="detail-grid"><div><div class="panel"><div class="panel-head"><div><h2>客户端配置</h2><p>${escapeHtml(data.url)}</p></div><button class="button ghost small" id="rotateToken">轮换密钥</button></div><div class="connection-tabs">${Object.keys(data.configs).map((k,i)=>`<button class="tab-button ${i===0?'active':''}" data-config="${escapeHtml(k)}">${escapeHtml(k)}</button>`).join('')}</div><div class="config-box"><pre id="configText">${escapeHtml(JSON.stringify(data.configs[first],null,2))}</pre><button class="button ghost small copy-button" id="copyConfig">复制</button></div></div><div class="panel"><div class="panel-head"><div><h2>公开工具</h2><p>${data.tools.length} 个业务级动作</p></div></div><div class="tools-list">${data.tools.map(t=>`<div class="tool-card"><code>${escapeHtml(t.name)}</code><p>${escapeHtml(t.description)}</p></div>`).join('')}</div></div></div><div class="panel"><div class="panel-head"><div><h2>连接状态</h2><p>本地 Streamable HTTP</p></div></div><div class="info-list"><div class="info-item"><span>Endpoint</span><code>${escapeHtml(data.url)}</code></div><div class="info-item"><span>认证</span><b>Bearer Token</b></div><div class="info-item"><span>当前密钥</span><code>${escapeHtml(data.token)}</code></div><div class="info-item"><span>安全边界</span><b>默认只监听 127.0.0.1</b></div></div></div></div>`;
  $$('[data-config]').forEach(btn=>btn.addEventListener('click',()=>{$$('[data-config]').forEach(x=>x.classList.remove('active'));btn.classList.add('active');$('#configText').textContent=JSON.stringify(data.configs[btn.dataset.config],null,2)}));
  $('#copyConfig').addEventListener('click',()=>navigator.clipboard.writeText($('#configText').textContent).then(()=>toast('配置已复制')));
  $('#rotateToken').addEventListener('click',async()=>{if(!confirm('旧密钥会立即失效，继续吗？'))return;await api('/api/mcp/token',{method:'POST',body:'{}'});toast('密钥已轮换');renderMcp()});
}

async function renderSettings(){
  setCrumb('系统设置');loading();const data=await api('/api/settings');
  const opts=(map,selected)=>Object.entries(map).map(([value,label])=>`<option value="${value}" ${value===selected?'selected':''}>${label}</option>`).join('');
  const modelOpts=(provider,selected)=>{
    const models=data.ai_models?.[provider]||data.ai_models?.auto||[{id:'auto',name:'自动选择'}];
    const selectedExists=models.some(model=>model.id===selected);
    return models.map(model=>`<option value="${escapeHtml(model.id)}" ${model.id===(selectedExists?selected:'auto')?'selected':''}>${escapeHtml(model.name)}${model.id==='auto'?'':`（${escapeHtml(model.id)}）`}</option>`).join('');
  };
  const selectedProvider=data.ai_provider||'auto';
  app.innerHTML=`<div class="hero"><div><span class="eyebrow">SYSTEM CONFIGURATION</span><h1>系统设置</h1><p>配置执行内核、AI 提供方和本地接入策略。</p></div></div>
  <div class="panel"><div class="panel-head"><div><h2>运行配置</h2><p>内置引擎随应用版本一起升级</p></div></div>
  <form class="settings-form" id="settingsForm">
    <label>内置切片内核<input value="${escapeHtml(data.engine_path||'')}" readonly><small>${data.engine_bundled?'当前项目自带，不再依赖外部旧引擎目录。':'当前使用外部引擎路径。'}</small></label>
    <label>引擎 Python<input name="engine_python" value="${escapeHtml(data.engine_python||'')}"><small>建议使用项目独立的 Python 3.13 环境。</small></label>
    <div class="settings-section"><b>AI 执行策略</b><small>选择编排的引擎与提供方。</small></div>
    <label>AI 引擎<select name="ai_engine">${opts({llm:'LLM（本地 CLI）',jev:'JEV（Typesafe 云）'},data.ai_engine||'llm')}</select><small>LLM 走本地 CLI，JEV 走云端判定服务。</small></label>
    <label>AI 提供方<select name="ai_provider" id="aiProvider">${opts({auto:'自动',opencode:'OpenCode',codex:'Codex',workbuddy:'WorkBuddy',antigravity:'Antigravity'},selectedProvider)}</select><small>auto 会按可用性自动选择。</small></label>
    <label>AI 模型<select name="ai_model" id="aiModel">${modelOpts(selectedProvider,data.ai_model||'auto')}</select><small>模型选项会根据 AI 提供方自动更新。</small></label>
    <div class="settings-section"><b>JEV 云端</b><small>${data.jev_api_key_configured?'已配置密钥。':'尚未配置密钥。'}</small></div>
    <label>JEV API Key<input name="jev_api_key" value="${escapeHtml(data.jev_api_key||'')}" placeholder="留空保持现有密钥"><small>仅为安全展示，留空或保持掩码不会覆盖现有密钥。</small></label>
    <label>JEV Base URL<input name="jev_base_url" value="${escapeHtml(data.jev_base_url||'')}"></label>
    <div class="settings-section"><b>接入与 Skill</b><small>MCP 服务与 Skill 文件位置。</small></div>
    <label>Skill 路径<input name="skill_path" value="${escapeHtml(data.skill_path||'')}"></label>
    <label><input type="checkbox" name="mcp_enabled" ${data.mcp_enabled?'checked':''}> 启用 MCP 服务<small>关闭后外部 Agent 无法提交任务。</small></label>
    <p class="form-error" id="settingsError" role="alert"></p>
    <button class="button primary" type="submit">保存设置</button>
  </form></div>`;
  $('#aiProvider').addEventListener('change',e=>{$('#aiModel').innerHTML=modelOpts(e.target.value,'auto')});
  $('#settingsForm').addEventListener('submit',async e=>{
    e.preventDefault();const f=new FormData(e.target),error=$('#settingsError');error.textContent='';
    try{
      await api('/api/settings',{method:'PUT',body:JSON.stringify({engine_python:f.get('engine_python'),ai_engine:f.get('ai_engine'),ai_provider:f.get('ai_provider'),ai_model:f.get('ai_model'),jev_api_key:f.get('jev_api_key'),jev_base_url:f.get('jev_base_url'),skill_path:f.get('skill_path'),mcp_enabled:f.get('mcp_enabled')==='on'})});
      toast('设置已保存');
    }catch(err){error.textContent=err.message}
  });
}

function labelClauseText(clause){return [...(clause.text||'')];}
function labelSelToken(clauseId){const s=state.label.sel[clauseId];const clause=state.label.session?.clauses?.find(c=>String(c.id)===String(clauseId));if(!s||!clause)return '';return labelClauseText(clause).slice(s.a,s.b+1).join('').replace(/[\s，。！？、,.!?；;：:]/g,'');}
function labelClauseRow(clause){
  const d=state.label.decisions[clause.id]||{};
  const changed=d.label!==undefined&&d.label!==clause.usable;
  const s=state.label.sel[clause.id];
  const chars=labelClauseText(clause).map((ch,i)=>`<i class="lb-c${s&&i>=s.a&&i<=s.b?' on':''}" data-clause="${clause.id}" data-i="${i}">${ch===' '?'&nbsp;':escapeHtml(ch)}</i>`).join('');
  const token=labelSelToken(clause.id);
  const reason=clause.usable?'':`<span class="lb-reason">${escapeHtml(reasonLabels[clause.reason]||clause.reason||'')}</span>`;
  const hit=clause.hit?`<span class="lb-hit">命中「${escapeHtml(clause.hit)}」</span>`:'';
  const badge=changed?`<span class="lb-changed">已改判为${d.label?'合格':'不合格'}</span>`:'';
  return `<article class="lb-row${changed?' changed':''}" data-row="${clause.id}">
    <header><span class="lb-time">${(clause.start||0).toFixed(1)}–${(clause.end||0).toFixed(1)}s</span>${reason}${hit}${badge}</header>
    <p class="lb-text">${chars}</p>
    <footer><span class="lb-sel">圈词：${token?`<code>${escapeHtml(token)}</code>`:'<em>拖选文字</em>'}</span>
      <button class="button ghost small" data-label-toggle="${clause.id}">标为${clause.usable?'不合格':'合格'}</button>
      ${d.label!==undefined?`<button class="link-button" data-label-clear="${clause.id}">撤销改判</button>`:''}
    </footer></article>`;
}
function labelColumnsHtml(){
  const session=state.label.session;
  if(!session)return `<div class="empty">${icons.empty}<h3>尚未打开标注会话</h3><p>选择一段直播素材，跑一次 S1+S2，再人工核对粗筛结果。</p></div>`;
  const clauses=session.clauses||[];
  const ok=clauses.filter(c=>c.usable),bad=clauses.filter(c=>!c.usable);
  return `<div class="label-columns">
    <section class="panel"><div class="panel-head"><div><h2>S2 判合格</h2><p>${ok.length} 条 · 实际不该用就圈出误放行的词并标为不合格</p></div></div><div class="label-list" data-list="ok">${ok.map(labelClauseRow).join('')||'<p class="muted-empty">无</p>'}</div></section>
    <section class="panel"><div class="panel-head"><div><h2>S2 判不合格</h2><p>${bad.length} 条 · 实际可用就标为合格（默认移除命中词）</p></div></div><div class="label-list" data-list="bad">${bad.map(labelClauseRow).join('')||'<p class="muted-empty">无</p>'}</div></section>
  </div>`;
}
function labelPatchHtml(){
  const result=state.label.patch;
  if(!result)return '<p class="muted-empty">改判后点“生成补丁预览”，把人工结论翻译成词表增删。</p>';
  const patch=result.patch||{},un=patch.unresolved||[];
  const row=(title,arr)=>`<div class="info-item"><span>${title}</span><b>${arr&&arr.length?arr.map(x=>`<code>${escapeHtml(x)}</code>`).join(' '):'—'}</b></div>`;
  return `<div class="info-list">${row('新增硬禁词',patch.hard_add)}${row('移除硬禁词',patch.hard_remove)}${row('新增硬禁正则',patch.hard_regex_add)}${row('移除硬禁正则',patch.hard_regex_remove)}</div>
  ${un.length?`<div class="node-section-title"><b>无法自动成规</b><span>${un.length} 条</span></div><div class="label-list">${un.map(u=>`<article class="lb-row"><header><span class="lb-time">#${escapeHtml(u.id)}</span><span class="lb-reason">${escapeHtml(reasonLabels[u.reason]||u.reason||'')}</span></header><p class="lb-text">${escapeHtml(u.text)}</p><footer><span class="lb-sel">${escapeHtml(u.detail)}</span></footer></article>`).join('')}</div>`:''}
  ${result.applied?`<div class="service-warning"><b>补丁已入库并热加载</b><span>生效后硬禁词 ${result.applied.summary.hard} · 硬禁正则 ${result.applied.summary.hard_regex}（账号级基础词表 + 标注补丁）</span></div>`:''}`;
}
function labelProfileHtml(profile){
  const o=profile.overrides||{},s=profile.summary||{};
  return `<div class="info-list">
    <div class="info-item"><span>当前词表来源</span><code>${escapeHtml(s.profile||'通用默认（未加载覆盖）')}</code></div>
    <div class="info-item"><span>生效硬禁词 / 正则</span><b>${s.hard||0} / ${s.hard_regex||0}</b></div>
    <div class="info-item"><span>标注新增</span><b>${(o.hard_add||[]).map(x=>`<code>${escapeHtml(x)}</code>`).join(' ')||'—'}</b></div>
    <div class="info-item"><span>标注移除</span><b>${(o.hard_remove||[]).map(x=>`<code>${escapeHtml(x)}</code>`).join(' ')||'—'}</b></div>
  </div>`;
}
function paintLabelSelection(){
  const session=state.label.session;if(!session)return;
  $$('.lb-c').forEach(el=>{const s=state.label.sel[el.dataset.clause];el.classList.toggle('on',!!s&&+el.dataset.i>=s.a&&+el.dataset.i<=s.b)});
  $$('[data-row]').forEach(row=>{const id=row.dataset.row,span=row.querySelector('.lb-sel'),token=labelSelToken(id);if(span)span.innerHTML=`圈词：${token?`<code>${escapeHtml(token)}</code>`:'<em>拖选文字</em>'}`});
}
function bindLabelSelection(){
  let dragging=false,active=null;
  $$('.lb-c').forEach(el=>{
    el.addEventListener('mousedown',e=>{e.preventDefault();dragging=true;active=el.dataset.clause;const i=+el.dataset.i;state.label.sel[active]={a:i,b:i};paintLabelSelection()});
    el.addEventListener('mouseenter',()=>{if(!dragging||el.dataset.clause!==active)return;const s=state.label.sel[active];if(!s)return;let i=+el.dataset.i;if(i<s.a)s.a=i;else s.b=i;paintLabelSelection()});
  });
  document.addEventListener('mouseup',()=>{dragging=false;active=null});
}
function bindLabel(){
  $$('[data-label-toggle]').forEach(btn=>btn.addEventListener('click',async()=>{
    const id=btn.dataset.labelToggle,clause=state.label.session.clauses.find(c=>String(c.id)===String(id));if(!clause)return;
    const target=!clause.usable;
    const current=state.label.decisions[id];
    if(current&&current.label===target){delete state.label.decisions[id]}else{state.label.decisions[id]={label:target,tokens:labelSelToken(id)?[labelSelToken(id)]:[],regex:false}}
    state.label.patch=null;renderLabelRegions();await saveLabelDecisions();
  }));
  $$('[data-label-clear]').forEach(btn=>btn.addEventListener('click',async()=>{delete state.label.decisions[btn.dataset.labelClear];state.label.patch=null;renderLabelRegions();await saveLabelDecisions()}));
}
function renderLabelRegions(){
  const scroll={};$$('#labelColumns .label-list').forEach(el=>{if(el.dataset.list)scroll[el.dataset.list]=el.scrollTop});
  patchJobRegion('#labelColumns',labelColumnsHtml(),JSON.stringify([state.label.decisions,state.label.sel]));
  $$('#labelColumns .label-list').forEach(el=>{if(el.dataset.list&&scroll[el.dataset.list]!=null)el.scrollTop=scroll[el.dataset.list]});
  patchJobRegion('#labelPatch',labelPatchHtml(),JSON.stringify(state.label.patch));
  bindLabel();bindLabelSelection();
  const meta=$('#labelStats');if(meta){const changed=Object.keys(state.label.decisions).length;meta.textContent=`${changed} 条已改判`}
}
async function saveLabelDecisions(){
  const session=state.label.session;if(!session)return;
  try{await api(`/api/label/sessions/${session.id}`,{method:'PUT',body:JSON.stringify({decisions:state.label.decisions})})}catch(err){toast(err.message)}
}
async function renderLabel(){
  setCrumb('S2 标注');loading();
  const [sessions,profile]=await Promise.all([api('/api/label/sessions'),api('/api/label/profile')]);
  const session=state.label.session;
  app.innerHTML=`<div class="hero"><div><span class="eyebrow">S2 RULE TUNING</span><h1>S2 标注工作台</h1><p>对素材跑 S1+S2，人工核对粗筛结论；圈出判错的词，生成并应用词表补丁（仅作用于规则粗筛）。</p></div><div class="detail-actions"><button class="button ghost" id="labelDiscard">删除会话</button><button class="button primary" id="labelPreview">生成补丁预览</button><button class="button primary" id="labelApply">应用补丁</button></div></div>
  <div class="panel"><div class="panel-head"><div><h2>标注素材</h2><p id="labelStats">${session?`${session.id} · ${session.clauses.length} 条子句`:'选择直播素材开始'}</p></div><div class="label-source"><input id="labelSource" placeholder="选择视频或粘贴绝对路径" value="${session?escapeHtml(session.source_path):''}"><button class="button ghost small" id="labelBrowse">浏览</button><button class="button primary small" id="labelStart">开始标注</button></div></div>
    ${sessions.sessions.length?`<div class="label-sessions">历史会话：${sessions.sessions.slice(0,8).map(s=>`<button class="link-button" data-open-label="${escapeHtml(s.id)}">${escapeHtml(s.created_at||s.id)}（${s.clause_count}）</button>`).join('')}</div>`:''}
  </div>
  <div id="labelColumns">${labelColumnsHtml()}</div>
  <div class="panel"><div class="panel-head"><div><h2>规则补丁预览</h2><p>只把「人工圈词」翻译成词表增删，结构性命中不入规则</p></div></div><div id="labelPatch">${labelPatchHtml()}</div></div>
  <div class="panel"><div class="panel-head"><div><h2>当前生效词表</h2><p>补丁应用后立即热加载，直接影响后续 S2 粗筛</p></div></div><div id="labelProfile">${labelProfileHtml(profile)}</div></div>`;
  bindLabel();bindLabelSelection();
  $('#labelBrowse')?.addEventListener('click',async()=>{try{const r=await api('/api/files/pick',{method:'POST',body:JSON.stringify({kind:'video'})});if(!r.cancelled)$('#labelSource').value=r.path}catch(err){toast(err.message)}});
  $('#labelStart')?.addEventListener('click',async()=>{
    const source=$('#labelSource').value.trim();if(!source)return toast('请先选择素材');
    const btn=$('#labelStart');btn.disabled=true;btn.textContent='转写中…';
    try{const created=await api('/api/label/sessions',{method:'POST',body:JSON.stringify({source_path:source})});state.label={session:created,decisions:created.decisions||{},sel:{},patch:null};toast(`已生成 ${created.clauses.length} 条子句`);await renderLabel()}
    catch(err){toast(err.message)}finally{btn.disabled=false;btn.textContent='开始标注'}
  });
  $('#labelPreview')?.addEventListener('click',async()=>{
    if(!state.label.session)return toast('请先开始标注');
    try{state.label.patch=await api(`/api/label/sessions/${state.label.session.id}/patch`,{method:'POST',body:JSON.stringify({decisions:state.label.decisions,apply:false})});renderLabelRegions();const p=state.label.patch.patch;toast(`补丁：+${p.hard_add.length} / -${p.hard_remove.length}，${p.unresolved.length} 条待人工`)}catch(err){toast(err.message)}
  });
  $('#labelApply')?.addEventListener('click',async()=>{
    if(!state.label.session)return toast('请先开始标注');
    if(!confirm('将把补丁写入数据库并立即生效（账号级基础词表 + 标注补丁），影响后续 S2 粗筛。继续吗？'))return;
    try{state.label.patch=await api(`/api/label/sessions/${state.label.session.id}/patch`,{method:'POST',body:JSON.stringify({decisions:state.label.decisions,apply:true})});renderLabelRegions();const prof=await api('/api/label/profile');const box=$('#labelProfile');if(box)box.innerHTML=labelProfileHtml(prof);toast('补丁已应用')}catch(err){toast(err.message)}
  });
  $('#labelDiscard')?.addEventListener('click',async()=>{
    if(!state.label.session)return toast('没有可删除的会话');
    if(!confirm('仅删除这次标注会话，已应用的词表补丁不受影响。继续吗？'))return;
    try{await api(`/api/label/sessions/${state.label.session.id}`,{method:'DELETE'});state.label={session:null,decisions:{},sel:{},patch:null};toast('会话已删除');await renderLabel()}catch(err){toast(err.message)}
  });
  $$('[data-open-label]').forEach(btn=>btn.addEventListener('click',async()=>{try{const session=await api(`/api/label/sessions/${btn.dataset.openLabel}`);state.label={session,decisions:session.decisions||{},sel:{},patch:null};await renderLabel()}catch(err){toast(err.message)}}));
}

function syncDurationChips(){
  const minVal = $('#newJobForm [name="target_min"]')?.value;
  const maxVal = $('#newJobForm [name="target_max"]')?.value;
  $$('#durationPresets .preset-chip').forEach(chip => {
    if (chip.dataset.min === minVal && chip.dataset.max === maxVal) chip.classList.add('active');
    else chip.classList.remove('active');
  });
}

async function openNewJob(){
  $('#newJobError').textContent='';
  const minInput = $('#newJobForm [name="target_min"]');
  const maxInput = $('#newJobForm [name="target_max"]');
  if (minInput && !minInput.value) minInput.value = '70';
  if (maxInput && !maxInput.value) maxInput.value = '90';
  syncDurationChips();
  const exportInputs = [
    $('#newJobForm [name="export_dir"]'),
    $('#newJobForm [name="timeline_export_dir"]')
  ].filter(Boolean);
  if (exportInputs.some(input=>!input.value)) {
    try {
      const settings = await api('/api/settings');
      if (settings.export_dir) exportInputs.forEach(input=>{if(!input.value)input.value=settings.export_dir});
    }
    catch (err) {}
  }
  $('#newJobDialog').showModal();
}
function bindCommon(){
  $$('[data-new-job]').forEach(x=>x.addEventListener('click',openNewJob));
  $$('[data-open-job]').forEach(x=>x.addEventListener('click',()=>{location.hash=`#/jobs/${x.dataset.openJob}`}));
  $$('[data-open-folder]').forEach(x=>x.addEventListener('click',async e=>{
    e.stopPropagation();
    try{await api(`/api/jobs/${x.dataset.openFolder}/open-folder`,{method:'POST',body:'{}'});toast('已打开成片文件夹')}catch(err){toast(err.message)}
  }));
  $$('[data-restart-job]').forEach(x=>x.addEventListener('click',async e=>{
    e.stopPropagation();const jobId=x.dataset.restartJob,title=x.dataset.jobTitle||jobId;
    if(!confirm(`确定要重置任务“${title}”吗？此操作将清除全部执行进度与产物。`))return;
    try{await api(`/api/jobs/${jobId}/restart`,{method:'POST',body:'{}'});toast('任务已重置');route()}catch(err){toast(err.message)}
  }));
  $$('[data-delete-job]').forEach(x=>x.addEventListener('click',async e=>{
    e.stopPropagation();const jobId=x.dataset.deleteJob,title=x.dataset.jobTitle||jobId;
    if(!confirm(`确定要彻底删除任务“${title}”吗？此操作不可恢复。`))return;
    try{await api(`/api/jobs/${jobId}/delete`,{method:'POST',body:'{}'});toast('任务已删除');route()}catch(err){toast(err.message)}
  }));
  $$('[data-mark-delivered]').forEach(x=>x.addEventListener('click',async e=>{
    e.stopPropagation();
    try{await api(`/api/jobs/${x.dataset.markDelivered}/delivered`,{method:'POST',body:'{}'});toast('已标记为已剪辑');route()}catch(err){toast(err.message)}
  }));
}
async function route(){
  clearTimeout(state.poll);const hash=location.hash||'#/dashboard';
  if(!hash.startsWith('#/jianying')&&state.jianyingAbort){state.jianyingAbort.abort();state.jianyingAbort=null;}
  try{
    if(hash.startsWith('#/jobs/'))return await renderJob(hash.split('/')[2]);
    if(hash==='#/queue')return await renderQueue();
    if(hash==='#/jianying')return await renderJianyingDrafts();
    if(hash==='#/viral-v2')return await renderViralV2();
    if(hash==='#/label')return await renderLabel();
    if(hash==='#/skill')return await renderSkill();
    if(hash==='#/mcp')return await renderMcp();
    if(hash==='#/settings')return await renderSettings();
    return await renderDashboard();
  }catch(e){app.innerHTML=`<div class="danger-box">${escapeHtml(e.message)}</div>`;}
}

$('#newJobButton').addEventListener('click',openNewJob);
$$('[data-close-new-job]').forEach(button=>button.addEventListener('click',()=>$('#newJobDialog').close()));
$('#newJobDialog').addEventListener('cancel',e=>{e.preventDefault();$('#newJobDialog').close()});
$('#newJobDialog').addEventListener('click',e=>{if(e.target===$('#newJobDialog'))$('#newJobDialog').close()});

function setJobMode(mode){
  const isTimeline = mode === 'timeline';
  const isRemix = mode === 'remix';
  const usesTimeline = isTimeline || isRemix;
  const secDirect = $('#sectionDirectMode'), secTimeline = $('#sectionTimelineMode');
  if(secDirect) secDirect.style.display = usesTimeline ? 'none' : 'grid';
  if(secTimeline) secTimeline.style.display = usesTimeline ? 'grid' : 'none';
  if($('#timelineProductField'))$('#timelineProductField').style.display=isRemix?'none':'grid';
  if($('#timelineExportModeField'))$('#timelineExportModeField').style.display=isRemix?'none':'grid';
  if($('#timelineDurationSection'))$('#timelineDurationSection').style.display=isRemix?'none':'grid';
  if($('#remixOptions'))$('#remixOptions').style.display=isRemix?'grid':'none';
  if($('#timelineSectionTitle'))$('#timelineSectionTitle').textContent=isRemix?'同商品成片集合时间线':'剪映虚拟时间线';
  if($('#timelineSectionHint'))$('#timelineSectionHint').textContent=isRemix?'选择已经汇总多个已发布成片片段的剪映时间线':'选择剪映草稿目录、draft_content.json 或时间线 JSON';
  if($('#timelineFileHint'))$('#timelineFileHint').textContent=isRemix?'每个现有剪映片段保持完整，只依据口播文字去重与重排。':'AI 仅在剪映保留的 30–40 分钟内分析，底层原素材文件自动隐藏。';
  if($('#timelineTitleInput')){
    const input=$('#timelineTitleInput');
    input.placeholder=isRemix?'留空则使用“时间线标题_成片重组”':'留空则使用时间线标题（如：F家限定 / 时间线01）';
    if(input.dataset.userEdited!=='true'&&input.value){
      if(isRemix&&!input.value.endsWith('_成片重组'))input.value=`${input.value}_成片重组`;
      if(!isRemix&&input.value.endsWith('_成片重组'))input.value=input.value.slice(0,-5);
    }
  }
  if($('#timelineTitleHint'))$('#timelineTitleHint').textContent=isRemix?'生成新的 MP4 成片，不修改原集合时间线。':'最终输出为“成片名称.mp4”。';
}
$$('input[name="job_mode_select"]').forEach(radio => radio.addEventListener('change', e => {
  setJobMode(e.target.value);
  $('#newJobError').textContent = '';
}));

async function inspectTimeline(path){
  if(!path) return;
  const resultBox = $('#timelineInspectResult');
  const titleInput = $('#timelineTitleInput');
  const error = $('#newJobError');
  try {
    const data = await api('/api/timeline/inspect', {method:'POST', body: JSON.stringify({path})});
    resultBox.innerHTML = `<div class="timeline-inspect-head"><span class="timeline-inspect-title">${escapeHtml(data.title)}</span><span class="timeline-inspect-badge">${escapeHtml(data.duration_text)} · ${data.segment_count} 个片段</span></div><div class="timeline-inspect-meta">底层素材：${data.source_count} 个文件（对 AI 与成片展示隐藏）</div>`;
    resultBox.style.display = 'grid';
    const mode=$('input[name="job_mode_select"]:checked')?.value||'direct';
    if(titleInput && titleInput.dataset.userEdited !== 'true') titleInput.value = mode==='remix'?`${data.title}_成片重组`:data.title;
    error.textContent = '';
  } catch(err) {
    resultBox.style.display = 'none';
    error.textContent = err.message;
  }
}

function timelineOptionLabel(item){
  const speeds=(item.speeds||[]).map(x=>`${x}x`).join('/');
  return `${item.name} · ${item.duration_text} · ${item.segment_count} 个片段${speeds?` · ${speeds}`:''}`;
}

async function loadTimelineChoices(path){
  if(!path)return;
  const input=$('#timelineDraftInput');
  const selector=$('#timelineSelect');
  const field=$('#timelineSelectorField');
  input.dataset.selectedPath='';
  try{
    const listing=await api('/api/timeline/list',{method:'POST',body:JSON.stringify({path})});
    const timelines=listing.timelines||[];
    selector.innerHTML='';
    timelines.forEach(item=>{
      const option=document.createElement('option');
      option.value=item.path;
      option.textContent=timelineOptionLabel(item);
      option.dataset.timelineId=item.timeline_id;
      selector.appendChild(option);
    });
    const strategy=input.dataset.selectStrategy;
    const selected=strategy==='longest'
      ? timelines.reduce((best,item)=>!best||Number(item.timeline_duration||0)>Number(best.timeline_duration||0)?item:best,null)
      : timelines.find(item=>item.selected)||timelines.find(item=>item.active)||timelines[0];
    if(!selected)throw new Error('草稿中没有找到可用时间线');
    selector.value=selected.path;
    input.dataset.selectedPath=selected.path;
    field.style.display=timelines.length>1?'grid':'none';
    await inspectTimeline(selected.path);
  }catch(err){
    field.style.display='none';
    selector.innerHTML='';
    input.dataset.selectedPath=path;
    await inspectTimeline(path);
  }
}

async function openNewJobForDraft(draft,defaults={}){
  await openNewJob();
  const radio=$('input[name="job_mode_select"][value="timeline"]');
  if(radio)radio.checked=true;
  setJobMode('timeline');
  const input=$('#timelineDraftInput'),title=$('#timelineTitleInput');
  input.value=draft.path;
  input.dataset.selectStrategy='longest';
  title.value=draft.suggested_title;
  title.dataset.userEdited='true';
  title.dataset.autoTitle='true';
  title.dataset.draftName=draft.name;
  $('#newJobForm [name="timeline_target_min"]').value=String(defaults.target_min||120);
  $('#newJobForm [name="timeline_target_max"]').value=String(defaults.target_max||180);
  $('#newJobForm [name="timeline_export_dir"]').value=defaults.export_dir||'D:\\切片\\袁艺灵\\AI粗筛视频';
  $('#newJobForm [name="timeline_export_mode"]').value=defaults.export_mode||'segments';
  syncTimelineDurationChips();
  await loadTimelineChoices(draft.path);
  title.value=draft.suggested_title;
}

$('#newJobForm [name="title"]')?.addEventListener('input',e=>{e.target.dataset.userEdited=e.target.value?'true':''});
$('#timelineTitleInput')?.addEventListener('input', e=>{e.target.dataset.userEdited=e.target.value?'true':'';e.target.dataset.autoTitle='';});
$('#timelineDraftInput')?.addEventListener('change', e=>loadTimelineChoices(e.target.value.trim()));
$('#timelineSelect')?.addEventListener('change',e=>{
  const input=$('#timelineDraftInput');
  input.dataset.selectedPath=e.target.value;
  inspectTimeline(e.target.value);
});

$$('#durationPresets .preset-chip').forEach(chip => {
  chip.addEventListener('click', () => {
    const minInput = $('#newJobForm [name="target_min"]');
    const maxInput = $('#newJobForm [name="target_max"]');
    if (minInput) minInput.value = chip.dataset.min;
    if (maxInput) maxInput.value = chip.dataset.max;
    syncDurationChips();
  });
});
$('#newJobForm [name="target_min"]')?.addEventListener('input', syncDurationChips);
$('#newJobForm [name="target_max"]')?.addEventListener('input', syncDurationChips);

function syncTimelineDurationChips(){
  const min=$('#newJobForm [name="timeline_target_min"]')?.value;
  const max=$('#newJobForm [name="timeline_target_max"]')?.value;
  $$('#timelineDurationPresets .preset-chip').forEach(c=>c.classList.toggle('active',c.dataset.min===min&&c.dataset.max===max));
}
$$('#timelineDurationPresets .preset-chip').forEach(chip => {
  chip.addEventListener('click', () => {
    $('#newJobForm [name="timeline_target_min"]').value = chip.dataset.min;
    $('#newJobForm [name="timeline_target_max"]').value = chip.dataset.max;
    syncTimelineDurationChips();
  });
});
$('#newJobForm [name="timeline_target_min"]')?.addEventListener('input', syncTimelineDurationChips);
$('#newJobForm [name="timeline_target_max"]')?.addEventListener('input', syncTimelineDurationChips);

$('#pickTimelineButton')?.addEventListener('click', async e=>{
  const button=e.currentTarget, input=$('#timelineDraftInput');
  button.disabled=true;button.textContent='选择中…';$('#newJobError').textContent='';
  try{
    const result=await api('/api/files/pick',{method:'POST',body:JSON.stringify({kind:'timeline'})});
    if(result.cancelled) return;
    input.value = result.path;
    await loadTimelineChoices(result.path);
  } catch(err){$('#newJobError').textContent=err.message}
  finally{button.disabled=false;button.textContent='浏览草稿';}
});
$('#timelineFileDrop')?.addEventListener('click', e=>{if(!e.target.closest('#pickTimelineButton'))$('#pickTimelineButton').click();});

$('#pickSourceButton').addEventListener('click',async e=>{
  const button=e.currentTarget,source=$('#newJobForm [name="source_path"]'),title=$('#newJobForm [name="title"]');
  button.disabled=true;button.textContent='选择中…';$('#newJobError').textContent='';
  try{const result=await api('/api/files/pick',{method:'POST',body:JSON.stringify({kind:'video'})});if(result.cancelled)return;source.value=result.path;if(title.dataset.userEdited!=='true')title.value=result.name;}
  catch(err){$('#newJobError').textContent=err.message}finally{button.disabled=false;button.textContent='浏览';}
});
$('#fileDrop').addEventListener('click',e=>{if(!e.target.closest('#pickSourceButton'))$('#pickSourceButton').click()});

$('#pickExportButton').addEventListener('click',async e=>{
  const button=e.currentTarget,input=$('#newJobForm [name="export_dir"]');
  button.disabled=true;button.textContent='选择中…';$('#newJobError').textContent='';
  try{const result=await api('/api/files/pick',{method:'POST',body:JSON.stringify({kind:'dir'})});if(result.cancelled)return;input.value=result.path;}
  catch(err){$('#newJobError').textContent=err.message}finally{button.disabled=false;button.textContent='选择文件夹';}
});
$('#exportDrop').addEventListener('click',e=>{if(!e.target.closest('#pickExportButton'))$('#pickExportButton').click()});

$('#pickTimelineExportButton')?.addEventListener('click',async e=>{
  const button=e.currentTarget,input=$('#newJobForm [name="timeline_export_dir"]');
  button.disabled=true;button.textContent='选择中…';$('#newJobError').textContent='';
  try{const result=await api('/api/files/pick',{method:'POST',body:JSON.stringify({kind:'dir'})});if(result.cancelled)return;input.value=result.path;}
  catch(err){$('#newJobError').textContent=err.message}finally{button.disabled=false;button.textContent='选择文件夹';}
});
$('#timelineExportDrop')?.addEventListener('click',e=>{if(!e.target.closest('#pickTimelineExportButton'))$('#pickTimelineExportButton').click()});

$('#newJobForm').addEventListener('submit',async e=>{
  e.preventDefault();const submit=$('#createJobSubmit'),error=$('#newJobError');submit.disabled=true;submit.textContent='正在创建…';error.textContent='';
  try{
    const mode = $('input[name="job_mode_select"]:checked')?.value || 'direct';
    let payload;
    if (mode === 'timeline' || mode === 'remix') {
      const timelineInput = $('#timelineDraftInput');
      const draftPath = timelineInput?.dataset.selectedPath || timelineInput?.value.trim();
      if(!draftPath){ error.textContent = '请选择剪映草稿目录或时间线 JSON'; submit.disabled=false; submit.textContent='加入队列'; return; }
      const title = $('#timelineTitleInput')?.value.trim();
      if(mode==='remix'){
        payload={
          job_type:'remix',draft_path:draftPath,title:title,
          export_mode:'merge',export_dir:$('#newJobForm [name="timeline_export_dir"]')?.value||'',
          dedupe_strength:$('#newJobForm [name="remix_dedupe_strength"]')?.value||'standard'
        };
      }else{
        const targetMin = parseFloat($('#newJobForm [name="timeline_target_min"]')?.value) || 70;
        const targetMax = parseFloat($('#newJobForm [name="timeline_target_max"]')?.value) || 90;
        if (targetMin <= 0) throw new Error('最短时长必须大于 0 秒');
        if (targetMax < targetMin) throw new Error('最长时长不能小于最短时长');
        payload = {
           job_type: 'timeline',
           draft_path: draftPath,
           title: title,
           draft_name: $('#timelineTitleInput')?.dataset.draftName || '',
           auto_title: $('#timelineTitleInput')?.dataset.autoTitle === 'true',
           product_name: $('#newJobForm [name="timeline_product_name"]')?.value || '',
          export_mode: $('#newJobForm [name="timeline_export_mode"]')?.value || 'merge',
          export_dir: $('#newJobForm [name="timeline_export_dir"]')?.value || '',
          target_min: targetMin,
          target_max: targetMax,
          target_seconds: `${targetMin}-${targetMax}`
         };
       }
    } else {
      const f=new FormData(e.target);
      const sourcePath = f.get('source_path')?.trim();
      if(!sourcePath){ error.textContent = '请选择视频或粘贴绝对路径'; submit.disabled=false; submit.textContent='加入队列'; return; }
      const targetMin = parseFloat(f.get('target_min')) || 70;
      const targetMax = parseFloat(f.get('target_max')) || 90;
      if (targetMin <= 0) throw new Error('最短时长必须大于 0 秒');
      if (targetMax < targetMin) throw new Error('最长时长不能小于最短时长');
      payload = {
        job_type: 'direct',
        source_path: sourcePath,
        title: f.get('title')||'',
        product_name: f.get('product_name')||'',
        export_mode: f.get('export_mode')||'merge',
        export_dir: f.get('export_dir')||'',
        target_min: targetMin,
        target_max: targetMax,
        target_seconds: `${targetMin}-${targetMax}`
      };
    }
    const data=await api('/api/jobs',{method:'POST',body:JSON.stringify(payload)});
    $('#newJobDialog').close();
    e.target.reset();
    $('#newJobForm [name="target_min"]').value = '70';
    $('#newJobForm [name="target_max"]').value = '90';
    $('#newJobForm [name="timeline_target_min"]').value = '70';
    $('#newJobForm [name="timeline_target_max"]').value = '90';
    syncDurationChips();
    syncTimelineDurationChips();
    $('#newJobForm [name="title"]').dataset.userEdited='';
    if($('#timelineTitleInput')) $('#timelineTitleInput').dataset.userEdited='';
    if($('#timelineTitleInput')) { $('#timelineTitleInput').dataset.autoTitle=''; $('#timelineTitleInput').dataset.draftName=''; }
    if($('#timelineInspectResult')) $('#timelineInspectResult').style.display='none';
    if($('#timelineSelectorField')) $('#timelineSelectorField').style.display='none';
    if($('#timelineSelect')) $('#timelineSelect').innerHTML='';
    if($('#timelineDraftInput')) $('#timelineDraftInput').dataset.selectedPath='';
    if($('#timelineDraftInput')) $('#timelineDraftInput').dataset.selectStrategy='';
    setJobMode('direct');
    location.hash=`#/jobs/${data.id}`;
    toast('任务已加入队列');
  }
  catch(err){error.textContent=err.message}
  finally{submit.disabled=false;submit.textContent='加入队列';}
});
$('#previewDialog .preview-close').addEventListener('click',()=>{$('#previewDialog video')?.pause();$('#previewDialog').close()});
$('#menuButton').addEventListener('click',()=>$('.sidebar').classList.toggle('open'));
window.addEventListener('hashchange',()=>{$('.sidebar').classList.remove('open');route()});
setInterval(()=>{$('#clock').textContent=new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date());updateLiveTimes()},1000);
api('/api/health').then(x=>$('#systemVersion').textContent=`v${x.version} · MCP online`).catch(()=>$('#systemVersion').textContent='连接失败');
route();

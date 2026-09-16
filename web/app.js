const $ = (selector, root=document) => root.querySelector(selector);
const $$ = (selector, root=document) => [...root.querySelectorAll(selector)];
const app = $('#app');
const state = { dashboard:null, job:null, poll:null, mcp:null };
const labels = {queued:'排队中',running:'执行中',waiting_input:'待决策',completed:'已完成',failed:'失败',cancelled:'已取消',pending:'等待',succeeded:'完成'};
const stageLabels = {material_index:'素材索引',edit_plan:'AI 音画编排',validation:'校验与自动修复',rough_cut:'低清粗剪与审片',delivery:'高清导出与 QC'};
const icons = {
  video:'<svg viewBox="0 0 24 24"><rect x="3" y="5" width="14" height="14" rx="2"/><path d="m17 10 4-2v8l-4-2z"/></svg>',
  file:'<svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h5M9 13h6M9 17h6"/></svg>',
  image:'<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m4 17 5-5 4 4 2-2 5 4"/></svg>',
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
function bytes(value=0){if(value<1024)return `${value} B`;if(value<1048576)return `${(value/1024).toFixed(1)} KB`;return `${(value/1048576).toFixed(1)} MB`;}
function clipTime(value=0){const seconds=Math.max(0,Number(value)||0),m=Math.floor(seconds/60),s=Math.floor(seconds%60);return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;}
function status(value){return `<span class="status ${value}">${labels[value]||value}</span>`;}
function toast(message){const el=$('#toast');el.textContent=message;el.classList.add('show');clearTimeout(el.timer);el.timer=setTimeout(()=>el.classList.remove('show'),2400);}
function loading(){app.innerHTML='<div class="loading"><div class="spinner"></div>正在读取本地任务状态</div>';}
function setCrumb(text){$('#pageCrumb').textContent=text;$$('[data-nav]').forEach(x=>x.classList.toggle('active',location.hash.includes(x.dataset.nav)));}

function jobRows(jobs){
  if(!jobs.length)return `<div class="empty">${icons.empty}<h3>还没有剪辑任务</h3><p>添加第一段直播素材，流程节点、日志和产物会在这里持续更新。</p><button class="button primary" data-new-job>新建剪辑任务</button></div>`;
  return `<table class="jobs-table"><thead><tr><th>任务</th><th>当前节点</th><th>进度</th><th>状态</th><th></th></tr></thead><tbody>${jobs.map(j=>`<tr>
    <td><div class="job-name"><span class="job-thumb">${icons.video}</span><div><b>${escapeHtml(j.title)}</b><small>${escapeHtml(j.source_path)}</small></div></div></td>
    <td><small>${escapeHtml(stageLabels[j.current_stage]||j.current_stage||'—')}</small></td>
    <td><div class="progress"><div class="progress-line"><i style="width:${Math.max(2,j.progress||0)}%"></i></div><small>${Math.round(j.progress||0)}% · ${formatTime(j.updated_at)}</small></div></td>
    <td>${status(j.status)}</td><td><button class="link-button" data-open-job="${j.id}">查看详情 →</button></td></tr>`).join('')}</tbody></table>`;
}

async function renderDashboard(){
  setCrumb('任务总览');loading();const data=await api('/api/dashboard');state.dashboard=data;$('#queueBadge').textContent=data.active;
  const running=data.counts.running||0, waiting=data.counts.waiting_input||0, done=data.counts.completed||0;
  app.innerHTML=`<div class="hero"><div><span class="eyebrow">PRODUCTION CONTROL</span><h1>今天，从值得看的内容开始</h1><p>统一管理素材分析、创意决策、粗剪审片和最终成片。每一步都可见、可暂停、可追溯。</p></div><button class="button primary" data-new-job>${icons.video}添加直播素材</button></div>
  <div class="stat-grid"><div class="stat-card"><span>队列任务</span><strong>${data.active}</strong><small>等待或正在执行</small></div><div class="stat-card"><span>执行中</span><strong>${running}</strong><small>本地工作进程</small></div><div class="stat-card"><span>需要决策</span><strong>${waiting}</strong><small>等待外部 Agent</small></div><div class="stat-card"><span>已完成</span><strong>${done}</strong><small>可查看与播放</small></div></div>
  <div class="panel"><div class="panel-head"><div><h2>最近任务</h2><p>按最后创建时间排列</p></div><a class="link-button" href="#/queue">查看全部</a></div>${jobRows(data.jobs.slice(0,8))}</div>`;
  bindCommon();
}

async function renderQueue(){setCrumb('剪辑队列');loading();const data=await api('/api/jobs');app.innerHTML=`<div class="hero"><div><span class="eyebrow">JOB QUEUE</span><h1>剪辑队列</h1><p>本地串行执行重型视频任务，避免转写与高清渲染争抢资源。</p></div><button class="button primary" data-new-job>添加任务</button></div><div class="panel"><div class="panel-head"><div><h2>全部任务</h2><p>${data.jobs.length} 个任务</p></div></div>${jobRows(data.jobs)}</div>`;bindCommon();}

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
function candidateRole(category){return ({hook:'hook',result:'result',color:'color',craft:'craft',material:'material',fit:'fit',styling:'styling',scene:'scene',demo:'demo',close:'close',pain:'pain',proof:'proof'}[category]||'bridge');}
function categoryLabel(category){return ({hook:'钩子',result:'效果',color:'颜色',craft:'工艺',material:'面料',fit:'版型',styling:'搭配',scene:'场景',demo:'展示',close:'收尾',pain:'痛点',proof:'佐证',other:'讲解'}[category]||category||'讲解');}
function decisionPanel(job,packet){
  const candidates=packet.candidate_digest||[];
  const overview=(packet.artifacts||[]).find(a=>(a.mime_type||'').startsWith('image/')&&(a.title||'').includes('概览'));
  const prompt=`调用 live-slicer 的 get_stage_packet 读取任务 ${job.id}，按照 LiveCut Skill 选择一个最强成片方案，并用 submit_stage_payload 提交 main_product 和 picks。默认只做 1 个钩子，不要重复使用同一画面，不要新建任务。`;
  return `<section class="decision-panel" aria-labelledby="decisionTitle"><div class="decision-head"><div><span class="eyebrow">ACTION REQUIRED</span><h2 id="decisionTitle">需要完成音画编排</h2><p>素材分析已经完成。请选择一种方式提交方案，提交后流程才会继续。</p></div><span class="status waiting_input">等待你的决策</span></div>
    <div class="agent-handoff"><div><b>让 WorkBuddy、Codex、Multica 等外部 Agent 决策</b><p>在已连接 LiveCut MCP 的客户端中发送下面这条指令，Agent 会读取候选、完成取舍并自动提交。</p><code>${escapeHtml(prompt)}</code></div><div class="handoff-actions"><button class="button primary" id="copyAgentPrompt">复制执行指令</button><a class="button ghost" href="#/mcp">查看 MCP 接入</a></div></div>
    <details class="manual-decision" open><summary><span><b>或在这里手动决定</b><small>适合你想亲自选开头和正文时使用</small></span><span class="selection-summary" id="selectionSummary">已选 0 段 · 0 秒</span></summary>
      <form id="editPlanForm"><div class="plan-toolbar"><label>主推款名称<input id="mainProduct" value="${escapeHtml(inferredProduct(job.title))}" required placeholder="例如：白山茶羊毛上衣"><small>用于字幕、文件记录和后续质检。</small></label><div class="plan-help"><b>怎么选</b><span>至少选 1 条“开头”和 1 条“正文”；同一画面只使用一次。片段按原视频时间顺序拼接。</span></div></div>
      ${overview?`<button class="overview-link" type="button" data-preview="${overview.id}" data-mime="${escapeHtml(overview.mime_type||'')}" data-title="${escapeHtml(overview.title)}"><img src="/api/artifacts/${overview.id}/content" alt="全场素材概览"><span>打开全场概览大图</span></button>`:''}
      <div class="candidate-list" aria-label="候选片段">${candidates.map(c=>`<label class="candidate-row"><input type="checkbox" data-candidate="${c.i}"><span class="candidate-time">${clipTime(c.s)}–${clipTime(c.e)}</span><span class="candidate-copy"><span class="candidate-tag ${escapeHtml(c.c)}">${escapeHtml(categoryLabel(c.c))}</span><span>${escapeHtml(c.t)}</span></span><select data-module="${c.i}" aria-label="片段用途" disabled><option value="${c.c==='hook'?'hook_A':'body'}">${c.c==='hook'?'开头':'正文'}</option><option value="${c.c==='hook'?'body':'hook_A'}">${c.c==='hook'?'正文':'开头'}</option></select></label>`).join('')}</div>
      <div class="plan-submit"><p class="form-error" id="editPlanError" role="alert"></p><button class="button primary" type="submit" id="submitEditPlan">提交编排并继续</button></div></form></details></section>`;
}
async function renderJob(jobId){
  setCrumb('任务详情');loading();const job=await api(`/api/jobs/${jobId}`);state.job=job;
  const active=['queued','running'].includes(job.status), canRetry=['failed','cancelled'].includes(job.status), canApprove=job.status==='waiting_input'&&job.current_stage==='rough_cut', canPlan=job.status==='waiting_input'&&job.current_stage==='edit_plan';
  const packet=canPlan?await api(`/api/jobs/${jobId}/packet`):null;
  app.innerHTML=`<a href="#/queue" class="back-link">${icons.arrow}返回队列</a><div class="detail-head"><div class="detail-title"><span class="eyebrow">${escapeHtml(job.id)}</span><h1>${escapeHtml(job.title)}</h1><p>${escapeHtml(job.source_path)}</p></div><div class="detail-actions">${status(job.status)}${canRetry?'<button class="button ghost small" id="retryJob">重新排队</button>':''}${active?'<button class="button danger small" id="cancelJob">取消任务</button>':''}</div></div>
  ${job.error?`<div class="danger-box"><b>执行失败：</b> ${escapeHtml(job.error)}</div>`:''}${canApprove?'<div class="review-callout"><div><b>低清粗剪已就绪</b><p>先在下方播放实际视频；内容确认后再生成高清成片，避免无效高清渲染。</p></div><button class="button primary" id="approveRoughCut">粗剪通过，生成成片</button></div>':''}
  ${canPlan?decisionPanel(job,packet):''}
  <div class="panel"><div class="panel-head"><div><h2>整体流程</h2><p>${Math.round(job.progress||0)}% · 当前节点 ${escapeHtml(job.current_stage||'—')}</p></div></div><div class="workflow">${job.stages.map((s,i)=>`<div class="stage ${s.status}"><span class="stage-dot">${s.status==='succeeded'?'✓':String(i+1).padStart(2,'0')}</span><b>${escapeHtml(s.name)}</b><small>${labels[s.status]||s.status}</small></div>`).join('')}</div></div>
  <div class="detail-grid"><div><div class="panel"><div class="panel-head"><div><h2>节点执行情况</h2><p>输入、结果与错误都保留在任务工作区</p></div></div><div class="stage-list">${job.stages.map((s,i)=>`<div class="stage-row"><span class="stage-index">${String(i+1).padStart(2,'0')}</span><div><h3>${escapeHtml(s.name)} · ${labels[s.status]||s.status}</h3><p>${escapeHtml(s.error||s.message||'等待上游节点')}</p></div><span class="stage-time">${formatTime(s.finished_at||s.started_at)}</span></div>`).join('')}</div></div>
  <div class="panel"><div class="panel-head"><div><h2>任务产物</h2><p>图片、时间线、日志与视频均可打开</p></div></div>${job.artifacts.length?`<div class="artifacts">${job.artifacts.map(artifactCard).join('')}</div>`:`<div class="empty">${icons.empty}<h3>暂无产物</h3><p>节点完成后会自动登记产物。</p></div>`}</div></div>
  <div class="panel"><div class="panel-head"><div><h2>实时事件</h2><p>最近 200 条</p></div></div><div class="timeline">${job.events.map(e=>`<div class="event ${e.level}"><time>${formatTime(e.created_at)} · ${escapeHtml(e.stage_id||'任务')}</time><p>${escapeHtml(e.message)}</p></div>`).join('')||'<div class="empty">暂无事件</div>'}</div></div></div>`;
  $('#cancelJob')?.addEventListener('click',async()=>{await api(`/api/jobs/${jobId}/cancel`,{method:'POST',body:'{}'});toast('任务已取消');renderJob(jobId)});
  $('#retryJob')?.addEventListener('click',async()=>{await api(`/api/jobs/${jobId}/retry`,{method:'POST',body:'{}'});toast('任务已重新排队');renderJob(jobId)});
  $('#approveRoughCut')?.addEventListener('click',async e=>{e.currentTarget.disabled=true;try{await api(`/api/jobs/${jobId}/submit`,{method:'POST',body:JSON.stringify({verdict:'approve'})});toast('粗剪已通过，开始高清导出');renderJob(jobId)}catch(err){toast(err.message);e.currentTarget.disabled=false}});
  if(canPlan){
    const candidates=new Map((packet.candidate_digest||[]).map(c=>[String(c.i),c]));
    const updateSelection=()=>{let count=0,duration=0;$$('[data-candidate]:checked').forEach(input=>{const c=candidates.get(input.dataset.candidate);count+=1;duration+=Number(c.e)-Number(c.s)});$('#selectionSummary').textContent=`已选 ${count} 段 · ${Math.round(duration)} 秒`;};
    $$('[data-candidate]').forEach(input=>input.addEventListener('change',()=>{const select=$(`[data-module="${input.dataset.candidate}"]`);select.disabled=!input.checked;input.closest('.candidate-row').classList.toggle('selected',input.checked);updateSelection()}));
    $('#copyAgentPrompt').addEventListener('click',async()=>{const text=$('.agent-handoff code').textContent;try{await navigator.clipboard.writeText(text);toast('执行指令已复制')}catch(_){toast('复制失败，请手动选择文字复制')}});
    $('#editPlanForm').addEventListener('submit',async e=>{e.preventDefault();const error=$('#editPlanError'),button=$('#submitEditPlan');error.textContent='';const mainProduct=$('#mainProduct').value.trim();const picks=$$('[data-candidate]:checked').map(input=>{const c=candidates.get(input.dataset.candidate);return {src:1,start:Number(c.s),end:Number(c.e),text:c.t,role:candidateRole(c.c),module:$(`[data-module="${input.dataset.candidate}"]`).value};});if(!mainProduct){error.textContent='请填写主推款名称';return}if(!picks.some(p=>p.module==='hook_A')){error.textContent='请至少选择一条片段作为开头';return}if(!picks.some(p=>p.module==='body')){error.textContent='请至少选择一条片段作为正文';return}button.disabled=true;button.textContent='正在提交…';try{await api(`/api/jobs/${jobId}/submit`,{method:'POST',body:JSON.stringify({main_product:mainProduct,picks})});toast('编排已提交，任务继续执行');renderJob(jobId)}catch(err){error.textContent=err.message;button.disabled=false;button.textContent='提交编排并继续'}});
  }
  $$('[data-preview]').forEach(x=>x.addEventListener('click',()=>previewArtifact(x.dataset.preview,x.dataset.mime,x.dataset.title)));
  if(['queued','running'].includes(job.status)){clearTimeout(state.poll);state.poll=setTimeout(()=>location.hash===`#/jobs/${jobId}`&&renderJob(jobId),1800);}
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
  setCrumb('系统设置');loading();const data=await api('/api/settings');
  app.innerHTML=`<div class="hero"><div><span class="eyebrow">LOCAL RUNTIME</span><h1>系统设置</h1><p>配置底层切片引擎、Skill 路径和本地接入策略。</p></div></div><div class="panel"><div class="panel-head"><div><h2>运行配置</h2><p>保存后对新任务生效</p></div></div><form class="settings-form" id="settingsForm"><label>切片引擎目录<input name="engine_path" value="${escapeHtml(data.engine_path||'')}"><small>目录内需要存在 scripts/run_slice.py。</small></label><label>引擎 Python<input name="engine_python" value="${escapeHtml(data.engine_python||'')}"><small>建议使用项目独立的 Python 3.13 环境。</small></label><label>Skill 文件路径<input name="skill_path" value="${escapeHtml(data.skill_path||'')}"></label><label>并行任务数<input name="max_parallel_jobs" type="number" min="1" max="4" value="${data.max_parallel_jobs||1}"><small>第一版实际采用单工作进程，避免视频转码争抢资源。</small></label><label><span><input name="mcp_enabled" type="checkbox" style="width:auto;min-height:0" ${data.mcp_enabled?'checked':''}> 启用 MCP HTTP 入口</span></label><div><button class="button primary" type="submit">保存设置</button></div></form></div>`;
  $('#settingsForm').addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(e.target);await api('/api/settings',{method:'PUT',body:JSON.stringify({engine_path:f.get('engine_path'),engine_python:f.get('engine_python'),skill_path:f.get('skill_path'),max_parallel_jobs:Number(f.get('max_parallel_jobs')),mcp_enabled:f.get('mcp_enabled')==='on'})});toast('设置已保存')});
}

function openNewJob(){
  $('#newJobError').textContent='';
  $('#newJobDialog').showModal();
}
function bindCommon(){
  $$('[data-new-job]').forEach(x=>x.addEventListener('click',openNewJob));
  $$('[data-open-job]').forEach(x=>x.addEventListener('click',()=>location.hash=`#/jobs/${x.dataset.openJob}`));
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
$('#pickSourceButton').addEventListener('click',async e=>{
  const button=e.currentTarget, source=$('#newJobForm [name="source_path"]'), title=$('#newJobForm [name="title"]');
  button.disabled=true;button.textContent='正在选择…';$('#newJobError').textContent='';
  try{const result=await api('/api/files/pick',{method:'POST',body:'{}'});if(result.cancelled)return;source.value=result.path;if(title.dataset.userEdited!=='true')title.value=result.name;}
  catch(err){$('#newJobError').textContent=err.message}finally{button.disabled=false;button.textContent='选择视频';}
});
$('#newJobForm').addEventListener('submit',async e=>{
  e.preventDefault();const submit=$('#createJobSubmit');submit.disabled=true;submit.textContent='正在创建…';$('#newJobError').textContent='';
  try{const f=new FormData(e.target);const data=await api('/api/jobs',{method:'POST',body:JSON.stringify(Object.fromEntries(f.entries()))});$('#newJobDialog').close();e.target.reset();$('#newJobForm [name="title"]').dataset.userEdited='';location.hash=`#/jobs/${data.id}`;toast('任务已加入队列');}
  catch(err){$('#newJobError').textContent=err.message}finally{submit.disabled=false;submit.textContent='加入队列';}
});
$('#previewDialog .preview-close').addEventListener('click',()=>{$('#previewDialog video')?.pause();$('#previewDialog').close()});
$('#menuButton').addEventListener('click',()=>$('.sidebar').classList.toggle('open'));
window.addEventListener('hashchange',()=>{$('.sidebar').classList.remove('open');route()});
setInterval(()=>{$('#clock').textContent=new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date())},1000);
api('/api/health').then(x=>$('#systemVersion').textContent=`v${x.version} · MCP online`).catch(()=>$('#systemVersion').textContent='连接失败');
route();

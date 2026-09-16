const $ = (selector, root=document) => root.querySelector(selector);
const $$ = (selector, root=document) => [...root.querySelectorAll(selector)];
const app = $('#app');
const state = { dashboard:null, job:null, poll:null, mcp:null };
const labels = {queued:'排队中',running:'执行中',waiting_input:'待决策',completed:'已完成',failed:'失败',cancelled:'已取消',pending:'等待',succeeded:'完成'};
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
function status(value){return `<span class="status ${value}">${labels[value]||value}</span>`;}
function toast(message){const el=$('#toast');el.textContent=message;el.classList.add('show');clearTimeout(el.timer);el.timer=setTimeout(()=>el.classList.remove('show'),2400);}
function loading(){app.innerHTML='<div class="loading"><div class="spinner"></div>正在读取本地任务状态</div>';}
function setCrumb(text){$('#pageCrumb').textContent=text;$$('[data-nav]').forEach(x=>x.classList.toggle('active',location.hash.includes(x.dataset.nav)));}

function jobRows(jobs){
  if(!jobs.length)return `<div class="empty">${icons.empty}<h3>还没有剪辑任务</h3><p>添加第一段直播素材，流程节点、日志和产物会在这里持续更新。</p><button class="button primary" data-new-job>新建剪辑任务</button></div>`;
  return `<table class="jobs-table"><thead><tr><th>任务</th><th>当前节点</th><th>进度</th><th>状态</th><th></th></tr></thead><tbody>${jobs.map(j=>`<tr>
    <td><div class="job-name"><span class="job-thumb">${icons.video}</span><div><b>${escapeHtml(j.title)}</b><small>${escapeHtml(j.source_path)}</small></div></div></td>
    <td><small>${escapeHtml(j.current_stage||'—')}</small></td>
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
async function renderJob(jobId){
  setCrumb('任务详情');loading();const job=await api(`/api/jobs/${jobId}`);state.job=job;
  const active=['queued','running'].includes(job.status), canRetry=['failed','waiting_input','cancelled'].includes(job.status);
  app.innerHTML=`<a href="#/queue" class="back-link">${icons.arrow}返回队列</a><div class="detail-head"><div class="detail-title"><span class="eyebrow">${escapeHtml(job.id)}</span><h1>${escapeHtml(job.title)}</h1><p>${escapeHtml(job.source_path)}</p></div><div class="detail-actions">${status(job.status)}${canRetry?'<button class="button ghost small" id="retryJob">重新排队</button>':''}${active?'<button class="button danger small" id="cancelJob">取消任务</button>':''}</div></div>
  ${job.error?`<div class="danger-box"><b>执行失败：</b> ${escapeHtml(job.error)}</div>`:''}
  <div class="panel"><div class="panel-head"><div><h2>整体流程</h2><p>${Math.round(job.progress||0)}% · 当前节点 ${escapeHtml(job.current_stage||'—')}</p></div></div><div class="workflow">${job.stages.map((s,i)=>`<div class="stage ${s.status}"><span class="stage-dot">${s.status==='succeeded'?'✓':String(i+1).padStart(2,'0')}</span><b>${escapeHtml(s.name)}</b><small>${labels[s.status]||s.status}</small></div>`).join('')}</div></div>
  <div class="detail-grid"><div><div class="panel"><div class="panel-head"><div><h2>节点执行情况</h2><p>输入、结果与错误都保留在任务工作区</p></div></div><div class="stage-list">${job.stages.map((s,i)=>`<div class="stage-row"><span class="stage-index">${String(i+1).padStart(2,'0')}</span><div><h3>${escapeHtml(s.name)} · ${labels[s.status]||s.status}</h3><p>${escapeHtml(s.error||s.message||'等待上游节点')}</p></div><span class="stage-time">${formatTime(s.finished_at||s.started_at)}</span></div>`).join('')}</div></div>
  <div class="panel"><div class="panel-head"><div><h2>任务产物</h2><p>图片、时间线、日志与视频均可打开</p></div></div>${job.artifacts.length?`<div class="artifacts">${job.artifacts.map(artifactCard).join('')}</div>`:`<div class="empty">${icons.empty}<h3>暂无产物</h3><p>节点完成后会自动登记产物。</p></div>`}</div></div>
  <div class="panel"><div class="panel-head"><div><h2>实时事件</h2><p>最近 200 条</p></div></div><div class="timeline">${job.events.map(e=>`<div class="event ${e.level}"><time>${formatTime(e.created_at)} · ${escapeHtml(e.stage_id||'任务')}</time><p>${escapeHtml(e.message)}</p></div>`).join('')||'<div class="empty">暂无事件</div>'}</div></div></div>`;
  $('#cancelJob')?.addEventListener('click',async()=>{await api(`/api/jobs/${jobId}/cancel`,{method:'POST',body:'{}'});toast('任务已取消');renderJob(jobId)});
  $('#retryJob')?.addEventListener('click',async()=>{await api(`/api/jobs/${jobId}/retry`,{method:'POST',body:'{}'});toast('任务已重新排队');renderJob(jobId)});
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

function bindCommon(){
  $$('[data-new-job]').forEach(x=>x.addEventListener('click',()=>$('#newJobDialog').showModal()));
  $$('[data-open-job]').forEach(x=>x.addEventListener('click',()=>location.hash=`#/jobs/${x.dataset.openJob}`));
}
async function route(){
  clearTimeout(state.poll);const hash=location.hash||'#/dashboard';
  try{if(hash.startsWith('#/jobs/'))return renderJob(hash.split('/')[2]);if(hash==='#/queue')return renderQueue();if(hash==='#/skill')return renderSkill();if(hash==='#/mcp')return renderMcp();if(hash==='#/settings')return renderSettings();return renderDashboard();}catch(e){app.innerHTML=`<div class="danger-box">${escapeHtml(e.message)}</div>`;}
}

$('#newJobButton').addEventListener('click',()=>$('#newJobDialog').showModal());
$('#newJobForm').addEventListener('submit',async e=>{
  e.preventDefault();const submit=$('#createJobSubmit');submit.disabled=true;submit.textContent='正在创建…';$('#newJobError').textContent='';
  try{const f=new FormData(e.target);const data=await api('/api/jobs',{method:'POST',body:JSON.stringify(Object.fromEntries(f.entries()))});$('#newJobDialog').close();e.target.reset();location.hash=`#/jobs/${data.id}`;toast('任务已加入队列');}
  catch(err){$('#newJobError').textContent=err.message}finally{submit.disabled=false;submit.textContent='加入队列';}
});
$('#previewDialog .preview-close').addEventListener('click',()=>{$('#previewDialog video')?.pause();$('#previewDialog').close()});
$('#menuButton').addEventListener('click',()=>$('.sidebar').classList.toggle('open'));
window.addEventListener('hashchange',()=>{$('.sidebar').classList.remove('open');route()});
setInterval(()=>{$('#clock').textContent=new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date())},1000);
api('/api/health').then(x=>$('#systemVersion').textContent=`v${x.version} · MCP online`).catch(()=>$('#systemVersion').textContent='连接失败');
route();

/* V3 UI owns its state and never reads Legacy/V2 task APIs. */
window.renderSmartV3 = async function () {
  const container = document.querySelector('#app');
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const request = async (url, options = {}) => {
    const response = await fetch(url, {headers: {'Content-Type': 'application/json'}, ...options});
    if (!(response.headers.get('Content-Type') || '').includes('application/json')) {
      throw new Error('V3 接口返回了非 JSON 内容。请停止旧 LiveCut 进程，确认只有一个新版服务运行后重试。');
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    return data;
  };
  document.querySelectorAll('[data-nav]').forEach(a => a.classList.toggle('active', a.dataset.nav === 'smart-v3'));
  document.querySelector('#pageCrumb').textContent = '整片优化剪辑 V3';
  const {jobs} = await request('/api/smart-v3/jobs');
  if (location.hash !== '#/smart-v3') return;
  container.innerHTML = `<div class="hero"><div><span class="eyebrow">WHOLE FILM OPTIMIZATION V3</span><h1>整片优化剪辑 V3</h1><p>素材理解 → 确定性粗筛 → 内容画像 → 动态整片编排 → 整片复审与渲染</p></div><button class="button ghost" id="v3Refresh">刷新阶段状态</button></div>
    <div class="panel"><h2>创建 V3 任务</h2><form id="v3Form" class="form-grid">
      <label class="field full">输入方式<select name="source_kind" id="v3Kind"><option value="media">本地视频素材</option><option value="draft">剪映草稿 / 虚拟时间线</option></select></label>
      <label class="field full">素材或草稿路径<div style="display:flex;gap:8px"><input name="source_path" required placeholder="视频、草稿目录或 draft_content.json 绝对路径"><button type="button" class="button ghost" id="v3Pick">浏览</button></div></label>
      <div class="field full" id="v3DraftOptions" style="display:none"><button type="button" class="button ghost" id="v3ListTimelines">读取草稿时间线</button><select name="draft_path" id="v3Timeline"><option value="">默认选择草稿活动时间线</option></select><small>仅分析草稿保留的主视频片段；支持多原素材和恒定倍速。字幕、特效、转场、叠加轨及独立音乐不复现，输出 MP4。</small></div>
      <label class="field">商品名称<input name="product_name" required placeholder="本次主商品"></label>
      <label class="field">最短时长（秒）<input type="number" min="1" max="600" name="target_min" value="70" required></label>
      <label class="field">最长时长（秒）<input type="number" min="1" max="600" name="target_max" value="120" required></label>
      <label class="field full">可选完整句转写 JSON<input name="transcript_path" placeholder="留空自动 ASR；已有转写文件填绝对路径"></label>
      <div class="form-actions full"><button class="button primary">创建 V3 任务</button></div><p id="v3Message" class="form-error full" role="status"></p>
    </form></div>
    <div class="panel"><h2>V3 独立任务列表</h2>${jobs.length ? jobs.map(job => `<article style="margin:16px 0;padding:16px;border:1px solid var(--border)">
      <h3>${esc(job.input.product_name)} · ${esc(job.status)}</h3><small>${esc(job.id)} · ${job.input.source_kind === 'draft' ? '剪映草稿' : '视频素材'} · 尝试 ${job.attempt}</small>
      <p>${Object.entries(job.stages).map(([s, info]) => `${s}: ${esc(info.status)}`).join(' · ')}</p>
      ${job.error ? `<p class="form-error">${esc(job.error)}</p>` : ''}
      <button class="button primary" data-v3-run="${esc(job.id)}" data-action="${job.status === 'draft' ? 'run' : 'retry'}">${job.status === 'draft' ? '启动' : '重试'}</button>
      <button class="button ghost" data-v3-delete="${esc(job.id)}">删除</button>
      ${Object.entries(job.stages).filter(([, info]) => info.result !== undefined).map(([s, info]) => `<details><summary>${s} ${s === 'S4' ? '候选方案与整片评分' : s === 'S5' ? '复审、替换记录与最终时间线' : '阶段结果'}</summary><pre style="white-space:pre-wrap;max-height:500px;overflow:auto">${esc(JSON.stringify(info.result, null, 2))}</pre></details>`).join('')}
      <p>${job.artifacts.map(a => `<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.name)}</a>`).join(' · ')}</p>
      <small>人工保留率：${job.feedback.retention_ratio === null ? '暂无人工反馈' : esc(job.feedback.retention_ratio)}。画面分析当前未实现，画面字段标记为未知。</small>
    </article>`).join('') : '<p>暂无 V3 任务</p>'}</div>`;
  const message = text => {const el = document.querySelector('#v3Message'); if (el) el.textContent = text;};
  document.querySelector('#v3Refresh').onclick = () => window.renderSmartV3().catch(e => message(e.message));
  document.querySelector('#v3Pick').onclick = async () => {
    try {const result = await request('/api/files/pick', {method:'POST', body:JSON.stringify({kind:document.querySelector('#v3Kind').value === 'draft' ? 'draft' : 'video'})});
      if (!result.cancelled) {document.querySelector('#v3Form [name="source_path"]').value = result.path; clearTimelines();}
    } catch (error) {message(error.message);}
  };
  const clearTimelines = () => {document.querySelector('#v3Timeline').innerHTML = '<option value="">默认选择草稿活动时间线</option>';};
  document.querySelector('#v3Kind').onchange = event => {document.querySelector('#v3DraftOptions').style.display = event.target.value === 'draft' ? 'grid' : 'none'; clearTimelines();};
  document.querySelector('#v3Form [name="source_path"]').oninput = clearTimelines;
  document.querySelector('#v3ListTimelines').onclick = async () => {
    try {const result = await request('/api/smart-v3/drafts/timelines', {method:'POST', body:JSON.stringify({draft_path:document.querySelector('#v3Form [name="source_path"]').value})});
      document.querySelector('#v3Timeline').innerHTML = result.timelines.map(t => `<option value="${esc(t.path)}" ${t.active ? 'selected' : ''}>${esc(t.name)} · ${esc(t.segment_count)} 段 · ${esc(t.timeline_duration)} 秒</option>`).join('');
    } catch (error) {message(error.message);}
  };
  document.querySelector('#v3Form').onsubmit = async event => {
    event.preventDefault();
    try {await request('/api/smart-v3/jobs', {method:'POST', body:JSON.stringify(Object.fromEntries(new FormData(event.target)))}); await window.renderSmartV3();}
    catch (error) {message(error.message);}
  };
  container.querySelectorAll('[data-v3-run]').forEach(button => {button.onclick = async () => {
    button.disabled = true; message('V3 正在执行。可点击刷新查看阶段；页面离开后服务仍会完成本次请求。');
    try {await request(`/api/smart-v3/jobs/${button.dataset.v3Run}/${button.dataset.action}`, {method:'POST', body:'{}'});
      if (location.hash === '#/smart-v3') await window.renderSmartV3();
    } catch (error) {message(error.message);} finally {button.disabled = false;}
  };});
  container.querySelectorAll('[data-v3-delete]').forEach(button => {button.onclick = async () => {
    if (!confirm('删除这个 V3 任务及其产物？')) return;
    try {await request(`/api/smart-v3/jobs/${button.dataset.v3Delete}`, {method:'DELETE'}); await window.renderSmartV3();}
    catch (error) {message(error.message);}
  };});
};

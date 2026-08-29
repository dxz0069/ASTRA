/* ASTRA · 星图 — app logic (Alpine.js) */
'use strict';

/* ============================================================
   流体背景: 特性检测后动态注入 vendor/fluid.js
   检测失败 / 减弱动态偏好 -> CSS 静态渐变兜底
   ============================================================ */
(function bootFluid(){
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  let ok = false;
  try {
    const c = document.createElement('canvas');
    ok = !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch (e) { ok = false; }
  if (reduced || !ok) {
    document.body.classList.add('no-fluid');
    const cv = document.getElementById('fluid-bg');
    if (cv) cv.remove();
    return;
  }
  const s = document.createElement('script');
  s.src = '/static/vendor/fluid.js';
  s.onload = () => { if (window.__astra_apply_ui) window.__astra_apply_ui(); };
  s.onerror = () => document.body.classList.add('no-fluid');
  document.body.appendChild(s);
})();

/* ============================================================
   液态玻璃折射滤镜 (Chromium 渐进增强):
   运行时生成圆角矩形透镜位移贴图 -> feDisplacementMap
   ============================================================ */
(function buildRefraction(){
  try {
    const size = 128, half = size / 2;
    const cv = document.createElement('canvas');
    cv.width = cv.height = size;
    const ctx = cv.getContext('2d');
    const img = ctx.createImageData(size, size);
    const R = 0.62;            // 透镜圆角 (归一化)
    const W = 0.30;            // 折射带宽度
    const MAX = 0.95;          // 最大位移强度
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        const nx = (x + 0.5 - half) / half, ny = (y + 0.5 - half) / half;
        // rounded-box SDF (半宽 1, 圆角 R)
        const qx = Math.max(Math.abs(nx) - (1 - R), 0);
        const qy = Math.max(Math.abs(ny) - (1 - R), 0);
        const sd = Math.hypot(qx, qy) + Math.min(Math.max(Math.abs(nx) - (1 - R), Math.abs(ny) - (1 - R)), 0) - R;
        let dx = 0, dy = 0;
        const t = Math.max(0, 1 - Math.abs(sd) / W);   // 距边缘越近越强
        if (t > 0) {
          const len = Math.hypot(nx, ny) || 1;
          const s = Math.sin(t * Math.PI * 0.5) * MAX;
          dx = -(nx / len) * s;
          dy = -(ny / len) * s;
        }
        const i = (y * size + x) * 4;
        img.data[i]     = 128 + dx * 127;  // R -> 水平位移
        img.data[i + 1] = 0;
        img.data[i + 2] = 128 + dy * 127;  // B -> 垂直位移
        img.data[i + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
    const url = cv.toDataURL('image/png');
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('style', 'position:absolute;width:0;height:0;overflow:hidden');
    svg.innerHTML =
      '<filter id="lg-refract" color-interpolation-filters="sRGB" x="-15%" y="-15%" width="130%" height="130%">' +
        '<feImage result="map" x="-15%" y="-15%" width="130%" height="130%" preserveAspectRatio="none" href="' + url + '"/>' +
        '<feDisplacementMap in="SourceGraphic" in2="map" scale="-56" xChannelSelector="R" yChannelSelector="B"/>' +
      '</filter>';
    document.body.appendChild(svg);
  } catch (e) { /* 折射增强失败则保留磨砂基底 */ }
})();

/* ============================================================
   主组件
   ============================================================ */
/* ============================================================
   界面控制台: 默认值 / 持久化 / 应用
   ============================================================ */
const UI_DEFAULTS = {
  fluidOn: true, ambient: true, dye: 0.21, dissipation: 0.7, veil: 0.45, veilMode: 'darken',
  blurPanel: 22, blurChip: 12, sat: 150,
  railL: 264, railR: 344, density: 'cozy',
  graphDir: 'LR', nodeSize: 24,
  motion: true,
};
const UI_KEY = 'astra-ui-v1';
function loadUIPrefs(){
  try {
    const raw = localStorage.getItem(UI_KEY);
    if (raw) return {...UI_DEFAULTS, ...JSON.parse(raw)};
  } catch (e) {}
  return {...UI_DEFAULTS};
}

function astraApp(){
  return {
    projects: [], selectedId: null, current: null,
    tab: 'detail', refreshing: false, modal: null,
    selectedNode: null, cy: null,
    toasts: [], stepFrom: [], completeFrom: [],
    filterText: '',
    consoleOpen: false, ui: loadUIPrefs(),
    graphQuery: '', _hits: [],
    createForm: {title:'', origin:'', goal:'', hints:'', bootstrap:true},
    stepForm: {description:'', expect:''}, concludeForm: {stepId:'', description:''},
    completeForm: {description:''}, hintForm: {content:''}, settingsForm: {},
    _nodeCount: -1, actionBusy: false,

    init(){
      window.__astra_apply_ui = () => this.applyUI(false);
      this.applyUI(false);
      this.loadProjects();
      setInterval(()=>{ if(this.selectedId) this.loadProject(this.selectedId, true); }, 5000);
    },

    /* ---- 控制台 ---- */
    applyUI(persist){
      const u=this.ui, root=document.documentElement.style;
      root.setProperty('--blur-panel', u.blurPanel+'px');
      root.setProperty('--blur-chip', u.blurChip+'px');
      root.setProperty('--glass-sat', u.sat+'%');
      root.setProperty('--veil-alpha', u.veil);
      root.setProperty('--rail-l', u.railL+'px');
      root.setProperty('--rail-r', u.railR+'px');
      document.body.classList.remove('veil-darken','veil-frost','veil-vignette','veil-none');
      document.body.classList.add('veil-'+(u.veilMode||'darken'));
      document.body.classList.toggle('compact', u.density==='compact');
      document.body.classList.toggle('no-motion', !u.motion);
      document.body.classList.toggle('fluid-off', !u.fluidOn);
      const F=window.ASTRA_FLUID;
      if(F){
        F.config.PAUSED = !u.fluidOn;
        F.config.DENSITY_DISSIPATION = u.dissipation;
        F.setDye(u.dye);
        F.setAmbient(u.ambient);
      }
      if(this.cy){
        this.cy.style(this.graphStyle());
        if(this._appliedDir !== u.graphDir){ this._appliedDir=u.graphDir; this.graphRelayout(); }
      }
      if(persist!==false){ try{ localStorage.setItem(UI_KEY, JSON.stringify(u)); }catch(e){} }
    },
    resetUI(){ this.ui={...UI_DEFAULTS}; this.applyUI(); this.toast('界面已恢复默认'); },
    burstFluid(){ if(window.ASTRA_FLUID) window.ASTRA_FLUID.splat(6); },

    api(method, path, body){
      return fetch(path, {method, headers:{'Content-Type':'application/json'},
        body: body!==undefined ? JSON.stringify(body) : undefined}).then(async r=>{
        if(!r.ok){ let m='HTTP '+r.status; try{ m=(await r.json()).detail||m }catch(e){} throw new Error(m); }
        return r.status===204 ? null : r.json();
      });
    },
    /* toast 仅用于失败与不可见副作用 */
    toast(text, type){ this.toasts.push({text, type:type||''}); setTimeout(()=>this.toasts.shift(), 3200); },

    statusColor(p){
      const map={active:'var(--accent)', completed:'var(--ok)', stopped:'var(--ink-faint)'};
      return 'background:'+(map[p.status]||'var(--ink-faint)');
    },
    statusText(s){ return {active:'进行中', completed:'已归航', stopped:'已停航'}[s]||s; },
    statusBadgeClass(s){ return {active:'high', completed:'summary', stopped:'medium'}[s]||'medium'; },

    filteredProjects(){
      const q = this.filterText.trim().toLowerCase();
      if(!q) return this.projects;
      return this.projects.filter(p => (p.title||'').toLowerCase().includes(q) || (p.id||'').toLowerCase().includes(q));
    },

    loadProjects(){
      return this.api('GET','/projects').then(list=>{
        this.projects=list;
        if(!this.selectedId && list.length) this.selectProject(list[0].id);
      }).catch(e=>this.toast('同步失败: '+e.message,'err'));
    },
    selectProject(id){ this.selectedId=id; this.selectedNode=null; this._nodeCount=-1; this.loadProject(id); },
    loadProject(id, silent){
      return this.api('GET',`/projects/${id}`).then(p=>{
        const prev=this.current;
        this.current=p;
        this.renderGraph(p);
        if(prev && prev.project.id===p.project.id && (prev.facts.length!==p.facts.length || prev.hints.length!==p.hints.length)) this.toast(`星图更新：${p.facts.length} 天枢 / ${p.hints.length} 辅星`);
      }).catch(e=>{ if(!silent) this.toast(e.message,'err'); });
    },
    refreshAll(){
      this.refreshing=true;
      Promise.all([this.loadProjects(), this.selectedId&&this.loadProject(this.selectedId)])
        .finally(()=>this.refreshing=false);
    },

    originDesc(){ const f=(this.current?.facts||[]).find(x=>x.id==='origin'); return f?f.description:''; },
    goalDesc(){ const f=(this.current?.facts||[]).find(x=>x.id==='goal'); return f?f.description:''; },

    /* ---- 星图渲染 ---- */
    buildElements(p){
      const els=[];
      for(const f of p.facts){
        const type = f.id==='goal' ? 'goal' : f.id==='origin' ? 'origin' : 'fact';
        els.push({data:{id:'f:'+f.id, nodeType:type, label:f.id, description:f.description,
          kind:f.kind, nodeId:f.id}});
      }
      for(const s of p.steps){
        els.push({data:{id:'s:'+s.id, nodeType:'step', label:s.id, description:s.description,
          expect:s.expect, status:s.status, to:s.to, worker:s.worker, concluded:!!s.to, nodeId:s.id}});
        const fromIds = s.from_ || s.from || [];
        for(const from of fromIds){ els.push({data:{id:'e:'+s.id+':'+from, source:'f:'+from, target:'s:'+s.id}}); }
        if(s.to){ els.push({data:{id:'ec:'+s.id+':'+s.to, source:'s:'+s.id, target:'f:'+s.to}}); }
      }
      return els;
    },
    graphStyle(){
      const ACCENT='#5fd4e6', GOAL='#e8c355', OK='#4ed6a0', BAD='#f0705e',
            NEUTRAL='#8b98a8', INK_DIM='#b6c2cf', EDGE='rgba(139,152,168,.5)';
      const S=this.ui.nodeSize;
      return [
        {selector:'node', style:{'background-color':ACCENT,'width':S,'height':S,'label':'data(label)',
          'color':INK_DIM,'font-family':'JetBrains Mono, Consolas, monospace','font-size':10.5,
          'text-valign':'bottom','text-margin-y':7,'text-background-color':'rgba(10,14,24,.55)',
          'text-background-opacity':1,'text-background-padding':2,'text-background-shape':'roundrectangle',
          'border-width':1.5,'border-color':'rgba(255,255,255,.28)'}},
        {selector:'node[nodeType="origin"]', style:{'background-color':NEUTRAL}},
        {selector:'node[nodeType="goal"]', style:{'background-color':GOAL,'shape':'star','width':S*1.4,'height':S*1.4,
          'border-color':'rgba(232,195,53,.8)','border-width':2}},
        {selector:'node[nodeType="step"]', style:{'background-color':'#141d2e','shape':'round-rectangle',
          'width':S*1.25,'height':S,'border-color':ACCENT,'border-width':1.5}},
        {selector:'node[?concluded]', style:{'opacity':0.55}},
        {selector:'node[nodeType="step"][status="closed"]', style:{'opacity':0.35,'border-style':'dashed'}},
        {selector:'node[kind="negative"]', style:{'border-color':NEUTRAL,'border-style':'dashed'}},
        {selector:':selected', style:{'overlay-color':ACCENT,'overlay-opacity':0.16,'overlay-padding':7}},
        {selector:'node.dim', style:{'opacity':0.15}},
        {selector:'edge.dim', style:{'opacity':0.07}},
        {selector:'node.hit', style:{'underlay-color':ACCENT,'underlay-opacity':0.35,'underlay-padding':9,
          'border-color':ACCENT,'border-width':2.5}},
        {selector:'edge', style:{'width':1.5,'line-color':EDGE,'curve-style':'bezier',
          'target-arrow-shape':'triangle','target-arrow-color':EDGE,'arrow-scale':0.75}},
        {selector:'edge[id^="ec"]', style:{'line-color':'rgba(95,212,230,.55)',
          'target-arrow-color':'rgba(95,212,230,.65)','line-style':'dashed'}}
      ];
    },
    graphLayout(animate){
      return {name:'dagre', rankDir:this.ui.graphDir, nodeSep:52, rankSep:72,
        animate:!!animate, animationDuration:260};
    },
    renderGraph(p){
      if(!this.cy){
        this.cy=cytoscape({container:document.getElementById('graph'),
          style:this.graphStyle(), layout:this.graphLayout(false)});
        this._appliedDir=this.ui.graphDir;
        this.cy.on('tap','node', ev=>{ this.selectedNode={id:ev.target.id(), data:{...ev.target.data()}}; });
        this.cy.on('tap', ev=>{ if(ev.target===this.cy) this.selectedNode=null; });
      }
      window.__astra_cy = this.cy;
      const els=this.buildElements(p);
      const newIds=new Set(els.map(e=>e.data.id));
      let structureChanged=false;
      /* 增量 diff: 只增删变化的元素, 数据变更就地更新 —— 手动拖动的位置在轮询中不丢 */
      this.cy.elements().forEach(el=>{
        if(!newIds.has(el.id())){ el.remove(); structureChanged=true; }
      });
      for(const e of els){
        const ex=this.cy.getElementById(e.data.id);
        if(ex.length){
          const d=ex.data();
          for(const k in e.data){ if(d[k]!==e.data[k]) ex.data(k, e.data[k]); }
        } else {
          this.cy.add(e); structureChanged=true;
        }
      }
      if(structureChanged || els.length!==this._nodeCount){
        this.cy.layout(this.graphLayout(false)).run();
        if(els.length!==this._nodeCount){
          this._nodeCount=els.length;
          this.cy.fit(undefined, 56);
          /* 稀疏图（仅起点+目标）不让 fit 无限放大 */
          if(this.cy.zoom()>1.15){ this.cy.zoom(1.15); this.cy.center(); }
        }
      }
      if(this.graphQuery && this.graphQuery.trim()) this.applyGraphSearch();
      if(!this.selectedNode || !this.cy.getElementById(this.selectedNode.id).length) this.selectedNode=null;
    },
    graphZoom(f){
      if(!this.cy) return;
      const w=this.cy.width(), h=this.cy.height();
      this.cy.zoom({level:this.cy.zoom()*f, renderedPosition:{x:w/2,y:h/2}});
    },
    graphFit(){ if(this.cy) this.cy.animate({fit:{padding:56}},{duration:200}); },
    graphRelayout(){
      if(!this.cy) return;
      this.cy.layout(this.graphLayout(true)).run();
    },

    /* ---- 图内搜索 ---- */
    applyGraphSearch(){
      if(!this.cy) return;
      const q=(this.graphQuery||'').trim().toLowerCase();
      const cy=this.cy;
      if(!q){ cy.elements().removeClass('hit dim'); this._hits=[]; return; }
      const hits=[];
      cy.nodes().forEach(n=>{
        const d=n.data();
        const hay=[d.label, d.description, d.expect, d.worker].filter(Boolean).join('\n').toLowerCase();
        if(hay.includes(q)){ n.removeClass('dim'); n.addClass('hit'); hits.push(n.id()); }
        else { n.removeClass('hit'); n.addClass('dim'); }
      });
      const hitSet=new Set(hits);
      cy.edges().forEach(e=>{
        if(hitSet.has(e.source().id()) || hitSet.has(e.target().id())) e.removeClass('dim');
        else e.addClass('dim');
      });
      this._hits=hits;
    },
    locateHit(){
      if(!this.cy || !this._hits || !this._hits.length) return;
      const n=this.cy.getElementById(this._hits[0]);
      if(n.length){
        this.selectedNode={id:n.id(), data:{...n.data()}};
        this.cy.animate({fit:{eles:n, padding:130}},{duration:260});
      }
    },
    clearGraphSearch(){
      this.graphQuery='';
      if(this.cy) this.cy.elements().removeClass('hit dim');
      this._hits=[];
    },
    locateNode(){
      if(!this.cy || !this.selectedNode) return;
      const n=this.cy.getElementById(this.selectedNode.id);
      if(n.length) this.cy.animate({fit:{eles:n, padding:130}},{duration:260});
    },

    nodeTypeLabel(n){
      const t=n.data.nodeType;
      return t==='fact'?'天枢':t==='step'?'斗柄':t==='goal'?'北辰':t==='origin'?'起点':'节点';
    },
    nodeBadgeClass(n){
      return n.data.nodeType==='goal' ? 'medium' : 'summary';
    },
    /* ---- 操作 ---- */
    /* in-flight 防抖：写操作进行中禁用全部提交按钮（双击曾致重复步骤/重复指引） */
    guard(fn){
      if(this.actionBusy) return;
      this.actionBusy=true;
      const done=()=>{ this.actionBusy=false; };
      try{
        const r=fn();
        if(r && typeof r.finally==='function'){ r.finally(done); } else { done(); }
      }catch(e){ done(); throw e; }
    },
    selectableFacts(){ return (this.current?.facts||[]).filter(f=>f.id!=='goal'&&f.id!=='origin'); },
    toggleStepFrom(id){ const i=this.stepFrom.indexOf(id); i>=0?this.stepFrom.splice(i,1):this.stepFrom.push(id); },
    toggleCompleteFrom(id){ const i=this.completeFrom.indexOf(id); i>=0?this.completeFrom.splice(i,1):this.completeFrom.push(id); },

    openCreate(){ this.createForm={title:'',origin:'',goal:'',hints:'',bootstrap:true}; this.modal='create'; },
    createProject(){
      if(!this.createForm.title.trim()){ this.toast('请填写星域标题','err'); return; }
      if(!this.createForm.origin.trim() || !this.createForm.goal.trim()){ this.toast('起点与北辰必填','err'); return; }
      this.guard(()=>{
        const hints=(this.createForm.hints||'').split('\n').map(s=>s.trim()).filter(Boolean).map(c=>({content:c,creator:'human'}));
        return this.api('POST','/projects',{title:this.createForm.title, origin:this.createForm.origin,
          goal:this.createForm.goal, bootstrap_enabled:this.createForm.bootstrap, hints}).then(p=>{
          this.modal=null; this.loadProjects(); this.selectProject(p.project.id);
        }).catch(e=>this.toast(e.message,'err'));
      });
    },
    openCreateStep(){ this.stepFrom=[]; this.stepForm={description:'', expect:''}; this.modal='step'; },
    createStep(){
      if(!this.stepFrom.length){ this.toast('至少选择一个源自天枢','err'); return; }
      if(!this.stepForm.description.trim()){ this.toast('请填写斗柄指向','err'); return; }
      this.guard(()=>{
        const body={from:this.stepFrom, description:this.stepForm.description, creator:'human', worker:null};
        if(this.stepForm.expect && this.stepForm.expect.trim()) body.expect=this.stepForm.expect.trim();
        return this.api('POST',`/projects/${this.selectedId}/steps`,body).then(()=>{
          this.modal=null; this.loadProject(this.selectedId);
        }).catch(e=>this.toast(e.message,'err'));
      });
    },
    openConclude(n){ this.concludeForm={stepId:n.nodeId, description:''}; this.modal='conclude'; },
    concludeStep(){
      if(!this.concludeForm.description.trim()){ this.toast('请填写结论天枢','err'); return; }
      this.guard(()=>{
        return this.api('POST',`/projects/${this.selectedId}/steps/${this.concludeForm.stepId}/conclude`,
          {worker:'human', description:this.concludeForm.description}).then(()=>{
          this.modal=null; this.loadProject(this.selectedId);
        }).catch(e=>this.toast(e.message,'err'));
      });
    },
    openComplete(){ this.completeFrom=[]; this.completeForm.description=''; this.modal='complete'; },
    completeProject(){
      if(!this.completeFrom.length){ this.toast('至少选择一个完成依据天枢','err'); return; }
      if(!this.completeForm.description.trim()){ this.toast('请填写完成说明','err'); return; }
      this.guard(()=>{
        return this.api('POST',`/projects/${this.selectedId}/complete`,{from:this.completeFrom,
          description:this.completeForm.description, worker:'human'}).then(()=>{
          this.modal=null; this.loadProject(this.selectedId);
        }).catch(e=>this.toast(e.message,'err'));
      });
    },
    openAddHint(){ this.hintForm.content=''; this.modal='hint'; },
    addHint(){
      if(!this.hintForm.content.trim()){ this.toast('请填写指引内容','err'); return; }
      this.guard(()=>{
        return this.api('POST',`/projects/${this.selectedId}/hints`,{content:this.hintForm.content, creator:'human'}).then(()=>{
          this.modal=null; this.loadProject(this.selectedId);
        }).catch(e=>this.toast(e.message,'err'));
      });
    },
    openSettings(){ this.api('GET','/settings').then(s=>{ this.settingsForm={...s}; this.modal='settings'; }).catch(e=>this.toast(e.message,'err')); },
    saveSettings(){
      this.guard(()=>{
        return this.api('PUT','/settings',this.settingsForm).then(()=>{ this.modal=null; }).catch(e=>this.toast(e.message,'err'));
      });
    },
    setStatus(status){ this.guard(()=>{ return this.api('PUT',`/projects/${this.selectedId}/status`,{status}).then(()=>{ this.loadProject(this.selectedId); }).catch(e=>this.toast(e.message,'err')); }); },
    reopenProject(){ this.guard(()=>{ return this.api('POST',`/projects/${this.selectedId}/reopen`,{description:'手动重开', creator:'human'}).then(()=>{ this.loadProject(this.selectedId); }).catch(e=>this.toast(e.message,'err')); }); },
    askDelete(){ this.modal='delete'; },
    deleteProject(){
      this.guard(()=>{
        return this.api('DELETE',`/projects/${this.selectedId}`).then(()=>{
          this.modal=null; this.selectedId=null; this.current=null; this._nodeCount=-1;
          this.selectedNode=null;
          if(this.cy){ this.cy.elements().remove(); }  // 画布残留已删项目的图（审计十一轮）
          this.loadProjects();
        }).catch(e=>this.toast(e.message,'err'));
      });
    },
    exportYaml(){ window.open(`/projects/${this.selectedId}/export?format=yaml`,'_blank'); },

    /* ---- 审查轨迹 ---- */
    traceItems(){
      const items=[];
      const hints=this.current?.hints||[];
      for(const h of hints){
        const c=h.content||'';
        if(c.includes('[失败学习]')) items.push({cls:'learn', text:c.replace('[失败学习]','').trim(), time:h.created_at});
        else items.push({cls:'hint', text:c, time:h.created_at});
      }
      return items.slice().reverse();
    },

    /* 指引卡: 识别审查/学习前缀, 剥离为徽章 */
    hintMeta(h){
      const c=h.content||'';
      if(c.includes('[失败学习]')) return {cls:'learn', label:'失败学习', text:c.replace('[失败学习]','').trim()};
      return {cls:'', label:'', text:c};
    },

    closeModals(){ if(this.modal) this.modal=null; this.consoleOpen=false; }
  };
}

'use strict';
const $=id=>document.getElementById(id), esc=x=>String(x??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let snapshot=null, profiles=[], profileId='NDX', range=90, filter='all', polling=false, autoRefreshStarted=false, toastTimer=null, tacticalStatus=null, tacticalLoadToken=0, capitalStatus=null, capitalLoadToken=0, stateMachineStatus=null, stateMachineLoadToken=0, realWorldStatus=null, realWorldLoadToken=0, realWorldCatalog=null;
const fmt=x=>x==null?'—':Number(x).toLocaleString('zh-CN',{maximumFractionDigits:2});
const percent=x=>x==null?'—':(x*100).toFixed(2)+'%';
const say=s=>{$('footerStatus').textContent=s;$('toast').textContent=s;$('toast').classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.remove('show'),5000)};
const updateStageLabels={queue:'准备更新',acquire:'联网采集',parse:'解析来源',component_fetch:'读取成分行情',archive:'归档记录',feature_store:'生成特征',strict_evaluation:'严格评估',audit:'校验审计链',snapshot:'整理页面快照',complete:'更新完成',error:'更新失败'};
let progressHideTimer=null;
const staticHint=Boolean(window.__STATIC_DASHBOARD__||new URLSearchParams(location.search).has('static'));
let staticMode=false,staticDataPromise=null;
function markStaticMode(){
  staticMode=true;
  document.documentElement.dataset.dataMode='static';
  const status=$('autoStatus');
  if(status){status.textContent='公网静态展示版：使用已发布快照；联网更新请在本地服务执行。';status.title='GitHub Pages 只提供已发布的只读快照。'}
}
async function staticApi(url){
  markStaticMode();
  staticDataPromise=staticDataPromise||fetch('./static-data.json',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error('未找到公网展示数据');return r.json()});
  const data=await staticDataPromise,u=new URL(url,location.href),route=u.pathname.split('/api/')[1]||'';
  if(route==='profiles')return data.profiles||[];
  if(route==='real-world-profiles')return data.real_world||{};
  if(route==='phase2e-status')return data.phase2e||{};
  if(route==='phase2f-status')return data.phase2f||{};
  if(route==='phase3a-status')return data.phase3a||{};
  if(route==='phase3b-status')return data.phase3b||{};
  if(route==='status')return data.status||{running:false,stage:'complete',progress:100,message:'公网静态展示版已载入'};
  if(route==='history')return data.history?.[u.searchParams.get('profile')||'NDX']||[];
  if(route==='snapshot'){
    const profile=u.searchParams.get('profile')||'NDX',map=data.snapshots?.[profile]||{},id=u.searchParams.get('id');
    return map[id]||map[data.latest?.[profile]]||Object.values(map)[0]||{};
  }
  throw Error('公网静态展示版不支持此接口');
}
async function api(url,data){
  if(staticHint&&!data)return staticApi(url);
  const options=data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{};
  try{
    const r=await fetch(url,options),text=await r.text();let j;
    try{j=text?JSON.parse(text):{}}catch(error){if(!data)return staticApi(url);throw error}
    if(!r.ok){const error=Error(j.error||r.statusText);error.status=r.status;throw error}
    return j;
  }catch(error){
    if(!data&&(error.status==null||error.status===404))return staticApi(url);
    throw error;
  }
}
const PAGE_CONFIG={
 overview:{label:'总览',kicker:'当前标的',copy:'用一屏查看价格压力、市场读数和最新判断。'},
 trigger:{label:'回撤触发',kicker:'价格规则',copy:'先看回撤档位，再决定是否启动证据核验。'},
 research:{label:'周期研究',kicker:'历史验证',copy:'查看 252 日 Tactical Cycle 的冻结回放结果。'},
 capital:{label:'资金管理',kicker:'只读模拟',copy:'检查 Opportunity Fund 与现实参数状态；页面不读取个人资产。'},
 evidence:{label:'证据核验',kicker:'证据工作台',copy:'把估值、盈利、情绪和市场内部结构分开核验。'},
 guide:{label:'指标说明',kicker:'方法手册',copy:'理解每项指标怎么算、变化代表什么，以及它的边界。'},
 ledger:{label:'评分账本',kicker:'审计记录',copy:'查看固定权重、评分资格、原始观察值与数据来源。'}
};
let currentPage=PAGE_CONFIG[location.hash.slice(1)]?location.hash.slice(1):'overview';
function setPage(page,{updateHash=true,scroll=true}={}){
  if(!PAGE_CONFIG[page])page='overview';
  currentPage=page;
  document.querySelectorAll('[data-page]').forEach(el=>{
    if(el.classList.contains('app-nav-item'))return;
    el.hidden=el.dataset.page!==page;
  });
  document.querySelectorAll('.app-nav-item').forEach(btn=>{
    const active=btn.dataset.page===page;
    btn.classList.toggle('is-active',active);
    if(active)btn.setAttribute('aria-current','page');else btn.removeAttribute('aria-current');
  });
  const dash=document.querySelector('.dashboard');
  const showEvidence=page==='evidence'||page==='ledger';
  dash?.classList.toggle('page-no-evidence',!showEvidence);
  const heading=$('pageHeading'),config=PAGE_CONFIG[page];
  if(heading){
    heading.hidden=page==='overview';
    $('pageHeadingKicker').textContent=config.kicker;
    $('pageHeadingTitle').textContent=config.label;
    $('pageHeadingCopy').textContent=config.copy;
  }
  if(updateHash&&location.hash!==`#${page}`)history.pushState(null,'',`#${page}`);
  if(scroll)window.scrollTo({top:0,behavior:'smooth'});
}
function navigateToTarget(id){
  const target=$(id);if(!target)return;
  setPage(target.dataset.page||'overview',{scroll:false});
  requestAnimationFrame(()=>target.scrollIntoView({behavior:'smooth',block:'start'}));
}
function updateProgress(job){
  const panel=$('updateProgressPanel');
  if(!panel)return;
  const running=Boolean(job?.running),failed=job?.stage==='error'||job?.error===true,complete=!running&&!failed&&job?.stage==='complete';
  if(!running&&!failed&&!complete){panel.hidden=true;return}
  clearTimeout(progressHideTimer);
  panel.hidden=false;
  const stage=String(job?.stage||'queue'),label=updateStageLabels[stage]||stage;
  const numeric=Number(job?.progress),progress=Number.isFinite(numeric)?Math.max(0,Math.min(100,numeric)):0;
  const title=failed?'更新失败':complete?'更新完成':`正在${label}`;
  const completed=job?.completed!=null&&job?.total!=null?`已处理 ${fmt(job.completed)} / ${fmt(job.total)}`:'';
  const source=job?.source?`当前来源：${job.source}`:'';
  const elapsed=Number(job?.elapsed_seconds);
  $('updateProgressTitle').textContent=title;
  $('updateProgressValue').textContent=`${Math.round(progress)}%`;
  $('updateProgressStage').textContent=completed?`${label} · ${completed}`:label;
  $('updateProgressElapsed').textContent=Number.isFinite(elapsed)?`已用 ${fmt(elapsed)} 秒`:'耗时读取中';
  $('updateProgressMessage').textContent=job?.message||'正在处理更新任务。';
  $('updateProgressSource').textContent=source;
  $('updateProgressSource').hidden=!source;
  $('updateProgressFill').style.transform=`scaleX(${progress/100})`;
  $('updateProgressTrack').setAttribute('aria-valuenow',String(Math.round(progress)));
  $('updateProgressTrack').setAttribute('aria-valuetext',`${title}，${Math.round(progress)}%`);
  panel.dataset.state=failed?'error':complete?'complete':'running';
  if(complete)progressHideTimer=setTimeout(()=>{panel.hidden=true},8000);
}
const value=r=>['drawdown','revision','growth','breadth'].includes(r.key)?percent(r.value):fmt(r.value);
const names={scored:'已计分',candidate:'观察值，未获评分资格',missing:'未取得'};
function render(){const s=snapshot,q=s.quote,m=Object.fromEntries(s.rows.map(r=>[r.key,r])),strict=s.strict_pit||null;
  const f=s.free_analysis||{},execution=s.execution||{status:'等待证据',code:'unknown',reason:'此旧快照没有执行状态，请按该日期更新。'},profile=s.profile||{};
  const tone={core_pass:'status-support',dca_only:'status-watch',earnings_risk:'status-hold',core_fail:'status-hold',core_unknown:'status-unknown',stale:'status-stale',unknown:'status-unknown'}[execution.code]||'status-unknown';
  $('railStatus').className='rail-status '+tone;$('railStatus').textContent=execution.status;
  $('heroTitle').textContent=f.title||'此旧快照尚未生成免费版分析';$('heroResult').textContent=execution.status;$('heroResult').className='execution-result '+tone;
  $('executionBadge').className='execution-badge '+tone;$('executionBadge').textContent=execution.status;$('executionBadge').title=execution.reason||'';
  $('railCopy').textContent=[execution.reason,f.summary].filter(Boolean).join(' ');
  $('heroCopy').textContent=f.summary||execution.reason||'按该日期更新后可查看免费证据分析；下方保留原始数据。';
  $('alertCopy').textContent=strict?`当前页面为 ${s.mode||'LEGACY'}（${s.mode==='STRICT_PIT'?'STRICT PIT':'NOT STRICT PIT'}）。研究代理覆盖 ${s.coverage}/100；独立 STRICT_PIT：${strict.decision}，覆盖 ${strict.coverage}/100。两条路径不混用。`:`执行状态：${execution.status}。当前页面为 ${s.mode||'LEGACY'}（NOT STRICT PIT）；研究代理覆盖 ${s.coverage}/100。`;
  $('heroResultNote').textContent=execution.reason||'已知信号优先解释，未知单列';$('heroKicker').textContent=`${profile.label||'Nasdaq-100'} · ${s.mode||'LEGACY'} · ${s.mode==='STRICT_PIT'?'STRICT PIT':'NOT STRICT PIT'} · 免费证据版 · 截至 ${s.asof}`;
  profileId=profile.id||profileId||'NDX';if($('instrumentSelect').value!==profileId)$('instrumentSelect').value=profileId;
  $('targetMeta').textContent=`${profile.currency||'USD'} · ${profile.underlying||profile.label||'当前标的'} · 代码 ${profile.price_symbol||profile.price_series||profile.id} · ${profile.price_basis||profile.price_source||'公共收盘序列'}；常规定投继续，额外投入只由验证状态提醒`;
  const pt=s.point_in_time||{},freshness=s.freshness||{};
  $('pointInTime').textContent=`时点边界：只使用 ${pt.cutoff||s.asof} 当日可用记录；排除之后行情 ${fmt(pt.price_rows_after_cutoff||0)} 条、证据 ${fmt(pt.evidence_rows_after_cutoff||0)} 条。${freshness.market_observation_date?'行情最后观察 '+freshness.market_observation_date+'，距评估日 '+fmt(freshness.market_age_business_days||0)+' 个工作日。':''}`;
  $('autoStatus').textContent=staticMode?'公网静态展示版：使用已发布快照；联网更新请在本地服务执行。':freshness.should_refresh?'最近行情需要联网更新，打开页面后将自动尝试一次。':'数据检查完成；只有新交易日且连续两工作日未更新时才自动尝试更新。';
  $('freeEvidence').innerHTML=f?[['支持因素',f.support],['制约因素',f.against],['仍待确认',f.unknown]].map(([label,items])=>`<section style="padding:14px 0;border-bottom:1px solid #d4ded7"><h3 style="font-size:15px">${esc(label)}</h3><ul style="padding-left:20px;font-size:14px;line-height:1.8">${items.length?items.map(t=>'<li>'+esc(t)+'</li>').join(''):'<li>当前没有足够的合格证据可列入此项。</li>'}</ul></section>`).join(''):'<p>该快照尚未包含免费版分析。</p>';
  $('freeBoundary').textContent=f?.boundary||'';
  renderRough(s.rough_estimates||[]);
  $('knownScore').textContent=fmt(s.known);$('scoreRange').textContent=`可能区间 ${fmt(s.known)}–${fmt(s.upper)}`;$('coverageLabel').textContent=s.coverage+' / 100';$('railDate').textContent=s.asof;
  $('scoreKnownBar').style.width=s.known+'%';$('scoreUnknownBar').style.left=s.known+'%';$('scoreUnknownBar').style.width=(100-s.coverage)+'%';
  $('headerStamp').textContent='采集 '+new Date(s.retrieved_at).toLocaleString('zh-CN');
  $('signalDrawdown').textContent=percent(q.drawdown);$('signalDrawdownNote').textContent='历史高点 '+q.athDate;
  $('signalRsi').textContent=fmt(q.rsi);$('signalRsiNote').textContent=q.rsi<30?'短期超卖，不代表盈利改善':'技术辅助，不能单独确认加仓';
  const isGold=profile.asset_type==='commodity_future';
  $('signalEarnings').textContent=m.revision.value==null?((s.quality||[]).some(z=>z.module==='earnings')?'部分资料已取得':isGold?'不适用':'预测资料待取得'):percent(m.revision.value);
  $('signalEarnings').parentElement.querySelector('.signal-note').textContent=m.revision.score==null?(isGold?'黄金无股票 EPS':'同年度EPS修正仍待验证'):'同年度EPS修正已计算';
  $('signalValuation').textContent=m.forward_pe.score!=null?'历史分位可计算':(s.quality||[]).some(z=>z.module==='valuation'&&z.status!=='stale')?'当期估值可观察':'估值资料待更新';
  $('signalValuation').parentElement.querySelector('.signal-note').textContent=m.forward_pe.score!=null?'同口径估值历史':isGold?'黄金不使用 PE':'历史评分资格仍待验证';
  const vol=m.vxn||{key:'vxn',value:null,date:null};$('marketMetrics').innerHTML=[[`${profile.short_label||profile.id||'标的'} 收盘`,fmt(q.close),q.date],['距历史高点',percent(q.drawdown),fmt(q.ath)],['MA200',fmt(q.ma200),'偏离 '+percent(q.close/q.ma200-1)],[profile.volatility_label||'波动率代理',value(vol),vol.date||'未取得']].map(([a,b,c])=>`<div class="metric-cell"><span>${esc(a)}</span><strong style="display:block;font-size:24px">${esc(b)}</strong><small>${esc(c)}</small></div>`).join('');
  $('marketTitle').textContent=`${profile.short_label||profile.id||'标的'} 市场读数`;$('module-pressure').querySelector('p').textContent=`回撤 20 分，${profile.volatility_label||'波动率代理'} 5 分`;$('module-valuation').querySelector('p').textContent=isGold?'黄金估值不以 PE 表示':'Forward PE、TTM PE、盈利收益率';$('module-earnings').querySelector('p').textContent=isGold?'黄金无股票 EPS 口径':'同财政期 EPS 修正与增长';
  for(const mod of ['valuation','earnings','pressure','macro','breadth','technical'])$(mod+'Body').innerHTML=s.rows.filter(r=>r.module===mod).map(r=>`<div class="data-row"><div><strong>${esc(r.label)}</strong><p>${esc(r.date||'无观察日期')} · ${esc(names[r.status])}</p></div><strong>${esc(value(r))}</strong></div><p class="module-note">${esc(r.reason)}</p>`).join('');
  $('valuationBody').innerHTML+=isGold?'<p class="module-note">黄金没有股票盈利和 PE；本模块保持未知，避免把期货价格伪装成估值分位。</p>':`<div class="data-row"><strong>盈利收益率（解释）</strong><strong>${m.forward_pe.value>0?percent(1/m.forward_pe.value):'—'}</strong></div><p class="module-note">1 / Forward PE 观察值；不是预测回报，也不直接等于股债风险溢价。</p>`;
  $('macroBody').innerHTML+='<p class="module-note">实际利率较高通常增加长期盈利的折现压力。NFCI 为负代表金融条件相对历史均值较宽松；两者不能替代企业盈利判断。</p>';
  $('logicList').innerHTML=(f?.context||[]).map(t=>`<li><p>${esc(t)}</p></li>`).join('');
  $('counterCopy').textContent='回撤、RSI 与趋势相关，不作为三份独立买入证据。未知盈利不视作盈利稳定。';
  $('conditionList').innerHTML=(f?.next_conditions||[]).map(t=>`<p class="condition-item">${esc(t)}</p>`).join('');
  renderVerification(s);
  const visible=s.rows.filter(r=>filter==='all'||r.status===filter);$('ledgerCount').textContent=visible.length+' / '+s.rows.length+' 项';
  $('scoreRows').innerHTML=visible.map(r=>`<div class="score-row"><div class="score-row-label"><strong>${esc(r.label)}</strong><span>${r.weight} 分</span></div><div class="score-row-main"><p>${esc(r.reason)}</p><small style="display:block;margin:6px 0;line-height:1.7">公式：${esc(r.formula||'旧快照未记录')}<br>${esc(r.calculation||'')}</small><div class="score-row-bar"><span style="width:${r.score==null||!r.weight?0:r.score/r.weight*100}%"></span></div></div><div class="score-row-value"><strong>${fmt(r.score)} / ${r.weight}</strong><span>${names[r.status]}</span></div></div>`).join('')||'<p class="empty-state">没有符合条件的证据。</p>';
  $('auditValues').innerHTML=Object.entries(s.audit||{}).filter(([k,v])=>v.value!=null).map(([k,v])=>`<p style="font-size:12px;overflow-wrap:anywhere;margin:12px 0"><strong>${esc(k)}：${fmt(v.value)}</strong><br>${esc(v.date)} · ${esc(v.status)}<br>${esc(v.basis)}</p>`).join('');
  renderEvidence(s.quality||[]);
  $('sourceCount').textContent=s.sources.length+' 个请求；'+s.sources.filter(x=>x.error).length+' 个失败';
  $('sourceList').innerHTML=s.sources.map(r=>`<div class="source-row"><div><a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.source)}</a><p class="source-meta">采集 ${esc(r.retrieved_at_utc)}<br>${esc(r.error||'HTTP '+r.http_status+' · '+r.bytes+' bytes')}<br>SHA256 ${esc(r.sha256||'无响应')}</p></div></div>`).join('');
  $('footerStatus').textContent=s.limitations.join(' ');renderMetricGuide(profile);renderTactical(tacticalStatus,profile);renderCapital(capitalStatus,profile);renderStateMachine(stateMachineStatus,profile);chart();
}
function renderTactical(report,profile){
  const status=$('tacticalStatus'),metrics=$('tacticalMetrics'),windows=$('tacticalWindows'),description=$('tacticalDescription');
  if(!status||!metrics||!windows||!description)return;
  if((profile?.id||profileId)!=='NDX'){
    status.innerHTML='<span aria-hidden="true">●</span><span>Phase 2E 当前只对 Nasdaq-100（NDX）运行；其他标的不会借用 NDX 周期结果。</span>';
    metrics.innerHTML='';windows.innerHTML='';description.textContent='选择 Nasdaq-100 后显示冻结的 252 日 Tactical Cycle 研究结果。该模块不读取本金、仓位或加仓金额。';return;
  }
  if(!report||report.PHASE_2E_STATUS==='NOT_RUN'){
    status.innerHTML='<span aria-hidden="true">●</span><span>尚未找到完成的 Phase 2E 回放。</span>';
    metrics.innerHTML='';windows.innerHTML='';description.textContent='研究结果尚未生成；当前页面仍可使用价格回撤与证据核验。';return;
  }
  const primary=report.tactical_primary_detail||{},stats=report.tactical_baseline?.evaluation_stats||{},one=stats.forward_1y||{},five=stats.forward_5y||{};
  status.innerHTML=`<span aria-hidden="true">●</span><span><strong>${esc(report.PHASE_2E_STATUS)}</strong> · Tactical 信号 ${esc(report.TACTICAL_DRAWDOWN_SIGNAL||'INCONCLUSIVE')} · 回撤参考 ${esc(report.DRAWDOWN_REFERENCE||'INCONCLUSIVE')}。这是历史研究结论，不是自动买入指令。</span>`;
  const cells=[['-10%事件',fmt(primary.event_count)],['涉及 Macro Episode',fmt(primary.macro_episode_count)],['中位数 1年后',percent(primary.median_forward_returns?.forward_1y)],['中位数 5年后',percent(primary.median_forward_returns?.forward_5y)],['Tactical Cycle',fmt(report.tactical_cycle_count)],['事件总数',fmt(report.tactical_event_count)]];
  metrics.innerHTML=cells.map(([label,value])=>`<div class="metric-cell"><span>${esc(label)}</span><strong style="display:block;font-size:24px">${esc(value)}</strong></div>`).join('');
  description.textContent=`完整回放 ${report.date_range?.start||'—'} 至 ${report.date_range?.end||'—'}：平均 1 年后 ${percent(one.mean)}，平均 5 年后 ${percent(five.mean)}；入场后 30/60/120 日时机遗憾分别记录在结果表。Overlay 增量价值为 ${report.OVERALL_OVERLAY_INCREMENTAL_VALUE||'INCONCLUSIVE'}，因此资金状态机仍为 ${report.READY_FOR_CAPITAL_STATE_MACHINE||'NO'}。`;
  const labels={"2000_2002":'2000–2002',"2007_2009":'2007–2009',"2018":'2018',"2020":'2020',"2022":'2022'};
  windows.innerHTML=`<p>过去 252 个交易日最高收盘只用当日及之前观察；窗口老化本身不能重置，触发过的周期只有遇到真实新高才重新武装。2008 事件来自 2007 年开始的周期。</p><table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr><th style="text-align:left;padding:8px 0">窗口</th><th>周期</th><th>事件</th></tr></thead><tbody>${Object.entries(labels).map(([key,label])=>{const row=report.key_history_windows?.[key]||{};return `<tr><td style="padding:8px 0;border-top:1px solid #d4ded7">${label}</td><td style="text-align:center;border-top:1px solid #d4ded7">${fmt(row.cycle_count)}</td><td style="text-align:center;border-top:1px solid #d4ded7">${fmt(row.event_count)}</td></tr>`}).join('')}</tbody></table>`;
}
function renderCapital(report,profile){
 const status=$('capitalStatus'),metrics=$('capitalMetrics'),ladders=$('capitalLadders'),paths=$('capitalPaths'),description=$('capitalDescription');
 if(!status||!metrics||!ladders||!paths||!description)return;
 if((profile?.id||profileId)!=='NDX'){
  status.innerHTML='<span aria-hidden="true">●</span><span>Phase 2F 当前只对 Nasdaq-100（NDX）运行；其他标的不会借用 NDX 资金压力结果。</span>';
  metrics.innerHTML='';ladders.innerHTML='';paths.innerHTML='';description.textContent='选择 Nasdaq-100 后显示冻结的 Opportunity Fund 压力测试。该模块不读取本金、工资或仓位。';return;
 }
 if(!report||report.PHASE_2F_STATUS==='NOT_RUN'){
  status.innerHTML='<span aria-hidden="true">●</span><span>尚未找到完成的 Phase 2F 资金压力回放。</span>';
  metrics.innerHTML='';ladders.innerHTML='';paths.innerHTML='';description.textContent='资金梯度研究尚未生成；当前页面仍可使用价格回撤与证据核验。';return;
 }
 const unit=report.capital_unit_definition||{},counts=report.path_counts||{},assess=report.ladder_assessments||{};
 status.innerHTML=`<span aria-hidden="true">●</span><span><strong>${esc(report.PHASE_2F_STATUS)}</strong> · 推荐候选 ${esc((report.RECOMMENDED_LADDER_CANDIDATES||[]).join('、')||'—')} · 状态机资格 ${esc(report.READY_FOR_CAPITAL_STATE_MACHINE||'NO')}。这是现金状态研究，不是自动买入指令。</span>`;
 const cells=[['历史事件',fmt(report.historical_event_count)],['配置回放',fmt(counts.total)],['初始资金',fmt(unit.initial_opportunity_fund_units)+' units'],['M换算',fmt(unit.m_to_opportunity_units)+' units'],['合成路径',fmt(counts.synthetic)],['顺序路径',fmt(counts.sequence)]];
 metrics.innerHTML=cells.map(([label,value])=>`<div class="metric-cell"><span>${esc(label)}</span><strong style="display:block;font-size:24px">${esc(value)}</strong></div>`).join('');
 description.textContent=`Opportunity Fund 与 Core DCA 分离；每次事件只由 Tactical 档位扣款，ATH 只作长期标签。固定尺度 1M=${fmt(unit.m_to_opportunity_units)} units，不代表人民币；超过 Cap 的现金记为 surplus。`;
 ladders.innerHTML='<table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr><th style="text-align:left;padding:8px 0">Ladder</th><th>分类</th><th>稳健排名</th><th>深档现金分数</th><th>历史提前耗尽均值</th><th>合成提前耗尽均值</th><th>欠资率均值</th></tr></thead><tbody>'+['A','B','C','D'].map(id=>{const x=assess[id]||{};return `<tr><td style="padding:9px 0;border-top:1px solid #d4ded7"><strong>${id} · ${esc(x.name||'')}</strong><br><small>${esc(x.description||'')}</small></td><td style="text-align:center;border-top:1px solid #d4ded7">${esc(x.classification||'—')}</td><td style="text-align:center;border-top:1px solid #d4ded7">${esc(x.robustness_rank??'—')}</td><td style="text-align:center;border-top:1px solid #d4ded7">${x.dry_powder_survival_score==null?'—':(x.dry_powder_survival_score*100).toFixed(1)+'%'}</td><td style="text-align:center;border-top:1px solid #d4ded7">${x.historical_early_exhaustion_rate?.mean==null?'—':(x.historical_early_exhaustion_rate.mean*100).toFixed(1)+'%'}</td><td style="text-align:center;border-top:1px solid #d4ded7">${x.synthetic_early_exhaustion_rate?.mean==null?'—':(x.synthetic_early_exhaustion_rate.mean*100).toFixed(1)+'%'}</td><td style="text-align:center;border-top:1px solid #d4ded7">${x.underfunded_event_rate?.mean==null?'—':(x.underfunded_event_rate.mean*100).toFixed(1)+'%'}</td></tr>`}).join('')+'</tbody></table>';
 const synth=report.synthetic_path_definitions||{};paths.innerHTML=`<p>历史、S1–S8 合成压力、S4 的 -60/-70/-80 扩展和三种顺序路径共用同一现金状态机。Extreme Extension 不增加正式触发档。</p><ul style="padding-left:18px">${Object.entries(synth).map(([id,x])=>`<li style="margin:5px 0">${esc(id)}：${fmt(x.event_count)} 个事件，${esc(x.start_date)} 至 ${esc(x.end_date)}</li>`).join('')}</ul><p>资金排序不看最终资产，先看提前耗尽、欠资、深档现金、补回时间、顺序风险和合成路径完整执行率。</p>`;
}
function renderStateMachine(report,profile){
 const status=$('stateMachineStatus'),metrics=$('stateMachineMetrics'),preview=$('stateMachinePreview'),accounts=$('stateMachineAccounts'),rules=$('stateMachineRules');
 if(!status||!metrics||!preview||!accounts||!rules)return;
 if((profile?.id||profileId)!=='NDX'){
  status.innerHTML='<span aria-hidden="true">●</span><span>Phase 3A 当前只对 Nasdaq-100 运行；其他标的不会借用 NDX 资金状态。</span>';metrics.innerHTML='';preview.innerHTML='';accounts.innerHTML='';rules.innerHTML='';return;
 }
 if(!report||report.PHASE_3A_STATUS==='NOT_RUN'){
  status.innerHTML='<span aria-hidden="true">●</span><span>尚未找到完成的 Phase 3A 状态机回放。</span>';metrics.innerHTML='';preview.innerHTML='';accounts.innerHTML='';rules.innerHTML='';return;
 }
 const runs=report.run_ids||{},inv=report.invariants||{};
 status.innerHTML=`<span aria-hidden="true">●</span><span><strong>${esc(report.PHASE_3A_STATUS)}</strong> · C ${esc(report.CAPITAL_STATE_C||'—')} · D ${esc(report.CAPITAL_STATE_D||'—')} · 重放 ${esc(report.STATE_MACHINE_REPLAY||'—')}。仅模拟：不会自动交易。</span>`;
 const cells=[['状态机版本',report.capital_state_machine_version||'—'],['C run',runs.C||'—'],['D run',runs.D||'—'],['现金守恒',inv.cash_conservation?'通过':'—'],['Core DCA',inv.core_dca_untouched?'独立':'—'],['Overlay',inv.overlay_cannot_modify?'不改金额':'—']];
 metrics.innerHTML=cells.map(([label,value])=>`<div class="metric-cell"><span>${esc(label)}</span><strong style="display:block;font-size:${label.includes('run')?'12px':'20px'};overflow-wrap:anywhere">${esc(value)}</strong></div>`).join('');
 const p=report.next_trigger_preview||{};
 preview.innerHTML='<h3 style="font-size:15px;margin:0 0 10px">下一档规则预览</h3><p style="font-size:13px;line-height:1.8;margin:0 0 12px">预览只读取当前状态：下一档、对应价格和 C/D 计划金额；它不是价格预测，也不会改变资金。</p><table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr><th style="text-align:left;padding:8px 0">候选</th><th>当前回撤</th><th>下一档</th><th>下一档价格</th><th>计划投入</th><th>现金足够</th></tr></thead><tbody>'+['C','D'].map(id=>{const x=p[id]||{},plan=x.ladder_plans?.[id]||{};return `<tr><td style="padding:9px 0;border-top:1px solid #d4ded7"><strong>${id}</strong></td><td style="text-align:center;border-top:1px solid #d4ded7">${percent(x.current_tactical_drawdown)}</td><td style="text-align:center;border-top:1px solid #d4ded7">${x.next_trigger_band?'-'+esc(x.next_trigger_band)+'%':'—'}</td><td style="text-align:center;border-top:1px solid #d4ded7">${fmt(x.next_trigger_price)}</td><td style="text-align:center;border-top:1px solid #d4ded7">${fmt(plan.planned_amount)}</td><td style="text-align:center;border-top:1px solid #d4ded7">${plan.cash_sufficient==null?'—':plan.cash_sufficient?'是':'否'}</td></tr>`}).join('')+'</tbody></table>';
 accounts.innerHTML='<div class="guide-row" style="margin-top:0"><div class="guide-name"><strong>Core DCA</strong><span>独立账户</span></div><div class="guide-cell"><b>日常定投</b><p>状态机不会暂停、减少或读取 Core DCA。</p></div><div class="guide-cell"><b>Opportunity Fund</b><p>按完整 Target F 的 C/D 比例，在当前现金不足时只投入可用余额。</p></div><div class="guide-cell"><b>Surplus Cash</b><p>达到 Fund Cap 的补充资金只记入 Surplus，本阶段不决定去向。</p></div></div>';
 const trigger=report.trigger||{},ladder=report.ladder_definitions||{};
 rules.innerHTML=`<p>触发版本 <strong>${esc(trigger.version||'NDX_TACTICAL_DRAWDOWN_V1')}</strong>：${fmt(trigger.window_trading_days||252)} 个交易日窗口，档位 ${esc((trigger.thresholds||[.1,.2,.3,.4,.5]).map(x=>'-'+(x*100)+'%').join('、'))}。ATH 只作长期标签；同一周期每档最多一次。</p><p>C：${esc(JSON.stringify(ladder.C?.fractions||{}))}；D：${esc(JSON.stringify(ladder.D?.fractions||{}))}。新周期只重新武装档位，不自动补满现金；每月补充在新月份首日、同日事件之前入账。</p><p>当前状态 hash 链和事件 hash 链用于重放审计。${inv.future_price_unused?'没有使用未来价格。':'存在待核查项。'} ${report.READY_FOR_REAL_WORLD_PARAMETERIZATION==='YES'?'参数接口已就绪，仍需真实现金流评审。':'参数化尚未就绪。'}</p>`;
}
function renderRealWorld(report,profile){
 const status=$('realWorldStatus'),metrics=$('realWorldMetrics'),profilesEl=$('realWorldProfiles'),ladders=$('realWorldLadders'),rules=$('realWorldRules');
 if(!status||!metrics||!profilesEl||!ladders||!rules)return;
 if((profile?.id||profileId)!=='NDX'){
  status.innerHTML='<span aria-hidden="true">●</span><span>Phase 3B 当前只对 Nasdaq-100（NDX）运行；其他标的不会借用 NDX 现金流参数。</span>';metrics.innerHTML='';profilesEl.innerHTML='';ladders.innerHTML='';rules.innerHTML='';return;
 }
 if(!report||report.PHASE_3B_STATUS==='NOT_RUN'){
  status.innerHTML='<span aria-hidden="true">●</span><span>尚未找到完成的 Phase 3B 现实资金参数验证。</span>';metrics.innerHTML='';profilesEl.innerHTML='';ladders.innerHTML='';rules.innerHTML='';return;
 }
 const inv=report.invariants||{},ready=report.READY_FOR_PERSONAL_PROFILE||'NO';
 status.innerHTML=`<span aria-hidden="true">●</span><span><strong>${esc(report.PHASE_3B_STATUS)}</strong> · 现实参数 ${esc(report.REAL_WORLD_PARAMETERIZATION||'—')} · 个人参数接口 ${esc(ready)}。${report.simulation_only?'只模拟，不下单。':'请核查模拟边界。'}</span>`;
 const cells=[['Profile 场景',fmt(report.profile_result_count)],['压力路径',fmt(report.path_result_count)],['触发历史',inv.phase3a_trigger_history_unchanged?'保持不变':'待核查'],['现金守恒',inv.cash_conservation?'通过':'待核查'],['核心定投',inv.core_dca_priority?'优先':'待核查'],['借款/杠杆',inv.debt_financing_never_used?'未使用':'待核查']];
 metrics.innerHTML=cells.map(([label,value])=>`<div class="metric-cell"><span>${esc(label)}</span><strong style="display:block;font-size:20px;overflow-wrap:anywhere">${esc(value)}</strong></div>`).join('');
 const rows=report.profile_results||[],base=rows.filter(row=>row.target_id==='F2'&&Number(row.cap_multiplier)===1.5&&row.refill_mode==='FIXED'&&row.growth_scenario==='G0'&&row.surplus_policy==='S0');
 const byProfile={};for(const row of base){byProfile[row.profile?.profile_id||'—']??={};byProfile[row.profile?.profile_id||'—'][row.ladder_id]=row}
 profilesEl.innerHTML='<h3 style="font-size:15px;margin:0 0 10px">标准 Profile · F2 / Cap 1.5 / 固定补充 / G0</h3><table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr><th style="text-align:left;padding:8px 0">Profile</th><th>月收入</th><th>月 Core DCA</th><th>C 资金充足度</th><th>D 资金充足度</th><th>C/D 欠资率</th></tr></thead><tbody>'+Object.entries(byProfile).sort().map(([id,x])=>{const c=x.C||{},d=x.D||{},p=c.profile||d.profile||{};return `<tr><td style="padding:9px 0;border-top:1px solid #d4ded7"><strong>${esc(id)}</strong><br><small>${esc(p.label||'')}</small></td><td style="text-align:center;border-top:1px solid #d4ded7">${fmt(p.monthly_income)}</td><td style="text-align:center;border-top:1px solid #d4ded7">${fmt(p.monthly_core_dca)}</td><td style="text-align:center;border-top:1px solid #d4ded7">${esc(c.capital_adequacy_ratio==null?'—':(Number(c.capital_adequacy_ratio)*100).toFixed(0)+'%')} · ${esc(c.real_world_viability||'—')}</td><td style="text-align:center;border-top:1px solid #d4ded7">${esc(d.capital_adequacy_ratio==null?'—':(Number(d.capital_adequacy_ratio)*100).toFixed(0)+'%')} · ${esc(d.real_world_viability||'—')}</td><td style="text-align:center;border-top:1px solid #d4ded7">${c.underfunded_event_rate==null?'—':(Number(c.underfunded_event_rate)*100).toFixed(1)+'%'} / ${d.underfunded_event_rate==null?'—':(Number(d.underfunded_event_rate)*100).toFixed(1)+'%'}</td></tr>`}).join('')+'</tbody></table><p style="font-size:12px;line-height:1.7;margin:10px 0">这些是参数压力场景，不是你的收入建议；资金充足度只说明现金是否覆盖剩余触发计划，不是收益概率。</p>';
 const comparison=report.ladder_comparison||{};ladders.innerHTML='<h3 style="font-size:15px;margin:0 0 10px">C / D 选择 · 按 Profile 分开观察</h3><table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr><th style="text-align:left;padding:8px 0">Profile</th><th>C 利用率</th><th>D 利用率</th><th>C 欠资率</th><th>D 欠资率</th><th>结论</th></tr></thead><tbody>'+Object.entries(comparison).sort().map(([id,x])=>{const c=x.C||{},d=x.D||{};return `<tr><td style="padding:9px 0;border-top:1px solid #d4ded7"><strong>${esc(id)}</strong></td><td style="text-align:center;border-top:1px solid #d4ded7">${c.mean_capital_utilization==null?'—':percent(c.mean_capital_utilization)}</td><td style="text-align:center;border-top:1px solid #d4ded7">${d.mean_capital_utilization==null?'—':percent(d.mean_capital_utilization)}</td><td style="text-align:center;border-top:1px solid #d4ded7">${c.mean_underfunded_rate==null?'—':percent(c.mean_underfunded_rate)}</td><td style="text-align:center;border-top:1px solid #d4ded7">${d.mean_underfunded_rate==null?'—':percent(d.mean_underfunded_rate)}</td><td style="text-align:center;border-top:1px solid #d4ded7">${esc(x.selection||'INCONCLUSIVE')}</td></tr>`}).join('')+'</tbody></table>';
 const targets=Object.entries(report.target_specs||{}).map(([id,x])=>`${id}=${x.label}`).join('、'),waterfall=(report.waterfall||[]).join(' → '),stress=(report.path_results||[]).map(x=>x.path_id).filter((x,i,a)=>a.indexOf(x)===i).join('、');
 rules.innerHTML=`<p><strong>目标/上限：</strong>${esc(targets)}；Cap 倍数 ${esc((report.cap_multipliers||[]).join('、'))}。Target 增长只改变目标与 Cap，不凭空增加现金。</p><p><strong>收入瀑布：</strong>${esc(waterfall)}。Core DCA 先结算；机会补充只使用 Core DCA 后的可用收入；超过 Cap 的部分进入 S0/S1/S2 指定 Surplus。</p><p><strong>现金拖累：</strong>只报告平均闲置现金与闲置天数，不假设固定收益率。压力路径：${esc(stress||'—')}。</p><p><strong>冻结边界：</strong>252 交易日 Tactical Drawdown、ATH 长期标签、-10/-20/-30/-40/-50 档及 C/D 状态机沿用 Phase 3A；紧急备用金排除，借款、杠杆和自动交易关闭。</p>`;
}
function readRealWorldForm(){
 const n=id=>{const value=Number($(id)?.value);return Number.isFinite(value)&&value>=0?value:0};
 return {profile_id:$('realWorldProfileSelect')?.value||'CUSTOM',label:'个人参数情景',currency:'CNY',monthly_income:n('realWorldIncome'),monthly_core_dca:n('realWorldCore'),monthly_opportunity_refill:n('realWorldRefill'),initial_opportunity_cash:n('realWorldInitial'),target_id:$('realWorldTarget')?.value||'F2',cap_multiplier:Number($('realWorldCap')?.value||1.5),surplus_policy:$('realWorldSurplus')?.value||'S0',emergency_fund_excluded:true,debt_financing_allowed:false,auto_trade:false};
}
function populateRealWorldForm(){
 const select=$('realWorldProfileSelect');if(!select||!realWorldCatalog)return;
 const values=realWorldCatalog.profiles||{};
 const apply=id=>{const p=values[id];if(!p)return;$('realWorldIncome').value=p.monthly_income;$('realWorldCore').value=p.monthly_core_dca;$('realWorldRefill').value=p.monthly_opportunity_refill;$('realWorldInitial').value=p.initial_opportunity_cash||0;$('realWorldTarget').value='F2';$('realWorldCap').value='1.5';$('realWorldSurplus').value=p.surplus_policy||'S0'};
 select.onchange=()=>{if(select.value!=='CUSTOM')apply(select.value)};
 if(select.value!=='CUSTOM')apply(select.value);
}
async function previewRealWorldProfile(){
 const output=$('realWorldProfilePreview');if(!output)return;
 output.innerHTML='<span aria-hidden="true">●</span><span>正在按当前输入重放现金流…</span>';
 try{
  const profile=readRealWorldForm(),mode=$('realWorldRefillMode')?.value||'FIXED';
  const payload=await api('/api/real-world-preview',{capital_profile:profile,start_date:'2000-01-01',end_date:snapshot?.asof||new Date().toLocaleDateString('en-CA'),phase3a_batch_id:realWorldStatus?.phase3a_batch_id,ladder_id:'C',target_id:profile.target_id,cap_multiplier:profile.cap_multiplier,refill_mode:mode,refill_ratio:mode==='INCOME_LINKED'?Number($('realWorldRatio')?.value||0.1):null,growth_scenario:'G0',surplus_policy:profile.surplus_policy});
  const band=payload.next_trigger_band?'-'+payload.next_trigger_band+'%':'暂无',plan=payload.next_trigger_planned_amount==null?'—':fmt(payload.next_trigger_planned_amount),sufficient=payload.next_trigger_cash_sufficient==null?'—':payload.next_trigger_cash_sufficient?'够':'不足';
  output.innerHTML=`<span aria-hidden="true">●</span><span><strong>${esc(payload.real_world_viability||'—')}</strong> · 目标 ${fmt(payload.target_at_start)} / Cap ${fmt(payload.cap_at_start)} · 当前机会现金 ${fmt(payload.ending_opportunity_cash)} · 充足度 ${payload.capital_adequacy_ratio==null?'—':percent(payload.capital_adequacy_ratio)} · 下一档 ${esc(band)}（${esc(payload.next_trigger_ladder||'C')} 计划 ${esc(plan)}，现金${esc(sufficient)}）· 欠资事件 ${fmt(payload.underfunded_event_count)}。历史触发输入 ${fmt(payload.source_event_count)} 条；这只是现金流情景预览，不是买入指令。</span>`;
 }catch(error){output.innerHTML=`<span aria-hidden="true">●</span><span>预览失败：${esc(error.message)}</span>`}
}
function renderRough(items){
 const section=$('roughSection');section.hidden=!items.length;
 if(!items.length){$('roughEvidence').innerHTML='<p class="empty-state">当前标的没有可用的免费预测粗估。</p>';return}
 const labels={rough_forward_pe:'粗估 Forward PE',rough_growth:'粗估下一财年 EPS 增长'};
 $('roughEvidence').innerHTML='<div class="rough-grid">'+items.map(r=>`<article class="rough-card"><span>${esc(labels[r.key]||r.key)}</span><strong>${r.key==='rough_growth'?percent(r.value):fmt(r.value)+'×'}</strong><p>有效成分 ${fmt(r.coverage_count)} / 300 · 数量覆盖 ${percent(r.coverage_percent)} · 机构数中位数 ${fmt(r.median_orgs)}</p><p>${esc(r.basis)}</p><p>观察 ${esc(r.date)} · 预测财年 FY${esc(r.fiscal_year)} · 辅助观察，不增加严格分数</p></article>`).join('')+'</div><p style="font-size:12px;line-height:1.7;margin-top:12px">系统会把每次联网更新保存为独立快照。积累约三个月后，可以比较同一财政年度的粗估盈利变化；在成分集合和覆盖口径稳定前，它仍不会替代严格 EPS 修正。</p>';
}
function renderMetricGuide(profile){
 const vol=profile.volatility_label||'波动率代理', gold=profile.asset_type==='commodity_future';
 const rows=[
  ['历史高点回撤','1 − 当前收盘 ÷ 历史最高收盘','绝对值变大：价格离高点更远，触发更深一档核验；不等于变便宜。','绝对值缩小：价格修复，价格压力降低。','高点 10,000、当前 8,500，回撤为 15%；达到 10% 档，尚未达到 20% 档。'],
  ['Forward PE','价格 ÷ 未来12个月预期 EPS','上升：价格涨得更快或预期盈利下调，通常估值更贵。','下降：可能更便宜，也可能是市场先于盈利恶化下跌，需结合 EPS 修正。','Forward PE 25× 对应盈利收益率 1/25=4%；它不是未来回报率。'],
  ['TTM PE','价格 ÷ 过去12个月实际 EPS','上升：相对已实现盈利更贵，或过去盈利下降。','下降：相对已实现盈利更便宜，但周期高点利润会让 PE 假性偏低。','价格不变、TTM EPS 从 100 降到 80，PE 会从 20×升至25×。'],
  ['同财政期 EPS 修正','同一财政年度最新预测 ÷ 3个月前预测 − 1','上升或为正：分析师上调同一批盈利，基本面确认更强。','下降或为负：盈利预期被下修，价格下跌可能有基本面原因。','FY2027 EPS 由100上调至105，修正为+5%；必须比较同一个财政年度。'],
  ['下一财政年 EPS 增长','下一财年 EPS ÷ 本财年 EPS − 1','上升：预期增长加快，可为较高估值提供部分支撑。','下降：增长溢价变弱；负值表示预期盈利收缩。','本财年100、下一财年112，预期增长12%。'],
  [vol,'期权隐含波动率；本页按历史分位解释','上升：市场预期波动扩大、风险情绪紧张；不说明涨跌方向。','下降：风险情绪缓和；也不代表估值已经合理。',profile.id==='NDX'?'VXN从20升至35说明纳指期权隐含波动明显上升。':'本标的暂无同口径本地波动率时，VIX只作跨市场背景，不能代替本地恐慌指标。'],
  ['10年实际利率','美国10年通胀保值国债实际收益率','上升：长期现金流折现压力通常增大；黄金持有机会成本也可能上升。','下降：折现压力通常缓和，但不保证资产上涨。','实际利率由1%升到2%，相同远期现金流的现值会降低。'],
  ['NFCI','芝加哥联储金融条件指数；0为长期平均附近','上升：金融条件趋紧，融资与风险偏好通常承压。','下降：金融条件趋松；负值表示相对历史均值宽松。','NFCI=-0.3比+0.2更宽松；它不是政策利率本身。'],
  ['市场宽度','成分股中收盘站上 MA200 的比例','上升：上涨由更多成分股参与，内部结构改善。','下降：参与面收窄；少数权重股可能掩盖多数股票走弱。','100只成分股中65只站上MA200，宽度为65%；历史成员必须按当日还原。'],
  ['RSI(14)','14期上涨与下跌幅度的 Wilder 平滑比率','上升：短期动量增强；高于70常称偏热，但不是卖出指令。','下降：动量走弱；低于30常称超卖，但不能确认底部。','RSI=25只说明近期跌势急，若EPS同步下修，仍不能据此加仓。'],
  ['MA200','最近200个交易日收盘均值','价格相对 MA200 上升：长期趋势改善。','价格相对 MA200 下降：趋势走弱，可能进入较深调整。','收盘900、MA200为1,000，偏离=-10%；它与回撤相关，不能重复当作独立证据。']
 ];
 $('metricGuide').innerHTML=rows.map(r=>`<div class="guide-row"><div class="guide-name"><strong>${esc(r[0])}</strong><span>${gold&&['Forward PE','TTM PE','同财政期 EPS 修正','下一财政年 EPS 增长'].includes(r[0])?'黄金不适用':'指标定义'}</span></div><div class="guide-cell"><b>怎么算 / 表示什么</b><p>${esc(r[1])}</p></div><div class="guide-cell"><b>上升与下降</b><p>${esc(r[2])}<br>${esc(r[3])}</p></div><div class="guide-cell"><b>例子</b><p>${esc(r[4])}</p></div></div>`).join('');
}
function chart(){if(!snapshot)return;const pts=snapshot.prices.slice(-range),svg=$('priceChart');if(!pts.length)return;const lo=Math.min(...pts.map(p=>p.value)),hi=Math.max(snapshot.quote.ath,...pts.map(p=>p.value)),y=v=>230-(v-lo)/(hi-lo||1)*200,x=i=>20+i/Math.max(1,pts.length-1)*800;
 svg.innerHTML=`<line x1="20" x2="820" y1="${y(snapshot.quote.ath)}" y2="${y(snapshot.quote.ath)}" stroke="#a7b5ad" stroke-dasharray="5 5"/><text x="20" y="20" font-size="11" fill="#64756c">历史高点 ${fmt(snapshot.quote.ath)}</text><polyline fill="none" stroke="#34715a" stroke-width="3" points="${pts.map((p,i)=>x(i)+','+y(p.value)).join(' ')}"/><line id="crosshair" y1="15" y2="240" stroke="#78998b" stroke-dasharray="4 4"/>`;
 $('chartStart').textContent=pts[0].date;$('chartEnd').textContent=pts.at(-1).date;$('chartSource').textContent=`${pts.length} 个交易日 · ${snapshot.profile?.price_source||'保存的历史行情'}`;
 const show=i=>{i=Math.max(0,Math.min(pts.length-1,i));$('chartHoverDate').textContent=pts[i].date;$('chartHoverValue').textContent=fmt(pts[i].value);$('crosshair').setAttribute('x1',x(i));$('crosshair').setAttribute('x2',x(i));$('chartSlider').value=i};
 $('chartSlider').max=pts.length-1;$('chartSlider').oninput=e=>show(+e.target.value);svg.onpointermove=e=>{const b=svg.getBoundingClientRect();show(Math.round(((e.clientX-b.left)/b.width*840-20)/800*(pts.length-1)))};show(pts.length-1);
}
async function loadProfiles(){profiles=await api('/api/profiles');const select=$('instrumentSelect');select.innerHTML=profiles.map(p=>`<option value="${esc(p.id)}">${esc(p.label)} · ${esc(p.short_label)}</option>`).join('');select.value=profileId}
async function loadRealWorldCatalog(){try{realWorldCatalog=await api('/api/real-world-profiles');populateRealWorldForm()}catch(error){realWorldCatalog=null;const output=$('realWorldProfilePreview');if(output)output.innerHTML=`<span aria-hidden="true">●</span><span>现实参数目录暂不可用：${esc(error.message)}</span>`}}
async function load(id){const params=new URLSearchParams({profile:profileId});if(id)params.set('id',id);snapshot=await api('/api/snapshot?'+params.toString());profileId=snapshot.profile?.id||profileId;if(id)$('asofDate').value=snapshot.asof;render();const history=await api('/api/history?profile='+encodeURIComponent(profileId));$('caseSelect').innerHTML=history.map(r=>`<option value="${r.id}">${r.asof} · ${r.id.slice(9,15)}</option>`).join('');$('caseSelect').value=snapshot.id;const tacticalToken=++tacticalLoadToken,capitalToken=++capitalLoadToken,stateToken=++stateMachineLoadToken,realWorldToken=++realWorldLoadToken;if(profileId==='NDX'){api('/api/phase2e-status').then(report=>{if(tacticalToken===tacticalLoadToken){tacticalStatus=report;renderTactical(report,snapshot.profile||{});}}).catch(()=>{if(tacticalToken===tacticalLoadToken){tacticalStatus=null;renderTactical(null,snapshot.profile||{});}});api('/api/phase2f-status').then(report=>{if(capitalToken===capitalLoadToken){capitalStatus=report;renderCapital(report,snapshot.profile||{});}}).catch(()=>{if(capitalToken===capitalLoadToken){capitalStatus=null;renderCapital(null,snapshot.profile||{});}});api('/api/phase3a-status').then(report=>{if(stateToken===stateMachineLoadToken){stateMachineStatus=report;renderStateMachine(report,snapshot.profile||{});}}).catch(()=>{if(stateToken===stateMachineLoadToken){stateMachineStatus=null;renderStateMachine(null,snapshot.profile||{});}});api('/api/phase3b-status').then(report=>{if(realWorldToken===realWorldLoadToken){realWorldStatus=report;renderRealWorld(report,snapshot.profile||{});}}).catch(()=>{if(realWorldToken===realWorldLoadToken){realWorldStatus=null;renderRealWorld(null,snapshot.profile||{});}})}else{tacticalStatus=null;capitalStatus=null;stateMachineStatus=null;realWorldStatus=null;renderTactical(null,snapshot.profile||{});renderCapital(null,snapshot.profile||{});renderStateMachine(null,snapshot.profile||{});renderRealWorld(null,snapshot.profile||{})}}
async function maybeAutoRefresh(){if(staticMode){$('autoStatus').textContent='公网静态展示版：使用已发布快照；联网更新请在本地服务执行。';return}if(autoRefreshStarted||!snapshot||snapshot.historical||!snapshot.freshness?.should_refresh)return;autoRefreshStarted=true;$('autoStatus').textContent='检测到行情滞后，正在自动尝试联网更新…';try{await api('/api/refresh',{asof:$('asofDate').value,profile:profileId});await poll();$('autoStatus').textContent='自动更新完成；请按新的观察日期阅读结论。'}catch(e){if(e.status===409){await poll();$('autoStatus').textContent='已有更新任务完成；已载入最新快照。'}else{$('autoStatus').textContent='自动更新未完成，保留上次快照；可手动点击「联网更新」。';updateProgress({stage:'error',progress:0,error:true,message:'自动更新未完成：'+e.message});say('自动更新未完成：'+e.message)}}}
async function poll(){if(polling)return;polling=true;$('refreshButton').disabled=true;updateProgress({running:true,stage:'queue',progress:0,message:'正在连接本地更新任务'});try{let j;do{j=await api('/api/status');updateProgress(j);$('footerStatus').textContent=j.message;if(j.running)await new Promise(r=>setTimeout(r,800))}while(j.running);updateProgress(j);if(j.snapshot_id)await load(j.snapshot_id);say(j.message)}catch(e){updateProgress({stage:'error',progress:0,error:true,message:'无法连接服务：'+e.message});say('无法连接服务：'+e.message)}finally{polling=false;$('refreshButton').disabled=false}}
 $('refreshButton').textContent='联网更新';$('refreshButton').onclick=async()=>{if(staticMode){const message='公网静态展示版只提供已发布快照；请在本地服务中联网更新后重新发布。';updateProgress({stage:'error',progress:0,error:true,message});say(message);return}updateProgress({running:true,stage:'queue',progress:0,message:'正在连接本地更新任务'});try{await api('/api/refresh',{asof:$('asofDate').value,profile:profileId});await poll()}catch(e){if(e.status===409){await poll()}else{updateProgress({stage:'error',progress:0,error:true,message:'更新未开始：'+e.message});say('更新未开始：'+e.message)}}};
$('instrumentSelect').onchange=async e=>{profileId=e.target.value;autoRefreshStarted=false;snapshot=null;$('caseSelect').innerHTML='<option>正在读取该标的快照…</option>';try{await load()}catch(err){try{say(`${profileId} 尚无快照，正在读取免费行情…`);await api('/api/refresh',{asof:$('asofDate').value,profile:profileId});await poll()}catch(refreshErr){say('无法读取该标的：'+refreshErr.message)}}};
$('caseSelect').onchange=e=>load(e.target.value).catch(e=>say(e.message));
 $('importInput').onchange=async e=>{try{const f=e.target.files[0];if(!f)return;if(f.size>10000000)throw Error('文件超过 10MB');const content=await f.text();const payload=f.name.toLowerCase().endsWith('.csv')?{csv:content}:{records:JSON.parse(content).records};const r=await api('/api/import',{...payload,asof:$('asofDate').value,profile:profileId});if(r.snapshot_id)await load(r.snapshot_id);say(r.message)}catch(e){say('未导入：'+e.message)}finally{e.target.value=''}};
function download(content,name,type){const url=URL.createObjectURL(new Blob([content],{type})),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
$('exportJson').onclick=()=>snapshot&&download(JSON.stringify(snapshot,null,2),(snapshot.profile?.short_label||'snapshot')+'-'+snapshot.asof+'.json','application/json');
$('exportCsv').onclick=()=>{if(!snapshot)return;const cols=['key','label','value','weight','score','date','status','reason','source','basis'];const cell=v=>'"'+String(v??'').replace(/^[=+@-]/,"'$&").replaceAll('"','""')+'"';download('\ufeff'+[cols.join(','),...snapshot.rows.map(r=>cols.map(k=>cell(r[k])).join(','))].join('\r\n'),(snapshot.profile?.short_label||'snapshot')+'-'+snapshot.asof+'.csv','text/csv')};
$('exportReport').onclick=()=>snapshot&&download(`# ${snapshot.profile?.label||'标的'} 证据报告\n\n截至 ${snapshot.asof}\n\n页面模式：${snapshot.mode||'LEGACY'}（${snapshot.mode==='STRICT_PIT'?'STRICT PIT':'NOT STRICT PIT'}）\n执行状态：${snapshot.execution?.status||'旧快照未记录'}\n${snapshot.execution?.reason||''}\n\n${snapshot.free_analysis?.title||snapshot.decision}；研究代理已知 ${fmt(snapshot.known)} 分，覆盖 ${snapshot.coverage}/100。\nSTRICT_PIT：${snapshot.strict_pit?.decision||'未记录'}；覆盖 ${snapshot.strict_pit?.coverage==null?'—':fmt(snapshot.strict_pit.coverage)}/100。两条路径不混用。\n\n时点边界：${snapshot.point_in_time?.availability_rule||'旧快照未记录'}\n\n`+snapshot.rows.map(r=>`- ${r.label}：${value(r)}；${names[r.status]}。${r.reason} 来源：${r.source||'未取得'}`).join('\n\n')+'\n\n'+(snapshot.free_analysis?[snapshot.free_analysis.summary,...snapshot.free_analysis.support,...snapshot.free_analysis.against,...snapshot.free_analysis.unknown,snapshot.free_analysis.boundary].join('\n\n'):'')+'\n\n'+snapshot.limitations.join('\n'),(snapshot.profile?.short_label||'snapshot')+'-'+snapshot.asof+'.md','text/markdown');
for(const b of document.querySelectorAll('[data-range]'))b.onclick=()=>{range=+b.dataset.range;document.querySelectorAll('[data-range]').forEach(x=>x.classList.toggle('is-active',x===b));chart()};
for(const b of document.querySelectorAll('[data-filter]'))b.onclick=()=>{filter=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('is-active',x===b));if(snapshot)render()};
for(const b of document.querySelectorAll('.app-nav-item'))b.onclick=()=>setPage(b.dataset.page);
for(const b of document.querySelectorAll('[data-scroll-target]'))b.onclick=()=>navigateToTarget(b.dataset.scrollTarget);
$('missingAction').onclick=()=>{document.querySelector('[data-filter="missing"]').click();navigateToTarget('scoreSection')};
window.addEventListener('popstate',()=>setPage(location.hash.slice(1),{updateHash:false}));
setPage(currentPage,{updateHash:false,scroll:false});
for(const d of document.querySelectorAll('details.module')){try{const v=localStorage.getItem(d.id);if(v!==null)d.open=v==='true';d.addEventListener('toggle',()=>localStorage.setItem(d.id,d.open))}catch{}}
$('asofDate').value=new Date().toLocaleDateString('en-CA');$('asofDate').max=$('asofDate').value;
if(location.protocol==='file:'){for(const id of ['heroTitle','heroResult','railStatus'])$(id).textContent='请启动本地数据服务';$('heroCopy').innerHTML='双击同目录的 Start.command，然后打开 <a href="http://127.0.0.1:8766">本地仪表盘</a>。直接打开 HTML 无法运行采集和数据库。';$('refreshButton').disabled=true;say('当前为文件预览，未连接后台')}else{loadProfiles().then(()=>loadRealWorldCatalog()).then(()=>load()).then(async()=>{const j=await api('/api/status');if(j.running)await poll();else await maybeAutoRefresh()}).catch(e=>say(e.message))}

function scenario(){const pe=+$('entryPe').value,n=+$('holdYears').value;if(!Number.isFinite(pe)||pe<1||pe>200||!Number.isInteger(n)||n<1||n>30){$('scenarioTable').textContent='PE 需为 1–200，持有年数需为 1–30 的整数';return}const growth=[0,.05,.1,.15];$('scenarioTable').innerHTML='<table style="width:100%;font-size:13px;border-collapse:collapse"><thead><tr><th>期末 PE / EPS 年增长</th>'+growth.map(g=>'<th>'+percent(g)+'</th>').join('')+'</tr></thead><tbody>'+[15,20,25,30].map(exit=>'<tr><th style="padding:12px">'+exit+'×</th>'+growth.map(g=>{const ratio=Math.pow(1+g,n)*exit/pe;return '<td style="padding:10px;border-top:1px solid #d3ded8">'+percent(ratio-1)+'<br><small>年化 '+percent(Math.pow(ratio,1/n)-1)+'</small></td>'}).join('')+'</tr>').join('')+'</tbody></table>'}
$('entryPe').oninput=scenario;$('holdYears').oninput=scenario;scenario();

function renderEvidence(items){
 $('evidenceExplorer').innerHTML=items.map((r,i)=>{
   const pts=r.history||[],values=pts.map(p=>p.value),lo=values.length?Math.min(...values):0,hi=values.length?Math.max(...values):1,xy=pts.map((p,k)=>(10+k/Math.max(1,pts.length-1)*620)+','+(90-(p.value-lo)/(hi-lo||1)*65)).join(' ');
   const v=r.module==='breadth'?percent(r.value):fmt(r.value);
   const historyNote=pts.length?`<svg viewBox="0 0 640 105" role="img" aria-label="${esc(r.label)} 同口径历史趋势"><polyline points="${xy}" fill="none" stroke="#5b8472" stroke-width="2"/></svg><p style="font-size:12px;color:#68796f">${esc(pts[0]?.date)} — ${esc(pts.at(-1)?.date)} · 最近 ${pts.length} 个观测点，横轴按观测次序，不对缺失日期插值</p>`:'<p class="empty-state">来源没有保存可绘制的历史序列；观察值不会因此自动进入评分。</p>';
   return `<details style="border-top:1px solid #d4ded7;padding:16px 0" ${i===0?'open':''}><summary style="cursor:pointer;display:flex;justify-content:space-between;gap:12px"><strong>${esc(r.label)} · ${esc(v)}</strong><small>${esc(r.date)} · ${r.status==='stale'?'已过期':r.status==='scored'?'已计分':'观察值未获评分资格'}</small></summary><p style="font-size:12px;margin:12px 0">观察至评估日 ${r.age_days==null?'—':esc(r.age_days)} 天 · 历史 ${r.history_months==null?'—':esc(r.history_months)} 个有效月末${r.percentile==null?'':' · 观察分位 '+percent(r.percentile)}${r.coverage_percent==null?'':' · 来源声明覆盖 '+fmt(r.coverage_percent)+'%'}</p>${historyNote}<ul style="padding-left:18px;font-size:13px;margin-top:12px">${(r.reasons||[]).map(t=>'<li style="margin:6px 0">'+esc(t)+'</li>').join('')}</ul></details>`
 }).join('')||(snapshot?.profile?.id&&snapshot.profile.id!=='NDX'?`<p>当前标的尚未接入可核验的 PE、EPS 和成员宽度历史；价格、波动率与宏观读数仍按上方来源单独展示，未知项不计分。</p>`:'<p>此历史快照未保存扩展证据，请按该日期更新以获取。</p>');
}

function renderVerification(s){
 const t=s.trigger,v=s.verification;
 if(!t||!v){$('triggerSummary').textContent='此旧快照尚未计算回撤触发记录，请按该日期更新。';$('triggerLevels').innerHTML='';$('triggerEvents').innerHTML='';$('triggerNote').textContent='';$('verificationState').textContent='旧快照待更新';$('verificationRows').innerHTML='';$('verificationNote').textContent='';return}
 $('triggerSummary').textContent=`截至 ${t.date}，历史最高收盘 ${fmt(t.peak)}（${t.peak_date}），当前 ${fmt(t.close)}。回撤 = 1 − 当前 / 高点 = ${percent(t.drawdown)}。${t.fresh?(t.triggered?'已达到第 '+t.current_tier+' 档，启动证据验证。':'尚未达到10%触发档。'):'行情已过期，先更新。'}`;
 $('triggerLevels').innerHTML='<table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr><th style="text-align:left;padding:8px">回撤档</th><th>对应点位</th><th>当前状态</th><th>本轮记录</th></tr></thead><tbody>'+t.levels.map(r=>`<tr><td style="padding:10px;border-top:1px solid #d4ded7">${percent(r.drawdown)}</td><td style="text-align:center">${fmt(r.price)}</td><td style="text-align:center">${r.currently_breached?'已达到':'未达到'}</td><td style="text-align:center">${r.reviewed_in_episode?'已触发过':'尚未触发'}</td></tr>`).join('')+'</tbody></table>';
 $('triggerNote').textContent=t.note+' 高点来自已获取历史收盘数据，不采用盘中高点。';
 $('triggerEvents').innerHTML=t.recent_events.slice().reverse().map(e=>`<p>${esc(e.date)} · 首次进入 ${e.tier*10}% 档 · 当日回撤 ${percent(e.drawdown)} · 该轮高点 ${fmt(e.peak)}（${esc(e.peak_date)}）</p>`).join('')||'尚无触发记录';
 $('verificationState').textContent=v.state;
 $('verificationRows').innerHTML=v.groups.map(g=>`<div style="padding:12px 0;border-bottom:1px solid #d4ded7"><strong>${esc(g.label)} · ${esc(g.state)}</strong><p style="font-size:13px;margin-top:6px">${g.missing.length===g.keys.length?'尚无可计分值':'已知 '+fmt(g.known)+' / '+g.maximum+' 分'}${g.threshold==null?'（辅助，无独立通过门槛）':'；门槛 '+g.threshold+' 分'}${g.missing.length?'。待核验：'+esc(g.missing.join('、')):''}</p></div>`).join('');
 $('verificationNote').textContent=v.note;
 const records=s.earnings_observations||[];
 const emptyEarnings=s.profile?.asset_type==='commodity_future'?'黄金不是股票，没有同财政年度 EPS；本模块保持未知，不用 PE/EPS 替代金价。':'没有取得同财政年度的绝对EPS预测值，不能把已显示的滚动EPS指数当作下一年EPS。下方列出已取得的盈利观察。';
 $('earningsBody').innerHTML+='<details style="margin-top:12px"><summary>具体预测值与日期</summary>'+ (records.length?records.map(r=>`<p style="font-size:12px;margin:10px 0">${esc(r.provider)} · FY ${r.fiscal_year} · EPS ${fmt(r.value)} ${esc(r.currency)}<br>观察 ${esc(r.date)}；首次可用 ${esc(r.available_at)}<br>${esc(r.basis)}</p>`).join(''):`<p style="font-size:12px;margin-top:10px">${emptyEarnings}</p>`)+'</details>';
 for(const z of (s.quality||[]).filter(z=>z.module==='earnings'))$('earningsBody').innerHTML+=`<p style="font-size:12px;margin-top:12px"><strong>${esc(z.label)}：${fmt(z.value)}</strong><br>观察 ${esc(z.date)} · 2024年初=100<br>仅为指数化盈利观察，不进入同年度EPS修正计分。</p>`;
 if(!$('exampleHigh').value){$('exampleHigh').value=t.peak;$('examplePrice').value=t.close;}calculateExample();
}
function calculateExample(){const high=+$('exampleHigh').value,price=+$('examplePrice').value;if(!Number.isFinite(high)||!Number.isFinite(price)||high<=0||price<=0||price>high){$('exampleResult').textContent='请输入正数，且最高点不得低于当前点位。';return}const dd=1-price/high;$('exampleResult').textContent=`10%回撤对应 ${fmt(high*.9)}，20%对应 ${fmt(high*.8)}。当前回撤 ${percent(dd)}，${dd+1e-12>=.1?'已达到10%验证触发档':'未达到10%验证触发档'}。这只是价格触发，不代表加仓验证通过。`}
$('exampleHigh').oninput=calculateExample;$('examplePrice').oninput=calculateExample;
$('realWorldProfileForm')?.addEventListener('submit',event=>{event.preventDefault();previewRealWorldProfile()});

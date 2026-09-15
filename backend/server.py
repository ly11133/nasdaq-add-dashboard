"""Local evidence dashboard. Bind loopback only; no trades, credentials or uploads to third parties."""
from pathlib import Path
from datetime import datetime, timezone, date
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import csv
import os
import sys,json,threading,sqlite3,hashlib,math,io,contextlib,argparse,time
import pandas as pd
ROOT=Path(__file__).resolve().parent
COLLECT_ROOT=ROOT.parent/'dashboard_data'
if not COLLECT_ROOT.exists():
    # Cloud bundles keep the collector beside the application so the service
    # can be deployed as one Render/Fly-style root directory.
    COLLECT_ROOT=ROOT/'dashboard_data'
sys.path.insert(0,str(COLLECT_ROOT))
from collect import collect,history_stats
from evidence_quality import assess
from free_analysis import analyze
from verification import trigger_path, verification, execution_status, RULES
from profiles import PROFILES, PROFILE_ORDER, get_profile, profile_list
from data_contract import CONTRACT_VERSION
from pit_repository import PITRepository, migrate_database, register_score_model, score_output_fingerprint
from score_model import CURRENT_SCORE_MODEL_VERSION
from strict_pit import MODES, STRICT_MODE, evaluate_strict_pit
from phase1c_report import build_report
from replay_engine import REPLAY_MODES, replay_date_view, run_replay
from phase2a_report import build_report as build_phase2a_report
from phase2b_report import build_report as build_phase2b_report
from phase2b_research import blind_proxy_date_view, run_proxy_research
from phase2c_report import build_report as build_phase2c_report
from phase2c_research import blind_episode_date_view, run_episode_validation
from phase2d_report import build_report as build_phase2d_report
from phase2d_research import blind_overlay_event_date_view, run_overlay_validation
from phase2e_report import build_report as build_phase2e_report
from phase2e_research import blind_tactical_event_date_view, run_tactical_validation
from phase2f_report import build_report as build_phase2f_report
from phase2f_research import blind_capital_run, run_capital_feasibility_validation
from phase3a_report import build_report as build_phase3a_report
from phase3a_state_machine import (
    DEFAULT_CAPITAL_PROFILE,
    STATE_MACHINE_MODEL_VERSION,
    STATE_MACHINE_CONFIG_HASH,
    next_trigger_preview,
    normalize_capital_profile,
    run_phase3a_validation,
)
from phase3b_parameterization import (
    REAL_WORLD_CONFIG,
    REAL_WORLD_CONFIG_HASH,
    REAL_WORLD_MODEL_VERSION,
    TARGET_SPECS,
    standard_profiles,
    normalize_real_world_profile,
    replay_real_world_profile,
    report_from_repository as phase3b_report_from_repository,
    run_phase3b_validation,
)
DATA=ROOT/'data'; DATA.mkdir(exist_ok=True)
DB=Path(os.environ.get('DASHBOARD_DB',str(DATA/'dashboard.sqlite3')))
CLOUD_MODE=os.environ.get('CLOUD_MODE','0').lower() in {'1','true','yes','on'}
PUBLIC_BUNDLE_PATH=ROOT/'static-data.json'
_PUBLIC_BUNDLE=None
LOCK=threading.Lock()
JOB_LOCK=threading.Lock()
JOB_STARTED_MONOTONIC=None
JOB={'running':False,'stage':'idle','progress':0.0,'message':'尚未请求联网更新','started_at':None,'updated_at':None,'finished_at':None,'elapsed_seconds':0.0}

def _update_job(**changes):
    """Publish a small, thread-safe progress snapshot for the local UI."""

    global JOB
    with JOB_LOCK:
        next_job=dict(JOB)
        next_job.update(changes)
        if JOB_STARTED_MONOTONIC is not None:
            next_job['elapsed_seconds']=round(max(0.0,time.monotonic()-JOB_STARTED_MONOTONIC),1)
        next_job['updated_at']=datetime.now(timezone.utc).isoformat()
        JOB=next_job

def _job_snapshot():
    with JOB_LOCK:return dict(JOB)

def _public_bundle():
    """Load the sanitized read-only bundle once for cloud cold starts.

    Free web services have an ephemeral filesystem.  The bundle is therefore
    the recovery point after a restart; it never replaces a freshly collected
    SQLite snapshot while the process is alive.
    """
    global _PUBLIC_BUNDLE
    if _PUBLIC_BUNDLE is not None:return _PUBLIC_BUNDLE
    try:
        _PUBLIC_BUNDLE=json.loads(PUBLIC_BUNDLE_PATH.read_text(encoding='utf-8'))
    except (OSError,TypeError,json.JSONDecodeError):
        _PUBLIC_BUNDLE={}
    return _PUBLIC_BUNDLE

def _bundle_value(key,default=None):
    value=_public_bundle().get(key,default)
    return default if value is None else value
# Kept as a compatibility alias for older imports and tests.  New snapshots
# always carry the selected profile in their own payload.
PROFILE=PROFILES['NDX']

def blind_replay_run(run):
    """Return replay metadata without the stored evaluation summary.

    Completed ``replay_runs.summary`` contains forward-return statistics. The
    default run/list endpoints are used by the historical Decision View, so
    they must not leak that evaluation payload unless the caller explicitly
    asks for ``include=report`` or ``/api/replay-outcomes``.
    """
    if not isinstance(run,dict):return run
    return {key:value for key,value in run.items() if key not in {'summary','error'}}

def blind_proxy_research_run(run):
    """Return proxy-run metadata without the stored evaluation summary."""
    if not isinstance(run,dict):return run
    return {key:value for key,value in run.items() if key not in {'summary','error'}}

def blind_episode_validation_run(run):
    """Return episode-run metadata without the stored evaluation summary."""
    if not isinstance(run,dict):return run
    return {key:value for key,value in run.items() if key not in {'summary','error'}}

def blind_drawdown_overlay_run(run):
    """Return overlay-run metadata without stored result summaries."""
    if not isinstance(run,dict):return run
    return {key:value for key,value in run.items() if key not in {'summary','error'}}

def _blind_tactical_run(run):
    """Return tactical-run metadata without future evaluation summaries."""
    if not isinstance(run,dict):return run
    return {key:value for key,value in run.items() if key not in {'summary','error'}}

def _blind_capital_feasibility_run(run):
    """Return capital-feasibility metadata without stored aggregate results."""
    return blind_capital_run(run)

def _public_phase3a_report(record):
    """Keep the internal replay cache out of ordinary API responses.

    The report row retains ``_run_result`` for idempotent re-opening of a
    completed batch. That cache contains every path's daily snapshots; callers
    can request the explicit state/event endpoints instead of downloading it
    on a normal status request.
    """
    if not isinstance(record, dict):
        return record
    report = record.get('report')
    if not isinstance(report, dict) or '_run_result' not in report:
        return record
    payload = dict(record)
    payload['report'] = dict(report)
    payload['report'].pop('_run_result', None)
    return payload

def _blind_real_world_run(run):
    """Return Phase 3B run metadata without stored scenario summaries."""
    if not isinstance(run,dict):return run
    return {key:value for key,value in run.items() if key not in {'summary','error','config','data_snapshot'}}

def _phase3b_context(repo, phase3a_batch_id, end_date):
    """Resolve the immutable Phase 3A inputs visible by ``end_date``.

    The event stream and the latest daily state come from the same completed C
    run.  Keeping this lookup point-in-time bounded prevents a preview from
    accidentally using a later snapshot or a D-ladder event stream.
    """
    runs=repo.get_capital_state_machine_runs(market='NDX',batch_id=phase3a_batch_id,ladder_id='C',limit=20) if phase3a_batch_id else repo.get_capital_state_machine_runs(market='NDX',ladder_id='C',limit=100)
    run=next((item for item in runs if item.get('status')=='COMPLETED'),None)
    if not run:return [],None,None
    run_id=run['state_machine_run_id']
    events=repo.get_capital_events(run_id,as_of=end_date,limit=1000000)
    states=repo.get_capital_snapshots(run_id,as_of=end_date,limit=2000000)
    state=dict(states[-1]) if states else {}
    # Phase 3A's capital snapshot deliberately stores only capital fields. For
    # a useful next-price preview, enrich it with the corresponding Phase 2E
    # tactical state at the same cutoff. This remains point-in-time bounded and
    # never substitutes a later close or peak.
    phase2e_run_id=run.get('phase2e_run_id')
    if phase2e_run_id:
        tactical=[item for item in repo.get_tactical_drawdown_states(phase2e_run_id,limit=2000000) if str(item.get('as_of_date',''))[:10] <= str(end_date)[:10]]
        if tactical:
            tactical_state=tactical[-1]
            for key in ('as_of_date','tactical_cycle_id','close_price','rolling_high_252','tactical_peak_price','tactical_drawdown','ath_drawdown'):
                if key in tactical_state: state[key]=tactical_state[key]
            state['source_phase2e_run_id']=phase2e_run_id
    return events,run.get('batch_id'),(state or None)


def _phase3b_events(repo, phase3a_batch_id, end_date):
    """Compatibility wrapper returning only the event stream and batch id."""
    events,batch,_state=_phase3b_context(repo,phase3a_batch_id,end_date)
    return events,batch


def _real_world_preview_metadata(source_state, result, ladder_id):
    """Attach a currency-aware next-trigger preview to a cash replay.

    Phase 3A supplies the contemporaneous tactical drawdown and peak price;
    Phase 3B supplies the profile's current cash, target and cap.  No future
    price or post-cutoff state is introduced, and the preview never creates an
    order.
    """
    final_state=result.get('final_state') or {}
    state=dict(source_state or {})
    # The real-world replay is authoritative for cash and for the bands that
    # were consumed inside the requested date window.  Trigger fields remain
    # sourced from the point-in-time Phase 3A snapshot.
    for key in ('available_opportunity_cash','target_opportunity_cash','opportunity_fund_cap'):
        if key in final_state: state[key]=final_state[key]
    if not source_state:
        for key in ('used_bands','tactical_cycle_id'):
            if key in final_state: state[key]=final_state[key]
    if 'tactical_drawdown' not in state:
        event_rows=result.get('events') or []
        state['tactical_drawdown']=event_rows[-1].get('trigger_drawdown',0.0) if event_rows else 0.0
    preview=next_trigger_preview(
        state,
        ladders=('C','D'),
        current_price=state.get('close_price'),
        tactical_peak_price=state.get('tactical_peak_price'),
    )
    selected=str(ladder_id or 'C').upper()
    plan=(preview.get('ladder_plans') or {}).get(selected) or {}
    return {
        'next_trigger_preview': preview,
        'next_trigger_band': preview.get('next_trigger_band'),
        'next_trigger_threshold': preview.get('next_trigger_threshold'),
        'next_trigger_price': preview.get('next_trigger_price'),
        'next_trigger_planned_amount': plan.get('planned_amount'),
        'next_trigger_cash_sufficient': plan.get('cash_sufficient'),
        'next_trigger_shortfall': plan.get('shortfall'),
        'next_trigger_ladder': selected,
        'source_phase3a_state_date': source_state.get('as_of_date') if isinstance(source_state,dict) else None,
        'future_price_used': False,
        'auto_trade': False,
    }

def corrected_replay_report(record):
    """Expose the corrected V2 conclusion without mutating its audit row."""
    if not isinstance(record,dict):return record
    report=record.get('report')
    if not isinstance(report,dict):return record
    decisions=report.get('decision_distribution') or {}
    score_distribution=report.get('score_distribution') or {}
    high_tail=sum(int(score_distribution.get(key,0) or 0) for key in ('80-90','90+'))
    blocked=sum(int(value or 0) for key,value in decisions.items() if key!='INSUFFICIENT_EVIDENCE')==0
    if report.get('current_score_information_value')=='NONE' and decisions and high_tail==0 and blocked:
        payload=dict(record);payload['report']=dict(report)
        payload['report']['current_score_information_value']='INCONCLUSIVE'
        payload['report']['information_value_correction']='No comparable high-score tail; all replay decisions were blocked by coverage, so value is INCONCLUSIVE rather than NONE.'
        return payload
    return record

def db():
    # Phase 1A/1B PIT tables are migrated before opening the legacy tables. The
    # migration is idempotent and creates a backup before its first change.
    migrate_database(DB,backup=not CLOUD_MODE)
    register_score_model(DB)
    c=sqlite3.connect(DB);c.execute('CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY, asof TEXT, payload TEXT)');c.execute('CREATE TABLE IF NOT EXISTS evidence(hash TEXT PRIMARY KEY,payload TEXT)');return c

def save(s):
    s['id']=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    s.setdefault('score_model_version', CURRENT_SCORE_MODEL_VERSION)
    s.setdefault('pit_contract_version', CONTRACT_VERSION)
    s.setdefault('mode', 'RESEARCH_PROXY')
    with db() as c:c.execute('INSERT INTO snapshots VALUES(?,?,?)',(s['id'],s['asof'],json.dumps(s,ensure_ascii=False,allow_nan=False)))
    return s

def datecheck(v):
    if not isinstance(v,str) or len(v)!=10:raise ValueError('日期必须为 YYYY-MM-DD')
    return date.fromisoformat(v)

def weekday_age(observed, reference):
    """Number of weekdays strictly after observed through reference."""
    try:
        start=date.fromisoformat(str(observed)[:10]);end=date.fromisoformat(str(reference)[:10])
    except (TypeError,ValueError):
        return None
    if end<=start:return 0
    return sum((start+pd.Timedelta(days=i)).weekday()<5 for i in range(1,(end-start).days+1))

def validate(rows):
    if not isinstance(rows,list) or not rows or len(rows)>20000:raise ValueError('records 需要 1–20000 条记录')
    allowed={'forward_pe','ttm_pe','eps_estimate','breadth'}
    for r in rows:
        if not isinstance(r,dict):raise ValueError('每条证据必须是对象')
        for k in ['metric','value','date','available_at','provider','basis','source_url','index']:
            if k not in r:raise ValueError('缺少字段：'+k)
        if r['index']!='NDX' or r['metric'] not in allowed:raise ValueError('只支持 NDX 的 PE / EPS / breadth 原始证据')
        if isinstance(r['value'],bool) or not isinstance(r['value'],(int,float)) or not math.isfinite(r['value']):raise ValueError('value 必须是有限数值')
        if r['value']<=0 and r['metric'] in ['forward_pe','ttm_pe']:raise ValueError('PE 必须为正数；EPS 可为零或负值')
        if r['metric']=='breadth' and not 0<=r['value']<=1:raise ValueError('breadth 必须在 0–1')
        if datecheck(r['available_at'])<datecheck(r['date']):raise ValueError('available_at 不能早于观察日')
        if datecheck(r['date'])>date.today() or datecheck(r['available_at'])>date.today():raise ValueError('不能导入未来证据')
        for k in ['provider','basis','source_url']:
            if not isinstance(r[k],str) or not 1<=len(r[k])<=1500:raise ValueError(k+' 无效')
        if urlparse(r['source_url']).scheme!='https':raise ValueError('来源必须为 https URL')
        if r['metric']=='eps_estimate' and (not isinstance(r.get('fiscal_year'),int) or not r.get('currency')):raise ValueError('EPS 需要 fiscal_year 和 currency')
        if r['metric']=='breadth' and (r.get('membership_date')!=r['date'] or r.get('coverage')!=1):raise ValueError('宽度需要当日成分名单日期及 coverage=1；不接受不完整分母')
    seen={}
    for r in rows:
        key=(r['metric'],r['date'],r['provider'],r['basis'],r.get('fiscal_year'),r.get('currency'))
        if key in seen and seen[key]!=r:raise ValueError('同一口径日期存在冲突值；请先核对数据')
        seen[key]=r
    return list(seen.values())

def parse_csv(content):
    if not isinstance(content,str):raise ValueError('CSV 必须为文本')
    reader=csv.DictReader(io.StringIO(content.lstrip('\ufeff')))
    if not reader.fieldnames or len(reader.fieldnames)!=len(set(reader.fieldnames)):raise ValueError('CSV 缺少表头或存在重复列')
    rows=[]
    for line,row in enumerate(reader,2):
        if None in row:raise ValueError(f'CSV 第 {line} 行列数超出表头')
        r={k:v.strip() for k,v in row.items() if v is not None and v.strip()}
        if not r:continue
        try:
            for k in ['value','coverage']:
                if k in r:r[k]=float(r[k])
            if 'fiscal_year' in r:r['fiscal_year']=int(r['fiscal_year'])
        except ValueError:raise ValueError(f'CSV 第 {line} 行数值格式错误')
        rows.append(r)
    return validate(rows)

def rsi(values):
    changes=[b-a for a,b in zip(values,values[1:])]
    if len(changes)<100:return None
    gain=sum(max(x,0) for x in changes[:14])/14;loss=sum(max(-x,0) for x in changes[:14])/14
    for x in changes[14:]:gain=(gain*13+max(x,0))/14;loss=(loss*13+max(-x,0))/14
    return 50 if gain==loss==0 else 100 if loss==0 else 100-100/(1+gain/loss)

def _formal_pit_history(series_id, asof):
    """Read only score-eligible versions through the PIT SQL boundary.

    Phase 1A archives public feeds as proxies, so this normally returns an
    empty list. Keeping the lookup here makes the future formal path explicit:
    it cannot silently fall back to reading all rows and filtering in Python.
    """
    try:
        return PITRepository(DB).get_history_available(series_id, asof)
    except (OSError, sqlite3.Error, ValueError):
        return []

def _formal_series_id(profile_id, price_metric):
    if profile_id=='NDX' and price_metric=='NASDAQ100':return 'NDX_CLOSE'
    if profile_id=='NDX' and price_metric=='VXNCLS':return 'NDX_VXN'
    if profile_id=='NDX' and price_metric=='VIXCLS':return 'VIX'
    if profile_id=='NDX' and price_metric=='DFII10':return 'US10Y_REAL'
    if profile_id=='NDX' and price_metric=='NFCI':return 'US_NFCI'
    return None

def build(asof,audit,profile_id='NDX',mode='RESEARCH_PROXY'):
    mode=str(mode).upper()
    if mode not in MODES:raise ValueError('无效评估模式')
    # STRICT_PIT is a hard cutover.  Returning here prevents the compatibility
    # CSV/evidence path below from being called accidentally by a strict
    # caller.  The strict evaluator itself writes the append-only Decision
    # Log, including insufficient-evidence results.
    if mode==STRICT_MODE:return evaluate_strict_pit(DB,asof,market=profile_id)
    profile=get_profile(profile_id);profile_id=profile['id']
    cutoff=pd.Timestamp(asof);folder=Path(audit['snapshot_directory']);raw=pd.read_csv(folder/'normalized.csv')
    price_metric=profile.get('price_series') if profile.get('price_source_type')=='fred' else 'YAHOO_PRICE'
    seed_all=pd.read_csv(DATA/'price-history.csv')[['date','price']] if profile_id=='NDX' else pd.DataFrame(columns=['date','price'])
    raw_dates=pd.to_datetime(raw['observation_date'],errors='coerce')
    raw_future=set(raw.loc[(raw['metric']==price_metric) & (raw_dates>cutoff),'observation_date'].astype(str))
    seed_future=set(seed_all.loc[pd.to_datetime(seed_all['date'])>cutoff,'date'].astype(str))
    price_future_count=len(raw_future|seed_future)
    formal_price=_formal_pit_history(_formal_series_id(profile_id,price_metric),asof) if mode!='LEGACY' and _formal_series_id(profile_id,price_metric) else []
    if len(formal_price)>=200:
        # A formally eligible history is already filtered by available_at in
        # SQL. Do not splice it with the legacy current-history proxy.
        series=pd.DataFrame([{'date':r['observation_date'],'price':float(r['value'])} for r in formal_price])
        price_future_count=0
    else:
        series=raw[raw.metric==price_metric][['observation_date','value']].rename(columns={'observation_date':'date','value':'price'}).copy()
        series['date']=series['date'].astype(str).str[:10]
    # Retained NDX observations are explicitly labelled and are never used as
    # vintage earnings.  ETF profiles use only the selected adjusted-close
    # proxy; no NDX seed is silently reused for another asset.
    seed=seed_all
    series=(pd.concat([seed,series]) if not seed.empty else series).drop_duplicates('date',keep='last').sort_values('date').reset_index(drop=True);series=series[series.date<=asof]
    if len(series)<200:raise ValueError('价格不足 200 个交易日，无法评估')
    prices=series.price.tolist();last=series.iloc[-1];ath=float(series.price.max());dd=1-float(last.price)/ath;rv=rsi(prices)
    q={'date':last.date,'close':float(last.price),'ath':ath,'athDate':series.loc[series.price.idxmax(),'date'],'drawdown':-dd,'rsi':rv,'ma200':sum(prices[-200:])/200,'historyStart':series.iloc[0].date}
    trigger=trigger_path([{'date':r.date,'value':float(r.price)} for _,r in series.iterrows()],asof)
    rows=[]
    def add(key,label,module,weight,value=None,score=None,obs=None,reason='',source='',basis=''):
        formula,category,calculation=RULES[key]
        rows.append(dict(formula=formula,category=category,calculation=calculation,key=key,label=label,module=module,weight=weight,value=value,score=score,date=obs,status='scored' if score is not None else 'candidate' if value is not None else 'missing',reason=reason,source=source,basis=basis))
    fresh=(cutoff-pd.Timestamp(last.date)).days<=7
    price_url=profile.get('price_source_url','https://fred.stlouisfed.org/series/NASDAQ100')
    price_label=profile.get('short_label',profile_id)
    price_basis=profile.get('price_basis','公共收盘序列')
    add('drawdown','历史高点回撤','pressure',20,-dd,20*min(dd/.4,1) if fresh else None,last.date,'跌幅体现价格压力，不能证明盈利便宜。高点取自 '+q['historyStart']+' 起已获取历史。价格口径：'+price_basis+'。',price_url,price_basis)
    add('rsi','RSI(14)','technical',5,rv,5*max(0,min(1,(50-rv)/20)) if fresh and rv is not None else None,last.date,'Wilder 平滑；全历史预热；仅辅助，不确认底部。价格口径：'+price_basis+'。',price_url,price_basis)
    macro_specs=[(profile.get('volatility_series','VXNCLS'),'vxn',profile.get('volatility_label','VXN'),'pressure',5,False,7),('DFII10','real','10Y 实际利率','macro',5,True,7),('NFCI','nfci','NFCI','macro',5,True,14)]
    if profile_id=='NDX':macro_specs.append(('VIXCLS','vix','VIX（背景）','pressure',0,False,7))
    for sid,key,label,mod,weight,invert,maxage in macro_specs:
        formal_macro_id=_formal_series_id(profile_id,sid)
        formal_macro=_formal_pit_history(formal_macro_id,asof) if mode!='LEGACY' and formal_macro_id else []
        if len(formal_macro)>=60:
            z=history_stats([{'date':r['observation_date'],'value':r['value']} for r in formal_macro],cutoff)
            z['basis']='PIT repository; available_at <= as_of_datetime'
        else:
            z=next((v for k,v in audit['audit'].items() if k.endswith(':'+sid)),{})
        p=z.get('percentile_candidate');value=z.get('value');ok=p is not None and z.get('age_days',999)<=maxage
        add(key,label,mod,weight,value,weight*((1-p) if invert else p) if ok else None,z.get('date'),'此前 '+str(z.get('history_months',0))+' 个月末；分位 '+(f'{p:.1%}' if p is not None else '不足 60 月')+('；数据过期' if z.get('age_days',999)>maxage else '')+'。'+('历史查询使用 ALFRED 日期版本，未认证盘中发布时间。' if sid=='NFCI' else ''),'https://fred.stlouisfed.org/series/'+sid,z.get('basis',''))
    with db() as c:evidence=[json.loads(x[0]) for x in c.execute('SELECT payload FROM evidence')]
    evidence_future_count=sum(r['date']>asof or r['available_at']>=asof for r in evidence)
    evidence=[r for r in evidence if r['date']<=asof and r['available_at']<asof] # Date-only releases: conservatively next day.
    def groups(metric):
        result={}
        for r in evidence:
            if r['metric']==metric:result.setdefault((r['provider'],r['basis'],r.get('currency')),[]).append(r)
        return [sorted(v,key=lambda r:r['date']) for v in result.values()]
    if profile_id=='NDX':
        for key,label,weight,sourcekey in [('forward_pe','Forward PE',20,'hom_pe:forward'),('ttm_pe','TTM PE',10,'hom_pe:trailing')]:
            candidates=[]
            for g in groups(key):
                z=history_stats([{'date':r['date'],'value':r['value']} for r in g],cutoff);candidates.append((g[-1],z))
            usable=[x for x in candidates if x[1].get('percentile_candidate') is not None and x[1]['age_days']<=14]
            if usable:
                r,z=max(usable,key=lambda x:x[0]['date']);add(key,label,'valuation',weight,r['value'],weight*(1-z['percentile_candidate']),r['date'],'用户授权来源；此前 '+str(z['history_months'])+' 个月末分位 '+f"{z['percentile_candidate']:.1%}"+'；导入声明尚未由平台独立认证。',r['source_url'],r['basis'])
            elif candidates:
                r,z=max(candidates,key=lambda x:x[0]['date'])
                add(key,label,'valuation',weight,r['value'],None,r['date'],f"已导入原始值；同口径历史 {z.get('history_months',0)} 月（至少60月），距观察 {z.get('age_days')} 天（至多14天），资格未满足。",r['source_url'],r['basis'])
            else:
                z=audit['audit'].get(sourcekey,{});add(key,label,'valuation',weight,z.get('value'),None,z.get('date'),'公开观察值未获评分资格：逐条发布时间或指数聚合口径未认证；导入同来源至少60个月历史及首次可用日期后可参与计算。','https://historyofmarket.com/nasdaq/',z.get('basis',''))
        revisions=[];growths=[]
        for g in groups('eps_estimate'):
            for fy in {r['fiscal_year'] for r in g if cutoff.year<=r['fiscal_year']<=cutoff.year+1}:
                same=[r for r in g if r['fiscal_year']==fy];now=same[-1]
                if (cutoff-pd.Timestamp(now['date'])).days>45:continue
                target=pd.Timestamp(now['date'])-pd.DateOffset(months=3)
                old=[r for r in same if 0<=(target-pd.Timestamp(r['date'])).days<=15]
                if old and old[-1]['value']>0:revisions.append((now,now['value']/old[-1]['value']-1))
                prior=[r for r in g if fy==cutoff.year+1 and r['fiscal_year']==fy-1 and r['date']==now['date']]
                if prior and prior[-1]['value']>0:growths.append((now,now['value']/prior[-1]['value']-1))
        for key,label,weight,items in [('revision','同财政期 EPS 修正','15',revisions),('growth','下一财政年 EPS 增长','10',growths)]:
            w=int(weight)
            if items:
                r,v=max(items,key=lambda x:(x[0]['date'],x[0]['fiscal_year']));score=(15 if v>=.05 else 10 if v>=0 else 5 if v>=-.05 else 0) if key=='revision' else (10 if v>=.15 else 7 if v>=.05 else 3 if v>=0 else 0)
                add(key,label,'earnings',w,v,score,r['date'],'同提供商、同币种、同聚合口径；FY '+str(r['fiscal_year'])+'；用户导入，发布时间按声明。',r['source_url'],r['basis'])
            else:add(key,label,'earnings',w,reason='尚无合格的同财政期预测记录。滚动 NTM 的变化包含时间滚动，不可冒充盈利修正。')
        bg=[r for g in groups('breadth') for r in g if (cutoff-pd.Timestamp(r['date'])).days<=7]
        if bg:
            r=max(bg,key=lambda r:r['date']);add('breadth','站上 MA200 比例','breadth',5,r['value'],5*r['value'],r['date'],'当日成员，全成员覆盖；用户导入声明。',r['source_url'],r['basis'])
        else:
            z=audit['audit'].get('hom_breadth:pct200',{})
            add('breadth','站上 MA200 比例','breadth',5,z.get('value'),None,z.get('date'),'已接入公开宽度；提供商声明使用当前成分股，成员数 '+str(z.get('members','未知'))+'，未证明逐日成员与完整分母。可作观察，不用于历史评分。','https://historyofmarket.com/nasdaq/ndx-breadth/',z.get('basis',''))
        quality=assess(audit,asof)
    else:
        missing_reason=profile.get('fundamental_status','该标的尚未接入合格基本面数据')+'；未知项不计分，也不把价格跌幅当作盈利证据。'
        add('forward_pe','Forward PE','valuation',20,reason=missing_reason,source=profile.get('price_source_url',''),basis=profile.get('price_basis',''))
        official=audit['audit'].get('csindex_perf:ttm_pe',{})
        official_ok=official.get('percentile_candidate') is not None and official.get('age_days',999)<=14 and asof>=date.today().isoformat()
        if official.get('value') is not None:
            p=official.get('percentile_candidate')
            reason=('中证指数官方同口径历史；此前 '+str(official.get('history_months',0))+' 个月末；分位 '+(f'{p:.1%}' if p is not None else '不足60月')+'。当前快照保存了本次取得时间。')
            if not official_ok: reason+=' 历史日期查询不使用当前回填序列计分。'
            add('ttm_pe','TTM PE','valuation',10,official['value'],10*(1-p) if official_ok else None,official.get('date'),reason,'https://www.csindex.com.cn/zh-CN/indices/index-detail/'+profile.get('csindex_code',''),official.get('basis',''))
        else:
            add('ttm_pe','TTM PE','valuation',10,reason=missing_reason,source=profile.get('price_source_url',''),basis=profile.get('price_basis',''))
        add('revision','同财政期 EPS 修正','earnings',15,reason=missing_reason,source=profile.get('price_source_url',''),basis=profile.get('price_basis',''))
        add('growth','下一财政年 EPS 增长','earnings',10,reason=missing_reason,source=profile.get('price_source_url',''),basis=profile.get('price_basis',''))
        breadth=audit['audit'].get('csindex_cons:breadth',{})
        breadth_ok=breadth.get('value') is not None and breadth.get('coverage',0)>=.95 and asof>=date.today().isoformat() and (cutoff-pd.Timestamp(breadth.get('date'))).days<=7
        if breadth.get('value') is not None:
            reason=f"当前成分 {breadth.get('members',0)} 只，有效 MA200 {breadth.get('valid_members',0)} 只，覆盖 {breadth.get('coverage',0):.1%}。"
            if not breadth_ok:reason+='覆盖不足95%、数据过期或属于历史日期时不计分。'
            add('breadth','成分股站上 MA200 比例','breadth',5,breadth['value'],5*breadth['value'] if breadth_ok else None,breadth.get('date'),reason,'https://www.csindex.com.cn/zh-CN/indices/index-detail/'+profile.get('csindex_code',''),breadth.get('basis',''))
        else:
            add('breadth','成分股站上 MA200 比例','breadth',5,reason='当前免费版未取得该标的完整成分名单与至少200日有效行情；未知项不计分。',source=profile.get('price_source_url',''),basis='未接入')
        quality=[]
    for item in quality:
        source,metric=item['key'].split(':',1)
        subset=raw[(raw.source==source)&(raw.metric==metric)&(raw.observation_date<=asof)].sort_values('observation_date').tail(260)
        item['history']=[{'date':r.observation_date,'value':float(r.value)} for _,r in subset.iterrows()]
    scored=[r for r in rows if r['score'] is not None];known=sum(r['score'] for r in scored);coverage=sum(r['weight'] for r in scored);unknown=100-coverage
    mapping={r['key']:r for r in rows};v=sum(mapping[k]['score'] or 0 for k in ['forward_pe','ttm_pe']);e=sum(mapping[k]['score'] or 0 for k in ['revision','growth'])
    decision='暂无法判断'
    if coverage==100:
        decision='额外加仓吸引力偏弱' if known<40 else '证据分歧，继续观察' if known<60 else '支持适度加仓' if v>=15 and e>=15 else '核心证据未通过'
        if known>=75 and v>=18 and e>=20:decision='加仓证据较强'
        if mapping['revision']['value']<=-.1:decision='盈利下修明显，继续观察'
    verification_result=verification(rows,trigger)
    free=analyze(q,rows,quality,asof,profile)
    execution=execution_status(trigger,verification_result,rows)
    market_age=weekday_age(q['date'],date.today().isoformat())
    historical=asof<date.today().isoformat()
    limitations=['评分规则未完成无前视回测，分数不是收益概率。','公开行情采用当前历史序列；此查询不是完全时点认证回测。','日期型发布数据在次日才使用，用户导入来源需自行核验。']
    if profile_id!='NDX':
        limitations.extend([f"{price_label} 使用 {price_basis}。",profile.get('fundamental_status','基本面数据尚未接入')+'；因此评分覆盖不足时不会输出强加仓。'])
    rough=[dict(key=k.split(':',1)[1],**v) for k,v in audit['audit'].items() if k.startswith('eastmoney_forecast:rough_')]
    model=register_score_model(DB)
    score_fingerprint=score_output_fingerprint(model['model_version'], {'asof':asof,'rows':rows}, {'known':known,'coverage':coverage,'decision':decision})
    return dict(schema=2,mode=mode,profile=profile,profile_id=profile_id,asof=asof,retrieved_at=audit['retrieved_at'],created_at=datetime.now(timezone.utc).isoformat(),score_model_version=model['model_version'],score_output_fingerprint=score_fingerprint,pit_contract_version=CONTRACT_VERSION,quote=q,rows=rows,known=known,coverage=coverage,upper=known+unknown,decision=decision,execution=execution,free_analysis=free,rough_estimates=rough,prices=[{'date':r.date,'value':float(r.price)} for _,r in series.tail(520).iterrows()],sources=json.loads((folder/'manifest.json').read_text()),audit=audit['audit'],quality=quality,trigger=trigger,verification=verification_result,earnings_observations=sorted([r for r in evidence if r['metric']=='eps_estimate' and cutoff.year<=r['fiscal_year']<=cutoff.year+1],key=lambda r:(r['date'],r['fiscal_year']),reverse=True)[:12],point_in_time={'cutoff':asof,'price_rows_after_cutoff':price_future_count,'evidence_rows_after_cutoff':evidence_future_count,'availability_rule':'only imported evidence with available_at < asof; current public series are date-truncated but publication-vintage certification is not implied'},freshness={'market_age_business_days':market_age,'market_observation_date':q['date'],'should_refresh':(not historical and market_age is not None and market_age>=2)},audit_directory=str(folder),historical=historical,limitations=limitations,pit_repository={'contract_version':CONTRACT_VERSION,'formal_query_boundary':'available_at <= as_of_datetime','legacy_evidence_path':'retained for compatibility; not the Phase 1B strict PIT path'})

def refresh(asof,profile_id='NDX'):
    global JOB_STARTED_MONOTONIC
    try:
        profile=get_profile(profile_id);profile_id=profile['id']
        JOB_STARTED_MONOTONIC=time.monotonic()
        started_at=datetime.now(timezone.utc).isoformat()
        _update_job(running=True,stage='queue',progress=0.0,profile_id=profile_id,message=f'已开始更新 {profile["short_label"]}，正在准备来源',started_at=started_at,finished_at=None,error=False,snapshot_id=None,completed=None,total=None,source=None)
        def progress(update):
            if isinstance(update,dict):
                _update_job(running=True,**update)
        with contextlib.redirect_stdout(io.StringIO()):a=collect(pd.Timestamp(asof),refresh=True,profile_id=profile_id,progress_callback=progress)
        # The strict evaluation is deliberately run before the compatibility
        # snapshot is built.  It reads only the just-archived PIT repository
        # and writes an append-only decision, even when coverage is zero.
        _update_job(running=True,stage='snapshot',progress=97.0,message='正在生成页面快照')
        strict_result=a.get('strict_evaluation') or evaluate_strict_pit(DB,asof,market=profile_id)
        _update_job(running=True,stage='snapshot',progress=98.0,message='正在整理评分与证据展示')
        s=build(asof,a,profile_id);s['strict_pit']=strict_result
        _update_job(running=True,stage='snapshot',progress=99.0,message='正在保存最新快照')
        s=save(s);failed=sum(bool(r.get('error')) for r in s['sources'])
        _update_job(running=False,stage='complete',progress=100.0,message=f'采集完成：{profile["short_label"]} {len(s["sources"])-failed}/{len(s["sources"])} 个请求成功；行情日期 {s["quote"]["date"]}；严格 PIT {strict_result["gate_status"]}；展示覆盖 {s["coverage"]}/100',snapshot_id=s['id'],finished_at=datetime.now(timezone.utc).isoformat(),completed=len(s['sources']),total=len(s['sources']),source=None,error=False)
    except Exception as e:
        _update_job(running=False,stage='error',message='更新失败，保留已有记录：'+str(e),error=True,finished_at=datetime.now(timezone.utc).isoformat())
    finally:
        JOB_STARTED_MONOTONIC=None
        LOCK.release()

class Handler(BaseHTTPRequestHandler):
    def reply(self,status,data,ctype='application/json; charset=utf-8'):
        body=json.dumps(data,ensure_ascii=False,allow_nan=False).encode() if isinstance(data,(dict,list)) else data
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        origin=self.headers.get('Origin')
        allowed={item.strip().rstrip('/') for item in os.environ.get('ALLOWED_ORIGINS','').split(',') if item.strip()}
        if origin and (origin.rstrip('/') in allowed or origin.rstrip('/') in {f'http://{self.headers.get("Host","")}'.rstrip('/'),f'https://{self.headers.get("Host","")}'.rstrip('/')}):
            self.send_header('Access-Control-Allow-Origin',origin)
            self.send_header('Vary','Origin')
        self.end_headers();self.wfile.write(body)

    def do_OPTIONS(self):
        origin=self.headers.get('Origin','').rstrip('/')
        host=self.headers.get('Host','')
        allowed={item.strip().rstrip('/') for item in os.environ.get('ALLOWED_ORIGINS','').split(',') if item.strip()}
        same_origin=origin in {f'http://{host}'.rstrip('/'),f'https://{host}'.rstrip('/')}
        if origin and origin not in allowed and not same_origin:return self.reply(403,{'error':'来源未获允许'})
        self.send_response(204);self.send_header('Access-Control-Allow-Origin',origin or f'https://{host}');self.send_header('Access-Control-Allow-Methods','GET,POST,OPTIONS');self.send_header('Access-Control-Allow-Headers','Content-Type');self.send_header('Access-Control-Max-Age','600');self.end_headers()
    def do_GET(self):
        u=urlparse(self.path);q=parse_qs(u.query)
        if u.path=='/api/status':return self.reply(200,_job_snapshot())
        if u.path=='/api/modes':
            return self.reply(200,{'modes':list(MODES),'strict_mode':STRICT_MODE,'default':'RESEARCH_PROXY','labels':{'LEGACY':'兼容旧快照','RESEARCH_PROXY':'研究代理（可展示、不可冒充 PIT）','STRICT_PIT':'严格时点（仅合格观察）'}})
        if u.path=='/api/decision-log':
            try:profile_id=get_profile(q.get('profile',['NDX'])[0])['id'];limit=int(q.get('limit',['100'])[0])
            except (ValueError,TypeError) as e:return self.reply(400,{'error':str(e)})
            return self.reply(200,PITRepository(DB).get_decision_log(profile_id,limit=limit))
        if u.path=='/api/decision-log/verify':
            try:profile_id=get_profile(q.get('profile',['NDX'])[0])['id']
            except ValueError as e:return self.reply(400,{'error':str(e)})
            return self.reply(200,PITRepository(DB).verify_decision_chain(profile_id))
        if u.path=='/api/pit/recovery':
            try:
                PITRepository(DB)
                series_id=q.get('series',[None])[0]
                return self.reply(200,PITRepository(DB).get_recovery_audit(series_id))
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/features':
            try:
                repo=PITRepository(DB)
                eligible=None if 'eligible' not in q else q.get('eligible',['0'])[0] in ('1','true','True')
                return self.reply(200,repo.get_feature_snapshots(feature_name=q.get('feature',[None])[0],series_id=q.get('series',[None])[0],as_of=q.get('as_of',[None])[0],score_eligible=eligible))
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/fetch-attempts':
            try:return self.reply(200,PITRepository(DB).get_fetch_attempts(q.get('run_id',[None])[0],limit=int(q.get('limit',['500'])[0])))
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/archive-runs':
            try:return self.reply(200,PITRepository(DB).get_archive_runs(q.get('profile',[None])[0],limit=int(q.get('limit',['100'])[0])))
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/phase1c-status':
            try:
                if CLOUD_MODE and _bundle_value('phase1c') is not None:return self.reply(200,_bundle_value('phase1c'))
                return self.reply(200,build_report(DB))
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/phase2a-status':
            try:
                if CLOUD_MODE and _bundle_value('phase2a') is not None:return self.reply(200,_bundle_value('phase2a'))
                return self.reply(200,build_phase2a_report(DB))
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/phase2b-status':
            try:
                if CLOUD_MODE and _bundle_value('phase2b') is not None:return self.reply(200,_bundle_value('phase2b'))
                return self.reply(200,build_phase2b_report(DB))
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/phase2c-status':
            try:
                if CLOUD_MODE and _bundle_value('phase2c') is not None:return self.reply(200,_bundle_value('phase2c'))
                return self.reply(200,build_phase2c_report(DB))
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/phase2d-status':
            try:
                if CLOUD_MODE and _bundle_value('phase2d') is not None:return self.reply(200,_bundle_value('phase2d'))
                return self.reply(200,build_phase2d_report(DB))
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/phase2e-status':
            try:
                if CLOUD_MODE and _bundle_value('phase2e') is not None:return self.reply(200,_bundle_value('phase2e'))
                return self.reply(200,build_phase2e_report(DB))
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/phase2f-status':
            try:
                if CLOUD_MODE and _bundle_value('phase2f') is not None:return self.reply(200,_bundle_value('phase2f'))
                return self.reply(200,build_phase2f_report(DB))
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/capital-feasibility-runs':
            try:
                repo=PITRepository(DB)
                runs=repo.get_capital_feasibility_runs(market=q.get('market',['NDX'])[0],limit=int(q.get('limit',['100'])[0]))
                return self.reply(200,[_blind_capital_feasibility_run(item) for item in runs])
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path.startswith('/api/capital-feasibility-run/'):
            run_id=u.path.split('/api/capital-feasibility-run/',1)[1]
            if not run_id:return self.reply(400,{'error':'缺少 capital_run_id'})
            try:
                repo=PITRepository(DB);run=repo.get_capital_feasibility_run(run_id)
                if not run:return self.reply(404,{'error':'capital feasibility run 不存在'})
                payload={'run':_blind_capital_feasibility_run(run),'blind_default':True}
                include=q.get('include',[''])[0]
                if include in {'report','all'}:payload['report']=repo.get_capital_feasibility_report(run_id)
                if include in {'scenarios','all'}:payload['scenarios']=repo.get_capital_feasibility_scenarios(run_id,limit=int(q.get('limit',['100000'])[0]))
                if include in {'ledger','all'}:payload['ledger']=repo.get_capital_feasibility_ledger(run_id,limit=int(q.get('limit',['1000000'])[0]))
                return self.reply(200,payload)
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/capital-feasibility-report':
            try:
                run_id=q.get('run_id',[None])[0]
                if not run_id:
                    candidates=PITRepository(DB).get_capital_feasibility_runs(market=q.get('market',['NDX'])[0],limit=100)
                    run_id=next((r['capital_run_id'] for r in candidates if r.get('status')=='COMPLETED'),None)
                if not run_id:return self.reply(404,{'error':'没有已完成的 capital feasibility run'})
                report=PITRepository(DB).get_capital_feasibility_report(run_id)
                return self.reply(200,report) if report else self.reply(404,{'error':'capital feasibility report 不存在'})
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/capital-feasibility-outcomes':
            try:
                run_id=q.get('run_id',[None])[0]
                if not run_id:raise ValueError('需要 run_id')
                repo=PITRepository(DB)
                return self.reply(200,{
                    'run_id':run_id,
                    'scenarios':repo.get_capital_feasibility_scenarios(run_id,limit=int(q.get('limit',['100000'])[0])),
                    'ledger':repo.get_capital_feasibility_ledger(run_id,limit=int(q.get('ledger_limit',['1000000'])[0])),
                })
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/phase3a-status':
            try:
                if CLOUD_MODE and _bundle_value('phase3a') is not None:return self.reply(200,_bundle_value('phase3a'))
                return self.reply(200,build_phase3a_report(DB))
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/phase3b-status':
            try:
                if CLOUD_MODE and _bundle_value('phase3b') is not None:return self.reply(200,_bundle_value('phase3b'))
                return self.reply(200,phase3b_report_from_repository(PITRepository(DB)))
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/real-world-profiles':
            try:
                profiles=standard_profiles()
                raw=q.get('profile_json',[None])[0]
                custom=normalize_real_world_profile(json.loads(raw)) if raw else None
                return self.reply(200,{
                    'profiles':profiles,
                    'custom_profile':custom,
                    'target_specs':TARGET_SPECS,
                    'config_hash':REAL_WORLD_CONFIG_HASH,
                    'simulation_only':True,
                    'auto_trade':False,
                })
            except (ValueError,TypeError,json.JSONDecodeError) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/real-world-profile':
            try:
                raw=q.get('profile_json',[None])[0]
                profile=normalize_real_world_profile(json.loads(raw) if raw else standard_profiles()['P1'])
                return self.reply(200,{'capital_profile':profile,'target_specs':TARGET_SPECS,'waterfall':REAL_WORLD_CONFIG['waterfall'],'simulation_only':True,'auto_trade':False})
            except (ValueError,TypeError,json.JSONDecodeError) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/real-world-parameterization-runs':
            try:
                repo=PITRepository(DB)
                runs=repo.get_real_world_parameterization_runs(market=q.get('market',['NDX'])[0],limit=int(q.get('limit',['100'])[0]))
                return self.reply(200,[_blind_real_world_run(item) for item in runs])
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path.startswith('/api/real-world-parameterization-run/'):
            run_id=u.path.split('/api/real-world-parameterization-run/',1)[1]
            if not run_id:return self.reply(400,{'error':'缺少 real_world_run_id'})
            try:
                repo=PITRepository(DB);run=repo.get_real_world_parameterization_run(run_id)
                if not run:return self.reply(404,{'error':'real-world parameterization run 不存在'})
                payload={'run':_blind_real_world_run(run),'blind_default':True}
                include=q.get('include',[''])[0]
                if include in {'report','all'}:payload['report']=repo.get_real_world_parameterization_report(run_id)
                if include in {'scenarios','all'}:payload['scenarios']=repo.get_real_world_profile_scenarios(run_id,profile_id=q.get('profile_id',[None])[0],ladder_id=q.get('ladder',[None])[0],limit=int(q.get('limit',['100000'])[0]))
                if include in {'paths','all'}:payload['paths']=repo.get_real_world_path_results(run_id,path_id=q.get('path_id',[None])[0],limit=int(q.get('limit',['100000'])[0]))
                return self.reply(200,payload)
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/real-world-parameterization-report':
            try:
                repo=PITRepository(DB);run_id=q.get('run_id',[None])[0]
                if not run_id:
                    runs=repo.get_real_world_parameterization_runs(market=q.get('market',['NDX'])[0],limit=100)
                    run_id=next((item['real_world_run_id'] for item in runs if item.get('status')=='COMPLETED'),None)
                if not run_id:return self.reply(404,{'error':'没有已完成的 Phase 3B run'})
                report=repo.get_real_world_parameterization_report(run_id)
                return self.reply(200,report) if report else self.reply(404,{'error':'real-world parameterization report 不存在'})
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/real-world-preview':
            try:
                repo=PITRepository(DB)
                raw=q.get('profile_json',[None])[0]
                profile=normalize_real_world_profile(json.loads(raw) if raw else standard_profiles()['P1'])
                end_day=q.get('date',q.get('as_of',[date.today().isoformat()]))[0]
                start_day=q.get('start_date',['2000-01-01'])[0]
                phase3a_batch=q.get('phase3a_batch_id',[None])[0]
                events,resolved_batch,source_state=_phase3b_context(repo,phase3a_batch,end_day)
                result=replay_real_world_profile(events,profile,ladder_id=q.get('ladder',['C'])[0],target_id=q.get('target_id',[None])[0],cap_multiplier=float(q.get('cap_multiplier',[profile.get('cap_multiplier',1.0)])[0]),refill_mode=q.get('refill_mode',['FIXED'])[0],refill_ratio=float(q.get('refill_ratio',[0])[0]) if q.get('refill_ratio') else None,growth_scenario=q.get('growth_scenario',['G0'])[0],surplus_policy=q.get('surplus_policy',[profile.get('surplus_policy','S0')])[0],start_date=start_day,end_date=end_day,path_id='PREVIEW',path_type='PREVIEW')
                public={key:value for key,value in result.items() if key not in {'events','snapshots','monthly_snapshots','refill_history','ignored_events','final_state'}}
                public['latest_state']=result.get('final_state')
                public['source_phase3a_batch_id']=resolved_batch
                public['source_event_count']=len(events)
                public.update(_real_world_preview_metadata(source_state,result,q.get('ladder',['C'])[0]))
                return self.reply(200,public)
            except (ValueError,TypeError,KeyError,json.JSONDecodeError) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/capital-profile':
            try:
                raw=q.get('profile_json',[None])[0]
                profile=normalize_capital_profile(json.loads(raw) if raw else None)
                return self.reply(200,{
                    'capital_profile':profile,
                    'default_profile':DEFAULT_CAPITAL_PROFILE,
                    'income_linked_formula':'income × savings_ratio',
                    'simulation_only':True,
                    'auto_trade':False,
                })
            except (ValueError,TypeError,json.JSONDecodeError) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/capital-state-machine-runs':
            try:
                repo=PITRepository(DB)
                runs=repo.get_capital_state_machine_runs(market=q.get('market',['NDX'])[0],ladder_id=q.get('ladder',[None])[0],batch_id=q.get('batch_id',[None])[0],limit=int(q.get('limit',['100'])[0]))
                return self.reply(200,[{key:value for key,value in item.items() if key not in {'summary','error'}} for item in runs])
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path.startswith('/api/capital-state-machine-run/'):
            run_id=u.path.split('/api/capital-state-machine-run/',1)[1]
            if not run_id:return self.reply(400,{'error':'缺少 state_machine_run_id'})
            try:
                repo=PITRepository(DB);run=repo.get_capital_state_machine_run(run_id)
                if not run:return self.reply(404,{'error':'capital state machine run 不存在'})
                payload={'run':{key:value for key,value in run.items() if key not in {'summary','error'}},'blind_default':True}
                include=q.get('include',[''])[0]
                if include in {'report','all'}:payload['report']=_public_phase3a_report(repo.get_capital_state_machine_report(run_id))
                if include in {'events','all'}:payload['events']=repo.get_capital_events(run_id,as_of=q.get('as_of',[None])[0],limit=int(q.get('limit',['100000'])[0]))
                if include in {'snapshots','all'}:payload['snapshots']=repo.get_capital_snapshots(run_id,as_of=q.get('as_of',[None])[0],limit=int(q.get('limit',['100000'])[0]))
                return self.reply(200,payload)
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/capital-state-machine-report':
            try:
                report=build_phase3a_report(DB)
                return self.reply(200,report)
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/capital-state':
            try:
                repo=PITRepository(DB);ladder=str(q.get('ladder',['C'])[0]).upper();run_id=q.get('run_id',[None])[0]
                if not run_id:
                    report=build_phase3a_report(DB);run_id=(report.get('run_ids') or {}).get(ladder)
                if not run_id:return self.reply(404,{'error':'没有已完成的 Phase 3A 状态机 run'})
                day=q.get('date',q.get('as_of',[None]))[0]
                if day:
                    state=repo.get_capital_snapshot(run_id,day)
                else:
                    states=repo.get_capital_snapshots(run_id,limit=2000000);state=states[-1] if states else None
                if not state:return self.reply(404,{'error':'没有对应日期的 capital state snapshot'})
                return self.reply(200,{'run_id':run_id,'ladder':ladder,'state':state,'simulation_only':True,'auto_trade':False})
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/capital-snapshot':
            try:
                repo=PITRepository(DB);run_id=q.get('run_id',[None])[0];day=q.get('date',q.get('as_of',[None]))[0]
                if not run_id or not day:raise ValueError('需要 run_id 与 date=YYYY-MM-DD')
                state=repo.get_capital_snapshot(run_id,day)
                return self.reply(200,state) if state else self.reply(404,{'error':'capital snapshot 不存在'})
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/capital-events':
            try:
                repo=PITRepository(DB);run_id=q.get('run_id',[None])[0]
                if not run_id:
                    report=build_phase3a_report(DB);run_id=(report.get('run_ids') or {}).get(str(q.get('ladder',['C'])[0]).upper())
                if not run_id:return self.reply(404,{'error':'没有已完成的 Phase 3A 状态机 run'})
                return self.reply(200,{'run_id':run_id,'events':repo.get_capital_events(run_id,as_of=q.get('as_of',[None])[0],limit=int(q.get('limit',['100000'])[0]))})
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/capital-preview':
            try:
                repo=PITRepository(DB);run_id=q.get('run_id',[None])[0];day=q.get('date',q.get('as_of',[None]))[0];state=None
                if run_id and day:state=repo.get_capital_snapshot(run_id,day)
                if state is None and run_id:
                    states=repo.get_capital_snapshots(run_id,as_of=day,limit=2000000);state=states[-1] if states else None
                if state is None:
                    state={'tactical_cycle_id':q.get('cycle_id',[None])[0],'tactical_drawdown':float(q.get('tactical_drawdown',['0'])[0]),'used_bands':json.loads(q.get('used_bands',['[]'])[0]),'available_opportunity_cash':float(q.get('available_cash',['0'])[0]),'target_opportunity_cash':float(q.get('target_fund',['100'])[0]),'tactical_peak_price':float(q.get('tactical_peak_price',['0'])[0])}
                preview=next_trigger_preview(state,current_price=float(q['current_price'][0]) if q.get('current_price') else None,tactical_peak_price=float(q['tactical_peak_price'][0]) if q.get('tactical_peak_price') else None)
                return self.reply(200,{'run_id':run_id,'as_of':day,'state':state,'preview':preview,'simulation_only':True,'auto_trade':False})
            except (ValueError,TypeError,KeyError,json.JSONDecodeError) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/tactical-drawdown-runs':
            try:
                repo=PITRepository(DB)
                runs=repo.get_tactical_drawdown_runs(market=q.get('market',['NDX'])[0],limit=int(q.get('limit',['100'])[0]))
                return self.reply(200,[_blind_tactical_run(item) for item in runs])
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path.startswith('/api/tactical-drawdown-run/'):
            run_id=u.path.split('/api/tactical-drawdown-run/',1)[1]
            if not run_id:return self.reply(400,{'error':'缺少 tactical_run_id'})
            try:
                repo=PITRepository(DB);run=repo.get_tactical_drawdown_run(run_id)
                if not run:return self.reply(404,{'error':'tactical drawdown run 不存在'})
                payload={'run':_blind_tactical_run(run),'blind_default':True}
                include=q.get('include',[''])[0]
                if include in {'report','all'}:payload['report']=repo.get_tactical_drawdown_report(run_id)
                if include in {'states','all'}:payload['states']=repo.get_tactical_drawdown_states(run_id,limit=int(q.get('limit',['200000'])[0]))
                if include in {'cycles','all'}:payload['cycles']=repo.get_tactical_drawdown_cycles(run_id,limit=int(q.get('limit',['50000'])[0]))
                if include in {'events','all'}:payload['events']=repo.get_tactical_drawdown_events(run_id,limit=int(q.get('limit',['100000'])[0]))
                if include in {'outcomes','all'}:payload['evaluations']=repo.get_tactical_drawdown_evaluations(run_id,limit=int(q.get('limit',['100000'])[0]))
                if include in {'conditional','all'}:payload['conditional_results']=repo.get_tactical_overlay_conditional_results(run_id,limit=int(q.get('limit',['10000'])[0]))
                if include in {'models','all'}:payload['model_results']=repo.get_tactical_overlay_model_results(run_id,limit=int(q.get('limit',['50000'])[0]))
                if include in {'loeo','all'}:payload['loeo_results']=repo.get_tactical_overlay_lome_results(run_id,limit=int(q.get('limit',['500000'])[0]))
                return self.reply(200,payload)
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path in {'/api/tactical-drawdown-date','/api/tactical-drawdown-state'}:
            try:
                repo=PITRepository(DB)
                run_id=q.get('run_id',[None])[0]
                day=q.get('date',q.get('as_of',[None]))[0]
                if not day:raise ValueError('需要 date=YYYY-MM-DD')
                if not run_id:
                    candidates=repo.get_tactical_drawdown_runs(market=q.get('market',['NDX'])[0],limit=100)
                    run_id=next((r['tactical_run_id'] for r in candidates if r['status']=='COMPLETED'),None)
                if not run_id:return self.reply(404,{'error':'没有已完成的 tactical drawdown run'})
                view=blind_tactical_event_date_view(repo,run_id,day)
                return self.reply(200,view) if view else self.reply(404,{'error':'当天不在 tactical drawdown 事件或状态范围'})
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/tactical-drawdown-report':
            try:
                run_id=q.get('run_id',[None])[0]
                if not run_id:raise ValueError('需要 run_id')
                report=PITRepository(DB).get_tactical_drawdown_report(run_id)
                return self.reply(200,report) if report else self.reply(404,{'error':'tactical drawdown report 不存在'})
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/tactical-drawdown-outcomes':
            try:
                run_id=q.get('run_id',[None])[0]
                if not run_id:raise ValueError('需要 run_id')
                repo=PITRepository(DB)
                return self.reply(200,{
                    'run_id':run_id,
                    'cycles':repo.get_tactical_drawdown_cycles(run_id,limit=int(q.get('limit',['50000'])[0])),
                    'events':repo.get_tactical_drawdown_events(run_id,limit=int(q.get('limit',['100000'])[0])),
                    'evaluations':repo.get_tactical_drawdown_evaluations(run_id,limit=int(q.get('limit',['100000'])[0])),
                    'conditional_results':repo.get_tactical_overlay_conditional_results(run_id,limit=int(q.get('limit',['10000'])[0])),
                    'model_results':repo.get_tactical_overlay_model_results(run_id,limit=int(q.get('limit',['50000'])[0])),
                    'loeo_results':repo.get_tactical_overlay_lome_results(run_id,limit=int(q.get('limit',['500000'])[0])),
                })
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/drawdown-overlay-runs':
            try:
                repo=PITRepository(DB)
                runs=repo.get_drawdown_overlay_runs(market=q.get('market',[None])[0],limit=int(q.get('limit',['100'])[0]))
                return self.reply(200,[blind_drawdown_overlay_run(item) for item in runs])
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path.startswith('/api/drawdown-overlay-run/'):
            run_id=u.path.split('/api/drawdown-overlay-run/',1)[1]
            if not run_id:return self.reply(400,{'error':'缺少 overlay_run_id'})
            try:
                repo=PITRepository(DB);run=repo.get_drawdown_overlay_run(run_id)
                if not run:return self.reply(404,{'error':'drawdown overlay run 不存在'})
                payload={'run':blind_drawdown_overlay_run(run),'blind_default':True}
                include=q.get('include',[''])[0]
                if include in {'report','all'}:payload['report']=repo.get_drawdown_overlay_report(run_id)
                if include in {'events','all'}:payload['events']=repo.get_drawdown_overlay_events(run_id,limit=int(q.get('limit',['100000'])[0]))
                if include in {'outcomes','all'}:payload['evaluations']=repo.get_drawdown_overlay_evaluations(run_id,limit=int(q.get('limit',['100000'])[0]))
                if include in {'conditional','all'}:payload['conditional_results']=repo.get_drawdown_overlay_conditional_results(run_id,limit=int(q.get('limit',['10000'])[0]))
                if include in {'models','all'}:payload['model_results']=repo.get_drawdown_overlay_model_results(run_id,limit=int(q.get('limit',['50000'])[0]))
                if include in {'loeo','all'}:payload['loeo_results']=repo.get_drawdown_overlay_loeo_results(run_id,limit=int(q.get('limit',['500000'])[0]))
                return self.reply(200,payload)
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path in {'/api/drawdown-overlay-date','/api/drawdown-overlay-state'}:
            try:
                repo=PITRepository(DB)
                run_id=q.get('run_id',[None])[0]
                day=q.get('date',q.get('as_of',[None]))[0]
                if not day:raise ValueError('需要 date=YYYY-MM-DD')
                if not run_id:
                    candidates=repo.get_drawdown_overlay_runs(market=q.get('market',['NDX'])[0],limit=100)
                    run_id=next((r['overlay_run_id'] for r in candidates if r['status']=='COMPLETED'),None)
                if not run_id:return self.reply(404,{'error':'没有已完成的 drawdown overlay run'})
                view=blind_overlay_event_date_view(repo,run_id,day)
                return self.reply(200,view) if view else self.reply(404,{'error':'当天不在 drawdown overlay 事件范围'})
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/drawdown-overlay-report':
            try:
                run_id=q.get('run_id',[None])[0]
                if not run_id:raise ValueError('需要 run_id')
                report=PITRepository(DB).get_drawdown_overlay_report(run_id)
                return self.reply(200,report) if report else self.reply(404,{'error':'drawdown overlay report 不存在'})
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/drawdown-overlay-outcomes':
            try:
                run_id=q.get('run_id',[None])[0]
                if not run_id:raise ValueError('需要 run_id')
                repo=PITRepository(DB)
                return self.reply(200,{
                    'run_id':run_id,
                    'events':repo.get_drawdown_overlay_events(run_id,limit=int(q.get('limit',['100000'])[0])),
                    'evaluations':repo.get_drawdown_overlay_evaluations(run_id,limit=int(q.get('limit',['100000'])[0])),
                    'conditional_results':repo.get_drawdown_overlay_conditional_results(run_id,limit=int(q.get('limit',['10000'])[0])),
                    'model_results':repo.get_drawdown_overlay_model_results(run_id,limit=int(q.get('limit',['50000'])[0])),
                })
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/episode-validation-runs':
            try:
                repo=PITRepository(DB)
                runs=repo.get_episode_validation_runs(market=q.get('market',[None])[0],limit=int(q.get('limit',['100'])[0]))
                return self.reply(200,[blind_episode_validation_run(item) for item in runs])
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path.startswith('/api/episode-validation-run/'):
            run_id=u.path.split('/api/episode-validation-run/',1)[1]
            if not run_id:return self.reply(400,{'error':'缺少 episode_run_id'})
            try:
                repo=PITRepository(DB);run=repo.get_episode_validation_run(run_id)
                if not run:return self.reply(404,{'error':'episode validation run 不存在'})
                payload={'run':blind_episode_validation_run(run),'blind_default':True}
                include=q.get('include',[''])[0]
                if include in {'report','all'}:payload['report']=repo.get_episode_validation_report(run_id)
                if include in {'episodes','all'}:payload['episodes']=repo.get_drawdown_episodes(run_id,limit=int(q.get('limit',['100000'])[0]))
                if include in {'states','all'}:payload['states']=repo.get_episode_daily_states(run_id,limit=int(q.get('limit',['100000'])[0]))
                if include in {'events','all'}:
                    payload['opportunity_events']=repo.get_proxy_opportunity_events(run_id,limit=int(q.get('limit',['100000'])[0]))
                    payload['mechanical_events']=repo.get_drawdown_mechanical_events(run_id,limit=int(q.get('limit',['100000'])[0]))
                if include in {'outcomes','all'}:
                    payload['evaluations']=repo.get_episode_event_evaluations(run_id,limit=int(q.get('limit',['100000'])[0]))
                    payload['at10']=repo.get_episode_at10_assessments(run_id,limit=int(q.get('limit',['100000'])[0]))
                return self.reply(200,payload)
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path in {'/api/episode-validation-date','/api/episode-validation-state'}:
            try:
                repo=PITRepository(DB)
                run_id=q.get('run_id',[None])[0]
                day=q.get('date',q.get('as_of',[None]))[0]
                if not day:raise ValueError('需要 date=YYYY-MM-DD')
                if not run_id:
                    candidates=repo.get_episode_validation_runs(market=q.get('market',['NDX'])[0],limit=100)
                    run_id=next((r['episode_run_id'] for r in candidates if r['status']=='COMPLETED'),None)
                if not run_id:return self.reply(404,{'error':'没有已完成的 episode validation run'})
                view=blind_episode_date_view(repo,run_id,day)
                return self.reply(200,view) if view else self.reply(404,{'error':'当天不在 episode validation 范围'})
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/episode-validation-report':
            try:
                run_id=q.get('run_id',[None])[0]
                if not run_id:raise ValueError('需要 run_id')
                report=PITRepository(DB).get_episode_validation_report(run_id)
                return self.reply(200,report) if report else self.reply(404,{'error':'episode validation report 不存在'})
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/episode-validation-outcomes':
            try:
                run_id=q.get('run_id',[None])[0]
                if not run_id:raise ValueError('需要 run_id')
                repo=PITRepository(DB)
                return self.reply(200,{
                    'run_id':run_id,
                    'episodes':repo.get_drawdown_episodes(run_id,limit=int(q.get('limit',['100000'])[0])),
                    'evaluations':repo.get_episode_event_evaluations(run_id,limit=int(q.get('limit',['100000'])[0])),
                    'at10':repo.get_episode_at10_assessments(run_id,limit=int(q.get('limit',['100000'])[0])),
                })
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/proxy-research-runs':
            try:
                repo=PITRepository(DB)
                runs=repo.get_proxy_research_runs(market=q.get('market',[None])[0],limit=int(q.get('limit',['100'])[0]))
                return self.reply(200,[blind_proxy_research_run(item) for item in runs])
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path.startswith('/api/proxy-research-run/'):
            run_id=u.path.split('/api/proxy-research-run/',1)[1]
            if not run_id:return self.reply(400,{'error':'缺少 proxy_run_id'})
            try:
                repo=PITRepository(DB);run=repo.get_proxy_research_run(run_id)
                if not run:return self.reply(404,{'error':'proxy research run 不存在'})
                payload={'run':blind_proxy_research_run(run),'blind_default':True}
                if q.get('include',[''])[0] in {'report','all'}:payload['report']=repo.get_proxy_research_report(run_id)
                if q.get('include',[''])[0]=='signals':payload['signals']=repo.get_proxy_signals(run_id,limit=int(q.get('limit',['10000'])[0]))
                return self.reply(200,payload)
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path in {'/api/proxy-research-date','/api/proxy-research-signal'}:
            try:
                repo=PITRepository(DB)
                run_id=q.get('run_id',[None])[0]
                day=q.get('date',q.get('as_of',[None]))[0]
                if not day:raise ValueError('需要 date=YYYY-MM-DD')
                if not run_id:
                    candidates=repo.get_proxy_research_runs(market=q.get('market',['NDX'])[0],limit=100)
                    run_id=next((r['proxy_run_id'] for r in candidates if r['status']=='COMPLETED'),None)
                if not run_id:return self.reply(404,{'error':'没有已完成的 proxy research run'})
                view=blind_proxy_date_view(repo,run_id,day)
                return self.reply(200,view) if view else self.reply(404,{'error':'当天没有已保存的 blind proxy signal'})
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/proxy-research-report':
            try:
                run_id=q.get('run_id',[None])[0]
                if not run_id:raise ValueError('需要 run_id')
                report=PITRepository(DB).get_proxy_research_report(run_id)
                return self.reply(200,report) if report else self.reply(404,{'error':'proxy research report 不存在'})
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/proxy-research-outcomes':
            try:
                run_id=q.get('run_id',[None])[0]
                if not run_id:raise ValueError('需要 run_id')
                return self.reply(200,PITRepository(DB).get_proxy_forward_outcomes(run_id,limit=int(q.get('limit',['100000'])[0])))
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/replay-runs':
            try:
                repo=PITRepository(DB)
                runs=repo.get_replay_runs(market=q.get('market',[None])[0],mode=q.get('mode',[None])[0],limit=int(q.get('limit',['100'])[0]))
                return self.reply(200,[blind_replay_run(item) for item in runs])
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path.startswith('/api/replay-run/'):
            run_id=u.path.split('/api/replay-run/',1)[1]
            if not run_id:return self.reply(400,{'error':'缺少 replay_run_id'})
            try:
                repo=PITRepository(DB);run=repo.get_replay_run(run_id)
                if not run:return self.reply(404,{'error':'replay_run 不存在'})
                payload={'run':blind_replay_run(run),'blind_default':True}
                if q.get('include',[''])[0] in {'report','all'}:
                    payload['report']=corrected_replay_report(repo.get_replay_report(run_id))
                if q.get('include',[''])[0]=='decisions':
                    payload['decisions']=repo.get_replay_decisions(run_id,limit=int(q.get('limit',['10000'])[0]))
                return self.reply(200,payload)
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path in {'/api/replay-date','/api/replay-decision'}:
            try:
                repo=PITRepository(DB)
                run_id=q.get('run_id',[None])[0]
                day=q.get('date',q.get('as_of',[None]))[0]
                if not day:raise ValueError('需要 date=YYYY-MM-DD')
                if not run_id:
                    mode=q.get('mode',['RESEARCH_PROXY'])[0].upper();market=q.get('market',['NDX'])[0].upper()
                    candidates=repo.get_replay_runs(market=market,mode=mode,limit=100)
                    run_id=next((r['replay_run_id'] for r in candidates if r['status']=='COMPLETED'),None)
                if not run_id:return self.reply(404,{'error':'没有已完成的 replay run'})
                view=replay_date_view(repo,run_id,day)
                return self.reply(200,view) if view else self.reply(404,{'error':'当天没有已保存的 blind decision'})
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/replay-report':
            try:
                run_id=q.get('run_id',[None])[0]
                if not run_id:raise ValueError('需要 run_id')
                report=corrected_replay_report(PITRepository(DB).get_replay_report(run_id))
                return self.reply(200,report) if report else self.reply(404,{'error':'replay report 不存在'})
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/replay-outcomes':
            try:
                run_id=q.get('run_id',[None])[0]
                if not run_id:raise ValueError('需要 run_id')
                return self.reply(200,PITRepository(DB).get_forward_outcomes(run_id,limit=int(q.get('limit',['100000'])[0])))
            except (ValueError,sqlite3.Error) as e:return self.reply(400,{'error':str(e)})
        if u.path=='/api/profiles':
            return self.reply(200,profile_list())
        if u.path=='/api/history':
            try:profile_id=get_profile(q.get('profile',['NDX'])[0])['id']
            except ValueError as e:return self.reply(400,{'error':str(e)})
            r=[]
            with db() as c:
                for x in c.execute('SELECT id,asof,payload FROM snapshots ORDER BY asof DESC,id DESC LIMIT 300'):
                    try:payload=json.loads(x[2]);pid=payload.get('profile',{}).get('id',payload.get('profile_id','NDX'))
                    except (TypeError,json.JSONDecodeError):continue
                    if pid==profile_id:r.append(dict(id=x[0],asof=x[1],profile_id=pid))
                    if len(r)>=100:break
            if not r and CLOUD_MODE:
                r=[dict(item,profile_id=profile_id) for item in (_bundle_value('history',{}).get(profile_id,[]) if isinstance(_bundle_value('history',{}),dict) else [])]
            return self.reply(200,r)
        if u.path=='/api/snapshot':
            try:profile_id=get_profile(q.get('profile',['NDX'])[0])['id']
            except ValueError as e:return self.reply(400,{'error':str(e)})
            with db() as c:
                if 'id' in q:r=c.execute('SELECT payload FROM snapshots WHERE id=?',(q['id'][0],)).fetchone()
                else:
                    r=None
                    for candidate in c.execute('SELECT payload FROM snapshots WHERE asof<=? ORDER BY asof DESC,id DESC LIMIT 300',(date.today().isoformat(),)):
                        try:payload=json.loads(candidate[0]);pid=payload.get('profile',{}).get('id',payload.get('profile_id','NDX'))
                        except (TypeError,json.JSONDecodeError):continue
                        if pid==profile_id:r=candidate;break
            if r:return self.reply(200,json.loads(r[0]))
            if CLOUD_MODE:
                bundle=_bundle_value('snapshots',{})
                by_profile=bundle.get(profile_id,{}) if isinstance(bundle,dict) else {}
                snapshot_id=q.get('id',[None])[0]
                payload=by_profile.get(snapshot_id) if snapshot_id else by_profile.get(_bundle_value('latest',{}).get(profile_id))
                if payload is None and by_profile:payload=next(iter(by_profile.values()))
                if payload is not None:return self.reply(200,payload)
            return self.reply(404,{'error':f'{profile_id} 还没有快照，请点击联网更新'})
        files={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/evidence-template.json':'evidence-template.json','/evidence-template.csv':'evidence-template.csv'}
        if u.path in files:
            p=ROOT/files[u.path];return self.reply(200,p.read_bytes(),'text/html; charset=utf-8' if p.suffix=='.html' else 'text/javascript; charset=utf-8' if p.suffix=='.js' else 'text/csv; charset=utf-8' if p.suffix=='.csv' else 'application/json')
        self.reply(404,{'error':'不存在'})
    def do_POST(self):
        origin=self.headers.get('Origin');host=self.headers.get('Host','')
        allowed={item.strip().rstrip('/') for item in os.environ.get('ALLOWED_ORIGINS','').split(',') if item.strip()}
        same_origin=not origin or origin.rstrip('/') in {f'http://{host}'.rstrip('/'),f'https://{host}'.rstrip('/')}
        if not same_origin and origin.rstrip('/') not in allowed:return self.reply(403,{'error':'来源未获允许'})
        try:
            n=int(self.headers.get('Content-Length','0'))
            if not 0<n<=10000000:raise ValueError('请求大小应为 1–10MB')
            p=json.loads(self.rfile.read(n))
            if not isinstance(p,dict):raise ValueError('请求必须为 JSON 对象')
            # Phase 3B accepts a CapitalProfile object and therefore cannot
            # pass through the legacy market-profile resolver.
            phase3b_request=self.path.startswith('/api/phase3b') or self.path.startswith('/api/real-world')
            profile_id='NDX' if phase3b_request else get_profile(p.get('profile','NDX'))['id']
            asof=p.get('asof',date.today().isoformat());d=datecheck(asof)
            if d>date.today() or d<date(1986,1,1):raise ValueError('日期必须在 1986-01-01 至今天之间')
            if self.path=='/api/replay-run':
                mode=str(p.get('mode','RESEARCH_PROXY')).upper()
                if mode not in REPLAY_MODES:raise ValueError('replay mode 只能是 STRICT_PIT 或 RESEARCH_PROXY')
                result=run_replay(DB,mode=mode,market=profile_id,start_date=p.get('start_date'),end_date=p.get('end_date'),replay_run_id=p.get('replay_run_id'))
                return self.reply(200,result)
            if self.path=='/api/proxy-research-run':
                result=run_proxy_research(str(DB),market=profile_id,start_date=p.get('start_date') or '2000-01-01',end_date=p.get('end_date'),proxy_run_id=p.get('proxy_run_id'))
                return self.reply(200,result)
            if self.path=='/api/episode-validation-run':
                result=run_episode_validation(
                    str(DB), market=profile_id, proxy_run_id=p.get('proxy_run_id'),
                    start_date=p.get('start_date'), end_date=p.get('end_date'),
                    episode_run_id=p.get('episode_run_id'),
                )
                return self.reply(200,result)
            if self.path=='/api/drawdown-overlay-run':
                result=run_overlay_validation(
                    PITRepository(DB), phase2c_run_id=p.get('phase2c_run_id'),
                    start_date=p.get('start_date'), end_date=p.get('end_date'),
                    overlay_run_id=p.get('overlay_run_id'),
                )
                return self.reply(200,result)
            if self.path=='/api/tactical-drawdown-run':
                result=run_tactical_validation(
                    PITRepository(DB), phase2c_run_id=p.get('phase2c_run_id'),
                    start_date=p.get('start_date'), end_date=p.get('end_date'),
                    tactical_run_id=p.get('tactical_run_id'),
                )
                return self.reply(200,result)
            if self.path=='/api/capital-feasibility-run':
                result=run_capital_feasibility_validation(
                    PITRepository(DB), phase2e_run_id=p.get('phase2e_run_id'),
                    start_date=p.get('start_date'), end_date=p.get('end_date'),
                    capital_run_id=p.get('capital_run_id'),
                )
                return self.reply(200,result)
            if self.path=='/api/capital-state-machine-run':
                ladders=p.get('ladders') or ([p.get('ladder_id')] if p.get('ladder_id') else ['C','D'])
                result=run_phase3a_validation(
                    PITRepository(DB), phase2e_run_id=p.get('phase2e_run_id'), phase2f_run_id=p.get('phase2f_run_id'),
                    start_date=p.get('start_date'), end_date=p.get('end_date'), batch_id=p.get('batch_id') or p.get('run_id'),
                    capital_profile=p.get('capital_profile'), ladders=ladders,
                )
                return self.reply(200,result)
            if self.path=='/api/phase3b-run':
                raw_profiles=p.get('profiles')
                profiles=None
                if raw_profiles is not None:
                    if not isinstance(raw_profiles,dict):raise ValueError('profiles 必须是 profile_id 到 CapitalProfile 的对象映射')
                    profiles={str(key):normalize_real_world_profile(value) for key,value in raw_profiles.items()}
                result=run_phase3b_validation(
                    PITRepository(DB),
                    phase3a_batch_id=p.get('phase3a_batch_id') or p.get('phase3a_batch'),
                    start_date=p.get('start_date') or '2000-01-01',
                    end_date=p.get('end_date') or date.today().isoformat(),
                    batch_id=p.get('batch_id') or p.get('run_id'),
                    profiles=profiles,
                )
                return self.reply(200,result)
            if self.path=='/api/real-world-profile':
                raw=p.get('capital_profile') or p.get('profile_data') or p
                return self.reply(200,{'capital_profile':normalize_real_world_profile(raw),'target_specs':TARGET_SPECS,'simulation_only':True,'auto_trade':False})
            if self.path=='/api/real-world-preview':
                repo=PITRepository(DB)
                raw=p.get('capital_profile') or p.get('profile_data') or p
                profile=normalize_real_world_profile(raw)
                start_day=p.get('start_date') or '2000-01-01';end_day=p.get('end_date') or p.get('asof') or date.today().isoformat()
                events,resolved_batch,source_state=_phase3b_context(repo,p.get('phase3a_batch_id'),end_day)
                result=replay_real_world_profile(events,profile,ladder_id=p.get('ladder_id','C'),target_id=p.get('target_id'),cap_multiplier=p.get('cap_multiplier'),refill_mode=p.get('refill_mode','FIXED'),refill_ratio=p.get('refill_ratio'),growth_scenario=p.get('growth_scenario','G0'),surplus_policy=p.get('surplus_policy'),start_date=start_day,end_date=end_day,path_id='PREVIEW',path_type='PREVIEW')
                public={key:value for key,value in result.items() if key not in {'events','snapshots','monthly_snapshots','refill_history','ignored_events','final_state'}}
                public['latest_state']=result.get('final_state');public['source_phase3a_batch_id']=resolved_batch;public['source_event_count']=len(events)
                public.update(_real_world_preview_metadata(source_state,result,p.get('ladder_id','C')))
                return self.reply(200,public)
            if self.path=='/api/capital-profile':
                return self.reply(200,{'capital_profile':normalize_capital_profile(p.get('capital_profile') or p), 'simulation_only':True, 'auto_trade':False})
            if self.path=='/api/refresh':
                if not LOCK.acquire(False):return self.reply(409,{'error':'更新已在进行'})
                threading.Thread(target=refresh,args=(asof,profile_id),daemon=True).start();return self.reply(202,{'message':f'{profile_id} 更新已开始'})
            if self.path=='/api/strict-evaluate':
                if p.get('mode') not in (None,STRICT_MODE):raise ValueError('该端点只接受 STRICT_PIT 模式')
                result=evaluate_strict_pit(DB,asof,market=profile_id)
                return self.reply(200,result)
            if self.path=='/api/import':
                if profile_id!='NDX':raise ValueError('原始 PE/EPS/宽度导入目前只支持 NDX；其他标的的基本面仍显示为未知')
                rows=parse_csv(p.get('csv')) if 'csv' in p else validate(p.get('records'))
                with db() as c:
                    existing=[json.loads(x[0]) for x in c.execute('SELECT payload FROM evidence')];validate(existing+rows)
                    for r in rows:
                        s=json.dumps(r,sort_keys=True);c.execute('INSERT OR IGNORE INTO evidence VALUES(?,?)',(hashlib.sha256(s.encode()).hexdigest(),s))
                # Recalculate with existing source audit without network or invented fields.
                audits=list(COLLECT_ROOT.glob(asof+'_source_audit.json'))
                result=save(build(asof,json.loads(audits[0].read_text()),profile_id)) if audits else None
                return self.reply(200,{'imported':len(rows),'snapshot_id':result['id'] if result else None,'message':'导入完成；按可用日期计算，历史不足或过期仍不计分'})
            self.reply(404,{'error':'不存在'})
        except (ValueError,KeyError,TypeError) as e:self.reply(400,{'error':str(e)})
        except Exception as e:self.reply(500,{'error':str(e)})

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=int(os.environ.get('PORT','8766')));parser.add_argument('--host',default=os.environ.get('HOST','0.0.0.0' if os.environ.get('PORT') else '127.0.0.1'));a=parser.parse_args();db().close()
    print(f'仪表盘 http://{a.host}:{a.port}',flush=True);ThreadingHTTPServer((a.host,a.port),Handler).serve_forever()

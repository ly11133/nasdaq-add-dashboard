"""Public-source acquisition with vintage validation and append-only raw snapshots.
Usage: python outputs/dashboard_data/collect.py --as-of 2026-07-29
No account credentials, trades, subscriptions, or scheduled tasks are created.
"""
from pathlib import Path
from datetime import datetime,timezone
from concurrent.futures import ThreadPoolExecutor,as_completed
import argparse,hashlib,io,json,shutil,subprocess,tempfile
import requests
from bs4 import BeautifulSoup
import pandas as pd
import sys

ROOT=Path(__file__).resolve().parent
BASE='https://siblisresearch.supabase.co/functions/v1/free-data-api/v1/'
APP_ROOT=ROOT.parent/'nasdaq-add-dashboard'
if not APP_ROOT.exists():
    # In the cloud bundle the collector lives under the application root.
    APP_ROOT=ROOT.parent
sys.path.insert(0,str(APP_ROOT))
from profiles import get_profile, PROFILE_ORDER
from pit_repository import PITRepository
from strict_pit import evaluate_strict_pit
from feature_store import generate_feature_snapshots

def history_stats(points,asof):
    s=pd.Series({r['date']:float(r['value']) for r in points},dtype=float)
    s.index=pd.to_datetime(s.index);s=s.sort_index();s=s[s.index<=asof]
    if s.empty:return {'status':'no_observation_before_cutoff'}
    start=asof.replace(day=1)-pd.DateOffset(months=120)
    h=s[(s.index>=start)&(s.index<asof.replace(day=1))].resample('ME').last().dropna()
    return {'date':str(s.index[-1].date()),'value':float(s.iloc[-1]),'age_days':int((asof-s.index[-1]).days),'history_months':len(h),'percentile_candidate':float((h<=s.iloc[-1]).mean()) if len(h)>=60 else None}

def collect(asof,refresh=False,profile_id='NDX',progress_callback=None):
    profile=get_profile(profile_id);profile_id=profile['id']
    def report_progress(stage,progress,message,completed=None,total=None,source=None):
        if not callable(progress_callback):return
        payload={
            'stage':str(stage),
            'progress':max(0.0,min(100.0,float(progress))),
            'message':str(message),
            # Explicit nulls prevent the previous phase's source/count from
            # leaking into a new phase in the long-polling status response.
            'completed':int(completed) if completed is not None else None,
            'total':int(total) if total is not None else None,
            'source':str(source) if source is not None else None,
        }
        try:progress_callback(payload)
        except Exception:
            # Progress reporting is observational; a stale browser or a
            # callback failure must never abort an evidence collection.
            pass
    now=datetime.now(timezone.utc);run=ROOT/'snapshots'/now.strftime('%Y%m%dT%H%M%S%fZ');run.mkdir(parents=True)
    date=str(asof.date());start=str((asof.replace(day=1)-pd.DateOffset(months=120)).date())
    urls={}
    if profile['price_source_type']=='fred':
        urls['fred_'+profile['price_series']]=f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={profile['price_series']}"
    elif profile['price_source_type']=='yahoo':
        period2=int(now.timestamp())
        urls['yahoo_price']=f"https://query1.finance.yahoo.com/v8/finance/chart/{profile['price_symbol']}?period1=0&period2={period2}&interval=1d&events=div%2Csplits"
    else:
        urls['eastmoney_price']='https://push2his.eastmoney.com/api/qt/stock/kline/get?secid='+profile['price_symbol']+'&klt=101&fqt=0&lmt=100000&end=20500101&fields1=f1%2Cf2%2Cf3%2Cf4%2Cf5%2Cf6&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55%2Cf56%2Cf57%2Cf58%2Cf59%2Cf60%2Cf61'
    if profile_id=='NDX':
        urls.update({'hom_breadth':'https://historyofmarket.com/api/ndx/breadth.json','hom_pe':'https://historyofmarket.com/api/ndx/forward-pe.json','hom_decomposition':'https://historyofmarket.com/api/ndx/driver-decomp.json','siblis_eps_page':'https://siblisresearch.com/data/nasdaq-100-pe-ratio/','siblis_indices':BASE+'indices','siblis_dates':BASE+'dates','siblis_forward':BASE+'NDX/pe-forward','siblis_trailing':BASE+'NDX/pe-trailing'})
    if profile.get('csindex_code'):
        urls['csindex_perf']='https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode='+profile['csindex_code']+'&startDate='+start.replace('-','')+'&endDate='+date.replace('-','')
        urls['csindex_cons']='https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/'+profile['csindex_code']+'cons.xls'
        forecast_base='https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_WEB_RESPREDICT&columns=WEB_RESPREDICT&quoteColumns=f2%7E01%7ESECURITY_CODE%7ENEW_PRICE%2Cf20%7E01%7ESECURITY_CODE%7ETOTAL_MARKET_CAP&pageSize=500&sortTypes=-1&sortColumns=RATING_ORG_NUM&pageNumber='
        for page in range(1,9):urls['eastmoney_forecast_'+str(page)]=forecast_base+str(page)
    macro_series=[profile.get('volatility_series','VXNCLS'),'DFII10','NFCI']
    if profile_id=='NDX':macro_series.append('VIXCLS')
    for s in dict.fromkeys(macro_series):
        if s=='NFCI':urls['alfred_'+s]=f'https://alfred.stlouisfed.org/graph/alfredgraph.csv?id={s}&vintage_date={date}'
        elif s=='NASDAQ100':urls['fred_'+s]=f'https://fred.stlouisfed.org/graph/fredgraph.csv?id=NASDAQ100'
        else:urls['fred_'+s]=f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={s}'
    responses={};manifest=[]
    report_progress('acquire',4,f'准备联网采集 {len(urls)} 个来源',0,len(urls))
    def fetch(key,url):
        attempted_at=datetime.now(timezone.utc).isoformat()
        meta={'source':key,'url':url,'attempted_at_utc':attempted_at,'retrieved_at_utc':attempted_at,'retry_count':0}
        try:
            cache=ROOT/'cache'/ (hashlib.sha256(url.encode()).hexdigest()+'.json')
            if cache.exists() and not refresh:
                saved=json.loads(cache.read_text());raw=bytes.fromhex(saved['raw_hex'])
                assert hashlib.sha256(raw).hexdigest()==saved['sha256']
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(saved['retrieved_at_utc'])).total_seconds()
                if age<86400:
                    (run/(key+'.raw')).write_bytes(raw);meta.update(status='CACHE_HIT',http_status=200,cache_hit=True,retrieved_at_utc=saved['retrieved_at_utc'],sha256=saved['sha256'],bytes=len(raw));return key,raw,meta
            last=None
            for attempt in range(2):
                try:
                    r=requests.get(url,timeout=(8,20),headers={} if key.startswith(('fred_','alfred_')) else {'User-Agent':'Mozilla/5.0'});r.raise_for_status()
                    meta['retry_count']=attempt
                    break
                except requests.RequestException as e:
                    last=e
                    meta['retry_count']=attempt
                    if attempt==1:raise
            raw=r.content
            cache.parent.mkdir(exist_ok=True)
            cache.write_text(json.dumps({'url':url,'retrieved_at_utc':meta['retrieved_at_utc'],'sha256':hashlib.sha256(raw).hexdigest(),'raw_hex':raw.hex()}))
            (run/(key+'.raw')).write_bytes(raw)
            meta.update(status='SUCCESS',http_status=r.status_code,sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))
            r.raise_for_status();return key,raw,meta
        except Exception as e:
            meta.update(status='ERROR',error=str(e))
            if cache.exists():
                try:
                    saved=json.loads(cache.read_text());raw=bytes.fromhex(saved['raw_hex'])
                    if hashlib.sha256(raw).hexdigest()==saved['sha256']:
                        (run/(key+'.raw')).write_bytes(raw);meta['fallback_warning']=meta.get('error');meta.update(status='CACHE_FALLBACK',cache_fallback=True,retrieved_at_utc=saved['retrieved_at_utc'],sha256=saved['sha256'],bytes=len(raw));return key,raw,meta
                except (ValueError,KeyError):pass
            return key,None,meta
    futures=[]
    with ThreadPoolExecutor(max_workers=min(8,max(1,len(urls)))) as pool:
        futures=[pool.submit(fetch,k,u) for k,u in urls.items()]
        total_fetches=len(futures);completed_fetches=0
        for future in as_completed(futures):
            k,raw,meta=future.result();manifest.append(meta)
            if raw:responses[k]=raw
            completed_fetches+=1
            report_progress('acquire',4+31*completed_fetches/max(total_fetches,1),f'已取得 {completed_fetches}/{total_fetches} 个来源：{k}',completed_fetches,total_fetches,k)
    report_progress('parse',38,f'联网采集完成，开始解析 {len(responses)} 个来源',0,len(responses))
    records=[];audit={};series={}
    def save_series(source,metric,points,basis,status,published=None,metadata=None):
        for p in points:
            retrieved=next((m['retrieved_at_utc'] for m in manifest if m['source']==source),now.isoformat())
            item_metadata=dict(metadata or {})
            item_metadata.setdefault('provider_date',p['date'])
            records.append({'source':source,'metric':metric,'observation_date':p['date'],'value':p['value'],'basis':basis,'quality_status':status,'published_at':published,'retrieved_at':retrieved,'metadata':item_metadata})
        z=history_stats(points,asof);z.update(basis=basis,status=status,published_at=published)
        audit[source+':'+metric]=z;series[source+':'+metric]=points
    response_items=list(responses.items());response_total=len(response_items)
    for source_index,(source,raw) in enumerate(response_items):
        source_progress_start=40+15*source_index/max(response_total,1)
        source_progress_end=40+15*(source_index+1)/max(response_total,1)
        report_progress('parse',source_progress_start,f'正在解析来源：{source}',source_index,response_total,source)
        try:
            if source.startswith(('alfred_','fred_')):
                sid=source.split('_',1)[1];df=pd.read_csv(io.BytesIO(raw))
                vintage=source.startswith('alfred_')
                selected_vintage=None
                if vintage:
                    # ALFRED returns the latest vintage on or before the
                    # requested date; it may therefore name the column with
                    # the previous business day (for example, a Monday
                    # request can return Friday's vintage).  Selecting the
                    # greatest returned vintage <= cutoff is point-in-time
                    # safe and avoids treating an available observation as
                    # missing merely because the requested date was a
                    # weekend or future business day.
                    candidates=[]
                    for column in df.columns:
                        prefix=sid+'_'
                        if not column.startswith(prefix):continue
                        try:vd=pd.Timestamp(column[len(prefix):]).date()
                        except (TypeError,ValueError):continue
                        if vd<=asof.date():candidates.append((vd,column))
                    if not candidates:raise ValueError('没有不晚于评估日的 ALFRED vintage；不接受当前历史替代')
                    selected_vintage,required=max(candidates)
                else:required=sid
                if required not in df.columns:raise ValueError('Series column missing; do not accept current-history substitute')
                df['observation_date']=pd.to_datetime(df['observation_date']);df=df.dropna(subset=[required]);df=df[df.observation_date<=asof]
                # H15 published after market close: use prior-day observation conservatively.
                if sid=='DFII10':df=df[df.observation_date<asof]
                pts=[{'date':str(r['observation_date'].date()),'value':float(r[required])} for _,r in df.iterrows()]
                save_series(source,sid,pts,('ALFRED vintage '+str(selected_vintage)+' selected on or before cutoff; intraday release time not certified') if vintage else 'FRED current historical series, not publication-vintage certified','vintage_date_verified' if vintage else 'historical_market_series',published=str(selected_vintage) if vintage else None)
            elif source=='yahoo_price':
                j=json.loads(raw);result=j.get('chart',{}).get('result') or []
                if not result:raise ValueError('Yahoo chart returned no result')
                chart=result[0];timestamps=chart.get('timestamp') or []
                indicators=chart.get('indicators',{});adjusted=(indicators.get('adjclose') or [{}])[0].get('adjclose') or []
                closes=(indicators.get('quote') or [{}])[0].get('close') or []
                pts=[]
                for i,ts in enumerate(timestamps):
                    adjusted_value=adjusted[i] if i<len(adjusted) else None
                    value=adjusted_value if adjusted_value is not None and pd.notna(adjusted_value) else closes[i] if i<len(closes) else None
                    if value is None or not pd.notna(value):continue
                    day=str(pd.to_datetime(ts,unit='s',utc=True).date())
                    pts.append({'date':day,'value':float(value)})
                if len(pts)<200:raise ValueError('Yahoo 1d history has fewer than 200 observations')
                save_series(source,'YAHOO_PRICE',pts,profile['price_basis'],'current_history_not_vintage')
                audit[source+':YAHOO_PRICE'].update(symbol=profile['price_symbol'],currency=profile['currency'],instrument_type=profile['asset_type'])
            elif source=='eastmoney_price':
                j=json.loads(raw);data=j.get('data') or {};lines=data.get('klines') or []
                pts=[]
                for line in lines:
                    fields=line.split(',')
                    if len(fields)<3:continue
                    day,value=fields[0],fields[2]
                    if day<=date and value not in ('','-'):pts.append({'date':day,'value':float(value)})
                if len(pts)<200:raise ValueError('东方财富日线历史少于200条')
                save_series(source,'YAHOO_PRICE',pts,profile['price_basis'],'current_history_not_vintage')
                audit[source+':YAHOO_PRICE'].update(symbol=profile['price_symbol'],currency=profile['currency'],instrument_type=profile['asset_type'],provider_name=data.get('name'))
            elif source=='csindex_perf':
                j=json.loads(raw);data=j.get('data') or []
                pts=[]
                for item in data:
                    value=item.get('peg')  # API field name; documented column is rolling P/E.
                    if value in (None,'','--'): continue
                    pts.append({'date':str(pd.Timestamp(str(item.get('tradeDate'))).date()), 'value':float(value)})
                if len(pts)<60:raise ValueError('中证指数滚动市盈率有效记录少于60条')
                save_series(source,'ttm_pe',pts,'中证指数 index-perf 滚动市盈率；同一官方指数口径','official_current_history_not_vintage',metadata={'period':'TTM','index_identity':profile.get('underlying'),'method_version':'collector_parser_v1'})
            elif source=='csindex_cons':
                soffice=shutil.which('soffice')
                if not soffice:raise ValueError('未找到可读取官方 xls 成分文件的本地转换器')
                with tempfile.TemporaryDirectory() as td:
                    src=Path(td)/'cons.xls';src.write_bytes(raw)
                    subprocess.run([soffice,'--headless','--convert-to','csv','--outdir',td,str(src)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
                    cons=pd.read_csv(Path(td)/'cons.csv',dtype=str)
                code_col=next(c for c in cons.columns if 'Constituent Code' in c)
                members=[str(x).zfill(6) for x in cons[code_col].dropna().tolist()]
                def one_member(code):
                    market='1' if code.startswith(('5','6','9')) else '0'
                    url='https://push2his.eastmoney.com/api/qt/stock/kline/get?secid='+market+'.'+code+'&klt=101&fqt=0&lmt=260&end='+date.replace('-','')+'&fields1=f1%2Cf2%2Cf3%2Cf4%2Cf5%2Cf6&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55%2Cf56'
                    for attempt in range(2):
                        try:
                            j=requests.get(url,headers={'User-Agent':'Mozilla/5.0'},timeout=(5,12)).json();lines=(j.get('data') or {}).get('klines') or []
                            closes=[float(x.split(',')[2]) for x in lines if len(x.split(','))>2 and x.split(',')[2] not in ('','-')]
                            if len(closes)>=200:return {'code':code,'close':closes[-1],'ma200':sum(closes[-200:])/200,'above':closes[-1]>sum(closes[-200:])/200}
                        except Exception:
                            if attempt:break
                    return {'code':code,'error':'少于200个有效交易日或请求失败'}
                detail=[];member_total=len(members);member_step=max(1,member_total//20)
                with ThreadPoolExecutor(max_workers=12) as member_pool:
                    for member_index,item in enumerate(member_pool.map(one_member,members),1):
                        detail.append(item)
                        if member_index==1 or member_index==member_total or member_index%member_step==0:
                            report_progress('component_fetch',source_progress_start+10*member_index/max(member_total,1),f'正在读取成分行情 {member_index}/{member_total}',member_index,member_total,source)
                valid=[x for x in detail if 'above' in x];coverage=len(valid)/len(members) if members else 0
                breadth=sum(x['above'] for x in valid)/len(valid) if valid else None
                obs=max((r['date'] for r in records if r['metric']=='YAHOO_PRICE'),default=date)
                audit['csindex_cons:breadth']={'date':obs,'value':breadth,'members':len(members),'valid_members':len(valid),'coverage':coverage,'status':'current_members_current_prices','basis':'中证指数当前成分名单；各成分东方财富不复权日线；收盘高于自身MA200'}
                (run/'breadth_detail.json').write_text(json.dumps(detail,ensure_ascii=False,indent=2))
            elif source.startswith('eastmoney_forecast_'):
                pass
            elif source=='siblis_eps_page':
                html=raw.decode();tables=pd.read_html(io.StringIO(html));table=next(t for t in tables if 'EPS (Forward) *' in t.columns)
                for metric,col in [('eps_ttm_indexed','EPS (TTM) *'),('eps_ntm_indexed','EPS (Forward) *')]:
                    pts=[{'date':str(pd.Timestamp(r['Date']).date()),'value':float(r[col])} for _,r in table.iterrows()]
                    save_series(source,metric,pts,'Provider EPS index, base100 on Jan1 2024; NTM rolling not same-fiscal-period revision','provider_page_no_immutable_vintage',metadata={'period':'LTM' if metric=='eps_ttm_indexed' else 'NTM','estimate_period':'rolling' if metric=='eps_ntm_indexed' else 'realized','index_identity':'Nasdaq-100','method_version':'collector_parser_v1'})
                soup=BeautifulSoup(html,'html.parser');published=soup.find('meta',property='article:published_time');modified=soup.find('meta',property='article:modified_time')
                audit[source+':page']={'declared_published_at':published.get('content') if published else None,'declared_modified_at':modified.get('content') if modified else None,'date_verification':'Current page metadata only, not immutable historical content; never backdate a modified table'}
            elif source in ['siblis_forward','siblis_trailing']:
                j=json.loads(raw)
                if j.get('ticker')!='NDX':raise ValueError('Wrong index')
                metric='forward_pe' if source.endswith('forward') else 'ttm_pe'
                pts=[{'date':p['trading_day (EOD)'],'value':p['value']} for p in j['data']]
                save_series(source,metric,pts,'provider index ratio; sparse free month-end observations','provider_history_no_vintage',metadata={'period':'Forward' if metric=='forward_pe' else 'TTM','estimate_period':'provider_defined','index_identity':'Nasdaq-100','method_version':'collector_parser_v1'})
            elif source=='hom_breadth':
                j=json.loads(raw)
                if j.get('index')!='Nasdaq 100':raise ValueError('Wrong breadth index')
                for col in ['pct50','pct200']:
                    pts=[{'date':p['date'],'value':float(p[col])/100} for p in j['series']]
                    if any(not 0<=p['value']<=1 for p in pts):raise ValueError('Breadth outside 0..1')
                    save_series(source,col,pts,j.get('source',''),'current_members_only_not_pit',metadata={'membership_source':j.get('source'),'membership_status':'current_members_only','membership_date_field':'provider_observation_date','method_version':'collector_parser_v1'})
                    audit[source+':'+col].update(members=j.get('members'),website_updated=j.get('updated'),warning='Current constituents; historical membership and denominator coverage not certified. Never backfill historical score.')
            elif source=='hom_pe':
                j=json.loads(raw)
                for key in ['forward','trailing','forwardOwn']:
                    pts=j.get(key,[])
                    if not pts:continue
                    save_series(source,key,pts,j.get('source',{}).get('method','') if key=='forward' else j.get('source',{}).get('forwardOwnMethod','') if key=='forwardOwn' else j.get('note',''),'candidate_not_score_approved',metadata={'period':'provider_defined','estimate_period':'provider_defined','index_identity':'Nasdaq-100','method_version':'collector_parser_v1'})
                    z=audit[source+':'+key];z['website_updated']=j.get('updated');z['coverage_percent']=j.get('current',{}).get('trailingCoverage' if key=='trailing' else 'forwardCoverage' if key=='forwardOwn' else 'terminalCoverage');z['refresh_sla_days']=14 if key=='forward' else 3;z['stale_for_current_use']=z.get('age_days',999)>z['refresh_sla_days'];z['weekend_date_count']=sum(pd.Timestamp(p['date']).dayofweek>=5 for p in pts);z['warning']='No per-observation publication vintage; sources/methods must not be spliced. Website refresh is not observation refresh.'
            elif source=='hom_decomposition':
                j=json.loads(raw);audit[source]={'status':'rejected_for_eps_revision_score','reason':'Implied rolling forward EPS from price/PE does not measure revisions to the same fiscal-period estimate','source_method':j.get('source')}
        except Exception as e:audit[source]={'status':'schema_or_validation_failed','error':str(e)}
        finally:
            report_progress('parse',source_progress_end,f'已解析来源：{source}',source_index+1,response_total,source)
    forecast_rows=[]
    for source,raw in responses.items():
        if not source.startswith('eastmoney_forecast_'):continue
        try:forecast_rows.extend((json.loads(raw).get('result') or {}).get('data') or [])
        except Exception:pass
    if forecast_rows and profile.get('csindex_code')=='000300':
        current=[]
        for row in forecast_rows:
            if 'HS300_' not in str(row.get('CONCEPTINDEX_BOARD','')):continue
            estimates=[]
            for n in range(1,5):
                if row.get('YEAR_MARK'+str(n))=='E' and row.get('EPS'+str(n)) not in (None,'','-','--'):
                    try:estimates.append((int(row['YEAR'+str(n)]),float(row['EPS'+str(n)])))
                    except (TypeError,ValueError):pass
            try:price=float(row.get('NEW_PRICE'));cap=float(row.get('TOTAL_MARKET_CAP'))
            except (TypeError,ValueError):continue
            if len(estimates)>=2 and price>0 and cap>0 and estimates[0][1]>0:
                shares=cap/price;current.append({'code':row.get('SECURITY_CODE'),'market_cap':cap,'shares_proxy':shares,'year1':estimates[0][0],'eps1':estimates[0][1],'year2':estimates[1][0],'eps2':estimates[1][1],'orgs':row.get('RATING_ORG_NUM') or 0})
        if current:
            cap=sum(x['market_cap'] for x in current);earn1=sum(x['shares_proxy']*x['eps1'] for x in current);earn2=sum(x['shares_proxy']*x['eps2'] for x in current)
            coverage=len(current)/300;fy1=max(set(x['year1'] for x in current),key=lambda y:sum(x['market_cap'] for x in current if x['year1']==y))
            common=[x for x in current if x['year1']==fy1 and x['year2']==fy1+1]
            if common:
                cap=sum(x['market_cap'] for x in common);earn1=sum(x['shares_proxy']*x['eps1'] for x in common);earn2=sum(x['shares_proxy']*x['eps2'] for x in common)
                audit['eastmoney_forecast:rough_forward_pe']={'date':date,'value':cap/earn1,'earnings_proxy':earn1,'market_cap_proxy':cap,'coverage_count':len(common),'coverage_percent':len(common)/300,'fiscal_year':fy1,'median_orgs':float(pd.Series([x['orgs'] for x in common]).median()),'status':'rough_current_snapshot','basis':'东方财富个股一致预期；当前总市值/价格近似股数；仅覆盖有效预测成分，不等于官方指数Forward PE'}
                audit['eastmoney_forecast:rough_growth']={'date':date,'value':earn2/earn1-1,'coverage_count':len(common),'coverage_percent':len(common)/300,'fiscal_year':fy1+1,'median_orgs':float(pd.Series([x['orgs'] for x in common]).median()),'status':'rough_current_snapshot','basis':'同一当前快照、同一有效成分集合的下一财年聚合预测盈利/本财年聚合预测盈利-1'}
                (run/'forecast_detail.json').write_text(json.dumps(common,ensure_ascii=False,indent=2))
    comparison=[]
    if 'siblis_forward:forward_pe' in series and 'hom_pe:forward' in series:
        h=pd.Series({p['date']:p['value'] for p in series['hom_pe:forward']});h.index=pd.to_datetime(h.index)
        for p in series['siblis_forward:forward_pe']:
            sub=h.loc[:p['date']]
            if len(sub):comparison.append({'siblis_date':p['date'],'siblis_value':p['value'],'hom_date':str(sub.index[-1].date()),'hom_value':float(sub.iloc[-1]),'relative_difference':float(sub.iloc[-1]/p['value']-1),'interpretation':'dates and basis differ; diagnostic only; never average'})
    result={'as_of':date,'retrieved_at':now.isoformat(),'profile_id':profile_id,'profile':profile,'snapshot_directory':str(run),'audit':audit,'cross_source_comparison':comparison,'core_eps_revision':{'status':'not_acquired' if profile_id!='NDX' else 'not_acquired','required':['index identity','same fixed fiscal-period EPS at two snapshot dates','provider aggregation basis','publication/availability timestamps'],'rolling_eps_proxy_allowed':False},'score_policy':'No automatic scoring promotion from fetch success. Candidate PE series remains candidate. Missing core data is not a negative fundamental view.'}
    (run/'manifest.json').write_text(json.dumps(manifest,indent=2));pd.DataFrame(records).to_csv(run/'normalized.csv',index=False)
    # Phase 1B archive: every successful raw response and parsed observation is
    # appended to the PIT store.  Only the latest value from a current run is
    # OBSERVED_LIVE for the explicitly allow-listed series; the historical
    # rows in the same response remain HISTORICAL_PROXY or CANDIDATE.
    pit_db=APP_ROOT/'data'/'dashboard.sqlite3'
    pit_repo=PITRepository(pit_db)
    report_progress('archive',55,f'开始归档 {len(records)} 条观察记录',0,len(records))
    def archive_progress(completed,total):
        report_progress('archive',55+20*completed/max(total,1),f'已归档 {completed}/{total} 条观察记录',completed,total)
    result['pit_archive']=pit_repo.archive_collection(run_dir=run,manifest=manifest,records=records,profile_id=profile_id,audit=audit,as_of=date,progress_callback=archive_progress)
    # Persist every final fetch outcome, including a failed request or an
    # explicitly labelled cache fallback.  This is independent of whether a
    # browser page is open and remains idempotent for a repeated run.
    result['fetch_attempts']=pit_repo.record_fetch_attempts(run_id=run.name,manifest=manifest,run_dir=run)
    report_progress('feature_store',78,'正在生成可追溯特征快照')
    result['feature_store']=generate_feature_snapshots(pit_db,date,market=profile_id)
    # A collection is a network update, so it must close the audit chain with
    # a strict evaluation even when called from the CLI rather than the HTTP
    # server.  The evaluator reads only the PIT repository and appends a
    # Decision Log row; it never falls back to this collector's proxy data.
    report_progress('strict_evaluation',85,'正在执行严格 PIT 资格评估')
    result['strict_evaluation']=evaluate_strict_pit(pit_db,date,market=profile_id)
    chain=pit_repo.verify_decision_chain(profile_id)
    result['decision_chain']=chain
    report_progress('audit',92,'正在校验决策链并写入归档记录')
    result['archive_run']=pit_repo.record_archive_run({
        'run_id':run.name,
        'profile_id':profile_id,
        'as_of_datetime':date,
        'started_at':now.isoformat(),
        'finished_at':datetime.now(timezone.utc).isoformat(),
        'status':'SUCCESS' if not any(meta.get('error') and not meta.get('cache_fallback') for meta in manifest) else 'PARTIAL_FETCH_FAILURE',
        'strict_decision_hash':result['strict_evaluation'].get('decision_hash'),
        'strict_coverage':result['strict_evaluation'].get('coverage'),
        'chain_valid':chain.get('valid'),
        'error':{'failed_sources':[meta.get('source') for meta in manifest if meta.get('error') and not meta.get('cache_fallback')]},
    })
    report_progress('snapshot',97,'正在保存本次快照和来源清单')
    (run/'audit.json').write_text(json.dumps(result,indent=2))
    prefix='' if profile_id=='NDX' else profile_id+'_'
    (ROOT/(prefix+date+'_source_audit.json')).write_text(json.dumps(result,indent=2));pd.DataFrame(comparison).to_csv(ROOT/(prefix+date+'_source_comparison.csv'),index=False)
    report_progress('complete',100,f'联网更新完成：{len(responses)}/{len(urls)} 个来源成功',len(urls),len(urls))
    print(json.dumps({'snapshot':str(run),'successful_downloads':len(responses),'attempts':len(urls),'asof':date,'metrics':{k:{kk:v.get(kk) for kk in ['date','value','history_months','percentile_candidate','status']} for k,v in audit.items()},'pit_archive':result['pit_archive']},indent=2))
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--as-of',required=True);parser.add_argument('--profile',default='NDX',choices=PROFILE_ORDER);parser.add_argument('--refresh',action='store_true');a=parser.parse_args();collect(pd.Timestamp(a.as_of).normalize(),refresh=a.refresh,profile_id=a.profile)

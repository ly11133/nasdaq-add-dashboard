"""Drawdown opens a review; evidence determines whether that review passes."""
from datetime import date
import math

RULES={
 'forward_pe':('20 × (1 − 历史分位)','估值','同口径过去最多120个月末，至少60月，排除当前月'),
 'ttm_pe':('10 × (1 − 历史分位)','估值','实际盈利估值作交叉核验，不拼接供应商'),
 'revision':('修正 ≥5%:15；≥0%:10；≥−5%:5；更低:0','盈利','同一预测年度当前EPS / 约3个月前EPS − 1；比较基期必须为正'),
 'growth':('增长 ≥15%:10；≥5%:7；≥0%:3；更低:0','盈利','同观察日下一年度EPS / 本年度EPS − 1；比较基期必须为正'),
 'drawdown':('20 × min(回撤 / 40%, 1)','价格压力','回撤 = 1 − 当前收盘 / 截至当时历史最高收盘'),
 'vxn':('5 × 历史分位','价格压力','波动压力辅助，不代表底部'),
 'real':('5 × (1 − 历史分位)','宏观','高实际利率对应更强折现压力'),
 'nfci':('5 × (1 − 历史分位)','宏观','金融条件辅助'),
 'breadth':('5 × 站上MA200的成员比例','宽度','必须有合格的成员口径和有效分母'),
 'rsi':('5 × clip((50 − RSI14) / 20, 0, 1)','技术','Wilder RSI14；辅助项上限5分'),
 'vix':('0（背景指标，不参与总分）','背景','VXN才是本模型的纳指波动指标')
}

def trigger_path(points, asof):
    peak=0.;peak_date=None;seen=set();events=[];last=None
    for p in sorted(points,key=lambda r:r['date']):
        if p['date']>asof:continue
        value=float(p['value'])
        if not math.isfinite(value) or value<=0:raise ValueError('价格必须为有限正数')
        last=p
        if value>peak:
            peak=value;peak_date=p['date'];seen=set()
        dd=1-value/peak
        for tier in range(1,10):
            if dd+1e-12>=tier/10 and tier not in seen:
                seen.add(tier);events.append(dict(date=p['date'],tier=tier,drawdown=dd,peak=peak,peak_date=peak_date))
    if last is None:raise ValueError('截止日期前无行情')
    dd=1-last['value']/peak;current=min(9,int(math.floor((dd+1e-12)*10)))
    fresh=(date.fromisoformat(asof)-date.fromisoformat(last['date'])).days<=7
    return dict(peak=peak,peak_date=peak_date,close=last['value'],date=last['date'],drawdown=dd,current_tier=current,fresh=fresh,
        triggered=fresh and current>=1,
        levels=[dict(tier=t,drawdown=t/10,price=peak*(1-t/10),currently_breached=current>=t,reviewed_in_episode=t in seen) for t in range(1,6)],
        recent_events=events[-12:],episode_max_tier=max(seen,default=0),
        note='每档首次跨越只记录一次验证事件；反弹后再跌回同档不重复记录，新历史最高收盘出现后重置。这些是历史触发记录，不是交易次数。')

def verification(rows, trigger):
    m={r['key']:r for r in rows}
    groups=[]
    for label,keys,threshold in [('估值',['forward_pe','ttm_pe'],15),('盈利',['revision','growth'],15),('宏观',['real','nfci'],None),('宽度',['breadth'],None),('价格辅助',['vxn','rsi'],None)]:
        records=[m[k] for k in keys];complete=all(r['score'] is not None for r in records)
        known=sum(r['score'] or 0 for r in records);maximum=sum(r['weight'] for r in records)
        state='待核验' if not complete else ('达到门槛' if known>=threshold else '未达门槛') if threshold is not None else '辅助证据已取得'
        groups.append(dict(label=label,keys=keys,known=known,maximum=maximum,state=state,missing=[r['label'] for r in records if r['score'] is None],threshold=threshold))
    ready=all(g['state']=='达到门槛' for g in groups[:2]);rev=m['revision']
    if not trigger['fresh']:state='行情过期，先更新'
    elif not trigger['triggered']:state='未到10%回撤，继续监测'
    elif rev['score'] is not None and rev['value']<=-.1:state='已触发验证，盈利风险优先'
    elif ready:state='已触发验证，估值与盈利达门槛'
    elif any(g['state']=='未达门槛' for g in groups[:2]):state='已触发验证，核心条件未通过'
    else:state='已触发验证，等待核心证据'
    return dict(state=state,groups=groups,note='触发不等于通过。核心门槛沿用原规则：估值至少15/30、盈利至少15/25；达到门槛仍不是收益保证或自动买入。')


def execution_status(trigger, result, rows):
    """Translate evidence into a low-maintenance review state.

    This deliberately does not return an amount, position size, or order.  The
    normal DCA instruction is assumed to continue outside this engine.
    """
    if not trigger.get('fresh'):
        return dict(status='行情需要更新', code='stale', reason='最近收盘数据已过期，暂停当前机会筛选；常规定投规则不受此状态影响。')
    if not trigger.get('triggered'):
        return dict(status='保持常规定投，等待回撤触发', code='dca_only', reason='当前回撤尚未达到10%验证档；系统继续观察，不启动额外投入核查。')
    rev=next((r for r in rows if r['key']=='revision'),None)
    if rev and rev.get('score') is not None and rev.get('value')<=-.10:
        return dict(status='已触发验证，额外投入暂缓', code='earnings_risk', reason='价格触发了回撤验证，但同财政期盈利下修达到10%，先处理盈利风险证据。')
    core=[g for g in result.get('groups',[]) if g['label'] in ('估值','盈利')]
    if len(core)==2 and all(g['state']=='达到门槛' for g in core):
        return dict(status='进入额外投入观察', code='core_pass', reason='回撤已触发，估值和盈利均达到核心门槛；仍需人工确认来源和自身风险承受能力。')
    failed=[g for g in core if g['state']=='未达门槛']
    if failed:
        return dict(status='已触发验证，额外投入暂缓', code='core_fail', reason='回撤已触发，但核心估值或盈利条件未通过；继续常规定投并等待新证据。')
    return dict(status='已触发验证，等待核心证据', code='core_unknown', reason='回撤已触发，但估值或同财政期盈利证据尚未齐全；不把缺失当作通过或否决。')

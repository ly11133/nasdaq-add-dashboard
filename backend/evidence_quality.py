"""Qualify public observations without promoting downloaded values into scored evidence."""
from datetime import date

LABELS={
 'hom_pe:forward':('终端一致预期 PE','valuation',14),
 'hom_pe:forwardOwn':('提供商自算预期 PE','valuation',3),
 'hom_pe:trailing':('提供商自算 TTM PE','valuation',3),
 'siblis_forward:forward_pe':('Siblis 预期 PE','valuation',35),
 'siblis_trailing:ttm_pe':('Siblis TTM PE','valuation',35),
 'siblis_eps_page:eps_ttm_indexed':('Siblis 已实现 EPS 指数','earnings',120),
 'siblis_eps_page:eps_ntm_indexed':('Siblis 滚动预期 EPS 指数','earnings',120),
 'hom_breadth:pct200':('当前成员站上 MA200','breadth',7),
 'hom_breadth:pct50':('当前成员站上 MA50','breadth',7),
}

def assess(audit,asof):
    result=[]
    for key,(label,module,sla) in LABELS.items():
        z=audit.get('audit',{}).get(key,{})
        if z.get('value') is None:continue
        obs=date.fromisoformat(z['date']);age=(date.fromisoformat(asof)-obs).days
        if age<0:continue
        reasons=[];n=z.get('history_months',0)
        if age>sla:reasons.append(f'观察值已过去 {age} 天，超过 {sla} 天刷新要求')
        if module=='valuation' and n<60:reasons.append(f'同口径历史仅 {n} 个月，评分至少需要 60 个月')
        if key=='hom_pe:forwardOwn':reasons.append('自算 FY1/FY2 混合预期，与终端历史是不同序列；禁止拼接')
        if key=='hom_pe:trailing':reasons.append('提供商使用 Σ(w×P)/Σ(w×EPS)，尚未与指数标准聚合口径核验')
        if module=='valuation':reasons.append('逐条首次可用时间未认证；不用于时点回测')
        if module=='earnings':reasons.append('以 2024-01-01=100 为基期的 EPS 指数；不是美元 EPS，也不是同财政年预测修正')
        if module=='breadth':
            reasons.append('使用当前成员；缺少历史成员与完整分母认证')
            members=z.get('members')
            if isinstance(members,int) and 1<=members<=1000 and z.get('date')==z.get('website_updated'):
                if not any(abs(i/members-z['value'])<=.00050001 for i in range(members+1)):
                    reasons.append(f"{z['value']:.1%} 与所报 {members} 只成员的等权计数不吻合，需确认有效分母")
        if obs.weekday()>=5:reasons.append('观察日期落在周末，不能直接称作美股交易日收盘值')
        result.append(dict(key=key,label=label,module=module,value=z['value'],date=z['date'],age_days=age,history_months=n,percentile=z.get('percentile_candidate'),status='stale' if age>sla else 'candidate',reasons=reasons,basis=z.get('basis',''),coverage_percent=z.get('coverage_percent'),website_updated=z.get('website_updated')))
    return result

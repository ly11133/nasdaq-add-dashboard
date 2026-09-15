"""Free evidence view v1: descriptive opportunity screening, separate from strict score."""
from datetime import date

def analyze(quote, rows, quality, asof, profile=None):
    profile=profile or {'id':'NDX','label':'Nasdaq-100','short_label':'NDX','volatility_label':'VXN','fundamental_status':'公开估值候选；同财政期 EPS 仍需合格导入'}
    profile_id=profile.get('id','NDX')
    asset_label=profile.get('short_label') or profile.get('label') or profile_id
    volatility_label=profile.get('volatility_label','波动率代理')
    m={r['key']:r for r in rows};support=[];against=[];unknown=[];context=[]
    def fresh(key):
        r=m.get(key,{})
        return r.get('value') is not None and r.get('score') is not None
    dd=-quote['drawdown'];price_fresh=(date.fromisoformat(asof)-date.fromisoformat(quote['date'])).days<=7
    if not price_fresh:
        title='行情过期，等待更新';summary='保留历史观察，不用过期价格给出当前机会判断。'
        unknown.append('最近行情已超过 7 天，需要更新。')
    elif dd>=.2:
        title='深度回撤，进入重点观察';summary='价格已显著回落，值得核查额外加仓机会；还需用估值与盈利判断下跌原因。'
        support.append(f'距已获取历史高点回撤 {dd:.2%}，达到 20% 深度回撤观察档。')
    elif dd>=.1:
        title='回撤出现，关注加仓条件';summary='价格达到 10% 回撤观察档；这是重新评估的触发条件，还不是便宜或盈利稳定的证明。'
        support.append(f'距已获取历史高点回撤 {dd:.2%}，达到 10% 回撤观察档。')
    else:
        title='价格回撤有限，额外机会待观察';summary='当前价格尚未达到 10% 回撤观察档，单从跌幅看，额外投入的价格触发条件尚未出现。'
        against.append(f'距已获取历史高点仅回撤 {dd:.2%}，未达到 10% 观察档；这不代表估值一定昂贵。')
    if fresh('rsi'):
        rv=quote['rsi'];context.append(f'RSI(14) 为 {rv:.2f}，'+('短期超卖，但与回撤属于相关价格证据，不重复增加支持票。' if rv<30 else '尚未进入低于 30 的短期超卖区。'))
    if fresh('vxn'):context.append(f"{volatility_label} 为 {m['vxn']['value']:.2f}，表示预期波动大小；不据此判断底部或涨跌方向。")
    if fresh('real'):
        r=m['real'];p=1-r['score']/r['weight']
        if p>=.8:against.append(f"10Y 实际利率为 {r['value']:.2f}%，处于此前样本较高区间，构成估值折现压力。")
        else:context.append(f"10Y 实际利率为 {r['value']:.2f}%；需结合盈利增速理解估值。")
    if fresh('nfci'):
        v=m['nfci']['value']
        if v<0:support.append(f'NFCI 为 {v:.3f}，金融条件较历史均值宽松；这是宏观背景支持，不是买点确认。')
        else:against.append(f'NFCI 为 {v:.3f}，金融条件较历史均值紧，对风险资产形成压力。')
    valuation_ok=all(fresh(k) for k in ['forward_pe','ttm_pe'])
    if valuation_ok:
        score=sum(m[k]['score'] for k in ['forward_pe','ttm_pe'])
        (support if score>=15 else against).append('合格同口径估值证据'+('达到原规则的估值支持门槛。' if score>=15 else '未达到原规则的估值支持门槛。'))
    else:
        if profile_id=='NDX':
            unknown.append('估值便宜与否仍待确认：公开 PE 的口径、时效和历史长度存在限制，不能混用计算分位。')
            candidate=next((z for z in quality if z.get('key')=='hom_pe:forward' and z.get('status')!='stale'),None)
            if candidate and candidate.get('percentile') is not None:
                p=candidate['percentile']
                if p<=.2:
                    support.append(f"公开终端预期 PE 候选分位约 {p:.1%}，处于历史较低区间；来源资格未认证，只作线索。")
                elif p>=.8:
                    against.append(f"公开终端预期 PE 候选分位约 {p:.1%}，处于历史较高区间；来源资格未认证，只作风险线索。")
                else:
                    context.append(f"公开终端预期 PE 候选分位约 {p:.1%}，处于历史中间区间；未进入严格评分。")
        else:
            unknown.append(f'{asset_label} 的估值证据尚未接入合格的指数聚合 PE；不把 ETF 折溢价或金价简单换算成 PE。')
    if fresh('revision'):
        v=m['revision']['value']
        (support if v>=0 else against).append(f'同财政期 EPS 三个月修正 {v:+.2%}。')
        if v<=-.1 and price_fresh:
            title='盈利明显下修，优先核查风险';summary='虽然价格可能回落，但同财政期盈利预期下修达到 10%；不能只凭回撤增强加仓倾向。'
    else:
        unknown.append('同财政期 EPS 修正未知；滚动预期 EPS 的变化不能证明盈利预期上调或下调。' if profile_id=='NDX' else f'{asset_label} 尚未接入合格的同财政期 EPS 预测；价格回撤不能替代盈利证据。')
    for k in ['vxn','real','nfci']:
        if not fresh(k):unknown.append(m.get(k,{}).get('label',k)+' 尚无有效的及时观察。')
    latest=next((r for r in quality if r['key']=='hom_pe:forwardOwn' and r['status']!='stale'),None)
    if latest and profile_id=='NDX':context.append(f"公开自算预期 PE 为 {latest['value']:.2f}（{latest['date']}），作为当期观察；不与终端 PE 历史拼接。")
    breadth=m.get('breadth',{})
    if breadth.get('value') is not None:
        if fresh('breadth'):context.append(f"合格宽度观察：{breadth['value']:.1%} 成员站上 MA200。")
        else:unknown.append(f"公开宽度为 {breadth['value']:.1%}（{breadth.get('date')}），有效分母或成员口径未认证，暂不用于方向判断。")
    else:unknown.append('市场宽度尚未取得。' if profile_id=='NDX' else f'{asset_label} 尚未接入逐日成员名单与完整分母，市场宽度暂不计分。')
    # A rolling NTM/TTM spread can be useful context when the free source has
    # both observations on the same date. It is deliberately not treated as
    # a next-fiscal-year estimate.
    eps_ttm=next((z for z in quality if z.get('key')=='siblis_eps_page:eps_ttm_indexed'),None)
    eps_ntm=next((z for z in quality if z.get('key')=='siblis_eps_page:eps_ntm_indexed'),None)
    if profile_id=='NDX' and eps_ttm and eps_ntm and eps_ttm.get('date')==eps_ntm.get('date') and eps_ttm.get('value'):
        context.append(f"滚动 EPS 指数背景：同日 TTM {eps_ttm['value']:.2f}、NTM {eps_ntm['value']:.2f}，NTM 高 {(eps_ntm['value']/eps_ttm['value']-1):.1%}；不能替代同财政期修正。")
    next_conditions=['价格回撤跨过 10% / 20% 观察档时重新评估；阈值沿用原回撤思路，未经收益回测优化。','有同口径估值历史或盈利新资料时更新证据；盈利明显下修时优先核查风险。']
    if profile_id!='NDX':
        next_conditions=[f'价格回撤跨过 10% / 20% 观察档时重新评估 {asset_label}；回撤只触发核查，不等于价格便宜。',f'补齐 {asset_label} 对应指数的合格 PE/EPS 或成员宽度数据后，才可能提升核心证据覆盖。']
    return dict(version='free-evidence-v1',title=title,summary=summary,support=support,against=against,unknown=unknown,context=context,
        next_conditions=next_conditions,
        boundary=f'免费版是证据观察和机会筛选，不是完整买入评级；支持项数量不表示概率，不按票数决定买卖。正常定投计划与额外加仓评估分开。{(" "+profile.get("price_basis","")) if profile_id!="NDX" else ""}')

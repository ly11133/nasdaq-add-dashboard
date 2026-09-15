"""Concrete index and asset profiles used by the dashboard."""

def yahoo_profile(profile_id, label, short_label, symbol, underlying, currency, url, fundamental_status, asset_type='index'):
    return {
        'id': profile_id, 'label': label, 'short_label': short_label,
        'asset_type': asset_type, 'underlying': underlying, 'currency': currency,
        'price_source_type': 'yahoo', 'price_symbol': symbol,
        'price_source': f'Yahoo Finance {symbol} 历史收盘', 'price_source_url': url,
        'price_basis': 'Yahoo Finance 当前历史收盘序列；价格指数口径，不含基金费用与分红再投资，且不是逐日发布版本',
        'volatility_series': 'VIXCLS', 'volatility_label': 'VIX（跨市场风险情绪代理）',
        'fundamental_status': fundamental_status, 'status': 'active',
    }

PROFILES = {
    'NDX': {
        'id': 'NDX', 'label': '纳斯达克100指数', 'short_label': 'NDX', 'asset_type': 'index',
        'underlying': 'Nasdaq-100 Index', 'currency': 'USD', 'price_source_type': 'fred',
        'price_series': 'NASDAQ100', 'price_source': 'FRED NASDAQ100',
        'price_source_url': 'https://fred.stlouisfed.org/series/NASDAQ100',
        'price_basis': 'FRED 当前历史收盘序列；价格指数口径，不含基金费用与分红再投资，且不是逐日发布版本',
        'volatility_series': 'VXNCLS', 'volatility_label': 'VXN',
        'fundamental_status': 'NDX 专用公开估值观察值；同财政期 EPS 仍需合格时点数据', 'status': 'active',
    },
    'SPX': yahoo_profile('SPX', '标普500指数', 'S&P 500', '^GSPC', 'S&P 500 Index', 'USD', 'https://finance.yahoo.com/quote/%5EGSPC/history/', '未接入标普500合格的时点化指数聚合 PE/EPS 预测'),
    'GOLD': yahoo_profile('GOLD', 'COMEX黄金期货连续合约', 'Gold', 'GC=F', 'COMEX Gold Futures continuous contract', 'USD', 'https://finance.yahoo.com/quote/GC%3DF/history/', '黄金没有股票 EPS；PE 与 EPS 模块不适用', 'commodity_future'),
    'N225': yahoo_profile('N225', '日经225指数', 'Nikkei 225', '^N225', 'Nikkei Stock Average (Nikkei 225)', 'JPY', 'https://finance.yahoo.com/quote/%5EN225/history/', '未接入日经225合格的时点化指数聚合 PE/EPS 预测'),
    'HSI': yahoo_profile('HSI', '恒生指数', 'HSI', '^HSI', 'Hang Seng Index', 'HKD', 'https://finance.yahoo.com/quote/%5EHSI/history/', '未接入恒生指数合格的时点化指数聚合 PE/EPS 预测'),
    'CSI300': yahoo_profile('CSI300', '沪深300指数', 'CSI 300', '000300.SS', 'CSI 300 Index', 'CNY', 'https://finance.yahoo.com/quote/000300.SS/history/', '未接入沪深300合格的时点化指数聚合 PE/EPS 预测'),
    'SSE': yahoo_profile('SSE', '上证综合指数', '上证指数', '000001.SS', 'SSE Composite Index', 'CNY', 'https://finance.yahoo.com/quote/000001.SS/history/', '未接入上证综指合格的时点化指数聚合 PE/EPS 预测'),
    'SZSE': yahoo_profile('SZSE', '深证成份指数', '深证成指', '399001.SZ', 'SZSE Component Index', 'CNY', 'https://finance.yahoo.com/quote/399001.SZ/history/', '未接入深证成指合格的时点化指数聚合 PE/EPS 预测'),
    'CHINEXT': yahoo_profile('CHINEXT', '创业板指数', '创业板指', '399006.SZ', 'ChiNext Index', 'CNY', 'https://finance.yahoo.com/quote/399006.SZ/history/', '未接入创业板指合格的时点化指数聚合 PE/EPS 预测'),
}

# Yahoo currently exposes only a fragment for 399006.SZ.  Eastmoney's public
# daily index series is used explicitly for this one profile.
PROFILES['CHINEXT'].update({
    'price_source_type': 'eastmoney', 'price_symbol': '0.399006',
    'price_source': '东方财富 399006 日线收盘',
    'price_source_url': 'https://quote.eastmoney.com/zs399006.html',
    'price_basis': '东方财富创业板指数不复权日线收盘；价格指数口径，不含基金费用与分红再投资，且不是逐日发布版本',
})
PROFILES['CSI300'].update({
    'price_source_type': 'eastmoney', 'price_symbol': '1.000300',
    'csindex_code': '000300',
    'price_source': '东方财富 000300 日线收盘',
    'price_source_url': 'https://quote.eastmoney.com/zs000300.html',
    'price_basis': '东方财富沪深300指数不复权日线收盘；价格指数口径，不含基金费用与分红再投资，且不是逐日发布版本',
})

PROFILE_ORDER = ['NDX', 'SPX', 'GOLD', 'N225', 'HSI', 'CSI300', 'SSE', 'SZSE', 'CHINEXT']

def profile_list():
    return [PROFILES[key] for key in PROFILE_ORDER]

def get_profile(profile_id):
    try:
        return PROFILES[str(profile_id).upper()]
    except KeyError as exc:
        raise ValueError(f'不支持的标的：{profile_id}') from exc

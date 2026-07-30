"""
PULSE — minimal markets & world dashboard.
Stdlib-only backend: proxies Yahoo Finance charts, RSS news and
World Bank GDP data with in-memory caching, and serves the frontend.
"""

import http.server
import json
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

PORT = int(os.environ.get('PORT', 8090))
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36')

ssl_ctx = ssl.create_default_context()
try:
    import certifi
    ssl_ctx.load_verify_locations(certifi.where())
except ImportError:
    try:
        ssl_ctx.load_default_certs()
        # probe: if system store is unusable (common on macOS), fall back
        import urllib.request as _u
        _u.urlopen(_u.Request('https://api.worldbank.org', method='HEAD'),
                   context=ssl_ctx, timeout=5)
    except Exception:
        ssl_ctx = ssl._create_unverified_context()

# ── symbol universe ─────────────────────────────────────────────
MARKETS = [
    ('^GSPC', 'S&P 500'), ('^IXIC', 'Nasdaq'), ('^DJI', 'Dow Jones'),
    ('^SSMI', 'SMI'), ('^GDAXI', 'DAX'), ('^FTSE', 'FTSE 100'),
    ('^N225', 'Nikkei 225'), ('^HSI', 'Hang Seng'),
    ('EURUSD=X', 'EUR/USD'), ('CHF=X', 'USD/CHF'),
    ('GC=F', 'Gold'), ('CL=F', 'WTI Crude'),
    ('BTC-USD', 'Bitcoin'), ('^TNX', 'US 10Y Yield'), ('^VIX', 'VIX'),
]

THEMES = {
    'ai': [('NVDA', 'Nvidia'), ('MSFT', 'Microsoft'), ('GOOGL', 'Alphabet'),
           ('AMD', 'AMD'), ('PLTR', 'Palantir'), ('TSM', 'TSMC'),
           ('ASML', 'ASML'), ('META', 'Meta'), ('AVGO', 'Broadcom'),
           ('SMCI', 'Supermicro')],
    'megacaps': [('AAPL', 'Apple'), ('MSFT', 'Microsoft'), ('AMZN', 'Amazon'),
                 ('GOOGL', 'Alphabet'), ('META', 'Meta'), ('NVDA', 'Nvidia'),
                 ('TSLA', 'Tesla'), ('BRK-B', 'Berkshire'), ('JPM', 'JPMorgan'),
                 ('V', 'Visa')],
    'swiss': [('NESN.SW', 'Nestlé'), ('NOVN.SW', 'Novartis'), ('ROG.SW', 'Roche'),
              ('UBSG.SW', 'UBS'), ('ZURN.SW', 'Zurich'), ('ABBN.SW', 'ABB'),
              ('CFR.SW', 'Richemont'), ('LONN.SW', 'Lonza'), ('SIKA.SW', 'Sika'),
              ('HOLN.SW', 'Holcim')],
    'energy': [('XOM', 'Exxon'), ('CVX', 'Chevron'), ('SHEL', 'Shell'),
               ('TTE', 'TotalEnergies'), ('BP', 'BP'), ('NEE', 'NextEra'),
               ('ENPH', 'Enphase'), ('FSLR', 'First Solar'), ('VWS.CO', 'Vestas'),
               ('CEG', 'Constellation')],
    'defense': [('LMT', 'Lockheed'), ('RTX', 'RTX'), ('NOC', 'Northrop'),
                ('GD', 'General Dynamics'), ('BA', 'Boeing'), ('RHM.DE', 'Rheinmetall'),
                ('HO.PA', 'Thales'), ('BA.L', 'BAE Systems'), ('LDO.MI', 'Leonardo'),
                ('SAAB-B.ST', 'Saab')],
    'crypto': [('BTC-USD', 'Bitcoin'), ('ETH-USD', 'Ethereum'), ('SOL-USD', 'Solana'),
               ('COIN', 'Coinbase'), ('MSTR', 'Strategy'), ('XRP-USD', 'XRP'),
               ('BNB-USD', 'BNB'), ('DOGE-USD', 'Dogecoin'), ('ADA-USD', 'Cardano'),
               ('AVAX-USD', 'Avalanche')],
}

NEWS_FEEDS = [
    ('https://feeds.bbci.co.uk/news/world/rss.xml', 'BBC', 'world'),
    ('https://www.theguardian.com/world/rss', 'Guardian', 'world'),
    ('https://www.cnbc.com/id/100003114/device/rss/rss.html', 'CNBC', 'markets'),
    ('https://feeds.content.dowjones.io/public/rss/mw_topstories', 'MarketWatch', 'markets'),
    ('https://www.ft.com/world?format=rss', 'FT', 'world'),
]

GDP_COUNTRIES = ['USA', 'CHN', 'DEU', 'JPN', 'IND', 'GBR', 'FRA', 'CHE',
                 'ITA', 'BRA', 'KOR', 'ESP', 'NLD', 'POL', 'ARE', 'SGP']

# ── tiny cache ──────────────────────────────────────────────────
_cache = {}
_lock = threading.Lock()


def cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    try:
        val = fn()
    except Exception:
        if hit:  # serve stale data rather than an error
            return hit[1]
        raise
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def fetch(url, timeout=12, headers=None):
    h = {'User-Agent': UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, context=ssl_ctx, timeout=timeout) as r:
        return r.read()


# ── yahoo session (cookie + crumb — required from datacenter IPs) ──
_yh = {'cookie': None, 'crumb': None, 'ts': 0}


def _yahoo_session(force=False):
    with _lock:
        fresh = _yh['cookie'] and time.time() - _yh['ts'] < 3000
        if fresh and not force:
            return _yh['cookie'], _yh['crumb']
    cookie = None
    req = urllib.request.Request('https://fc.yahoo.com/', headers={'User-Agent': UA})
    try:
        urllib.request.urlopen(req, context=ssl_ctx, timeout=10)
    except urllib.error.HTTPError as e:  # 404/999 is fine — we want the cookie
        cookie = e.headers.get('Set-Cookie', '')
    except Exception:
        pass
    if cookie:
        cookie = cookie.split(';')[0]
    crumb = None
    if cookie:
        try:
            crumb = fetch('https://query1.finance.yahoo.com/v1/test/getcrumb',
                          headers={'Cookie': cookie}).decode().strip()
        except Exception:
            crumb = None
    with _lock:
        _yh.update(cookie=cookie, crumb=crumb, ts=time.time())
    return cookie, crumb


def yahoo_fetch(url, timeout=15):
    cookie, crumb = _yahoo_session()
    if crumb:
        url += ('&' if '?' in url else '?') + 'crumb=' + urllib.parse.quote(crumb)
    headers = {'Cookie': cookie} if cookie else None
    try:
        return fetch(url, timeout=timeout, headers=headers)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429):  # refresh session once and retry
            cookie, crumb = _yahoo_session(force=True)
            headers = {'Cookie': cookie} if cookie else None
            time.sleep(1.2)
            return fetch(url, timeout=timeout, headers=headers)
        raise


# ── yahoo quotes (batched spark endpoint: many symbols, one call) ──
def _mk_quote(symbol, price, prev, closes, chg_pct=None):
    spark = [c for c in (closes or []) if isinstance(c, (int, float)) and c > 0]
    if len(spark) >= 5:  # drop glitch points (zero prints, bad ticks) far off the median
        med = sorted(spark)[len(spark) // 2]
        spark = [c for c in spark if abs(c - med) <= med * 0.12]
    chg = chg_pct
    if chg is None and price is not None and prev and abs(price - prev) > 1e-9:
        chg = (price - prev) / prev * 100
    if chg is None and price and len(spark) > 1:
        chg = (price - spark[0]) / spark[0] * 100
    return {
        'symbol': symbol,
        'price': price,
        'prevClose': prev,
        'changePct': round(chg, 2) if chg is not None else None,
        'spark': spark[-40:],
    }


def _parse_spark(data):
    """Handle both spark response shapes Yahoo serves."""
    out = {}
    if 'spark' in data:  # {"spark":{"result":[{"symbol":..,"response":[chart]}]}}
        for r in (data['spark'].get('result') or []):
            sym = r.get('symbol')
            resp = (r.get('response') or [{}])[0]
            meta = resp.get('meta', {})
            closes = []
            try:
                closes = resp['indicators']['quote'][0]['close']
            except (KeyError, IndexError, TypeError):
                pass
            out[sym] = _mk_quote(sym, meta.get('regularMarketPrice'),
                                 meta.get('chartPreviousClose') or meta.get('previousClose'),
                                 closes)
    else:  # {"^GSPC":{"close":[...],"previousClose":..,...}}
        for sym, r in data.items():
            if not isinstance(r, dict):
                continue
            closes = r.get('close') or []
            price = closes[-1] if closes else None
            out[sym] = _mk_quote(sym, price,
                                 r.get('previousClose') or r.get('chartPreviousClose'),
                                 closes)
    return out


def spark_batch(symbols, rng='1d', interval='15m'):
    from urllib.parse import quote as urlq
    url = (f'https://query1.finance.yahoo.com/v8/finance/spark?'
           f'symbols={urlq(",".join(symbols))}&range={rng}&interval={interval}')
    last_err = None
    for attempt in range(3):
        try:
            return _parse_spark(json.loads(yahoo_fetch(url)))
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise last_err


# ── fallback sources (Yahoo blocks many datacenter IPs) ─────────
# Cboe delayed-quotes CDN: index products, US stocks, ETFs, liquid ADRs
CBOE_MAP = {
    '^GSPC': ('_SPX', 1, None),
    '^IXIC': ('_NDX', 1, 'Nasdaq 100'),
    '^DJI': ('_DJX', 100, None),
    '^TNX': ('_TNX', 0.1, None),
    '^VIX': ('_VIX', 1, None),
    '^SSMI': ('EWL', 1, 'Switzerland · EWL'),
    '^GDAXI': ('EWG', 1, 'Germany · EWG'),
    '^FTSE': ('EWU', 1, 'UK · EWU'),
    '^N225': ('EWJ', 1, 'Japan · EWJ'),
    '^HSI': ('EWH', 1, 'Hong Kong · EWH'),
    'GC=F': ('GLD', 1, 'Gold · GLD'),
    'CL=F': ('USO', 1, 'Oil · USO'),
    # European/Swiss listings via US ADRs / NYSE lines
    'NESN.SW': ('NSRGY', 1, None), 'NOVN.SW': ('NVS', 1, None),
    'ROG.SW': ('RHHBY', 1, None), 'UBSG.SW': ('UBS', 1, None),
    'ZURN.SW': ('ZURVY', 1, None), 'ABBN.SW': ('ABB', 1, None),
    'CFR.SW': ('CFRUY', 1, None), 'LONN.SW': ('LZAGY', 1, None),
    'SIKA.SW': ('SXYAY', 1, None), 'HOLN.SW': ('HCMLY', 1, None),
    'RHM.DE': ('RNMBY', 1, None), 'HO.PA': ('THLLY', 1, None),
    'BA.L': ('BAESY', 1, None), 'LDO.MI': ('FINMY', 1, None),
    'SAAB-B.ST': ('SAABY', 1, None), 'VWS.CO': ('VWDRY', 1, None),
    'BRK-B': ('BRK.B', 1, None),
}
COINBASE = {'BTC-USD', 'ETH-USD', 'SOL-USD', 'XRP-USD', 'ADA-USD',
            'AVAX-USD', 'DOGE-USD'}
FRANKFURTER = {'EURUSD=X': ('EUR', 'USD'), 'CHF=X': ('USD', 'CHF')}


def cboe_quote(sym, with_chart=False):
    cboe_sym, mult, rename = CBOE_MAP.get(sym) or (sym, 1, None)
    d = json.loads(fetch(
        f'https://cdn.cboe.com/api/global/delayed_quotes/quotes/{cboe_sym}.json',
        timeout=10))['data']
    price = d.get('current_price')
    prev = d.get('prev_day_close')
    # after the session Cboe rolls prev_day_close onto the close (change would
    # read 0.00%) — the real day move sits in price_change_percent
    chg_pct = d.get('price_change_percent')
    if not isinstance(chg_pct, (int, float)):
        chg_pct = None
    elif price and chg_pct > -100:
        prev = price / (1 + chg_pct / 100)
    closes = []
    if with_chart:
        try:  # intraday chart for the sparkline (best effort)
            ch = json.loads(fetch(
                f'https://cdn.cboe.com/api/global/delayed_quotes/charts/intraday/{cboe_sym}.json',
                timeout=10))
            pts = ch.get('data') or []
            if pts and isinstance(pts[0], dict) and 'data_points' in pts[0]:
                pts = pts[0]['data_points']
            for p in pts:
                v = p.get('price') if isinstance(p, dict) else None
                if isinstance(v, dict):  # index feeds: price is an OHLC object
                    v = v.get('close')
                if isinstance(v, (int, float)):
                    closes.append(v)
        except Exception:
            pass
    if not closes and prev and price:
        closes = [prev, price]
    if mult != 1:
        price = price * mult if price else price
        prev = prev * mult if prev else prev
        closes = [c * mult for c in closes if c]
    q = _mk_quote(sym, price, prev, closes, chg_pct=chg_pct)
    if rename:
        q['rename'] = rename
    return q


def coinbase_quote(sym):
    spot = json.loads(fetch(
        f'https://api.coinbase.com/v2/prices/{sym}/spot', timeout=10))
    price = float(spot['data']['amount'])
    closes, prev = [], None
    try:
        candles = json.loads(fetch(
            f'https://api.exchange.coinbase.com/products/{sym}/candles?granularity=3600',
            timeout=10))[:24]
        closes = [c[4] for c in reversed(candles)]
        prev = closes[0] if closes else None
    except Exception:
        pass
    return _mk_quote(sym, price, prev, closes + [price])


def kraken_quote(sym):
    pair = sym.replace('-USD', 'USD').replace('BTC', 'XBT')
    o = json.loads(fetch(
        f'https://api.kraken.com/0/public/OHLC?pair={pair}&interval=60',
        timeout=10))
    key = next(k for k in o['result'] if k != 'last')
    closes = [float(c[4]) for c in o['result'][key][-24:]]
    price = closes[-1] if closes else None
    prev = closes[0] if closes else None
    return _mk_quote(sym, price, prev, closes)


def crypto_quote(sym):
    try:
        return coinbase_quote(sym)
    except Exception:
        return kraken_quote(sym)


def frankfurter_quote(sym):
    base, target = FRANKFURTER[sym]
    hist = json.loads(fetch(
        f'https://api.frankfurter.dev/v1/2026-06-25..?base={base}&symbols={target}',
        timeout=10))
    days = sorted(hist['rates'].items())
    closes = [v[target] for _, v in days]
    price = closes[-1] if closes else None
    prev = closes[-2] if len(closes) > 1 else None
    return _mk_quote(sym, price, prev, closes)


def fallback_quote(sym, with_chart=False):
    if sym in FRANKFURTER:
        return frankfurter_quote(sym)
    if sym in COINBASE:
        return crypto_quote(sym)
    if sym in CBOE_MAP or (sym.replace('.', '').isalpha() and sym.isupper()):
        return cboe_quote(sym, with_chart=with_chart)
    raise ValueError('no fallback source')


_LAST_GOOD = {}  # per-symbol memory: heal tiles a rate-limited pass left empty


def quotes_for(pairs, with_chart=False):
    quotes = {}
    syms = [s for s, _ in pairs]
    try:  # primary: Yahoo batched spark (works from residential IPs)
        for i in range(0, len(syms), 20):
            quotes.update(spark_batch(syms[i:i + 20]))
    except Exception:
        pass
    for attempt in range(2):  # fill gaps via fallback sources, politely throttled
        missing = [s for s in syms
                   if not (quotes.get(s) and quotes[s].get('price') is not None)]
        if not missing:
            break
        if attempt:
            time.sleep(1.5)  # let the Cboe rate limiter cool off before retrying
        for sym in missing:
            try:
                quotes[sym] = fallback_quote(sym, with_chart=with_chart)
            except Exception:
                pass
            time.sleep(0.25)
    out = []
    for sym, name in pairs:
        q = quotes.get(sym)
        if q and q.get('price') is not None:
            _LAST_GOOD[sym] = q
        else:
            q = _LAST_GOOD.get(sym)
        q = dict(q) if q else {'symbol': sym, 'price': None,
                               'changePct': None, 'spark': []}
        q['name'] = q.pop('rename', None) or name
        out.append(q)
    return out


def get_markets():
    data = cached('markets', 240, lambda: quotes_for(MARKETS, with_chart=True))
    if any(q.get('price') is None for q in data):
        with _lock:  # incomplete build: age the cache so it refreshes after 60s
            hit = _cache.get('markets')
            if hit:
                _cache['markets'] = (min(hit[0], time.time() - 180), hit[1])
    return data


def get_theme(name):
    pairs = THEMES.get(name)
    if not pairs:
        return None
    return cached(f'theme:{name}', 300, lambda: quotes_for(pairs))


def get_movers():
    def build():
        seen, pairs = set(), []
        for plist in THEMES.values():
            for sym, name in plist:
                if sym not in seen and '-USD' not in sym:
                    seen.add(sym)
                    pairs.append((sym, name))
        qs = [q for q in quotes_for(pairs) if q.get('changePct') is not None]
        qs.sort(key=lambda q: q['changePct'], reverse=True)
        return {'gainers': qs[:6], 'losers': qs[-6:][::-1]}
    return cached('movers', 300, build)


# ── news ────────────────────────────────────────────────────────
def parse_feed(url, source, category):
    items = []
    try:
        raw = fetch(url, timeout=10)
        root = ET.fromstring(raw)
        for item in root.iter('item'):
            title = (item.findtext('title') or '').strip()
            link = (item.findtext('link') or '').strip()
            pub = (item.findtext('pubDate') or '').strip()
            ts = 0
            if pub:
                try:
                    ts = parsedate_to_datetime(pub).timestamp()
                except Exception:
                    pass
            if title:
                items.append({'title': title, 'link': link, 'source': source,
                              'category': category, 'ts': ts})
    except Exception:
        pass
    return items[:12]


def get_news():
    def build():
        all_items, threads = [], []

        def work(url, src, cat):
            all_items.extend(parse_feed(url, src, cat))

        for url, src, cat in NEWS_FEEDS:
            t = threading.Thread(target=work, args=(url, src, cat))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        # de-dup near-identical titles, newest first
        seen, out = set(), []
        for it in sorted(all_items, key=lambda x: x['ts'], reverse=True):
            key = re.sub(r'\W+', '', it['title'].lower())[:60]
            if key not in seen:
                seen.add(key)
                out.append(it)
        return out[:40]
    return cached('news', 600, build)


# ── world bank gdp (growth % + absolute level in current USD) ────
def _wb_indicator(indicator):
    codes = ';'.join(GDP_COUNTRIES)
    url = (f'https://api.worldbank.org/v2/country/{codes}/indicator/'
           f'{indicator}?format=json&per_page=400&date=2015:2026')
    d = json.loads(fetch(url, timeout=25))
    rows = d[1] if len(d) > 1 and d[1] else []
    by_c = {}
    for r in rows:
        iso = r.get('countryiso3code')
        val = r.get('value')
        if not iso or val is None:
            continue
        by_c.setdefault(iso, {'name': r['country']['value'], 'series': []})
        by_c[iso]['series'].append((int(r['date']), val))
    return by_c


def get_gdp():
    def build():
        res = {}

        def load(key, ind):
            try:
                res[key] = _wb_indicator(ind)
            except Exception:
                res[key] = {}
        t1 = threading.Thread(target=load, args=('g', 'NY.GDP.MKTP.KD.ZG'))
        t2 = threading.Thread(target=load, args=('l', 'NY.GDP.MKTP.CD'))
        t1.start(); t2.start(); t1.join(); t2.join()
        growth = res.get('g') or {}
        level = res.get('l') or {}  # current US$
        if not growth:
            raise RuntimeError('worldbank unavailable')
        out = []
        for iso in GDP_COUNTRIES:
            g = growth.get(iso)
            if not g:
                continue
            series = sorted(g['series'])[-8:]
            lvl = None
            lv = level.get(iso)
            if lv and lv['series']:
                lvl = sorted(lv['series'])[-1][1]  # latest absolute GDP
            out.append({
                'iso': iso, 'name': g['name'],
                'latestYear': series[-1][0],
                'latest': round(series[-1][1], 2),
                'gdpUsd': lvl,
                'series': [{'year': y, 'value': round(v, 2)} for y, v in series],
            })
        out.sort(key=lambda c: -(c['gdpUsd'] or 0))
        return out
    return cached('gdp', 86400, build)


# ── source diagnostics (temporary helper) ───────────────────────
def probe_sources():
    tests = {
        'yahoo_q1_spark': 'https://query1.finance.yahoo.com/v8/finance/spark?symbols=NVDA&range=1d&interval=15m',
        'yahoo_q2_spark': 'https://query2.finance.yahoo.com/v8/finance/spark?symbols=NVDA&range=1d&interval=15m',
        'yahoo_q2_chart': 'https://query2.finance.yahoo.com/v8/finance/chart/NVDA?range=1d&interval=15m',
        'cboe_spx': 'https://cdn.cboe.com/api/global/delayed_quotes/quotes/_SPX.json',
        'cboe_nvda': 'https://cdn.cboe.com/api/global/delayed_quotes/quotes/NVDA.json',
        'coingecko': 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd',
        'frankfurter': 'https://api.frankfurter.dev/v1/latest?base=USD&symbols=CHF',
    }
    out = {}
    for name, url in tests.items():
        try:
            body = fetch(url, timeout=10)
            out[name] = f'OK {len(body)}b: ' + body[:60].decode(errors='replace')
        except Exception as e:
            out[name] = f'FAIL: {str(e)[:80]}'
    return out


# ── http server ─────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        try:
            if path == '/api/markets':
                return self._json(get_markets())
            if path.startswith('/api/theme/'):
                data = get_theme(path.rsplit('/', 1)[-1])
                return self._json(data if data is not None else {'error': 'unknown theme'},
                                  200 if data is not None else 404)
            if path == '/api/themes':
                return self._json(list(THEMES.keys()))
            if path == '/api/movers':
                return self._json(get_movers())
            if path == '/api/news':
                return self._json(get_news())
            if path == '/api/gdp':
                return self._json(get_gdp())
            if path == '/api/debug/sources':
                return self._json(probe_sources())
            if path == '/health':
                return self._json({'status': 'ok'})
            if path in ('/', '/index.html'):
                with open(os.path.join(os.path.dirname(__file__), 'index.html'), 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(body)
                return
            # static assets (icons, manifest) — path-traversal safe
            safe = {'/manifest.webmanifest': 'application/manifest+json'}
            if path in safe or (path.startswith('/icons/') and re.fullmatch(r'/icons/[\w.-]+\.png', path)):
                fpath = os.path.join(os.path.dirname(__file__), path.lstrip('/'))
                if os.path.isfile(fpath):
                    with open(fpath, 'rb') as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', safe.get(path, 'image/png'))
                    self.send_header('Cache-Control', 'public, max-age=86400')
                    self.end_headers()
                    self.wfile.write(body)
                    return
            self._json({'error': 'not found'}, 404)
        except Exception as e:
            self._json({'error': str(e)[:200]}, 502)


if __name__ == '__main__':
    server = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'PULSE listening on :{PORT}')
    server.serve_forever()

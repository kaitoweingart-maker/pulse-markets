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
    ('BTC-USD', 'Bitcoin'), ('^TNX', 'US 10Y Yield'),
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
def _mk_quote(symbol, price, prev, closes):
    chg = ((price - prev) / prev * 100) if (price is not None and prev) else None
    return {
        'symbol': symbol,
        'price': price,
        'prevClose': prev,
        'changePct': round(chg, 2) if chg is not None else None,
        'spark': [c for c in (closes or []) if c is not None][-40:],
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


def quotes_for(pairs):
    quotes = {}
    syms = [s for s, _ in pairs]
    for i in range(0, len(syms), 20):  # chunk to keep URLs sane
        quotes.update(spark_batch(syms[i:i + 20]))
    out = []
    for sym, name in pairs:
        q = quotes.get(sym) or {'symbol': sym, 'price': None,
                                'changePct': None, 'spark': []}
        q['name'] = name
        out.append(q)
    return out


def get_markets():
    return cached('markets', 180, lambda: quotes_for(MARKETS))


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


# ── world bank gdp ──────────────────────────────────────────────
def get_gdp():
    def build():
        codes = ';'.join(GDP_COUNTRIES)
        url = (f'https://api.worldbank.org/v2/country/{codes}/indicator/'
               f'NY.GDP.MKTP.KD.ZG?format=json&per_page=400&date=2015:2026')
        d = json.loads(fetch(url, timeout=15))
        rows = d[1] if len(d) > 1 and d[1] else []
        by_c = {}
        for r in rows:
            iso = r.get('countryiso3code')
            val = r.get('value')
            year = r.get('date')
            if not iso or val is None:
                continue
            by_c.setdefault(iso, {'name': r['country']['value'], 'series': []})
            by_c[iso]['series'].append((int(year), round(val, 2)))
        out = []
        for iso in GDP_COUNTRIES:
            c = by_c.get(iso)
            if not c:
                continue
            series = sorted(c['series'])[-8:]
            out.append({'iso': iso, 'name': c['name'],
                        'latestYear': series[-1][0], 'latest': series[-1][1],
                        'series': [{'year': y, 'value': v} for y, v in series]})
        return out
    return cached('gdp', 86400, build)


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
            self._json({'error': 'not found'}, 404)
        except Exception as e:
            self._json({'error': str(e)[:200]}, 502)


if __name__ == '__main__':
    server = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'PULSE listening on :{PORT}')
    server.serve_forever()

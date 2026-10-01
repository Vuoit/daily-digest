"""News digest: gaming news first, then stock and crypto prices, on one page.

Usage:
    python news_digest.py               # build the page and open it
    python news_digest.py --hours 72    # look further back for news
    python news_digest.py --no-open     # just write news.html

No API keys needed. News comes from the sites' public RSS feeds, stock prices
from Yahoo Finance and crypto prices from CoinGecko. Edit the lists below to
change what you follow.
"""

import argparse
import datetime as dt
import html
import json
import re
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from pathlib import Path

OUTPUT_FILE = Path(__file__).resolve().parent / "news.html"

FEEDS = {
    "IGN":                "https://feeds.feedburner.com/ign/games-all",
    "GameSpot":           "https://www.gamespot.com/feeds/game-news/",
    "PC Gamer":           "https://www.pcgamer.com/rss/",
    "Eurogamer":          "https://www.eurogamer.net/feed/news",
    "Polygon":            "https://www.polygon.com/rss/index.xml",
    "Kotaku":             "https://kotaku.com/rss",
    "Rock Paper Shotgun": "https://www.rockpapershotgun.com/feed/news",
    "VGC":                "https://www.videogameschronicle.com/feed/",
    "Gematsu":            "https://www.gematsu.com/feed",
}
PER_FEED = 20                  # newest N stories kept from each site

# Yahoo Finance symbols: (symbol, display name)
STOCKS = [
    ("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"),
    ("NTDOY", "Nintendo"), ("SONY", "Sony"), ("MSFT", "Microsoft"),
    ("TTWO", "Take-Two"), ("RBLX", "Roblox"), ("NVDA", "Nvidia"),
    ("AMD", "AMD"), ("GME", "GameStop"),
]
# CoinGecko coin ids (the part after /coins/ in a coingecko.com URL)
COINS = ["bitcoin", "ethereum", "solana", "ripple", "binancecoin", "dogecoin", "cardano"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- news

def strip_html(text):
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def clip(text, n=240):
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def find_image(node, *html_bits):
    for tag in ("media:content", "media:thumbnail", "media:group/media:content"):
        for el in node.findall(tag, NS):
            url = el.get("url", "")
            kind = el.get("medium", "") + el.get("type", "")
            if url and (not kind or "image" in kind):
                return url
    for el in node.findall("enclosure"):
        if el.get("type", "").startswith("image") and el.get("url"):
            return el.get("url")
    for bit in html_bits:
        m = re.search(r"<img[^>]+src=[\"']([^\"']+)", bit or "")
        if m:
            return html.unescape(m.group(1))
    return ""


def parse_feed(source, raw):
    root = ET.fromstring(raw)
    stories = []
    for it in root.iter("item"):
        desc = it.findtext("description") or ""
        body = it.findtext("content:encoded", "", NS)
        stories.append({
            "title": strip_html(it.findtext("title")),
            "link": (it.findtext("link") or "").strip(),
            "date": parse_date(it.findtext("pubDate") or it.findtext("dc:date", "", NS)),
            "summary": clip(strip_html(desc) or strip_html(body)),
            "image": find_image(it, desc, body),
        })
    for it in root.iter(f"{{{NS['atom']}}}entry"):
        link = it.find("atom:link[@rel='alternate']", NS)
        if link is None:  # Elements with no children are falsy, so no `or` here
            link = it.find("atom:link", NS)
        summary = it.findtext("atom:summary", "", NS) or it.findtext("atom:content", "", NS)
        stories.append({
            "title": strip_html(it.findtext("atom:title", "", NS)),
            "link": link.get("href", "") if link is not None else "",
            "date": parse_date(it.findtext("atom:published", "", NS)
                               or it.findtext("atom:updated", "", NS)),
            "summary": clip(strip_html(summary)),
            "image": find_image(it, summary),
        })
    stories = [s for s in stories if s["title"] and s["link"]]
    for s in stories:
        s["source"] = source
    stories.sort(key=lambda s: s["date"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                 reverse=True)
    return stories[:PER_FEED]


def fetch_news(hours):
    def one(item):
        name, url = item
        try:
            return name, parse_feed(name, get(url)), None
        except Exception as e:  # noqa: BLE001
            return name, [], str(e)

    with ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
        results = list(pool.map(one, FEEDS.items()))

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    seen, stories, errors = set(), [], []
    for name, got, err in results:
        if err:
            errors.append((name, err))
        for s in got:
            key = re.sub(r"\W+", "", s["title"].lower())
            if key in seen or (s["date"] and s["date"] < cutoff):
                continue
            seen.add(key)
            stories.append(s)
    stories.sort(key=lambda s: s["date"] or cutoff, reverse=True)
    return stories, errors


# ---------------------------------------------------------------- markets

def fetch_stock(symbol, name):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range=1mo&interval=1d")
    res = json.loads(get(url))["chart"]["result"][0]
    meta = res["meta"]
    closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
    price = meta.get("regularMarketPrice") or closes[-1]
    prev = closes[-2] if len(closes) > 1 else meta.get("chartPreviousClose", price)
    first = closes[0] if closes else price
    return {
        "symbol": name if symbol.startswith("^") else symbol,  # indices: ^GSPC -> S&P 500
        "name": "Index" if symbol.startswith("^") else name,
        "index": symbol.startswith("^"),
        "price": price,
        "currency": meta.get("currency", "USD"),
        "change": (price - prev) / prev * 100 if prev else 0,
        "change_long": (price - first) / first * 100 if first else 0,
        "spark": closes,
    }


def fetch_stocks():
    def one(item):
        try:
            return fetch_stock(*item), None
        except Exception as e:  # noqa: BLE001
            return None, (item[1], str(e))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, STOCKS))
    return [r for r, _ in results if r], [e for _, e in results if e]


def fetch_crypto():
    url = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids="
           + ",".join(COINS) + "&sparkline=true&price_change_percentage=24h,7d")
    try:
        data = json.loads(get(url))
    except Exception as e:  # noqa: BLE001
        return [], [("CoinGecko", str(e))]
    order = {c: i for i, c in enumerate(COINS)}
    data.sort(key=lambda c: order.get(c["id"], 99))
    return [{
        "symbol": c["symbol"].upper(),
        "name": c["name"],
        "price": c["current_price"] or 0,
        "currency": "USD",
        "change": c.get("price_change_percentage_24h_in_currency") or 0,
        "change_long": c.get("price_change_percentage_7d_in_currency") or 0,
        "spark": (c.get("sparkline_in_7d") or {}).get("price") or [],
        "icon": c.get("image", ""),
    } for c in data], []


# ---------------------------------------------------------------- output

def fmt_price(p, currency):
    sign = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}.get(currency, "")
    if p >= 1000:
        s = f"{p:,.0f}" if p >= 100_000 else f"{p:,.2f}"
    elif p >= 1:
        s = f"{p:,.2f}"
    else:
        s = f"{p:.4f}"
    return sign + s


def sparkline(values, up):
    if len(values) < 2:
        return ""
    step = max(1, len(values) // 60)
    pts = values[::step] + ([values[-1]] if (len(values) - 1) % step else [])
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1
    w, h = 120, 36
    coords = [(i * w / (len(pts) - 1), h - 2 - (v - lo) / rng * (h - 4)) for i, v in enumerate(pts)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"0,{h} {line} {w},{h}"
    cls = "up" if up else "down"
    return (f'<svg class="spark {cls}" viewBox="0 0 {w} {h}" preserveAspectRatio="none" aria-hidden="true">'
            f'<polygon points="{area}"/><polyline points="{line}"/></svg>')


def pct(v):
    arrow = "▲" if v >= 0 else "▼"
    return f'<span class="pct {"up" if v >= 0 else "down"}">{arrow} {abs(v):.2f}%</span>'


def render_tile(a, long_label):
    e = html.escape
    icon = (f'<img class="coin" src="{e(a["icon"])}" alt="" loading="lazy">' if a.get("icon") else "")
    return f"""
    <div class="tile">
      <div class="tile-head">
        {icon}<div><div class="sym">{e(a['symbol'])}</div><div class="name">{e(a['name'])}</div></div>
        {pct(a['change'])}
      </div>
      <div class="price">{e(fmt_price(a['price'], a['currency']))}</div>
      {sparkline(a['spark'], a['change_long'] >= 0)}
      <div class="long">{long_label} <span class="{'up' if a['change_long'] >= 0 else 'down'}">{a['change_long']:+.2f}%</span></div>
    </div>"""


def render_story(s, lead=False):
    e = html.escape
    iso = s["date"].isoformat() if s["date"] else ""
    img = (f'<div class="thumb"><img src="{e(s["image"])}" alt="" loading="lazy" '
           f'referrerpolicy="no-referrer" onerror="this.parentNode.classList.add(\'noimg\');this.remove()"></div>'
           if s["image"] else '<div class="thumb noimg"></div>')
    text = f"{s['title']} {s['summary']} {s['source']}".lower()
    return f"""
    <a class="story{' lead' if lead else ''}" href="{e(s['link'])}" target="_blank" rel="noopener"
       data-source="{e(s['source'])}" data-text="{e(text)}">
      {img}
      <div class="body">
        <div class="meta"><span class="src">{e(s['source'])}</span><time datetime="{iso}"></time></div>
        <h3>{e(s['title'])}</h3>
        <p>{e(s['summary'])}</p>
      </div>
    </a>"""


def movers_line(assets):
    assets = [a for a in assets if not a.get("index")]
    if not assets:
        return ""
    best = max(assets, key=lambda a: a["change"])
    worst = min(assets, key=lambda a: a["change"])
    e = html.escape
    return (f'<p class="movers">Best <b>{e(best["name"])}</b> {pct(best["change"])} · '
            f'Worst <b>{e(worst["name"])}</b> {pct(worst["change"])}</p>')


def render(stories, stocks, crypto, errors, hours):
    e = html.escape
    sources = sorted({s["source"] for s in stories})
    counts = {src: sum(1 for s in stories if s["source"] == src) for src in sources}
    chips = '<button class="chip on" data-src="">All <b>%d</b></button>' % len(stories) + "".join(
        f'<button class="chip" data-src="{e(src)}">{e(src)} <b>{counts[src]}</b></button>'
        for src in sources)
    lead = next((s for s in stories if s["image"]), None)
    rest = [s for s in stories if s is not lead]
    news_html = (render_story(lead, lead=True) if lead else "") + "".join(render_story(s) for s in rest)
    err_html = "".join(f'<div class="error">Couldn\'t load <b>{e(n)}</b>: {e(m)}</div>' for n, m in errors)
    now = dt.datetime.now()

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>News Digest</title>
<style>
:root {{ color-scheme:dark; --bg:#0e0e12; --card:#17171d; --text:#ececf1; --muted:#9696a3; --line:#26262f;
        --accent:#9d85ff; --accent-soft:#241f3d; --up:#3ddc84; --down:#ff6b6b;
        --up-soft:#133222; --down-soft:#3a1a1a; --err:#3a1f1c; --shadow:none; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text);
       font:15px/1.45 "Segoe UI", system-ui, sans-serif; }}
header {{ position:sticky; top:0; z-index:5; background:color-mix(in srgb, var(--bg) 85%, transparent);
         backdrop-filter:blur(10px); border-bottom:1px solid var(--line); }}
.bar {{ max-width:1180px; margin:0 auto; padding:12px 16px; display:flex; align-items:center; gap:16px; }}
.bar h1 {{ font-size:18px; margin:0; letter-spacing:-.01em; }}
.bar nav {{ display:flex; gap:4px; margin-left:auto; }}
.bar nav a {{ color:var(--muted); text-decoration:none; padding:6px 12px; border-radius:8px; font-weight:600; font-size:14px; }}
.bar nav a:hover {{ background:var(--card); color:var(--text); }}
.stamp {{ color:var(--muted); font-size:13px; }}
main {{ max-width:1180px; margin:0 auto; padding:24px 16px 64px; }}
section {{ scroll-margin-top:64px; }}
h2 {{ font-size:22px; margin:8px 0 14px; letter-spacing:-.01em; }}
h2 small {{ color:var(--muted); font-weight:400; font-size:14px; margin-left:6px; }}
h3.sub {{ font-size:15px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:28px 0 10px; }}
.tools {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:16px; }}
.chips {{ display:flex; flex-wrap:wrap; gap:6px; flex:1; }}
.chip {{ font:inherit; font-size:13px; border:1px solid var(--line); background:var(--card); color:var(--text);
        border-radius:999px; padding:4px 11px; cursor:pointer; }}
.chip b {{ color:var(--muted); font-weight:500; margin-left:2px; }}
.chip.on {{ background:var(--accent); border-color:var(--accent); color:var(--bg); font-weight:600; }}
.chip.on b {{ color:var(--bg); opacity:.7; }}
input[type=search] {{ font:inherit; font-size:14px; padding:7px 12px; border-radius:10px; width:220px;
                     border:1px solid var(--line); background:var(--card); color:var(--text); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(260px, 1fr)); gap:14px; }}
.story {{ display:flex; flex-direction:column; background:var(--card); border:1px solid var(--line);
         border-radius:14px; overflow:hidden; color:inherit; text-decoration:none; box-shadow:var(--shadow);
         transition:transform .15s, border-color .15s; }}
.story:hover {{ transform:translateY(-2px); border-color:var(--accent); }}
.story .thumb {{ aspect-ratio:16/9; background:var(--accent-soft); overflow:hidden; }}
.story .thumb img {{ width:100%; height:100%; object-fit:cover; display:block; }}
.story .thumb.noimg {{ background:linear-gradient(135deg, var(--accent-soft), var(--line)); }}
.story .body {{ padding:12px 14px 14px; }}
.story .meta {{ display:flex; justify-content:space-between; gap:8px; font-size:12px; color:var(--muted); margin-bottom:4px; }}
.story .src {{ color:var(--accent); font-weight:600; }}
.story h3 {{ font-size:15.5px; line-height:1.3; margin:0 0 6px; }}
.story p {{ margin:0; font-size:13.5px; color:var(--muted); display:-webkit-box; -webkit-line-clamp:3;
           -webkit-box-orient:vertical; overflow:hidden; }}
.story.lead {{ grid-column:1 / -1; flex-direction:row; }}
.story.lead .thumb {{ flex:1.4; aspect-ratio:auto; min-height:280px; }}
.story.lead .body {{ flex:1; padding:24px; display:flex; flex-direction:column; justify-content:center; }}
.story.lead h3 {{ font-size:24px; }}
.story.lead p {{ font-size:15px; -webkit-line-clamp:5; }}
.more {{ display:block; margin:18px auto 0; font:inherit; font-weight:600; padding:9px 20px; border-radius:10px;
        border:1px solid var(--line); background:var(--card); color:var(--text); cursor:pointer; }}
.empty {{ color:var(--muted); }}
#markets {{ margin-top:48px; padding-top:8px; border-top:1px solid var(--line); }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr)); gap:12px; }}
.tile {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; box-shadow:var(--shadow); }}
.tile-head {{ display:flex; align-items:flex-start; gap:8px; }}
.tile-head > div {{ flex:1; min-width:0; }}
.coin {{ width:28px; height:28px; border-radius:50%; }}
.sym {{ font-weight:700; }}
.name {{ font-size:12.5px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.price {{ font-size:22px; font-weight:650; margin:8px 0 4px; font-variant-numeric:tabular-nums; }}
.pct {{ font-size:12.5px; font-weight:600; padding:2px 7px; border-radius:6px; white-space:nowrap; font-variant-numeric:tabular-nums; }}
.pct.up {{ color:var(--up); background:var(--up-soft); }}
.pct.down {{ color:var(--down); background:var(--down-soft); }}
.spark {{ width:100%; height:40px; display:block; }}
.spark polyline {{ fill:none; stroke-width:1.6; vector-effect:non-scaling-stroke; }}
.spark.up polyline {{ stroke:var(--up); }} .spark.up polygon {{ fill:var(--up-soft); }}
.spark.down polyline {{ stroke:var(--down); }} .spark.down polygon {{ fill:var(--down-soft); }}
.long {{ font-size:12px; color:var(--muted); margin-top:4px; }}
.long .up {{ color:var(--up); }} .long .down {{ color:var(--down); }}
.movers {{ color:var(--muted); font-size:14px; margin:-4px 0 12px; }}
.movers b {{ color:var(--text); }}
.error {{ background:var(--err); border-radius:8px; padding:8px 12px; margin-bottom:8px; font-size:13px; }}
.note {{ color:var(--muted); font-size:12px; margin-top:24px; }}
@media (max-width:700px) {{
  .stamp {{ display:none; }}
  .story.lead {{ flex-direction:column; }}
  .story.lead .thumb {{ min-height:0; aspect-ratio:16/9; }}
  .story.lead .body {{ padding:14px; }}
  .story.lead h3 {{ font-size:19px; }}
  input[type=search] {{ width:100%; }}
}}
</style></head>
<body>
<header><div class="bar">
  <h1>News Digest</h1>
  <span class="stamp">Updated {now:%a} {now.day} {now:%b}, {now:%H:%M}</span>
  <nav><a href="#gaming">Gaming</a><a href="#markets">Markets</a></nav>
</div></header>
<main>
{err_html}
<section id="gaming">
  <h2>Gaming news <small>last {hours} hours</small></h2>
  <div class="tools">
    <div class="chips">{chips}</div>
    <input type="search" id="q" placeholder="Search headlines…">
  </div>
  <div class="grid" id="news">{news_html or '<p class="empty">No stories found.</p>'}</div>
  <button class="more" id="more" hidden>Show more</button>
</section>

<section id="markets">
  <h2>Markets</h2>
  <h3 class="sub">Stocks</h3>
  {movers_line(stocks)}
  <div class="tiles">{''.join(render_tile(a, '1 month') for a in stocks) or '<p class="empty">Unavailable.</p>'}</div>
  <h3 class="sub">Crypto</h3>
  {movers_line(crypto)}
  <div class="tiles">{''.join(render_tile(a, '7 days') for a in crypto) or '<p class="empty">Unavailable.</p>'}</div>
  <p class="note">Stock badges show change since the previous close; crypto badges show the last 24 hours.
  Prices can be delayed. Not financial advice.</p>
</section>
</main>
<script>
const PAGE = 24;
let shown = PAGE, src = "", q = "";
const cards = [...document.querySelectorAll(".story")];
const more = document.getElementById("more");

function ago(d) {{
  const m = Math.round((Date.now() - d) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return m + "m ago";
  const h = Math.round(m / 60);
  return h < 24 ? h + "h ago" : Math.round(h / 24) + "d ago";
}}
function stamp() {{
  document.querySelectorAll("time[datetime]").forEach(t => {{
    if (t.dateTime) t.textContent = ago(new Date(t.dateTime));
  }});
}}
function apply() {{
  let n = 0;
  cards.forEach(c => {{
    const match = (!src || c.dataset.source === src) && (!q || c.dataset.text.includes(q));
    c.hidden = !match || ++n > shown;
    c.classList.toggle("lead", c === cards[0] && !src && !q);
  }});
  more.hidden = n <= shown;
}}
document.querySelectorAll(".chip").forEach(b => b.onclick = () => {{
  document.querySelectorAll(".chip").forEach(x => x.classList.toggle("on", x === b));
  src = b.dataset.src; shown = PAGE; apply();
}});
document.getElementById("q").oninput = e => {{ q = e.target.value.trim().toLowerCase(); shown = PAGE; apply(); }};
more.onclick = () => {{ shown += PAGE; apply(); }};
stamp(); apply(); setInterval(stamp, 60000);
</script>
</body></html>"""


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Gaming news, then stock and crypto prices, on one page.")
    ap.add_argument("--hours", type=int, default=48, help="how far back to look for news (default 48)")
    ap.add_argument("--no-open", action="store_true", help="don't open the page in a browser")
    args = ap.parse_args()

    print("Fetching gaming news, stocks and crypto...")
    with ThreadPoolExecutor(max_workers=3) as pool:
        news_job = pool.submit(fetch_news, args.hours)
        stocks_job = pool.submit(fetch_stocks)
        crypto_job = pool.submit(fetch_crypto)
        stories, news_err = news_job.result()
        stocks, stock_err = stocks_job.result()
        crypto, crypto_err = crypto_job.result()

    errors = news_err + stock_err + crypto_err
    print(f"  {len(stories)} stories, {len(stocks)} stocks, {len(crypto)} coins")
    for name, msg in errors:
        print(f"  couldn't load {name}: {msg}")

    OUTPUT_FILE.write_text(render(stories, stocks, crypto, errors, args.hours), encoding="utf-8")
    print(f"\nWritten to {OUTPUT_FILE}")
    if not args.no_open:
        webbrowser.open(OUTPUT_FILE.as_uri())


if __name__ == "__main__":
    main()

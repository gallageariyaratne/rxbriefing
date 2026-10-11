"""
RxBriefing build script.

What this does, in order:
  1. Loads the ticker list from tickers.csv
  2. Pulls news from Finnhub (if FINNHUB_API_KEY is set) and from free RSS feeds
  3. Keeps only stories that mention one of our tickers / companies
  4. Tags each story (FDA, Clinical, M&A, Earnings, ...)
  5. Fetches today's % price change for each ticker that appears
  6. Merges with the stories we already saved (data/news.json), keeps the last 10 days
  7. Writes site/data/news.json + site/index.html + feed.xml + sitemap.xml

Run it with:  python build.py
"""

import csv
import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).parent
SITE = ROOT / "site"
DATA_FILE = ROOT / "data" / "news.json"
SITE_URL = os.environ.get("SITE_URL", "https://rxbriefing.com")
SITE_NAME = "RxBriefing"
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
KEEP_DAYS = 10

# Daily email digest window (see write_daily_feed). Times are US Eastern.
DIGEST_SEND_HOUR = 7      # the email goes out at 7:00 AM ET
DIGEST_LEAD_HOURS = 2     # cutoff is 2 h before send; window covers the previous 24 h
MAX_SUMMARY = 240

# Free RSS feeds. Add or remove lines freely.
RSS_FEEDS = [
    ("FDA", "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"),
    ("GlobeNewswire", "https://www.globenewswire.com/RssFeed/subjectcode/13-Health/feedTitle/GlobeNewswire%20-%20Health"),
    ("PR Newswire", "https://www.prnewswire.com/rss/health-latest-news/health-latest-news-list.rss"),
    ("Fierce Biotech", "https://www.fiercebiotech.com/rss/xml"),
    ("Fierce Pharma", "https://www.fiercepharma.com/rss/xml"),
    ("Fierce Healthcare", "https://www.fiercehealthcare.com/rss/xml"),
    ("BioPharma Dive", "https://www.biopharmadive.com/feeds/news/"),
    ("MedTech Dive", "https://www.medtechdive.com/feeds/news/"),
    ("Healthcare Dive", "https://www.healthcaredive.com/feeds/news/"),
    ("STAT", "https://www.statnews.com/feed/"),
    ("Endpoints", "https://endpts.com/feed/"),
]

# Event tags. Order matters: the first rule that matches wins.
TAG_RULES = [
    ("FDA", r"\bfda\b|\bapprov(es|ed|al)\b|\bpdufa\b|complete response letter|\bcrl\b|breakthrough (therapy|designation)|priority review|fast track|orphan drug|\bnda\b|\bbla\b|\bema\b|\bchmp\b|510\(k\)|\bde novo\b|fda clearance|accelerated approval"),
    ("M&A", r"\bacquir(e|es|ed|ing)\b|\bacquisition\b|\bmerger\b|\bbuyout\b|\btakeover\b|\bto buy\b|agrees to be acquired|all-cash deal|\bbid for\b|takes? stake"),
    ("GLP-1", r"glp-1|glp1|\bobesity\b|weight[- ]loss|semaglutide|tirzepatide|zepbound|wegovy|ozempic|mounjaro|orforglipron|retatrutide|cagrisema"),
    ("Clinical", r"\bphase (1|2|3|i|ii|iii)\b|clinical trial|\btrial (data|results|readout)|\breadout\b|\btopline\b|top-line|primary endpoint|\bplacebo\b|\bpivotal\b|interim (data|analysis)|\bdosed\b|first patient"),
    ("Earnings", r"\bearnings\b|\bq[1-4]\b|quarterly (results|revenue|profit)|\brevenue\b|\beps\b|(raises|cuts|lowers|reaffirms|boosts) (guidance|outlook|forecast)|\bbeats\b|\bmisses\b|full-year|fiscal (year|quarter)"),
    ("Analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|initiates coverage|price target|\boverweight\b|\bunderweight\b|\boutperform\b|\bunderperform\b|(buy|sell|hold) rating|analyst"),
    ("Legal", r"\blawsuit\b|\bsues?\b|\bsettle(s|d|ment)\b|\blitigation\b|\bpatent\b|\bcourt\b|\binjunction\b|\bverdict\b|\bdoj\b|\bftc\b|antitrust|\bprobe\b|\bfine[sd]?\b"),
    ("Policy", r"\bmedicare\b|\bmedicaid\b|\bcms\b|\bhhs\b|\btariff|drug pric|most favored nation|\bira\b|\bcongress\b|\bsenate\b|senators?\b|star rating|\brfk\b|kennedy|executive order|\b340b\b|regulator"),
    ("Financing", r"public offering|\bnotes due\b|secured notes|\bconvertible\b|\bipo\b|at-the-market|\bprices? \$|raises \$|\bbanks \$|\bfinancing\b|credit facility|\bseries [a-d]\b|venture|funding round"),
    ("Health AI", r"\bai\b|\bai-|artificial intelligence|machine learning|\balgorithm|\bllm\b|\bgenerative\b|foundation model|\bagentic\b|\bcopilot\b"),
]

SECTOR_LABELS = {
    "pharma": "Pharma", "biotech": "Biotech", "medtech": "Medtech", "tools": "Tools & Dx",
    "payers": "Payers & Providers", "pharmacy": "Pharmacy & Dist.", "healthit": "Health IT",
    "healthai": "Health AI", "etf": "ETFs",
}


# ---------------------------------------------------------------- helpers
def load_tickers():
    out = {}
    with open(ROOT / "tickers.csv", newline="") as f:
        for row in csv.DictReader(f):
            row["ticker"] = row["ticker"].strip().upper()
            row["aliases"] = [a.strip().lower() for a in row["match"].split("|") if a.strip()]
            out[row["ticker"]] = row
    return out


def clean(text, limit=None):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


def parse_date(e):
    """RSS feeds disagree on where the date lives; try everything, fall back to now."""
    from email.utils import parsedate_to_datetime
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        when = e.get(key)
        if when:
            return datetime(*when[:6], tzinfo=timezone.utc).isoformat()
    for key in ("published", "updated", "dc_date", "date", "pubDate"):
        raw = e.get(key)
        if raw:
            try:
                d = parsedate_to_datetime(raw)
            except Exception:
                try:
                    d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except Exception:
                    continue
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def story_id(url, headline):
    return hashlib.sha1((url or headline).encode()).hexdigest()[:12]


def match_tickers(text, tickers):
    """Find tickers whose company name/alias appears in the text."""
    t = " " + text.lower() + " "
    found = []
    for sym, row in tickers.items():
        for alias in row["aliases"]:
            # whole-word match so 'hims' doesn't match 'whims'
            if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", t):
                found.append(sym)
                break
    return found


def tag(headline, summary):
    text = (headline + " " + summary).lower()
    for name, pattern in TAG_RULES:
        if re.search(pattern, text):
            return name
    return "News"


def finnhub(path, **params):
    params["token"] = FINNHUB_KEY
    r = requests.get("https://finnhub.io/api/v1" + path, params=params, timeout=20)
    if r.status_code == 429:  # rate limited, wait and retry once
        time.sleep(20)
        r = requests.get("https://finnhub.io/api/v1" + path, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- fetchers
def fetch_finnhub_news(tickers):
    if not FINNHUB_KEY:
        print("FINNHUB_API_KEY not set — skipping Finnhub news")
        return []
    stories = []
    today = datetime.now(timezone.utc).date()
    frm, to = (today - timedelta(days=2)).isoformat(), today.isoformat()
    # Speed-up: each run queries a rotating slice of the list so a run stays
    # under ~3 minutes on the free tier. Every ticker gets checked at least
    # once per hour (4 runs x 15 min).
    syms = [s for s in tickers if tickers[s]["sector"] != "etf"]
    slot = (datetime.now(timezone.utc).minute // 15) % 4
    batch = [s for i, s in enumerate(syms) if i % 4 == slot]
    print(f"Finnhub: checking {len(batch)} of {len(syms)} tickers (slice {slot+1}/4)")
    for sym in batch:
        try:
            for a in finnhub("/company-news", symbol=sym, **{"from": frm, "to": to}):
                stories.append({
                    "id": story_id(a.get("url"), a.get("headline")),
                    "headline": clean(a.get("headline")),
                    "summary": clean(a.get("summary"), MAX_SUMMARY),
                    "url": a.get("url"),
                    "source": a.get("source") or "Finnhub",
                    "published": datetime.fromtimestamp(a.get("datetime", 0), timezone.utc).isoformat(),
                    "tickers": [sym],
                })
        except Exception as e:
            print(f"  finnhub {sym}: {e}")
        time.sleep(1.05)  # free tier allows 60 calls/minute
    print(f"Finnhub: {len(stories)} stories")
    return stories


def fetch_rss(tickers):
    stories = []
    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "RxBriefing/1.0"})
            for e in feed.entries:
                headline = clean(e.get("title"))
                summary = clean(e.get("summary") or e.get("description"), MAX_SUMMARY)
                published = parse_date(e)
                stories.append({
                    "id": story_id(e.get("link"), headline),
                    "headline": headline,
                    "summary": summary,
                    "url": e.get("link"),
                    "source": source,
                    "published": published,
                    "tickers": match_tickers(headline + " " + summary, tickers),
                })
        except Exception as ex:
            print(f"  rss {source}: {ex}")
    print(f"RSS: {len(stories)} stories")
    return stories


def fetch_quotes(symbols):
    quotes = {}
    if not FINNHUB_KEY:
        return quotes
    for sym in symbols:
        try:
            q = finnhub("/quote", symbol=sym)
            if q.get("c"):
                quotes[sym] = {"price": round(q["c"], 2), "change_pct": round(q.get("dp") or 0, 2)}
        except Exception as e:
            print(f"  quote {sym}: {e}")
        time.sleep(1.05)
    print(f"Quotes: {len(quotes)}")
    return quotes


# ---------------------------------------------------------------- main
def main():
    tickers = load_tickers()
    SITE.mkdir(exist_ok=True)
    (SITE / "data").mkdir(exist_ok=True)
    DATA_FILE.parent.mkdir(exist_ok=True)

    old = {}
    if DATA_FILE.exists():
        old = {s["id"]: s for s in json.loads(DATA_FILE.read_text()).get("stories", [])}

    fresh = fetch_rss(tickers) + fetch_finnhub_news(tickers)

    # Keep only health-relevant stories: those matched to a ticker, or from a
    # health-only source (FDA / Fierce / Dive / STAT / Endpoints) — those are
    # relevant even without a ticker.
    health_sources = {"FDA", "Fierce Biotech", "Fierce Pharma", "Fierce Healthcare",
                      "BioPharma Dive", "MedTech Dive", "Healthcare Dive", "STAT", "Endpoints"}
    merged = dict(old)
    for s in fresh:
        if not s["headline"] or not s["url"]:
            continue
        if not s["tickers"] and s["source"] not in health_sources:
            continue
        if s["id"] in merged:
            merged[s["id"]]["tickers"] = sorted(set(merged[s["id"]]["tickers"]) | set(s["tickers"]))
            continue
        s["tag"] = tag(s["headline"], s["summary"])
        s["sectors"] = sorted({tickers[t]["sector"] for t in s["tickers"] if t in tickers})
        merged[s["id"]] = s

    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    stories = sorted((s for s in merged.values() if s["published"] >= cutoff),
                     key=lambda s: s["published"], reverse=True)

    # Quotes only for tickers seen in the last 24h (keeps API calls low)
    recent_cut = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    recent_syms = sorted({t for s in stories if s["published"] >= recent_cut for t in s["tickers"]})
    quotes = fetch_quotes(recent_syms[:80])

    payload = {
        "site": SITE_NAME,
        "updated": datetime.now(timezone.utc).isoformat(),
        "quotes": quotes,
        "tickers": {k: {"name": v["name"], "sector": v["sector"]} for k, v in tickers.items()},
        "sector_labels": SECTOR_LABELS,
        "stories": stories,
    }
    DATA_FILE.write_text(json.dumps(payload, indent=1))
    (SITE / "data" / "news.json").write_text(json.dumps(payload))
    write_feed(stories)
    write_daily_feed(stories, quotes)
    write_sitemap()
    print(f"Done: {len(stories)} stories kept")


def write_feed(stories):
    items = "".join(
        f"<item><title>{html.escape(s['headline'])}</title><link>{html.escape(s['url'])}</link>"
        f"<guid>{s['id']}</guid><pubDate>{datetime.fromisoformat(s['published']).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
        f"<description>{html.escape(s['summary'])}</description></item>"
        for s in stories[:50])
    (SITE / "feed.xml").write_text(
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>{SITE_NAME}</title>'
        f'<link>{SITE_URL}</link><description>Pharma, biotech, medtech and health AI stock news</description>{items}</channel></rss>')


def write_daily_feed(stories, quotes):
    """Builds site/daily.xml: only stories inside the digest window.

    Window = [cutoff - 24h, cutoff], where cutoff = today's send time minus
    DIGEST_LEAD_HOURS. Before the cutoff on a given day the window still
    points at the previous day, so whenever the email tool fetches the feed
    in the morning it gets exactly 'yesterday'.
    """
    et = ZoneInfo("America/New_York")
    now = datetime.now(et)
    cutoff = now.replace(hour=DIGEST_SEND_HOUR - DIGEST_LEAD_HOURS, minute=0, second=0, microsecond=0)
    if now < cutoff:
        cutoff -= timedelta(days=1)
    start = cutoff - timedelta(days=1)
    s_iso, c_iso = start.astimezone(timezone.utc).isoformat(), cutoff.astimezone(timezone.utc).isoformat()
    picked = [s for s in stories if s_iso <= s["published"] < c_iso]

    # Order: tagged events first (FDA, M&A, Clinical...) then the rest; cap at 40.
    priority = {"FDA": 0, "M&A": 1, "Clinical": 2, "GLP-1": 3, "Earnings": 4, "Policy": 5,
                "Analyst": 6, "Health AI": 7, "Financing": 8, "Legal": 9, "News": 10}
    picked.sort(key=lambda s: (priority.get(s["tag"], 10), s["published"]), reverse=False)
    picked = picked[:40]

    day_label = start.strftime("%A, %B %d, %Y")
    movers = sorted(((k, v["change_pct"]) for k, v in quotes.items()), key=lambda kv: -abs(kv[1]))[:8]
    movers_txt = "  ·  ".join(f"{k} {v:+.2f}%" for k, v in movers)
    items = "".join(
        f"<item><title>[{html.escape(s['tag'])}] {html.escape(s['headline'])}</title><link>{html.escape(s['url'])}</link>"
        f"<guid>{s['id']}</guid><pubDate>{datetime.fromisoformat(s['published']).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
        f"<description>{html.escape(', '.join(s['tickers']) + ' — ' if s['tickers'] else '')}{html.escape(s['summary'] or s['source'])}</description></item>"
        for s in picked)
    (SITE / "daily.xml").write_text(
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>{SITE_NAME} Daily — {day_label}</title>'
        f'<link>{SITE_URL}</link><description>Movers: {html.escape(movers_txt)}. {len(picked)} healthcare stories from the last trading day.</description>{items}</channel></rss>')
    print(f"Daily digest: {len(picked)} stories ({start:%m/%d %I%p} → {cutoff:%m/%d %I%p} ET)")


def write_sitemap():
    urls = [f"<url><loc>{SITE_URL}/</loc><changefreq>hourly</changefreq></url>",
            f"<url><loc>{SITE_URL}/pipeline/</loc><changefreq>monthly</changefreq></url>"]
    for d in sorted((SITE / "company").glob("*/index.html")) if (SITE / "company").exists() else []:
        urls.append(f"<url><loc>{SITE_URL}/company/{d.parent.name}/</loc><changefreq>daily</changefreq></url>")
    (SITE / "sitemap.xml").write_text(
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(urls) + "</urlset>")
    (SITE / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n")


if __name__ == "__main__":
    main()

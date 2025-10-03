import os, time, random, requests, json, re, unicodedata
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlparse
from html import escape, unescape
import feedparser
from dotenv import load_dotenv
from requests.exceptions import ReadTimeout, HTTPError, RequestException

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WP_BASE_URL    = os.getenv("WP_BASE_URL")
WP_USER        = os.getenv("WP_USER")
WP_APP_PASS    = os.getenv("WP_APP_PASS")
MAX_TRENDS     = int(os.getenv("MAX_TRENDS", "8"))  # gärna 8 för att rymma alla kvoter

# YouTube är VALFRITT. Fyll i YT_API_KEY i Render om du vill använda.
YT_API_KEY     = os.getenv("YT_API_KEY", "").strip()
YT_REGION      = os.getenv("YT_REGION", "SE").strip() or "SE"

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# ----- Konfig: kategorier, kvoter, queries -----
# Prioritetsordning: säkerställer att VIRALT & UNDERHÅLLNING alltid tas med
CATEGORIES = [
    {"slug": "viralt-trend",   "name": "Viralt & Trendord", "query": "tiktok OR viralt OR meme OR trend OR hashtag"},
    {"slug": "underhallning",  "name": "Underhållning",     "query": "film OR serie OR streaming OR musik OR kändis OR influencer"},
    {"slug": "sport",          "name": "Sport",             "query": "Allsvenskan OR SHL OR Premier League Sverige OR Champions League Sverige OR landslaget"},
    {"slug": "prylradar",      "name": "Prylradar",         "query": "lansering OR släpper OR release OR uppdatering OR recension teknik pryl gadget"},
    {"slug": "teknik-prylar",  "name": "Teknik & Prylar",   "query": "smartphone OR lansering OR 'ny mobil' OR pryl OR teknik"},
    {"slug": "ekonomi-bors",   "name": "Ekonomi & Börs",    "query": "börsen OR aktier OR inflation OR ränta OR Riksbanken"},
    {"slug": "nyheter",        "name": "Nyheter",           "query": "Sverige"},
    {"slug": "gaming-esport",  "name": "Gaming & e-sport",  "query": "gaming OR e-sport OR playstation OR xbox OR nintendo OR steam"},
]

# hur många ämnen per kategori (summa ≈ MAX_TRENDS)
CATEGORY_QUOTA = {
    "viralt-trend": 1,
    "underhallning": 1,
    "sport": 1,
    "prylradar": 1,
    "teknik-prylar": 1,
    "ekonomi-bors": 1,
    "nyheter": 1,
    "gaming-esport": 1,
}

SPORT_QUERIES = [
    "Allsvenskan", "SHL", "Premier League Sverige", "Champions League Sverige",
    "Damallsvenskan", "Landslaget fotboll", "Tre Kronor"
]

PRYL_QUERIES = [
    "lansering smartphone","\"ny mobil\"","iPhone lansering","Samsung släpper",
    "smartwatch lansering","AI-kamera lansering","RTX grafikkort","Playstation uppdatering",
]

PRYL_FEEDS = [
    "https://www.gsmarena.com/rss-news-reviews.php3",
    "https://www.theverge.com/rss/index.xml",
    "https://www.engadget.com/rss.xml",
    "https://www.techradar.com/rss",
]

# ----- Datum/recency helpers -----
def _to_aware_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

def parse_entry_dt(entry) -> datetime | None:
    # feedparser ger ibland published_parsed (struct_time)
    if getattr(entry, "published_parsed", None):
        try:
            dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    # annars free text
    for k in ("published", "updated", "pubDate"):
        s = getattr(entry, k, None) or (entry.get(k) if isinstance(entry, dict) else None)
        if s:
            try:
                # grov parse – feedparser har redan gjort sitt bästa; fallback, anta UTC
                return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                continue
    return None

def is_recent(dt: datetime | None, max_age_hours=48) -> bool:
    if not dt:
        return False
    return (_to_aware_utc(datetime.now(timezone.utc)) - _to_aware_utc(dt)) <= timedelta(hours=max_age_hours)

# ----- String normalisering (dubblettskydd) -----
def normalize_title_key(s: str) -> str:
    # NFKD, ta bort accenter, ersätt typografiska tecken, ta bort icke-alfanumeriskt
    s = s.strip().lower()
    repl = {
        "’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "-": "-", "–": "-",
    }
    for a,b in repl.items():
        s = s.replace(a,b)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    s = re.sub(r"\s+", " ", s)
    return s.strip()

# ----- RSS / API helpers -----
def fetch_rss(url):
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=15)
        r.raise_for_status()
        return feedparser.parse(r.text)
    except Exception as e:
        print("⚠️ RSS-fel på", url, "→", e)
        return feedparser.FeedParserDict(entries=[])

def gnews_recent_titles(query, max_items=6, max_age_hours=48):
    # använd when:2d + filtrera på published
    q = f"{query} when:2d"
    url = f"https://news.google.com/rss/search?q={quote(q)}&hl=sv-SE&gl=SE&ceid=SE:sv"
    feed = fetch_rss(url)
    titles = []
    for e in (feed.entries or []):
        dt = parse_entry_dt(e)
        if not is_recent(dt, max_age_hours=max_age_hours):
            continue
        titles.append(e.title)
        if len(titles) >= max_items:
            break
    return titles

def gnews_snippets_sv(query, max_items=3, max_age_hours=72):
    q = f"{query} when:3d"
    url = f"https://news.google.com/rss/search?q={quote(q)}&hl=sv-SE&gl=SE&ceid=SE:sv"
    feed = fetch_rss(url)
    items = []
    for entry in (feed.entries or []):
        dt = parse_entry_dt(entry)
        if not is_recent(dt, max_age_hours=max_age_hours):
            continue
        items.append({"title": entry.title, "link": entry.link, "published": entry.get("published", "")})
        if len(items) >= max_items:
            break
    return items

def prylradar_items(max_items=12, max_age_days=14):
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    # Feeds
    for u in PRYL_FEEDS:
        feed = fetch_rss(u)
        for e in (feed.entries or []):
            dt = parse_entry_dt(e)
            if dt and dt >= cutoff:
                items.append((e.title, getattr(e, "link", u)))
            if len(items) >= max_items:
                break
        if len(items) >= max_items: break
    # GNews queries
    for q in PRYL_QUERIES:
        for t in gnews_recent_titles(q, max_items=4, max_age_hours=max_age_days*24):
            items.append((t, ""))  # origin okänd via gnews
            if len(items) >= max_items: break
        if len(items) >= max_items: break
    return items[:max_items]

def wiki_top_sv(limit=10):
    today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    url = f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/sv.wikipedia/all-access/{today}"
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])
        if not items: return []
        articles = items[0].get("articles", [])
        res = []
        for a in articles:
            title = a.get("article","").replace("_"," ")
            if not title or title.startswith("Special:") or title.startswith("Huvudsida"):
                continue
            res.append(title)
            if len(res) >= limit: break
        return res
    except Exception as e:
        print("⚠️ wiki_top_sv fel:", e)
        return []

def reddit_top_sweden(limit=10):
    try:
        url = "https://www.reddit.com/r/sweden/top/.json?t=day&limit=20"
        r = requests.get(url, headers={"User-Agent": UA_HEADERS["User-Agent"]}, timeout=15)
        r.raise_for_status()
        js = r.json()
        titles = []
        for c in js.get("data",{}).get("children",[]):
            t = c.get("data",{}).get("title","").strip()
            if t: titles.append(t)
        return titles[:limit]
    except Exception as e:
        print("⚠️ reddit_top_sweden fel:", e)
        return []

def youtube_trending_titles(limit=10):
    if not YT_API_KEY:
        return []
    try:
        url = (
            "https://www.googleapis.com/youtube/v3/videos"
            f"?part=snippet&chart=mostPopular&regionCode={quote(YT_REGION)}&maxResults={min(limit,50)}&key={quote(YT_API_KEY)}"
        )
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        items = r.json().get("items", [])
        titles = [it["snippet"]["title"] for it in items if "snippet" in it]
        return titles[:limit]
    except Exception as e:
        print("⚠️ youtube_trending_titles fel:", e)
        return []

# ----- Text helpers -----
def clean_topic_title(t: str) -> str:
    t = t.strip()
    t = re.sub(r'^(JUST NU:|DN Direkt\s*-\s*|LIVE:)\s*', '', t, flags=re.I)
    t = re.sub(r'\s+[–-]\s+[^\-–—|:]{2,}$', '', t).strip()
    return t

def swedishify_title_if_needed(title: str) -> str:
    t = title.strip()
    repl = {"update":"uppdatering","rollout":"utrullning","stable":"stabil","launch":"lansering","review":"recension"}
    for k,v in repl.items():
        t = re.sub(rf'\b{k}\b', v, t, flags=re.I)
    t = re.sub(r'\b(now|just)\s+rolling\s+out\b', 'utrullas nu', t, flags=re.I)
    t = clean_topic_title(t)
    return t

def make_excerpt(raw_text: str, max_chars=160) -> str:
    if not raw_text: return ""
    parts = [p.strip() for p in re.split(r'[.!?]\s+', raw_text) if p.strip()]
    for p in parts:
        if not p.startswith("-") and not p.lower().startswith("affiliate-idéer"):
            excerpt = p; break
    else:
        excerpt = parts[0] if parts else raw_text
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars].rsplit(" ", 1)[0] + "…"
    return excerpt

def text_to_html(txt: str) -> str:
    lines = [l.strip() for l in (txt or '').splitlines() if l.strip()]
    parts, bullets = [], []
    def flush_bullets():
        nonlocal bullets
        if bullets:
            parts.append("<ul>" + "".join(f"<li>{escape(b)}</li>" for b in bullets) + "</ul>")
            bullets.clear()
    for l in lines:
        if l.startswith("- "): bullets.append(l[2:].strip())
        else:
            flush_bullets(); parts.append(f"<p>{escape(l)}</p>")
    flush_bullets()
    return "\n".join(parts) if parts else "<p></p>"

# ----- OpenAI (GPT-5) -----
def openai_chat_summarize(topic, snippets, model="gpt-5"):
    system = (
        "Skriv på svensk nyhetsprosa. 110–150 ord. Ingen rubrik.\n"
        "Struktur:\n"
        "- Detta har hänt: 1–2 meningar (konkret vad/när/var).\n"
        "- Varför det spelar roll: 1–2 meningar (påverkan/siffror om möjligt).\n"
        "- Vad händer härnäst: 1 mening (besked/datum/nästa steg).\n"
        "Lägg sedan 2–3 korta punkter som börjar med '- ' och sammanfattar viktigaste faktan.\n"
        "Avsluta med: 'Affiliate-idéer:' och 1–2 punkter som börjar med '- '.\n"
        "Undvik klichéer. Var specifik. Ingen markdown."
    )
    snip = "; ".join([f"{s['title']} ({s['link']})" for s in snippets]) if snippets else "Inga källsnuttar"
    payload = {"model": model,"messages": [{"role":"system","content":system},{"role":"user","content": f"Ämne: {topic}\nNyhetssnuttar: {snip}"}]}
    resp = requests.post("https://api.openai.com/v1/chat/completions",
                         headers={"Authorization": f"Bearer {OPENAI_API_KEY}","Content-Type": "application/json"},
                         json=payload, timeout=60)
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        print("OpenAI response text:", (resp.text or "")[:800]); raise
    j = resp.json()
    return j["choices"][0]["message"]["content"].strip()

def summarize_with_retries(topic, snippets):
    models = ["gpt-5", "gpt-5-mini"]
    for model in models:
        for attempt in range(2):
            try:
                return openai_chat_summarize(topic, snippets, model=model)
            except ReadTimeout:
                wait = 2 ** attempt; print(f"⏳ OpenAI timeout ({model}) – försöker igen om {wait}s..."); time.sleep(wait); continue
            except HTTPError as e:
                print("OpenAI HTTPError:", e); break
            except RequestException as e:
                print("OpenAI RequestException:", e); break
            except Exception as e:
                print("OpenAI annat fel:", e); break
    raise Exception("Alla modellförsök misslyckades")

# ----- WordPress helpers -----
def wp_post_trend(title, body, topics=None, categories=None, excerpt=""):
    url = f"{WP_BASE_URL}/wp-json/trendkollen/v1/ingest"
    payload = {"title": title,"content": body,"excerpt": excerpt,"topics": topics or [],"categories": categories or []}
    resp = requests.post(url, json=payload, auth=(WP_USER, WP_APP_PASS), timeout=30)
    resp.raise_for_status()
    return resp.json()

def wp_trend_exists_exact(title, within_hours=24):
    try:
        url = f"{WP_BASE_URL}/wp-json/wp/v2/trend?search={quote(title)}&per_page=10&orderby=date&order=desc"
        resp = requests.get(url, auth=(WP_USER, WP_APP_PASS), timeout=20)
        resp.raise_for_status()
        posts = resp.json()
    except Exception as e:
        print("⚠️ Kunde inte läsa WP-lista för duplikat:", e); return False

    def _parse_wp_dt(p):
        raw_gmt = p.get("date_gmt") or ""; raw_loc = p.get("date") or ""
        for s in (raw_gmt, raw_loc):
            if not s: continue
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
                return dt
            except Exception: continue
        return datetime.now(timezone.utc)

    now = datetime.now(timezone.utc)
    want_key = normalize_title_key(title)
    for p in posts:
        rendered = unescape(p.get("title", {}).get("rendered", "")).strip()
        have_key = normalize_title_key(rendered)
        if have_key == want_key:
            dt = _parse_wp_dt(p)
            if (now - dt) <= timedelta(hours=within_hours): return True
    return False

# ----- Urval: mix per kategori -----
def pick_diverse_topics(max_total):
    print(f"▶ YouTube {'ON' if YT_API_KEY else 'OFF'} (region {YT_REGION})")
    seen_keys = set()
    picked = []
    for cat in CATEGORIES:
        quota = CATEGORY_QUOTA.get(cat["slug"], 0)
        if quota <= 0: continue

        titles_pool = []  # kan vara str eller (title, origin)
        if cat["slug"] == "sport":
            for q in SPORT_QUERIES:
                titles_pool.extend(gnews_recent_titles(q, max_items=4, max_age_hours=72))
        elif cat["slug"] == "prylradar":
            titles_pool.extend(prylradar_items(max_items=12, max_age_days=14))  # (title, origin)
        elif cat["slug"] == "viralt-trend":
            wiki = wiki_top_sv(limit=10)
            reddit = reddit_top_sweden(limit=10)
            yt = youtube_trending_titles(limit=10)
            print(f"▶ Viralt pool: wiki={len(wiki)} reddit={len(reddit)} youtube={len(yt)}")
            pool = []
            pool.extend(wiki); pool.extend(reddit); pool.extend(yt)
            titles_pool.extend(pool)
        else:
            titles_pool.extend(gnews_recent_titles(cat["query"], max_items=10, max_age_hours=48))

        count = 0
        for t in titles_pool:
            if len(picked) >= max_total: break
            if isinstance(t, tuple):
                raw_title, origin = t
            else:
                raw_title, origin = t, ""
            clean = clean_topic_title(raw_title)
            if cat["slug"] in ("prylradar","teknik-prylar"):
                clean = swedishify_title_if_needed(clean)
            key = normalize_title_key(clean)
            if not clean or key in seen_keys: continue
            picked.append({"title": clean, "cat_slug": cat["slug"], "cat_name": cat["name"], "origin": origin})
            seen_keys.add(key)
            count += 1
            if count >= quota: break

        if len(picked) >= max_total: break

    # Fyll på från Nyheter om vi saknar ämnen
    if len(picked) < max_total:
        extra = gnews_recent_titles("Sverige", max_items=24, max_age_hours=48)
        for t in extra:
            if len(picked) >= max_total: break
            clean = clean_topic_title(t)
            key = normalize_title_key(clean)
            if clean and key not in seen_keys:
                picked.append({"title": clean, "cat_slug": "nyheter", "cat_name": "Nyheter", "origin": ""})
                seen_keys.add(key)
    return picked

# ----- Main -----
def main():
    print("🔎 Startar Trendkoll-worker..."); print("BASE_URL:", WP_BASE_URL, "| USER:", WP_USER)

    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bundles = pick_diverse_topics(max_total=MAX_TRENDS)
    if not bundles:
        print("⚠️ Hittade inga topics. Avbryter."); return

    posted_now_keys = set()

    for b in bundles:
        title = b["title"]; cat = b["cat_slug"]; origin = b.get("origin") or ""
        key = normalize_title_key(title)
        print(f"➡️  [{cat}] {title}")

        if key in posted_now_keys:
            print("⏭️ Hoppar över (dubblett i samma körning)."); continue
        if wp_trend_exists_exact(title, within_hours=24):
            print("⏭️ Hoppar över (fanns redan senaste 24h i WP)."); continue

        snippets = gnews_snippets_sv(title, max_items=4, max_age_hours=72)
        if not snippets and origin:
            dom = urlparse(origin).netloc or "Källa"
            snippets = [{"title": dom, "link": origin, "published": ""}]

        # GPT-5 → 5-mini → no-AI fallback
        try:
            raw_summary = summarize_with_retries(title, snippets)
        except Exception as e2:
            print("❌ OpenAI-fel, kör no-AI fallback:", e2)
            bullets = "\n".join([f"- {s['title']}" for s in snippets[:3]]) if snippets else "- Ingen nyhetskälla tillgänglig"
            raw_summary = f"{bullets}\n\nAffiliate-idéer:\n- Sök efter relaterade produkter/tjänster hos dina partnernätverk."

        summary_html = text_to_html(raw_summary)
        published_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
        source_items = "".join([f"<li><a href='{s['link']}' target='_blank' rel='nofollow noopener'>{escape(s['title'])}</a></li>" for s in snippets]) if snippets else ""
        source_header = "<h3>Källor</h3>" if len(snippets) != 1 else "<h3>Källa</h3>"
        sources_html  = f"{source_header}\n<ul>{source_items or '<li>(Inga källor tillgängliga just nu)</li>'}</ul>"

        body = f"""
        <p><em>Publicerad: {published_str} UTC</em></p>
        <div class='tk-summary'>
{summary_html}
        </div>
        {sources_html}
        """

        try:
            res = wp_post_trend(
                title=title,
                body=body,
                topics=["idag", "svenska-trender", date_tag],
                categories=[cat],
                excerpt=make_excerpt(raw_summary, max_chars=160)
            )
            posted_now_keys.add(key); print("✅ Postad:", res)
            time.sleep(random.uniform(0.8, 1.6))
        except Exception as e:
            print("❌ Fel vid postning till WP:", e)

    print("🏁 Klar körning.")

if __name__ == "__main__":
    main()

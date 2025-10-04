import os, time, random, requests, re, unicodedata, hashlib, math
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlparse
from html import escape, unescape
import feedparser
from dotenv import load_dotenv
from requests.exceptions import ReadTimeout, HTTPError, RequestException
from PIL import Image, ImageDraw, ImageFont  # Pillow för bildgenerering

load_dotenv()

# === ENV ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WP_BASE_URL    = os.getenv("WP_BASE_URL")
WP_USER        = os.getenv("WP_USER")
WP_APP_PASS    = os.getenv("WP_APP_PASS")
MAX_TRENDS     = int(os.getenv("MAX_TRENDS", "8"))

YT_API_KEY     = os.getenv("YT_API_KEY", "").strip()
YT_REGION      = os.getenv("YT_REGION", "SE").strip() or "SE"

FONT_REG_PATH  = os.getenv("FONT_REG_PATH", "assets/fonts/Inter-Regular.ttf")
FONT_BOLD_PATH = os.getenv("FONT_BOLD_PATH","assets/fonts/Inter-Bold.ttf")

UA_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

# === Kategorier & kvoter ===
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
CATEGORY_QUOTA = {
    "viralt-trend": 1, "underhallning": 1, "sport": 1, "prylradar": 1,
    "teknik-prylar": 1, "ekonomi-bors": 1, "nyheter": 1, "gaming-esport": 1,
}

# === Svenska pryl/teknik-feeds (prioriteras) + internationella fallback ===
PRYL_FEEDS_SV = [
    "https://www.sweclockers.com/feeds/nyheter",
    "https://feber.se/rss/teknik/",
    "https://feber.se/rss/pryl/",
    "https://m3.idg.se/rss.xml",
    "https://www.mobil.se/rss.xml",
    "https://surfa.se/feed/",
    "https://www.nyteknik.se/rss/",
]
PRYL_FEEDS_INT = [
    "https://www.gsmarena.com/rss-news-reviews.php3",
    "https://www.theverge.com/rss/index.xml",
    "https://www.engadget.com/rss.xml",
    "https://www.techradar.com/rss",
]

# (enkla queries som backas av GNews)
SPORT_QUERIES = [
    "Allsvenskan", "SHL", "Damallsvenskan", "Tre Kronor", "Sveriges landslag fotboll", "Premier League Sverige",
    "Champions League Sverige"
]
PRYL_QUERIES = [
    "lansering smartphone", "\"ny mobil\"", "iPhone lansering", "Samsung släpper", "smartwatch lansering",
    "AI-kamera lansering", "RTX grafikkort", "Playstation uppdatering"
]

# === Färger för kategorier (för bilder) ===
CAT_COLORS = {
    "viralt-trend":   ("#ff7a00", "#d60b52"),
    "underhallning":  ("#7a5cff", "#2bb0ff"),
    "sport":          ("#00b140", "#006837"),
    "prylradar":      ("#ff4d4f", "#ff7a45"),
    "teknik-prylar":  ("#2b3a67", "#0ea5e9"),
    "ekonomi-bors":   ("#1f2937", "#10b981"),
    "nyheter":        ("#111827", "#374151"),
    "gaming-esport":  ("#7c3aed", "#22d3ee"),
}

# === Hjälpare: tid, normalisering ===
def _to_aware_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

def parse_entry_dt(entry):
    if getattr(entry, "published_parsed", None):
        try:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        except Exception:
            pass
    for k in ("published","updated","pubDate"):
        s = getattr(entry,k,None) or (entry.get(k) if isinstance(entry,dict) else None)
        if s:
            try:
                return datetime.fromisoformat(s.replace("Z","+00:00")).astimezone(timezone.utc)
            except Exception:
                continue
    return None

def is_recent(dt, max_age_hours=48):
    if not dt: return False
    return (_to_aware_utc(datetime.now(timezone.utc)) - _to_aware_utc(dt)) <= timedelta(hours=max_age_hours)

def normalize_title_key(s: str) -> str:
    s = s.strip().lower()
    for a,b in {"’":"'", "‘":"'", "“":'"', "”":'"', "–":"-", "—":"-"}.items():
        s = s.replace(a,b)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    return re.sub(r"\s+"," ",s).strip()

# === RSS/APIs ===
def fetch_rss(url):
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=15)
        r.raise_for_status()
        return feedparser.parse(r.text)
    except Exception as e:
        print("⚠️ RSS-fel på", url, "→", e)
        return feedparser.FeedParserDict(entries=[])

def gnews_recent_titles(query, max_items=6, max_age_hours=48):
    q = f"{query} when:2d"
    url = f"https://news.google.com/rss/search?q={quote(q)}&hl=sv-SE&gl=SE&ceid=SE:sv"
    feed = fetch_rss(url)
    titles = []
    for e in (feed.entries or []):
        if is_recent(parse_entry_dt(e), max_age_hours=max_age_hours):
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
        if is_recent(parse_entry_dt(entry), max_age_hours=max_age_hours):
            items.append({"title": entry.title, "link": entry.link, "published": entry.get("published","")})
            if len(items) >= max_items:
                break
    return items

# === Wikipedia: idag → igår → i förrgår, och filtrera bort meta-sidor ===
def wiki_top_sv(limit=10):
    META_PREFIXES = ("Special:", "Huvudsida", "Portal:", "Wikipedia:", "Mall:", "Kategori:", "Diskussion:", "Användare:", "Fil:", "Wikidata:")
    for back in [0,1,2]:
        date_str = (datetime.now(timezone.utc) - timedelta(days=back)).strftime("%Y/%m/%d")
        url = f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/sv.wikipedia/all-access/{date_str}"
        try:
            r = requests.get(url, headers=UA_HEADERS, timeout=15)
            r.raise_for_status()
            items = r.json().get("items", [])
            if not items: 
                continue
            arts = items[0].get("articles", [])
            res = []
            for a in arts:
                title = a.get("article","").replace("_"," ")
                if not title or title.startswith(META_PREFIXES):
                    continue
                res.append(title)
                if len(res) >= limit:
                    break
            if res:
                if back>0: print(f"▶ wiki fallback: -{back}d ({len(res)} träffar)")
                return res
        except Exception as e:
            if back==0: print("⚠️ wiki_top_sv fel:", e)
            continue
    return []

# === Reddit: JSON → RSS fallback (hanterar 403) ===
def reddit_top_sweden(limit=10):
    url_json = "https://www.reddit.com/r/sweden/top/.json?t=day&limit=20"
    try:
        r = requests.get(url_json, headers={"User-Agent": UA_HEADERS["User-Agent"]}, timeout=15)
        r.raise_for_status()
        titles = []
        for c in r.json().get("data",{}).get("children",[]):
            t = c.get("data",{}).get("title","").strip()
            if t: titles.append(t)
        return titles[:limit]
    except Exception as e:
        print("⚠️ reddit_top_sweden fel:", e)

    try:
        feed = fetch_rss("https://www.reddit.com/r/sweden/top/.rss?t=day&limit=20")
        titles = [e.title for e in (feed.entries or [])]
        if titles:
            print(f"▶ reddit fallback via RSS: {len(titles)} titlar")
        return titles[:limit]
    except Exception as e2:
        print("⚠️ reddit RSS fallback fel:", e2)
        return []

# === YouTube trending (valfritt) ===
def youtube_trending_titles(limit=10):
    if not YT_API_KEY:
        return []
    try:
        url = ("https://www.googleapis.com/youtube/v3/videos"
               f"?part=snippet&chart=mostPopular&regionCode={quote(YT_REGION)}"
               f"&maxResults={min(limit,50)}&key={quote(YT_API_KEY)}")
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        items = r.json().get("items", [])
        return [it["snippet"]["title"] for it in items if "snippet" in it][:limit]
    except Exception as e:
        print("⚠️ youtube_trending_titles fel:", e)
        return []

# === Samling av feedtitlar (med åldersfilter) ===
def feed_titles(feed_urls, max_items=20, max_age_days=14):
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    for u in feed_urls:
        feed = fetch_rss(u)
        for e in (feed.entries or []):
            dt = parse_entry_dt(e)
            if not dt or dt < cutoff:
                continue
            items.append((e.title, getattr(e, "link", u)))
            if len(items) >= max_items:
                break
        if len(items) >= max_items:
            break
    return items

# === Prylradar: svenska först, GNews .se-filter, sedan internationell fallback ===
def prylradar_items(max_items=12, max_age_days=14):
    items = []
    items.extend(feed_titles(PRYL_FEEDS_SV, max_items=max_items, max_age_days=max_age_days))

    if len(items) < max_items:
        for q in PRYL_QUERIES:
            for domain in ["site:surfa.se", "site:m3.idg.se", "site:mobil.se", "site:sweclockers.com",
                           "site:feber.se", "site:nyteknik.se"]:
                ts = gnews_recent_titles(f"{q} {domain}", max_items=3, max_age_hours=max_age_days*24)
                for t in ts:
                    items.append((t, ""))  # ok utan origin-länk
                    if len(items) >= max_items:
                        break
                if len(items) >= max_items:
                    break
            if len(items) >= max_items:
                break

    if len(items) < max_items:
        items.extend(feed_titles(PRYL_FEEDS_INT, max_items=max_items - len(items), max_age_days=max_age_days))

    return items[:max_items]

# === Rubrikstädning + "svenskifiering" ===
def clean_topic_title(t: str) -> str:
    t = t.strip()
    t = re.sub(r'^(JUST NU:|DN Direkt\s*-\s*|LIVE:)\s*', '', t, flags=re.I)
    t = re.sub(r'\s+[–-]\s+[^\-–—|:]{2,}$', '', t).strip()
    return t

def swedishify_title_if_needed(title: str) -> str:
    t = title.strip()
    repl = {
        r"\bupdate\b": "uppdatering",
        r"\breview\b": "recension",
        r"\blaunch\b": "lansering",
        r"\brollout\b": "utrullning",
        r"\bstable\b": "stabil",
        r"\bnow rolling out\b": "utrullas nu",
        r"\bis rolling out\b": "utrullas",
        r"\bwill (likely )?feature\b": "väntas få",
        r"\bis finally headed to\b": "lanseras i",
        r"\bcoming to\b": "kommer till",
        r"\bteases?\b": "teasar",
        r"\bleaked\b": "läckt",
    }
    for k,v in repl.items():
        t = re.sub(k, v, t, flags=re.I)
    return clean_topic_title(t)

# === "Svenskhet" heuristik + poängsystem ===
SV_DOMAINS = {"svt.se","svtplay.se","sr.se","aftonbladet.se","expressen.se","dn.se","svd.se","gp.se","nyheter24.se",
              "omni.se","breakit.se","di.se","privataaffarer.se","sweclockers.com","m3.idg.se","mobil.se","surfa.se",
              "nyteknik.se","feber.se","fotbollskanalen.se","hockeysverige.se","svenskafans.com"}

SPORT_WORDS = {"allsvenskan","shl","slutspel","kvartsfinal","semifinal","landslaget","hockeyallsvenskan","derby",
               "aik","djurgården","hammarby","mff","ifk","mjällby","häcken","malmö ff","brynäs","frölunda"}

SE_WORDS = {"sverige","svensk","svenska","stockholm","göteborg","malmö","umeå","luleå","umea","lulea","örebro","uppsala","borås","boras"}

def is_probably_swedish(title: str) -> bool:
    if re.search(r"[åäöÅÄÖ]", title):
        return True
    # vanliga småord (inte 100%, men hjälper)
    return bool(re.search(r"\b(är|och|eller|men|som|på|för|med|utan|en|ett|det|den|i|från)\b", title, flags=re.I))

def score_candidate(title: str, cat_slug: str, origin: str) -> tuple[int, dict]:
    score = 0
    reasons = {}

    # Bas: svenskhet
    if is_probably_swedish(title):
        score += 3; reasons["åäö/sv-ord"] = +3

    # Domainpoäng
    dom = ""
    if origin:
        try:
            dom = urlparse(origin).netloc.lower()
        except Exception:
            dom = ""
    if dom:
        if dom.endswith(".se") or dom in SV_DOMAINS:
            score += 3; reasons[".se/domän"] = +3
        elif dom.endswith(".com"):
            score += 0  # neutral
        else:
            score -= 1; reasons["utländsk domän"] = -1

    # Sverige-ord
    if any(w in title.lower() for w in SE_WORDS):
        score += 2; reasons["Sverige-ord"] = +2

    # Sportboost
    if cat_slug == "sport":
        if any(w in title.lower() for w in SPORT_WORDS):
            score += 2; reasons["sport-ord"] = +2

    # Prylar: boosta ord som signalerar lanseringar/test
    if cat_slug in ("prylradar","teknik-prylar","gaming-esport"):
        if re.search(r"\b(lanser|släpper|uppdatering|recension|test|release|utrullning)\b", title, flags=re.I):
            score += 2; reasons["pryl-signal"] = +2

    # Nedviktning för "Indien/USA/Kina" om inte svensk kontext
    if re.search(r"\b(India|Indien|China|Kina|USA|US|UK)\b", title):
        if not any(w in title.lower() for w in ("sverige","svensk","stockholm","göteborg","malmö")):
            score -= 2; reasons["utlandsfokus"] = -2

    # Lång/kort rubrik-penalty/bonus
    L = len(title)
    if L < 28:
        score -= 1; reasons["för kort"] = -1
    elif L > 120:
        score -= 1; reasons["för lång"] = -1
    else:
        score += 1; reasons["lagom längd"] = +1

    return score, reasons

# === Samla kandidater per kategori och välj topp efter poäng ===
def pick_diverse_topics(max_total):
    print(f"▶ YouTube {'ON' if YT_API_KEY else 'OFF'} (region {YT_REGION})")
    seen_keys = set()
    picked = []

    for cat in CATEGORIES:
        quota = CATEGORY_QUOTA.get(cat["slug"], 0)
        if quota <= 0:
            continue

        # Översampla inom kategorin
        pool = []

        if cat["slug"] == "sport":
            # GNews från svenska sportbegrepp
            for q in SPORT_QUERIES:
                for t in gnews_recent_titles(q, max_items=6, max_age_hours=72):
                    pool.append((t, ""))  # origin okänd
        elif cat["slug"] == "prylradar":
            pool.extend(prylradar_items(max_items=24, max_age_days=14))  # (title, origin)
        elif cat["slug"] == "viralt-trend":
            wiki = wiki_top_sv(limit=15)
            reddit = reddit_top_sweden(limit=15)
            yt = youtube_trending_titles(limit=15)
            print(f"▶ Viralt pool: wiki={len(wiki)} reddit={len(reddit)} youtube={len(yt)}")
            pool.extend([(t, "") for t in wiki])
            pool.extend([(t, "") for t in reddit])
            pool.extend([(t, "") for t in yt])
        else:
            # Underhållning, teknik, ekonomi, nyheter, gaming – GNews
            for t in gnews_recent_titles(cat["query"], max_items=18, max_age_hours=48):
                pool.append((t, ""))

        # Poängsätt och sortera
        ranked = []
        for tup in pool:
            title, origin = tup if isinstance(tup, tuple) else (tup, "")
            clean = clean_topic_title(title)
            if cat["slug"] in ("prylradar","teknik-prylar"):
                clean = swedishify_title_if_needed(clean)
            key = normalize_title_key(clean)
            if not clean or key in seen_keys:
                continue
            sc, why = score_candidate(clean, cat["slug"], origin)
            ranked.append({"title": clean, "origin": origin, "cat_slug": cat["slug"], "cat_name": cat["name"], "score": sc, "why": why, "key": key})

        ranked.sort(key=lambda x: x["score"], reverse=True)

        # Välj bästa i kategorin, undvik duplicerade titlar globalt
        count = 0
        for r in ranked:
            if count >= quota or len(picked) >= max_total:
                break
            if r["key"] in seen_keys:
                continue
            picked.append({k: r[k] for k in ("title","origin","cat_slug","cat_name")})
            seen_keys.add(r["key"])
            count += 1

        if len(picked) >= max_total:
            break

    # Om vi ändå har för få: fyll på med GNews "Sverige"
    if len(picked) < max_total:
        extras = gnews_recent_titles("Sverige", max_items=50, max_age_hours=48)
        for t in extras:
            if len(picked) >= max_total:
                break
            clean = clean_topic_title(t)
            key = normalize_title_key(clean)
            if clean and key not in seen_keys:
                picked.append({"title": clean, "origin": "", "cat_slug": "nyheter", "cat_name": "Nyheter"})
                seen_keys.add(key)

    return picked

# === Text / utdrag ===
def make_excerpt(raw_text: str, max_chars=160) -> str:
    if not raw_text:
        return ""
    parts = [p.strip() for p in re.split(r'[.!?]\s+', raw_text) if p.strip()]
    for p in parts:
        if not p.startswith("-") and not p.lower().startswith("affiliate-idéer"):
            excerpt = p; break
    else:
        excerpt = parts[0] if parts else raw_text
    return (excerpt[:max_chars].rsplit(" ", 1)[0] + "…") if len(excerpt) > max_chars else excerpt

def text_to_html(txt: str) -> str:
    lines = [l.strip() for l in (txt or '').splitlines() if l.strip()]
    parts, bullets = [], []
    def flush_bullets():
        nonlocal bullets
        if bullets:
            parts.append("<ul>" + "".join(f"<li>{escape(b)}</li>" for b in bullets) + "</ul>")
            bullets.clear()
    for l in lines:
        if l.startswith("- "):
            bullets.append(l[2:].strip())
        else:
            flush_bullets()
            parts.append(f"<p>{escape(l)}</p>")
    flush_bullets()
    return "\n".join(parts) if parts else "<p></p>"

# === OpenAI sammanfattning (GPT-5 → GPT-5-mini) ===
def openai_chat_summarize(topic, snippets, model="gpt-5"):
    system = (
        "Skriv på svensk nyhetsprosa. 110–150 ord. Ingen rubrik.\n"
        "Struktur:\n- Detta har hänt: 1–2 meningar (konkret vad/när/var).\n"
        "- Varför det spelar roll: 1–2 meningar (påverkan/siffror om möjligt).\n"
        "- Vad händer härnäst: 1 mening (besked/datum/nästa steg).\n"
        "Lägg sedan 2–3 korta punkter som börjar med '- '.\n"
        "Avsluta med: 'Affiliate-idéer:' och 1–2 punkter som börjar med '- '.\n"
        "Undvik klichéer. Var specifik. Ingen markdown."
    )
    snip = "; ".join([f"{s['title']} ({s['link']})" for s in snippets]) if snippets else "Inga källsnuttar"
    payload = {"model": model,
               "messages": [{"role":"system","content":system},
                            {"role":"user","content": f"Ämne: {topic}\nNyhetssnuttar: {snip}"}]}
    resp = requests.post("https://api.openai.com/v1/chat/completions",
                         headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                                  "Content-Type": "application/json"},
                         json=payload, timeout=60)
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        print("OpenAI response text:", (resp.text or "")[:800])
        raise
    return resp.json()["choices"][0]["message"]["content"].strip()

def summarize_with_retries(topic, snippets):
    models = ["gpt-5", "gpt-5-mini"]
    for model in models:
        for attempt in range(2):
            try:
                return openai_chat_summarize(topic, snippets, model=model)
            except ReadTimeout:
                wait = 2 ** attempt
                print(f"⏳ OpenAI timeout ({model}) – försöker igen om {wait}s...")
                time.sleep(wait); continue
            except HTTPError as e:
                print("OpenAI HTTPError:", e); break
            except RequestException as e:
                print("OpenAI RequestException:", e); break
            except Exception as e:
                print("OpenAI annat fel:", e); break
    raise Exception("Alla modellförsök misslyckades")

# === WordPress ===
def wp_post_trend(title, body, topics=None, categories=None, excerpt=""):
    url = f"{WP_BASE_URL}/wp-json/trendkollen/v1/ingest"
    payload = {"title": title,"content": body,"excerpt": excerpt,
               "topics": topics or [],"categories": categories or []}
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
        print("⚠️ Kunde inte läsa WP-lista för duplikat:", e)
        return False

    def _parse_wp_dt(p):
        raw_gmt = p.get("date_gmt") or ""
        raw_loc = p.get("date") or ""
        for s in (raw_gmt, raw_loc):
            if not s: 
                continue
            try:
                dt = datetime.fromisoformat(s.replace("Z","+00:00"))
                dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
                return dt
            except Exception:
                continue
        return datetime.now(timezone.utc)

    now = datetime.now(timezone.utc)
    want_key = normalize_title_key(title)
    for p in posts:
        rendered = unescape(p.get("title", {}).get("rendered", "")).strip()
        have_key = normalize_title_key(rendered)
        if have_key == want_key:
            dt = _parse_wp_dt(p)
            if (now - dt) <= timedelta(hours=within_hours):
                return True
    return False

# === Bildgenerator (card + social) ===
def _hex_to_rgb(h): h=h.lstrip('#'); return tuple(int(h[i:i+2],16) for i in (0,2,4))
def _lerp(a,b,t): return int(a+(b-a)*t)
def _grad_color(c1, c2, t):
    r1,g1,b1 = _hex_to_rgb(c1); r2,g2,b2 = _hex_to_rgb(c2)
    return (_lerp(r1,r2,t), _lerp(g1,g2,t), _lerp(b1,b2,t))
def _seed_from_title(title: str) -> int: return int(hashlib.sha1(title.encode("utf-8")).hexdigest()[:8], 16)

def _load_font(path, size, label=''):
    try:
        f = ImageFont.truetype(path, size=size)
        print(f"🅵 Font OK ({label}): {path}")
        return f
    except Exception as e:
        print(f"⚠️ Font FAIL ({label}) vid {path}: {e}. Faller tillbaka till PIL default.")
        return ImageFont.load_default()

def generate_og_image(title: str, cat_slug: str, cat_name: str, date_str: str, out_path: str, with_text: bool = True):
    """1200x630 PNG. with_text=False => ren card-bild (ingen text) för grid/featured."""
    W,H = 1200, 630
    base1, base2 = CAT_COLORS.get(cat_slug, ("#111827","#374151"))
    seed = _seed_from_title(title)
    random.seed(seed)

    img = Image.new("RGB", (W,H), _hex_to_rgb(base1))
    draw = ImageDraw.Draw(img)
    # gradient
    for y in range(H):
        t = y / (H-1)
        col = _grad_color(base1, base2, t)
        draw.line([(0,y),(W,y)], fill=col)
    # mönster
    for _ in range(120):
        x = random.randint(0,W); y = random.randint(0,H)
        r = random.randint(2,5); alpha = random.randint(18,32)
        dot = Image.new("RGBA",(r*2,r*2),(0,0,0,0))
        ImageDraw.Draw(dot).ellipse((0,0,r*2,r*2), fill=(255,255,255,alpha))
        img.paste(dot,(x,y),dot)

    if with_text:
        padX, padY = 72, 60
        title_font = _load_font(FONT_BOLD_PATH, 64, 'Bold')
        chip_font  = _load_font(FONT_BOLD_PATH, 28, 'Bold')
        meta_font  = _load_font(FONT_REG_PATH, 28, 'Regular')

        # kategori-chip
        chip_text = cat_name
        chip_padX, chip_padY = 18, 10
        chip_text_w, chip_text_h = draw.textbbox((0,0), chip_text, font=chip_font)[2:]
        chip_w = chip_text_w + chip_padX*2; chip_h = chip_text_h + chip_padY*2
        chip_x, chip_y = padX, padY
        draw.rounded_rectangle((chip_x, chip_y, chip_x+chip_w, chip_y+chip_h), radius=16,
                               fill=(255,255,255,38), outline=(255,255,255,64), width=1)
        draw.text((chip_x+chip_padX, chip_y+chip_padY-2), chip_text, font=chip_font, fill=(255,255,255,230))

        # datum
        dt_w, _ = draw.textbbox((0,0), date_str, font=meta_font)[2:]
        draw.text((W-padX-dt_w, padY+2), date_str, font=meta_font, fill=(236,242,255,220))

        # titel (wrap ≤3 rader)
        max_width = W - padX*2; words = re.split(r'\s+', title.strip())
        lines, size = [], 64
        while size >= 40:
            f = _load_font(FONT_BOLD_PATH, size, 'Bold')
            tmp, cur = [], ""
            for w in words:
                test = (cur+" "+w).strip()
                tw = draw.textbbox((0,0), test, font=f)[2]
                if tw <= max_width: cur = test
                else: tmp.append(cur); cur = w
            if cur: tmp.append(cur)
            if len(tmp) <= 3:
                title_font = f; lines = tmp; break
            size -= 4
        y = chip_y + chip_h + 36
        for ln in lines:
            draw.text((padX, y), ln, font=title_font, fill=(255,255,255,245))
            y += title_font.size + 6

        brand_font = _load_font(FONT_BOLD_PATH, 24, 'Bold')
        draw.text((padX, H-60-28), "Trendkoll", font=brand_font, fill=(255,255,255,200))

    img.save(out_path, "PNG")

def upload_media_to_wp(png_path: str, filename: str):
    url = f"{WP_BASE_URL}/wp-json/wp/v2/media"
    with open(png_path, "rb") as f:
        headers = {"Content-Disposition": f'attachment; filename="{filename}"',
                   "Content-Type": "image/png"}
        resp = requests.post(url, headers=headers, data=f, auth=(WP_USER, WP_APP_PASS), timeout=60)
    resp.raise_for_status()
    j = resp.json()
    return j.get("id"), j.get("source_url")

def set_post_featured_media(post_id: int, media_id: int):
    url = f"{WP_BASE_URL}/wp-json/wp/v2/trend/{post_id}"
    resp = requests.post(url, json={"featured_media": media_id}, auth=(WP_USER, WP_APP_PASS), timeout=30)
    resp.raise_for_status()
    return resp.json()

def set_post_social_image_url(post_id: int, social_url: str):
    url = f"{WP_BASE_URL}/wp-json/wp/v2/trend/{post_id}"
    resp = requests.post(url, json={"meta": {"tk_social_image": social_url}}, auth=(WP_USER, WP_APP_PASS), timeout=30)
    resp.raise_for_status()
    return resp.json()

# === MAIN ===
def main():
    print("🔎 Startar Trendkoll-worker...")
    print("BASE_URL:", WP_BASE_URL, "| USER:", WP_USER)

    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Översampla 3× så vi kan hoppa över dubbletter men ändå nå kvoten
    bundles = pick_diverse_topics(max_total=MAX_TRENDS * 3)
    if not bundles:
        print("⚠️ Hittade inga topics. Avbryter.")
        return

    posted_now_keys = set()
    posted = 0

    for b in bundles:
        if posted >= MAX_TRENDS:
            break

        title = b["title"]
        cat   = b["cat_slug"]
        cat_name = b["cat_name"]
        origin = b.get("origin") or ""
        key = normalize_title_key(title)

        print(f"➡️  [{cat}] {title}")

        # Skydda mot dubbletter
        if key in posted_now_keys:
            print("⏭️ Hoppar över (dubblett i samma körning).")
            continue
        if wp_trend_exists_exact(title, within_hours=24):
            print("⏭️ Hoppar över (fanns redan senaste 24h i WP).")
            continue

        # Källsnuttar till summering
        snippets = gnews_snippets_sv(title, max_items=4, max_age_hours=72)
        if not snippets and origin:
            dom = urlparse(origin).netloc or "Källa"
            snippets = [{"title": dom, "link": origin, "published": ""}]

        # Summering (GPT-5 → 5-mini → fallback)
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

        excerpt = make_excerpt(raw_summary, max_chars=160)

        try:
            res = wp_post_trend(
                title=title,
                body=body,
                topics=["idag", "svenska-trender", date_tag],
                categories=[cat],
                excerpt=excerpt
            )
            post_id = res.get("post_id")
            print("✅ Postad:", res)

            if post_id:
                # 1) Card/featured (ingen text)
                card_path   = f"/tmp/card_trend_{post_id}.png"
                social_path = f"/tmp/social_trend_{post_id}.png"
                date_for_img = datetime.now(timezone.utc).strftime("%Y-%m-%d")

                generate_og_image(title, cat, cat_name, date_for_img, card_path, with_text=False)
                try:
                    media_id_card, url_card = upload_media_to_wp(card_path, f"card_trend_{post_id}.png")
                    set_post_featured_media(post_id, media_id_card)
                    print(f"🖼️  Featured (card) image satt: {url_card}")
                except Exception as e:
                    print("⚠️ Kunde inte sätta featured card image:", e)

                # 2) Social/OG (med text)
                generate_og_image(title, cat, cat_name, date_for_img, social_path, with_text=True)
                try:
                    media_id_social, url_social = upload_media_to_wp(social_path, f"social_trend_{post_id}.png")
                    set_post_social_image_url(post_id, url_social)
                    print(f"🔗  Social image satt (og:image): {url_social}")
                except Exception as e:
                    print("⚠️ Kunde inte sätta social image:", e)

            posted_now_keys.add(key)
            posted += 1
            time.sleep(random.uniform(0.8, 1.6))

        except Exception as e:
            print("❌ Fel vid postning till WP:", e)

    print("🏁 Klar körning.")

if __name__ == "__main__":
    main()

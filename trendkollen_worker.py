# trendkollen_worker.py
import os, time, random, requests, re, unicodedata, hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlparse, parse_qs
from html import escape, unescape
import feedparser
from dotenv import load_dotenv
from requests.exceptions import ReadTimeout, HTTPError, RequestException
from PIL import Image, ImageDraw, ImageFont  # Pillow för bildgenerering
from bs4 import BeautifulSoup  # för att plocka original-länkar ur Google News RSS

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

# Minimikrav på antal källsnuttar per kategori
MIN_SNIPPETS = {
    "nyheter":2, "sport":2, "ekonomi-bors":2,
    "teknik-prylar":1, "prylradar":1, "underhallning":1, "gaming-esport":1, "viralt-trend":2
}
# Minsta poäng som krävs för “wow”
WOW_THRESHOLD = {
    "nyheter":5, "sport":5, "ekonomi-bors":5, "teknik-prylar":4, "prylradar":4,
    "underhallning":3, "gaming-esport":4, "viralt-trend":4
}

# Betrodda domäner där 1 källa räcker
TRUSTED_ONE_SOURCE = {
    "svt.se","smhi.se","polisen.se","fotbollskanalen.se","svenskfotboll.se",
    "hockeysverige.se","m3.idg.se","sweclockers.com"
}

# === Svenska pryl/teknik-feeds + internationell fallback ===
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

SPORT_QUERIES = [
    "Allsvenskan", "SHL", "Damallsvenskan", "Tre Kronor",
    "Sveriges landslag fotboll", "Premier League Sverige", "Champions League Sverige"
]
PRYL_QUERIES = [
    "lansering smartphone", "\"ny mobil\"", "iPhone lansering",
    "Samsung släpper", "smartwatch lansering", "AI-kamera lansering",
    "RTX grafikkort", "Playstation uppdatering"
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

def _first_external_href_from_html(html: str):
    try:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "news.google.com" not in href:
                return href
    except Exception:
        pass
    return None

def resolve_final_url(u: str) -> str:
    if not u: return u
    try:
        r = requests.head(u, headers=UA_HEADERS, timeout=10, allow_redirects=True)
        r.raise_for_status()
        return r.url
    except Exception:
        try:
            r = requests.get(u, headers=UA_HEADERS, timeout=10, allow_redirects=True, stream=True)
            final = r.url
            r.close()
            return final
        except Exception:
            return u

def extract_original_from_gnews_entry(entry):
    link = getattr(entry, "link", "")
    # Försök 1: följ redirect
    final = resolve_final_url(link)
    dom = urlparse(final).netloc if final else ""
    if final and dom and "news.google.com" not in dom:
        src_name = getattr(getattr(entry, "source", {}), "title", "") or dom.replace("www.", "")
        return final, src_name
    # Försök 2: första externa länk i summary
    summary = getattr(entry, "summary", "")
    href2 = _first_external_href_from_html(summary)
    if href2:
        dom2 = urlparse(href2).netloc
        src_name = getattr(getattr(entry, "source", {}), "title", "") or dom2.replace("www.", "")
        return href2, src_name
    # Försök 3: url= i query
    try:
        q = parse_qs(urlparse(link).query)
        if "url" in q and q["url"]:
            href3 = q["url"][0]
            dom3 = urlparse(href3).netloc
            if dom3:
                src_name = getattr(getattr(entry, "source", {}), "title", "") or dom3.replace("www.", "")
                return href3, src_name
    except Exception:
        pass
    # ge upp
    src_name = getattr(getattr(entry, "source", {}), "title", "") or (dom.replace("www.", "") if dom else "Källa")
    return final or link, src_name

def gnews_snippets_sv(query, max_items=3, max_age_hours=72):
    q = f"{query} when:3d"
    url = f"https://news.google.com/rss/search?q={quote(q)}&hl=sv-SE&gl=SE&ceid=SE:sv"
    feed = fetch_rss(url)
    items = []
    for entry in (feed.entries or []):
        if is_recent(parse_entry_dt(entry), max_age_hours=max_age_hours):
            final_url, source_name = extract_original_from_gnews_entry(entry)
            items.append({
                "title": entry.title,
                "link": final_url,
                "source": source_name or urlparse(final_url).netloc.replace("www.", "")
            })
            if len(items) >= max_items:
                break
    return items

# === Wikipedia: idag → igår → i förrgår, filtrera meta-sidor ===
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

# === Reddit: JSON → RSS fallback ===
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
    if not YT_API_KEY: return []
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

# === Prylradar ===
def prylradar_items(max_items=12, max_age_days=14):
    items = []
    items.extend(feed_titles(PRYL_FEEDS_SV, max_items=max_items, max_age_days=max_age_days))
    if len(items) < max_items:
        for q in PRYL_QUERIES:
            for domain in ["site:surfa.se", "site:m3.idg.se", "site:mobil.se", "site:sweclockers.com",
                           "site:feber.se", "site:nyteknik.se"]:
                ts = gnews_recent_titles(f"{q} {domain}", max_items=3, max_age_hours=max_age_days*24)
                for t in ts:
                    items.append((t, ""))  # origin ok
                    if len(items) >= max_items:
                        break
                if len(items) >= max_items: break
            if len(items) >= max_items: break
    if len(items) < max_items:
        items.extend(feed_titles(PRYL_FEEDS_INT, max_items=max_items - len(items), max_age_days=max_age_days))
    return items[:max_items]

# === Rubrikstädning + svenskifiering ===
def clean_topic_title(t: str) -> str:
    t = t.strip()
    t = re.sub(r'^(JUST NU:|DN Direkt\s*-\s*|LIVE:|AB:\s*|Aftonbladet:\s*|Expressen:\s*)\s*', '', t, flags=re.I)
    if re.match(r'^(se\s|ett inlägg i\s*”?se)', t, flags=re.I):
        return ""  # tv-promo
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

# === “Svenskhet” heuristik + poängsystem ===
SV_DOMAINS = {"svt.se","svtplay.se","sr.se","aftonbladet.se","expressen.se","dn.se","svd.se","gp.se","nyheter24.se",
              "omni.se","breakit.se","di.se","privataaffarer.se","sweclockers.com","m3.idg.se","mobil.se","surfa.se",
              "nyteknik.se","feber.se","fotbollskanalen.se","hockeysverige.se","svenskafans.com"}
SPORT_WORDS = {"allsvenskan","shl","slutspel","kvartsfinal","semifinal","landslaget","hockeyallsvenskan","derby",
               "aik","djurgården","hammarby","mff","ifk","mjällby","häcken","malmö ff","brynäs","frölunda"}
SE_WORDS = {"sverige","svensk","svenska","stockholm","göteborg","malmö","umeå","luleå","umea","lulea","örebro","uppsala","borås","boras"}

def is_probably_swedish(title: str) -> bool:
    if re.search(r"[åäöÅÄÖ]", title): return True
    return bool(re.search(r"\b(är|och|eller|men|som|på|för|med|utan|en|ett|det|den|i|från)\b", title, flags=re.I))

def score_candidate(title: str, cat_slug: str, origin: str):
    score = 0; reasons = {}
    if is_probably_swedish(title): score += 3; reasons["åäö/sv-ord"] = +3
    dom = ""
    if origin:
        try: dom = urlparse(origin).netloc.lower()
        except Exception: dom = ""
    if dom:
        if dom.endswith(".se") or dom in SV_DOMAINS: score += 3; reasons[".se/domän"] = +3
        elif not dom.endswith(".com"): score -= 1; reasons["utländsk domän"] = -1
    if any(w in title.lower() for w in SE_WORDS): score += 2; reasons["Sverige-ord"] = +2
    if cat_slug == "sport" and any(w in title.lower() for w in SPORT_WORDS): score += 2; reasons["sport-ord"] = +2
    if cat_slug in ("prylradar","teknik-prylar","gaming-esport") and re.search(r"\b(lanser|släpper|uppdatering|recension|test|release|utrullning)\b", title, flags=re.I):
        score += 2; reasons["pryl-signal"] = +2
    if re.search(r"\b(India|Indien|China|Kina|USA|US|UK)\b", title) and not any(w in title.lower() for w in ("sverige","svensk","stockholm","göteborg","malmö")):
        score -= 2; reasons["utlandsfokus"] = -2
    L = len(title)
    if L < 28: score -= 1; reasons["för kort"] = -1
    elif L > 120: score -= 1; reasons["för lång"] = -1
    else: score += 1; reasons["lagom längd"] = +1
    return score, reasons

def reasons_to_str(d: dict) -> str:
    if not d: return "{}"
    return "{" + ", ".join(f"{k}:{'+' if v>0 else ''}{v}" for k,v in d.items()) + "}"

# === Kandidater per kategori ===
def pick_diverse_topics(max_total):
    print(f"▶ YouTube {'ON' if YT_API_KEY else 'OFF'} (region {YT_REGION})")
    seen_keys = set(); picked = []
    for cat in CATEGORIES:
        quota = CATEGORY_QUOTA.get(cat["slug"], 0)
        if quota <= 0: continue
        pool = []
        if cat["slug"] == "sport":
            for q in SPORT_QUERIES:
                for t in gnews_recent_titles(q, max_items=6, max_age_hours=72): pool.append((t, ""))
        elif cat["slug"] == "prylradar":
            pool.extend(prylradar_items(max_items=24, max_age_days=14))
        elif cat["slug"] == "viralt-trend":
            wiki = wiki_top_sv(limit=15); reddit = reddit_top_sweden(limit=15); yt = youtube_trending_titles(limit=15)
            print(f"▶ Viralt pool: wiki={len(wiki)} reddit={len(reddit)} youtube={len(yt)}")
            pool += [(t, "") for t in wiki] + [(t, "") for t in reddit] + [(t, "") for t in yt]
        else:
            for t in gnews_recent_titles(cat["query"], max_items=18, max_age_hours=48): pool.append((t, ""))

        ranked = []
        for tup in pool:
            title, origin = tup if isinstance(tup, tuple) else (tup, "")
            clean = clean_topic_title(title)
            if not clean: continue
            if cat["slug"] in ("prylradar","teknik-prylar"): clean = swedishify_title_if_needed(clean)
            key = normalize_title_key(clean)
            if key in seen_keys: continue
            sc, why = score_candidate(clean, cat["slug"], origin)
            ranked.append({"title": clean, "origin": origin, "cat_slug": cat["slug"], "cat_name": cat["name"], "score": sc, "why": why, "key": key})

        thr = WOW_THRESHOLD.get(cat["slug"], 3)
        ranked = [r for r in ranked if r["score"] >= thr]
        ranked.sort(key=lambda x: x["score"], reverse=True)

        for r in ranked[:3]:
            print(f"🧪 {cat['slug']} kandidat: {r['title']} | score={r['score']} {reasons_to_str(r['why'])}")

        count = 0
        for r in ranked:
            if count >= quota or len(picked) >= max_total: break
            if r["key"] in seen_keys: continue
            picked.append(r); seen_keys.add(r["key"]); count += 1
        if len(picked) >= max_total: break

    if len(picked) < max_total:
        extras = gnews_recent_titles("Sverige", max_items=50, max_age_hours=48)
        for t in extras:
            if len(picked) >= max_total: break
            clean = clean_topic_title(t)
            if not clean: continue
            key = normalize_title_key(clean)
            if key in seen_keys: continue
            sc, why = score_candidate(clean, "nyheter", "")
            if sc >= WOW_THRESHOLD.get("nyheter", 3):
                picked.append({"title": clean, "origin": "", "cat_slug": "nyheter", "cat_name": "Nyheter", "score": sc, "why": why, "key": key})
                seen_keys.add(key)
    return picked

# === Text / utdrag ===
def make_excerpt(raw_text: str, max_chars=160) -> str:
    if not raw_text: return ""
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

# === OpenAI sammanfattning (folkbildningsläge) ===
def openai_chat_summarize(topic, snippets, model="gpt-5"):
    system = (
      "Skriv på enkel svenska (ca högstadienivå), 110–150 ord. Ingen rubrik.\n"
      "MÅSTE ingå i ordning:\n"
      "1) Enkelt förklarat: 1 mening som sammanfattar med vardagsord (undvik fackspråk; förklara termer i parentes, t.ex. 'reaktor (el-fabrik)').\n"
      "2) Detta har hänt: 1–2 meningar (vad/när/var MED namn på personer/bolag/lag och siffror/datum om de finns).\n"
      "3) Varför det spelar roll: 1–2 meningar (påverkan/siffror: pris, tid, risk, omfattning).\n"
      "4) Så påverkar det dig: 2–4 punkter som börjar med '- ' (konkreta effekter i vardagen för en person i Sverige).\n"
      "5) Vad händer härnäst: 1 mening (nästa steg med datum eller tydlig trigger).\n"
      "Avsluta med: 'Affiliate-idéer:' och 1–2 punkter som börjar med '- '.\n"
      "Undvik jargong och klichéer. Var specifik. Ingen markdown."
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
            if not s: continue
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

# hitta “senaste post” genom sök (för event-uppdatering)
def wp_find_recent_trend_by_query(query: str, within_hours=24):
    try:
        url = f"{WP_BASE_URL}/wp-json/wp/v2/trend?search={quote(query)}&per_page=10&orderby=date&order=desc"
        r = requests.get(url, auth=(WP_USER, WP_APP_PASS), timeout=20)
        r.raise_for_status()
        posts = r.json()
        now = datetime.now(timezone.utc)
        for p in posts:
            raw = (p.get("date_gmt") or p.get("date") or "").replace("Z","+00:00")
            try:
                dt = datetime.fromisoformat(raw)
                dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
            except Exception:
                dt = now
            if (now - dt) <= timedelta(hours=within_hours):
                return p.get("id")
    except Exception as e:
        print("⚠️ wp_find_recent_trend_by_query fel:", e)
    return None

def wp_append_update(post_id: int, extra_html: str):
    url = f"{WP_BASE_URL}/wp-json/wp/v2/trend/{post_id}"
    try:
        cur = requests.get(url, auth=(WP_USER, WP_APP_PASS), timeout=20).json()
        old_content = cur.get("content",{}).get("rendered","")
    except Exception:
        old_content = ""
    new_content = old_content + "\n<hr />\n<h3>Uppdatering</h3>\n" + extra_html
    resp = requests.post(url, json={"content": new_content}, auth=(WP_USER, WP_APP_PASS), timeout=30)
    resp.raise_for_status()
    return resp.json()

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
    W,H = 1200, 630
    base1, base2 = CAT_COLORS.get(cat_slug, ("#111827","#374151"))
    seed = _seed_from_title(title); random.seed(seed)

    img = Image.new("RGB", (W,H), _hex_to_rgb(base1))
    draw = ImageDraw.Draw(img)
    # gradient
    for y in range(H):
        t = y / (H-1); col = _grad_color(base1, base2, t)
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

        chip_text = cat_name
        chip_padX, chip_padY = 18, 10
        chip_text_w, chip_text_h = draw.textbbox((0,0), chip_text, font=chip_font)[2:]
        chip_w = chip_text_w + chip_padX*2; chip_h = chip_text_h + chip_padY*2
        chip_x, chip_y = padX, padY
        draw.rounded_rectangle((chip_x, chip_y, chip_x+chip_w, chip_y+chip_h), radius=16,
                               fill=(255,255,255,38), outline=(255,255,255,64), width=1)
        draw.text((chip_x+chip_padX, chip_y+chip_padY-2), chip_text, font=chip_font, fill=(255,255,255,230))

        dt_w, _ = draw.textbbox((0,0), date_str, font=meta_font)[2:]
        draw.text((W-padX-dt_w, padY+2), date_str, font=meta_font, fill=(236,242,255,220))

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

# === Event-grouping: slå ihop upprepade händelser (storm, sport, etc.) ===
def canonical_event_key(title: str):
    t = title.lower()
    m = re.search(r"(stormen|orkan(en)?)\s+([a-zåäö]+)", t)
    if m: return f"weather:{m.group(3)}"
    clubs = ["mjällby","mff","malmö ff","häcken","aik","djurgården","hammarby","ifk","elfsborg","norrköping","rosengård","brynäs","frölunda"]
    for c in clubs:
        if c in t: return f"sport:{c}"
    if "kärnkraft" in t: return "policy:karnkraft"
    return None

def dynamic_min_snippets(cat_slug: str, resolved_snippets: list[dict]) -> int:
    base = MIN_SNIPPETS.get(cat_slug, 1)
    if base <= 1: return base
    for r in resolved_snippets:
        dom = (urlparse(r['link']).netloc or "").replace("www.","")
        if any(dom.endswith(d) for d in TRUSTED_ONE_SOURCE):
            return 1
    return base

# === MAIN ===
def main():
    print("🔎 Startar Trendkoll-worker...")
    print("BASE_URL:", WP_BASE_URL, "| USER:", WP_USER)

    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bundles = pick_diverse_topics(max_total=MAX_TRENDS * 3)
    if not bundles:
        print("⚠️ Hittade inga topics. Avbryter."); return

    posted_now_keys = set(); posted = 0

    for b in bundles:
        if posted >= MAX_TRENDS: break

        title    = b["title"]
        cat      = b["cat_slug"]
        cat_name = b["cat_name"]
        origin   = b.get("origin") or ""
        key      = b.get("key") or normalize_title_key(title)
        score    = b.get("score", None)
        why      = b.get("why", {})

        print(f"➡️  [{cat}] {title}")
        if score is not None: print(f"🧮 score={score} {reasons_to_str(why)}")

        # Dubblettskydd
        if key in posted_now_keys:
            print("⏭️ Hoppar över (dubblett i samma körning)."); continue
        if wp_trend_exists_exact(title, within_hours=24):
            print("⏭️ Hoppar över (fanns redan senaste 24h i WP)."); continue

        # Snippets + källor (riktiga URL:er, varumärke + domän)
        snippets = gnews_snippets_sv(title, max_items=4, max_age_hours=72)
        resolved = []
        for s in snippets:
            final = s["link"]
            dom = urlparse(final).netloc.replace("www.", "") if final else "Källa"
            resolved.append({"title": s["title"], "link": final, "source": s.get("source") or dom, "dom": dom})

        need = dynamic_min_snippets(cat, resolved)
        if len(resolved) < need and not origin:
            print(f"⏭️ Skippas: för få källor ({len(resolved)}/{need})."); continue
        if not resolved and origin:
            dom = urlparse(origin).netloc.replace("www.", "") if origin else "Källa"
            resolved = [{"title": dom, "link": origin, "source": dom, "dom": dom}]

        # Event-sammanslagning: uppdatera befintlig post i stället för att skapa ny
        event_key = canonical_event_key(title)
        if event_key and ("weather:" in event_key or "sport:" in event_key):
            q = event_key.split(":")[1]
            existing_id = wp_find_recent_trend_by_query(q, within_hours=12)
            if existing_id:
                print(f"🔁 Uppdaterar befintlig händelse ({event_key}) → post {existing_id}")
                # kort uppdatering av typen: text + källor
                update_txt = f"{title}. " + (", ".join(r['source'] for r in resolved) if resolved else "")
                update_html = text_to_html(make_excerpt(update_txt, max_chars=220))
                try:
                    wp_append_update(existing_id, update_html)
                    posted += 1; posted_now_keys.add(key)
                    time.sleep(random.uniform(0.6, 1.2))
                    continue  # hoppa ny post
                except Exception as e:
                    print("⚠️ Misslyckades uppdatera, postar nytt istället:", e)

        # Sammanfattning
        try:
            raw_summary = summarize_with_retries(title, [{"title": r["source"], "link": r["link"]} for r in resolved])
        except Exception as e2:
            print("❌ OpenAI-fel, kör no-AI fallback:", e2)
            bullets = "\n".join([f"- {r['source']}" for r in resolved[:3]]) if resolved else "- Ingen nyhetskälla tillgänglig"
            raw_summary = f"Enkelt förklarat: {title}.\n\n{bullets}\n\nAffiliate-idéer:\n- Sök efter relaterade produkter/tjänster hos dina partnernätverk."

        summary_html   = text_to_html(raw_summary)
        published_str  = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')

        # Källrendering
        li = []
        for r in resolved:
            dom = (urlparse(r['link']).netloc or "").replace("www.","")
            label = r.get("source") or dom or "Källa"
            label_full = f"{label} ({dom})" if dom and label.lower() not in dom.lower() else label
            li.append(f"<li><a href='{r['link']}' target='_blank' rel='nofollow noopener'>{escape(label_full)}</a></li>")
        source_items = "".join(li)
        source_header = "<h3>Källor</h3>" if len(resolved) != 1 else "<h3>Källa</h3>"
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

                generate_og_image(title, cat, cat_name, date_for_img, social_path, with_text=True)
                try:
                    media_id_social, url_social = upload_media_to_wp(social_path, f"social_trend_{post_id}.png")
                    set_post_social_image_url(post_id, url_social)
                    print(f"🔗  Social image satt (og:image): {url_social}")
                except Exception as e:
                    print("⚠️ Kunde inte sätta social image:", e)

            posted_now_keys.add(key); posted += 1
            time.sleep(random.uniform(0.8, 1.6))

        except Exception as e:
            print("❌ Fel vid postning till WP:", e)

    print(f"📊 Summering: publicerade={posted}, översamlade={len(bundles)}, kvar_kvot={max(0, MAX_TRENDS-posted)}")
    print("🏁 Klar körning.")

if __name__ == "__main__":
    main()

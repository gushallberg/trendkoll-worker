import os, time, random, requests, json, re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from html import escape, unescape
import feedparser
from dotenv import load_dotenv
from requests.exceptions import ReadTimeout, HTTPError, RequestException

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WP_BASE_URL    = os.getenv("WP_BASE_URL")
WP_USER        = os.getenv("WP_USER")
WP_APP_PASS    = os.getenv("WP_APP_PASS")
MAX_TRENDS     = int(os.getenv("MAX_TRENDS", "7"))  # totala målämnen per körning

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# --------- Kategorier & kvoter ---------
# slug = vad som lagras som term i WP-taxonomin trend_category
CATEGORIES = [
    {"slug": "nyheter",        "name": "Nyheter",         "query": "Sverige"},
    {"slug": "sport",          "name": "Sport",           "query": "Allsvenskan OR SHL OR Premier League Sverige OR Champions League Sverige OR landslaget"},
    {"slug": "teknik-prylar",  "name": "Teknik & Prylar", "query": "smartphone OR lansering OR 'ny mobil' OR pryl OR teknik"},
    {"slug": "prylradar",      "name": "Prylradar",       "query": "lansering OR släpper OR release OR uppdatering OR recension teknik pryl gadget"},
    {"slug": "underhallning",  "name": "Underhållning",   "query": "film OR serie OR streaming OR musik OR kändis OR influencer"},
    {"slug": "ekonomi-bors",   "name": "Ekonomi & Börs",  "query": "börsen OR aktier OR inflation OR ränta OR Riksbanken"},
    {"slug": "gaming-esport",  "name": "Gaming & e-sport","query": "gaming OR e-sport OR playstation OR xbox OR nintendo OR steam"},
    {"slug": "viralt-trend",   "name": "Viralt & Trendord","query": "tiktok OR viralt OR meme OR trend OR hashtag"},
]

# hur många ämnen per kategori (försöker uppfylla kvoterna, totalt begränsas också av MAX_TRENDS)
CATEGORY_QUOTA = {
    "nyheter": 1,
    "sport": 1,
    "teknik-prylar": 1,
    "prylradar": 1,
    "underhallning": 1,
    "ekonomi-bors": 1,
    "gaming-esport": 1,
    "viralt-trend": 1,
}

# extra källor/queries per kategori
SPORT_QUERIES = [
    "Allsvenskan",
    "SHL",
    "Premier League Sverige",
    "Champions League Sverige",
    "Damallsvenskan",
    "Landslaget fotboll",
    "Tre Kronor",
]

PRYL_QUERIES = [
    "lansering smartphone",
    "\"ny mobil\"",
    "iPhone lansering",
    "Samsung släpper",
    "smartwatch lansering",
    "AI-kamera lansering",
    "RTX grafikkort",
    "Playstation uppdatering",
]

PRYL_FEEDS = [
    # internationella tech-feeds (om någon 404:ar är det lugnt – vi fångar det)
    "https://www.gsmarena.com/rss-news-reviews.php3",
    "https://www.theverge.com/rss/index.xml",
    "https://www.engadget.com/rss.xml",
    "https://www.techradar.com/rss",
]

# --------- RSS helpers ---------

def fetch_rss(url):
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=15)
        r.raise_for_status()
        return feedparser.parse(r.text)
    except Exception as e:
        print("⚠️ RSS-fel på", url, "→", e)
        return feedparser.FeedParserDict(entries=[])

def gnews_titles(query, max_items=6):
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=sv-SE&gl=SE&ceid=SE:sv"
    feed = fetch_rss(url)
    return [e.title for e in (feed.entries or [])[:max_items]]

def prylradar_titles(max_items=10):
    titles = []
    # Feeds
    for u in PRYL_FEEDS:
        feed = fetch_rss(u)
        titles.extend([e.title for e in (feed.entries or [])[:max_items//2]])
        if len(titles) >= max_items:
            break
    # GNews queries
    for q in PRYL_QUERIES:
        titles.extend(gnews_titles(q, max_items=3))
        if len(titles) >= max_items:
            break
    return titles[:max_items]

# --------- Text helpers ---------

def clean_topic_title(t: str) -> str:
    # Ta bort onödiga prefix/suffix (t.ex. "JUST NU:", "DN Direkt -", och källnamn på slutet)
    t = t.strip()
    t = re.sub(r'^(JUST NU:|DN Direkt\s*-\s*|LIVE:)\s*', '', t, flags=re.I)
    t = re.sub(r'\s+[–-]\s+[^\-–—|:]{2,}$', '', t).strip()
    return t

def gnews_snippets_sv(query, max_items=3):
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=sv-SE&gl=SE&ceid=SE:sv"
    feed = fetch_rss(url)
    items = []
    for entry in (feed.entries or [])[:max_items]:
        items.append({
            "title": entry.title,
            "link": entry.link,
            "published": entry.get("published", "")
        })
    return items

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

# --------- OpenAI (GPT-5) ---------

def openai_chat_summarize(topic, snippets, model="gpt-5"):
    system = (
        "Du är en svensk nyhetsredaktör. Skriv en kort sammanfattning (120–180 ord) "
        "om varför ämnet trendar just nu. Skriv INTE någon rubrik och upprepa inte ämnets rubrik. "
        "Skriv 2–3 korta punkter (börja varje punkt med '- ') och 2–3 meningar sammanfattning. "
        "Avsluta med raden 'Affiliate-idéer:' följt av 1–2 idéer på nya rader som börjar med '- '. "
        "Ingen markdown, bara ren text med radbrytningar."
    )
    snip = "; ".join([f"{s['title']} ({s['link']})" for s in snippets]) if snippets else "Inga källsnuttar"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Ämne: {topic}\nNyhetssnuttar: {snip}"}
        ]
    }
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload, timeout=60
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        print("OpenAI response text:", (resp.text or "")[:800])
        raise
    j = resp.json()
    return j["choices"][0]["message"]["content"].strip()

def summarize_with_retries(topic, snippets):
    """Försök gpt-5 → gpt-5-mini, två försök per modell, backoff vid timeout."""
    models = ["gpt-5", "gpt-5-mini"]
    for model in models:
        for attempt in range(2):
            try:
                return openai_chat_summarize(topic, snippets, model=model)
            except ReadTimeout:
                wait = 2 ** attempt
                print(f"⏳ OpenAI timeout ({model}) – försöker igen om {wait}s...")
                time.sleep(wait)
                continue
            except HTTPError as e:
                print("OpenAI HTTPError:", e)
                break
            except RequestException as e:
                print("OpenAI RequestException:", e)
                break
            except Exception as e:
                print("OpenAI annat fel:", e)
                break
    raise Exception("Alla modellförsök misslyckades")

# --------- WordPress helpers ---------

def wp_post_trend(title, body, topics=None, categories=None, excerpt=""):
    url = f"{WP_BASE_URL}/wp-json/trendkollen/v1/ingest"
    payload = {"title": title, "content": body, "excerpt": excerpt, "topics": topics or [], "categories": categories or []}
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
    now = datetime.now(timezone.utc)
    for p in posts:
        rendered = unescape(p.get("title", {}).get("rendered", "")).strip()
        if rendered.lower() == title.strip().lower():
            date_gmt = p.get("date_gmt")
            dt = datetime.fromisoformat(date_gmt.replace("Z", "+00:00")) if date_gmt else now
            if (now - dt) <= timedelta(hours=within_hours):
                return True
    return False

# --------- Urval: mix per kategori ---------

def pick_diverse_topics(max_total):
    """Välj ämnen enligt CATEGORY_QUOTA, undvik dubbletter, fyll upp från Nyheter."""
    seen = set()
    picked = []
    for cat in CATEGORIES:
        quota = CATEGORY_QUOTA.get(cat["slug"], 0)
        if quota <= 0: 
            continue

        # Källa per kategori
        titles_pool = []
        if cat["slug"] == "sport":
            for q in SPORT_QUERIES:
                titles_pool.extend(gnews_titles(q, max_items=4))
        elif cat["slug"] == "prylradar":
            titles_pool.extend(prylradar_titles(max_items=12))
        else:
            titles_pool.extend(gnews_titles(cat["query"], max_items=10))

        # Välj upp till quota unika
        count = 0
        for t in titles_pool:
            if len(picked) >= max_total: break
            clean = clean_topic_title(t)
            if not clean or clean.lower() in seen: continue
            picked.append({"title": clean, "cat_slug": cat["slug"], "cat_name": cat["name"]})
            seen.add(clean.lower())
            count += 1
            if count >= quota: break

        if len(picked) >= max_total: break

    # Fyll på från Nyheter om vi saknar ämnen
    if len(picked) < max_total:
        extra = gnews_titles("Sverige", max_items=24)
        for t in extra:
            if len(picked) >= max_total: break
            clean = clean_topic_title(t)
            if clean and clean.lower() not in seen:
                picked.append({"title": clean, "cat_slug": "nyheter", "cat_name": "Nyheter"})
                seen.add(clean.lower())
    return picked

# --------- Main ---------

def main():
    print("🔎 Startar Trendkoll-worker...")
    print("BASE_URL:", WP_BASE_URL, "| USER:", WP_USER)

    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bundles = pick_diverse_topics(max_total=MAX_TRENDS)
    if not bundles:
        print("⚠️ Hittade inga topics. Avbryter.")
        return

    posted_now = set()

    for b in bundles:
        title = b["title"]
        cat   = b["cat_slug"]
        print(f"➡️  [{cat}] {title}")

        if title.lower() in (t.lower() for t in posted_now):
            print("⏭️ Hoppar över (dubblett i samma körning).")
            continue
        if wp_trend_exists_exact(title, within_hours=24):
            print("⏭️ Hoppar över (fanns redan senaste 24h i WP).")
            continue

        snippets = gnews_snippets_sv(title, max_items=4)

        # GPT-5 → 5-mini → no-AI fallback
        try:
            raw_summary = summarize_with_retries(title, snippets)
        except Exception as e2:
            print("❌ OpenAI-fel, kör no-AI fallback:", e2)
            bullets = "\n".join([f"- {s['title']}" for s in snippets[:3]]) if snippets else "- Ingen nyhetskälla tillgänglig"
            raw_summary = f"{bullets}\n\nAffiliate-idéer:\n- Sök efter relaterade produkter/tjänster hos dina partnernätverk."

        summary_html = text_to_html(raw_summary)
        published_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
        points = "".join([
            f"<li><a href='{s['link']}' target='_blank' rel='nofollow noopener'>{escape(s['title'])}</a></li>"
            for s in snippets
        ]) if snippets else "<li>(Inga källor tillgängliga just nu)</li>"

        body = f"""
        <p><em>Publicerad: {published_str} UTC</em></p>
        <div class='tk-summary'>
{summary_html}
        </div>
        <h3>Källor</h3>
        <ul>{points}</ul>
        """

        try:
            res = wp_post_trend(
                title=title,
                body=body,
                topics=["idag", "svenska-trender", date_tag],
                categories=[cat],
                excerpt=raw_summary.replace("\n", " ")[:140]
            )
            posted_now.add(title)
            print("✅ Postad:", res)
            time.sleep(random.uniform(0.8, 1.6))
        except Exception as e:
            print("❌ Fel vid postning till WP:", e)

    print("🏁 Klar körning.")

if __name__ == "__main__":
    main()

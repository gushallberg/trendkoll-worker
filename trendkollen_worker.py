import os, time, random, requests, json, feedparser, urllib.request
from datetime import datetime
from urllib.parse import quote
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WP_BASE_URL    = os.getenv("WP_BASE_URL")
WP_USER        = os.getenv("WP_USER")
WP_APP_PASS    = os.getenv("WP_APP_PASS")
MAX_TRENDS     = int(os.getenv("MAX_TRENDS", "5"))

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

def fetch_rss(url):
    """Hämta RSS med headers och returnera feedparser-parsning."""
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=15)
        r.raise_for_status()
        return feedparser.parse(r.text)
    except Exception as e:
        print("⚠️ RSS-fel på", url, "→", e)
        return feedparser.FeedParserDict(entries=[])

def get_trending_topics(max_items=5):
    """
    Försök i ordning:
    1) Google Trends daily RSS, Sverige (SE)
    2) Google Trends daily RSS, Sverige med engelsk locale
    3) Google Trends daily RSS, USA (US)
    4) Fallback: Google News huvudflöde för Sverige (tar rubriker som topics)
    """
    urls = [
        "https://trends.google.com/trends/trendingsearches/daily/rss?geo=SE",
        "https://trends.google.com/trends/trendingsearches/daily/rss?geo=SE&hl=en-US",
        "https://trends.google.com/trends/trendingsearches/daily/rss?geo=US&hl=en-US",
    ]
    for u in urls:
        feed = fetch_rss(u)
        topics = [e.title for e in feed.entries[:max_items]] if feed.entries else []
        if topics:
            print("✅ Hämtade topics från:", u)
            return topics

    # Sista fallback – Google News Sverige (tar rubriker som topics)
    gnews = fetch_rss("https://news.google.com/rss?hl=sv-SE&gl=SE&ceid=SE:sv")
    topics = [e.title for e in gnews.entries[:max_items]] if gnews.entries else []
    if topics:
        print("✅ Fallback: tog topics från Google News huvudflöde (SE)")
    return topics

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

def openai_summarize(topic, snippets):
    system = (
        "Skriv en kort svensk sammanfattning (120–180 ord) om varför detta ämne trendar just nu. "
        "Ha en tydlig rubrik överst (en rad), följt av 2–3 punktlistor med de viktigaste orsakerna. "
        "Avsluta med 1–2 förslag på relevanta produkter/tjänster som kan länkas som affiliate. "
        "Skriv utan markdown, bara ren text med radbrytningar."
    )
    snip = "; ".join([f"{s['title']} ({s['link']})" for s in snippets]) if snippets else "Inga källsnuttar"

    payload = {
        "model": "gpt-5.1-mini",
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Ämne: {topic}\nNyhetssnuttar: {snip}"}
        ]
    }
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"}
    )
    with urllib.request.urlopen(req) as resp:
        out = resp.read().decode("utf-8")
    j = json.loads(out)
    return j["output"][0]["content"][0]["text"]

def wp_post_trend(title, body, topics=None, excerpt=""):
    url = f"{WP_BASE_URL}/wp-json/trendkollen/v1/ingest"
    payload = {"title": title, "content": body, "excerpt": excerpt, "topics": topics or []}
    resp = requests.post(url, json=payload, auth=(WP_USER, WP_APP_PASS), timeout=30)
    resp.raise_for_status()
    return resp.json()

def main():
    print("🔎 Startar Trendkoll-worker...")
    print("BASE_URL:", WP_BASE_URL, "| USER:", WP_USER)

    topics = get_trending_topics(MAX_TRENDS)
    if not topics:
        print("⚠️ Hittade fortfarande inga topics. Avbryter.")
        return

    for topic in topics:
        print(f"➡️  Ämne: {topic}")
        snippets = gnews_snippets_sv(topic, max_items=4)
        try:
            summary = openai_summarize(topic, snippets)
        except Exception as e:
            print("❌ OpenAI-fel:", e)
            continue

        points = "".join([
            f"<li><a href='{s['link']}' target='_blank' rel='nofollow noopener'>{s['title']}</a></li>"
            for s in snippets
        ]) if snippets else "<li>(Inga källor tillgängliga just nu)</li>"

        body = f"""
        <h2>{topic}</h2>
        <p><em>Publicerad: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC</em></p>
        <div class='tk-summary'>
{summary}
        </div>
        <h3>Källor</h3>
        <ul>{points}</ul>
        """

        try:
            res = wp_post_trend(title=topic, body=body, topics=["idag", "svenska-trender"], excerpt=summary[:140])
            print("✅ Postad:", res)
            time.sleep(random.uniform(0.8, 1.6))
        except Exception as e:
            print("❌ Fel vid postning till WP:", e)

    print("🏁 Klar körning.")

if __name__ == "__main__":
    main()

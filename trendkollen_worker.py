import os, time, random, requests, json, feedparser
from datetime import datetime
from urllib.parse import quote
from pytrends.request import TrendReq
from dotenv import load_dotenv
import urllib.request

# Ladda miljövariabler från Render (eller .env lokalt)
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WP_BASE_URL    = os.getenv("WP_BASE_URL")      # ex: https://trendkoll.se
WP_USER        = os.getenv("WP_USER")          # ditt WP-användarnamn
WP_APP_PASS    = os.getenv("WP_APP_PASS")      # Application Password från WP
MAX_TRENDS     = int(os.getenv("MAX_TRENDS", "5"))

# ---- Helpers ----

def gnews_snippets_sv(query, max_items=3):
    """Hämta korta Google News-snuttar för ett givet ämne"""
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=sv-SE&gl=SE&ceid=SE:sv"
    feed = feedparser.parse(url)
    items = []
    for entry in feed.entries[:max_items]:
        items.append({
            "title": entry.title,
            "link": entry.link,
            "published": entry.get("published", "")
        })
    return items


def openai_summarize(topic, snippets):
    """Skicka ämne + nyhetssnuttar till GPT och få en svensk sammanfattning"""
    system = (
        "Skriv en kort svensk sammanfattning (120–180 ord) om varför detta ämne trendar just nu. "
        "Lista 2–3 viktiga punkter. Föreslå 1–2 möjliga produkter/tjänster som kan länkas som affiliate. "
        "Rubrik + underrubriker. Ingen markdown."
    )
    user = f"Ämne: {topic}\nNyhetssnuttar: {snippets}"

    data = json.dumps({
        "model": "gpt-5.1-mini",
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        }
    )
    with urllib.request.urlopen(req) as resp:
        out = resp.read().decode("utf-8")
    j = json.loads(out)
    return j["output"][0]["content"][0]["text"]


def wp_post_trend(title, body, topics=None, excerpt=""):
    """Posta ett trend-inlägg till WordPress via vårt plugin-endpoint"""
    url = f"{WP_BASE_URL}/wp-json/trendkollen/v1/ingest"
    payload = {
        "title": title,
        "content": body,
        "excerpt": excerpt,
        "topics": topics or []
    }
    resp = requests.post(url, json=payload, auth=(WP_USER, WP_APP_PASS), timeout=30)
    resp.raise_for_status()
    return resp.json()


def main():
    pytrends = TrendReq(hl="sv-SE", tz=60)
    df = pytrends.trending_searches(pn="sweden")
    topics = [t[0] for t in df.values.tolist()][:MAX_TRENDS]

    for topic in topics:
        snippets = gnews_snippets_sv(topic, max_items=4)
        summary = openai_summarize(topic, snippets)

        # Bygg upp innehållet i HTML
        points = "".join([
            f"<li><a href='{s['link']}' target='_blank' rel='nofollow noopener'>{s['title']}</a></li>"
            for s in snippets
        ])
        body = f"""
        <h2>{topic}</h2>
        <p><em>Publicerad: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC</em></p>
        <div class='tk-summary'>{summary}</div>
        <h3>Källor</h3>
        <ul>{points}</ul>
        """

        try:
            res = wp_post_trend(
                title=topic,
                body=body,
                topics=["idag", "svenska-trender"],
                excerpt=summary[:140]
            )
            print("✅ Postad:", res)
            time.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            print("❌ Fel vid postning:", topic, e)


if __name__ == "__main__":
    main()

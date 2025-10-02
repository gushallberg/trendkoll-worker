import os, time, random, requests, json
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from html import unescape
import feedparser
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WP_BASE_URL    = os.getenv("WP_BASE_URL")      # ex: https://trendkoll.se
WP_USER        = os.getenv("WP_USER")          # WP-användare (samma som App Password skapades för)
WP_APP_PASS    = os.getenv("WP_APP_PASS")      # Application Password
MAX_TRENDS     = int(os.getenv("MAX_TRENDS", "5"))

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# ---------- RSS helpers ----------

def fetch_rss(url):
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=15)
        r.raise_for_status()
        return feedparser.parse(r.text)
    except Exception as e:
        print("⚠️ RSS-fel på", url, "→", e)
        return feedparser.FeedParserDict(entries=[])

def get_trending_topics(max_items=5):
    """
    Hämtar rubriker från Google News huvudflöde för Sverige och använder dem som topics.
    Stabilt och enkelt att köra schemalagt.
    """
    feed = fetch_rss("https://news.google.com/rss?hl=sv-SE&gl=SE&ceid=SE:sv")
    topics = [e.title for e in (feed.entries or [])[:max_items]]
    if topics:
        print("✅ Topics från Google News SE")
    else:
        print("⚠️ Inga topics i Google News.")
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

# ---------- OpenAI helper (GPT-5) ----------

def openai_chat_summarize(topic, snippets, model="gpt-5"):
    """
    Chat Completions med GPT-5 (fallback: gpt-5-mini). Skicka INTE temperature för dessa modeller.
    Returnerar ren text utan markdown.
    """
    system = (
        "Du är en svensk nyhetsredaktör. Skriv en kort sammanfattning (120–180 ord) "
        "om varför ämnet trendar just nu. Börja med en rubrik (en rad), skriv sedan "
        "2–3 punktlistor med de viktigaste orsakerna, och avsluta med 1–2 relevanta "
        "affiliate-idéer. Ingen markdown, bara ren text med radbrytningar."
    )
    snip = "; ".join([f"{s['title']} ({s['link']})" for s in snippets]) if snippets else "Inga källsnuttar"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Ämne: {topic}\nNyhetssnuttar: {snip}"}
        ]
        # OBS: ingen 'temperature' – GPT-5/5-mini stödjer endast default
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

# ---------- WordPress helpers ----------

def wp_post_trend(title, body, topics=None, excerpt=""):
    """
    Postar via vårt Trendkollen-endpoint (kräver att pluginet är aktivt i Live).
    """
    url = f"{WP_BASE_URL}/wp-json/trendkollen/v1/ingest"
    payload = {"title": title, "content": body, "excerpt": excerpt, "topics": topics or []}
    resp = requests.post(url, json=payload, auth=(WP_USER, WP_APP_PASS), timeout=30)
    resp.raise_for_status()
    return resp.json()

def wp_trend_exists_exact(title, within_hours=24):
    """
    Kollar via WP REST (CPT: trend) om det redan finns ett inlägg
    med exakt samma titel de senaste 'within_hours' timmarna.
    """
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
            # datum i UTC
            date_gmt = p.get("date_gmt")
            if date_gmt:
                try:
                    dt = datetime.fromisoformat(date_gmt.replace("Z", "+00:00"))
                except Exception:
                    dt = now  # om parse faller
            else:
                dt = now
            age = now - dt
            if age <= timedelta(hours=within_hours):
                return True
    return False

# ---------- Main ----------

def main():
    print("🔎 Startar Trendkoll-worker...")
    print("BASE_URL:", WP_BASE_URL, "| USER:", WP_USER)

    topics = get_trending_topics(MAX_TRENDS)
    if not topics:
        print("⚠️ Hittade inga topics. Avbryter.")
        return

    posted_titles = set()
    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for topic in topics:
        topic_clean = topic.strip()
        if topic_clean.lower() in (t.lower() for t in posted_titles):
            print("⏭️ Hoppar över (dubblett i samma körning):", topic_clean)
            continue

        # Duplikatskydd mot tidigare publicerad titel
        if wp_trend_exists_exact(topic_clean, within_hours=24):
            print("⏭️ Hoppar över (fanns redan senaste 24h i WP):", topic_clean)
            continue

        print(f"➡️  Ämne: {topic_clean}")
        snippets = gnews_snippets_sv(topic_clean, max_items=4)

        # Försök GPT-5 → fallback till 5-mini → sista fallback: no-AI
        try:
            try:
                summary = openai_chat_summarize(topic_clean, snippets, model="gpt-5")
            except Exception as e1:
                print("⚠️ gpt-5 fail, testar gpt-5-mini →", e1)
                summary = openai_chat_summarize(topic_clean, snippets, model="gpt-5-mini")
        except Exception as e2:
            print("❌ OpenAI-fel, kör no-AI fallback:", e2)
            bullets = "\n".join([f"- {s['title']}" for s in snippets[:3]]) if snippets else "- Ingen nyhetskälla tillgänglig"
            summary = (
                f"{topic_clean}\n\n"
                "Viktiga punkter:\n"
                f"{bullets}\n\n"
                "Affiliate-idéer: Sök efter relaterade produkter/tjänster hos dina partnernätverk."
            )

        # HTML-body
        published_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
        if snippets:
            points = "".join([
                f"<li><a href='{s['link']}' target='_blank' rel='nofollow noopener'>{s['title']}</a></li>"
                for s in snippets
            ])
        else:
            points = "<li>(Inga källor tillgängliga just nu)</li>"

        body = f"""
        <h2>{topic_clean}</h2>
        <p><em>Publicerad: {published_str} UTC</em></p>
        <div class='tk-summary'>
{summary}
        </div>
        <h3>Källor</h3>
        <ul>{points}</ul>
        """

        try:
            res = wp_post_trend(
                title=topic_clean,
                body=body,
                topics=["idag", "svenska-trender", date_tag],  # datum-tagg
                excerpt=summary[:140]
            )
            posted_titles.add(topic_clean)
            print("✅ Postad:", res)
            time.sleep(random.uniform(0.8, 1.6))
        except Exception as e:
            print("❌ Fel vid postning till WP:", e)

    print("🏁 Klar körning.")

if __name__ == "__main__":
    main()

import os
import logging
import re
from html import unescape, escape
from datetime import datetime
from time import mktime
from zoneinfo import ZoneInfo
from typing import List, Dict, Set

import feedparser
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ==========================
#        РќРђРЎРўР РћР™РљР
# ==========================

TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("BOT_TOKEN")
    or os.environ.get("TOKEN")
)

CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = os.environ.get("ADMIN_ID")  # РѕРїС†РёРѕРЅР°Р»СЊРЅРѕ

if not TOKEN:
    raise RuntimeError("вќЊ РќРµ РЅР°Р№РґРµРЅ TELEGRAM_BOT_TOKEN / BOT_TOKEN / TOKEN!")

if not CHANNEL_ID:
    raise RuntimeError("вќЊ РќРµ РЅР°Р№РґРµРЅ CHANNEL_ID РІ РїРµСЂРµРјРµРЅРЅС‹С… РѕРєСЂСѓР¶РµРЅРёСЏ!")

TZ = ZoneInfo("Asia/Dushanbe")

NEWS_INTERVAL = int(os.environ.get("NEWS_INTERVAL", "1800"))  # 30 РјРёРЅСѓС‚
MAX_POSTS_PER_RUN = 5

FEED_URLS: List[str] = [
    "https://news.google.com/rss/search?q=РёСЃРєСѓСЃСЃС‚РІРµРЅРЅС‹Р№+РёРЅС‚РµР»Р»РµРєС‚&hl=ru&gl=RU&ceid=RU:ru",
    "https://www.cnews.ru/inc/rss/news_top.xml",  # Р”РѕР±Р°РІР»РµРЅ IT/Tech РёСЃС‚РѕС‡РЅРёРє
]

SENT_URLS_FILE = "sent_urls.json"
sent_urls: Set[str] = set()

DEFAULT_IMAGE = "https://cdn0.tnwcdn.com/wp-content/blogs.dir/1/files/2010/06/News.jpg"  # РІР°С€ fallback


# ==========================
#          Р›РћР“Р
# ==========================

logging.basicConfig(
    format="%(asctime)s вЂ” %(name)s вЂ” %(levelname)s вЂ” %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ai-news-bot")


# ==========================
#     Р’РЎРџРћРњРћР“РђРўР•Р›Р¬РќР«Р•
# ==========================

def clean_html(text: str) -> str:
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r"<.*?>", "", text)
    return text.strip()


def load_sent_urls() -> None:
    import json
    global sent_urls

    if not os.path.exists(SENT_URLS_FILE):
        sent_urls = set()
        return

    try:
        with open(SENT_URLS_FILE, "r", encoding="utf-8") as f:
            sent_urls = set(json.load(f))
        logger.info("Р—Р°РіСЂСѓР¶РµРЅРѕ %d РѕР±СЂР°Р±РѕС‚Р°РЅРЅС‹С… СЃСЃС‹Р»РѕРє.", len(sent_urls))
    except Exception as e:
        logger.exception("РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ %s: %s", SENT_URLS_FILE, e)
        sent_urls = set()


def save_sent_urls() -> None:
    import json
    try:
        with open(SENT_URLS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(sent_urls), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("РћС€РёР±РєР° СЃРѕС…СЂР°РЅРµРЅРёСЏ СЃСЃС‹Р»РѕРє: %s", e)


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not ADMIN_ID:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"вљ пёЏ {text}")
    except Exception:
        logger.exception("РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РїСЂР°РІРёС‚СЊ СЃРѕРѕР±С‰РµРЅРёРµ Р°РґРјРёРЅСѓ.")


# ==========================
#      РџРђР РЎРРќР“ РќРћР’РћРЎРўР•Р™
# ==========================

def fetch_news() -> List[Dict]:
    items: List[Dict] = []

    for feed_url in FEED_URLS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                link = entry.get("link")
                if not link or link in sent_urls:
                    continue

                title = entry.get("title", "").strip()
                summary = (entry.get("summary", "") or entry.get("description", ""))
                summary = summary.split("<br")[0]  # СѓР±РёСЂР°РµРј РїРѕРІС‚РѕСЂС‹ РёР· Google News

                # Р±РµСЂС‘Рј РїРѕРїС‹С‚РєСѓ РєР°СЂС‚РёРЅРєРё
                image = (
                    entry.get("media_content", [{}])[0].get("url")
                    or entry.get("media_thumbnail", [{}])[0].get("url")
                    or DEFAULT_IMAGE
                )

                items.append(
                    {
                        "title": clean_html(title),
                        "summary": clean_html(summary),
                        "url": link,
                        "image": image,
                    }
                )

        except Exception as e:
            logger.exception("РћС€РёР±РєР° RSS %s: %s", feed_url, e)

    return items


def normalize_for_compare(text: str) -> str:
    s = text.lower()
    s = re.sub(r"\b[\w.-]+\.(ru|com|org|net|io|ai|info|biz)\b", "", s)
    s = re.sub(r"\s[-вЂ“вЂ”]\s.*$", "", s)
    s = re.sub(r"[^a-zР°-СЏ0-9С‘\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def jaccard_similarity(a: str, b: str) -> float:
    set_a = set(a.split())
    set_b = set(b.split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def build_body_text(title: str, summary: str) -> str:
    title_clean = clean_html(title)
    summary_clean = clean_html(summary)

    if not summary_clean:
        return ""

    t_norm = normalize_for_compare(title_clean)
    s_norm = normalize_for_compare(summary_clean)

    if not t_norm or not s_norm or t_norm == s_norm:
        return ""

    big, small = (t_norm, s_norm) if len(t_norm) >= len(s_norm) else (s_norm, t_norm)
    if small in big and len(small) / len(big) >= 0.7:
        return ""

    if jaccard_similarity(t_norm, s_norm) >= 0.8:
        return ""

    return summary_clean


def build_post_text(title: str, body: str, url: str) -> str:
    safe_title = escape(title)
    safe_body = escape(body)
    safe_url = escape(url, quote=True)

    return (
        f"рџ§  <b>{safe_title}</b>\n\n"
        f"{safe_body}\n\n"
        f'<a href="{safe_url}">РСЃС‚РѕС‡РЅРёРє</a>'
    )


# ==========================
#      JOB: РќРћР’РћРЎРўР
# ==========================

async def periodic_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("РџСЂРѕРІРµСЂСЏРµРј РЅРѕРІРѕСЃС‚РёвЂ¦")

    try:
        news = fetch_news()
        if not news:
            logger.info("РЎРІРµР¶РёС… РЅРѕРІРѕСЃС‚РµР№ РЅРµС‚.")
            return

        count = 0

        for item in news:
            if count >= MAX_POSTS_PER_RUN:
                break

            url = item["url"]
            title = item["title"]
            summary = item["summary"]
            image = item["image"]

            if url in sent_urls:
                continue

            body = build_body_text(title, summary)
            if not body:  # РЅРµС‚ РЅРѕСЂРјР°Р»СЊРЅРѕРіРѕ РѕРїРёСЃР°РЅРёСЏ
                sent_urls.add(url)
                save_sent_urls()
                continue

            post = build_post_text(title, body, url)

            try:
                await context.bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=image,
                    caption=post,
                    parse_mode=ParseMode.HTML
                )
                logger.info("РћС‚РїСЂР°РІР»РµРЅР° РЅРѕРІРѕСЃС‚СЊ: %s", url)

                sent_urls.add(url)
                save_sent_urls()
                count += 1

            except Exception as e:
                logger.exception("РћС€РёР±РєР° РѕС‚РїСЂР°РІРєРё РїРѕСЃС‚Р°: %s", e)
                await notify_admin(context, f"РћС€РёР±РєР° РѕС‚РїСЂР°РІРєРё РїРѕСЃС‚Р°: {e}")

    except Exception as e:
        logger.exception("РћС€РёР±РєР° periodic_news: %s", e)
        await notify_admin(context, f"РћС€РёР±РєР° periodic_news: {e}")


# ==========================
#         HANDLERS
# ==========================

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return

    await update.effective_chat.send_message(
        "рџ‘‹ РџСЂРёРІРµС‚!\n"
        "Р­С‚Рѕ Р±РѕС‚ РЅРѕРІРѕСЃС‚РµР№ РїСЂРѕ РСЃРєСѓСЃСЃС‚РІРµРЅРЅС‹Р№ РРЅС‚РµР»Р»РµРєС‚.\n"
        "вњ” РўРѕР»СЊРєРѕ СѓРЅРёРєР°Р»СЊРЅРѕРµ РѕРїРёСЃР°РЅРёРµ\n"
        "вњ” Р‘РµР· РґСѓР±Р»РµР№\n"
        "вњ” РЎ РєР°СЂС‚РёРЅРєР°РјРё рџЋ"
    )


# ==========================
#          MAIN
# ==========================

def main() -> None:
    logger.info("Р—Р°РїСѓСЃРє ai-news-workerвЂ¦")
    load_sent_urls()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_handler))

    app.job_queue.run_repeating(
        periodic_news,
        interval=NEWS_INTERVAL,
        first=10,
        name="periodic_news",
    )

    app.run_polling()


if __name__ == "__main__":
    main()


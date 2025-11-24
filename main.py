import os
import logging
import re
import json
from html import unescape, escape
from datetime import datetime, time
from time import mktime
from zoneinfo import ZoneInfo
from typing import Dict, List, Set

import feedparser
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
)

# ---------------------- НАСТРОЙКИ ----------------------

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID") or None

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN / TOKEN")

if not CHANNEL_ID:
    raise RuntimeError("Не задан CHANNEL_ID")

NEWS_INTERVAL = int(os.getenv("NEWS_INTERVAL", "1800"))  # 30 мин
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Dushanbe"))

SENT_URLS_FILE = "sent_urls.json"

FEEDS = [
    "https://nplus1.ru/rss",
    "https://habr.com/ru/rss/hub/machine_learning/all/",
    "https://habr.com/ru/rss/hub/artificial_intelligence/all/",
    "https://ai.googleblog.com/feeds/posts/default?alt=rss",
    "https://openai.com/blog/rss.xml",
]

TODAY_NEWS: List[Dict] = []
SENT_URLS: Set[str] = set()

# ---------------------- ЛОГИ ----------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("AI-NEWS")


# ---------------------- ХЕЛПЕРЫ ----------------------

def clean_html(text: str) -> str:
    if not text:
        return ""
    text = unescape(text)
    return re.sub(r"<.*?>", "", text).strip()


def load_sent_urls():
    global SENT_URLS
    if not os.path.exists(SENT_URLS_FILE):
        SENT_URLS = set()
        return
    try:
        with open(SENT_URLS_FILE, "r", encoding="utf-8") as f:
            SENT_URLS = set(json.load(f))
            logger.info(f"Загружено отправленных ссылок: {len(SENT_URLS)}")
    except Exception as e:
        logger.error(f"Ошибка чтения sent_urls.json: {e}")
        SENT_URLS = set()


def save_sent_urls():
    try:
        with open(SENT_URLS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(SENT_URLS), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка записи sent_urls.json: {e}")


# ---------------------- ПАРСИНГ RSS ----------------------

def parse_entry(feed_title: str, entry) -> Dict:
    title = entry.get("title", "").strip()
    summary = entry.get("summary", "") or entry.get("description", "")
    link = entry.get("link", "")

    published = entry.get("published_parsed") or entry.get("updated_parsed")
    if published:
        dt = datetime.fromtimestamp(mktime(published), tz=TZ)
    else:
        dt = datetime.now(TZ)

    # Поиск картинки
    image = ""
    content = ""
    if "content" in entry and entry.content:
        content = " ".join([c.value for c in entry.content if hasattr(c, "value")])
    else:
        content = summary or ""

    m = re.search(r'src="([^"]+)"', content)
    if m:
        image = m.group(1)

    return {
        "title": title or feed_title,
        "summary": summary,
        "url": link,
        "image": image,
        "date": dt.date().isoformat(),
    }


def fetch_news() -> List[Dict]:
    items = []

    for url in FEEDS:
        try:
            parsed = feedparser.parse(url)
            feed_title = parsed.feed.get("title", "Источник")

            for entry in parsed.entries:
                link = entry.get("link")
                if not link or link in SENT_URLS:
                    continue
                items.append(parse_entry(feed_title, entry))

        except Exception as e:
            logger.error(f"Ошибка парсинга {url}: {e}")

    items.sort(key=lambda x: x["date"], reverse=True)
    return items


# ---------------------- ФОРМИРОВАНИЕ ПОСТА ----------------------

def build_body(title: str, summary: str) -> str:
    title_clean = clean_html(title)
    summary_clean = clean_html(summary)

    if summary_clean and summary_clean.lower() != title_clean.lower():
        return summary_clean
    return ""


def build_post(item: Dict) -> str:
    title = clean_html(item["title"])
    body = build_body(item["title"], item["summary"])
    url = escape(item["url"], quote=True)

    parts = []
    parts.append(f"🧠 <b>{escape(title)}</b>")

    if body:
        parts.append("")
        body = escape(body)
        if len(body) > 3500:
            body = body[:3490] + "…"
        parts.append(body)

    parts.append("")
    parts.append(f'🔗 <a href="{url}">Источник</a>')

    return "\n".join(parts)


# ---------------------- JOBS ----------------------

async def periodic_news_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Запуск сбора новостей")
    try:
        items = fetch_news()
        for item in items:
            url = item["url"]
            if url in SENT_URLS:
                continue

            post_text = build_post(item)

            try:
                if item["image"]:
                    await context.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=item["image"],
                        caption=post_text,
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=post_text,
                        parse_mode=ParseMode.HTML
                    )

                SENT_URLS.add(url)
                save_sent_urls()

                TODAY_NEWS.append({
                    "date": item["date"],
                    "title": item["title"],
                    "url": item["url"]
                })

            except Exception as e:
                logger.error(f"Ошибка отправки: {e}")
                if ADMIN_CHAT_ID:
                    await context.bot.send_message(ADMIN_CHAT_ID, f"❗ Ошибка отправки: {e}")

    except Exception as e:
        logger.error(f"Ошибка periodic_news_job: {e}")


async def daily_digest_job(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ).date().isoformat()
    items = [n for n in TODAY_NEWS if n["date"] == today]

    if not items:
        logger.info("Сегодня новостей нет — дайджест не отправляем")
        return

    lines = ["🌙 <b>Вечерний дайджест ИИ</b>", ""]

    for i, n in enumerate(items, 1):
        lines.append(f'{i}. <a href="{escape(n["url"], quote=True)}">{escape(clean_html(n["title"]))}</a>')

    msg = "\n".join(lines)

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=msg,
        parse_mode=ParseMode.HTML
    )

    TODAY_NEWS.clear()


# ---------------------- HANDLERS ----------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот, который публикует новости об искусственном интеллекте в канал."
    )


# ---------------------- MAIN ----------------------

def main():
    logger.info("Запуск AI News")
    load_sent_urls()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    job = app.job_queue
    job.run_repeating(periodic_news_job, interval=NEWS_INTERVAL, first=30)
    job.run_daily(daily_digest_job, time=time(21, 0, tzinfo=TZ))

    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()

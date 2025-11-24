import os
import logging
import re
from html import unescape, escape
from time import mktime
from datetime import datetime
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
#        НАСТРОЙКИ
# ==========================

TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("BOT_TOKEN")
    or os.environ.get("TOKEN")
)

CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = os.environ.get("ADMIN_ID")

if not TOKEN:
    raise RuntimeError("❌ Не найден TELEGRAM_BOT_TOKEN!")

if not CHANNEL_ID:
    raise RuntimeError("❌ Не найден CHANNEL_ID!")

TZ = ZoneInfo("Asia/Dushanbe")

# Интервал проверки новостей (секунды)
NEWS_INTERVAL = 1800  # 30 минут

# RSS-источники
FEED_URLS = [
    "https://news.yandex.ru/computers.rss",
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
]

# файл сохранения отправленных ссылок
SENT_URLS_FILE = "sent_urls.json"
sent_urls: Set[str] = set()

# ==========================
#          ЛОГИ
# ==========================

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ai-news-bot")


# ==========================
#     ВСПОМОГАТЕЛЬНЫЕ
# ==========================

def clean_html(text: str) -> str:
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r"<.*?>", "", text)
    return text.strip()


def load_sent_urls() -> None:
    """Загрузить отправленные ссылки."""
    import json
    global sent_urls

    if not os.path.exists(SENT_URLS_FILE):
        sent_urls = set()
        return

    try:
        with open(SENT_URLS_FILE, "r", encoding="utf-8") as f:
            sent_urls = set(json.load(f))
        logger.info("Загружено %d отправленных ссылок.", len(sent_urls))
    except:
        sent_urls = set()


def save_sent_urls() -> None:
    """Сохранить отправленные ссылки."""
    import json
    try:
        with open(SENT_URLS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(sent_urls), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Ошибка сохранения ссылок: %s", e)


async def notify_admin(context, text: str):
    if ADMIN_ID:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ {text}")


# ==========================
#      ПАРСИНГ НОВОСТЕЙ
# ==========================

def fetch_news() -> List[Dict]:
    items = []

    for url in FEED_URLS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                link = entry.get("link")
                if not link or link in sent_urls:
                    continue

                title = entry.get("title", "").strip()
                summary = entry.get("summary", "") or entry.get("description", "")

                items.append(
                    {
                        "title": clean_html(title),
                        "summary": clean_html(summary),
                        "url": link,
                    }
                )
        except Exception as e:
            logger.exception("Ошибка RSS %s: %s", url, e)

    return items


def build_news_text(item: Dict) -> str:
    title = item["title"]
    summary = item["summary"]

    # убрать дубли
    if summary.lower().startswith(title.lower()):
        summary = ""

    if not summary:
        return title

    return summary


def build_post_text(item: Dict) -> str:
    title = escape(item["title"])
    body = escape(build_news_text(item))
    url = escape(item["url"], quote=True)

    return (
        f"🧠 <b>{title}</b>\n\n"
        f"{body}\n\n"
        f"🔗 <a href=\"{url}\">Источник</a>"
    )


# ==========================
#      JOB: НОВОСТИ
# ==========================

async def periodic_news(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Проверяем новости...")

    try:
        news = fetch_news()

        if not news:
            logger.info("Нет новых новостей.")
            return

        for item in news:
            url = item["url"]

            if url in sent_urls:
                continue

            post = build_post_text(item)

            try:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=post,
                    parse_mode=ParseMode.HTML,
                )
                logger.info("Отправил: %s", url)
                sent_urls.add(url)
                save_sent_urls()

            except Exception as e:
                logger.exception("Ошибка отправки поста: %s", e)
                await notify_admin(context, f"Ошибка отправки поста: {e}")

    except Exception as e:
        logger.exception("Ошибка periodic_news: %s", e)
        await notify_admin(context, f"Ошибка periodic_news: {e}")


# ==========================
#         HANDLERS
# ==========================

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет!\nЭто новостной бот ИИ.\nОн будет публиковать свежие новости в канал каждые 30 минут."
    )


# ==========================
#          MAIN
# ==========================

def main():
    logger.info("Запуск бота…")
    load_sent_urls()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))

    # периодический запуск
    app.job_queue.run_repeating(
        periodic_news,
        interval=NEWS_INTERVAL,
        first=10,
    )

    app.run_polling()


if __name__ == "__main__":
    main()

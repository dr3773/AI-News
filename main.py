import os
import logging
import re
from html import escape
from time import mktime
from datetime import time, datetime
from zoneinfo import ZoneInfo
from typing import Dict

import feedparser
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# -------------------- НАСТРОЙКИ --------------------

TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")

if not TOKEN:
    raise RuntimeError("❌ Не задан BOT_TOKEN в переменных окружения!")

if not CHANNEL_ID:
    raise RuntimeError("❌ Не задан CHANNEL_ID в переменных окружения!")

FEED_URLS = [
    "https://news.yandex.ru/computers.rss",
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
]

TZ = ZoneInfo("Asia/Dushanbe")
NEWS_INTERVAL = 1800  # каждые 30 минут

sent_urls = set()

# ----------------------------------------------------

logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ai-news-bot")


# ----------------------------------------------------
# ФУНКЦИЯ: создать текст новости
# ----------------------------------------------------
def build_news_text(title: str, summary: str) -> str:
    """
    Генерируем аккуратный текст новости, без бардака.
    - Нет повторов заголовка
    - Нормальный короткий смысловой текст
    - Без шаблонного мусора
    """

    title_clean = title.strip()
    summary_clean = summary.strip()

    # Если summary повторяет title — не используем его
    if summary_clean.lower().startswith(title_clean.lower()):
        summary_clean = ""

    # Если вообще нет summary — просто заголовок
    if not summary_clean:
        return title_clean

    # Нормальный короткий текст
    return f"{summary_clean}"


# ----------------------------------------------------
# ФУНКЦИЯ: создать пост в Telegram
# ----------------------------------------------------
def build_post_text(item: Dict) -> str:
    title = item["title"]
    summary = item["summary"]
    url = item["url"]

    body = build_news_text(title, summary)

    safe_title = escape(title)
    safe_body = escape(body)
    safe_url = escape(url, quote=True)

    text = (
        f"🧠 <b>{safe_title}</b>\n\n"
        f"{safe_body}\n\n"
        f"🔗 <a href=\"{safe_url}\">Источник</a>"
    )

    return text


# ----------------------------------------------------
# Получение новостей
# ----------------------------------------------------
def fetch_news():
    result = []

    for feed_url in FEED_URLS:
        feed = feedparser.parse(feed_url)

        for entry in feed.entries:
            link = entry.get("link")
            if not link or link in sent_urls:
                continue

            title = entry.get("title", "").strip()
            summary = entry.get("summary", "").strip()

            result.append({
                "title": title,
                "summary": summary,
                "url": link,
            })

    return result


# ----------------------------------------------------
# ПЕРИОДИЧЕСКАЯ ПРОВЕРКА НОВОСТЕЙ
# ----------------------------------------------------
async def periodic_news(context: ContextTypes.DEFAULT_TYPE):
    try:
        news_list = fetch_news()

        for item in news_list:
            url = item["url"]

            if url in sent_urls:
                continue

            sent_urls.add(url)

            post = build_post_text(item)

            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=post,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )

            logger.info(f"Отправлена новость: {url}")

    except Exception as e:
        logger.error(f"Ошибка в periodic_news: {e}")


# ----------------------------------------------------
# Команда /start
# ----------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот новостей об искусственном интеллекте.\n"
        "Новости публикуются автоматически весь день."
    )


# ----------------------------------------------------
# ОСНОВНОЙ ЗАПУСК
# ----------------------------------------------------
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    # Периодическая публикация новостей
    app.job_queue.run_repeating(
        periodic_news,
        interval=NEWS_INTERVAL,
        first=20,  # первая проверка через 20 сек
    )

    # Просто polling
    app.run_polling()


# ----------------------------------------------------

if __name__ == "__main__":
    main()

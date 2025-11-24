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

# Берём токен из TELEGRAM_BOT_TOKEN (как у тебя в Render),
# а BOT_TOKEN / TOKEN — как запасные варианты.
TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("BOT_TOKEN")
    or os.environ.get("TOKEN")
)

CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = os.environ.get("ADMIN_ID")

if not TOKEN:
    raise RuntimeError("❌ Не найден TELEGRAM_BOT_TOKEN / BOT_TOKEN / TOKEN!")

if not CHANNEL_ID:
    raise RuntimeError("❌ Не найден CHANNEL_ID!")

# Часовой пояс (если понадобится по времени)
TZ = ZoneInfo("Asia/Dushanbe")

# Интервал проверки новостей (секунды)
NEWS_INTERVAL = int(os.environ.get("NEWS_INTERVAL", "1800"))  # 30 минут

# 🔹 МАКСИМУМ 5 НОВОСТЕЙ ЗА ОДИН ЦИКЛ
MAX_POSTS_PER_RUN = 5

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
    """Убираем HTML-теги и лишние пробелы."""
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r"<.*?>", "", text)
    return text.strip()


def load_sent_urls() -> None:
    """Загрузить отправленные ссылки из файла."""
    import json
    global sent_urls

    if not os.path.exists(SENT_URLS_FILE):
        sent_urls = set()
        return

    try:
        with open(SENT_URLS_FILE, "r", encoding="utf-8") as f:
            sent_urls = set(json.load(f))
        logger.info("Загружено %d отправленных ссылок.", len(sent_urls))
    except Exception as e:
        logger.exception("Не удалось загрузить %s: %s", SENT_URLS_FILE, e)
        sent_urls = set()


def save_sent_urls() -> None:
    """Сохранить отправленные ссылки."""
    import json
    try:
        with open(SENT_URLS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(sent_urls), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Ошибка сохранения ссылок: %s", e)


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Отправить сообщение админу, если указан ADMIN_ID."""
    if not ADMIN_ID:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ {text}")
    except Exception:
        logger.exception("Не удалось отправить сообщение админу.")


# ==========================
#      ПАРСИНГ НОВОСТЕЙ
# ==========================


def fetch_news() -> List[Dict]:
    """Читаем RSS-ленты и собираем новости, которых ещё не отправляли."""
    items: List[Dict] = []

    for feed_url in FEED_URLS:
        try:
            feed = feedparser.parse(feed_url)
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
            logger.exception("Ошибка RSS %s: %s", feed_url, e)

    return items


def build_body_text(title: str, summary: str) -> str:
    """
    Формируем текст описания новости.
    ВАЖНО: если нормального описания нет — возвращаем ПУСТУЮ строку.
    То есть заголовок в тексте не дублируем.
    """
    title_clean = clean_html(title)
    summary_clean = clean_html(summary)

    # Если summary пустое или начинается с заголовка — считаем его бесполезным
    if not summary_clean:
        return ""

    if summary_clean.lower().startswith(title_clean.lower()):
        return ""

    return summary_clean


def build_post_text(item: Dict) -> str:
    """Собираем финальный текст поста для Telegram."""
    title = item["title"]
    summary = item["summary"]
    url = item["url"]

    body = build_body_text(title, summary)

    safe_title = escape(title)
    safe_url = escape(url, quote=True)

    lines = [f"🧠 <b>{safe_title}</b>"]

    # Добавляем описание только если оно есть и НЕ дублирует заголовок
    if body:
        safe_body = escape(body)
        lines.append("")
        lines.append(safe_body)

    lines.append("")
    lines.append(f'🔗 <a href="{safe_url}">Источник</a>')

    return "\n".join(lines)


# ==========================
#      JOB: НОВОСТИ
# ==========================


async def periodic_news(context: ContextTypes.DEFAULT_TYPE):
    """Периодическая проверка новостей и отправка новых постов в канал."""
    logger.info("Проверяем новости…")

    try:
        news = fetch_news()

        if not news:
            logger.info("Свежих новостей нет.")
            return

        count = 0  # сколько уже отправили за этот цикл

        for item in news:
            if count >= MAX_POSTS_PER_RUN:
                logger.info("Достигнут лимит %d постов за цикл.", MAX_POSTS_PER_RUN)
                break

            url = item["url"]
            if url in sent_urls:
                continue

            post = build_post_text(item)

            try:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=post,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
                logger.info("Отправлена новость: %s", url)

                sent_urls.add(url)
                save_sent_urls()
                count += 1

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
        "👋 Привет!\n"
        "Это новостной бот об искусственном интеллекте.\n"
        "Он публикует до 5 свежих новостей за один цикл без спама и дублей заголовка."
    )


# ==========================
#          MAIN
# ==========================


def main():
    logger.info("Запуск ai-news-worker…")
    load_sent_urls()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))

    # периодический запуск
    app.job_queue.run_repeating(
        periodic_news,
        interval=NEWS_INTERVAL,
        first=10,  # первая проверка через 10 секунд
        name="periodic_news",
    )

    app.run_polling()


if __name__ == "__main__":
    main()

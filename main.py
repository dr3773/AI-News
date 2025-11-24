import os
import logging
import re
from html import unescape, escape
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

# Токен бота и ID канала берем из переменных окружения Render
TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("BOT_TOKEN")
    or os.environ.get("TOKEN")
)

CHANNEL_ID = os.environ.get("CHANNEL_ID")  # у тебя: -1003238891648
ADMIN_ID = os.environ.get("ADMIN_ID")      # можно оставить пустым

if not TOKEN:
    raise RuntimeError("❌ Не найден TELEGRAM_BOT_TOKEN (или BOT_TOKEN / TOKEN) в переменных окружения!")

if not CHANNEL_ID:
    raise RuntimeError("❌ Не задан CHANNEL_ID в переменных окружения!")

# Интервал проверки новостей (секунды)
NEWS_INTERVAL = int(os.environ.get("NEWS_INTERVAL", "1800"))  # по умолчанию 30 минут

# RSS-источники новостей по ИИ (Яндекс удалён)
FEED_URLS: List[str] = [
    # Google News по запросу "искусственный интеллект" (RU)
    "https://news.google.com/rss/search?q=%D0%B8%D1%81%D0%BA%D1%83%D1%81%D1%81%D1%82%D0%B2%D0%B5%D0%BD%D0%BD%D1%8B%D0%B9+%D0%B8%D0%BD%D1%82%D0%B5%D0%BB%D0%BB%D0%B5%D0%BA%D1%82&hl=ru&gl=RU&ceid=RU:ru",

    # Habr — Machine Learning
    "https://habr.com/ru/rss/hub/machine_learning/all/",

    # Habr — Искусственный интеллект
    "https://habr.com/ru/rss/hub/artificial_intelligence/all/",

    # Блог Google AI
    "https://ai.googleblog.com/feeds/posts/default?alt=rss",

    # Блог OpenAI
    "https://openai.com/blog/rss.xml",
]

# файл, где храним уже отправленные ссылки (чтобы не было дублей после рестарта)
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
    """Убираем HTML-теги (<a>, <font> и т.п.) и лишние пробелы."""
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
    """Сохранить отправленные ссылки в файл."""
    import json
    try:
        with open(SENT_URLS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(sent_urls), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Не удалось сохранить %s: %s", SENT_URLS_FILE, e)


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
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
    """
    Читаем все RSS-ленты и собираем новости.
    Для Google News в summary идёт <description> с HTML — мы его чистим.
    """
    items: List[Dict] = []

    for feed_url in FEED_URLS:
        try:
            feed = feedparser.parse(feed_url)

            for entry in feed.entries:
                link = entry.get("link")
                if not link or link in sent_urls:
                    continue

                raw_title = entry.get("title", "") or ""
                raw_summary = entry.get("summary", "") or entry.get("description", "") or ""

                title = clean_html(raw_title)
                summary = clean_html(raw_summary)

                items.append(
                    {
                        "title": title,
                        "summary": summary,
                        "url": link,
                    }
                )
        except Exception as e:
            logger.exception("Ошибка при чтении %s: %s", feed_url, e)

    return items


def build_news_text(title: str, summary: str) -> str:
    """
    Формируем текст новости:
    - если есть summary, который не совпадает полностью с заголовком, используем его;
    - если summary нет или он идентичен заголовку — оставляем только заголовок.
    Так мы как раз берём описание из Google <description>, где есть
    'Заголовок  Источник'.
    """
    title_clean = title.strip()
    summary_clean = summary.strip()

    if summary_clean and summary_clean.lower() != title_clean.lower():
        return summary_clean

    return title_clean


def build_post_text(item: Dict) -> str:
    """Собираем финальный текст поста для Telegram."""
    title = item["title"]
    summary = item["summary"]
    url = item["url"]

    body_text = build_news_text(title, summary)

    safe_title = escape(title)
    safe_body = escape(body_text)
    safe_url = escape(url, quote=True)

    if len(safe_body) > 3500:
        safe_body = safe_body[:3490] + "…"

    lines: List[str] = []

    # Заголовок
    lines.append(f"🧠 <b>{safe_title}</b>")

    # Описание (если оно не пустое)
    if safe_body and safe_body.lower() != safe_title.lower():
        lines.append("")
        lines.append(safe_body)

    # Ссылка
    lines.append("")
    lines.append(f'🔗 <a href="{safe_url}">Источник</a>')

    return "\n".join(lines)


# ==========================
#      JOB: НОВОСТИ
# ==========================

async def periodic_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодически проверяем новости и отправляем новые в канал."""
    logger.info("Проверяем новости...")

    try:
        news_items = fetch_news()

        if not news_items:
            logger.info("Свежих новостей нет.")
            return

        for item in news_items:
            url = item["url"]
            if url in sent_urls:
                continue

            text = build_post_text(item)

            try:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
                logger.info("Отправлена новость: %s", url)

                sent_urls.add(url)
                save_sent_urls()

            except Exception as e:
                logger.exception("Ошибка отправки новости: %s", e)
                await notify_admin(context, f"Ошибка отправки новости: {e}")

    except Exception as e:
        logger.exception("Ошибка в periodic_news: %s", e)
        await notify_admin(context, f"Ошибка в periodic_news: {e}")


# ==========================
#         HANDLERS
# ==========================

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start в личке с ботом."""
    if update.effective_chat is None:
        return

    await update.effective_chat.send_message(
        "👋 Привет! Я бот с новостями об искусственном интеллекте.\n\n"
        "• Я собираю свежие новости из разных источников (Google News, Habr, Google AI, OpenAI).\n"
        "• Публикую их в канале в коротком и понятном формате."
    )


# ==========================
#          MAIN
# ==========================

def main() -> None:
    logger.info("Запуск ai-news-bot")

    load_sent_urls()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))

    # Периодическая проверка новостей
    app.job_queue.run_repeating(
        periodic_news,
        interval=NEWS_INTERVAL,
        first=15,  # первая проверка через 15 секунд после старта
        name="periodic_news",
    )

    # Никакого asyncio.run, run_polling сам управляет циклом
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()

import os
import logging
import re
from html import unescape, escape
from time import mktime
from datetime import time
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

# ------------------- ЛОГИ -------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ai-news-bot")

# ------------------- НАСТРОЙКИ -------------------

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID_ENV = os.getenv("CHANNEL_ID")
ADMIN_ID_ENV = os.getenv("ADMIN_ID")

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID_ENV:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID_ENV)
ADMIN_ID = int(ADMIN_ID_ENV) if ADMIN_ID_ENV else None

TZ = ZoneInfo("Asia/Dushanbe")

# Google News по ИИ в разных формулировках
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=нейросети&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=машинное+обучение&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=генеративный+ИИ&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=en&gl=US&ceid=US:en",
]

# Файл, куда пишем уже отправленные ссылки
SENT_URLS_FILE = "sent_urls.txt"

# В памяти – уже отправленные за текущий запуск
SENT_URLS: Set[str] = set()

# Для вечернего дайджеста
TODAY_NEWS: List[Dict[str, str]] = []

# Через сколько секунд проверять новости
NEWS_INTERVAL = 45 * 60  # 45 минут

_html_tag_re = re.compile(r"<[^>]+>")
_cyr_re = re.compile(r"[А-Яа-яЁё]")


# ------------------- ВСПОМОГАТЕЛЬНЫЕ -------------------

def load_sent_urls() -> None:
    global SENT_URLS
    if not os.path.exists(SENT_URLS_FILE):
        SENT_URLS = set()
        return
    try:
        with open(SENT_URLS_FILE, "r", encoding="utf-8") as f:
            SENT_URLS = {line.strip() for line in f if line.strip()}
    except Exception as e:
        logger.warning("Не удалось загрузить список отправленных ссылок: %s", e)
        SENT_URLS = set()


def save_sent_urls() -> None:
    try:
        with open(SENT_URLS_FILE, "w", encoding="utf-8") as f:
            for url in sorted(SENT_URLS):
                f.write(url + "\n")
    except Exception as e:
        logger.warning("Не удалось сохранить список отправленных ссылок: %s", e)


def clean_html(text: str | None) -> str:
    if not text:
        return ""
    text = unescape(text)
    text = _html_tag_re.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_image(entry) -> str | None:
    media = getattr(entry, "media_content", None)
    if media and isinstance(media, list):
        url = media[0].get("url")
        if url:
            return url

    links = getattr(entry, "links", [])
    for l in links:
        if l.get("type", "").startswith("image/") and l.get("href"):
            return l["href"]

    return None


def fetch_new_articles(limit: int = 5) -> List[Dict]:
    """Собирает новые новости (которых ещё не было в SENT_URLS)."""
    items: List[Dict] = []

    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            logger.warning("Ошибка парсинга RSS %s: %s", feed_url, e)
            continue

        feed_title = parsed.feed.get("title", "Новости ИИ")

        for entry in parsed.entries:
            link = entry.get("link")
            if not link or link in SENT_URLS:
                continue

            title = clean_html(entry.get("title", ""))
            summary = clean_html(entry.get("summary") or entry.get("description") or "")

            if not title and not summary:
                continue

            published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
            ts = mktime(published_parsed) if published_parsed else 0

            items.append(
                {
                    "title": title,
                    "summary": summary,
                    "url": link,
                    "source": feed_title,
                    "image": extract_image(entry),
                    "ts": ts,
                }
            )

    if not items:
        return []

    # Сортировка: сначала более новые
    items.sort(key=lambda x: x["ts"], reverse=True)

    # Отбираем только N новых и обновляем SENT_URLS
    result: List[Dict] = []
    for item in items:
        if len(result) >= limit:
            break
        if item["url"] in SENT_URLS:
            continue
        SENT_URLS.add(item["url"])
        result.append(item)

    save_sent_urls()
    return result


def build_body_text(title: str, summary: str) -> str:
    """
    Простейшая "расширенная" версия текста:
    - если summary есть и он не копия заголовка – используем его;
    - если summary короткий – добавим чуть контекста.
    Без OpenAI, чтобы не ловить лишние ошибки.
    """
    title_lower = title.lower().strip()
    summary_lower = summary.lower().strip()

    if summary and summary_lower != title_lower:
        base = summary
    else:
        base = title

    # Если текста совсем мало – чуть расширим шаблонно
    if len(base) < 120:
        body = f"{base} Это одна из свежих новостей в сфере искусственного интеллекта. " \
               f"Специалистам и инвесторам стоит держать такие события в поле зрения, " \
               f"так как они отражают текущие тенденции развития ИИ."
    else:
        body = base

    return body


def build_post_text(item: Dict) -> str:
    """
    Собираем текст для поста в HTML-формате:
    🧠 <жирный заголовок>
    <текст новости>
    ➜ Источник (кликабельная ссылка)
    """
    title = item["title"]
    summary = item["summary"]
    url = item["url"]

    body = build_body_text(title, summary)

    safe_title = escape(title)
    safe_body = escape(body)
    safe_url = escape(url, quote=True)

    # ограничим длину, чтобы Телеграм не ругался
    if len(safe_body) > 3500:
        safe_body = safe_body[:3490] + "…"

    parts = [
        f"🧠 <b>{safe_title}</b>",
        "",
        safe_body,
        "",
        f'<a href="{safe_url}">➜ Источник</a>',
    ]
    return "\n".join(parts)


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if ADMIN_ID is None:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ AI News: {text}")
    except Exception:
        logger.exception("Не удалось отправить сообщение админу")


# ------------------- JOBS -------------------

async def periodic_news_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодическая задача: забрать новые новости и запостить в канал.
    """
    try:
        logger.info("Проверка новых новостей…")
        items = fetch_new_articles(limit=3)
        if not items:
            logger.info("Новых новостей не найдено.")
            return

        today_str = datetime.now(TZ).date().isoformat()

        for item in items:
            text = build_post_text(item)

            # сохраняем в буфер для дайджеста
            TODAY_NEWS.append(
                {
                    "title": item["title"],
                    "url": item["url"],
                    "date": today_str,
                }
            )

            try:
                if item.get("image"):
                    await context.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=item["image"],
                        caption=text,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=False,
                    )
            except Exception as e:
                logger.exception("Ошибка отправки новости: %s", e)
                await notify_admin(context, f"Ошибка отправки новости: {e}")

    except Exception as e:
        logger.exception("Ошибка в periodic_news_job: %s", e)
        await notify_admin(context, f"Ошибка в periodic_news_job: {e}")


async def daily_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вечерний дайджест: один раз в день в 21:00 по Душанбе.
    """
    try:
        today_str = datetime.now(TZ).date().isoformat()
        today_items = [n for n in TODAY_NEWS if n["date"] == today_str]

        if not today_items:
            logger.info("За сегодня новостей нет — дайджест не отправляем.")
            return

        lines = ["🌙 <b>Вечерний дайджест ИИ</b>", ""]
        for i, item in enumerate(today_items, start=1):
            safe_title = escape(item["title"])
            safe_url = escape(item["url"], quote=True)
            lines.append(f'{i}. <a href="{safe_url}">{safe_title}</a>')

        text = "\n".join(lines)

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )

        # очищаем буфер за день
        TODAY_NEWS.clear()

    except Exception as e:
        logger.exception("Ошибка в daily_digest_job: %s", e)
        await notify_admin(context, f"Ошибка в daily_digest_job: {e}")


# ------------------- HANDLERS -------------------

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start в личке с ботом."""
    if update.effective_chat is None:
        return

    await update.effective_chat.send_message(
        "👋 Привет! Я бот для канала с новостями об искусственном интеллекте.\n\n"
        "⚙️ Что я делаю:\n"
        "• в течение дня публикую свежие новости об ИИ;\n"
        "• для каждой новости даю короткое, но понятное описание;\n"
        "• в 21:00 по Душанбе отправляю вечерний дайджест за день."
    )


# ------------------- MAIN -------------------

def main() -> None:
    logger.info("Запуск ai-news-bot")

    load_sent_urls()

    app = Application.builder().token(TOKEN).build()

    # Команда /start
    app.add_handler(CommandHandler("start", start_handler))

    # Планировщик (JobQueue)
    job_queue = app.job_queue
    if job_queue is None:
        raise RuntimeError(
            "JobQueue не инициализирован. В requirements.txt должна быть строка "
            "'python-telegram-bot[job-queue]==21.6'"
        )

    # Периодическая проверка новостей
    job_queue.run_repeating(
        periodic_news_job,
        interval=NEWS_INTERVAL,
        first=30,   # через 30 секунд после старта
        name="periodic_news",
    )

    # Вечерний дайджест в 21:00
    job_queue.run_daily(
        daily_digest_job,
        time=time(21, 0, tzinfo=TZ),
        name="daily_digest",
    )

    # ВАЖНО: никаких asyncio.run, никаких ручных event loop
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()

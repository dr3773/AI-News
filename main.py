import os
import logging
import html
import re
from time import mktime
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from typing import List, Dict, Set
from urllib.parse import urlparse

import feedparser
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    Defaults,
)

# ----------------- ЛОГИ -----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ai_news_bot")

# ----------------- НАСТРОЙКИ -----------------

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

# --- RSS-источники ---

# Google News по ИИ на русском (тянет много РФ-СМИ сразу)
GOOGLE_NEWS_RU_AI = (
    "https://news.google.com/rss/search?"
    "q=искусственный+интеллект+OR+нейросети+OR+AI&"
    "hl=ru&gl=RU&ceid=RU:ru"
)

# РФ-источники через Google News (фильтр по домену)
RUS_SOURCES = [
    "https://news.google.com/rss/search?q=site:ria.ru+искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:tass.ru+искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:kommersant.ru+искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:vedomosti.ru+искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:rbc.ru+искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:lenta.ru+искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:habr.com+искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
]

# Глобальные AI/tech-источники
GLOBAL_SOURCES = [
    "https://www.marktechpost.com/feed",                 # MarkTechPost
    "https://venturebeat.com/category/ai/feed/",         # VentureBeat AI
    "https://syncedreview.com/feed",                     # Synced
    "https://unite.ai/feed/",                            # Unite.AI
    "https://the-decoder.com/feed/",                     # THE DECODER
    "https://techcrunch.com/feed/",                      # TechCrunch
    "https://www.theverge.com/rss/index.xml",            # The Verge
    "https://feeds.arstechnica.com/arstechnica/index",   # Ars Technica
]

FEED_URLS: List[str] = [GOOGLE_NEWS_RU_AI] + RUS_SOURCES + GLOBAL_SOURCES

# файл, где храним уже отправленные ссылки, чтобы не было дублей
SENT_URLS_FILE = "sent_urls.txt"
SENT_URLS: Set[str] = set()

# буфер новостей за сегодня для вечернего дайджеста
TODAY_NEWS: List[Dict[str, str]] = []

NEWS_INTERVAL_SECONDS = 45 * 60  # каждые 45 минут

_html_tag_re = re.compile(r"<[^>]+>")


# ----------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ -----------------

def load_sent_urls() -> None:
    """Загружаем уже отправленные ссылки из файла."""
    global SENT_URLS
    if not os.path.exists(SENT_URLS_FILE):
        SENT_URLS = set()
        return
    try:
        with open(SENT_URLS_FILE, "r", encoding="utf-8") as f:
            SENT_URLS = {line.strip() for line in f if line.strip()}
    except Exception as e:
        logger.warning("Не удалось загрузить SENT_URLS: %s", e)
        SENT_URLS = set()


def save_sent_urls() -> None:
    """Сохраняем отправленные ссылки в файл."""
    try:
        with open(SENT_URLS_FILE, "w", encoding="utf-8") as f:
            for url in sorted(SENT_URLS):
                f.write(url + "\n")
    except Exception as e:
        logger.warning("Не удалось сохранить SENT_URLS: %s", e)


def clean_html(text: str | None) -> str:
    """Убираем HTML-теги, &nbsp; и лишние пробелы."""
    if not text:
        return ""
    text = html.unescape(text)
    text = _html_tag_re.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_image(entry) -> str | None:
    """Пробуем достать картинку из RSS (media_content, media_thumbnail, enclosure)."""
    # media_content
    media = getattr(entry, "media_content", None)
    if media and isinstance(media, list):
        url = media[0].get("url")
        if url:
            return url

    # media_thumbnail
    thumb = getattr(entry, "media_thumbnail", None)
    if thumb and isinstance(thumb, list):
        url = thumb[0].get("url")
        if url:
            return url

    # enclosure / links
    links = getattr(entry, "links", [])
    for l in links:
        if "image" in l.get("type", "") and l.get("href"):
            return l["href"]

    return None


def get_source_name(entry, fallback_link: str) -> str:
    """Определяем название источника: сначала entry.source.title, потом домен."""
    source = getattr(entry, "source", None)
    if source and getattr(source, "title", None):
        s = str(source.title).strip()
        if s:
            return s

    # Парсим домен из ссылки
    try:
        netloc = urlparse(fallback_link).netloc
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc or "Источник"
    except Exception:
        return "Источник"


def collect_new_articles(limit: int = 5) -> List[Dict]:
    """
    Обходит все RSS-ленты, собирает до limit новых новостей,
    которых ещё нет в SENT_URLS.
    """
    items: List[Dict] = []

    for feed_url in FEED_URLS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            logger.warning("Ошибка парсинга RSS %s: %s", feed_url, e)
            continue

        for entry in getattr(parsed, "entries", []):
            link = entry.get("link")
            if not link:
                continue
            if link in SENT_URLS:
                continue

            title = clean_html(entry.get("title"))
            summary = clean_html(
                entry.get("summary") or entry.get("description") or ""
            )

            if not title and not summary:
                continue

            published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
            ts = mktime(published_parsed) if published_parsed else 0

            items.append(
                {
                    "title": title,
                    "summary": summary,
                    "url": link,
                    "source": get_source_name(entry, link),
                    "image": extract_image(entry),
                    "ts": ts,
                }
            )

    if not items:
        return []

    # сортируем по времени (новые сверху)
    items.sort(key=lambda x: x["ts"], reverse=True)

    new_items: List[Dict] = []
    for it in items:
        if len(new_items) >= limit:
            break
        url = it["url"]
        if url in SENT_URLS:
            continue
        SENT_URLS.add(url)
        new_items.append(it)

    save_sent_urls()
    return new_items


def build_body_text(title: str, summary: str) -> str:
    """
    Делаем более-менее "расширенный" текст по-русски без OpenAI.
    • если summary есть и он не копия заголовка – используем его;
    • если summary короткий – добавляем общий контекст.
    """
    t = (title or "").strip()
    s = (summary or "").strip()
    tl = t.lower()
    sl = s.lower()

    if s and sl != tl:
        base = s
    else:
        base = t

    if len(base) < 160:
        body = (
            f"{base} Это одна из свежих новостей в сфере искусственного интеллекта. "
            f"Такие события помогают понимать, как развивается ИИ и какие технологии "
            f"становятся ключевыми для компаний и разработчиков."
        )
    else:
        body = base

    return body


def build_post_text(item: Dict) -> str:
    """
    Формат поста:
    🧠 <b>Заголовок</b>

    текст

    ➜ Источник (слово «Источник» — кликабельная ссылка, урл не виден)
    """
    title = item["title"]
    summary = item["summary"]
    url = item["url"]

    body = build_body_text(title, summary)

    safe_title = html.escape(title)
    safe_body = html.escape(body)
    safe_url = html.escape(url, quote=True)

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
    if not ADMIN_ID:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ AI News: {text}")
    except Exception:
        logger.exception("Не удалось отправить сообщение админу")


async def post_single_news(context: ContextTypes.DEFAULT_TYPE, item: Dict) -> None:
    """Публикация ОДНОЙ новости в канал (с картинкой, если есть)."""
    text = build_post_text(item)

    # добавляем в буфер для вечернего дайджеста
    today_str = datetime.now(TZ).date().isoformat()
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


# ----------------- JOB'Ы -----------------

async def periodic_news_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Каждые NEWS_INTERVAL_SECONDS проверяем новые новости
    и публикуем до 3 штук.
    """
    try:
        logger.info("Периодический обход RSS-источников…")
        items = collect_new_articles(limit=3)
        if not items:
            logger.info("Новых новостей не найдено.")
            return

        for it in items:
            await post_single_news(context, it)

    except Exception as e:
        logger.exception("Ошибка в periodic_news_job: %s", e)
        await notify_admin(context, f"Ошибка в periodic_news_job: {e}")


async def daily_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вечерний дайджест в 21:00 по Душанбе:
    список ссылок на главные новости за день.
    """
    try:
        today_str = datetime.now(TZ).date().isoformat()
        today_items = [n for n in TODAY_NEWS if n["date"] == today_str]

        if not today_items:
            logger.info("За сегодня новостей нет — дайджест не отправляем.")
            return

        lines = ["🌙 <b>Вечерний дайджест ИИ</b>", ""]
        for i, it in enumerate(today_items, start=1):
            safe_title = html.escape(it["title"])
            safe_url = html.escape(it["url"], quote=True)
            lines.append(f'{i}. <a href="{safe_url}">{safe_title}</a>')

        text = "\n".join(lines)

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

        TODAY_NEWS.clear()
    except Exception as e:
        logger.exception("Ошибка в daily_digest_job: %s", e)
        await notify_admin(context, f"Ошибка в daily_digest_job: {e}")


# ----------------- ХЭНДЛЕРЫ -----------------

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ на /start в личке с ботом."""
    if update.effective_chat is None:
        return

    await update.effective_chat.send_message(
        "👋 Привет! Это бот новостного канала об искусственном интеллекте.\n\n"
        "• В течение дня я публикую свежие новости по ИИ из российских и мировых источников.\n"
        "• В 21:00 по Душанбе отправляю вечерний дайджест за день."
    )


# ----------------- MAIN -----------------

def main() -> None:
    logger.info("Запуск AI News бота")

    load_sent_urls()

    defaults = Defaults(
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )

    app = (
        Application.builder()
        .token(TOKEN)
        .defaults(defaults)
        .build()
    )

    # команды
    app.add_handler(CommandHandler("start", start_handler))

    # задачи по расписанию
    job_queue = app.job_queue
    if job_queue is None:
        raise RuntimeError(
            "JobQueue не инициализирован. Проверь, что в requirements.txt: "
            "python-telegram-bot[job-queue]==21.6"
        )

    # каждые 45 минут — проверка новых новостей
    job_queue.run_repeating(
        periodic_news_job,
        interval=NEWS_INTERVAL_SECONDS,
        first=30,
        name="periodic_news",
    )

    # вечерний дайджест в 21:00 по Душанбе
    job_queue.run_daily(
        daily_digest_job,
        time=dtime(21, 0, tzinfo=TZ),
        name="daily_digest",
    )

    # запуск бота (polling)
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()

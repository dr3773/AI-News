import os
import logging
import re
from html import unescape, escape
from time import mktime
from datetime import time, datetime
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

# ------------------- НАСТРОЙКИ -------------------

TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("Не задан токен бота. Укажи BOT_TOKEN или TOKEN в переменных окружения.")

CHANNEL_ID = os.environ.get("CHANNEL_ID")
if not CHANNEL_ID:
    raise RuntimeError("Не задан CHANNEL_ID — id или @username канала.")

ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip() or None

# интервал проверки новостей (секунды)
NEWS_INTERVAL = int(os.environ.get("NEWS_INTERVAL", "1800"))  # 30 минут по умолчанию

# часовой пояс (Душанбе)
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Dushanbe"))

SENT_URLS_FILE = "sent_urls.json"

FEEDS: List[str] = [
    "https://nplus1.ru/rss",
    "https://habr.com/ru/rss/hub/machine_learning/all/",
    "https://habr.com/ru/rss/hub/artificial_intelligence/all/",
    "https://ai.googleblog.com/feeds/posts/default?alt=rss",
    "https://openai.com/blog/rss.xml",
]

TODAY_NEWS: List[Dict] = []
SENT_URLS: Set[str] = set()

# ------------------- ЛОГИ -------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ai-news-bot")

# ------------------- ХЕЛПЕРЫ -------------------


def clean_html(text: str) -> str:
    """Убираем HTML-теги и лишние пробелы."""
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r"<.*?>", "", text)
    return text.strip()


def load_sent_urls() -> None:
    """Загружаем отправленные ссылки из файла."""
    import json

    global SENT_URLS
    if not os.path.exists(SENT_URLS_FILE):
        SENT_URLS = set()
        return

    try:
        with open(SENT_URLS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        SENT_URLS = set(data)
        logger.info("Загружено %d отправленных ссылок.", len(SENT_URLS))
    except Exception as e:
        logger.exception("Не удалось загрузить %s: %s", SENT_URLS_FILE, e)
        SENT_URLS = set()


def save_sent_urls() -> None:
    """Сохраняем отправленные ссылки в файл."""
    import json

    try:
        with open(SENT_URLS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(SENT_URLS), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Не удалось сохранить %s: %s", SENT_URLS_FILE, e)


def parse_entry(feed_title: str, entry) -> Dict:
    """Превращаем запись RSS в словарь."""
    title = entry.get("title", "").strip()
    summary = entry.get("summary", "") or entry.get("description", "")
    link = entry.get("link", "")

    published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if published_parsed:
        dt = datetime.fromtimestamp(mktime(published_parsed), tz=TZ)
    else:
        dt = datetime.now(TZ)

    image_url = ""
    content = ""
    if "content" in entry and entry.content:
        content = " ".join([c.value for c in entry.content if hasattr(c, "value")])
    else:
        content = summary or ""

    m = re.search(r'src="([^"]+)"', content)
    if m:
        image_url = m.group(1)

    return {
        "title": title or feed_title,
        "summary": summary,
        "url": link,
        "image": image_url,
        "date": dt.date().isoformat(),
        "feed": feed_title,
    }


def fetch_news() -> List[Dict]:
    """Читаем все RSS-ленты и собираем новые новости."""
    items: List[Dict] = []

    for feed_url in FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
            feed_title = parsed.feed.get("title", "Источник")
            for entry in parsed.entries:
                link = entry.get("link")
                if not link or link in SENT_URLS:
                    continue

                item = parse_entry(feed_title, entry)
                items.append(item)
        except Exception as e:
            logger.exception("Ошибка при чтении %s: %s", feed_url, e)

    # новые сверху
    items.sort(key=lambda x: x["date"], reverse=True)
    return items


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Отправка служебного сообщения админу (если указан)."""
    if not ADMIN_CHAT_ID:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"⚠️ {text}")
    except Exception:
        logger.exception("Не удалось отправить сообщение админу.")


def build_body_text(title: str, summary: str) -> str:
    """
    Короткий нормальный текст новости:
    - чистим теги;
    - если есть внятный summary (не дублирует заголовок) — используем его;
    - иначе возвращаем пустую строку.
    """
    title_clean = clean_html(title)
    summary_clean = clean_html(summary)

    if summary_clean and summary_clean.lower() != title_clean.lower():
        return summary_clean
    else:
        return ""


def build_post_text(item: Dict) -> str:
    """
    Итоговый формат поста:
    🧠 <жирный заголовок>

    <краткое описание, если есть>

    🔗 Источник
    """
    raw_title = item.get("title", "") or ""
    raw_summary = item.get("summary", "") or ""
    url = item.get("url", "") or ""

    title_clean = clean_html(raw_title)
    body = build_body_text(raw_title, raw_summary)

    safe_title = escape(title_clean)
    safe_body = escape(body) if body else ""
    safe_url = escape(url, quote=True) if url else ""

    parts: List[str] = []
    parts.append(f"🧠 <b>{safe_title}</b>")

    if safe_body:
        parts.append("")
        if len(safe_body) > 3500:
            safe_body = safe_body[:3490] + "…"
        parts.append(safe_body)

    if safe_url:
        parts.append("")
        parts.append(f'🔗 <a href="{safe_url}">Источник</a>')

    return "\n".join(parts)


# ------------------- JOBS -------------------


async def periodic_news_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодическая проверка новостей и отправка новых в канал."""
    logger.info("Запуск periodic_news_job")
    try:
        new_items = fetch_news()
        if not new_items:
            logger.info("Новых новостей нет.")
            return

        for item in new_items:
            url = item["url"]
            if not url or url in SENT_URLS:
                continue

            text = build_post_text(item)

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

                SENT_URLS.add(url)
                save_sent_urls()

                TODAY_NEWS.append(
                    {
                        "date": item["date"],
                        "title": item["title"],
                        "url": item["url"],
                    }
                )

            except Exception as e:
                logger.exception("Ошибка отправки новости: %s", e)
                await notify_admin(context, f"Ошибка отправки новости: {e}")

    except Exception as e:
        logger.exception("Ошибка в periodic_news_job: %s", e)
        await notify_admin(context, f"Ошибка в periodic_news_job: {e}")


async def daily_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вечерний дайджест: один раз в день в 21:00 по Душанбе."""
    try:
        today_str = datetime.now(TZ).date().isoformat()
        today_items = [n for n in TODAY_NEWS if n["date"] == today_str]

        if not today_items:
            logger.info("За сегодня новостей нет — дайджест не отправляем.")
            return

        lines = ["🌙 <b>Вечерний дайджест ИИ</b>", ""]
        for i, item in enumerate(today_items, start=1):
            safe_title = escape(clean_html(item["title"]))
            safe_url = escape(item["url"], quote=True)
            lines.append(f'{i}. <a href="{safe_url}">{safe_title}</a>')

        text = "\n".join(lines)

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )

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
        "👋 Привет! Я бот канала с новостями об искусственном интеллекте.\n\n"
        "Что я делаю:\n"
        "• в течение дня публикую свежие новости об ИИ из разных источников;\n"
        "• аккуратно оформляю заголовок и короткое описание;\n"
        "• в 21:00 по Душанбе отправляю вечерний дайджест за день."
    )


# ------------------- MAIN -------------------


def main() -> None:
    logger.info("Запуск ai-news-bot")

    load_sent_urls()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))

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
        first=30,  # через 30 секунд после старта
        name="periodic_news",
    )

    # Вечерний дайджест в 21:00
    job_queue.run_daily(
        daily_digest_job,
        time=time(21, 0, tzinfo=TZ),
        name="daily_digest",
    )

    # ВАЖНО: без asyncio.run, без своих event loop
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()

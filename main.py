import os
import asyncio
import logging
import sqlite3
from datetime import datetime, date
import re

import feedparser
from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

# -------------------- НАСТРОЙКИ --------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
NEWS_CHAT_ID = os.getenv("NEWS_CHAT_ID")

if not BOT_TOKEN or not NEWS_CHAT_ID:
    raise RuntimeError("Нужно задать BOT_TOKEN и NEWS_CHAT_ID в переменных окружения!")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s"
)

logger = logging.getLogger(__name__)

# Оставлены только русскоязычные источники
NEWS_FEEDS = [
    {
        "name": "Forklog AI",
        "url": "https://forklog.com/tag/iskusstvennyj-intellekt/feed"
    },
    {
        "name": "CNews AI",
        "url": "https://www.cnews.ru/inc/rss/news/tag/iskusstvennyj_intellekt"
    },
    {
        "name": "Lenta.ru — Технологии",
        "url": "https://lenta.ru/rss/top7"
    }
]

DB_PATH = "news.db"

# -------------------- РАБОТА С БД --------------------

def init_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE news (
            url TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            image_url TEXT,
            source TEXT,
            published_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def news_exists(url: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM news WHERE url = ?", (url,))
    found = cur.fetchone() is not None
    conn.close()
    return found


def save_news(url, title, description, image_url, source, published_at):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO news (url, title, description, image_url, source, published_at) VALUES (?, ?, ?, ?, ?, ?)",
        (url, title, description, image_url, source, published_at.isoformat())
    )
    conn.commit()
    conn.close()


def get_today_news():
    today_str = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT title, description, url, source, published_at
        FROM news
        WHERE DATE(published_at) = ?
        ORDER BY published_at ASC
        """,
        (today_str,)
    )
    rows = cur.fetchall()
    conn.close()
    return rows

# -------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ --------------------

def shorten_text(text: str, max_len: int = 280) -> str:
    text = re.sub(r"<.*?>", "", text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def split_message(text: str, limit: int = 4000):
    parts = []
    while len(text) > limit:
        cut_pos = text.rfind("\n\n", 0, limit)
        if cut_pos == -1:
            cut_pos = limit
        parts.append(text[:cut_pos])
        text = text[cut_pos:]
    parts.append(text)
    return parts


def extract_image(entry):
    # 1 — media:thumbnail
    if "media_thumbnail" in entry and entry.media_thumbnail:
        return entry.media_thumbnail[0].get("url")

    # 2 — media:content
    if "media_content" in entry and entry.media_content:
        return entry.media_content[0].get("url")

    # 3 — enclosures
    if hasattr(entry, "enclosures"):
        for e in entry.enclosures:
            if "image" in e.get("type", ""):
                return e.get("href")

    # 4 — <img> inside summary
    summary = entry.get("summary", "")
    m = re.search(r'<img[^>]+src="([^"]+)"', summary)
    if m:
        return m.group(1)

    return None


# -------------------- ОСНОВНАЯ ЛОГИКА НОВОСТЕЙ --------------------

async def fetch_and_send_news(bot: Bot):
    logger.info("Проверка новых новостей...")
    total_new = 0

    for feed in NEWS_FEEDS:
        source_name = feed["name"]
        url = feed["url"]

        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            logger.error(f"Ошибка парсинга {url}: {e}")
            continue

        for entry in parsed.entries:
            link = entry.get("link")
            title = entry.get("title", "").strip()
            description = entry.get("summary") or entry.get("description") or title

            if not link or not title:
                continue

            if news_exists(link):
                continue

            published = datetime.now()
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    published = datetime(
                        entry.published_parsed.tm_year,
                        entry.published_parsed.tm_mon,
                        entry.published_parsed.tm_mday,
                        entry.published_parsed.tm_hour,
                        entry.published_parsed.tm_min,
                        entry.published_parsed.tm_sec,
                    )
                except Exception:
                    pass

            image_url = extract_image(entry)

            short_title = shorten_text(title)

            text = (
                f"🧠 <b>{short_title}</b>\n"
                f"<i>{source_name}</i>\n"
                f"<a href=\"{link}\">Читать полностью</a>"
            )

            try:
                if image_url:
                    await bot.send_photo(
                        chat_id=NEWS_CHAT_ID,
                        photo=image_url,
                        caption=text,
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await bot.send_message(
                        chat_id=NEWS_CHAT_ID,
                        text=text,
                        parse_mode=ParseMode.HTML
                    )

                save_news(link, title, description, image_url, source_name, published)
                total_new += 1
                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Ошибка отправки новости: {e}")

    logger.info(f"Новых новостей отправлено: {total_new}")


async def send_evening_digest(bot: Bot):
    logger.info("Формирование вечернего дайджеста...")
    rows = get_today_news()

    if not rows:
        logger.info("За сегодня нет новостей для дайджеста.")
        return

    header = (
        f"🍔 <b>Вечерний дайджест ИИ-новостей за "
        f"{date.today().strftime('%d.%m.%Y')}:</b>\n\n"
    )

    body_lines = []
    for i, (title, description, url, source, published_at) in enumerate(rows, start=1):
        desc = description or title
        short_desc = shorten_text(desc, 300)

        line = (
            f"{i}. {short_desc}\n"
            f"<i>{source}</i> — <a href=\"{url}\">Источник</a>\n"
        )
        body_lines.append(line)

    full_text = header + "\n".join(body_lines)
    parts = split_message(full_text)

    try:
        for part in parts:
            await bot.send_message(
                chat_id=NEWS_CHAT_ID,
                text=part,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            await asyncio.sleep(1)
        logger.info("Вечерний дайджест отправлен.")
    except Exception as e:
        logger.error(f"Ошибка отправки вечернего дайджеста: {e}")

# -------------------- TELEGRAM-БОТ --------------------

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я бот канала «AI News | ИИ Новости».\n"
        "Новости публикуются автоматически в канал.\n"
        "Здесь можно только проверить, что бот работает 🙂"
    )


async def main():
    init_db()

    bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

    scheduler.add_job(
        fetch_and_send_news,
        IntervalTrigger(minutes=30),
        args=(bot,),
        id="fetch_news_job",
        replace_existing=True
    )

    scheduler.add_job(
        send_evening_digest,
        CronTrigger(hour=21, minute=0),
        args=(bot,),
        id="evening_digest_job",
        replace_existing=True
    )

    scheduler.start()

    logger.info("Бот запущен.")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

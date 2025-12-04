import os
import asyncio
import logging
import sqlite3
from datetime import datetime, date

import feedparser
from aiogram import Bot, Dispatcher, Router, types, F
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
NEWS_CHAT_ID = os.getenv("NEWS_CHAT_ID")  # ID или @username канала

if not BOT_TOKEN or not NEWS_CHAT_ID:
    raise RuntimeError("Нужно задать BOT_TOKEN и NEWS_CHAT_ID в переменных окружения!")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
)

logger = logging.getLogger(__name__)

# RSS-ленты. Добавляй / меняй по вкусу
NEWS_FEEDS = [
    # Зарубежные про ИИ
    {
        "name": "404 Media",
        "url": "https://www.404media.co/rss",
    },
    {
        "name": "Ahead of AI",
        "url": "https://www.aheadofai.com/rss/",
    },
    # Российские / русскоязычные про ИИ
    {
        "name": "Forklog AI",
        "url": "https://forklog.com/tag/iskusstvennyj-intellekt/feed",
    },
    {
        "name": "CNews AI",
        "url": "https://www.cnews.ru/inc/rss/news/tag/iskusstvennyj_intellekt",
    },
    {
        "name": "Lenta.ru – Технологии",
        "url": "https://lenta.ru/rss/top7",  # при желании можно отфильтровать по ИИ по ключевым словам
    },
]

DB_PATH = "news.db"

# -------------------- РАБОТА С БД --------------------


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS news (
            url TEXT PRIMARY KEY,
            title TEXT,
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
    row = cur.fetchone()
    conn.close()
    return row is not None


def save_news(url: str, title: str, source: str, published_at: datetime):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO news (url, title, source, published_at) VALUES (?, ?, ?, ?)",
        (url, title, source, published_at.isoformat()),
    )
    conn.commit()
    conn.close()


def get_today_news():
    """Все новости за сегодняшний день для дайджеста."""
    today_str = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT title, url, source, published_at
        FROM news
        WHERE DATE(published_at) = ?
        ORDER BY published_at ASC
        """,
        (today_str,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# -------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ --------------------


def shorten_text(text: str, max_len: int = 180) -> str:
    """Укорачиваем заголовок/описание, чтобы не было полотна."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def split_message(text: str, limit: int = 4000):
    """Делим длинный текст на части для Telegram (лимит ~4096 символов)."""
    parts = []
    while len(text) > limit:
        # режем по ближайшему переводу строки
        cut_pos = text.rfind("\n\n", 0, limit)
        if cut_pos == -1:
            cut_pos = limit
        parts.append(text[:cut_pos])
        text = text[cut_pos:]
    parts.append(text)
    return parts


# -------------------- ОСНОВНАЯ ЛОГИКА НОВОСТЕЙ --------------------


async def fetch_and_send_news(bot: Bot):
    """Проверяем новые новости и отправляем только то, чего ещё не было."""
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

            if not link or not title:
                continue

            if news_exists(link):
                continue  # уже было

            # Дата
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

            short_title = shorten_text(title)

            # Формируем сообщение для ОДНОЙ новости
            text = (
                f"🧠 <b>{short_title}</b>\n"
                f"<i>{source_name}</i>\n"
                f"<a href=\"{link}\">Источник</a>"
            )

            try:
                await bot.send_message(
                    chat_id=NEWS_CHAT_ID,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
                save_news(link, title, source_name, published)
                total_new += 1
                await asyncio.sleep(1)  # чтобы не спамить слишком быстро
            except Exception as e:
                logger.error(f"Ошибка отправки новости: {e}")

    logger.info(f"Новых новостей отправлено: {total_new}")


async def send_evening_digest(bot: Bot):
    """Вечерний дайджест за сегодня. Ссылки у слова 'Источник' как и в обычных новостях."""
    logger.info("Формирование вечернего дайджеста...")
    rows = get_today_news()

    if not rows:
        logger.info("За сегодня нет новостей для дайджеста.")
        return

    header = (
        f"🍔 <b>Вечерний дайджест ИИ-новостей за {date.today().strftime('%d.%m.%Y')}:</b>\n\n"
    )

    body_lines = []
    for i, (title, url, source, published_at) in enumerate(rows, start=1):
        short_title = shorten_text(title, 220)
        line = (
            f"{i}. {short_title}\n"
            f"<i>{source}</i> — <a href=\"{url}\">Источник</a>\n"
        )
        body_lines.append(line)

    full_text = header + "\n".join(body_lines)

    # Делим на части, если слишком длинно
    parts = split_message(full_text)

    try:
        for part in parts:
            await bot.send_message(
                chat_id=NEWS_CHAT_ID,
                text=part,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
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
        "Здесь можно только проверить, что бот жив 😊"
    )


async def main():
    init_db()

    bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")  # при желании поменяй

    # каждые 30 минут — проверка новых новостей
    scheduler.add_job(
        fetch_and_send_news,
        IntervalTrigger(minutes=30),
        args=(bot,),
        id="fetch_news_job",
        replace_existing=True,
    )

    # каждый день в 21:00 — вечерний дайджест
    scheduler.add_job(

send_evening_digest,
        CronTrigger(hour=21, minute=0),
        args=(bot,),
        id="evening_digest_job",
        replace_existing=True,
    )

    scheduler.start()

    logger.info("Бот запущен.")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if name == "__main__":
    asyncio.run(main())

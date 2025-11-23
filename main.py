import os
import asyncio
from datetime import time
from zoneinfo import ZoneInfo

import feedparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

# ============ НАСТРОЙКИ ============

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # как строка в переменной окружения
ADMIN_ID = 797726160  # твой user_id, чтобы бот писал тебе об ошибках

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID)

# Часовой пояс
TZ = ZoneInfo("Asia/Dushanbe")

# RSS-ленты по ИИ
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=ru&gl=RU&ceid=RU:ru",
]

# Создаём бота (без Application, только Bot, чтобы не было getUpdates)
bot = Bot(TOKEN)


# ============ РАБОТА С НОВОСТЯМИ ============

def extract_image(entry) -> str | None:
    """
    Достаём картинку из RSS-записи, если она есть.
    Для Google News обычно лежит в media_content.
    """
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


def fetch_ai_news(limit: int = 3):
    """
    Собираем новости по ИИ из нескольких RSS-лент.
    Возвращаем список словарей: title, url, image, source.
    """
    items: list[dict] = []

    for feed_url in RSS_FEEDS:
        parsed = feedparser.parse(feed_url)
        source_title = parsed.feed.get("title", "Новости ИИ")

        for entry in parsed.entries:
            title = entry.get("title")
            link = entry.get("link")
            if not title or not link:
                continue

            image = extract_image(entry)
            items.append(
                {
                    "title": title,
                    "url": link,
                    "image": image,
                    "source": source_title,
                }
            )

    # Удаляем дубли по ссылке, оставляем первые limit штук
    seen = set()
    unique_items = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        unique_items.append(it)
        if len(unique_items) >= limit:
            break

    return unique_items


# ============ ОТПРАВКА ДАЙДЖЕСТА ============

async def send_digest(label: str) -> None:
    """
    Отправляет один дайджест (утренний / дневной / вечерний / ночной).
    В канал — новости, при ошибке — сообщение тебе в личку.
    """
    try:
        news = fetch_ai_news(limit=3)
    except Exception as e:
        # Ошибка при парсинге новостей — пишем только тебе
        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=f"❌ Ошибка при получении новостей ({label}): {e}",
            )
        except TelegramError:
            pass
        return

    if not news:
        # Нет новостей — напишем тебе, чтобы ты знал
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ {label}: свежих новостей по ИИ не найдено.",
        )
        return

    # Заголовок выпуска в канал
    await bot.send_message(
        chat_id=CHANNEL_ID,
        text=f"🤖 {label}\nПодборка свежих новостей об искусственном интеллекте:",
    )

    # Каждую новость отправляем отдельным сообщением
    for i, item in enumerate(news, start=1):
        title = item["title"]
        url = item["url"]
        image = item["image"]
        source = item["source"]

        caption = f"{i}. {title}\n📎 Источник: {source}"
        if len(caption) > 1024:
            caption = caption[:1020] + "…"

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Читать полностью 📖", url=url)]]
        )

        try:
            if image:
                # Пробуем отправить с фото
                await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=image,
                    caption=caption,
                    reply_markup=keyboard,
                )
            else:
                # Если картинки нет
                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=caption,
                    reply_markup=keyboard,
                )
        except TelegramError as e:
            # Если не получилось (битая картинка и т.п.) — пишем тебе
            try:
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"⚠️ Ошибка при отправке новости ({label}): {e}",
                )
            except TelegramError:
                pass


# ============ ЗАПУСК ПЛАНИРОВЩИКА ============

async def main() -> None:
    scheduler = AsyncIOScheduler(timezone=TZ)

    # Расписание: 5 раз в день
    schedule = [
        ("Утренний дайджест ИИ", 9, 0),
        ("Дневной дайджест ИИ", 12, 0),
        ("Дневной дайджест ИИ", 15, 0),
        ("Вечерний дайджест ИИ", 18, 0),
        ("Ночной дайджест ИИ", 21, 0),
    ]

    for label, hour, minute in schedule:
        scheduler.add_job(
            send_digest,
            "cron",
            hour=hour,
            minute=minute,
            args=[label],
            id=label,  # чтобы не дублировались
            replace_existing=True,
        )

    scheduler.start()

    # Сообщение только тебе, что бот запущен
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text="🤖 AI News Bot запущен. Расписание дайджестов активировано.",
        )
    except TelegramError:
        pass

    # Держим процесс живым
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())


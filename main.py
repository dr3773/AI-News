import os
import asyncio
from datetime import time
from zoneinfo import ZoneInfo

import feedparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup


# ========= НАСТРОЙКИ =========
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID_ENV = os.getenv("CHANNEL_ID")

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID_ENV:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID_ENV)

# Один глобальный бот
bot = Bot(token=TOKEN)

# RSS-источники по ИИ (можно расширять)
RSS_FEEDS = [
    # Русский ИИ
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=нейросети&hl=ru&gl=RU&ceid=RU:ru",

    # Англоязычный ИИ
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=en&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=machine+learning&hl=en&gl=US&ceid=US:en",

    # Примеры тематических лент (позже можно дополнять)
    # MIT Technology Review AI
    "https://www.technologyreview.com/feed/",
    # The Verge (тут много ИТ, но ИИ тоже часто)
    "https://www.theverge.com/rss/index.xml",
]


# ========= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =========
def extract_image(entry) -> str | None:
    """
    Пытаемся вытащить ссылку на картинку из записи RSS.
    """
    # 1) media_content
    media = getattr(entry, "media_content", None)
    if media and isinstance(media, list):
        url = media[0].get("url")
        if url:
            return url

    # 2) media_thumbnail
    thumb = getattr(entry, "media_thumbnail", None)
    if thumb and isinstance(thumb, list):
        url = thumb[0].get("url")
        if url:
            return url

    # 3) ссылки типа image/*
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

    # Удаляем дубли по ссылке и обрезаем список до limit
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


# ========= ОТПРАВКА ДАЙДЖЕСТА =========
async def send_digest(label: str) -> None:
    """
    Отправляет один выпуск дайджеста.
    label — название: "Утренний дайджест ИИ" и т.п.
    """

    news = fetch_ai_news(limit=3)

    if not news:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=f"⚠️ {label}\nСегодня свежих новостей по ИИ не нашлось.",
        )
        return

    # Заголовок выпуска
    await bot.send_message(
        chat_id=CHANNEL_ID,
        text=f"🤖 {label}\nПодборка свежих новостей об искусственном интеллекте:",
    )

    # Каждую новость отправляем отдельным красивым постом
    for i, item in enumerate(news, start=1):
        title = item["title"]
        url = item["url"]
        image = item["image"]
        source = item["source"]

        # Небольшая «шапка» — как у нормального медиа
        caption = f"""\
{i}. {title}

📎 Источник: {source}
"""

        # Ограничение Telegram на подпись к фото
        if len(caption) > 1024:
            caption = caption[:1020] + "…"

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Читать полностью 📖", url=url)]]
        )

        # Пытаемся отправить с фото
        if image:
            try:
                await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=image,
                    caption=caption,
                    reply_markup=keyboard,
                )
                continue
            except Exception:
                # Если фото не загрузилось — отправим как текст
                pass

        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=caption,
            reply_markup=keyboard,
        )


# ========= ОСНОВНАЯ ФУНКЦИЯ =========
async def main() -> None:
    """
    Стартуем планировщик и живём в вечном цикле.
    Никаких обновлений, никакого polling, только рассылка в канал.
    """
    tz = ZoneInfo("Asia/Dushanbe")
    scheduler = AsyncIOScheduler(timezone=tz)

    schedule = [
        ("Утренний дайджест ИИ", time(9, 0)),
        ("Дневной дайджест ИИ", time(12, 0)),
        ("Дневной дайджест ИИ", time(15, 0)),
        ("Вечерний дайджест ИИ", time(18, 0)),
        ("Ночной дайджест ИИ", time(21, 0)),
    ]

    for label, t in schedule:
        scheduler.add_job(
            send_digest,
            "cron",
            hour=t.hour,
            minute=t.minute,
            args=[label],
        )

    scheduler.start()

    # Разовый тестовый запуск при старте (можно убрать)
    await send_digest("Тестовый автодайджест ИИ")

    # Вечный цикл, чтобы worker не завершался
    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())

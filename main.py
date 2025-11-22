import os
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ===================== НАСТРОЙКИ =====================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # должно быть с минусом, как у тебя: -1003...

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

# RSS-ленты по ИИ (можно добавлять свои)
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=ru&gl=RU&ceid=RU:ru",
]

TIMEZONE = ZoneInfo("Asia/Dushanbe")


# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================


async def telegram_request(method: str, params: dict) -> dict:
    """Отправляем запрос к Telegram Bot API и печатаем ошибку, если она есть."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(f"{BASE_URL}/{method}", data=params)
        data = response.json()
        if not data.get("ok", False):
            print(f"[TELEGRAM ERROR] {method}: {data}")
        return data


def extract_image(entry) -> str | None:
    """
    Пытаемся достать картинку из RSS-записи.
    Для Google News чаще всего в media_content.
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


# ===================== ОТПРАВКА ДАЙДЖЕСТА =====================


async def send_digest(label: str):
    """
    Отправка одного выпуска дайджеста.
    label — строка вида "Утренний дайджест ИИ".
    """
    print(f"[{datetime.now(TIMEZONE)}] Запуск рассылки: {label}")
    news = fetch_ai_news(limit=3)

    if not news:
        await telegram_request(
            "sendMessage",
            {
                "chat_id": CHANNEL_ID,
                "text": f"⚠️ {label}\nСегодня свежих новостей по ИИ не нашлось.",
                "parse_mode": "HTML",
            },
        )
        return

    # Заголовок выпуска
    await telegram_request(
        "sendMessage",
        {
            "chat_id": CHANNEL_ID,
            "text": f"🤖 <b>{label}</b>\nПодборка свежих новостей об искусственном интеллекте:",
            "parse_mode": "HTML",
        },
    )

    # Каждую новость отправляем отдельным сообщением
    for i, item in enumerate(news, start=1):
        title = item["title"]
        url = item["url"]
        image = item["image"]
        source = item["source"]

        text = f"<b>{i}. {title}</b>\n📎 <i>{source}</i>"

        # подпись ограничена 1024 символами
        if len(text) > 1000:
            text = text[:997] + "…"

        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "Читать полностью 📖",
                        "url": url,
                    }
                ]
            ]
        }

        if image:
            # Пытаемся отправить с фотографией
            resp = await telegram_request(
                "sendPhoto",
                {
                    "chat_id": CHANNEL_ID,
                    "photo": image,
                    "caption": text,
                    "parse_mode": "HTML",
                    "reply_markup": httpx.dumps(reply_markup),
                },
            )
            if resp.get("ok"):
                continue  # успешно отправили фото — идём к следующей новости

        # Если фото не получилось — шлём просто текст
        await telegram_request(
            "sendMessage",
            {
                "chat_id": CHANNEL_ID,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": httpx.dumps(reply_markup),
            },
        )


# ===================== ОСНОВНОЙ ЦИКЛ =====================


async def main():
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    # 5 выпусков в день
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
            trigger=CronTrigger(hour=hour, minute=minute),
            args=[label],
            id=label,
            replace_existing=True,
        )

    scheduler.start()
    print("AI News scheduler started ✅")

    # Один тестовый выпуск при старте, чтобы ты сразу увидел результат
    await send_digest("Тестовый автодайджест ИИ")

    # Держим процесс живым
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())


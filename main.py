import os
from datetime import time
from zoneinfo import ZoneInfo

import feedparser
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes

# ====== НАСТРОЙКИ ======
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID")

CHANNEL_ID = int(CHANNEL_ID)

# RSS-ленты
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=ru&gl=RU&ceid=RU:ru",
]


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


def fetch_ai_news(limit: int = 3):
    items = []

    for feed_url in RSS_FEEDS:
        parsed = feedparser.parse(feed_url)
        source_title = parsed.feed.get("title", "Новости ИИ")

        for entry in parsed.entries:
            title = entry.get("title")
            link = entry.get("link")
            if not title or not link:
                continue

            image = extract_image(entry)
            items.append({
                "title": title,
                "url": link,
                "image": image,
                "source": source_title,
            })

    seen = set()
    unique = []

    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        unique.append(it)
        if len(unique) >= limit:
            break

    return unique


async def send_digest(context: ContextTypes.DEFAULT_TYPE):
    label: str = context.job.data.get("label", "Дайджест ИИ")
    news = fetch_ai_news(limit=3)

    if not news:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=f"⚠️ {label}\nСегодня свежих новостей по ИИ не нашлось."
        )
        return

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=f"🤖 {label}\nПодборка свежих новостей об искусственном интеллекте:"
    )

    for i, item in enumerate(news, start=1):
        caption = f"{i}. {item['title']}\n📎 Источник: {item['source']}"

        if len(caption) > 1024:
            caption = caption[:1020] + "…"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Читать полностью 📖", url=item["url"])]
        ])

        if item["image"]:
            try:
                await context.bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=item["image"],
                    caption=caption,
                    reply_markup=keyboard
                )
                continue
            except:
                pass

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=caption,
            reply_markup=keyboard
        )


def main():
    app = Application.builder().token(TOKEN).build()

    tz = ZoneInfo("Asia/Dushanbe")

    schedule = [
        ("Утренний дайджест ИИ", time(9, 0, tzinfo=tz)),
        ("Дневной дайджест ИИ", time(12, 0, tzinfo=tz)),
        ("Дневной дайджест ИИ", time(15, 0, tzinfo=tz)),
        ("Вечерний дайджест ИИ", time(18, 0, tzinfo=tz)),
        ("Ночной дайджест ИИ", time(21, 0, tzinfo=tz)),
    ]

    for label, t in schedule:
        app.job_queue.run_daily(
            send_digest,
            time=t,
            data={"label": label},
            name=label
        )

    app.run_polling(allowed_updates=[])


if name == "__main__":
    main()

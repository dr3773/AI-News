import os
from datetime import time
from zoneinfo import ZoneInfo

import feedparser
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ContextTypes, CommandHandler

# ====== НАСТРОЙКИ ======
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # строка из переменной окружения

# Твой Telegram ID (из IDBot)
OWNER_ID = 797726160

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID)

# RSS-ленты по ИИ
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=ru&gl=RU&ceid=RU:ru",
]


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


async def send_digest_for_label(label: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отправляет дайджест с заданным заголовком label.
    """
    news = fetch_ai_news(limit=3)

    if not news:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=f"⚠️ {label}\nСегодня свежих новостей по ИИ не нашлось.",
        )
        return

    # Заголовок выпуска
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=f"🤖 {label}\nПодборка свежих новостей об искусственном интеллекте:",
    )

    # Каждую новость отправляем отдельным сообщением с кнопкой
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

        if image:
            try:
                await context.bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=image,
                    caption=caption,
                    reply_markup=keyboard,
                )
                continue
            except Exception:
                # Если фото не получилось – отправляем просто текст
                pass

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=caption,
            reply_markup=keyboard,
        )


# ====== JOB-ДЛЯ РАСПИСАНИЯ ======
async def send_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    label: str = context.job.data.get("label", "Дайджест ИИ")
    await send_digest_for_label(label, context)


# ====== РУЧНОЙ ЗАПУСК /test ======
async def test_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Разрешаем использовать только тебе
    if update.effective_user is None or update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Команда доступна только владельцу бота.")
        return

    await update.message.reply_text("✅ Запускаю тестовый дайджест в канал...")
    await send_digest_for_label("Тестовый дайджест ИИ (ручной запуск)", context)


def main() -> None:
    app = Application.builder().token(TOKEN).build()

    # Команда /test в личке с ботом
    app.add_handler(CommandHandler("test", test_digest))

    tz = ZoneInfo("Asia/Dushanbe")

    # 5 выпусков в день
    schedule = [
        ("Утренний дайджест ИИ", time(9, 0, tzinfo=tz)),
        ("Дневной дайджест ИИ", time(12, 0, tzinfo=tz)),
        ("Дневной дайджест ИИ", time(15, 0, tzinfo=tz)),
        ("Вечерний дайджест ИИ", time(18, 0, tzinfo=tz)),
        ("Ночной дайджест ИИ", time(21, 0, tzinfo=tz)),
    ]

    for label, t in schedule:
        app.job_queue.run_daily(
            send_digest_job,
            time=t,
            data={"label": label},
            name=label,
        )

    # run_polling сам создаёт и закрывает event loop
    app.run_polling()



    main()


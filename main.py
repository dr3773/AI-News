import os
from datetime import time
from zoneinfo import ZoneInfo
import logging

import feedparser
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

# ====== ЛОГИ ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ====== НАСТРОЙКИ ======
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # строка из переменной окружения

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


async def send_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Универсальный отправщик дайджеста.
    Название (утренний/дневной/вечерний) берём из context.job.data["label"].
    """
    label: str = context.job.data.get("label", "Дайджест ИИ")

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
            except Exception as e:
                logger.warning("Не удалось отправить фото: %s", e)

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=caption,
            reply_markup=keyboard,
        )


# ----- команды для проверки -----

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бот AI News. Пишу дайджесты в канал.\n"
        "Команда /test — отправить тестовый дайджест в канал."
    )


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручной тест: отправляет один дайджест прямо сейчас."""
    await update.message.reply_text("Ок! Отправляю тестовый дайджест в канал.")
    job_context = type("obj", (), {"job": type("obj2", (), {"data": {"label": "Тестовый дайджест ИИ"}})})()
    # небольшой хак, чтобы переиспользовать send_digest
    await send_digest(context)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Если написать 'test' без слэша — тоже запускаем ручной тест."""
    if update.message and update.message.text:
        if update.message.text.strip().lower() == "test":
            await cmd_test(update, context)


def main() -> None:
    application = Application.builder().token(TOKEN).build()

    # Хендлеры команд
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("test", cmd_test))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Планировщик дайджестов
    tz = ZoneInfo("Asia/Dushanbe")
    schedule = [
        ("Утренний дайджест ИИ", time(9, 0, tzinfo=tz)),
        ("Дневной дайджест ИИ", time(12, 0, tzinfo=tz)),
        ("Дневной дайджест ИИ", time(15, 0, tzinfo=tz)),
        ("Вечерний дайджест ИИ", time(18, 0, tzinfo=tz)),
        ("Ночной дайджест ИИ", time(21, 0, tzinfo=tz)),
    ]

    for label, t in schedule:
        application.job_queue.run_daily(
            send_digest,
            time=t,
            data={"label": label},
            name=label,
        )

    # ВАЖНО: никаких asyncio.run, никаких app.idle()
    application.run_polling()  # по умолчанию слушает только нужные обновления


if __name__ == "__main__":
    main()

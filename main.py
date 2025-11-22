import os
from datetime import time
from zoneinfo import ZoneInfo
import logging

import feedparser
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ---------- ЛОГИ ----------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------- НАСТРОЙКИ ----------

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID)

# Новости об ИИ (новостной блок)
RSS_NEWS = [
    # Google News – ИИ по-русски и по-английски
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=en&gl=US&ceid=US:en",

    # MIT Technology Review – общий технологический поток (много ИИ)
    "https://www.technologyreview.com/feed/",

    # OpenAI News – официальные обновления
    "https://openai.com/news/rss.xml",

    # AITopics – AI in the News
    "http://feeds.feedburner.com/AIInTheNews",
]

# Обучающие материалы (блок «Прокачка в ИИ»)
RSS_LEARNING = [
    "https://machinelearningmastery.com/feed/",
    "https://machinelearningguide.libsyn.com/rss",
]


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------

def extract_image(entry):
    """Пытаемся достать картинку из RSS-записи."""
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


def fetch_from_feeds(feeds, limit=5):
    """
    Универсальная функция: собирает записи из списка RSS-лент.
    Возвращает список словарей: {title, url, image, source}.
    """
    items = []

    for feed_url in feeds:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            logger.warning("Ошибка парсинга RSS %s: %s", feed_url, e)
            continue

        source_title = parsed.feed.get("title", feed_url)

        for entry in getattr(parsed, "entries", []):
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

    # Убираем дубли по ссылке и ограничиваем по количеству
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


def fetch_news(limit=3):
    """Новости об ИИ (основной блок)."""
    return fetch_from_feeds(RSS_NEWS, limit=limit)


def fetch_learning(limit=3):
    """Обучающие материалы для блока «Прокачка в ИИ»."""
    return fetch_from_feeds(RSS_LEARNING, limit=limit)


# ---------- ОТПРАВКА ДАЙДЖЕСТА ----------

async def send_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Рассылщик дайджеста:
    1. Заголовок
    2. Новости
    3. Блок «Прокачка в ИИ»
    """
    # Название выпуска берём из job.data["label"], если есть
    label = "Дайджест ИИ"
    job = getattr(context, "job", None)
    if job is not None:
        data = getattr(job, "data", {})
        if isinstance(data, dict) and "label" in data:
            label = data["label"]

    news = fetch_news(limit=3)
    learning_items = fetch_learning(limit=3)

    # — 1. Заголовок выпуска —
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=f"🤖 {label}\nСвежие новости об искусственном интеллекте:",
    )

    # — 2. Новости —
    if not news:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text="⚠️ Сегодня свежих новостей по ИИ не нашлось.",
        )
    else:
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

    # — 3. Блок «Прокачка в ИИ» —
    if learning_items:
        lines = ["🧠 *Прокачка в ИИ — коротко*"]

        if len(learning_items) >= 1:
            li = learning_items[0]
            lines.append(f"\n📖 *Статья дня*\n{li['title']}\n{li['url']}")

        if len(learning_items) >= 2:
            li = learning_items[1]
            lines.append(f"\n🎓 *Для изучения*\n{li['title']}\n{li['url']}")

        if len(learning_items) >= 3:
            li = learning_items[2]
            lines.append(f"\n🧠 *Для углубления*\n{li['title']}\n{li['url']}")

        text = "\n".join(lines)

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode="Markdown",
            disable_web_page_preview=False,
        )


# ---------- КОМАНДЫ /start и /test ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Это AI News Digest.\n\n"
        "Я отправляю в канал дайджесты по ИИ 5 раз в день — новости + блок «Прокачка в ИИ».\n"
        "Можешь написать /test, чтобы вручную запустить тестовый выпуск в канал."
    )


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручной запуск дайджеста по команде /test (для тебя)."""
    await update.message.reply_text("✅ Запускаю тестовый дайджест в канал.")
    await send_digest(context)


# ---------- MAIN ----------

async def main() -> None:
    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("test", cmd_test))

    # Планировщик
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
            name=label,
        )

    await app.run_polling()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

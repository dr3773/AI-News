import os
import logging
from datetime import time
from zoneinfo import ZoneInfo

import feedparser
from telegram.ext import Application, ContextTypes

# ----------------- ЛОГИ -----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ----------------- НАСТРОЙКИ -----------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

# ID канала должен быть строкой, например "-1003238891648"
CHANNEL_ID = int(CHANNEL_ID)

TZ = ZoneInfo("Asia/Dushanbe")

# RSS-ленты Google News
AI_FEED_RU = (
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru"
)
AI_FEED_EN = (
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=en&gl=US&ceid=US:en"
)
AI_CRYPTO_FEED = (
    "https://news.google.com/rss/search?q=AI+crypto+blockchain&hl=en&gl=US&ceid=US:en"
)


# ----------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ -----------------
def fetch_news(feed_url: str, max_items: int = 5):
    """
    Получает новости из RSS-ленты.
    Возвращает список кортежей (title, link, source).
    """
    try:
        feed = feedparser.parse(feed_url)
    except Exception as e:
        logger.error("Ошибка при загрузке RSS: %s", e)
        return []

    items = []
    for entry in feed.entries[:max_items]:
        title = entry.get("title", "Без названия")
        link = entry.get("link", "")
        # источник (если есть)
        source = ""
        if "source" in entry and getattr(entry.source, "title", None):
            source = entry.source.title
        elif "publisher" in entry:
            source = entry.publisher
        items.append((title, link, source))

    return items


def build_digest(header: str, feed_url: str) -> str:
    """
    Строит текст дайджеста из RSS-ленты.
    """
    news = fetch_news(feed_url)
    if not news:
        return (
            f"{header}\n\n"
            "Сегодня не удалось автоматически загрузить новости. "
            "Мы уже работаем над этим. ⏳"
        )

    lines = [header, ""]
    for i, (title, link, source) in enumerate(news, start=1):
        src = f" ({source})" if source else ""
        lines.append(f"{i}. {title}{src}\n{link}")

    lines.append("\nСпасибо, что вы с нами — @AI_News3773 🚀")
    return "\n".join(lines)


# ----------------- JOB-ФУНКЦИИ ДЛЯ РАСПИСАНИЯ -----------------
async def job_morning(context: ContextTypes.DEFAULT_TYPE):
    text = build_digest("🌅 Утренний дайджест ИИ", AI_FEED_RU)
    await context.bot.send_message(
        chat_id=CHANNEL_ID, text=text, disable_web_page_preview=True
    )


async def job_afternoon(context: ContextTypes.DEFAULT_TYPE):
    text = build_digest("📌 Дневной обзор ИИ", AI_FEED_EN)
    await context.bot.send_message(
        chat_id=CHANNEL_ID, text=text, disable_web_page_preview=True
    )


async def job_crypto(context: ContextTypes.DEFAULT_TYPE):
    text = build_digest("💹 ИИ и крипта — главное", AI_CRYPTO_FEED)
    await context.bot.send_message(
        chat_id=CHANNEL_ID, text=text, disable_web_page_preview=True
    )


async def job_evening(context: ContextTypes.DEFAULT_TYPE):
    text = build_digest("🌙 Вечерний дайджест ИИ", AI_FEED_RU)
    await context.bot.send_message(
        chat_id=CHANNEL_ID, text=text, disable_web_page_preview=True
    )


async def job_test_digest(context: ContextTypes.DEFAULT_TYPE):
    """
    Один раз после запуска — тестовый автодайджест,
    чтобы ты увидел, что всё работает.
    """
    text = build_digest("🧪 Тестовый автодайджест ИИ", AI_FEED_RU)
    await context.bot.send_message(
        chat_id=CHANNEL_ID, text=text, disable_web_page_preview=True
    )


# ----------------- ЗАПУСК ПРИЛОЖЕНИЯ -----------------
def main():
    application = Application.builder().token(TOKEN).build()

    job_queue = application.job_queue

    # Расписание по Душанбе
    job_queue.run_daily(
        job_morning,
        time=time(9, 0, tzinfo=TZ),
        name="morning_digest",
    )
    job_queue.run_daily(
        job_afternoon,
        time=time(12, 0, tzinfo=TZ),
        name="afternoon_digest",
    )
    job_queue.run_daily(
        job_crypto,
        time=time(18, 0, tzinfo=TZ),
        name="crypto_digest",
    )
    job_queue.run_daily(
        job_evening,
        time=time(21, 0, tzinfo=TZ),
        name="evening_digest",
    )

    # Тестовый дайджест через ~10 секунд после запуска
    job_queue.run_once(job_test_digest, when=10, name="test_digest")

    # allowed_updates=[] — бот НЕ получает апдейты,
    # работает только job_queue (чтобы не было конфликтов getUpdates)
    application.run_polling(allowed_updates=[])


if __name__ == "__main__":
    main()

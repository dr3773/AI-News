import os
import logging
from datetime import time
from zoneinfo import ZoneInfo

import feedparser
from openai import OpenAI
from telegram import Update
from telegram.constants import ParseMode
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
logger = logging.getLogger("ai-news-bot")

# ---------- НАСТРОЙКИ ----------

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_ID")  # твой личный ID (для /start и служебных сообщений)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID")

CHANNEL_ID = int(CHANNEL_ID)
ADMIN_ID = int(ADMIN_ID) if ADMIN_ID else None

TZ = ZoneInfo("Asia/Dushanbe")

# Google News уже тянет много крупных и авторитетных источников
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=ru&gl=RU&ceid=RU:ru",
]

# Память о том, какие ссылки уже публиковали (за жизнь процесса)
LAST_LINKS: set[str] = set()

# Клиент OpenAI (если ключа нет – бот просто будет брать текст из самой ленты)
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# ---------- УТИЛИТЫ ----------

def clean_text(text: str | None) -> str:
    if not text:
        return ""
    return (
        text.replace("&nbsp;", " ")
        .replace("\xa0", " ")
        .replace("\u200b", "")
        .strip()
    )


def fetch_news(limit: int = 20) -> list[dict]:
    """Забираем свежие новости из RSS и убираем дубли по ссылке."""
    items: list[dict] = []

    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            logger.exception("Ошибка парсинга RSS %s: %s", feed_url, e)
            continue

        feed_title = parsed.feed.get("title", "Новости ИИ")

        for entry in parsed.entries:
            title = clean_text(entry.get("title"))
            link = entry.get("link")
            summary = clean_text(
                entry.get("summary")
                or entry.get("description")
                or ""
            )

            if not title or not link:
                continue

            items.append(
                {
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "source": feed_title,
                }
            )

    # Удаляем дубли по ссылке, оставляем первые limit штук
    seen_links: set[str] = set()
    result: list[dict] = []
    for it in items:
        if it["link"] in seen_links:
            continue
        seen_links.add(it["link"])
        result.append(it)
        if len(result) >= limit:
            break

    return result


async def make_summary_ru(item: dict) -> str:
    """
    Делаем нормальную выжимку новости на русском.
    Если OpenAI недоступен — возвращаем текст из RSS.
    """
    base_text = item["summary"] or item["title"]

    if not openai_client:
        return base_text

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты редактор новостного канала про искусственный интеллект. "
                        "Пиши краткое, но содержательное резюме новости на русском языке. "
                        "4–7 связанных предложений, без воды, клише и лишней рекламы. "
                        "Не повторяй заголовок дословно, не обращайся к читателю напрямую."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Заголовок: {item['title']}\n\nТекст новости:\n{base_text}",
                },
            ],
            max_tokens=280,
            temperature=0.4,
        )
        text = resp.choices[0].message.content or ""
        return clean_text(text)
    except Exception as e:
        logger.exception("Ошибка при генерации выжимки: %s", e)
        return base_text


async def send_news_post(item: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Публикуем одну новость в канал в нужном формате."""
    summary = await make_summary_ru(item)

    parts: list[str] = [f"🧠 {item['title']}"]
    if summary and summary.lower() != item["title"].lower():
        parts.append("")
        parts.append(summary)

    # Строчка с источником. Ссылка спрятана за словом «Источник».
    parts.append("")
    parts.append(f"➜ <a href=\"{item['link']}\">Источник</a>")

    text = "\n".join(p for p in parts if p.strip())

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,  # Telegram сам подтянет картинку/превью
    )


# ---------- JOB'Ы ----------

async def periodic_news_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодическая публикация свежих новостей.
    Берём те, ссылки которых ещё не публиковались.
    """
    logger.info("Запуск periodic_news_job")
    news = fetch_news(limit=20)
    if not news:
        logger.info("Свежих новостей нет")
        return

    new_items = [n for n in news if n["link"] not in LAST_LINKS]

    if not new_items:
        logger.info("Новых ссылок нет")
        return

    # За один запуск публикуем максимум 3 новости, чтобы не спамить
    for item in new_items[:3]:
        LAST_LINKS.add(item["link"])
        await send_news_post(item, context)


async def evening_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вечерний дайджест — короткий список главных заголовков за день.
    """
    logger.info("Запуск evening_digest_job")

    news = fetch_news(limit=7)
    if not news:
        return

    lines: list[str] = [
        "🌙 Вечерний дайджест ИИ",
        "Краткий обзор заметных новостей за день:",
        "",
    ]

    for idx, item in enumerate(news, start=1):
        lines.append(f"{idx}. {item['title']}")

    text = "\n".join(lines)

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
    )


# ---------- HANDLERS ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ на /start в личке с ботом."""
    chat_id = update.effective_chat.id if update.effective_chat else None

    if ADMIN_ID and chat_id == ADMIN_ID:
        await update.message.reply_text(
            "🤖 AI News Bot запущен.\n"
            "Автопубликация новостей включена, вечерний дайджест в 21:00 (Душанбе)."
        )
    else:
        await update.message.reply_text(
            "Привет! Это новостной канал про искусственный интеллект.\n"
            "Все свежие новости автоматически публикуются в канале."
        )


# ---------- MAIN ----------

def main() -> None:
    logger.info("Старт приложения ai-news-bot")

    app = Application.builder().token(TOKEN).build()

    # Команда /start
    app.add_handler(CommandHandler("start", start))

    # JobQueue уже встроен в Application
    job_queue = app.job_queue

    # Периодическая публикация новостей (каждые 45 минут)
    job_queue.run_repeating(
        periodic_news_job,
        interval=45 * 60,   # 45 минут
        first=60,           # первый запуск через 1 минуту после старта
        name="periodic_news",
    )

    # Вечерний дайджест в 21:00 по Душанбе
    job_queue.run_daily(
        evening_digest_job,
        time=time(21, 0, tzinfo=TZ),
        name="evening_digest",
    )

    # ВАЖНО: никакого asyncio.run, никаких idle/shutdown вручную.
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()

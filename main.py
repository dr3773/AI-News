import os
import json
import logging
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from html import unescape, escape

import feedparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

# ====== ЛОГИ ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ai-news-bot")

# ====== НАСТРОЙКИ И ОКРУЖЕНИЕ ======
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID)
ADMIN_ID_INT = int(ADMIN_ID) if ADMIN_ID else None

TZ = ZoneInfo("Asia/Dushanbe")

# ====== OpenAI (опционально) ======
try:
    if OPENAI_API_KEY:
        from openai import AsyncOpenAI

        oai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    else:
        oai_client = None
except Exception as e:
    logger.warning("Не удалось инициализировать OpenAI: %s", e)
    oai_client = None

# ====== ИСТОЧНИКИ НОВОСТЕЙ ======
RSS_FEEDS = [
    # Google News по ИИ (русский)
    {
        "name": "Google News (ИИ, RU)",
        "url": "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    },
    # Google News по AI (английский, будем пересказывать по-русски)
    {
        "name": "Google News (AI, EN)",
        "url": "https://news.google.com/rss/search?q=artificial+intelligence&hl=en&gl=US&ceid=US:en",
    },
    # Habr — ИИ/машинное обучение
    {
        "name": "Habr (ML/AI)",
        "url": "https://habr.com/ru/rss/hub/machine_learning/all/",
    },
    # Forklog AI (через общий RSS Forklog — будем фильтровать по 'AI' в заголовке)
    {
        "name": "Forklog",
        "url": "https://forklog.com/feed",
    },
]

SEEN_FILE = "seen_urls.json"
TODAY_BUFFER_FILE = "today_news.json"


# ====== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ФАЙЛОВ ======
def load_json_set(path: str) -> set[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data)
    except FileNotFoundError:
        return set()
    except Exception as e:
        logger.warning("Не удалось прочитать %s: %s", path, e)
        return set()


def save_json_set(path: str, data: set[str]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(list(data), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Не удалось сохранить %s: %s", path, e)


def load_today_buffer() -> list[dict]:
    try:
        with open(TODAY_BUFFER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # фильтруем только сегодняшние
        today_str = datetime.now(tz=TZ).date().isoformat()
        return [item for item in data if item.get("date") == today_str]
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning("Не удалось прочитать today buffer: %s", e)
        return []


def save_today_buffer(items: list[dict]) -> None:
    try:
        with open(TODAY_BUFFER_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Не удалось сохранить today buffer: %s", e)


# ====== РАБОТА С RSS ======
def extract_image(entry) -> str | None:
    """Пытаемся достать картинку из записи RSS."""
    # media_content
    media = getattr(entry, "media_content", None)
    if media and isinstance(media, list):
        url = media[0].get("url")
        if url:
            return url

    # media_thumbnail
    thumb = getattr(entry, "media_thumbnail", None)
    if thumb and isinstance(thumb, list):
        url = thumb[0].get("url")
        if url:
            return url

    # Ссылки типа image/*
    for link in getattr(entry, "links", []):
        if link.get("type", "").startswith("image/") and link.get("href"):
            return link["href"]

    return None


def clean_html(text: str | None) -> str:
    if not text:
        return ""
    # Убираем HTML-теги очень грубо
    import re

    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    # Убираем лишние пробелы
    text = " ".join(text.split())
    return text


def fetch_raw_news(limit_per_feed: int = 5) -> list[dict]:
    """Собираем сырые новости из всех RSS."""
    items: list[dict] = []

    for feed in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed["url"])
        except Exception as e:
            logger.warning("Ошибка парсинга RSS %s: %s", feed["url"], e)
            continue

        for entry in parsed.entries[:limit_per_feed]:
            title = entry.get("title", "").strip()
            link = entry.get("link")
            if not link or not title:
                continue

            summary = clean_html(
                getattr(entry, "summary", None)
                or getattr(entry, "description", None)
            )

            image = extract_image(entry)

            # Попробуем дату
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                dt = datetime(*published[:6], tzinfo=TZ)
            else:
                dt = datetime.now(tz=TZ)

            source_name = feed["name"]
            items.append(
                {
                    "title": title,
                    "url": link,
                    "summary": summary,
                    "image": image,
                    "source": source_name,
                    "published": dt.isoformat(),
                }
            )

    # убираем дубли по ссылке
    seen = set()
    unique: list[dict] = []
    for item in sorted(items, key=lambda x: x["published"], reverse=True):
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)

    return unique


# ====== SUMMARIZE СТАТЬИ ЧЕРЕЗ OPENAI ======
async def build_russian_summary(raw: dict) -> str:
    """
    Делает нормальный человеческий пересказ новости на русском.
    Если OpenAI недоступен — делает простую, но опрятную выжимку.
    """
    title = clean_html(raw["title"])
    snippet = clean_html(raw.get("summary", ""))
    source = raw.get("source", "источник")

    base_text = snippet or title

    if not oai_client:
        # Фоллбэк без OpenAI
        if len(base_text) < 40:
            return f"{base_text}."
        return base_text

    try:
        content = (
            "Сделай краткий, но содержательный пересказ новости на русском языке. "
            "Пиши 3–5 связанных предложений, без воды, без приветствий, без выводов от себя. "
            "Не упоминай, что это пересказ. Просто нормальный текст для телеграм-канала.\n\n"
            f"Заголовок: {title}\n\nКраткое содержание/фрагмент новости:\n{base_text}\n"
            f"Источник: {source}"
        )

        resp = await oai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": content}],
            max_tokens=320,
            temperature=0.4,
        )
        summary = resp.choices[0].message.content.strip()
        return summary
    except Exception as e:
        logger.warning("Ошибка OpenAI: %s", e)
        return base_text


# ====== ФОРМИРОВАНИЕ ТЕКСТА ПОСТА ======
def build_post_text(title: str, summary: str, url: str) -> str:
    safe_title = escape(title)
    safe_summary = escape(summary)

    text = (
        f"🧠 <b>{safe_title}</b>\n\n"
        f"{safe_summary}\n\n"
        f"➜ <a href=\"{escape(url)}\">Источник</a>"
    )
    return text


async def send_news_post(app: Application, item: dict) -> None:
    """Публикует одну новость в канал: фото + текст или только текст."""
    title = clean_html(item["title"])
    summary = await build_russian_summary(item)
    url = item["url"]
    image = item.get("image")

    text = build_post_text(title, summary, url)

    # Ограничение для caption — 1024 символа
    if image:
        if len(text) > 1000:
            # Немного режем summary для подписи
            # (чтобы не отвалился parse_mode)
            lines = text.split("\n\n")
            # оставим заголовок + часть summary
            short = "\n\n".join(lines[:2])
            if len(short) > 950:
                short = short[:947] + "…"
            short += f"\n\n➜ <a href=\"{escape(url)}\">Источник</a>"
            text_to_send = short
        else:
            text_to_send = text

        try:
            await app.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image,
                caption=text_to_send,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception as e:
            logger.warning("Не удалось отправить фото, падаем в текст: %s", e)

    # Текстовый вариант
    await app.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )


# ====== АДМИН УВЕДОМЛЕНИЯ ======
async def notify_admin(app: Application, message: str) -> None:
    if not ADMIN_ID_INT:
        return
    try:
        await app.bot.send_message(chat_id=ADMIN_ID_INT, text=f"⚠️ AI News бот: {message}")
    except Exception as e:
        logger.warning("Не удалось отправить сообщение админу: %s", e)


# ====== ПЕРИОДИЧЕСКИЕ ЗАДАЧИ ======
async def periodic_news_check(app: Application) -> None:
    """
    Каждые 45 минут: ищем новые новости и публикуем отдельными постами.
    """
    try:
        logger.info("Запуск периодической проверки новостей")
        seen = load_json_set(SEEN_FILE)
        today_items = load_today_buffer()

        raw_items = fetch_raw_news(limit_per_feed=5)

        new_items: list[dict] = []
        for item in raw_items:
            if item["url"] in seen:
                continue

            # Немного фильтраций для Forklog: оставляем только AI
            if item["source"] == "Forklog" and "ai" not in item["title"].lower():
                continue

            seen.add(item["url"])
            new_items.append(item)

        if not new_items:
            logger.info("Новых новостей не найдено")
            save_json_set(SEEN_FILE, seen)
            return

        today_str = datetime.now(tz=TZ).date().isoformat()

        for item in reversed(new_items):  # старые сначала, свежие — в конце
            await send_news_post(app, item)

            today_items.append(
                {
                    "title": clean_html(item["title"]),
                    "url": item["url"],
                    "source": item["source"],
                    "date": today_str,
                }
            )

        save_json_set(SEEN_FILE, seen)
        save_today_buffer(today_items)

    except Exception as e:
        logger.exception("Ошибка в periodic_news_check: %s", e)
        await notify_admin(app, f"Ошибка в периодическом постинге: {e}")


async def send_daily_digest(app: Application) -> None:
    """
    Один раз в день в 21:00 — вечерний дайджест.
    """
    try:
        today_items = load_today_buffer()
        if not today_items:
            logger.info("За сегодня новостей в буфере нет — дайджест не отправляем")
            return

        lines = []
        for idx, item in enumerate(today_items, start=1):
            title = item["title"]
            url = item["url"]
            safe_title = escape(title)
            safe_url = escape(url)
            lines.append(
                f"{idx}. <a href=\"{safe_url}\">{safe_title}</a>"
            )

        text = (
            "🌙 <b>Вечерний дайджест ИИ</b>\n\n"
            "Подборка важных новостей об искусственном интеллекте за сегодня:\n\n"
            + "\n".join(lines)
        )

        await app.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )

    except Exception as e:
        logger.exception("Ошибка в send_daily_digest: %s", e)
        await notify_admin(app, f"Ошибка при отправке вечернего дайджеста: {e}")


# ====== ХЕНДЛЕРЫ БОТА ======
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🤖 AI News Bot запущен.\n\n"
        "Я автоматически публикую важные новости об искусственном интеллекте "
        "в течение дня и делаю вечерний дайджест в 21:00 по Душанбе."
    )
    await update.message.reply_text(text)


async def test_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Если ты напишешь боту 'test' — он сделает один тестовый пост в канал.
    Удобно для проверки без ожидания расписания.
    """
    await update.message.reply_text("Ок, делаю тестовую новость в канал (если что-то есть в лентах)…")
    await periodic_news_check(context.application)


# ====== MAIN ======
async def main() -> None:
    logger.info("Старт приложения AI News")

    app = Application.builder().token(TOKEN).build()

    # Команды /start
    app.add_handler(CommandHandler("start", start_handler))

    # Сообщение "test" — вручную дергаем постинг
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"(?i)^test$"),
            test_handler,
        )
    )

    # Планировщик
    scheduler = AsyncIOScheduler(timezone=TZ)

    # Каждые 45 минут — проверяем новости
    scheduler.add_job(
        periodic_news_check,
        "interval",
        minutes=45,
        args=[app],
        id="periodic_news",
        max_instances=1,
        coalesce=True,
    )

    # Каждый день в 21:00 — вечерний дайджест
    scheduler.add_job(
        send_daily_digest,
        "cron",
        hour=21,
        minute=0,
        args=[app],
        id="daily_digest",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    logger.info("Планировщик запущен")

    # Запускаем polling
    await app.run_polling(
        allowed_updates=["message"],
        stop_signals=None,  # Render сам перезапускает
    )


if __name__ == "__main__":
    asyncio.run(main())

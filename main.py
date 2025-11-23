import os
import logging
import html
import re
from datetime import time
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Set

import feedparser
from openai import AsyncOpenAI

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    Defaults,
    JobQueue,
)

# ================== НАСТРОЙКИ ==================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_ID")  # твой user_id (строкой)

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID)

if ADMIN_ID:
    ADMIN_ID = int(ADMIN_ID)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("Не найден OPENAI_API_KEY в переменных окружения")

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Google News по ИИ (разные запросы -> много разных источников)
RSS_FEEDS: List[str] = [
    # русские запросы
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=нейросеть&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=машинное+обучение&hl=ru&gl=RU&ceid=RU:ru",
    # английские запросы (даёт много мировых источников)
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=en&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=AI+startup&hl=en&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=LLM+model&hl=en&gl=US&ceid=US:en",
]

# сколько новостей максимум за один проход
MAX_ITEMS_PER_POLL = 5
# интервал опроса RSS (секунды) – каждые 15 минут
POLL_INTERVAL = 15 * 60

# будем помнить, что уже публиковали, чтобы не спамить дублями
SEEN_URLS: Set[str] = set()

# ================== ЛОГИ ==================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================


def strip_tags(text: str) -> str:
    """Убираем HTML-теги и &nbsp; из описаний RSS."""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<.*?>", "", text)
    text = text.replace("\xa0", " ").replace("&nbsp;", " ")
    return text.strip()


def extract_image(entry) -> str | None:
    """
    Пытаемся достать картинку из записи RSS (если есть).
    Для Google News иногда лежит в media_content.
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


def fetch_raw_news(limit: int = 20) -> List[Dict[str, Any]]:
    """
    Сырые новости из нескольких RSS.
    Возвращает список словарей: title, summary, url, source.
    """
    items: List[Dict[str, Any]] = []

    for feed_url in RSS_FEEDS:
        parsed = feedparser.parse(feed_url)
        source_title = parsed.feed.get("title", "Google News")

        for entry in parsed.entries:
            title = entry.get("title")
            link = entry.get("link")
            if not title or not link:
                continue

            summary = entry.get("summary", "") or entry.get("description", "")
            summary = strip_tags(summary)
            image = extract_image(entry)

            items.append(
                {
                    "title": title,
                    "summary": summary,
                    "url": link,
                    "source": source_title,
                    "image": image,
                }
            )

    # убираем дубли по ссылке, оставляем limit штук
    seen = set()
    unique_items: List[Dict[str, Any]] = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        unique_items.append(it)
        if len(unique_items) >= limit:
            break

    return unique_items


async def build_ai_post(item: Dict[str, Any]) -> str:
    """
    Строим нормальный пост на русском:
    - первая строка — короткий заголовок (без повтора исходного 1 в 1)
    - дальше 3–6 предложений нормального пересказа
    - в конце ➜ Источник (кликабельный, без длинной ссылки)
    """

    title = item["title"]
    summary = item["summary"]
    url = item["url"]
    source = item["source"]

    base_text = f"Заголовок: {title}\n\nКраткое описание (может быть пустым): {summary}\n\nИсточник: {source}"

    prompt = (
        "Ты — профессиональный русскоязычный редактор новостей по искусственному интеллекту.\n"
        "Получишь заголовок, краткое описание и источник.\n\n"
        "Сделай НОРМАЛЬНЫЙ пост для телеграм-канала:\n"
        "1) Первая строка: короткий, понятный заголовок на русском (до 120 символов), "
        "без дословного повтора исходного.\n"
        "2) Затем один пустой перенос строки.\n"
        "3) Затем 3–6 предложений развёрнутого пересказа новости по сути. "
        "Пиши живым языком, без воды, без клише, без слов 'эта новость', 'данный материал' и без 'что это значит'.\n"
        "4) Не пиши ссылку и слово 'Источник' — это я добавлю сам.\n"
        "5) Не используй разметку Markdown или HTML.\n"
    )

    try:
        resp = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": base_text},
            ],
            max_tokens=600,
            temperature=0.4,
        )
        text = resp.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("OpenAI error: %s", e)
        # fallback: просто используем заголовок + summary
        text_parts = [f"🧠 {title}"]
        if summary:
            text_parts.append("")
            text_parts.append(summary)
        text = "\n".join(text_parts)

    # добавляем строку с источником (кликабельная, без длинной урлы в тексте)
    post = text + f'\n\n➜ <a href="{html.escape(url)}">Источник</a>'
    return post


# ================== ОТПРАВКА НОВОСТЕЙ ==================


async def post_single_news(context: ContextTypes.DEFAULT_TYPE, item: Dict[str, Any]) -> None:
    """Отправляет один нормальный новостной пост в канал."""
    text = await build_ai_post(item)
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,  # пусть превью иногда подтягивается
    )


async def poll_news_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодический опрос RSS.
    Идея:
      - берём свежие новости
      - отфильтровываем уже опубликованные по URL
      - новые постим сразу в канал
    """
    logger.info("Запуск poll_news_job")
    global SEEN_URLS

    raw_items = fetch_raw_news(limit=MAX_ITEMS_PER_POLL * 2)

    new_items: List[Dict[str, Any]] = []
    for item in raw_items:
        url = item["url"]
        if url in SEEN_URLS:
            continue
        SEEN_URLS.add(url)
        new_items.append(item)

    if not new_items:
        logger.info("Новых новостей не найдено")
        return

    # ограничиваем чтобы не заваливать канал
    new_items = new_items[:MAX_ITEMS_PER_POLL]

    for item in new_items:
        try:
            await post_single_news(context, item)
        except Exception as e:
            logger.exception("Ошибка при отправке новости: %s", e)


async def daily_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вечерний дайджест в 21:00 — компактный список главных тем дня.
    Берём свежие новости ещё раз и просим ИИ сделать общий обзор.
    """
    logger.info("Запуск daily_digest_job")

    items = fetch_raw_news(limit=10)
    if not items:
        return

    # собираем краткий список для ИИ
    bullet_list = []
    for i, it in enumerate(items, start=1):
        bullet_list.append(f"{i}. {it['title']} — {it['summary'][:300]}")

    base_text = "\n".join(bullet_list)

    system_prompt = (
        "Ты — аналитик новостей по ИИ.\n"
        "На основе списка новостей составь вечерний дайджест для телеграм-канала:\n"
        "1) Заголовок: '🧠 Вечерний дайджест ИИ'.\n"
        "2) Далее 3–6 пунктов с кратким пересказом ключевых новостей дня.\n"
        "3) Пиши по-русски, без лишней воды и без 'что это значит'.\n"
        "4) Не добавляй ссылки — в дайджесте это не нужно.\n"
    )

    try:
        resp = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": base_text},
            ],
            max_tokens=800,
            temperature=0.4,
        )
        text = resp.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("OpenAI error в дайджесте: %s", e)
        text = "🧠 Вечерний дайджест ИИ\n\nСегодня вышло несколько важных новостей, но при формировании обзора произошла ошибка. Попробуем завтра ещё раз."

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    # уведомление админу, что дайджест отправлен
    if ADMIN_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text="✅ Вечерний дайджест ИИ отправлен в канал.",
            )
        except Exception:
            pass


# ================== ХЕНДЛЕРЫ БОТА ==================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ на /start в личке с ботом."""
    if update.effective_chat is None:
        return

    text = (
        "👋 Привет! Я AI News Bot.\n\n"
        "Я автоматически собираю важные новости об искусственном интеллекте "
        "из крупных мировых источников, делаю по ним нормальные человеческие "
        "пересказы и публикую их в канале:\n"
        "AI News Digest | ИИ Новости.\n\n"
        "Ты можешь просто подписаться на канал и читать там все посты. "
        "Вечером я делаю общий дайджест за день."
    )
    await update.effective_chat.send_message(text)


async def echo_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Если написать 'test' — принудительно выслать одну свежую новость в канал (для тебя)."""
    if update.effective_chat is None:
        return

    if update.effective_chat.type not in ("private",):
        return

    text = (update.message.text or "").strip().lower()
    if text != "test":
        return

    await update.effective_chat.send_message("Ок, пробую отправить свежую новость в канал…")

    items = fetch_raw_news(limit=5)
    for item in items:
        if item["url"] in SEEN_URLS:
            continue
        SEEN_URLS.add(item["url"])
        await post_single_news(context, item)
        break
    else:
        await update.effective_chat.send_message("Свежих новостей не нашлось.")


# ================== MAIN ==================


async def main() -> None:
    defaults = Defaults(parse_mode=ParseMode.HTML)

    app = (
        Application.builder()
        .token(TOKEN)
        .defaults(defaults)
        .job_queue(JobQueue())
        .build()
    )

    # хендлеры
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_test))

    # таймзона Душанбе
    tz = ZoneInfo("Asia/Dushanbe")

    # job_queue уже точно есть, т.к. мы явно передали JobQueue()
    jq = app.job_queue

    # опрос новостей каждые N минут
    jq.run_repeating(
        poll_news_job,
        interval=POLL_INTERVAL,
        first=30,  # через 30 секунд после старта
        name="poll_news",
    )

    # вечерний дайджест в 21:00
    jq.run_daily(
        daily_digest_job,
        time=time(21, 0, tzinfo=tz),
        name="daily_digest",
    )

    logger.info("Бот запускается…")
    await app.run_polling(close_loop=False)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())

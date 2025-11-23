import os
import logging
import html
from dataclasses import dataclass
from datetime import time
from typing import List, Optional, Set

from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import feedparser
from openai import OpenAI

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ===================== ЛОГИ =====================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ai-news-bot")

# ===================== НАСТРОЙКИ =====================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID_ENV = os.getenv("CHANNEL_ID")
ADMIN_ID_ENV = os.getenv("ADMIN_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")

if not CHANNEL_ID_ENV:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID_ENV)

if not ADMIN_ID_ENV:
    raise RuntimeError("Не найден ADMIN_ID в переменных окружения")

ADMIN_ID = int(ADMIN_ID_ENV)

# OpenAI клиент (для нормальных человеческих пересказов)
client: Optional[OpenAI]
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)
    USE_OPENAI = True
    logger.info("OpenAI клиент инициализирован")
else:
    client = None
    USE_OPENAI = False
    logger.warning(
        "OPENAI_API_KEY не задан. Будут использоваться упрощённые описания новостей."
    )

# Часовой пояс Душанбе
TZ = ZoneInfo("Asia/Dushanbe")

# Интервал проверки новостей (в секундах)
NEWS_CHECK_INTERVAL = 45 * 60  # 45 минут


# ===================== ИСТОЧНИКИ НОВОСТЕЙ =====================

# Все эти фиды отдают ИИ/tech-новости, дальше мы фильтруем и переформатируем
RSS_FEEDS: List[str] = [
    # Общий поиск по ИИ на русском
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    # ИИ по миру (английский, но мы переведём)
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=en&gl=US&ceid=US:en",
    # Машинное обучение
    "https://news.google.com/rss/search?q=machine+learning&hl=en&gl=US&ceid=US:en",
    # Нейросети
    "https://news.google.com/rss/search?q=neural+network&hl=ru&gl=RU&ceid=RU:ru",
]


# ===================== МОДЕЛЬ НОВОСТИ =====================

@dataclass
class NewsItem:
    title: str
    summary: str
    url: str
    source: str
    image: Optional[str] = None


# Уже отправленные ссылки (чтобы не было дублей)
SEEN_URLS: Set[str] = set()
# Новости за сегодня (для вечернего дайджеста)
TODAY_ITEMS: List[NewsItem] = []


# ===================== УТИЛИТЫ =====================

def clean_text(text: Optional[str]) -> str:
    if not text:
        return ""
    # убираем html-сущности и мусор
    text = text.replace("&nbsp;", " ")
    text = text.replace("\xa0", " ")
    return html.unescape(text).strip()


def extract_image(entry) -> Optional[str]:
    """Пытаемся вытащить картинку из RSS-записи, если есть."""
    # media_content
    media = getattr(entry, "media_content", None)
    if media and isinstance(media, list):
        url = media[0].get("url")
        if url:
            return url

    # enclosure
    enclosure = getattr(entry, "enclosures", None)
    if enclosure and isinstance(enclosure, list):
        for enc in enclosure:
            if enc.get("type", "").startswith("image/") and enc.get("href"):
                return enc["href"]

    # links
    links = getattr(entry, "links", [])
    for l in links:
        if l.get("type", "").startswith("image/") and l.get("href"):
            return l["href"]

    return None


def get_source_name(parsed_feed, feed_url: str) -> str:
    title = getattr(parsed_feed, "feed", {}).get("title")
    if title:
        return clean_text(title)
    return urlparse(feed_url).netloc or "Источник"


def build_russian_summary(title: str, description: str, source: str) -> str:
    """
    Строим нормальную русскую новость 4–7 предложений.
    Если OpenAI недоступен — возвращаем описание или заголовок.
    """
    if not USE_OPENAI or client is None:
        # простой режим
        base = description or title
        return base.strip()

    prompt = (
        "Сделай связную новостную заметку на русском языке по данным ниже.\n"
        "Размер 4–7 предложений, без повторения заголовка дословно, без воды, "
        "без обращений к читателю. Просто выжимка сути.\n\n"
        f"Заголовок: {title}\n"
        f"Описание/отрывок: {description}\n"
        f"Источник: {source}"
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "Ты новостной редактор. Пишешь чёткие и понятные заметки на русском.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=380,
            temperature=0.4,
        )
        text = resp.choices[0].message.content.strip()
        return text
    except Exception as e:
        logger.warning("Ошибка при обращении к OpenAI: %s", e)
        return (description or title).strip()


def collect_new_items(max_total: int = 5) -> List[NewsItem]:
    """
    Читаем RSS-фиды, забираем свежие новости, которых ещё не было.
    Возвращаем список новых NewsItem.
    """
    new_items: List[NewsItem] = []

    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            logger.warning("Ошибка парсинга фида %s: %s", feed_url, e)
            continue

        source_name = get_source_name(parsed, feed_url)

        entries = getattr(parsed, "entries", [])
        for entry in entries:
            if len(new_items) >= max_total:
                return new_items

            link = entry.get("link")
            if not link or link in SEEN_URLS:
                continue

            title = clean_text(entry.get("title", ""))
            if not title:
                continue

            description = clean_text(
                entry.get("summary") or entry.get("description") or ""
            )

            # строим нормальную русскую заметку
            summary = build_russian_summary(title, description, source_name)
            image = extract_image(entry)

            item = NewsItem(
                title=title,
                summary=summary,
                url=link,
                source=source_name,
                image=image,
            )

            SEEN_URLS.add(link)
            TODAY_ITEMS.append(item)
            new_items.append(item)

    return new_items


async def post_news_item(bot, item: NewsItem) -> None:
    """
    Публикация одной новости в канал.
    Формат:
    <жирный заголовок>
    пустая строка
    текст новости
    пустая строка
    ➜ Источник (ссылка)
    """
    title = html.escape(item.title)
    summary = html.escape(item.summary)

    # "Источник" как кликабельная ссылка
    source_link = f'➜ <a href="{html.escape(item.url)}">Источник</a>'

    text = f"<b>{title}</b>\n\n{summary}\n\n{source_link}"

    # Ограничение на подпись к фото — 1024 символа
    caption = text
    if len(caption) > 1024:
        caption = caption[:1000].rstrip() + "…\n\n" + source_link

    try:
        if item.image:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=item.image,
                caption=caption,
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                parse_mode="HTML",
            )
    except Exception as e:
        logger.error("Ошибка при отправке новости: %s", e)


# ===================== JOB'Ы =====================

async def periodic_news_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодический сбор и публикация свежих новостей."""
    logger.info("Запуск периодической проверки новостей")
    items = collect_new_items(max_total=5)
    if not items:
        logger.info("Новых новостей не найдено")
        return

    for item in items:
        await post_news_item(context.bot, item)


async def daily_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вечерний дайджест в 21:00 по Душанбе."""
    logger.info("Формируем вечерний дайджест: %d новостей", len(TODAY_ITEMS))
    if not TODAY_ITEMS:
        # Можем тихо ничего не отправлять или залогировать
        return

    lines: List[str] = []
    lines.append("🤖 <b>Вечерний дайджест ИИ</b>")
    lines.append("")
    lines.append("Сегодня в мире искусственного интеллекта случилось главное:")

    for idx, item in enumerate(TODAY_ITEMS[:15], start=1):
        title = html.escape(item.title)
        lines.append(f"{idx}. {title}")

    lines.append("")
    lines.append("Подробности по каждой новости уже есть в ленте канала 🔽")

    text = "\n".join(lines)

    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Ошибка при отправке дайджеста: %s", e)
    finally:
        # очищаем список новостей дня, но не SEEN_URLS
        TODAY_ITEMS.clear()


# ===================== ХЕНДЛЕРЫ КОМАНД =====================

def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ADMIN_ID)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        # в ответ на чужие /start можно ничего не слать
        return

    await update.message.reply_text(
        "🤖 AI News Bot запущен.\n\n"
        "• Периодически собираю свежие новости об ИИ из крупных источников.\n"
        "• Публикую их в канал с русским пересказом.\n"
        "• В 21:00 отправляю вечерний дайджест за день."
    )


async def test_news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return

    await update.message.reply_text("Ок, пробую отправить тестовую новость в канал.")
    items = collect_new_items(max_total=1)
    if not items:
        await update.message.reply_text("Свежих новостей сейчас не нашёл.")
        return

    await post_news_item(context.bot, items[0])
    await update.message.reply_text("Тестовая новость отправлена.")


async def digest_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return

    await update.message.reply_text("Отправляю пробный дайджест в канал.")
    dummy_context = ContextTypes.DEFAULT_TYPE
    # проще просто вызвать job-функцию, переиспользуя context из команды
    await daily_digest_job(context)
    await update.message.reply_text("Дайджест отправлен (если были новости за сегодня).")


# ===================== MAIN =====================

def main() -> None:
    logger.info("Запуск приложения")

    app = Application.builder().token(TOKEN).build()

    # Команды только для админа
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("test", test_news_command))
    app.add_handler(CommandHandler("digest_now", digest_now_command))

    # Расписание
    job_queue = app.job_queue

    # Периодическая проверка новостей (весь день)
    job_queue.run_repeating(
        periodic_news_job,
        interval=NEWS_CHECK_INTERVAL,
        first=10,  # через 10 секунд после запуска
        name="periodic_news",
    )

    # Вечерний дайджест в 21:00
    job_queue.run_daily(
        daily_digest_job,
        time=time(21, 0, tzinfo=TZ),
        name="daily_digest",
    )

    logger.info("Бот запущен, начинаю polling")
    app.run_polling(allowed_updates=[])


if __name__ == "__main__":
    main()

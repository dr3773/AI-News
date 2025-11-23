import os
import logging
import html
import re
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

import feedparser
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)


# ================= БАЗОВЫЕ НАСТРОЙКИ =================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")      # строка
ADMIN_ID = os.getenv("ADMIN_ID")          # можно не задавать

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID)
if ADMIN_ID:
    try:
        ADMIN_ID = int(ADMIN_ID)
    except ValueError:
        ADMIN_ID = None

# Логи, чтобы видеть, что происходит
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ================= ИСТОЧНИКИ НОВОСТЕЙ =================
# Google News сам тянет новости из множества авторитетных источников.
# Делаем несколько разных запросов по ИИ, чтобы охват был шире.

RSS_FEEDS = [
    # ИИ / искусственный интеллект (русский запрос)
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    # Нейросети (русский запрос)
    "https://news.google.com/rss/search?q=нейросети&hl=ru&gl=RU&ceid=RU:ru",
    # Artificial intelligence (английский запрос, но часто даёт хорошие статьи)
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=en&gl=US&ceid=US:en",
]

# чтобы не спамить одинаковыми ссылками
POSTED_URLS: set[str] = set()

# для вечернего дайджеста (запоминаем последние новости)
@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    short_text: str = ""
    image: str | None = None


RECENT_NEWS: list[NewsItem] = []
MAX_RECENT = 30  # сколько последних новостей держать в памяти


# ================= УТИЛИТЫ =================

def clean_html(text: str) -> str:
    """Убираем теги <...> из описания."""
    return re.sub(r"<[^>]+>", "", text or "")


def split_sentences(text: str) -> list[str]:
    """Примитивное разбиение на предложения."""
    text = text.strip()
    if not text:
        return []
    # режем по точкам/вопросам/восклицаниям
    parts = re.split(r"(?<=[\.\!\?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def build_russian_summary(raw_text: str, max_chars: int = 900) -> str:
    """
    Делаем осмысленную выжимку по-русски.
    Без ИИ-моделей, просто аккуратно режем текст.
    """
    text = clean_html(raw_text)

    # иногда в summary дублируется заголовок — убираем повторения
    sentences = split_sentences(text)

    if not sentences:
        return ""

    # берём первые 3–6 предложений, пока не превысили лимит
    result = []
    length = 0
    for s in sentences:
        if length + len(s) > max_chars and result:
            break
        result.append(s)
        length += len(s)

    summary = " ".join(result).strip()
    return summary


def extract_image(entry) -> str | None:
    """
    Достаём картинку из RSS-записи, если есть.
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


def fetch_ai_news(max_per_feed: int = 10) -> list[NewsItem]:
    """
    Собираем новости по ИИ из всех RSS_FEEDS.
    Возвращаем список NewsItem, отсортированный по времени (как пришло).
    """
    items: list[NewsItem] = []

    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            logger.warning("Ошибка парсинга RSS %s: %r", feed_url, e)
            continue

        source_title = parsed.feed.get("title", "Новости ИИ")

        for i, entry in enumerate(parsed.entries[:max_per_feed]):
            title = entry.get("title") or ""
            link = entry.get("link")
            if not link:
                continue

            summary_field = (
                entry.get("summary")
                or entry.get("description")
                or ""
            )

            short_text = build_russian_summary(summary_field)
            image = extract_image(entry)

            items.append(
                NewsItem(
                    title=title,
                    url=link,
                    source=source_title,
                    short_text=short_text,
                    image=image,
                )
            )

    # убираем дубли по url, сохраняем порядок
    seen = set()
    unique_items: list[NewsItem] = []
    for it in items:
        if it.url in seen:
            continue
        seen.add(it.url)
        unique_items.append(it)

    return unique_items


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, message: str) -> None:
    if not ADMIN_ID:
        return
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=message,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning("Не удалось отправить сообщение админу: %r", e)


async def send_news_post(context: ContextTypes.DEFAULT_TYPE, item: NewsItem) -> None:
    """
    Отправляем одну новость в канал в «красивом» виде.
    Формат:
    🧠 <жирный заголовок>

    основной текст (выжимка)

    ➜ Источник   (кликабельная ссылка)
    """
    title_html = html.escape(item.title)
    body_html = html.escape(item.short_text or item.title)

    source_link = f'➜ <a href="{html.escape(item.url)}">Источник</a>'

    text = f"🧠 <b>{title_html}</b>\n\n{body_html}\n\n{source_link}"

    # клавиатура под постом (дублируем ссылку как кнопку)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Читать полностью 📖",
                    url=item.url,
                )
            ]
        ]
    )

    if item.image:
        # сначала пробуем отправить с картинкой
        try:
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=item.image,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return
        except Exception as e:
            logger.warning("Не получилось отправить фото, шлём текст. Причина: %r", e)

    # если с картинкой не вышло — обычное сообщение
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ================= ЗАДАЧИ ДЛЯ JOB_QUEUE =================

async def job_post_fresh_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодически проверяем источники и публикуем новые новости.
    Ограничиваемся несколькими свежими за запуск, чтобы не спамить.
    """
    try:
        all_news = fetch_ai_news(max_per_feed=8)
        new_items: list[NewsItem] = []

        for item in all_news:
            if item.url in POSTED_URLS:
                continue
            new_items.append(item)

        if not new_items:
            logger.info("Свежих новостей не найдено.")
            return

        # чтобы не засыпать людей — максимум 5 постов за один проход
        for item in new_items[:5]:
            await send_news_post(context, item)

            POSTED_URLS.add(item.url)
            RECENT_NEWS.append(item)
            if len(RECENT_NEWS) > MAX_RECENT:
                del RECENT_NEWS[0]

        logger.info("Отправлено свежих новостей: %d", len(new_items[:5]))

    except Exception as e:
        logger.exception("Ошибка в job_post_fresh_news: %r", e)
        await notify_admin(
            context,
            f"❌ <b>Ошибка в job_post_fresh_news</b>\n<code>{html.escape(str(e))}</code>",
        )


async def job_daily_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вечерний дайджест в 21:00 — кратко напоминаем, что было за день.
    Берём последние несколько новостей из RECENT_NEWS.
    """
    try:
        if not RECENT_NEWS:
            logger.info("Для дайджеста новостей нет.")
            return

        # берём последние 5–7 новостей
        last_items = RECENT_NEWS[-7:]
        lines = ["🌙 <b>Вечерний дайджест ИИ</b>\n"]

        for i, item in enumerate(last_items, start=1):
            title = html.escape(item.title)
            line = f"{i}. {title}"
            lines.append(line)

        text = "\n".join(lines)

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        logger.exception("Ошибка в job_daily_digest: %r", e)
        await notify_admin(
            context,
            f"❌ <b>Ошибка в job_daily_digest</b>\n<code>{html.escape(str(e))}</code>",
        )


# ================= ХЕНДЛЕРЫ КОМАНД =================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 AI News Bot запущен.\n"
        "• Автоновости по ИИ — в течение дня.\n"
        "• Вечерний дайджест — в 21:00 (по Душанбе)."
    )


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ручной запуск: посмотрим, что бот считает свежими новостями.
    """
    await update.message.reply_text("Ок! Отправляю тестовый выпуск в канал.")
    await job_post_fresh_news(context)


# ================= MAIN =================

def main() -> None:
    app = Application.builder().token(TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("test", cmd_test))

    # расписание
    tz = ZoneInfo("Asia/Dushanbe")
    job_queue = app.job_queue

    # свежие новости каждые 30 минут
    job_queue.run_repeating(
        job_post_fresh_news,
        interval=30 * 60,      # 30 минут
        first=10,              # через 10 секунд после запуска
        name="fresh_news",
    )

    # вечерний дайджест в 21:00
    job_queue.run_daily(
        job_daily_digest,
        time=time(21, 0, tzinfo=tz),
        name="daily_digest",
    )

    # запускаем бота
    logger.info("Бот запущен, начинаем polling.")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()


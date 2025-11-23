import os
import logging
import html
import re
from datetime import time, datetime
from zoneinfo import ZoneInfo

import feedparser
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ContextTypes,
    Defaults,
    CommandHandler,
    MessageHandler,
    filters,
)

# ================== НАСТРОЙКИ ==================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_ID")  # не обязательно, но желательно

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID)
ADMIN_ID_INT = int(ADMIN_ID) if ADMIN_ID else None

TZ = ZoneInfo("Asia/Dushanbe")

# Большой набор источников по ИИ и технологиям.
RSS_FEEDS = [
    # Google News – ИИ по-русски
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=нейросети+OR+нейросеть+ИИ&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=GPT+чат-бот+ИИ&hl=ru&gl=RU&ceid=RU:ru",
    # Habr – все материалы, дальше фильтруем по ключевым словам
    "https://habr.com/ru/rss/all/all/",
    # РБК: наука и технологии
    "https://rssexport.rbc.ru/rbcnews/science_tech/index.rss",
    # ТАСС – общая лента, фильтруем по ключевым словам
    "https://tass.ru/rss/v2.xml",
]

AI_KEYWORDS = [
    "искусственный интеллект",
    "нейросеть",
    "нейросети",
    "ИИ",
    " gpt",
    "gpt-",
    "чат-бот",
    "чатбот",
    "machine learning",
    " ai ",
    "artificial intelligence",
]


# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====

def is_ai_related(text: str) -> bool:
    """Проверяем, относится ли новость к ИИ по ключевым словам."""
    lower = text.lower()
    return any(k.lower() in lower for k in AI_KEYWORDS)


def extract_image(entry) -> str | None:
    """Пытаемся достать картинку из RSS-записи."""
    media = getattr(entry, "media_content", None)
    if media and isinstance(media, list):
        url = media[0].get("url")
        if url:
            return url

    # Иногда картинки лежат в links
    for link in getattr(entry, "links", []):
        if link.get("type", "").startswith("image/") and link.get("href"):
            return link["href"]

    # Иногда ссылка на картинку в содержимом
    content = getattr(entry, "content", None)
    if content and isinstance(content, list):
        html_text = content[0].get("value", "")
        m = re.search(
            r'(https?://[^"\s]+\.(?:jpg|jpeg|png|gif))',
            html_text,
            re.IGNORECASE,
        )
        if m:
            return m.group(1)

    return None


def clean_html(text: str) -> str:
    """Убираем html-теги и спецсимволы, чистим &nbsp;."""
    if not text:
        return ""
    # убираем теги
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    # схлопываем пробелы
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_summary(entry) -> str:
    """
    Строим осмысленный текст новости:
    1) берём summary/description/content;
    2) чистим HTML;
    3) убираем дубли заголовка;
    4) длина ~ 600–700 символов (чтобы было что почитать).
    """
    title = clean_html(entry.get("title", ""))

    # возможные поля с описанием
    raw_parts = []
    for key in ("summary", "description"):
        if key in entry:
            raw_parts.append(str(entry.get(key, "")))

    # content (часто несколько абзацев)
    content = getattr(entry, "content", None)
    if content and isinstance(content, list):
        raw_parts.append(content[0].get("value", ""))

    text = " ".join(raw_parts)
    text = clean_html(text)

    # убираем прямое повторение заголовка
    if title and text.startswith(title):
        text = text[len(title):].lstrip(" .,-:–")

    # если после всех манипуляций текста нет — хотя бы заголовок
    if not text:
        return title

    # делаем осмысленную длину ~ 600–700 символов
    max_len = 700
    if len(text) <= max_len:
        return text

    # стараемся обрезать по концу предложения
    cut = text[:max_len]
    last_dot = cut.rfind(".")
    if last_dot > 200:  # чтобы не отрезать слишком рано
        cut = cut[: last_dot + 1]
    else:
        cut = cut.rstrip() + "…"
    return cut


def build_post(entry):
    """
    Собираем готовый текст и картинку.
    Формат:
    🧠 <b>Заголовок</b>

    Тело новости (нормальный пересказ).

    ➜ <a href="...">Источник</a>
    """
    title = clean_html(entry.get("title", ""))
    link = entry.get("link", "")

    summary = build_summary(entry)
    image = extract_image(entry)

    # заголовок
    header = f"🧠 <b>{html.escape(title)}</b>" if title else "🧠 <b>Новость по ИИ</b>"
    body = summary

    # «Источник» как кликаемое слово, без сырой ссылки
    footer = ""
    if link:
        safe_link = html.escape(link, quote=True)
        footer = f'\n\n➜ <a href="{safe_link}">Источник</a>'

    text = f"{header}\n\n{body}{footer}"
    return text, image, link


# Глобальные структуры для отслеживания уже опубликованных ссылок и дайджеста
SEEN_URLS: set[str] = set()
TODAY_ARTICLES: list[tuple[str, str]] = []  # (title, link)


async def send_news_post(context: ContextTypes.DEFAULT_TYPE, entry) -> None:
    """Отправляем одиночную новость в канал (с картинкой, если есть)."""
    text, image, link = build_post(entry)

    try:
        if image:
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image,
                caption=text,
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
    except Exception as e:
        logger.exception("Ошибка при отправке новости: %s", e)
        if ADMIN_ID_INT:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID_INT,
                    text=(
                        "⚠️ Ошибка при отправке новости:\n"
                        f"<code>{html.escape(str(e))}</code>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.exception("Не удалось отправить сообщение админу")
        return

    # добавляем в список для вечернего дайджеста
    title = clean_html(entry.get("title", ""))
    if link and title:
        TODAY_ARTICLES.append((title, link))


async def poll_feeds(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодически обходим все RSS, вытаскиваем новые ИИ-новости
    и сразу публикуем в канал.
    """
    logger.info("Проверяю RSS-ленты...")
    new_count = 0

    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            logger.exception("Ошибка при парсинге %s: %s", feed_url, e)
            continue

        for entry in parsed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "")

            # пропускаем старые/уже опубликованные
            if not link or link in SEEN_URLS:
                continue

            combined_text = " ".join(
                [title or "", getattr(entry, "summary", "") or ""]
            )
            if not is_ai_related(combined_text):
                continue

            # помечаем как опубликованную до реальной отправки,
            # чтобы не задвоить при повторном заходе
            SEEN_URLS.add(link)
            await send_news_post(context, entry)
            new_count += 1

    logger.info("Новых новостей: %s", new_count)


async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вечерний дайджест всех новостей за день (21:00)."""
    if not TODAY_ARTICLES:
        return

    today_str = datetime.now(TZ).strftime("%d.%m.%Y")

    lines = [f"📊 Вечерний дайджест ИИ — {today_str}", ""]
    for idx, (title, link) in enumerate(TODAY_ARTICLES, start=1):
        safe_link = html.escape(link, quote=True)
        lines.append(
            f'{idx}. {html.escape(title)} — <a href="{safe_link}">Источник</a>'
        )

    text = "\n".join(lines)

    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
    except Exception as e:
        logger.exception("Ошибка при отправке дайджеста: %s", e)
        if ADMIN_ID_INT:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID_INT,
                    text=(
                        "⚠️ Ошибка при отправке дайджеста:\n"
                        f"<code>{html.escape(str(e))}</code>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.exception("Не удалось отправить сообщение админу")
        return

    # очищаем список на следующий день
    TODAY_ARTICLES.clear()


# ===== ХЕНДЛЕРЫ ДЛЯ ЛИЧКИ БОТА =====

async def start_command(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start в личке: приветствие и краткое объяснение."""
    user = update.effective_user
    if not user or not update.message:
        return

    await update.message.reply_text(
        "🤖 Привет! Я бот AI News Digest.\n"
        "Я автоматически отслеживаю новости об искусственном интеллекте "
        "и публикую их в канале.\n"
        "Вечером ты получаешь короткий дайджест за день.\n\n"
        "Чтобы посмотреть пример прямо сейчас, напиши: test",
    )


async def test_command(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда /test — отправляем 3 свежие новости лично тебе,
    без публикации в канал.
    """
    chat_id = update.effective_chat.id
    news_items = []

    # берём несколько свежих новостей напрямую из RSS (без SEEN_URLS)
    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception:
            continue

        for entry in parsed.entries:
            title = entry.get("title", "")
            combined_text = " ".join(
                [title or "", getattr(entry, "summary", "") or ""]
            )
            if not is_ai_related(combined_text):
                continue
            news_items.append(entry)
            if len(news_items) >= 3:
                break
        if len(news_items) >= 3:
            break

    if not news_items:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Пока свежих новостей по ИИ не нашлось.",
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text="🧪 Тестовый мини-дайджест ИИ:",
    )

    # отправляем 3 новости в личку
    for entry in news_items:
        text, image, link = build_post(entry)
        if image:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=image,
                caption=text,
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )


async def echo_text(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Любой текст 'test' воспринимаем как запрос на тестовый дайджест."""
    if not update.message or not update.message.text:
        return

    if update.message.text.lower().strip() == "test":
        await test_command(update, context)


# ================== ЗАПУСК ПРИЛОЖЕНИЯ ==================

async def main() -> None:
    defaults = Defaults(parse_mode=ParseMode.HTML)
    app = Application.builder().token(TOKEN).defaults(defaults).build()

    # Команды в личке
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("test", test_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, echo_text)
    )

    # Периодическая проверка RSS (каждые 30 минут)
    app.job_queue.run_repeating(
        poll_feeds,
        interval=30 * 60,
        first=60,
        name="poll_feeds",
        job_kwargs={"misfire_grace_time": 60},
    )

    # Вечерний дайджест в 21:00 каждый день
    app.job_queue.run_daily(
        send_daily_digest,
        time=time(21, 0, tzinfo=TZ),
        name="daily_digest",
        job_kwargs={"misfire_grace_time": 300},
    )

    logger.info("Бот запущен (polling)")
    await app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())


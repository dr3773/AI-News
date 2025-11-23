import os
import sys
import types
import html
import logging
import asyncio
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import feedparser
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ----------------- ПАТЧ ДЛЯ feedparser НА PYTHON 3.13 -----------------
# В Python 3.13 удалили модуль cgi, а feedparser до сих пор его импортирует.
# Подсовываем "фейковый" cgi с нужной функцией escape.
if "cgi" not in sys.modules:
    fake_cgi = types.SimpleNamespace(
        escape=lambda s, quote=True: html.escape(s, quote=quote)
    )
    sys.modules["cgi"] = fake_cgi
# ----------------------------------------------------------------------


# -------------------------- НАСТРОЙКИ ---------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_ID")  # Твой личный ID (строкой)

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID)
ADMIN_ID = int(ADMIN_ID) if ADMIN_ID else None

# Временная зона бота
TZ = ZoneInfo("Asia/Dushanbe")

# RSS-источники по ИИ (Google News агрегирует много авторитетных медиа)
RSS_FEEDS = [
    # Общие новости по ИИ
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=нейросеть&hl=ru&gl=RU&ceid=RU:ru",

    # Специализированные ресурсы
    "https://habr.com/ru/rss/hub/artificial_intelligence/all/?fl=ru",
    "https://forklog.com/feed",  # Будем отбирать только ИИ-новости
]

# В памяти храним, какие ссылки уже публиковали,
# чтобы не спамить одинаковыми постами
ALREADY_SENT_URLS: set[str] = set()


# ---------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------------------


def clean_text(text: str) -> str:
    """Удаляем HTML-теги, &nbsp; и лишние пробелы."""
    if not text:
        return ""
    # Удаляем простые HTML-теги
    inside_tag = False
    out_chars = []
    for ch in text:
        if ch == "<":
            inside_tag = True
            continue
        if ch == ">":
            inside_tag = False
            continue
        if not inside_tag:
            out_chars.append(ch)
    text = "".join(out_chars)
    # Служебные сущности
    text = text.replace("&nbsp;", " ")
    text = html.unescape(text)
    # Нормализуем пробелы
    text = " ".join(text.split())
    return text


def extract_image(entry) -> str | None:
    """Пытаемся достать ссылку на картинку из RSS-записи."""
    media = getattr(entry, "media_content", None)
    if media and isinstance(media, list):
        url = media[0].get("url")
        if url:
            return url

    thumbs = getattr(entry, "media_thumbnail", None)
    if thumbs and isinstance(thumbs, list):
        url = thumbs[0].get("url")
        if url:
            return url

    links = getattr(entry, "links", [])
    for l in links:
        if l.get("type", "").startswith("image/") and l.get("href"):
            return l["href"]

    return None


def fetch_news(max_items: int = 30):
    """Собираем новости из всех RSS-источников.

    Возвращаем список словарей:
    {
      'title': ...,
      'summary': ...,
      'url': ...,
      'source': ...,
      'published': datetime | None,
      'image': url | None,
    }
    """
    items: list[dict] = []

    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            logger.warning("Ошибка парсинга %s: %s", feed_url, e)
            continue

        source_title = parsed.feed.get("title", "Новости ИИ")

        for entry in parsed.entries:
            title = entry.get("title") or ""
            link = entry.get("link")
            if not link:
                continue

            # Отфильтруем ForkLog: берём только новости, где упоминается AI / ИИ
            if "forklog" in (feed_url or "").lower():
                low = (title or "").lower()
                if ("искусствен" not in low) and ("нейросет" not in low) and ("ai " not in low):
                    continue

            summary = entry.get("summary") or entry.get("description") or ""
            summary = clean_text(summary)

            # Если summary получилось пустым — хотя бы не дублируем заголовок,
            # но чуть расширяем.
            if not summary:
                summary = f"В материале разбираются детали этой новости и её влияние на развитие ИИ."

            # Время публикации
            published_struct = (
                entry.get("published_parsed")
                or entry.get("updated_parsed")
                or None
            )
            if published_struct:
                published = datetime(
                    year=published_struct.tm_year,
                    month=published_struct.tm_mon,
                    day=published_struct.tm_mday,
                    hour=published_struct.tm_hour,
                    minute=published_struct.tm_min,
                    second=published_struct.tm_sec,
                    tzinfo=TZ,
                )
            else:
                published = None

            image = extract_image(entry)

            items.append(
                {
                    "title": clean_text(title),
                    "summary": summary,
                    "url": link,
                    "source": source_title,
                    "published": published,
                    "image": image,
                }
            )

    # Удаляем дубли по ссылке
    seen = set()
    unique_items = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        unique_items.append(it)

    # Сортируем по дате (свежие сверху)
    unique_items.sort(
        key=lambda x: x["published"] or datetime.now(TZ),
        reverse=True,
    )

    return unique_items[:max_items]


async def send_single_news(context: ContextTypes.DEFAULT_TYPE, item: dict):
    """Отправка одного полноценного поста в канал."""

    title = item["title"]
    summary = item["summary"]
    url = item["url"]

    # Текст поста: заголовок + развёрнутое описание + Источник
    text = (
        f"<b>{html.escape(title)}</b>\n\n"
        f"{html.escape(summary)}\n\n"
        f"➜ <a href=\"{html.escape(url, quote=True)}\">Источник</a>"
    )

    image = item.get("image")

    try:
        if image:
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image,
                caption=text,
                parse_mode="HTML",
            )
        else:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                parse_mode="HTML",
            )
    except Exception as e:
        logger.error("Ошибка при отправке новости: %s", e)
        if ADMIN_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"⚠️ Ошибка при отправке новости: {e}",
                )
            except Exception:
                pass


# ---------------------------- JOB'Ы -----------------------------------


async def job_poll_news(context: ContextTypes.DEFAULT_TYPE):
    """Периодическая проверка новых новостей.
    Каждые N минут отправляем до 3 ещё не опубликованных постов.
    """
    global ALREADY_SENT_URLS

    news = fetch_news(max_items=40)
    # Берём в обратном порядке, чтобы старые ушли раньше новых
    to_send = []
    for item in reversed(news):
        if item["url"] in ALREADY_SENT_URLS:
            continue
        to_send.append(item)

    # Ограничимся 3 новостями за один заход
    to_send = to_send[:3]

    if not to_send:
        return

    for item in to_send:
        await send_single_news(context, item)
        ALREADY_SENT_URLS.add(item["url"])


async def job_evening_digest(context: ContextTypes.DEFAULT_TYPE):
    """Вечерний дайджест в 21:00 — 3 самые свежие новости за сутки."""
    now = datetime.now(TZ)
    since = now - timedelta(hours=24)

    news = fetch_news(max_items=40)
    selected = [n for n in news if (n["published"] or now) >= since][:3]

    if not selected:
        # Если за день ничего не нашли — просто молчим
        return

    # Шапка дайджеста
    header = (
        "🌙 <b>Вечерний дайджест ИИ</b>\n"
        "Самое важное за последние 24 часа:\n"
    )
    lines = []
    for i, item in enumerate(selected, start=1):
        lines.append(f"{i}. {html.escape(item['title'])}")

    text = header + "\n".join(lines)

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="HTML",
    )


# ------------------------- ОБРАБОТЧИКИ КОМАНД -------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /start в личке с тобой."""
    if update.effective_chat is None:
        return

    chat_id = update.effective_chat.id
    if ADMIN_ID and chat_id != ADMIN_ID:
        # Для других людей можно что-то другое сделать,
        # но сейчас бот рассчитан только на тебя.
        await update.message.reply_text(
            "Этот бот настроен как новостной агрегатор для канала."
        )
        return

    await update.message.reply_text(
        "🤖 AI News Bot запущен.\n"
        "Он автоматически публикует важные новости об ИИ в канал\n"
        "и делает вечерний дайджест в 21:00."
    )


# --------------------------- MAIN -------------------------------------


async def main() -> None:
    app = Application.builder().token(TOKEN).build()

    # Команда /start для тебя
    app.add_handler(CommandHandler("start", start_command))

    # Периодический постинг новостей
    # Каждые 30 минут
    app.job_queue.run_repeating(
        job_poll_news,
        interval=30 * 60,
        first=10,  # через 10 секунд после запуска
        name="poll_news",
    )

    # Вечерний дайджест в 21:00 по Душанбе
    app.job_queue.run_daily(
        job_evening_digest,
        time=time(21, 0, tzinfo=TZ),
        name="evening_digest",
    )

    logger.info("Бот запущен (run_polling)")
    await app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())

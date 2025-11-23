import os
import logging
import html
from datetime import time
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from urllib.request import urlopen
import xml.etree.ElementTree as ET

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ================= НАСТРОЙКИ ОКРУЖЕНИЯ =================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID_ENV = os.getenv("CHANNEL_ID")
ADMIN_ID_ENV = os.getenv("ADMIN_ID")  # твой личный Telegram ID (для ошибок)

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")

if not CHANNEL_ID_ENV:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

try:
    CHANNEL_ID = int(CHANNEL_ID_ENV)
except ValueError:
    raise RuntimeError("CHANNEL_ID должен быть целым числом (например -1001234567890)")

ADMIN_ID = None
if ADMIN_ID_ENV:
    try:
        ADMIN_ID = int(ADMIN_ID_ENV)
    except ValueError:
        # если неправильно указали, просто не используем
        ADMIN_ID = None

# ================= ИСТОЧНИКИ НОВОСТЕЙ =================
# Здесь можно постепенно добавлять свои RSS-ленты про ИИ

RSS_FEEDS = [
    # Google News по запросу "искусственный интеллект" (рус)
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    # Google News по запросу "artificial intelligence" (англ, но описание часто на англ)
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=ru&gl=RU&ceid=RU:ru",
]

# Чтобы не спамить одинаковыми новостями
posted_urls = set()


# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================

def fetch_url(url: str, timeout: int = 10) -> bytes:
    """Загрузить сырые данные по URL."""
    with urlopen(url, timeout=timeout) as resp:
        return resp.read()


def strip_html(text: str) -> str:
    """Грубая очистка HTML из описания RSS."""
    import re

    if not text:
        return ""
    # убираем теги
    clean = re.sub(r"<[^>]+>", " ", text)
    # декодируем HTML-сущности
    clean = html.unescape(clean)
    # сжимаем пробелы
    clean = " ".join(clean.split())
    return clean


def parse_rss(url: str):
    """
    Простейший парсер RSS.
    Возвращает список словарей: title, url, summary, image.
    """
    items = []
    try:
        raw = fetch_url(url)
    except Exception as e:
        logging.exception("Ошибка загрузки RSS %s: %s", url, e)
        return items

    try:
        root = ET.fromstring(raw)
    except Exception as e:
        logging.exception("Ошибка разбора XML %s: %s", url, e)
        return items

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = (item.findtext("description") or "").strip()

        if not title or not link:
            continue

        summary = strip_html(description)

        # Картинка, если есть <enclosure type="image/*" url="...">
        image_url = None
        enclosure = item.find("enclosure")
        if enclosure is not None and enclosure.get("type", "").startswith("image"):
            image_url = enclosure.get("url")

        items.append(
            {
                "title": title,
                "url": link,
                "summary": summary,
                "image": image_url,
            }
        )

    return items


def fetch_ai_news(max_items: int = 5):
    """
    Собираем новости из всех RSS-лент,
    убираем дубли по URL и возвращаем первые max_items штук.
    """
    all_items = []

    for feed in RSS_FEEDS:
        parsed = parse_rss(feed)
        for item in parsed:
            # убираем уже отправленные
            if item["url"] in posted_urls:
                continue
            all_items.append(item)

    unique = []
    seen = set()
    for it in all_items:
        url = it["url"]
        if url in seen:
            continue
        seen.add(url)
        unique.append(it)
        if len(unique) >= max_items:
            break

    return unique


def build_source_line(url: str) -> str:
    """
    Строка вида:
    ➡️ Источник: <a href="...">rbc.ru</a>
    Никаких «Подробнее», «Google Новости» и т.п.
    """
    parsed = urlparse(url)
    host = parsed.netloc or "источник"
    if host.startswith("www."):
        host = host[4:]

    safe_url = html.escape(url, quote=True)
    safe_host = html.escape(host)

    return f'➡️ Источник: <a href="{safe_url}">{safe_host}</a>'


async def send_single_news(
    context: ContextTypes.DEFAULT_TYPE,
    item: dict,
    prefix_emoji: str = "🧠",
) -> None:
    """
    Отправка одной новости в канал:
    - жирный заголовок
    - короткое описание
    - в конце только строка с источником
    """
    title = html.escape(item["title"])

    summary = item.get("summary") or ""
    # режем описание, чтобы не было простыней
    if len(summary) > 700:
        summary = summary[:700].rsplit(" ", 1)[0] + "…"
    summary = html.escape(summary)

    source_line = build_source_line(item["url"])

    text = f"{prefix_emoji} <b>{title}</b>\n\n{summary}\n\n{source_line}"

    # Пытаемся отправить с фото
    image_url = item.get("image")
    if image_url:
        try:
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image_url,
                caption=text,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception as e:
            logging.exception("Не удалось отправить фото: %s. Падаем в текст.", e)

    # Текстовый вариант
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode=ParseMode.HTML,
    )


# ================= JOB'Ы =================

async def auto_news_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодический постинг свежих новостей в течение дня.
    Проверяем RSS, берём несколько новых, отправляем.
    """
    news = fetch_ai_news(max_items=3)
    if not news:
        logging.info("Свежих новостей сейчас нет.")
        return

    for item in news:
        url = item["url"]
        posted_urls.add(url)
        await send_single_news(context, item, prefix_emoji="🤖")


async def daily_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вечерний дайджест в 21:00 — краткий список с заголовками и ссылками.
    """
    news = fetch_ai_news(max_items=5)
    if not news:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text="Сегодня не нашлось достойных новостей по ИИ.",
        )
        return

    lines = ["🤖 Вечерний дайджест ИИ", ""]
    for i, item in enumerate(news, start=1):
        title = html.escape(item["title"])
        source_line = build_source_line(item["url"])
        lines.append(f"{i}. {title}")
        lines.append(source_line)
        lines.append("")

        posted_urls.add(item["url"])

    text = "\n".join(lines).strip()

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode=ParseMode.HTML,
    )


# ================= ОБРАБОТЧИКИ КОМАНД =================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 AI News Bot запущен.\n"
        "Буду публиковать свежие новости об ИИ в канал и делать вечерний дайджест в 21:00."
    )


async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /test — отправить одну тестовую новость в канал.
    Удобно для проверки, что всё работает.
    """
    news = fetch_ai_news(max_items=1)
    if not news:
        await update.message.reply_text("Свежих новостей пока не нашлось.")
        return

    item = news[0]
    posted_urls.add(item["url"])
    await send_single_news(context, item, prefix_emoji="🧪")
    await update.message.reply_text("Тестовая новость отправлена в канал.")


# ================= ОБРАБОТЧИК ОШИБОК =================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Ошибка в обработчике: %s", context.error)

    if ADMIN_ID is None:
        return

    try:
        text = f"⚠️ Ошибка в боте: {repr(context.error)}"
        await context.bot.send_message(chat_id=ADMIN_ID, text=text)
    except Exception:
        logging.exception("Не удалось отправить сообщение администратору.")


# ================= ЗАПУСК ПРИЛОЖЕНИЯ =================

def main() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("test", test_cmd))

    # Ошибки
    app.add_error_handler(error_handler)

    # Планировщик
    tz = ZoneInfo("Asia/Dushanbe")

    # Автоновости в течение дня — каждые 60 минут
    app.job_queue.run_repeating(
        auto_news_job,
        interval=60 * 60,   # 1 час
        first=30,           # первая проверка через 30 секунд после старта
        name="auto_news",
    )

    # Вечерний дайджест в 21:00
    app.job_queue.run_daily(
        daily_digest_job,
        time=time(21, 0, tzinfo=tz),
        name="daily_digest",
    )

    # Запускаем long polling
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()

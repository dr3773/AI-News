import os
import sys
import types
import time
import re
import html
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

# --- Хак для feedparser на Python 3.13 (там нет модуля cgi) ---
if "cgi" not in sys.modules:
    cgi = types.ModuleType("cgi")

    def escape(s, quote=True):
        return html.escape(s, quote=quote)

    cgi.escape = escape
    sys.modules["cgi"] = cgi

import feedparser  # noqa: E402

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from telegram.error import TelegramError, Conflict

# ========= НАСТРОЙКИ =========

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_ID")  # твой user_id, как строка

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в переменных окружения")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID не задан в переменных окружения")

try:
    CHANNEL_ID = int(CHANNEL_ID)
except ValueError:
    raise RuntimeError("CHANNEL_ID должен быть целым числом (id канала с минусом)")

if ADMIN_ID:
    try:
        ADMIN_ID = int(ADMIN_ID)
    except ValueError:
        ADMIN_ID = None

TZ = ZoneInfo("Asia/Dushanbe")

# Авторитетные источники по ИИ (можно дополнять)
RSS_FEEDS = [
    # Google News — ИИ по-русски
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=нейросети&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=машинное+обучение&hl=ru&gl=RU&ceid=RU:ru",
    # Google News — ИИ по-английски (важные мировые новости)
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=en&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=AI+startup&hl=en&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=OpenAI&hl=en&gl=US&ceid=US:en",
]

# В памяти храним, что уже публиковали
seen_urls: set[str] = set()
posted_today: list[dict] = []  # {"title": str, "url": str}


# ========= УТИЛИТЫ =========

def clean_html(text: str) -> str:
    """Грубая очистка HTML -> обычный текст."""
    if not text:
        return ""
    # убрать теги
    text = re.sub(r"<[^>]+>", " ", text)
    # HTML-сущности
    text = html.unescape(text)
    # сжать пробелы
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_image(entry) -> str | None:
    """Пытаемся достать ссылку на картинку из записи RSS."""
    media = getattr(entry, "media_content", None)
    if media and isinstance(media, list):
        url = media[0].get("url")
        if url:
            return url

    enclosures = getattr(entry, "enclosures", None)
    if enclosures and isinstance(enclosures, list):
        for e in enclosures:
            url = e.get("href")
            if url and e.get("type", "").startswith("image/"):
                return url

    links = getattr(entry, "links", [])
    for l in links:
        if l.get("type", "").startswith("image/") and l.get("href"):
            return l["href"]

    return None


def build_news_items(max_items: int = 5, only_new: bool = True) -> list[dict]:
    """
    Собираем новости из всех RSS.
    Возвращаем список словарей:
    {title, summary, url, image, source, published}
    """
    items: list[dict] = []
    now = datetime.now(timezone.utc)

    for feed_url in RSS_FEEDS:
        parsed = feedparser.parse(feed_url)
        source_title = parsed.feed.get("title", "Новости ИИ")

        for entry in parsed.entries:
            link = entry.get("link")
            if not link:
                continue

            if only_new and link in seen_urls:
                continue

            title_raw = entry.get("title", "").strip()
            title = clean_html(title_raw)

            # summary/description содержат уже осмысленную выжимку от издания
            summary_raw = (
                entry.get("summary")
                or entry.get("description")
                or ""
            )
            summary = clean_html(summary_raw)

            # убираем полное дублирование заголовка в тексте
            if summary and title and summary.lower().startswith(title.lower()):
                summary = summary[len(title):].strip(" .,-–—")

            if not summary:
                summary = title

            published_struct = (
                entry.get("published_parsed")
                or entry.get("updated_parsed")
            )
            if published_struct:
                published = datetime.fromtimestamp(
                    time.mktime(published_struct),
                    tz=timezone.utc,
                )
            else:
                published = now

            image = extract_image(entry)

            items.append(
                {
                    "title": title,
                    "summary": summary,
                    "url": link,
                    "image": image,
                    "source": source_title,
                    "published": published,
                }
            )

    # самые свежие сверху
    items.sort(key=lambda x: x["published"], reverse=True)

    # если only_new=True, ещё раз фильтруем вдруг где-то попались старые ссылки
    if only_new:
        unique: list[dict] = []
        local_seen: set[str] = set()
        for it in items:
            if it["url"] in seen_urls or it["url"] in local_seen:
                continue
            local_seen.add(it["url"])
            unique.append(it)
        items = unique

    return items[:max_items]


async def notify_admin(bot, text: str) -> None:
    if not ADMIN_ID:
        return
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text[:4000])
    except TelegramError:
        pass


async def send_news_post(bot, item: dict) -> None:
    """
    Отправляем ОДИН красивый пост в канал.
    Формат:
    🧠 Жирный заголовок

    Развёрнутый текст (из summary)

    ➜ Источник  (кликабельно, без уродливого URL)
    """
    title = item["title"]
    summary = item["summary"]
    url = item["url"]
    image = item["image"]

    # Текст поста
    body_lines = []

    if title:
        body_lines.append(f"🧠 <b>{html.escape(title)}</b>")

    if summary:
        body_lines.append("")
        body_lines.append(html.escape(summary))

    body_lines.append("")
    body_lines.append(f"➜ <a href=\"{html.escape(url)}\">Источник</a>")

    text = "\n".join(body_lines)

    # лимиты Telegram
    if len(text) > 4096:
        text = text[:4000] + "…"

    try:
        if image:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image,
                caption=text,
                parse_mode=ParseMode.HTML,
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                parse_mode=ParseMode.HTML,
            )
    except TelegramError as e:
        # Конфликт из-за getUpdates в браузере — просто игнорируем
        if isinstance(e, Conflict):
            return
        await notify_admin(bot, f"Ошибка отправки новости: {e!r}")


# ========= JOB-ФУНКЦИИ =========

async def push_fresh_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодически проверяем источники и постим свежие новости.
    """
    try:
        items = build_news_items(max_items=5, only_new=True)
    except Exception as e:
        await notify_admin(
            context.bot,
            f"Ошибка при загрузке новостей: {e!r}",
        )
        return

    if not items:
        return

    for item in items:
        url = item["url"]
        if url in seen_urls:
            continue

        await send_news_post(context.bot, item)

        seen_urls.add(url)
        posted_today.append({"title": item["title"], "url": url})


async def send_evening_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Один вечерний дайджест в 21:00 — список того, что уже вышло.
    """
    if not posted_today:
        # Ничего не постили — можно вообще молчать.
        return

    lines = [
        "🌙 <b>Вечерний дайджест ИИ</b>",
        "",
        "Сегодня в канале вышли самые важные новости об искусственном интеллекте:",
        "",
    ]

    for i, item in enumerate(posted_today, start=1):
        title = html.escape(item["title"])
        url = html.escape(item["url"])
        lines.append(f"{i}. <a href=\"{url}\">{title}</a>")

    text = "\n".join(lines)
    if len(text) > 4096:
        text = text[:4000] + "…"

    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        await notify_admin(context.bot, f"Ошибка вечернего дайджеста: {e!r}")

    # После дайджеста очищаем список новостей за день
    posted_today.clear()


# ========= COMMAND-HANDLERS =========

async def start(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Короткое приветствие в личке с ботом."""
    msg = (
        "👋 Привет!\n\n"
        "Я публикую важные новости об искусственном интеллекте "
        "в канале <b>AI News Digest | ИИ Новости</b>.\n\n"
        "В течение дня появляются свежие посты, а в 21:00 — один вечерний дайджест.\n"
        "Чтобы ничего не пропустить, подпишись на канал 🙂"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def test(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /test — только для тебя.
    Присылает одну свежую новость в канал и пишет тебе, что всё ок.
    """
    user_id = update.effective_user.id
    if ADMIN_ID and user_id != ADMIN_ID:
        return

    await update.message.reply_text("Пробую отправить тестовую новость в канал…")

    try:
        items = build_news_items(max_items=1, only_new=True)
        if not items:
            await update.message.reply_text("Свежих новостей сейчас не нашли.")
            return

        item = items[0]
        seen_urls.add(item["url"])
        posted_today.append({"title": item["title"], "url": item["url"]})
        await send_news_post(context.bot, item)

        await update.message.reply_text("Готово. Пост должен появиться в канале.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e!r}")
        await notify_admin(context.bot, f"/test упал: {e!r}")


# ========= ЗАПУСК ПРИЛОЖЕНИЯ =========

def main() -> None:
    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("test", test))

    # Планировщик
    jq = app.job_queue

    # Свежие новости: раз в 30 минут
    jq.run_repeating(
        push_fresh_news,
        interval=30 * 60,
        first=10,  # через 10 секунд после старта
        name="fresh_news",
    )

    # Вечерний дайджест в 21:00
    jq.run_daily(
        send_evening_digest,
        time=dtime(21, 0, tzinfo=TZ),
        name="evening_digest",
    )

    # Запускаем обычный polling
    app.run_polling()


if __name__ == "__main__":
    main()

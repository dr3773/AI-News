import os
import sys
import types
import logging
import re
from html import escape as html_escape, unescape as html_unescape
from datetime import time, date
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ----------------------------------------------------
# КОСТЫЛЬ ДЛЯ feedparser НА PYTHON 3.13 (нет модуля cgi)
# ----------------------------------------------------
cgi_mod = types.ModuleType("cgi")


def _cgi_escape(s, quote=True):
    return html_escape(s, quote=quote)


cgi_mod.escape = _cgi_escape
sys.modules.setdefault("cgi", cgi_mod)

import feedparser  # noqa: E402

# ----------------------------------------------------
# ЛОГИ
# ----------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------
# ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ----------------------------------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID_ENV = os.getenv("CHANNEL_ID")
ADMIN_ID_ENV = os.getenv("ADMIN_ID")

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID_ENV:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID_ENV)
ADMIN_ID = int(ADMIN_ID_ENV) if ADMIN_ID_ENV else None

# ----------------------------------------------------
# RSS-ИСТОЧНИКИ ПО ИИ (много разных запросов)
# ----------------------------------------------------
RSS_FEEDS = [
    # Общий ИИ
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=нейросети+ИИ&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=машинное+обучение&hl=ru&gl=RU&ceid=RU:ru",

    # Крупные игроки и тренды
    "https://news.google.com/rss/search?q=OpenAI&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=NVIDIA+AI&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=DeepMind&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=ChatGPT+или+GPT-4+или+GPT-5&hl=ru&gl=RU&ceid=RU:ru",

    # Бизнес и рынок ИИ
    "https://news.google.com/rss/search?q=стартап+искусственного+интеллекта&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=рынок+искусственного+интеллекта&hl=ru&gl=RU&ceid=RU:ru",
]

# ----------------------------------------------------
# ГЛОБАЛЬНОЕ СОСТОЯНИЕ
# ----------------------------------------------------
seen_links: set[str] = set()
today_articles: list[dict] = []
today_date: date = date.today()
TZ = ZoneInfo("Asia/Dushanbe")


# ----------------------------------------------------
# УТИЛИТЫ
# ----------------------------------------------------
def notify_admin_sync(bot, text: str) -> None:
    if not ADMIN_ID:
        return
    try:
        bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ {text}")
    except Exception as e:
        logger.warning("Не удалось отправить админу: %r", e)


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not ADMIN_ID:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ {text}")
    except Exception as e:
        logger.warning("Не удалось отправить админу: %r", e)


def clean_html(text: str | None) -> str:
    """Убираем теги, декодируем HTML, сжимаем пробелы."""
    if not text:
        return ""
    text = html_unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_russian(text: str) -> bool:
    """Есть ли кириллица — чтобы отсечь чисто англоязычные мусорные фиды."""
    return bool(re.search(r"[А-Яа-яЁё]", text))


def similarity(a: str, b: str) -> float:
    """Очень грубая 'похожесть' строк для отлова дублей заголовка."""
    a = a.lower().strip()
    b = b.lower().strip()
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    # доля символов a, которые входят в b
    same = sum(1 for ch in a if ch in b)
    return same / len(a)


def build_extended_summary(entry, max_len: int = 900) -> str | None:
    """
    Делаем осмысленный расширенный текст (несколько предложений),
    а не дубль заголовка. Если нормальной выжимки нет — вернём None
    (такую новость вообще не постим).
    """
    title = clean_html(entry.get("title") or "")
    # пробуем разные поля
    candidates = [
        entry.get("summary"),
        entry.get("description"),
    ]

    # иногда есть content
    content_list = entry.get("content")
    if isinstance(content_list, list) and content_list:
        candidates.append(content_list[0].get("value"))

    # Берём самый длинный из кандидатов
    raw = ""
    for c in candidates:
        if c and len(c) > len(raw):
            raw = c

    summary = clean_html(raw)

    # Если пусто – ничего не поделаешь
    if not summary:
        return None

    # Убираем очевидное дублирование заголовка в начале summary
    # Например: "Философ рассказал... Философ рассказал..."
    if summary.lower().startswith(title.lower()):
        summary = summary[len(title):].lstrip(" -—:–,.")
        summary = summary.strip()

    # Если после этого всё равно очень похоже на заголовок — выкидываем
    if similarity(summary, title) > 0.8 or len(summary) < 150:
        # меньше 150 символов — скорее всего, мусорная выжимка
        return None

    # Теперь аккуратно режем до max_len по предложениям/точкам
    if len(summary) > max_len:
        cut = summary[:max_len]
        last_dot = cut.rfind(".")
        if last_dot > max_len * 0.4:
            cut = cut[: last_dot + 1]
        summary = cut.strip() + "…"

    return summary


def extract_image(entry) -> str | None:
    """Достаём URL картинки, если есть."""
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


def reset_today_if_needed() -> None:
    global today_date, today_articles
    now = date.today()
    if now != today_date:
        today_date = now
        today_articles = []


# ----------------------------------------------------
# ЗАГРУЗКА НОВОСТЕЙ
# ----------------------------------------------------
def fetch_ai_news(limit: int = 10, only_new: bool = False) -> list[dict]:
    """
    Собираем новости из всех RSS_FEEDS.
    Только русские, только с нормальной выжимкой.
    Если only_new=True — возвращаем только те, которых нет в seen_links.
    """
    items: list[dict] = []

    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            logger.warning("Ошибка парсинга %s: %r", feed_url, e)
            continue

        for entry in parsed.entries:
            link = entry.get("link")
            title = clean_html(entry.get("title") or "")
            if not link or not title:
                continue

            if not is_russian(title):
                # если даже в заголовке нет кириллицы — пропускаем
                continue

            if only_new and link in seen_links:
                continue

            summary = build_extended_summary(entry)
            if not summary:
                # нет нормальной выжимки — не постим такую новость
                continue

            full_text = title + " " + summary
            if not is_russian(full_text):
                # защита от английских summary
                continue

            image = extract_image(entry)

            items.append(
                {
                    "title": title,
                    "url": link,
                    "summary": summary,
                    "image": image,
                }
            )

    # Удаляем дубли по ссылке и режем по лимиту
    seen_local = set()
    result: list[dict] = []
    for it in items:
        if it["url"] in seen_local:
            continue
        seen_local.add(it["url"])
        result.append(it)
        if len(result) >= limit:
            break

    return result


# ----------------------------------------------------
# ОТПРАВКА ОДНОГО ПОСТА
# ----------------------------------------------------
async def send_article(context: ContextTypes.DEFAULT_TYPE, item: dict) -> None:
    """
    Формат:
    🧠 <b>Заголовок</b>

    Нормальный расширенный текст (несколько предложений, выжимка статьи).

    ➜ Источник   (слово «Источник» — кликабельная ссылка, URL не видно)
    """
    title = item["title"]
    url = item["url"]
    summary = item["summary"]
    image = item["image"]

    header = f"🧠 <b>{html_escape(title)}</b>"
    body = html_escape(summary)
    footer = f'➜ <a href="{html_escape(url, quote=True)}">Источник</a>'

    text = f"{header}\n\n{body}\n\n{footer}"

    # caption для фото максимум ~1024 символа
    caption = text
    if len(caption) > 1000:
        # если длинно — слегка укоротим body
        short_body = summary
        if len(short_body) > 700:
            short_body_cut = short_body[:700]
            last_dot = short_body_cut.rfind(".")
            if last_dot > 200:
                short_body_cut = short_body_cut[: last_dot + 1]
            short_body = short_body_cut.strip() + "…"
        body_short = html_escape(short_body)
        caption = f"{header}\n\n{body_short}\n\n{footer}"

    try:
        if image:
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        logger.exception("Ошибка при отправке статьи: %r", e)
        await notify_admin(context, f"Ошибка при отправке статьи: {e!r}")


# ----------------------------------------------------
# ПЕРИОДИЧЕСКИЕ ЗАДАЧИ
# ----------------------------------------------------
async def poll_and_post_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Каждые N минут:
    - ищем новые новости с нормальным summary,
    - постим до 3–4 свежих материалов.
    """
    global seen_links, today_articles
    reset_today_if_needed()

    try:
        items = fetch_ai_news(limit=10, only_new=True)
    except Exception as e:
        logger.exception("Ошибка fetch_ai_news: %r", e)
        await notify_admin(context, f"Ошибка получения новостей: {e!r}")
        return

    if not items:
        return

    # не спамим — максимум 3 новости за один проход
    for item in items[:3]:
        seen_links.add(item["url"])
        today_articles.append(item)
        await send_article(context, item)


async def send_evening_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вечерний дайджест: просто список заголовков за день.
    Без повторного текста, чтобы не захламлять.
    """
    reset_today_if_needed()

    if not today_articles:
        return

    last_items = today_articles[-7:]  # до 7 главных новостей

    lines = ["📊 <b>Вечерний дайджест ИИ</b>", ""]
    lines.append("Ключевые новости за сегодня:")
    lines.append("")

    for i, item in enumerate(last_items, start=1):
        lines.append(f"{i}. {html_escape(item['title'])}")

    lines.append("")
    lines.append("Подробности — в сегодняшних постах выше 👆")

    text = "\n".join(lines)

    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.exception("Ошибка отправки дайджеста: %r", e)
        await notify_admin(context, f"Ошибка при отправке дайджеста: {e!r}")


# ----------------------------------------------------
# КОМАНДЫ
# ----------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.type == "private":
        await update.message.reply_text(
            "🤖 AI News Bot.\n\n"
            "• В течение дня публикую только осмысленные новости об ИИ (без дублирования заголовков).\n"
            "• В 21:00 делаю короткий дайджест заголовков за день."
        )


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручной тест: взять одну адекватную новость и отправить в канал."""
    await update.message.reply_text("Ок, публикую тестовую новость в канал.")
    try:
        items = fetch_ai_news(limit=5, only_new=True)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e!r}")
        return

    if not items:
        await update.message.reply_text("Пока нет новостей с нормальной выжимкой.")
        return

    item = items[0]
    seen_links.add(item["url"])
    today_articles.append(item)
    await send_article(context, item)


# ----------------------------------------------------
# MAIN
# ----------------------------------------------------
def main() -> None:
    app = (
        Application.builder()
        .token(TOKEN)
        .parse_mode(ParseMode.HTML)
        .build()
    )

    # команды
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("test", test_command))

    # задачи
    app.job_queue.run_repeating(
        poll_and_post_news,
        interval=30 * 60,  # каждые 30 минут
        first=20,
        name="poll_news",
    )

    app.job_queue.run_daily(
        send_evening_digest,
        time=time(21, 0, tzinfo=TZ),
        name="evening_digest",
    )

    # уведомление админу при старте
    async def on_startup(app_):
        if ADMIN_ID:
            notify_admin_sync(app_.bot, "✅ AI News Bot запущен.")

    app.post_init = on_startup

    logger.info("Запускаю AI News Bot...")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()

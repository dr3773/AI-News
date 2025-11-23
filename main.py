import os
import sys
import types
import html as _html
import logging
from datetime import date, time
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# --------------------------------------------------------------------
# ЛОГИРОВАНИЕ
# --------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------
# КОСТЫЛЬ ДЛЯ feedparser НА PYTHON 3.13 (МОДУЛЬ cgi УДАЛЁН)
# --------------------------------------------------------------------
# Создаём фейковый модуль cgi, чтобы feedparser не падал.
cgi_mod = types.ModuleType("cgi")


def _cgi_escape(s, quote=True):
    return _html.escape(s, quote=quote)


cgi_mod.escape = _cgi_escape
sys.modules.setdefault("cgi", cgi_mod)

import feedparser  # noqa: E402  (после вставки cgi)


# --------------------------------------------------------------------
# НАСТРОЙКИ И ОКРУЖЕНИЕ
# --------------------------------------------------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_ID")  # id твоего личного аккаунта

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID)

if ADMIN_ID:
    try:
        ADMIN_ID = int(ADMIN_ID)
    except ValueError:
        logger.warning("ADMIN_ID не является числом, уведомления админу отключены")
        ADMIN_ID = None
else:
    ADMIN_ID = None

# Только русскоязычные/основные источники по ИИ.
# При желании сюда можно добавлять RSS-ленты конкретных изданий.
RSS_FEEDS = [
    # Google News по запросу «искусственный интеллект»
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    # Можно дублировать с другими ключами
    "https://news.google.com/rss/search?q=нейросети+искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
]

# --------------------------------------------------------------------
# ГЛОБАЛЬНОЕ СОСТОЯНИЕ ДЛЯ НОВОСТЕЙ
# --------------------------------------------------------------------
seen_links: set[str] = set()       # уже опубликованные ссылки (за всё время работы процесса)
today_articles: list[dict] = []    # статьи за текущий день (для дайджеста)
today_date: date = date.today()    # чтобы в полночь чистить список


# --------------------------------------------------------------------
# УТИЛИТЫ
# --------------------------------------------------------------------
def clean_html(text: str) -> str:
    """Удаляем простейшие HTML-теги из summary."""
    if not text:
        return ""
    import re

    text = _html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_image(entry) -> str | None:
    """Пытаемся вытащить картинку из RSS-записи."""
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


def fetch_ai_news(max_per_feed: int = 10) -> list[dict]:
    """
    Забираем новости по ИИ из всех RSS_FEEDS.
    Возвращаем список словарей: {title, url, summary, image, source}.
    """
    items: list[dict] = []

    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:  # защитимся от падения одной ленты
            logger.warning("Ошибка парсинга RSS %s: %r", feed_url, e)
            continue

        source_title = parsed.feed.get("title", "Новости ИИ")

        for entry in parsed.entries[:max_per_feed]:
            title = entry.get("title")
            link = entry.get("link")
            if not title or not link:
                continue

            summary_raw = entry.get("summary") or entry.get("description") or ""
            summary = clean_html(summary_raw)

            # если summary слишком короткий и копирует заголовок — всё равно оставим
            image = extract_image(entry)

            items.append(
                {
                    "title": title.strip(),
                    "url": link.strip(),
                    "summary": summary,
                    "image": image,
                    "source": source_title,
                }
            )

    # можно было бы сортировать по дате, но для начала можно и так
    return items


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Отправка служебного сообщения админу (только в личку)."""
    if not ADMIN_ID:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ {text}")
    except Exception as e:
        logger.warning("Не удалось отправить сообщение админу: %r", e)


def reset_today_if_needed() -> None:
    """Если наступил новый день — очищаем список today_articles."""
    global today_date, today_articles
    now = date.today()
    if now != today_date:
        today_date = now
        today_articles = []


# --------------------------------------------------------------------
# ОТПРАВКА ОДНОЙ НОВОСТИ В КАНАЛ
# --------------------------------------------------------------------
async def send_article(context: ContextTypes.DEFAULT_TYPE, item: dict) -> None:
    """
    Формат поста:
    🧠 Заголовок

    Нормальный текст (пересказ из summary / description).

    ➡️ Источник   (слово «Источник» — кликабельная ссылка, домен не виден)
    """
    from telegram import Bot

    bot: Bot = context.bot

    title = item["title"]
    url = item["url"]
    summary = item["summary"]
    image = item["image"]

    # Если summary пустой — хотя бы один раз используем заголовок
    if not summary:
        summary = title

    # делаем текст чуть длиннее — summary часто уже нормальный абзац
    text_parts = [
        f"🧠 <b>{_html.escape(title)}</b>",
        "",
        _html.escape(summary),
        "",
        f'➡️ <a href="{_html.escape(url)}">Источник</a>',
    ]
    text = "\n".join(text_parts)

    try:
        if image:
            # Вариант 3: картинка + заголовок/текст под ней
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
    except Exception as e:
        logger.exception("Ошибка при отправке статьи: %r", e)
        await notify_admin(context, f"Ошибка при отправке новости: {e!r}")


# --------------------------------------------------------------------
# ПЕРИОДИЧЕСКАЯ ПРОВЕРКА НОВОСТЕЙ
# --------------------------------------------------------------------
async def poll_and_post_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Регулярно (например, каждые 20–30 минут) берём новости из RSS,
    отбрасываем уже опубликованные ссылки и постим только новые.
    """
    reset_today_if_needed()
    global seen_links, today_articles

    try:
        all_items = fetch_ai_news(max_per_feed=10)
    except Exception as e:
        logger.exception("Ошибка fetch_ai_news: %r", e)
        await notify_admin(context, f"Ошибка при загрузке новостей: {e!r}")
        return

    new_items: list[dict] = []
    for item in all_items:
        link = item["url"]
        if link in seen_links:
            continue
        seen_links.add(link)
        new_items.append(item)

    if not new_items:
        return

    # чтобы не заспамить — ограничим пачку, например, 3–4 новостями за цикл
    for item in new_items[:4]:
        await send_article(context, item)
        today_articles.append(item)


# --------------------------------------------------------------------
# ВЕЧЕРНИЙ ДАЙДЖЕСТ (21:00)
# --------------------------------------------------------------------
async def send_evening_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Один вечерний дайджест за день: список главных новостей."""
    reset_today_if_needed()

    if not today_articles:
        # ничего не было — ничего не шлём в канал
        return

    # Возьмём последние 5 новостей дня
    last_items = today_articles[-5:]

    lines = ["📊 <b>Вечерний дайджест ИИ</b>", "", "Главные новости сегодня:"]
    for i, item in enumerate(last_items, start=1):
        lines.append(f"{i}. {_html.escape(item['title'])}")

    lines.append("")
    lines.append("Подробнее — в постах выше за сегодня 👆")

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


# --------------------------------------------------------------------
# ОБРАБОТЧИКИ КОМАНД
# --------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запуск бота в личке /start."""
    msg = (
        "🤖 AI News Bot запущен.\n\n"
        "▫️ Днём он публикует важные новости об искусственном интеллекте по мере появления в источниках.\n"
        "▫️ В 21:00 по Душанбе выходит вечерний дайджест с главными заголовками за день.\n\n"
        "Если будут ошибки — я пришлю тебе уведомление отдельно."
    )
    await update.message.reply_text(msg)


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручной тест: взять одну свежую новость и отправить в канал."""
    await update.message.reply_text("Ок! Публикую тестовую новость в канал.")

    try:
        items = fetch_ai_news(max_per_feed=3)
    except Exception as e:
        logger.exception("Ошибка fetch_ai_news в /test: %r", e)
        await update.message.reply_text(f"Ошибка загрузки новостей: {e!r}")
        return

    if not items:
        await update.message.reply_text("Свежих новостей сейчас не нашлось.")
        return

    # Берём первую
    item = items[0]
    # Помечаем как увиденную, чтобы потом не дублировать
    seen_links.add(item["url"])
    today_articles.append(item)

    await send_article(context, item)


# --------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------
def main() -> None:
    app = (
        Application.builder()
        .token(TOKEN)
        .parse_mode(ParseMode.HTML)  # по умолчанию HTML-разметка
        .build()
    )

    # Команды
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("test", test_command))

    # Часовой пояс Душанбе
    tz = ZoneInfo("Asia/Dushanbe")

    # Периодическая проверка новостей (каждые 30 минут)
    app.job_queue.run_repeating(
        poll_and_post_news,
        interval=30 * 60,
        first=30,  # через 30 секунд после запуска
        name="poll_news",
    )

    # Вечерний дайджест в 21:00
    app.job_queue.run_daily(
        send_evening_digest,
        time=time(21, 0, tzinfo=tz),
        name="evening_digest",
    )

    logger.info("AI News Bot запускается (run_polling)...")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()

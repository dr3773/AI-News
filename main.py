import os
import re
from datetime import time
from zoneinfo import ZoneInfo
from html import unescape

import feedparser
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes, CommandHandler

# ========= НАСТРОЙКИ =========

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")      # ID канала (отрицательное число)
ADMIN_ID = os.getenv("ADMIN_ID")          # твой личный chat_id (строка)

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID)
ADMIN_ID_INT = int(ADMIN_ID) if ADMIN_ID else None

# --- МНОГО ИСТОЧНИКОВ ЧЕРЕЗ GOOGLE NEWS ---

# Каждый URL — это отдельная "виртуальная лента", внутри которой десятки крупных медиа.
RSS_FEEDS = [
    # Общие новости об ИИ на русском
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=нейросеть+ИИ&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=машинное+обучение&hl=ru&gl=RU&ceid=RU:ru",

    # Крупные игроки и тренды
    "https://news.google.com/rss/search?q=OpenAI&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=DeepMind&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=NVIDIA+AI&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=GPT-4+или+GPT-5&hl=ru&gl=RU&ceid=RU:ru",

    # Бизнес и рынок ИИ
    "https://news.google.com/rss/search?q=стартап+искусственного+интеллекта&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=рынок+искусственного+интеллекта&hl=ru&gl=RU&ceid=RU:ru",
]

# Запоминаем уже опубликованные ссылки (чтобы не спамить одинаковыми)
posted_urls: set[str] = set()


# ========= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =========

def clean_html(text: str | None) -> str:
    """Убираем HTML-теги и приводим текст к нормальному виду."""
    if not text:
        return ""
    # грубая очистка тегов
    text = re.sub(r"<.*?>", "", text)
    text = unescape(text)
    # заменяем кучки пробелов и переводов строки
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def extract_image(entry) -> str | None:
    """Пытаемся достать картинку из записи RSS."""
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


def get_source_title(entry, parsed_feed) -> str:
    """
    Берём человеческое название источника:
    - сначала entry.source.title (обычно 'РИА Новости', 'The Verge' и т.д.),
    - если нет, то заголовок самой RSS-ленты.
    """
    src = getattr(entry, "source", None)
    if isinstance(src, dict):
        title = src.get("title")
        if title:
            return title

    return parsed_feed.feed.get("title", "Источник")


def fetch_ai_news(limit: int = 5, only_new: bool = False):
    """
    Собираем новости по ИИ из нескольких RSS-лент.
    Возвращаем список словарей: title, url, image, source, summary.
    """
    items: list[dict] = []

    for feed_url in RSS_FEEDS:
        parsed = feedparser.parse(feed_url)

        for entry in parsed.entries:
            title = entry.get("title")
            link = entry.get("link")
            if not title or not link:
                continue

            if only_new and link in posted_urls:
                continue

            image = extract_image(entry)
            source = get_source_title(entry, parsed)
            summary_raw = entry.get("summary") or entry.get("description") or ""
            summary = clean_html(summary_raw)

            # Иногда summary пустой — тогда дублируем заголовок
            if not summary:
                summary = clean_html(title)

            items.append(
                {
                    "title": clean_html(title),
                    "url": link,
                    "image": image,
                    "source": source,
                    "summary": summary,
                }
            )

    # Удаляем дубли по ссылке, сортируем по порядку появления и режем по лимиту
    seen = set()
    unique_items = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        unique_items.append(it)
        if len(unique_items) >= limit:
            break

    return unique_items


# ========= ОТПРАВКА НОВОСТЕЙ =========

async def send_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Универсальный дайджест.
    Название (утренний/дневной/вечерний) берём из context.job.data["label"].
    """
    label: str = context.job.data.get("label", "Дайджест ИИ")

    news = fetch_ai_news(limit=5, only_new=False)

    if not news:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=f"⚠️ {label}\nСегодня свежих новостей по ИИ не нашлось.",
        )
        return

    # Заголовок выпуска
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=f"🤖 {label}\nПодборка важных новостей об искусственном интеллекте:",
    )

    for item in news:
        url = item["url"]
        title = item["title"]
        summary = item["summary"]

        # Делаем нормальный, более подробный текст (несколько предложений)
        body = f"<b>{title}</b>\n\n{summary}"

        # Ограничиваем по длине подписи / текста
        max_len = 1000
        if len(body) > max_len:
            body = body[: max_len - 1] + "…"

        # Внизу только стрелка и слово "Источник", без доменов и лишних фраз
        footer = f'\n\n➜ <a href="{url}">Источник</a>'
        text = body + footer

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=False,  # пусть будет превью, как у ForkLog
        )

        # Запоминаем, что эту ссылку уже публиковали
        posted_urls.add(url)


async def send_realtime_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодический постинг свежих новостей в течение дня.
    Берём только те, которых ещё не было (по URL).
    """
    news = fetch_ai_news(limit=3, only_new=True)

    if not news:
        # Ничего не пишем в канал, чтобы не спамить
        return

    for item in news:
        url = item["url"]
        title = item["title"]
        summary = item["summary"]

        body = f"<b>{title}</b>\n\n{summary}"
        max_len = 1000
        if len(body) > max_len:
            body = body[: max_len - 1] + "…"

        footer = f'\n\n➜ <a href="{url}">Источник</a>'
        text = body + footer

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=False,
        )

        posted_urls.add(url)


# ========= СЛУЖЕБНЫЕ ВЕЩИ =========

async def start_command(update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start в личке с ботом — просто служебное сообщение для тебя.
    Пользователям можно будет позже что-то красивое написать.
    """
    if update.effective_chat and update.effective_chat.type == "private":
        await update.message.reply_text(
            "🤖 AI News Bot запущен.\n"
            "Я публикую новости об искусственном интеллекте в канале."
        )


def main() -> None:
    app = Application.builder().token(TOKEN).build()

    # Команда /start (в личке)
    app.add_handler(CommandHandler("start", start_command))

    tz = ZoneInfo("Asia/Dushanbe")

    # 1) Периодический постинг свежих новостей в течение дня
    #    (каждый час проверяем источники и публикуем только новые материалы)
    app.job_queue.run_repeating(
        send_realtime_news,
        interval=60 * 60,        # каждые 60 минут
        first=10,                # старт через 10 секунд после запуска
        name="realtime_news",
    )

    # 2) Вечерний дайджест всех важных новостей (можно оставить как было — 21:00)
    app.job_queue.run_daily(
        send_digest,
        time=time(21, 0, tzinfo=tz),
        data={"label": "Вечерний дайджест ИИ"},
        name="evening_digest",
    )

    # Если указан ADMIN_ID — шлём тебе служебное сообщение при старте
    async def notify_admin(app_: Application):
        if ADMIN_ID_INT:
            try:
                await app_.bot.send_message(
                    chat_id=ADMIN_ID_INT,
                    text="✅ AI News Bot перезапущен и работает.",
                )
            except Exception:
                pass

    # Запускаем бот
    async def on_startup(app_: Application):
        await notify_admin(app_)

    app.post_init = on_startup

    # Один раз запускаем polling — без лишних asyncio.run и idle()
    app.run_polling(allowed_updates=["message", "edited_message"])


if __name__ == "__main__":
    main()

import os
import asyncio
import logging
import html
import re
from datetime import datetime, timedelta

import feedparser
from openai import OpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ----------------- базовые настройки -----------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0") or "0")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or "0")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Google News как агрегатор ИИ-новостей (разные языки)
RSS_FEEDS = [
    # ИИ на русском
    "https://news.google.com/rss/search?q=%22искусственный+интеллект%22+OR+ИИ+when:1d&hl=ru&gl=RU&ceid=RU:ru",
    # ИИ на английском
    "https://news.google.com/rss/search?q=AI+OR+%22artificial+intelligence%22+when:1d&hl=en&gl=US&ceid=US:en",
]

# какие новости уже публиковали
seen_ids: set[str] = set()
# когда в последний раз был вечерний дайджест
last_digest_date: datetime | None = None

LOCAL_UTC_OFFSET = 5  # твой пояс ~UTC+5


# ----------------- вспомогательные функции -----------------


def clean_summary(text: str) -> str:
    """Убираем html-теги, &nbsp; и прочий мусор из описания."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)        # теги
    text = html.unescape(text)                  # &nbsp; и др.
    text = " ".join(text.split())               # лишние пробелы
    return text


async def make_russian_body(title: str, summary: str, source: str) -> str:
    """
    Делаем нормальный текст новости по-русски:
    3–6 предложений, без HTML, без ссылок, без "Google Новости".
    """
    base = clean_summary(summary)

    # если вдруг нет ключа OpenAI – просто склеиваем заголовок + описание
    if client is None:
        combined = f"{title}. {base}" if base else title
        return combined

    prompt = (
        "У тебя есть новость об искусственном интеллекте.\n"
        "Нужно сделать связный новостной текст на русском языке.\n"
        "Требования:\n"
        "• 3–6 предложений.\n"
        "• Нейтральный, информативный стиль.\n"
        "• Без HTML-разметки, без ссылок, без упоминания Google Новостей.\n"
        "• Не копируй заголовок дословно, перефразируй его.\n\n"
        f"Заголовок: {title}\n\n"
        f"Краткое описание / детали: {base or 'нет описания, используй только заголовок.'}"
    )

    # выносим запрос к OpenAI в отдельный поток, чтобы не блокировать бота
    response = await asyncio.to_thread(
        client.chat.completions.create,
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "Ты лаконичный русскоязычный новостной редактор."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.5,
        max_tokens=320,
    )

    return response.choices[0].message.content.strip()


def parse_feeds(max_items: int = 20) -> list[dict]:
    """
    Читаем RSS-ленты, собираем список новостей:
    id, title, summary, link, source.
    """
    items: list[dict] = []
    per_feed = max(3, max_items // max(len(RSS_FEEDS), 1))

    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            logger.error("Ошибка парсинга фида %s: %s", url, e)
            continue

        for entry in feed.entries[:per_feed]:
            link = getattr(entry, "link", None)
            uid = getattr(entry, "id", link)
            if not link or not uid:
                continue

            title = getattr(entry, "title", "").strip()
            summary = getattr(entry, "summary", "")
            source = ""

            # пытаемся вытащить оригинальный источник – Habr, Коммерсант и т.п.
            if hasattr(entry, "source") and getattr(entry.source, "title", None):
                source = entry.source.title.strip()
            elif "-" in title:
                possible = title.split("-")[-1].strip()
                if 2 <= len(possible) <= 40:
                    source = possible

            items.append(
                {
                    "id": uid,
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "source": source,
                }
            )

    # убираем дубли по id
    uniq: dict[str, dict] = {}
    for it in items:
        uniq.setdefault(it["id"], it)

    return list(uniq.values())


async def publish_item(bot, item: dict) -> None:
    """
    Публикуем ОДНУ новость в канал в виде:
    🧠 Жирный заголовок
    нормальный текст
    ➜ Источник (кликабельно)
    """
    title = item["title"]
    summary = item.get("summary") or ""
    url = item["link"]

    body = await make_russian_body(title, summary, item.get("source") or "")

    title_html = html.escape(title)
    body_html = html.escape(body)
    url_html = html.escape(url, quote=True)

    text = (
        f"<b>🧠 {title_html}</b>\n\n"
        f"{body_html}\n\n"
        f"➜ <a href=\"{url_html}\">Источник</a>"
    )

    await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=False,
    )


async def send_digest(bot) -> None:
    """
    Вечерний дайджест: список 3–5 главных новостей за день.
    """
    global last_digest_date

    now_local = datetime.utcnow() + timedelta(hours=LOCAL_UTC_OFFSET)
    last_digest_date = now_local

    items = parse_feeds(max_items=12)
    new_items = [i for i in items if i["id"] not in seen_ids][:5]
    if not new_items:
        return

    lines: list[str] = []
    for idx, it in enumerate(new_items, start=1):
        title = html.escape(it["title"])
        url = html.escape(it["link"], quote=True)
        source = html.escape(it.get("source") or "")

        line = f"{idx}. <a href=\"{url}\">{title}</a>"
        if source:
            line += f" — {source}"

        lines.append(line)
        seen_ids.add(it["id"])

    text = "🤖 Вечерний дайджест ИИ\nГлавные новости за день:\n\n" + "\n".join(lines)

    await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ----------------- фоновый воркер -----------------


async def background_worker(app: Application) -> None:
    """
    Фоновый цикл:
    - раз в 15 минут ищет новые новости и постит их;
    - один раз в день в 21:00 делает дайджест.
    Никаких JobQueue, apscheduler и прочей фигни.
    """
    # на старте помечаем текущие новости как уже увиденные, чтобы не заспамить канал
    for it in parse_feeds(max_items=30):
        seen_ids.add(it["id"])

    if ADMIN_ID:
        try:
            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text="🤖 AI News Bot запущен. Новости и дайджесты будут приходить автоматически.",
            )
        except Exception as e:
            logger.warning("Не удалось отправить сообщение администратору: %s", e)

    while True:
        try:
            # 1) новые одиночные новости
            items = parse_feeds(max_items=8)
            # идём от старых к новым, чтобы порядок в канале был нормальный
            for it in reversed(items):
                if it["id"] in seen_ids:
                    continue
                seen_ids.add(it["id"])
                await publish_item(app.bot, it)

            # 2) вечерний дайджест (21:00 по местному)
            now_local = datetime.utcnow() + timedelta(hours=LOCAL_UTC_OFFSET)
            if now_local.hour == 21:
                if not last_digest_date or last_digest_date.date() != now_local.date():
                    await send_digest(app.bot)

        except Exception as e:
            logger.error("Ошибка в background_worker: %s", e)
            if ADMIN_ID:
                try:
                    await app.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=f"⚠️ Ошибка в боте новостей: {e}",
                    )
                except Exception:
                    pass

        # ждём 15 минут и снова проверяем источники
        await asyncio.sleep(900)


# ----------------- хэндлеры команд -----------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 AI News Bot.\n"
        "Я публикую в канал свежие новости об искусственном интеллекте "
        "и один вечерний дайджест в 21:00."
    )


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /test или сообщение 'test' в личку боту:
    берём первую новую новость и публикуем её в канал.
    """
    await update.message.reply_text("Ок! Публикую тестовую новость в канал.")

    items = parse_feeds(max_items=5)
    for it in items:
        if it["id"] not in seen_ids:
            seen_ids.add(it["id"])
            await publish_item(context.bot, it)
            break
    else:
        await update.message.reply_text("Пока нет новых новостей для теста.")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip().lower()
    if text == "test":
        await test_command(update, context)


async def on_startup(app: Application) -> None:
    # запускаем фоновый воркер после инициализации приложения
    asyncio.create_task(background_worker(app))


# ----------------- точка входа -----------------


async def main() -> None:
    if not TOKEN or not CHANNEL_ID:
        raise RuntimeError("Не заданы TELEGRAM_BOT_TOKEN или CHANNEL_ID в переменных окружения.")

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("test", test_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # хук, который вызовется сразу после инициализации и запустит background_worker
    application.post_init = on_startup

    await application.run_polling(close_loop=False)


if name == "__main__":
    import asyncio
    asyncio.get_event_loop().run_until_complete(main())

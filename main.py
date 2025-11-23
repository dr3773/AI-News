import os
import logging
import random
from datetime import time
from zoneinfo import ZoneInfo
from html import escape as html_escape

import feedparser
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ===== ЛОГИРОВАНИЕ =====
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ===== НАСТРОЙКИ И ТОКЕНЫ =====
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID_ENV = os.getenv("CHANNEL_ID")
ADMIN_ID_ENV = os.getenv("ADMIN_ID")  # твой ID, чтобы слать ошибки (можно не задавать)

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID_ENV:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

try:
    CHANNEL_ID = int(CHANNEL_ID_ENV)
except ValueError:
    raise RuntimeError("CHANNEL_ID должен быть числом (например -1003238891648)")

ADMIN_ID: int | None = None
if ADMIN_ID_ENV:
    try:
        ADMIN_ID = int(ADMIN_ID_ENV)
    except ValueError:
        logger.warning("ADMIN_ID задан некорректно, уведомления админу отключены")


# ===== ИСТОЧНИКИ НОВОСТЕЙ (РАСШИРЕННЫЙ НАБОР) =====
RSS_FEEDS = [
    # Русский ИИ
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=нейросети&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=машинное+обучение&hl=ru&gl=RU&ceid=RU:ru",

    # Английский ИИ
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=machine+learning&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=\"generative+ai\"+OR+genai&hl=en-US&gl=US&ceid=US:en",
]


def extract_image(entry) -> str | None:
    """
    Достаём картинку из RSS-записи, если она есть.
    Для Google News иногда лежит в media_content или ссылках с type=image/*.
    """
    media = getattr(entry, "media_content", None)
    if media and isinstance(media, list):
        url = media[0].get("url")
        if url:
            return url

    links = getattr(entry, "links", [])
    for link in links:
        if link.get("type", "").startswith("image/") and link.get("href"):
            return link["href"]

    return None


def fetch_ai_news(limit: int = 3) -> list[dict]:
    """
    Собираем новости по ИИ из нескольких RSS-лент.
    Возвращаем список словарей: title, url, image, source.
    """
    items: list[dict] = []

    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            logger.warning("Не удалось прочитать RSS %s: %s", feed_url, e)
            continue

        source_title = parsed.feed.get("title", "Новости ИИ")

        for entry in parsed.entries:
            title = entry.get("title")
            link = entry.get("link")
            if not title or not link:
                continue

            image = extract_image(entry)

            items.append(
                {
                    "title": title,
                    "url": link,
                    "image": image,
                    "source": source_title,
                }
            )

    if not items:
        return []

    # Перемешиваем, чтобы каждый дайджест был чуть разный
    random.shuffle(items)

    # Удаляем дубли по ссылке и ограничиваем количеством
    seen = set()
    unique_items: list[dict] = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        unique_items.append(it)
        if len(unique_items) >= limit:
            break

    return unique_items


async def post_digest(label: str, application: Application) -> None:
    """
    Общая функция отправки дайджеста в канал.
    label — заголовок (утренний/дневной/вечерний и т.п.).
    """
    try:
        news = fetch_ai_news(limit=3)
    except Exception as e:
        logger.exception("Ошибка при получении новостей")
        if ADMIN_ID:
            try:
                await application.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"⚠️ {label}: ошибка при получении новостей: {e}",
                )
            except Exception:
                pass
        return

    if not news:
        text = (
            f"⚠️ {label}\n"
            f"Сегодня свежих новостей по ИИ не нашлось. "
            f"Попробуем снова в следующем выпуске."
        )
        await application.bot.send_message(chat_id=CHANNEL_ID, text=text)
        return

    # Заголовок выпуска
    header = (
        f"🤖 {label}\n"
        f"Подборка свежих новостей об искусственном интеллекте:"
    )
    await application.bot.send_message(chat_id=CHANNEL_ID, text=header)

    # Каждую новость отправляем отдельным сообщением
    for i, item in enumerate(news, start=1):
        title = item["title"]
        url = item["url"]
        image = item["image"]
        source = item["source"]

        safe_url = html_escape(url, quote=True)
        safe_source = html_escape(source, quote=True)

        # Источник — кликабельная ссылка, без текста "читать полностью"
        caption = (
            f"{i}. {title}\n"
            f'📎 Источник: <a href="{safe_url}">{safe_source}</a>'
        )

        # Ограничение на длину подписи
        if len(caption) > 1024:
            caption = caption[:1020] + "…"

        try:
            if image:
                try:
                    await application.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=image,
                        caption=caption,
                        parse_mode="HTML",
                    )
                    continue
                except Exception as e_photo:
                    logger.warning("Не удалось отправить фото (%s): %s", image, e_photo)

            await application.bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("Ошибка при отправке новости в канал")
            if ADMIN_ID:
                try:
                    await application.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            f"⚠️ {label}: ошибка при отправке новости.\n"
                            f"Новость: {title}\nПричина: {e}"
                        ),
                    )
                except Exception:
                    pass


# ====== JOB-ФУНКЦИЯ ДЛЯ ПЛАНИРОВЩИКА ======
async def send_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обёртка для job_queue: достаём label из context.job.data и шлём дайджест."""
    label: str = context.job.data.get("label", "Дайджест ИИ")
    await post_digest(label, context.application)


# ===== ХЕНДЛЕРЫ КОМАНД БОТА =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start в личке с ботом:
    - отвечает текстом,
    - отправляет тестовый дайджест в канал.
    """
    chat_id = update.effective_chat.id

    await update.message.reply_text(
        "🤖 Привет! Я AI News Bot.\n\n"
        "Каждый день я делаю несколько дайджестов новостей об искусственном интеллекте "
        "и публикую их в канале.\n\n"
        "Сейчас отправлю тестовый дайджест в канал, чтобы всё проверить."
    )

    await post_digest("Тестовый автодайджест ИИ", context.application)

    if ADMIN_ID and chat_id != ADMIN_ID:
        logger.info("Команду /start использовал пользователь %s", chat_id)


# ===== ОБРАБОТЧИК ОШИБОК =====
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка при обработке апдейта:", exc_info=context.error)

    if ADMIN_ID:
        try:
            msg = f"⚠️ AI News Bot: ошибка: {context.error}"
            await context.application.bot.send_message(chat_id=ADMIN_ID, text=msg)
        except Exception as e:
            logger.error("Не удалось отправить ошибку админу: %s", e)


# ===== ОСНОВНАЯ ФУНКЦИЯ =====
async def main() -> None:
    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))

    # Обработчик ошибок
    app.add_error_handler(error_handler)

    # Часовой пояс Душанбе
    tz = ZoneInfo("Asia/Dushanbe")

    # Расписание дайджестов
    schedule = [
        ("Утренний дайджест ИИ", time(9, 0, tzinfo=tz)),
        ("Дневной дайджест ИИ", time(12, 0, tzinfo=tz)),
        ("Дневной дайджест ИИ", time(15, 0, tzinfo=tz)),
        ("Вечерний дайджест ИИ", time(18, 0, tzinfo=tz)),
        ("Ночной дайджест ИИ", time(21, 0, tzinfo=tz)),
    ]

    for label, t in schedule:
        app.job_queue.run_daily(
            send_digest_job,
            time=t,
            data={"label": label},
            name=label,
        )

    logging.info("Запускаю бота с расписанием дайджестов…")

    # Разрешаем только сообщения (для /start), остальное не нужно
    await app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

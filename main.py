import os
import logging
from datetime import time
from zoneinfo import ZoneInfo
from html import unescape
from typing import List, Dict, Optional
import urllib.request
import xml.etree.ElementTree as ET

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------- ЛОГИ ----------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- НАСТРОЙКИ ----------

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID_ENV = os.getenv("CHANNEL_ID")

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID_ENV:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID_ENV)

# твой Telegram ID — для личных уведомлений
OWNER_ID = 797726160

# Google News RSS по ИИ (ru + en)
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=ru&gl=RU&ceid=RU:ru",
]

# namespace для media:thumbnail / media:content
NS = {"media": "http://search.yahoo.com/mrss/"}


# ---------- РАБОТА С RSS ----------

def _fetch_rss(url: str, limit: Optional[int] = None) -> List[Dict]:
    """Скачиваем и разбираем один RSS-фид без сторонних библиотек."""
    logger.info("Загружаю RSS: %s", url)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = resp.read()
    except Exception as e:
        logger.warning("Не удалось загрузить RSS %s: %s", url, e)
        return []

    try:
        root = ET.fromstring(data)
    except Exception as e:
        logger.warning("Не удалось распарсить RSS %s: %s", url, e)
        return []

    channel_title = (
        root.findtext("./channel/title")
        or root.findtext(".//title")
        or "Новости ИИ"
    )

    items: List[Dict] = []
    for item in root.findall(".//item"):
        title = item.findtext("title")
        link = item.findtext("link")

        if not title or not link:
            continue

        title = unescape(title.strip())
        link = link.strip()

        # Пытаемся достать картинку
        image: Optional[str] = None
        media_content = item.find("media:content", NS)
        if media_content is not None:
            image = media_content.get("url")

        if not image:
            media_thumb = item.find("media:thumbnail", NS)
            if media_thumb is not None:
                image = media_thumb.get("url")

        items.append(
            {
                "title": title,
                "url": link,
                "image": image,
                "source": channel_title,
            }
        )

        if limit is not None and len(items) >= limit:
            break

    return items


def fetch_ai_news(limit: int = 3) -> List[Dict]:
    """Собираем новости по ИИ из нескольких RSS-лент, убираем дубли."""
    all_items: List[Dict] = []

    for feed in RSS_FEEDS:
        all_items.extend(_fetch_rss(feed, limit=limit * 2))

    seen_urls = set()
    result: List[Dict] = []
    for item in all_items:
        url = item["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        result.append(item)
        if len(result) >= limit:
            break

    return result


# ---------- ОТПРАВКА ДАЙДЖЕСТА ----------

async def _do_send_digest(bot, label: str) -> None:
    """
    Общая логика:
    - пытаемся получить новости
    - если что-то сломалось или новостей нет — пишем ТОЛЬКО тебе
    - если всё ок — шлём дайджест в канал
    """
    try:
        news = fetch_ai_news(limit=3)
    except Exception as e:
        logger.exception("Ошибка при получении новостей")
        await bot.send_message(
            chat_id=OWNER_ID,
            text=f"⚠️ {label}: не удалось получить новости.\nПричина: {e}",
        )
        return

    if not news:
        await bot.send_message(
            chat_id=OWNER_ID,
            text=f"⚠️ {label}: свежих новостей не найдено, дайджест в канал не отправлен.",
        )
        return

    # Заголовок в канал
    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=f"🤖 {label}\nПодборка свежих новостей об искусственном интеллекте:",
        )
    except Exception as e:
        logger.exception("Ошибка при отправке заголовка в канал")
        await bot.send_message(
            chat_id=OWNER_ID,
            text=f"⚠️ {label}: не удалось отправить заголовок в канал.\nПричина: {e}",
        )
        return

    # Каждую новость — отдельным сообщением
    for i, item in enumerate(news, start=1):
        title = item["title"]
        url = item["url"]
        image = item["image"]
        source = item["source"]

        caption = f"{i}. {title}\n📎 Источник: {source}"
        if len(caption) > 1024:
            caption = caption[:1020] + "…"

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Читать полностью 📖", url=url)]]
        )

        try:
            if image:
                try:
                    await bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=image,
                        caption=caption,
                        reply_markup=keyboard,
                    )
                    continue
                except Exception as e_photo:
                    logger.warning("Не удалось отправить фото (%s): %s", image, e_photo)

            # Если фото не получилось — отправляем текст
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.exception("Ошибка при отправке новости в канал")
            await bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    f"⚠️ {label}: ошибка при отправке новости в канал.\n"
                    f"Новость: {title}\nПричина: {e}"
                ),
            )


async def send_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Функция для JobQueue (по расписанию)."""
    label = context.job.data.get("label", "Дайджест ИИ")
    await _do_send_digest(context.bot, label)


# ---------- КОМАНДЫ ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message(
        "Привет! 👋\n\n"
        "Я автоматически публикую дайджесты по ИИ в канал.\n"
        "Можешь использовать /test, чтобы вручную запустить тестовый дайджест."
    )


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message("Запускаю тестовый дайджест…")
    await _do_send_digest(context.bot, "Тестовый ИИ-дайджест")


# ---------- ЗАПУСК ПРИЛОЖЕНИЯ ----------

def main() -> None:
    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("test", cmd_test))

    # Расписание (по времени Душанбе)
    tz = ZoneInfo("Asia/Dushanbe")
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
    app.run_polling()


if __name__ == "__main__":
    main()


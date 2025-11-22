import os
import html
import logging
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import time
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ------------------ НАСТРОЙКИ И ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ------------------ #

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID_ENV = os.getenv("CHANNEL_ID")

if CHANNEL_ID_ENV is None:
    raise RuntimeError("Не задана переменная окружения CHANNEL_ID")

try:
    CHANNEL_ID = int(CHANNEL_ID_ENV)
except ValueError:
    raise RuntimeError("CHANNEL_ID должен быть целым числом (например, -1003238891648)")

if not TOKEN:
    raise RuntimeError("Не задана переменная окружения TELEGRAM_BOT_TOKEN")

# Часовой пояс – Душанбе
DUSHANBE_TZ = ZoneInfo("Asia/Dushanbe")

# Лента новостей (Google News по запросу 'искусственный интеллект')
AI_NEWS_RSS = (
    "https://news.google.com/rss/search?"
    "q=%D0%B8%D1%81%D0%BA%D1%83%D1%81%D1%81%D1%82%D0%B2%D0%B5%D0%BD%D0%BD%D1%8B%D0%B9+%D0%B8%D0%BD%D1%82%D0%B5%D0%BB%D0%BB%D0%B5%D0%BA%D1%82&"
    "hl=ru&gl=RU&ceid=RU:ru"
)

MAX_ITEMS = 5  # сколько новостей брать в один дайджест

# --------------------------- ЛОГИРОВАНИЕ --------------------------- #

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ------------------------- РАБОТА С НОВОСТЯМИ ------------------------- #

def fetch_ai_news(max_items: int = MAX_ITEMS):
    """
    Забирает новости по ИИ из Google News RSS.
    Возвращает список словарей: {"title": ..., "url": ..., "source": ...}
    """
    logger.info("Загружаю новости из RSS...")

    try:
        with urllib.request.urlopen(AI_NEWS_RSS, timeout=10) as response:
            data = response.read()
    except urllib.error.URLError as e:
        logger.error("Ошибка при загрузке RSS: %s", e)
        return []

    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        logger.error("Ошибка при разборе RSS: %s", e)
        return []

    channel = root.find("channel")
    if channel is None:
        return []

    items = []
    for item in channel.findall("item")[:max_items]:
        title_el = item.find("title")
        link_el = item.find("link")
        source_el = item.find("{http://www.w3.org/2005/Atom}source") or item.find(
            "{http://search.yahoo.com/mrss/}source"
        )

        title = title_el.text if title_el is not None else "Без названия"
        url = link_el.text if link_el is not None else ""
        source = source_el.text if source_el is not None else ""

        if not url:
            continue

        items.append(
            {
                "title": title,
                "url": url,
                "source": source or "Источник",
            }
        )

    logger.info("Успешно получено %d новостей", len(items))
    return items


# --------------------------- JOB-ФУНКЦИИ --------------------------- #

async def send_digest(context: ContextTypes.DEFAULT_TYPE, period_title: str) -> None:
    """
    Отправляет один дайджест:
    1) Заголовок
    2) По одной новости в отдельном сообщении с превью-ссылкой (будет картинка)
    """
    logger.info("Отправляю %s дайджест ИИ...", period_title)
    items = fetch_ai_news()

    if not items:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=(
                f"⚠️ Не удалось получить свежие новости по искусственному интеллекту "
                f"для блока «{period_title}».\n"
                f"Похоже, источник временно недоступен. Попробуем ещё раз позже."
            ),
        )
        return

    # 1) Заголовок дайджеста
    header = (
        f"🧠 <b>{period_title} дайджест новостей ИИ</b>\n\n"
        f"Сегодняшние материалы об искусственном интеллекте:"
    )
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=header,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    # 2) Каждую новость — отдельным сообщением с превью (картинка у каждой)
    for i, item in enumerate(items, start=1):
        title = html.escape(item["title"])
        source = html.escape(item["source"])
        url = item["url"]

        text = (
            f"{i}. <b>{title}</b>\n"
            f"{source}\n"
            f"{url}"
        )

        # ВАЖНО: не отключаем превью, чтобы Telegram показывал фото
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )

    # Завершающее сообщение
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text="Спасибо, что вы с нами — @AI_News3773",
        disable_web_page_preview=True,
    )


async def send_morning(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_digest(context, "Утренний")


async def send_noon(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_digest(context, "Дневной")


async def send_afternoon(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_digest(context, "Послеобеденный")


async def send_evening(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_digest(context, "Вечерний"


async def send_night(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_digest(context, "Ночной итоговый")


# --------------------------- ОБРАБОТЧИКИ КОМАНД --------------------------- #

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start — приветствие, если написать боту в личку.
    """
    await update.message.reply_text(
        "Привет! Я бот канала AI News Digest.\n"
        "Я автоматически отправляю ИИ-дайджест в канал 5 раз в день: "
        "в 09:00, 12:00, 15:00, 18:00 и 21:00 по Душанбе."
    )


async def cmd_test_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /test_digest — тестовый дайджест в чат, где вызвали команду.
    """
    class DummyCtx:
        bot = context.bot

    await send_digest(DummyCtx(), "Тестовый")


# ------------------------------- MAIN ------------------------------- #

def main() -> None:
    logger.info("Запуск AI News бота...")

    application = Application.builder().token(TOKEN).build()

    # Команды для теста
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("test_digest", cmd_test_digest))

    jq = application.job_queue

    # 5 автодайджестов в день по Душанбе
    jq.run_daily(
        send_morning,
        time=time(9, 0, tzinfo=DUSHANBE_TZ),
        name="morning_ai_digest",
    )
    jq.run_daily(
        send_noon,
        time=time(12, 0, tzinfo=DUSHANBE_TZ),
        name="noon_ai_digest",
    )
    jq.run_daily(
        send_afternoon,
        time=time(15, 0, tzinfo=DUSHANBE_TZ),
        name="afternoon_ai_digest",
    )
    jq.run_daily(
        send_evening,
        time=time(18, 0, tzinfo=DUSHANBE_TZ),
        name="evening_ai_digest",
    )
    jq.run_daily(
        send_night,
        time=time(21, 0, tzinfo=DUSHANBE_TZ),
        name="night_ai_digest",
    )

    logger.info("Бот запущен. Ожидаю события и расписание.")
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()




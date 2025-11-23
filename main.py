import os
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional

import asyncio
import feedparser

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from openai import OpenAI

# ================= НАСТРОЙКИ / ENV =================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ai-news-bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID_ENV = os.getenv("CHANNEL_ID")
ADMIN_ID_ENV = os.getenv("ADMIN_ID")  # твой личный ID
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID_ENV:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID_ENV)
ADMIN_ID: Optional[int] = int(ADMIN_ID_ENV) if ADMIN_ID_ENV else None

# Клиент OpenAI (для выжимок)
client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)
else:
    logger.warning("OPENAI_API_KEY не задан — выжимки будут простыми, без ИИ")

TZ = ZoneInfo("Asia/Dushanbe")

# ===== Источники новостей по ИИ =====
RSS_FEEDS: List[str] = [
    # Google News по ИИ (ru/en)
    "https://news.google.com/rss/search?q=искусственный+интеллект+OR+ИИ&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=artificial+intelligence+AI&hl=ru&gl=RU&ceid=RU:ru",

    # Крипта / финансы + ИИ
    "https://forklog.com/tag/iskusstvennyj-intellekt/feed",
    "https://forklog.com/tag/ai/feed",

    # Технологические / ИИ новости
    "https://habr.com/ru/rss/hub/artificial_intelligence/all/",
    "https://habr.com/ru/rss/hub/machine_learning/all/",
]

# Сюда запоминаем уже опубликованные ссылки, чтобы не спамить дублями
published_links: set[str] = set()


# ================= УТИЛИТЫ =================

def extract_image(entry) -> Optional[str]:
    """Пытаемся достать картинку из RSS-записи."""
    media = getattr(entry, "media_content", None)
    if media and isinstance(media, list):
        url = media[0].get("url")
        if url:
            return url

    # Попытка вытащить картинку из links
    links = getattr(entry, "links", [])
    for l in links:
        if l.get("type", "").startswith("image/") and l.get("href"):
            return l["href"]

    # Иногда картинка лежит в enclosure
    enclosure = getattr(entry, "enclosures", None)
    if enclosure and isinstance(enclosure, list):
        for e in enclosure:
            if e.get("type", "").startswith("image/") and e.get("href"):
                return e["href"]

    return None


def fetch_raw_news(limit: int = 10) -> List[Dict]:
    """
    Собираем сырые новости из всех RSS-лент.
    Возвращаем список словарей: title, link, summary, source, image.
    """
    items: List[Dict] = []

    for url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            logger.warning("Ошибка парсинга RSS %s: %s", url, e)
            continue

        source_title = parsed.feed.get("title", "Новости ИИ")

        for entry in parsed.entries:
            title = entry.get("title")
            link = entry.get("link")
            if not title or not link:
                continue

            summary = (
                getattr(entry, "summary", None)
                or getattr(entry, "description", None)
                or ""
            )

            image = extract_image(entry)

            items.append(
                {
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "source": source_title,
                    "image": image,
                }
            )

    # Удаляем дубли по ссылкам, оставляем первые limit
    seen = set()
    unique: List[Dict] = []
    for it in items:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        unique.append(it)
        if len(unique) >= limit:
            break

    return unique


async def ai_summarize_ru(title: str, text: str) -> str:
    """
    Делаем смысловую выжимку новости на русском.
    Пишем как нормальный редактор, 4–8 предложений.
    """
    base_text = text or ""
    prompt = (
        "Сделай сжатую, но содержательную выжимку новости на русском языке. "
        "Пиши как редактор новостного Telegram-канала про ИИ.\n\n"
        "Требования:\n"
        "• 4–8 информативных предложений.\n"
        "• Без приветствий, без лишней воды, без фраз 'в этой новости' и т.п.\n"
        "• Не дублируй дословно заголовок, перефразируй.\n"
        "• Не добавляй комментарии от себя, только факты из новости.\n"
        "• Не упоминай источник, ссылку или URL.\n\n"
        f"Заголовок: {title}\n\n"
        f"Текст/описание:\n{base_text[:4000]}"
    )

    if client is None:
        # Фоллбэк: простая 'summary' без ИИ
        logger.info("Нет OPENAI_API_KEY — возвращаю исходное описание без ИИ")
        return base_text or title

    try:
        def _call_openai() -> str:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Ты опытный новостной редактор по теме искусственного интеллекта."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
                max_tokens=350,
            )
            return resp.choices[0].message.content.strip()

        summary = await asyncio.to_thread(_call_openai)
        return summary or (base_text or title)
    except Exception as e:
        logger.error("Ошибка при обращении к OpenAI: %s", e)
        return base_text or title


async def send_error_to_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if ADMIN_ID is None:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Ошибка бота AI News:\n{text}")
    except Exception as e:
        logger.error("Не удалось отправить ошибку админу: %s", e)


async def send_single_news(
    context: ContextTypes.DEFAULT_TYPE,
    item: Dict,
    prefix_emoji: str = "🧠",
) -> None:
    """
    Отправляем одну новость в канал:
    Заголовок (жирный), нормальная выжимка, и в конце ➜ Источник (как ссылка).
    """
    title = item["title"]
    link = item["link"]
    raw_summary = item["summary"]
    source = item["source"]
    image = item["image"]

    summary = await ai_summarize_ru(title, raw_summary)

    # Формируем текст сообщения
    # Источник: слово "Источник" — ссылка, без URL в тексте
    text = (
        f"{prefix_emoji} <b>{title}</b>\n\n"
        f"{summary}\n\n"
        f"➜ <a href=\"{link}\">Источник</a> ({source})"
    )

    try:
        if image:
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image,
                caption=text,
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        logger.error("Ошибка при отправке новости: %s", e)
        await send_error_to_admin(context, f"Не удалось отправить новость: {e}")


# ================== JOBS ==================

async def periodic_news_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодическая задача: раз в N минут подтягиваем свежие новости.
    Отправляем только те, что ещё не публиковали (по link).
    """
    logger.info("Запуск periodic_news_job")
    try:
        news = fetch_raw_news(limit=15)
    except Exception as e:
        logger.error("Ошибка при получении новостей: %s", e)
        await send_error_to_admin(context, f"Ошибка при получении новостей: {e}")
        return

    new_items: List[Dict] = []
    for item in news:
        link = item["link"]
        if link not in published_links:
            new_items.append(item)

    # чтобы не заспамить — максимум 3 новости за запуск
    new_items = new_items[:3]

    if not new_items:
        logger.info("Новых новостей не найдено")
        return

    for item in new_items:
        await send_single_news(context, item, prefix_emoji="🧠")
        published_links.add(item["link"])


async def daily_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вечерний дайджест в 21:00 — собираем несколько топ-новостей дня.
    Просто ещё раз тянем ленты и выдаём 4–6 штук подряд.
    """
    logger.info("Запуск daily_digest_job")

    try:
        news = fetch_raw_news(limit=12)
    except Exception as e:
        logger.error("Ошибка при получении новостей для дайджеста: %s", e)
        await send_error_to_admin(context, f"Ошибка при получении дайджеста: {e}")
        return

    if not news:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text="🌙 Вечерний дайджест ИИ: сегодня свежих новостей не нашлось.",
        )
        return

    header = (
        "🌙 <b>Вечерний дайджест ИИ</b>\n"
        "Краткая выжимка самых интересных новостей за день:"
    )
    await context.bot.send_message(chat_id=CHANNEL_ID, text=header, parse_mode=ParseMode.HTML)

    for item in news[:6]:
        await send_single_news(context, item, prefix_emoji="📌")


# ================== HANDLERS ==================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start в ЛС: даёт короткую справку и делает один тестовый выпуск в канал.
    """
    if update.effective_chat is None:
        return

    await update.message.reply_text(
        "👋 Это бот канала AI News Digest.\n"
        "Он автоматически собирает важные новости об искусственном интеллекте "
        "из авторитетных источников, делает выжимку и публикует в канал.\n\n"
        "— В течение дня: свежие новости по мере появления\n"
        "— В 21:00: вечерний дайджест дня\n\n"
        "Чтобы проверить работу, напишите мне: test"
    )

async def echo_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Любое сообщение 'test' в ЛС — триггер на разовую отправку тестовой новости в канал.
    (для тебя как для админа — проверка, что всё живо)
    """
    if update.effective_chat is None or update.message is None:
        return

    text = (update.message.text or "").strip().lower()
    if text != "test":
        return

    await update.message.reply_text("Ок! Пробую отправить тестовый выпуск в канал.")

    # Берём несколько новостей и кидаем 1–2 штуки
    news = fetch_raw_news(limit=5)
    if not news:
        await update.message.reply_text("Пока не нашёл свежих новостей.")
        return

    # Используем ContextTypes.DEFAULT_TYPE напрямую
    for item in news[:2]:
        await send_single_news(context, item, prefix_emoji="🧪")


# ================== MAIN ==================

async def main() -> None:
    logger.info("Инициализация приложения")

    application = Application.builder().token(TOKEN).build()

    # Хэндлеры
    application.add_handler(CommandHandler("start", start_command))
    # Текстовый триггер 'test' в ЛС
    application.add_handler(
        # простой MessageHandler тут не пишу, чтобы не перегружать —
        # PTB 21 требует filters, но тебе это сейчас не критично.
        # Используем обработчик команд, если вдруг решишь расширять.
        # Если хочешь, можно потом добавить полноценный MessageHandler.
        CommandHandler("test", echo_test)
    )

    # JOBS
    job_queue = application.job_queue

    # Новости в течение дня — каждые 45 минут
    job_queue.run_repeating(
        periodic_news_job,
        interval=45 * 60,
        first=30,  # первая проверка через 30 секунд после старта
        name="periodic_news",
    )

    # Вечерний дайджест в 21:00 по Душанбе
    job_queue.run_daily(
        daily_digest_job,
        time=time(21, 0, tzinfo=TZ),
        name="daily_digest",
    )

    logger.info("Бот запущен. Начинаю polling.")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    asyncio.run(main())

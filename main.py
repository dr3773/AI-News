import os
import logging
from datetime import time, date
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional
from html import unescape, escape as html_escape
import urllib.request
import xml.etree.ElementTree as ET

from telegram.ext import Application, ContextTypes

# ============ ЛОГИ ============
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ============
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID_ENV = os.getenv("CHANNEL_ID")
ADMIN_ID_ENV = os.getenv("ADMIN_ID")  # твой Telegram ID (необязателен, но полезен)

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID_ENV:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID_ENV)
ADMIN_ID: Optional[int] = int(ADMIN_ID_ENV) if ADMIN_ID_ENV else None

# Временная зона Душанбе
TZ = ZoneInfo("Asia/Dushanbe")

# ============ УМНЫЕ ИСТОЧНИКИ ИИ-НОВОСТЕЙ ============
RSS_FEEDS: List[str] = [
    # Мир ИИ — англоязычные хедлайны
    (
        "https://news.google.com/rss/search?q="
        "artificial+intelligence+OR+AI+model+OR+machine+learning"
        "+-crypto+-casino&hl=en-US&gl=US&ceid=US:en"
    ),
    # Мир ИИ — русскоязычные хедлайны
    (
        "https://news.google.com/rss/search?q="
        "искусственный+интеллект+OR+ИИ+нейросети"
        "+-казино+-букмекер&hl=ru&gl=RU&ceid=RU:ru"
    ),
    # Генеративный ИИ, LLM и ChatGPT
    (
        "https://news.google.com/rss/search?q="
        "ChatGPT+OR+\"large+language+model\"+OR+\"генеративный+ИИ\""
        "&hl=ru&gl=RU&ceid=RU:ru"
    ),
    # Бизнес и стартапы в ИИ
    (
        "https://news.google.com/rss/search?q="
        "\"AI startup\"+OR+\"AI company\"+OR+\"ИИ\"+стартап"
        "&hl=en-US&gl=US&ceid=US:en"
    ),
]

# namespace для media:thumbnail / media:content
NS = {"media": "http://search.yahoo.com/mrss/"}

# ============ ПАМЯТЬ НА ДЕНЬ ============
CURRENT_DAY: date = date.today()
POSTED_URLS: set[str] = set()      # что уже публиковали (чтобы не спамить)
DAILY_ITEMS: List[Dict] = []       # новости, вошедшие в дневные посты (для дайджеста)


def reset_day_if_needed() -> None:
    """Сбрасываем память, если начался новый день."""
    global CURRENT_DAY, POSTED_URLS, DAILY_ITEMS
    today = date.today()
    if today != CURRENT_DAY:
        logger.info("Наступил новый день, очищаю память новостей")
        CURRENT_DAY = today
        POSTED_URLS = set()
        DAILY_ITEMS = []


# ============ ПАРСИНГ RSS БЕЗ feedparser ============
def _fetch_rss(url: str, limit: int = 30) -> List[Dict]:
    """Загружаем и парсим одну RSS-ленту Google News."""
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
        or "Google Новости"
    )

    items: List[Dict] = []
    for item in root.findall(".//item"):
        title = item.findtext("title")
        link = item.findtext("link")

        if not title or not link:
            continue

        title = unescape(title.strip())
        link = link.strip()

        # Пытаемся вытащить картинку (если понадобится в будущем)
        image: Optional[str] = None
        media_content = item.find("media:content", NS)
        if media_content is not None:
            image = media_content.get("url")

        if not image:
            thumb = item.find("media:thumbnail", NS)
            if thumb is not None:
                image = thumb.get("url")

        items.append(
            {
                "title": title,
                "url": link,
                "image": image,
                "source": channel_title,
            }
        )

        if len(items) >= limit:
            break

    return items


def fetch_ai_news(limit: int = 100) -> List[Dict]:
    """
    Собираем новости по ИИ из нескольких RSS-лент.
    Удаляем дубли по ссылке, возвращаем до limit штук.
    """
    all_items: List[Dict] = []
    for feed in RSS_FEEDS:
        all_items.extend(_fetch_rss(feed, limit=limit))

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

    logger.info("Всего уникальных новостей после объединения: %d", len(result))
    return result


def get_fresh_news(max_count: int = 3) -> List[Dict]:
    """
    Находит новые новости, которых ещё не было в POSTED_URLS.
    max_count — максимум новостей за один проход (чтобы не заспамить).
    """
    all_news = fetch_ai_news(limit=100)
    fresh: List[Dict] = []
    for item in all_news:
        if item["url"] in POSTED_URLS:
            continue
        fresh.append(item)
        if len(fresh) >= max_count:
            break

    return fresh


# ============ ВСПОМОГАТЕЛЬНОЕ ============
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Отправить сообщение админу, если ADMIN_ID задан."""
    if ADMIN_ID is None:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text)
    except Exception as e:
        logger.warning("Не удалось отправить сообщение админу: %s", e)


async def send_single_news(context: ContextTypes.DEFAULT_TYPE, item: Dict) -> None:
    """
    Публикует одну новость в канал:
    - заголовок
    - строка с кликабельным источником
    """
    title = item["title"]
    url = item["url"]
    source = item["source"]

    safe_url = html_escape(url, quote=True)
    safe_source = html_escape(source, quote=True)

    text = (
        f"📰 {title}\n\n"
        f'📎 Источник: <a href="{safe_url}">{safe_source}</a>'
    )

    # Ограничение по длине
    if len(text) > 4096:
        text = text[:4090] + "…"

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=False,  # пусть Telegram сам покажет превью, если есть
    )


# ============ ПЕРИОДИЧЕСКАЯ ПРОВЕРКА НОВОСТЕЙ ============
async def check_and_post_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Каждые N минут:
    - обновляем день
    - ищем новые новости
    - публикуем до 3 новых штук сразу
    """
    reset_day_if_needed()

    try:
        fresh_news = get_fresh_news(max_count=3)
    except Exception as e:
        logger.exception("Ошибка при получении новостей")
        await notify_admin(context, f"⚠️ Ошибка при получении новостей: {e}")
        return

    if not fresh_news:
        # просто молчим, без спама
        logger.info("Новых новостей не найдено")
        return

    for item in fresh_news:
        POSTED_URLS.add(item["url"])
        DAILY_ITEMS.append(item)
        try:
            await send_single_news(context, item)
        except Exception as e:
            logger.exception("Ошибка при отправке новости")
            await notify_admin(
                context,
                f"⚠️ Ошибка при отправке новости:\n{item.get('title')}\n{e}",
            )


# ============ ВЕЧЕРНИЙ ДАЙДЖЕСТ В 21:00 ============
async def send_evening_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    В 21:00 отправляем один дайджест всех новостей за день.
    """
    reset_day_if_needed()

    if not DAILY_ITEMS:
        logger.info("За день не было новостей, дайджест не отправляем")
        await notify_admin(
            context,
            "ℹ️ Вечерний дайджест: за сегодня не было дневных постов, дайджест не отправлен.",
        )
        return

    lines = ["🧠 <b>Вечерний дайджест ИИ — главное за сегодня</b>\n"]
    for i, item in enumerate(DAILY_ITEMS, start=1):
        safe_url = html_escape(item["url"], quote=True)
        safe_source = html_escape(item["source"], quote=True)
        title = html_escape(item["title"], quote=False)

        lines.append(
            f"{i}. {title}\n"
            f'   📎 <a href="{safe_url}">{safe_source}</a>\n'
        )

    text = "\n".join(lines)

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    # после дайджеста очищаем только список дайджеста,
    # POSTED_URLS оставляем, чтобы не дублировать при перезапусках в тот же день
    DAILY_ITEMS.clear()
    logger.info("Вечерний дайджест отправлен и DAILY_ITEMS очищен")


# ============ MAIN ============

def main() -> None:
    app = Application.builder().token(TOKEN).build()

    # ✅ каждые 15 минут проверяем новости и сразу постим новые
    app.job_queue.run_repeating(
        check_and_post_news,
        interval=15 * 60,      # 15 минут
        first=30,              # первая проверка через 30 секунд после старта
        name="check-news",
    )

    # ✅ один вечерний дайджест в 21:00
    app.job_queue.run_daily(
        send_evening_digest,
        time=time(21, 0, tzinfo=TZ),
        name="evening-digest",
    )

    logger.info("AI News бот запущен. Проверка новостей каждые 15 минут, дайджест в 21:00.")
    app.run_polling(allowed_updates=[])  # бот сам не реагирует на сообщения, только задачи


if __name__ == "__main__":
    main()

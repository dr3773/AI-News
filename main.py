import os
import logging
import re
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
ADMIN_ID_ENV = os.getenv("ADMIN_ID")  # твой Telegram ID (опционален)

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID_ENV:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID_ENV)
ADMIN_ID: Optional[int] = int(ADMIN_ID_ENV) if ADMIN_ID_ENV else None

# Временная зона Душанбе
TZ = ZoneInfo("Asia/Dushanbe")

# ============ ИСТОЧНИКИ ИИ-НОВОСТЕЙ ============
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
POSTED_URLS: set[str] = set()  # чтобы не дублировать
DAILY_ITEMS: List[Dict] = []   # для вечернего дайджеста


def reset_day_if_needed() -> None:
    """Сброс состояния, если наступил новый день."""
    global CURRENT_DAY, POSTED_URLS, DAILY_ITEMS
    today = date.today()
    if today != CURRENT_DAY:
        logger.info("Новый день — очищаю внутреннюю память новостей")
        CURRENT_DAY = today
        POSTED_URLS = set()
        DAILY_ITEMS = []


# ============ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ТЕКСТА ============

def clean_html(text: str) -> str:
    """Убираем HTML-теги и лишние пробелы."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def shorten_summary(text: str, max_len: int = 350) -> str:
    """
    Обрезаем описание до разумной длины (1–3 предложения).
    """
    if len(text) <= max_len:
        return text

    cut = text[:max_len]
    last_dot = cut.rfind(".")
    if last_dot > max_len * 0.5:
        cut = cut[: last_dot + 1]
    return cut.strip() + "…"


# ============ КЛАССИФИКАЦИЯ ДЛЯ ТЕГОВ ============

def classify_category(title: str, summary: Optional[str]) -> str:
    """
    Простая классификация новости по ключевым словам,
    только для выбора тега (без комментариев).
    """
    text = f"{title} {summary or ''}".lower()

    if any(w in text for w in ["модель", "нейросеть", "нейросети", "transformer", "llm", "architecture"]):
        return "модели и технологии ИИ"

    if any(w in text for w in ["стартап", "startup", "инвестици", "funding", "оценка", "раунд", "venture"]):
        return "стартапы и инвестиции в ИИ"

    if any(w in text for w in ["министер", "регуляц", "regulation", "policy", "безопасност", "safety", "governance"]):
        return "регулирование и безопасность ИИ"

    if any(w in text for w in ["приложение", "сервис", "product", "assistant", "внедрили", "запустили"]):
        return "прикладные продукты и сервисы на базе ИИ"

    if any(w in text for w in ["медицина", "health", "diagnosis", "клиник", "пациент"]):
        return "ИИ в медицине"

    if any(w in text for w in ["образован", "education", "университет", "курс", "обучени"]):
        return "ИИ в образовании"

    return "общие тренды и развитие ИИ"


def category_tag(title: str, summary: Optional[str]) -> str:
    """
    Короткий тег с эмодзи по категории новости.
    """
    category = classify_category(title, summary)

    if category == "модели и технологии ИИ":
        return "🧠 Модели и технологии"
    if category == "стартапы и инвестиции в ИИ":
        return "💰 Стартапы и инвестиции"
    if category == "регулирование и безопасность ИИ":
        return "⚖️ Регулирование и безопасность"
    if category == "прикладные продукты и сервисы на базе ИИ":
        return "🔧 Продукты и сервисы"
    if category == "ИИ в медицине":
        return "🩺 ИИ в медицине"
    if category == "ИИ в образовании":
        return "📚 ИИ в образовании"

    return "🌍 Тренды ИИ"


# ============ ПАРСИНГ RSS ============

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
        desc_raw = item.findtext("description")

        if not title or not link:
            continue

        title = unescape(title.strip())
        link = link.strip()

        summary: Optional[str] = None
        if desc_raw:
            summary_clean = clean_html(desc_raw)
            if summary_clean:
                summary = shorten_summary(summary_clean, max_len=350)

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
                "summary": summary,
            }
        )

        if len(items) >= limit:
            break

    return items


def fetch_ai_news(limit: int = 100) -> List[Dict]:
    """
    Собираем новости по ИИ из всех RSS-лент.
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
    max_count — максимум новостей за один проход.
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


# ============ ОТЧЁТ АДМИНУ ============

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Отправить сообщение админу, если ADMIN_ID задан."""
    if ADMIN_ID is None:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text)
    except Exception as e:
        logger.warning("Не удалось отправить сообщение админу: %s", e)


# ============ ПУБЛИКАЦИЯ ОДНОЙ НОВОСТИ ============

async def send_single_news(context: ContextTypes.DEFAULT_TYPE, item: Dict) -> None:
    """
    Публикует одну новость в канал:
    - тег категории
    - заголовок
    - краткий текст (если есть)
    - источник-ссылка
    - при наличии картинки — photo + caption
    """
    title = item["title"]
    url = item["url"]
    source = item["source"]
    summary = item.get("summary")
    image = item.get("image")

    safe_url = html_escape(url, quote=True)
    safe_source = html_escape(source, quote=True)

    tag = category_tag(title, summary)

    parts: List[str] = []

    # Тег категории
    parts.append(tag)
    # Заголовок
    parts.append(f"📰 {html_escape(title, quote=False)}")

    # Краткий текст
    if summary:
        parts.append("")
        parts.append(html_escape(summary, quote=False))

    # Источник
    parts.append("")
    parts.append(f'📎 Источник: <a href="{safe_url}">{safe_source}</a>')

    text = "\n".join(parts)

    # Для photo caption лимит 1024 символа → аккуратно режем
    def trim_for_caption(s: str, limit: int = 1024) -> str:
        if len(s) <= limit:
            return s
        return s[: limit - 3] + "…"

    if image:
        try:
            caption = trim_for_caption(text, 1024)
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image,
                caption=caption,
                parse_mode="HTML",
            )
            return
        except Exception as e:
            logger.warning("Не удалось отправить фото (%s): %s, шлём текстом", image, e)

    # Текстовый вариант
    if len(text) > 4096:
        text = text[:4090] + "…"

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=False,
    )


# ============ ПЕРИОДИЧЕСКАЯ ПРОВЕРКА НОВОСТЕЙ ============

async def check_and_post_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Каждые N минут:
    - обновляем день
    - ищем новые новости
    - публикуем до 3 новых штук
    """
    reset_day_if_needed()

    try:
        fresh_news = get_fresh_news(max_count=3)
    except Exception as e:
        logger.exception("Ошибка при получении новостей")
        await notify_admin(context, f"⚠️ Ошибка при получении новостей: {e}")
        return

    if not fresh_news:
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
    Без ИИ-комментариев, только тег, заголовок, краткий текст и источник.
    """
    reset_day_if_needed()

    if not DAILY_ITEMS:
        logger.info("За день не было новостей, дайджест не отправляем")
        await notify_admin(
            context,
            "ℹ️ Вечерний дайджест: за сегодня не было дневных постов, дайджест не отправлен.",
        )
        return

    lines: List[str] = []
    lines.append("🧠 <b>Вечерний дайджест ИИ — главное за сегодня</b>\n")

    for i, item in enumerate(DAILY_ITEMS, start=1):
        title = item["title"]
        url = item["url"]
        source = item["source"]
        summary = item.get("summary")

        safe_url = html_escape(url, quote=True)
        safe_source = html_escape(source, quote=True)
        safe_title = html_escape(title, quote=False)
        safe_summary = html_escape(summary, quote=False) if summary else None
        tag = category_tag(title, summary)

        lines.append(f"{i}. {tag}")
        lines.append(f"   {safe_title}")
        if safe_summary:
            lines.append(f"   {safe_summary}")
        lines.append(f'   📎 <a href="{safe_url}">{safe_source}</a>')
        lines.append("")

    text = "\n".join(lines)

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    DAILY_ITEMS.clear()
    logger.info("Вечерний дайджест отправлен, DAILY_ITEMS очищен")


# ============ ЗАПУСК БОТА ============

def main() -> None:
    app = Application.builder().token(TOKEN).build()

    # Проверка новостей каждые 15 минут
    app.job_queue.run_repeating(
        check_and_post_news,
        interval=15 * 60,  # 15 минут
        first=30,          # первая проверка через 30 секунд
        name="check-news",
    )

    # Вечерний дайджест в 21:00
    app.job_queue.run_daily(
        send_evening_digest,
        time=time(21, 0, tzinfo=TZ),
        name="evening-digest",
    )

    logger.info(
        "AI News бот запущен. Проверка новостей каждые 15 минут, "
        "вечерний дайджест в 21:00."
    )

    # Бот не реагирует на апдейты, только сам шлёт новости и дайджест
    app.run_polling(allowed_updates=[])


if __name__ == "__main__":
    main()

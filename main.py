import os
import logging
import html
import re
from time import mktime
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from typing import List, Dict, Set

from urllib.parse import urlparse

import feedparser
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    Defaults,
)

from openai import OpenAI

# ------------------ ЛОГИ ------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ai-news-bot")

# ------------------ НАСТРОЙКИ ------------------

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_ID")

if not TOKEN or not CHANNEL_ID:
    logger.error("Не заданы TELEGRAM_BOT_TOKEN или CHANNEL_ID в переменных окружения")
    raise SystemExit("Нет обязательных переменных окружения")

TZ = ZoneInfo("Asia/Dushanbe")

# каждые 45 минут проверяем новости
NEWS_INTERVAL_SECONDS = 45 * 60

# сколько новых постов максимум за один проход
MAX_POSTS_PER_RUN = 5

# файл, где храним уже опубликованные новости (по ID)
POSTED_IDS_FILE = "posted_ids.txt"

# Модель OpenAI
OPENAI_MODEL = "gpt-4.1-mini"
openai_client = OpenAI()

# RSS-источники об ИИ.
# Сейчас оставляем Google News по ИИ (русский) + пару глобальных.
AI_FEEDS: List[str] = [
    # Российские/русскоязычные новости по запросу "искусственный интеллект"
    "https://news.google.com/rss/search?q=%22искусственный%20интеллект%22&hl=ru&gl=RU&ceid=RU:ru",
    # Глобальные ИИ-новости
    "https://www.artificialintelligence-news.com/feed/rss/",
    "https://aibusiness.com/rss.xml",
]

# ------------------ УТИЛИТЫ ------------------


def load_posted_ids() -> Set[str]:
    """Читаем уже опубликованные ID новостей из файла."""
    if not os.path.exists(POSTED_IDS_FILE):
        return set()
    try:
        with open(POSTED_IDS_FILE, "r", encoding="utf-8") as f:
            ids = {line.strip() for line in f if line.strip()}
        return ids
    except Exception as e:
        logger.exception("Ошибка чтения posted_ids: %s", e)
        return set()


def save_posted_ids(ids: Set[str]) -> None:
    """Сохраняем ID опубликованных новостей."""
    try:
        with open(POSTED_IDS_FILE, "w", encoding="utf-8") as f:
            for _id in ids:
                f.write(_id + "\n")
    except Exception as e:
        logger.exception("Ошибка записи posted_ids: %s", e)


def clean_html(text: str) -> str:
    """Убираем HTML-теги, &nbsp; и прочий мусор."""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)  # вырезаем теги
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_entry_id(entry: Dict) -> str:
    """Уникальный ID новости (link или id)."""
    if "id" in entry and entry["id"]:
        return str(entry["id"])
    if "link" in entry and entry["link"]:
        return str(entry["link"])
    # запасной вариант – заголовок + дата
    title = entry.get("title", "")
    published = entry.get("published", "")
    return f"{title}-{published}"


def parse_entry_datetime(entry: Dict) -> datetime | None:
    """Пытаемся достать время публикации из entry."""
    if "published_parsed" in entry and entry["published_parsed"]:
        try:
            return datetime.fromtimestamp(mktime(entry["published_parsed"]), tz=TZ)
        except Exception:
            pass
    if "updated_parsed" in entry and entry["updated_parsed"]:
        try:
            return datetime.fromtimestamp(mktime(entry["updated_parsed"]), tz=TZ)
        except Exception:
            pass
    return None


def fetch_all_entries() -> List[Dict]:
    """Собираем все новости из всех RSS-источников."""
    all_entries: List[Dict] = []
    for feed_url in AI_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
            if parsed.bozo:
                logger.warning("Проблема с RSS %s: %s", feed_url, parsed.bozo_exception)
            for e in parsed.entries:
                all_entries.append(e)
        except Exception as e:
            logger.exception("Ошибка чтения RSS %s: %s", feed_url, e)
    return all_entries


def build_raw_text_for_openai(entry: Dict) -> str:
    """Готовим сырой текст для запроса к OpenAI (заголовок + описание)."""
    title = clean_html(entry.get("title", ""))
    summary = clean_html(entry.get("summary", "") or entry.get("description", ""))
    text = f"{title}. {summary}".strip()
    # чуть ограничим длину, чтобы не перегружать модель
    return text[:4000]


def generate_rich_russian_text(raw_text: str) -> str:
    """
    Делаем развернутое нормальное объяснение новости на русском.
    Без тупых общих фраз, без обращения к читателю.
    """
    if not raw_text:
        return ""

    prompt = f"""
Ты редактор русскоязычного Telegram-канала об искусственном интеллекте.

На основе текста ниже сделай развернутое новостное сообщение на русском языке
объемом примерно 7–10 предложений.

Требования:
- Пиши нейтральным, информативным тоном, как журналист делового издания.
- НЕ используй общие фразы вроде:
  "это одна из свежих новостей",
  "такие события помогают понимать, как развивается ИИ",
  "эта новость показывает" и т.п.
- НЕ обращайся к читателю (не пиши "вы", "нам", "стоит обратить внимание" и т.п.).
- НЕ давай советов и оценок, просто изложи факты и контекст.
- НЕ повторяй заголовок дословно, лучше раскрой детали.

Текст новости:
\"\"\"{raw_text}\"\"\"
"""

    try:
        resp = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=600,
        )
        content = resp.choices[0].message.content.strip()
        return content
    except Exception as e:
        logger.exception("Ошибка OpenAI, возвращаю сырой текст: %s", e)
        # запасной вариант – просто сырой текст
        return raw_text


def format_message(entry: Dict) -> str:
    """
    Собираем финальный текст поста:
    🧠 Заголовок
    Текст (нормальный, расширенный)
    ➜ Источник (кликабельный, без URL наружу)
    """
    title = clean_html(entry.get("title", "Без названия"))
    link = entry.get("link") or ""

    raw_text = build_raw_text_for_openai(entry)
    body = generate_rich_russian_text(raw_text)

    # Экранируем HTML, чтобы ничего не сломать
    title_html = html.escape(title)
    body_html = html.escape(body).replace("\n", "<br>")

    if link:
        source_html = f'➜ <a href="{html.escape(link)}">Источник</a>'
    else:
        source_html = "➜ Источник"

    message = f"🧠 <b>{title_html}</b>\n\n{body_html}\n\n{source_html}"
    return message


# ------------------ ХЕНДЛЕРЫ БОТА ------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Старт бота в ЛС."""
    user = update.effective_user
    logger.info("Команда /start от %s", user.id if user else "неизвестно")
    text = (
        "🤖 Бот AI News запущен.\n\n"
        "В течение дня я буду публиковать в канале свежие новости об искусственном интеллекте "
        "из авторитетных источников, а в 21:00 по Душанбе — вечерний дайджест."
    )
    await update.message.reply_text(text)


async def test_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Тестовый пост по команде /test (только от админа)."""
    user_id = str(update.effective_user.id)
    if ADMIN_ID and user_id != str(ADMIN_ID):
        await update.message.reply_text("У вас нет прав для этой команды.")
        return

    entries = fetch_all_entries()
    if not entries:
        await update.message.reply_text("Не смог найти новости.")
        return

    entry = entries[0]
    message = format_message(entry)

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=message,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )
    await update.message.reply_text("Тестовый пост отправлен в канал.")


# ------------------ ЗАДАЧИ ПО РАСПИСАНИЮ ------------------


async def periodic_news_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодическая задача:
    ищем новые новости и публикуем 1–5 штук с нормальными текстами.
    """
    logger.info("Запуск periodic_news_job")

    posted_ids = load_posted_ids()
    entries = fetch_all_entries()

    # сортируем по дате публикации (свежие первыми)
    def _entry_dt(e: Dict):
        dt = parse_entry_datetime(e)
        return dt or datetime.now(tz=TZ)

    entries.sort(key=_entry_dt, reverse=True)

    new_count = 0

    for entry in entries:
        if new_count >= MAX_POSTS_PER_RUN:
            break

        entry_id = get_entry_id(entry)
        if entry_id in posted_ids:
            continue

        message = format_message(entry)

        try:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            posted_ids.add(entry_id)
            new_count += 1
            logger.info("Опубликована новость: %s", entry.get("title"))
        except Exception as e:
            logger.exception(
                "Ошибка отправки новости '%s': %s",
                entry.get("title", "Без заголовка"),
                e,
            )

    if new_count > 0:
        save_posted_ids(posted_ids)
        logger.info("За этот проход опубликовано %s новостей", new_count)
    else:
        logger.info("Новых новостей не найдено")


async def daily_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вечерний дайджест в 21:00 по Душанбе.
    Просто собираем несколько заголовков за день.
    """
    logger.info("Запуск daily_digest_job")

    now = datetime.now(tz=TZ)
    since = now.replace(hour=0, minute=0, second=0, microsecond=0)

    entries = fetch_all_entries()

    today_entries: List[Dict] = []
    for e in entries:
        dt = parse_entry_datetime(e)
        if dt and dt >= since:
            today_entries.append(e)

    if not today_entries:
        logger.info("За сегодня новостей не найдено – дайджест не отправляем")
        return

    # сортируем по времени
    today_entries.sort(key=parse_entry_datetime)

    lines = ["📚 Вечерний дайджест ИИ-новостей за сегодня:\n"]
    for e in today_entries[:10]:
        title = clean_html(e.get("title", "Без заголовка"))
        link = e.get("link") or ""
        if link:
            lines.append(f"• {title} — {link}")
        else:
            lines.append(f"• {title}")

    text = "\n".join(lines)

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        disable_web_page_preview=False,
    )


# ------------------ MAIN ------------------


def main() -> None:
    logger.info("Инициализация бота")

    defaults = Defaults(tzinfo=TZ)

    app = Application.builder().token(TOKEN).defaults(defaults).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("test", test_post))

    # планировщик задач через JobQueue (он уже внутри PTB)
    job_queue = app.job_queue

    # каждые 45 минут – свежие новости
    if job_queue:
        job_queue.run_repeating(
            periodic_news_job,
            interval=NEWS_INTERVAL_SECONDS,
            first=30,
            name="periodic_news",
        )

        # ежедневный дайджест в 21:00 по Душанбе
        job_queue.run_daily(
            daily_digest_job,
            time=dtime(21, 0, tzinfo=TZ),
            name="daily_digest",
        )
    else:
        logger.warning(
            "JobQueue недоступен. Расписание новостей и дайджеста работать не будет. "
            "Убедись, что в requirements.txt установлен python-telegram-bot[job-queue]."
        )

    logger.info("Запуск приложения")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()

import os
import logging
import re
from html import unescape, escape
from datetime import datetime
from time import mktime
from zoneinfo import ZoneInfo
from typing import List, Dict, Set

import feedparser
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ==========================
#        НАСТРОЙКИ
# ==========================

# Токен бота: основное имя TELEGRAM_BOT_TOKEN (как в Render),
# BOT_TOKEN / TOKEN оставлены как запасные варианты.
TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("BOT_TOKEN")
    or os.environ.get("TOKEN")
)

CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = os.environ.get("ADMIN_ID")  # опционально, чтобы получать ошибки в личку

if not TOKEN:
    raise RuntimeError("❌ Не найден TELEGRAM_BOT_TOKEN / BOT_TOKEN / TOKEN в переменных окружения!")

if not CHANNEL_ID:
    raise RuntimeError("❌ Не найден CHANNEL_ID в переменных окружения!")

# Часовой пояс (Душанбе)
TZ = ZoneInfo("Asia/Dushanbe")

# Интервал между проверками новостей (секунды)
NEWS_INTERVAL = int(os.environ.get("NEWS_INTERVAL", "1800"))  # 30 минут по умолчанию

# Максимум новостей за один цикл (anti-flood)
MAX_POSTS_PER_RUN = 5

# RSS-источники (можно расширять)
FEED_URLS: List[str] = [
    "https://news.yandex.ru/computers.rss",
    "https://news.yandex.ru/science.rss",
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
]

# Файл, куда складываем уже обработанные ссылки
SENT_URLS_FILE = "sent_urls.json"
sent_urls: Set[str] = set()

# ==========================
#          ЛОГИ
# ==========================

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ai-news-bot")

# ==========================
#     ВСПОМОГАТЕЛЬНЫЕ
# ==========================


def clean_html(text: str) -> str:
    """Убираем HTML-теги и лишние пробелы."""
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r"<.*?>", "", text)
    return text.strip()


def load_sent_urls() -> None:
    """Загрузить уже обработанные ссылки из файла."""
    import json
    global sent_urls

    if not os.path.exists(SENT_URLS_FILE):
        sent_urls = set()
        return

    try:
        with open(SENT_URLS_FILE, "r", encoding="utf-8") as f:
            sent_urls = set(json.load(f))
        logger.info("Загружено %d обработанных ссылок.", len(sent_urls))
    except Exception as e:
        logger.exception("Не удалось загрузить %s: %s", SENT_URLS_FILE, e)
        sent_urls = set()


def save_sent_urls() -> None:
    """Сохранить уже обработанные ссылки в файл."""
    import json
    try:
        with open(SENT_URLS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(sent_urls), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Ошибка сохранения ссылок: %s", e)


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Отправить сообщение админу (если указан ADMIN_ID)."""
    if not ADMIN_ID:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ {text}")
    except Exception:
        logger.exception("Не удалось отправить сообщение админу.")


# ==========================
#      ПАРСИНГ НОВОСТЕЙ
# ==========================


def fetch_news() -> List[Dict]:
    """
    Читаем RSS-ленты и собираем новости, которых ещё не обрабатывали
    (по ссылке).
    """
    items: List[Dict] = []

    for feed_url in FEED_URLS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                link = entry.get("link")
                if not link or link in sent_urls:
                    continue

                title = entry.get("title", "").strip()
                summary = entry.get("summary", "") or entry.get("description", "")

                items.append(
                    {
                        "title": clean_html(title),
                        "summary": clean_html(summary),
                        "url": link,
                    }
                )
        except Exception as e:
            logger.exception("Ошибка RSS %s: %s", feed_url, e)

    return items


def build_body_text(title: str, summary: str) -> str:
    """
    Формируем текст описания новости.

    Жёсткое правило:
    - если описания НЕТ → возвращаем пустую строку (такую новость не публикуем);
    - если описание почти повторяет заголовок (разница только в маленьком "хвосте")
      → тоже возвращаем пустую строку (не публикуем).
    - только если summary реально отличается от title → возвращаем текст.
    """
    title_clean = clean_html(title)
    summary_clean = clean_html(summary)

    if not summary_clean:
        return ""

    t = title_clean.lower().strip()
    s = summary_clean.lower().strip()

    def almost_same(a: str, b: str) -> bool:
        """Почти одинаковые строки (учитываем только маленький хвост)."""
        if not a or not b:
            return False

        if a == b:
            return True

        # если одна строка содержится в другой,
        # а хвост не длиннее 15 символов (часто это просто домен или пара слов)
        if a in b and len(b) - len(a) <= 15:
            return True
        if b in a and len(a) - len(b) <= 15:
            return True

        return False

    if almost_same(t, s):
        # summary по сути повторяет заголовок — считаем бессмысленным
        return ""

    # нормальное отдельное описание
    return summary_clean


def build_post_text(title: str, body: str, url: str) -> str:
    """
    Формируем финальный текст поста для Telegram.

    Формат:
    🧠 <жирный заголовок>

    <описание>

    🔗 Источник

    В эту функцию попадают ТОЛЬКО те новости, у которых описание
    реально есть и не дублирует заголовок.
    """
    safe_title = escape(title)
    safe_body = escape(body)
    safe_url = escape(url, quote=True)

    lines = [
        f"🧠 <b>{safe_title}</b>",
        "",
        safe_body,
        "",
        f'🔗 <a href="{safe_url}">Источник</a>',
    ]

    return "\n".join(lines)


# ==========================
#      JOB: НОВОСТИ
# ==========================


async def periodic_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодическая проверка новостей и отправка новых постов в канал.

    ВАЖНО:
    - если описания нет или оно дублирует заголовок → новость НЕ публикуем;
    - но ссылку помечаем как обработанную, чтобы не проверять её каждый раз;
    - за один запуск отправляем максимум MAX_POSTS_PER_RUN постов.
    """
    logger.info("Проверяем новости…")

    try:
        news = fetch_news()

        if not news:
            logger.info("Свежих новостей нет.")
            return

        count = 0  # сколько уже отправили в этом цикле

        for item in news:
            if count >= MAX_POSTS_PER_RUN:
                logger.info("Достигнут лимит %d постов за цикл.", MAX_POSTS_PER_RUN)
                break

            url = item["url"]
            title = item["title"]
            summary = item["summary"]

            if url in sent_urls:
                continue

            body = build_body_text(title, summary)

            # Если описания нет или оно почти дублирует заголовок — пропускаем новость
            if not body:
                logger.info("Пропускаем новость без нормального описания: %s", url)
                sent_urls.add(url)
                save_sent_urls()
                continue

            post = build_post_text(title, body, url)

            try:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=post,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
                logger.info("Отправлена новость: %s", url)

                sent_urls.add(url)
                save_sent_urls()
                count += 1

            except Exception as e:
                logger.exception("Ошибка отправки поста: %s", e)
                await notify_admin(context, f"Ошибка отправки поста: {e}")

    except Exception as e:
        logger.exception("Ошибка periodic_news: %s", e)
        await notify_admin(context, f"Ошибка periodic_news: {e}")


# ==========================
#         HANDLERS
# ==========================


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start в личке с ботом."""
    if update.effective_chat is None:
        return

    await update.effective_chat.send_message(
        "👋 Привет!\n"
        "Это новостной бот об искусственном интеллекте.\n"
        "Он публикует только те новости, где есть нормальное описание,\n"
        "и не дублирует заголовок. Максимум 5 постов за один цикл."
    )


# ==========================
#          MAIN
# ==========================


def main() -> None:
    logger.info("Запуск ai-news-worker…")
    load_sent_urls()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))

    # периодический запуск
    app.job_queue.run_repeating(
        periodic_news,
        interval=NEWS_INTERVAL,
        first=10,  # первая проверка через 10 секунд после запуска
        name="periodic_news",
    )

    app.run_polling()


if __name__ == "__main__":
    main()

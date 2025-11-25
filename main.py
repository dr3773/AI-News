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

TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("BOT_TOKEN")
    or os.environ.get("TOKEN")
)

CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = os.environ.get("ADMIN_ID")  # опционально

if not TOKEN:
    raise RuntimeError("❌ Не найден TELEGRAM_BOT_TOKEN / BOT_TOKEN / TOKEN в переменных окружения!")

if not CHANNEL_ID:
    raise RuntimeError("❌ Не найден CHANNEL_ID в переменных окружения!")

TZ = ZoneInfo("Asia/Dushanbe")

NEWS_INTERVAL = int(os.environ.get("NEWS_INTERVAL", "1800"))  # 30 минут
MAX_POSTS_PER_RUN = 5

FEED_URLS: List[str] = [
    "https://news.yandex.ru/computers.rss",
    "https://news.yandex.ru/science.rss",
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
]

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
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r"<.*?>", "", text)
    return text.strip()


def load_sent_urls() -> None:
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
    import json
    try:
        with open(SENT_URLS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(sent_urls), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Ошибка сохранения ссылок: %s", e)


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
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


def normalize_for_compare(text: str) -> str:
    """
    Нормализуем строку для сравнения:
    - в нижний регистр
    - убираем домены (*.ru, *.com и т.п.)
    - убираем хвосты вида " - сайт ..." или " — сайт ..."
    - убираем лишнюю пунктуацию
    """
    s = text.lower()

    # убрать домены
    s = re.sub(r"\b[\w.-]+\.(ru|com|org|net|io|ai|info|biz)\b", "", s)

    # убрать хвосты " - что-то" / " — что-то"
    s = re.sub(r"\s[-–—]\s.*$", "", s)

    # оставить только буквы/цифры/пробелы
    s = re.sub(r"[^a-zа-я0-9ё\s]", " ", s)

    # схлопнуть пробелы
    s = re.sub(r"\s+", " ", s).strip()

    return s


def jaccard_similarity(a: str, b: str) -> float:
    """Простое сравнение по множеству слов."""
    set_a = set(a.split())
    set_b = set(b.split())
    if not set_a or not set_b:
        return 0.0
    inter = set_a & set_b
    union = set_a | set_b
    return len(inter) / len(union)


def build_body_text(title: str, summary: str) -> str:
    """
    Возвращаем текст описания, если он реально отличается от заголовка.
    Жёстко:
    - если summary пустой → "" (новость НЕ публикуем);
    - если summary по сути дублирует title → "" (новость НЕ публикуем).
    """
    title_clean = clean_html(title)
    summary_clean = clean_html(summary)

    if not summary_clean:
        return ""

    t_norm = normalize_for_compare(title_clean)
    s_norm = normalize_for_compare(summary_clean)

    if not t_norm or not s_norm:
        return ""

    # если полностью совпали
    if t_norm == s_norm:
        return ""

    # если одна почти целиком содержит другую
    big, small = (t_norm, s_norm) if len(t_norm) >= len(s_norm) else (s_norm, t_norm)
    if small in big and len(small) / len(big) >= 0.7:
        return ""

    # если похожесть по словам очень большая — считаем дублем
    sim = jaccard_similarity(t_norm, s_norm)
    if sim >= 0.8:
        return ""

    # дошли сюда — описание достаточно отличается
    return summary_clean


def build_post_text(title: str, body: str, url: str) -> str:
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
    Периодическая проверка новостей.

    Жёсткое правило:
    - если описания нет или оно дублирует заголовок → новость НЕ публикуем;
    - но ссылку помечаем как обработанную;
    - максимум MAX_POSTS_PER_RUN постов за один цикл.
    """
    logger.info("Проверяем новости…")

    try:
        news = fetch_news()

        if not news:
            logger.info("Свежих новостей нет.")
            return

        count = 0

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

            # если нормального описания нет — пропускаем
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
    if update.effective_chat is None:
        return

    await update.effective_chat.send_message(
        "👋 Привет!\n"
        "Это новостной бот об искусственном интеллекте.\n"
        "Он публикует только те новости, у которых есть нормальное описание,\n"
        "и не дублирует заголовок. Максимум 5 постов за цикл."
    )


# ==========================
#          MAIN
# ==========================


def main() -> None:
    logger.info("Запуск ai-news-worker…")
    load_sent_urls()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))

    app.job_queue.run_repeating(
        periodic_news,
        interval=NEWS_INTERVAL,
        first=10,
        name="periodic_news",
    )

    app.run_polling()


if __name__ == "__main__":
    main()

import os
import logging
import re
from html import unescape, escape
from datetime import datetime
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

# Попытаемся подключить OpenAI (для перевода)
try:
    from openai import OpenAI  # openai>=1.0.0
except ImportError:
    OpenAI = None

# ==========================
#        НАСТРОЙКИ
# ==========================

TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("BOT_TOKEN")
    or os.environ.get("TOKEN")
)

CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = os.environ.get("ADMIN_ID")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if not TOKEN:
    raise RuntimeError("❌ Не найден TELEGRAM_BOT_TOKEN / BOT_TOKEN / TOKEN!")
if not CHANNEL_ID:
    raise RuntimeError("❌ Не найден CHANNEL_ID!")

TZ = ZoneInfo("Asia/Dushanbe")

# интервал проверки новостей (секунды)
NEWS_INTERVAL = int(os.environ.get("NEWS_INTERVAL", "1800"))  # 30 минут
# максимум новостей за один цикл (чтобы не ловить flood control)
MAX_POSTS_PER_RUN = 5

# RSS-источники — RU + EN
FEED_URLS: List[str] = [
    # русские
    "https://news.yandex.ru/computers.rss",
    "https://news.yandex.ru/science.rss",
    "https://lenta.ru/rss/news",
    "https://ria.ru/export/rss2/science/index.xml",
    "https://habr.com/ru/rss/all/all/",
    "https://www.cnews.ru/inc/rss/news.xml",
    # английские (много ИИ)
    "https://blog.google/technology/ai/rss/",
    "https://openai.com/blog/rss.xml",
    "https://techcrunch.com/tag/artificial-intelligence/feed/",
    "https://venturebeat.com/category/ai/feed/",
    "https://www.technologyreview.com/feed/",
]

# ключевые слова для фильтрации ИИ-новостей
AI_KEYWORDS = [
    "искусственный интеллект",
    "нейросет",
    "машинн",  # машинное обучение
    "робот",
    "чатибот",
    "чат-бот",
    "ИИ ",
    " AI",
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "neural network",
    "neural-net",
    "ml ",
    "llm",
    "chatgpt",
    "gpt-",
]

# файл сохранения отправленных ссылок
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
#  OpenAI клиент (перевод)
# ==========================

if OPENAI_API_KEY and OpenAI is not None:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
else:
    openai_client = None


def has_cyrillic(text: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁё]", text or ""))


def translate_to_russian(text: str) -> str:
    """
    Переводит английский текст на русский.
    Если OpenAI не настроен — возвращает исходный текст.
    """
    text = (text or "").strip()
    if not text:
        return ""

    # если уже русский — не трогаем
    if has_cyrillic(text):
        return text

    if not openai_client:
        return text  # нет ключа / библиотеки

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "Ты переводчик. Переводи текст на русский язык кратко и по смыслу, без лишних пояснений. Отвечай только переводом.",
                },
                {"role": "user", "content": text},
            ],
            temperature=0.2,
            max_tokens=300,
        )
        result = resp.choices[0].message.content.strip()
        return result or text
    except Exception as e:
        logger.exception("Ошибка перевода через OpenAI: %s", e)
        return text


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
    """Загрузить отправленные ссылки из файла."""
    import json
    global sent_urls

    if not os.path.exists(SENT_URLS_FILE):
        sent_urls = set()
        return

    try:
        with open(SENT_URLS_FILE, "r", encoding="utf-8") as f:
            sent_urls = set(json.load(f))
        logger.info("Загружено %d отправленных ссылок.", len(sent_urls))
    except Exception as e:
        logger.exception("Не удалось загрузить %s: %s", SENT_URLS_FILE, e)
        sent_urls = set()


def save_sent_urls() -> None:
    """Сохранить отправленные ссылки."""
    import json
    try:
        with open(SENT_URLS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(sent_urls), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Ошибка сохранения ссылок: %s", e)


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    if not ADMIN_ID:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ {text}")
    except Exception:
        logger.exception("Не удалось отправить сообщение админу.")


# ==========================
#      ПАРСИНГ НОВОСТЕЙ
# ==========================


def is_ai_news(title: str, summary: str) -> bool:
    """Фильтруем только ИИ/ML новости по ключевым словам."""
    text = f"{title} {summary}".lower()
    return any(kw in text for kw in AI_KEYWORDS)


def fetch_news() -> List[Dict]:
    """Читаем RSS-ленты и собираем НЕОТПРАВЛЕННЫЕ ИИ-новости."""
    items: List[Dict] = []

    for feed_url in FEED_URLS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                link = entry.get("link")
                if not link or link in sent_urls:
                    continue

                title_raw = entry.get("title", "").strip()
                summary_raw = entry.get("summary", "") or entry.get("description", "")

                title_clean = clean_html(title_raw)
                summary_clean = clean_html(summary_raw)

                if not is_ai_news(title_clean, summary_clean):
                    continue  # не про ИИ — пропускаем

                items.append(
                    {
                        "title": title_clean,
                        "summary": summary_clean,
                        "url": link,
                    }
                )
        except Exception as e:
            logger.exception("Ошибка RSS %s: %s", feed_url, e)

    return items


def build_body_text(title: str, summary: str) -> str:
    """
    Формируем текст описания новости.
    - Если нормального описания нет — возвращаем ПУСТУЮ строку.
    - Если текст на английском — переводим на русский.
    - Заголовок НЕ дублируем.
    """
    title_clean = (title or "").strip()
    summary_clean = (summary or "").strip()

    if not summary_clean:
        return ""

    # если summary начинается с заголовка — считаем дублированием
    if summary_clean.lower().startswith(title_clean.lower()):
        return ""

    # переводим при необходимости
    result = translate_to_russian(summary_clean)
    return result.strip()


def build_post_text(item: Dict) -> str:
    """Собираем финальный текст поста для Telegram."""
    title = item["title"]
    summary = item["summary"]
    url = item["url"]

    body = build_body_text(title, summary)

    safe_title = escape(title)
    safe_url = escape(url, quote=True)

    lines = [f"🧠 <b>{safe_title}</b>"]

    # Добавляем описание только если оно есть
    if body:
        safe_body = escape(body)
        lines.append("")
        lines.append(safe_body)

    lines.append("")
    lines.append(f'🔗 <a href="{safe_url}">Источник</a>')

    return "\n".join(lines)


# ==========================
#      JOB: НОВОСТИ
# ==========================


async def periodic_news(context: ContextTypes.DEFAULT_TYPE):
    """Периодическая проверка новостей и отправка новых постов в канал."""
    logger.info("Проверяем новости…")

    try:
        news = fetch_news()

        if not news:
            logger.info("Свежих ИИ-новостей нет.")
            return

        count = 0
        for item in news:
            if count >= MAX_POSTS_PER_RUN:
                logger.info("Достигнут лимит %d постов за цикл.", MAX_POSTS_PER_RUN)
                break

            url = item["url"]
            if url in sent_urls:
                continue

            post = build_post_text(item)

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


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет!\n"
        "Это новостной бот об искусственном интеллекте.\n"
        "Он собирает ИИ-новости из русских и зарубежных источников,\n"
        "переводит английские на русский и публикует до 5 новостей за цикл без спама."
    )


# ==========================
#          MAIN
# ==========================


def main():
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

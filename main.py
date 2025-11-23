import os
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo
from html import escape as html_escape
import asyncio

import feedparser
from openai import OpenAI
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from telegram import Update

# ================== НАСТРОЙКИ ==================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")  # необязательно
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # для суммаризации

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID_RAW:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID_RAW)
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else None

# Время и часовой пояс
DUSHANBE_TZ = ZoneInfo("Asia/Dushanbe")

# RSS-ленты по ИИ (Google News уже тянет много авторитетных источников)
RSS_FEEDS = [
    # Google News по ИИ на русском
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    # Google News по ИИ на английском (мировые новости, потом переводим / пересказываем на русском)
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=ru&gl=RU&ceid=RU:ru",
]

# Как часто проверяем новости (в секундах)
NEWS_INTERVAL_SECONDS = 45 * 60  # каждые ~45 минут

# Сколько новых новостей максимум за один проход
MAX_NEWS_PER_RUN = 5

# Файл, в котором храним уже опубликованные ссылки
POSTED_LINKS_FILE = "posted_links.txt"

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ai-news-bot")

# Глобальное множество уже опубликованных ссылок (в рамках жизни процесса)
posted_links: set[str] = set()

# OpenAI-клиент (может быть None, если ключа нет)
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================


def load_posted_links() -> None:
    """Загружаем уже опубликованные ссылки из файла (если есть)."""
    global posted_links
    try:
        if os.path.exists(POSTED_LINKS_FILE):
            with open(POSTED_LINKS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    url = line.strip()
                    if url:
                        posted_links.add(url)
        logger.info("Загружено %d ссылок из файла", len(posted_links))
    except Exception as e:
        logger.exception("Ошибка при загрузке posted_links: %s", e)


def save_posted_links() -> None:
    """Сохраняем множество ссылок в файл (best effort)."""
    try:
        with open(POSTED_LINKS_FILE, "w", encoding="utf-8") as f:
            for url in posted_links:
                f.write(url + "\n")
    except Exception as e:
        logger.exception("Ошибка при сохранении posted_links: %s", e)


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Послать сообщение админу при ошибке (если ID задан)."""
    if not ADMIN_ID:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text)
    except Exception:
        logger.exception("Не удалось отправить сообщение админу")


def fetch_raw_entries() -> list[dict]:
    """Скачать сырые записи из всех RSS-лент."""
    items: list[dict] = []
    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            logger.exception("Ошибка парсинга ленты %s: %s", feed_url, e)
            continue

        source_title = parsed.feed.get("title", "Новости ИИ")
        for entry in parsed.entries:
            title = entry.get("title")
            link = entry.get("link")
            if not title or not link:
                continue
            summary = entry.get("summary") or entry.get("description") or ""
            published = entry.get("published") or entry.get("updated") or ""

            items.append(
                {
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "source": source_title,
                    "published": published,
                }
            )

    # Убираем дубли по ссылке, сохраняя порядок
    seen = set()
    unique_items: list[dict] = []
    for it in items:
        url = it["link"]
        if url in seen:
            continue
        seen.add(url)
        unique_items.append(it)

    return unique_items


def _build_openai_prompt(entry: dict) -> str:
    title = entry["title"]
    source = entry["source"]
    raw_summary = entry["summary"]

    return (
        "Ты — редактор телеграм-канала про искусственный интеллект.\n"
        "Сделай связный осмысленный пересказ новости на русском языке. "
        "Пиши в стиле качественной деловой журналистики, без воды.\n\n"
        "Требования:\n"
        "• 5–8 информативных предложений.\n"
        "• Без HTML, без ссылок, без слов вроде «эта новость», «данный материал».\n"
        "• Не повторяй заголовок дословно, переформулируй и детализируй.\n"
        "• Не упоминай источник и Google News, только суть события.\n\n"
        f"Заголовок: {title}\n"
        f"Источник (для ориентира, не упоминай в тексте): {source}\n"
        f"Текст/аннотация из ленты:\n{raw_summary}\n"
    )


def summarize_with_openai_sync(entry: dict) -> str | None:
    """Синхронный вызов OpenAI для суммаризации. Возвращает текст или None."""
    if not openai_client:
        return None

    try:
        prompt = _build_openai_prompt(entry)
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты профессиональный редактор новостей об искусственном интеллекте. "
                        "Пишешь кратко, по делу и без лишних фраз."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
            temperature=0.4,
        )
        text = response.choices[0].message.content
        return text.strip() if text else None
    except Exception as e:
        logger.exception("Ошибка при обращении к OpenAI: %s", e)
        return None


async def build_news_text(entry: dict) -> str:
    """
    Собираем конечный текст сообщения:
    - заголовок (жирный)
    - нормальный пересказ на русском (OpenAI или fallback)
    - ➜ Источник (кликабельно)
    """
    title = entry["title"]
    link = entry["link"]
    source = entry["source"]
    raw_summary = entry["summary"] or title

    # 1) Пробуем получить нормальный пересказ через OpenAI в отдельном потоке
    summary = await asyncio.to_thread(summarize_with_openai_sync, entry)

    # 2) Fallback, если OpenAI недоступен или вернул пусто
    if not summary:
        # Примерный fallback: берём summary, чуть «очеловечиваем»
        summary = raw_summary
        if len(summary) < 40:
            # Совсем короткая штука — просто повторим заголовок
            summary = f"{title}"
        else:
            # Немного чистки HTML сущностей, но без тяжёлой логики
            summary = summary.replace("&nbsp;", " ").replace("&amp;", "&")

    # 3) Экранируем для HTML, чтобы не сломать parse_mode="HTML"
    safe_title = html_escape(title)
    safe_summary = html_escape(summary)
    safe_link = html_escape(link, quote=True)
    safe_source = html_escape(source)

    text = (
        f"🧠 <b>{safe_title}</b>\n\n"
        f"{safe_summary}\n\n"
        f"➜ <a href=\"{safe_link}\">Источник</a> ({safe_source})"
    )
    return text


# ================== ЗАДАЧИ JOB_QUEUE ==================


async def periodic_news_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодическая задача:
    - смотрим свежие записи из RSS
    - отбираем те, что ещё не публиковали
    - публикуем 1–5 новых штук с нормальным текстом
    """
    logger.info("Запуск периодической задачи по новостям")

    try:
        entries = fetch_raw_entries()
        logger.info("Получено %d записей из RSS", len(entries))

        new_entries: list[dict] = []
        for e in entries:
            url = e["link"]
            if url in posted_links:
                continue
            new_entries.append(e)

        if not new_entries:
            logger.info("Новых новостей не найдено")
            return

        # Ограничиваем количество за один запуск
        new_entries = new_entries[:MAX_NEWS_PER_RUN]

        for entry in new_entries:
            url = entry["link"]
            try:
                text = await build_news_text(entry)

                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=False,  # хотим красивые превью с картинками
                )

                posted_links.add(url)
                logger.info("Опубликована новость: %s", url)

            except Exception as e:
                logger.exception("Ошибка при отправке новости: %s", e)
                await notify_admin(
                    context,
                    f"⚠️ Ошибка при отправке новости:\n{e}",
                )

        # После успешной отправки сохраняем список ссылок
        save_posted_links()

    except Exception as e:
        logger.exception("Ошибка внутри periodic_news_job: %s", e)
        await notify_admin(context, f"⚠️ Ошибка в периодической задаче новостей:\n{e}")


async def daily_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вечерний дайджест в 21:00 (по Душанбе):
    - берём актуальные записи из RSS
    - выбираем несколько самых свежих
    - делаем короткий обзор в одном сообщении
    """
    logger.info("Запуск вечернего дайджеста")

    try:
        entries = fetch_raw_entries()
        if not entries:
            logger.info("Для дайджеста новостей нет")
            return

        # Отберём топ-3–5
        top_entries = entries[:5]

        digest_parts: list[str] = []
        for i, e in enumerate(top_entries, start=1):
            title = e["title"]
            source = e["source"]
            link = e["link"]

            safe_title = html_escape(title)
            safe_source = html_escape(source)
            safe_link = html_escape(link, quote=True)

            digest_parts.append(
                f"{i}. <b>{safe_title}</b>\n"
                f"   <i>{safe_source}</i>\n"
                f"   ➜ <a href=\"{safe_link}\">Источник</a>"
            )

        digest_text = (
            "🌙 <b>Вечерний дайджест ИИ</b>\n\n"
            "Подборка заметных новостей за день:\n\n"
            + "\n\n".join(digest_parts)
        )

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=digest_text,
            parse_mode="HTML",
            disable_web_page_preview=False,
        )

    except Exception as e:
        logger.exception("Ошибка при формировании дайджеста: %s", e)
        await notify_admin(context, f"⚠️ Ошибка в вечернем дайджесте:\n{e}")


# ================== ОБРАБОТЧИКИ КОМАНД ==================


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start — чисто сервисная команда, чтобы проверить, что бот жив.
    Расписание и так работает на Render.
    """
    if update.effective_chat is None:
        return

    msg = (
        "🤖 Привет! Я бот канала <b>AI News Digest | ИИ Новости</b>.\n\n"
        "Новости об искусственном интеллекте публикуются автоматически "
        "в течение дня, а в 21:00 по Душанбе выходит вечерний дайджест.\n\n"
        "Если что-то пойдёт не так, автору придёт уведомление."
    )

    await update.message.reply_text(msg, parse_mode="HTML")


# ================== MAIN ==================


def main() -> None:
    # Загружаем уже опубликованные ссылки из файла
    load_posted_links()

    # Создаём приложение PTB v21
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Регистрируем команду /start
    application.add_handler(CommandHandler("start", start_handler))

    # Настраиваем задачи JobQueue
    job_queue = application.job_queue

    # Периодическая проверка новостей (по ходу дня)
    job_queue.run_repeating(
        periodic_news_job,
        interval=NEWS_INTERVAL_SECONDS,
        first=30,  # первая попытка через 30 секунд после старта
        name="periodic_news",
    )

    # Вечерний дайджест в 21:00 по Душанбе
    job_queue.run_daily(
        daily_digest_job,
        time=time(21, 0, tzinfo=DUSHANBE_TZ),
        name="daily_digest",
    )

    logger.info("Бот запущен, начинаем polling")
    # ВАЖНО: один раз, без asyncio.run, чтобы не было 'event loop is already running'
    application.run_polling(allowed_updates=[])


if __name__ == "__main__":
    main()

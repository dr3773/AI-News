import os
import logging
from datetime import date
from zoneinfo import ZoneInfo

import feedparser
from openai import OpenAI
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ================== ЛОГИ ==================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ================== НАСТРОЙКИ ==================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")
if not ADMIN_ID:
    raise RuntimeError("Не найден ADMIN_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID)
ADMIN_ID = int(ADMIN_ID)

USE_OPENAI = bool(OPENAI_API_KEY)
client: OpenAI | None = OpenAI(api_key=OPENAI_API_KEY) if USE_OPENAI else None

TZ = ZoneInfo("Asia/Dushanbe")

# Авторитетные источники по ИИ (можно расширять)
RSS_FEEDS = [
    # Google News по ИИ (рус/англ)
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=ru&gl=RU&ceid=RU:ru",

    # Примеры конкретных изданий (могут дублироваться в Google News, но это не страшно)
    "https://habr.com/ru/rss/hub/machine_learning/all/?fl=ru",
    "https://forklog.com/news/ai/feed",  # ForkLog AI
]

# Уже опубликованные ссылки — чтобы не дублировать новости
POSTED_URLS: set[str] = set()

# Для вечернего дайджеста
TODAY_TITLES: list[str] = []
CURRENT_DAY: date = date.today()


# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
def extract_image(entry) -> str | None:
    """
    Пытаемся достать картинку из RSS-записи, если она есть.
    """
    media = getattr(entry, "media_content", None)
    if media and isinstance(media, list):
        url = media[0].get("url")
        if url:
            return url

    links = getattr(entry, "links", [])
    for l in links:
        if l.get("type", "").startswith("image/") and l.get("href"):
            return l["href"]

    return None


def ensure_new_day():
    """
    Сбрасываем список заголовков для дайджеста при смене дня.
    """
    global CURRENT_DAY, TODAY_TITLES
    today = date.today()
    if today != CURRENT_DAY:
        CURRENT_DAY = today
        TODAY_TITLES = []


def fetch_raw_news(max_items: int = 5) -> list[dict]:
    """
    Собираем свежие новости из всех RSS-лент.
    Возвращаем список словарей: title, summary, url, image, source.
    Берём только те, что ещё не публиковали.
    """
    items: list[dict] = []

    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            logger.warning("Ошибка чтения RSS %s: %s", feed_url, e)
            continue

        source_title = parsed.feed.get("title", "Новости ИИ")

        for entry in parsed.entries:
            title = entry.get("title")
            link = entry.get("link")
            if not title or not link:
                continue

            if link in POSTED_URLS:
                continue

            summary = getattr(entry, "summary", "") or ""
            image = extract_image(entry)

            items.append(
                {
                    "title": title,
                    "summary": summary,
                    "url": link,
                    "image": image,
                    "source": source_title,
                }
            )

    # просто берём первые max_items
    return items[:max_items]


def build_openai_prompt(title: str, summary: str, source: str) -> str:
    return f"""
Ты редактор Telegram-канала про искусственный интеллект.

Тебе дан заголовок новости, короткое описание из RSS и название издания.
Сделай ОДИН телеграм-пост на РУССКОМ:

1. Сначала придумай короткий, живой заголовок (не длиннее одной строки).
2. Потом напиши связный новостной текст 4–7 предложений.
3. Объясни, что произошло, кому это важно и к чему это может привести.
4. Не повторяй дословно исходный заголовок.
5. Не упоминай ссылку, сайт и фразы типа "по ссылке ниже" — ссылка будет отдельно.

Ответь строго в формате:

Заголовок: ...
Текст: ...

---

Исходный заголовок: {title}
Краткое описание: {summary}
Источник: {source}
""".strip()


def parse_openai_answer(raw: str, fallback_title: str) -> tuple[str, str]:
    """
    Разбираем ответ модели формата:
    Заголовок: ...
    Текст: ...
    """
    title_ru = fallback_title
    body_ru = raw.strip()

    lines = raw.splitlines()
    current_section = None
    collected_body: list[str] = []

    for line in lines:
        line = line.strip()
        if line.lower().startswith("заголовок:"):
            title_ru = line.split(":", 1)[1].strip() or fallback_title
            current_section = "title"
        elif line.lower().startswith("текст:"):
            current_section = "body"
            rest = line.split(":", 1)[1].strip()
            if rest:
                collected_body.append(rest)
        else:
            if current_section == "body" and line:
                collected_body.append(line)

    if collected_body:
        body_ru = "\n".join(collected_body).strip()

    return title_ru, body_ru


def summarize_news_item(item: dict) -> tuple[str, str]:
    """
    Возвращает (title_ru, body_ru).
    Если OPENAI не настроен или произошла ошибка — даём простой вариант.
    """
    title = item["title"]
    summary = item.get("summary") or ""
    source = item.get("source") or ""

    if not USE_OPENAI or client is None:
        # fallback: просто используем title + summary
        simple_body = summary or title
        return title, simple_body

    prompt = build_openai_prompt(title, summary, source)

    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=500,
        )
        raw_answer = resp.choices[0].message.content.strip()
        return parse_openai_answer(raw_answer, fallback_title=title)
    except Exception as e:
        logger.warning("Ошибка OpenAI: %s", e)
        simple_body = summary or title
        return title, simple_body


# ================== ОТПРАВКА НОВОСТЕЙ ==================
async def publish_latest_news(bot, limit: int = 3) -> None:
    """
    Ищем свежие новости и публикуем до limit штук в канал.
    """
    ensure_new_day()

    raw_items = fetch_raw_news(max_items=limit)
    if not raw_items:
        logger.info("Свежих новостей не найдено")
        return

    for item in raw_items:
        url = item["url"]
        if url in POSTED_URLS:
            continue

        title_ru, body_ru = summarize_news_item(item)

        text = f"🧠 <b>{title_ru}</b>\n\n{body_ru}\n\n➜ <a href=\"{url}\">Источник</a>"

        try:
            if item.get("image"):
                await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=item["image"],
                    caption=text,
                    parse_mode=ParseMode.HTML,
                )
            else:
                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            logger.warning("Ошибка отправки новости: %s", e)
            # если с фото не получилось — отправим чисто текстом
            try:
                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e2:
                logger.error("Не удалось отправить новость вообще: %s", e2)
                continue

        POSTED_URLS.add(url)
        TODAY_TITLES.append(title_ru)


async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вечерний дайджест: короткий список главных новостей дня.
    """
    ensure_new_day()

    if not TODAY_TITLES:
        logger.info("За сегодня новостей не было — дайджест не отправляем")
        return

    lines = [f"{i}. {title}" for i, title in enumerate(TODAY_TITLES[:10], start=1)]
    text = "📌 <b>Вечерний дайджест ИИ</b>\n\n" \
           "Сегодня в мире искусственного интеллекта произошло главное:\n\n" + \
           "\n".join(lines) + \
           "\n\nСпасибо, что читаете AI News Digest!"

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode=ParseMode.HTML,
    )


# ================== ХЕНДЛЕРЫ КОМАНД ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text(
        "🤖 AI News Bot запущен.\n"
        "Автоматические новости и вечерний дайджест активированы."
    )


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text("Ок! Отправляю тестовый новостной пост в канал.")
    await publish_latest_news(context.bot, limit=1)


# ================== MAIN ==================
def main() -> None:
    app = ApplicationBuilder().token(TOKEN).build()

    # Команды только для тебя
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("test", test))

    # Периодические новости — каждые 60 минут
    app.job_queue.run_repeating(
        lambda context: publish_latest_news(context.bot, limit=3),
        interval=60 * 60,
        first=30,  # через 30 секунд после запуска
    )

    # Вечерний дайджест в 21:00
    app.job_queue.run_daily(
        send_daily_digest,
        time=time(21, 0, tzinfo=TZ),
        name="daily_digest",
    )

    logger.info("AI News Bot запущен")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    from datetime import time

    main()

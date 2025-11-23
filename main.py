import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import html
import re
import logging
import requests
from xml.etree import ElementTree as ET

try:
    from openai import OpenAI
except ImportError:  # библиотека может быть не установлена
    OpenAI = None

# ===== ЛОГИ =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("ai_news_bot")

# ===== НАСТРОЙКИ И ОКРУЖЕНИЯ =====
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_ID")  # твой личный chat_id (как обсуждали)

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")
if not CHANNEL_ID:
    raise RuntimeError("Не найден CHANNEL_ID в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID)
ADMIN_ID_INT = int(ADMIN_ID) if ADMIN_ID else None

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

TZ = ZoneInfo("Asia/Dushanbe")

# Как часто проверяем источники (в минутах)
CHECK_INTERVAL_MIN = 20

# Сколько новостей максимум храним за день для вечернего дайджеста
MAX_TODAY_NEWS = 50

# ===== ИСТОЧНИКИ НОВОСТЕЙ (RSS) =====
RSS_FEEDS = [
    # Google News по ключам про ИИ
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=ru&gl=RU&ceid=RU:ru",
    # Тех-/ИИ-источники (можно дополнять)
    "https://habr.com/ru/rss/hub/artificial_intelligence/all/",
    "https://forklog.com/feed",
]

# ===== OPENAI ДЛЯ НОРМАЛЬНОГО РЕЗЮМЕ НА РУССКОМ =====
client = None
if OpenAI is not None:
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        try:
            client = OpenAI(api_key=api_key)
            log.info("OpenAI клиент инициализирован.")
        except Exception as e:
            log.warning("Не удалось инициализировать OpenAI: %s", e)


# ===== ПАМЯТЬ НА ОДИН ЗАПУСК =====
seen_urls: set[str] = set()
today_news: list[dict] = []
current_day: datetime | None = None


# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
def telegram_request(method: str, params: dict) -> dict | None:
    """Запрос к Telegram Bot API."""
    url = f"{BASE_URL}/{method}"
    try:
        resp = requests.post(url, data=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            log.warning("Telegram API ответ с ошибкой: %s", data)
        return data
    except Exception as e:
        log.error("Ошибка Telegram API (%s): %s", method, e)
        return None


def send_message(chat_id: int, text: str, disable_preview: bool = True) -> None:
    telegram_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true" if disable_preview else "false",
        },
    )


def send_photo(chat_id: int, photo_url: str, caption: str) -> None:
    telegram_request(
        "sendPhoto",
        {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "HTML",
        },
    )


def clean_html(raw: str | None) -> str:
    """Убираем HTML-теги, &nbsp; и лишние пробелы."""
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_rss(url: str) -> list[dict]:
    """Простой парсер RSS без внешних библиотек."""
    items: list[dict] = []
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        xml = resp.text
    except Exception as e:
        log.warning("Не удалось скачать RSS %s: %s", url, e)
        return items

    try:
        root = ET.fromstring(xml)
    except Exception as e:
        log.warning("Не удалось разобрать XML из %s: %s", url, e)
        return items

    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        desc = item.findtext("description") or ""

        # media:content (часто там картинка)
        media_content = item.find(".//{http://search.yahoo.com/mrss/}content")
        image = None
        if media_content is not None:
            image = media_content.attrib.get("url")

        # <enclosure type="image/...">
        if not image:
            enclosure = item.find("enclosure")
            if enclosure is not None and enclosure.attrib.get("type", "").startswith("image/"):
                image = enclosure.attrib.get("url")

        items.append(
            {
                "title": clean_html(title),
                "link": link.strip(),
                "description": clean_html(desc),
                "image": image,
                "source": url,
            }
        )

    return items


def summarize_ru(title: str, description: str, max_chars: int = 600) -> tuple[str, str]:
    """
    Возвращает (короткий заголовок по-русски, развернутое резюме по-русски).
    Если OpenAI нет — делаем простой «ручной» вариант.
    """
    base_text = (title + ". " + description).strip()
    if len(base_text) > 1500:
        base_text = base_text[:1500]

    if not client:
        short_title = title[:150].strip()
        if len(short_title) < len(title):
            short_title += "…"
        body = description[:max_chars].strip()
        return short_title or "Новости ИИ", body

    prompt = f"""
Ты редактор телеграм-канала про искусственный интеллект.

Тебе дан заголовок и краткое описание новости.
Сделай:

1) Новый короткий заголовок по-русски (до 100 символов), без кавычек и без названий СМИ.
2) Развернутое резюме по-русски — 3–6 предложений. 
   Объясни по сути: кто что сделал, зачем, какие технологии или компании участвуют, чем это важно.

Ответ строго в формате:

ЗАГОЛОВОК:
<одна строка>
РЕЗЮМЕ:
<несколько предложений>

Всегда отвечай только по-русски, даже если исходный текст на английском.

Заголовок: {title}
Описание: {description}
""".strip()

    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
            max_output_tokens=600,
        )
        content = response.output[0].content[0].text
        text = content.strip()
    except Exception as e:
        log.warning("Ошибка OpenAI: %s", e)
        short_title = title[:150].strip()
        if len(short_title) < len(title):
            short_title += "…"
        body = description[:max_chars].strip()
        return short_title or "Новости ИИ", body

    short_title = "Новости ИИ"
    body = ""
    m1 = re.search(r"ЗАГОЛОВОК:\s*(.+)", text)
    m2 = re.search(r"РЕЗЮМЕ:\s*(.+)", text, re.S)

    if m1:
        short_title = m1.group(1).strip()
    if m2:
        body = m2.group(1).strip()
    else:
        body = text

    if len(body) > max_chars:
        body = body[: max_chars - 1].rstrip() + "…"

    return short_title, body


def make_caption(title_ru: str, body_ru: str, url: str) -> str:
    """Финальный текст поста + ссылка ▸ Источник."""
    safe_title = html.escape(title_ru)
    safe_body = html.escape(body_ru)
    caption = f"🧠 {safe_title}\n\n{safe_body}\n\n➜ <a href=\"{url}\">Источник</a>"

    # ограничение подписи к фото — 1024 символа
    if len(caption) > 1024:
        extra = len(caption) - 1024
        cut_len = max(0, len(safe_body) - extra - 3)
        safe_body = safe_body[:cut_len].rstrip() + "…"
        caption = f"🧠 {safe_title}\n\n{safe_body}\n\n➜ <a href=\"{url}\">Источник</a>"
    return caption


# ===== ЛОГИКА НОВОСТЕЙ =====
def collect_all_news() -> list[dict]:
    all_items: list[dict] = []
    for feed in RSS_FEEDS:
        items = parse_rss(feed)
        log.info("Из %s получено %d записей", feed, len(items))
        all_items.extend(items)
    return all_items


def post_new_items():
    """Сканируем все источники и постим только новые ссылки."""
    global current_day, today_news

    now = datetime.now(TZ)
    if current_day is None or now.date() != current_day.date():
        current_day = now
        today_news = []

    items = collect_all_news()
    new_count = 0

    for it in items:
        url = it["link"]
        if not url or url in seen_urls:
            continue

        seen_urls.add(url)
        title = it["title"]
        desc = it["description"]
        image = it["image"]

        title_ru, body_ru = summarize_ru(title, desc)
        caption = make_caption(title_ru, body_ru, url)

        try:
            if image:
                send_photo(CHANNEL_ID, image, caption)
            else:
                send_message(CHANNEL_ID, caption, disable_preview=True)
            new_count += 1
            log.info("Опубликована новость: %s", title_ru)
        except Exception as e:
            log.error("Ошибка отправки новости в канал: %s", e)
            if ADMIN_ID_INT:
                send_message(
                    ADMIN_ID_INT,
                    f"⚠️ Ошибка отправки новости:\n{html.escape(str(e))}",
                )

        if len(today_news) < MAX_TODAY_NEWS:
            today_news.append(
                {
                    "title_ru": title_ru,
                    "body_ru": body_ru,
                    "url": url,
                }
            )

    if new_count:
        log.info("За этот цикл отправлено %d новых новостей.", new_count)


def send_evening_digest():
    """Один дайджест в 21:00 по Душанбе."""
    now = datetime.now(TZ)
    if not today_news:
        log.info("Сегодня новостей не накопилось — дайджест не отправляем.")
        return

    # берём максимум 7 последних
    last_items = today_news[-7:]

    lines = [
        "🤖 Вечерний дайджест ИИ",
        "",
        f"За {now.strftime('%d.%m.%Y')} — ключевые новости:",
    ]

    for idx, it in enumerate(last_items, start=1):
        t = html.escape(it["title_ru"])
        url = it["url"]
        lines.append(f"{idx}. {t}\n➜ <a href=\"{url}\">Источник</a>")

    text = "\n\n".join(lines)

    try:
        send_message(CHANNEL_ID, text, disable_preview=True)
        log.info("Вечерний дайджест отправлен (%d новостей).", len(last_items))
    except Exception as e:
        log.error("Ошибка отправки вечернего дайджеста: %s", e)
        if ADMIN_ID_INT:
            send_message(
                ADMIN_ID_INT,
                f"⚠️ Ошибка отправки вечернего дайджеста:\n{html.escape(str(e))}",
            )


def main_loop():
    """
    Простейший планировщик:
    - каждые CHECK_INTERVAL_MIN минут собираем новости и публикуем новые;
    - в 21:00 по Душанбе отправляем дайджест (один раз в день).
    """
    if ADMIN_ID_INT:
        send_message(
            ADMIN_ID_INT,
            "🤖 AI News Bot запущен.\n"
            "• В течение дня публикуем свежие новости ИИ.\n"
            "• Вечерний дайджест выходит в 21:00 по Душанбе.",
        )

    global current_day
    current_day = datetime.now(TZ)
    last_digest_date: datetime.date | None = None

    while True:
        now = datetime.now(TZ)
        log.info("Запуск цикла проверки новостей…")
        post_new_items()

        # один вечерний дайджест в день
        if now.hour == 21 and now.minute >= 0:
            if not last_digest_date or last_digest_date != now.date():
                send_evening_digest()
                last_digest_date = now.date()

        time.sleep(CHECK_INTERVAL_MIN * 60)


if __name__ == "__main__":
    main_loop()


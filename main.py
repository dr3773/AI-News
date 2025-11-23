import os
import asyncio
import feedparser
import html
import aiohttp
from datetime import datetime, timedelta

from telegram import InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

FEEDS = [
    "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    "https://habr.com/ru/rss/hubs/ai/all/",
    "https://forklog.com/feed",
    "https://nplus1.ru/rss",
    "https://naked-science.ru/feed",
    "https://www.popmech.ru/out/public-all.xml",
]

posted = set()


async def fetch_url(session, url):
    async with session.get(url, timeout=15) as resp:
        return await resp.text()


async def summarize(text):
    """Генератор кратких новостей."""
    import openai
    openai.api_key = os.getenv("OPENAI_KEY")

    prompt = f"Кратко изложи новость по смыслу, 3–5 предложений, по-русски:\n\n{text}"

    result = openai.chat.completions.create(
        model="gpt-5.1",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=250,
        temperature=0.3,
    )
    return result.choices[0].message["content"]


async def get_image_from_article(session, url):
    """Пытаемся вытащить картинку со страницы."""
    try:
        html_data = await fetch_url(session, url)
        import re
        img = re.search(r'<img[^>]+src="([^"]+)"', html_data)
        if img:
            return img.group(1)
    except:
        pass
    return None


async def process_feed(session, app):
    for feed_url in FEEDS:
        raw = await fetch_url(session, feed_url)
        parsed = feedparser.parse(raw)

        for entry in parsed.entries[:5]:
            url = entry.link
            title = html.unescape(entry.title)

            if url in posted:
                continue

            posted.add(url)

            # summary
            summary = await summarize(title)

            # get image
            image = await get_image_from_article(session, url)

            text_msg = (
                f"<b>{title}</b>\n\n"
                f"{summary}\n\n"
                f"➜ <a href=\"{url}\">Источник</a>"
            )

            try:
                if image:
                    await app.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=image,
                        caption=text_msg,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await app.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=text_msg,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
            except Exception as e:
                print("SEND ERROR:", e)


async def news_loop(app):
    """Бесконечный цикл проверки новостей."""
    async with aiohttp.ClientSession() as session:
        while True:
            await process_feed(session, app)
            await asyncio.sleep(600)  # каждые 10 минут


async def admin_handler(update, context):
    if update.message.from_user.id != ADMIN_ID:
        return

    await update.message.reply_text("Проверяю новости вручную…")
    async with aiohttp.ClientSession() as session:
        await process_feed(session, context.application)


async def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), admin_handler))

    # запускаем цикл новостей
    asyncio.create_task(news_loop(app))

    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())

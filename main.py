import asyncio
import feedparser
import html
import logging
import aiohttp
from telegram import Bot
from telegram.constants import ParseMode
from datetime import datetime, timedelta

TELEGRAM_TOKEN = "YOUR_TOKEN"
CHANNEL_ID = "@your_channel"

# Российские источники
RSS_FEEDS = [
    "https://ria.ru/export/rss2/archive/index.xml",
    "https://habr.com/ru/rss/all/all/",
    "https://iz.ru/xml/rss/all.xml",
    "https://www.kommersant.ru/RSS/news.xml",
    "https://tass.ru/xml/rss2",
    "https://lenta.ru/rss",
    "https://rssexport.rbc.ru/rbcnews/news/20/full.rss",
    "https://www.vedomosti.ru/rss/news",
    "https://russian.rt.com/rss",
    "https://sputniknews.ru/export/rss2/archive/index.xml",
]

posted_links = set()
logging.basicConfig(level=logging.INFO)

bot = Bot(token=TELEGRAM_TOKEN)


async def fetch(session, url):
    try:
        async with session.get(url, timeout=10) as resp:
            return await resp.text()
    except Exception:
        return None


async def get_news():
    global posted_links

    async with aiohttp.ClientSession() as session:
        for feed_url in RSS_FEEDS:
            xml_data = await fetch(session, feed_url)
            if not xml_data:
                continue

            feed = feedparser.parse(xml_data)

            for entry in feed.entries[:5]:
                link = entry.get("link")
                if not link or link in posted_links:
                    continue

                posted_links.add(link)

                title = html.escape(entry.get("title", ""))
                summary = html.escape(entry.get("summary", ""))

                # Ищем картинку в enclosure
                img_url = None
                if "media_content" in entry:
                    try:
                        img_url = entry.media_content[0]["url"]
                    except:
                        pass
                if "media_thumbnail" in entry and not img_url:
                    try:
                        img_url = entry.media_thumbnail[0]["url"]
                    except:
                        pass

                text = f"<b>{title}</b>\n\n{summary}\n\n<a href='{link}'>Источник</a>"

                try:
                    if img_url:
                        await bot.send_photo(
                            chat_id=CHANNEL_ID,
                            photo=img_url,
                            caption=text,
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        await bot.send_message(
                            chat_id=CHANNEL_ID,
                            text=text,
                            parse_mode=ParseMode.HTML
                        )

                    logging.info(f"Posted: {title}")

                except Exception as e:
                    logging.error(f"Error posting: {e}")

                await asyncio.sleep(2)


async def scheduler():
    while True:
        await get_news()
        await asyncio.sleep(300)  # каждые 5 минут


async def main():
    await scheduler()


if __name__ == "__main__":
    asyncio.run(main())

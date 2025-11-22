import os
import asyncio
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

bot = Bot(token=TOKEN)

# =====================================================
# 🔥 ПРИВЕТСТВЕННОЕ СООБЩЕНИЕ (ВСТАВЛЕНО ВЕРНО!)
# =====================================================
async def send_welcome_message():
    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text="🔥 Привет! Канал теперь полностью подключён!\n\nAI News Channel Bot работает автоматически 🤖"
        )
    except Exception as e:
        print("Ошибка при отправке приветственного сообщения:", e)

# =====================================================


async def send_news():
    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text="📰 Новость: тестовое автоматическое обновление!"
        )
    except Exception as e:
        print("Ошибка при отправке новости:", e)


async def main():
    scheduler = AsyncIOScheduler()

    # отправляем тестовый пост каждые 30 минут
    scheduler.add_job(send_news, "interval", minutes=30)

    scheduler.start()

    # отправляем приветственный пост ПРИ ЗАПУСКЕ
    await send_welcome_message()

    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())


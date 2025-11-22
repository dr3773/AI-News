import os
from datetime import time
from zoneinfo import ZoneInfo

from telegram.ext import Application, ContextTypes

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@AI_News3773")

if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")


def build_morning_digest() -> str:
    return (
        "🧠 AI Daily — утренний дайджест\n\n"
        "• Новость 1: Краткое обновление из мира ИИ.\n"
        "• Новость 2: Запуск новой модели или сервиса.\n"
        "• Новость 3: Исследование или тренд.\n\n"
        "Больше — в течение дня на @AI_News3773"
    )


def build_tool_post() -> str:
    return (
        "🧰 AI Tool of the Day\n\n"
        "Сегодняшний сервис: Название ИИ-сервиса.\n"
        "Что делает: краткое описание пользы.\n"
        "Для кого: предприниматели, врачи, трейдеры и т.д.\n"
    )


def build_afternoon_post() -> str:
    return (
        "🤖 ИИ в реальном мире\n\n"
        "Кейс дня: пример использования ИИ в бизнесе, медицине, образовании или госструктурах.\n"
        "Такие кейсы помогают понять, как ИИ меняет профессию и рынок.\n"
    )


def build_crypto_post() -> str:
    return (
        "💹 AI + Crypto\n\n"
        "Обсуждаем пересечение ИИ и криптовалют.\n"
        "Позже сюда можно будет встроить ваши реферальные ссылки.\n"
    )


def build_evening_digest() -> str:
    return (
        "📊 Вечерний дайджест\n\n"
        "• Итог 1: главное событие дня в ИИ.\n"
        "• Итог 2: важный тренд или прогноз.\n"
        "• Итог 3: инструмент или идея.\n"
        "Спасибо, что с нами — @AI_News3773"
    )


async def job_morning(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=CHANNEL_ID, text=build_morning_digest())


async def job_tool(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=CHANNEL_ID, text=build_tool_post())


async def job_afternoon(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=CHANNEL_ID, text=build_afternoon_post())


async def job_crypto(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=CHANNEL_ID, text=build_crypto_post())


async def job_evening(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=CHANNEL_ID, text=build_evening_digest())


def main():
    app = Application.builder().token(TOKEN).build()

    tz = ZoneInfo("Asia/Dushanbe")
    jq = app.job_queue

    jq.run_daily(job_morning, time=time(9, 0, tzinfo=tz))
    jq.run_daily(job_tool, time=time(12, 0, tzinfo=tz))
    jq.run_daily(job_afternoon, time=time(15, 0, tzinfo=tz))
    jq.run_daily(job_crypto, time=time(18, 0, tzinfo=tz))
    jq.run_daily(job_evening, time=time(21, 0, tzinfo=tz))

    # run_polling сам создаёт и управляет event loop
    app.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()

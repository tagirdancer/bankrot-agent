import os, asyncio, telegram

async def test():
    bot = telegram.Bot(token=os.getenv('TELEGRAM_TOKEN'))
    await bot.send_message(
        chat_id=os.getenv('TELEGRAM_CHAT_ID'),
        text='✅ Тест работает! Агент v8.0 готов.'
    )

asyncio.run(test())

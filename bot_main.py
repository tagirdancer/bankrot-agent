import asyncio, os, re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
load_dotenv()
TG_TOKEN  = os.getenv("TELEGRAM_TOKEN")
TG_CHAT   = os.getenv("TELEGRAM_CHAT_ID")
GROQ_KEY  = os.getenv("GROQ_API_KEY")
GROQ_URL  = "https://api.groq.com/openai/v1/chat/completions"

analysis_running = False

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Запустить полный анализ", callback_data="run_full")],
        [
            InlineKeyboardButton("🏠 Квартиры",   callback_data="run_квартира"),
            InlineKeyboardButton("🏢 Коммерция",  callback_data="run_коммерция"),
        ],
        [
            InlineKeyboardButton("🏡 Дома",        callback_data="run_дом"),
            InlineKeyboardButton("🌱 Земля",        callback_data="run_земля"),
        ],
        [
            InlineKeyboardButton("🚗 Авто",         callback_data="run_авто"),
            InlineKeyboardButton("⚡ Горячие 9+",   callback_data="run_hot"),
        ],
        [InlineKeyboardButton("📊 Статус",          callback_data="status")],
    ])

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Банкротный агент*\n\n"
        "Выберите что анализировать:",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 Меню:", reply_markup=main_menu())

async def ask_expert(question: str) -> str:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "user", "content":
                        f"Ты эксперт по банкротным торгам России. "
                        f"Отвечай кратко и конкретно.\n\n{question}"}],
                    "max_tokens": 500,
                    "temperature": 0.5,
                }
            )
            data = resp.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Ошибка: {e}"
    return "Нет ответа"

async def trigger_github_action(category: str = "все") -> bool:
    import httpx
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.github.com/repos/tagirdancer/bankrot-agent/actions/workflows/agent.yml/dispatches",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                json={"ref": "main", "inputs": {"category": category}}
            )
            return resp.status_code == 204
    except:
        return False

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global analysis_running
    q    = update.callback_query
    await q.answer()
    data = q.data
    if data == "status":
        status = "🔄 Анализ запущен" if analysis_running else "✅ Готов к работе"
        await q.edit_message_text(
            f"📊 *Статус бота*\n\n{status}\n\nДайджест приходит каждый день в 08:00",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
        return
    if data.startswith("run_"):
        cat = data.replace("run_", "")
        cat_names = {
            "full":"полный анализ всех категорий","квартира":"квартиры",
            "коммерция":"коммерческая недвижимость","дом":"дома и дачи",
            "земля":"земельные участки","авто":"транспорт","hot":"горячие лоты балл 9+",
        }
        label = cat_names.get(cat, cat)
        github_cat = "все" if cat in ("full","hot") else cat
        ok = await trigger_github_action(github_cat)
        if ok:
            analysis_running = True
            await q.edit_message_text(
                f"🚀 *Запущен анализ: {label}*\n\n"
                f"⏳ Займёт ~30 минут\n"
                f"Результаты придут в этот чат автоматически\n\n"
                f"_Можете закрыть приложение — агент работает в облаке_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Меню", callback_data="back_menu")
                ]])
            )
        else:
            await q.edit_message_text(
                f"⚠️ *Для запуска нужен GITHUB TOKEN*\n\n"
                f"Добавьте GITHUB TOKEN в переменные Railway\n"
                f"или запустите вручную:\ngithub.com/tagirdancer/bankrot-agent/actions",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Меню", callback_data="back_menu")
                ]])
            )
    elif data == "back_menu":
        await q.edit_message_text("📱 Меню:", reply_markup=main_menu())

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if "tbankrot.ru/item" in text or re.search(r'id=\d+', text):
        await update.message.reply_text("⏳ Анализирую лот...")
        url_m = re.search(r'https?://\S+', text)
        url   = url_m.group() if url_m else ""
        if not url:
            id_m = re.search(r'id=(\d+)', text)
            url  = f"https://tbankrot.ru/item?id={id_m.group(1)}" if id_m else ""
        if url:
            answer = await ask_expert(
                f"Проанализируй лот: {url}\n"
                f"Дай краткую оценку: стоит ли смотреть, риски, что проверить."
            )
            await update.message.reply_text(answer, reply_markup=main_menu())
        return
    if len(text) > 3:
        msg = await update.message.reply_text("💭 Думаю...")
        answer = await ask_expert(text)
        await msg.edit_text(answer, reply_markup=main_menu())

def run():
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(CommandHandler("m",     cmd_menu))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    run()

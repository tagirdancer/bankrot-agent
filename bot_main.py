# v2.0 — Bankrot Bot with regions
import asyncio, os, re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
load_dotenv()

TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")
GROQ_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GH_TOKEN = os.getenv("GITHUB_TOKEN", "")

# Все регионы Т-Банкрот
REGIONS = {
    "🏛 Москва":          "moskva",
    "🌆 Московская обл.": "moskovskaya-oblast",
    "🌊 Санкт-Петербург": "sankt-peterburg",
    "🌿 Ленинградская":   "leningradskaya-oblast",
    "☀️ Краснодар":       "krasnodar",
    "🏔 Екатеринбург":    "ekaterinburg",
    "❄️ Новосибирск":     "novosibirsk",
    "🌲 Татарстан":       "tatarstan",
    "💎 Башкортостан":    "bashkortostan",
    "🏙 Ростов-на-Дону":  "rostov-na-donu",
    "🌸 Самара":          "samara",
    "🎯 Нижний Новгород": "nizhegorodskaya-oblast",
    "🌊 Волгоград":       "volgogradskaya-oblast",
    "🏔 Красноярск":      "krasnoyarskiy-kray",
    "🌏 Все регионы":     "all",
}

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Запустить анализ", callback_data="menu_run")],
        [InlineKeyboardButton("🗺 Выбрать регион",   callback_data="menu_regions")],
        [
            InlineKeyboardButton("🏠 Квартиры",  callback_data="run_квартира"),
            InlineKeyboardButton("🏢 Коммерция", callback_data="run_коммерция"),
        ],
        [
            InlineKeyboardButton("🏡 Дома",  callback_data="run_дом"),
            InlineKeyboardButton("🌱 Земля", callback_data="run_земля"),
        ],
        [
            InlineKeyboardButton("🚗 Авто",        callback_data="run_авто"),
            InlineKeyboardButton("⚡ Горячие 9+",  callback_data="run_hot"),
        ],
        [InlineKeyboardButton("📊 Статус", callback_data="status")],
    ])

def regions_menu():
    rows = []
    items = list(REGIONS.items())
    for i in range(0, len(items), 2):
        row = []
        for label, code in items[i:i+2]:
            row.append(InlineKeyboardButton(label, callback_data=f"region_{code}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("↩️ Назад", callback_data="back_menu")])
    return InlineKeyboardMarkup(rows)

async def trigger_workflow(category: str = "все", region: str = "moskva") -> bool:
    import httpx
    if not GH_TOKEN:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.github.com/repos/tagirdancer/bankrot-agent/actions/workflows/agent.yml/dispatches",
                headers={
                    "Authorization": f"token {GH_TOKEN}",
                    "Accept": "application/vnd.github.v3+json",
                },
                json={"ref": "main", "inputs": {"category": category}}
            )
            return resp.status_code == 204
    except:
        return False

async def ask_expert(question: str) -> str:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"token {GROQ_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "user", "content":
                        f"Ты эксперт по банкротным торгам России. Отвечай кратко.\n\n{question}"}],
                    "max_tokens": 400, "temperature": 0.5,
                }
            )
            data = resp.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Ошибка: {e}"
    return "Нет ответа"

# Хранилище выбранного региона
user_region = {}

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Банкротный агент*\n\n"
        "Анализирует лоты с Т-Банкрот и отправляет дайджест\n\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 Меню:", reply_markup=main_menu())

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data
    chat = str(q.message.chat_id)

    if data == "back_menu":
        await q.edit_message_text("📱 Меню:", reply_markup=main_menu())

    elif data == "menu_regions":
        cur = user_region.get(chat, "moskva")
        cur_name = next((k for k,v in REGIONS.items() if v == cur), cur)
        await q.edit_message_text(
            f"🗺 *Выбор региона*\n\nТекущий: *{cur_name}*\n\nВыберите регион для следующего анализа:",
            parse_mode="Markdown",
            reply_markup=regions_menu()
        )

    elif data.startswith("region_"):
        code = data[7:]
        user_region[chat] = code
        name = next((k for k,v in REGIONS.items() if v == code), code)
        await q.edit_message_text(
            f"✅ Регион выбран: *{name}*\n\nТеперь запускайте анализ — он будет по этому региону.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚀 Запустить анализ", callback_data="menu_run"),
                InlineKeyboardButton("↩️ Меню", callback_data="back_menu"),
            ]])
        )

    elif data == "menu_run" or data == "status":
        region = user_region.get(chat, "moskva")
        region_name = next((k for k,v in REGIONS.items() if v == region), region)
        if data == "status":
            await q.edit_message_text(
                f"📊 *Статус*\n\nРегион: *{region_name}*\nДайджест: каждый день в 08:00\nАгент: работает в GitHub Actions",
                parse_mode="Markdown",
                reply_markup=main_menu()
            )
        else:
            await q.edit_message_text(
                f"🚀 *Запустить анализ*\n\nРегион: *{region_name}*\n\nВыберите категорию:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📦 Все категории", callback_data="run_full")],
                    [
                        InlineKeyboardButton("🏠 Квартиры",  callback_data="run_квартира"),
                        InlineKeyboardButton("🏢 Коммерция", callback_data="run_коммерция"),
                    ],
                    [
                        InlineKeyboardButton("🏡 Дома",  callback_data="run_дом"),
                        InlineKeyboardButton("🌱 Земля", callback_data="run_земля"),
                    ],
                    [
                        InlineKeyboardButton("🚗 Авто",       callback_data="run_авто"),
                        InlineKeyboardButton("⚡ Горячие 9+", callback_data="run_hot"),
                    ],
                    [InlineKeyboardButton("↩️ Назад", callback_data="back_menu")],
                ])
            )

    elif data.startswith("run_"):
        cat = data[4:]
        region = user_region.get(chat, "moskva")
        region_name = next((k for k,v in REGIONS.items() if v == region), region)
        cat_names = {
            "full":"все категории","квартира":"квартиры","коммерция":"коммерция",
            "дом":"дома","земля":"земля","авто":"авто","hot":"горячие 9+",
        }
        label = cat_names.get(cat, cat)
        github_cat = "все" if cat in ("full","hot") else cat

        ok = await trigger_workflow(github_cat, region)
        if ok:
            await q.edit_message_text(
                f"🚀 *Анализ запущен!*\n\n"
                f"📂 Категория: *{label}*\n"
                f"📍 Регион: *{region_name}*\n\n"
                f"⏳ ~30 минут — результаты придут в этот чат\n"
                f"_Агент работает в GitHub Actions_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Меню", callback_data="back_menu")
                ]])
            )
        else:
            await q.edit_message_text(
                f"⚠️ *GITHUB TOKEN не настроен*\n\n"
                f"Зайдите вручную:\n"
                f"github.com/tagirdancer/bankrot-agent/actions\n"
                f"→ Run workflow",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Меню", callback_data="back_menu")
                ]])
            )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if "tbankrot.ru/item" in text or re.search(r'id=\d+', text):
        await update.message.reply_text("⏳ Анализирую лот...")
        url_m = re.search(r'https?://\S+', text)
        url = url_m.group() if url_m else ""
        if not url:
            id_m = re.search(r'id=(\d+)', text)
            url = f"https://tbankrot.ru/item?id={id_m.group(1)}" if id_m else ""
        if url:
            answer = await ask_expert(f"Проанализируй лот: {url}\nДай оценку: риски, что проверить.")
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
    print("Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    run()

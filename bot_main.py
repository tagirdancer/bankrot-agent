# v2.0 — Bankrot Bot with regions
import asyncio, os, re, logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot_main")

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
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        print("GITHUB_TOKEN not found in environment")
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.github.com/repos/tagirdancer/bankrot-agent/actions/workflows/agent.yml/dispatches",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.v3+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"ref": "main", "inputs": {"category": category}}
            )
            print(f"GitHub API status: {resp.status_code}")
            return resp.status_code == 204
    except Exception as e:
        print(f"GitHub API error: {e}")
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


async def deep_analysis(lot_id: str, facts: dict = None) -> str:
    import httpx
    facts = facts or {}
    if not GROQ_KEY:
        log.error("deep_analysis: GROQ_API_KEY отсутствует в окружении (проверьте Railway -> Variables)")
        return ("\u26a0\ufe0f На сервере не настроен ключ GROQ_API_KEY.\n"
                "Добавьте переменную GROQ_API_KEY в Railway -> сервис web -> Variables.")
    url = f"https://tbankrot.ru/item?id={lot_id}"

    # Берём ТОЛЬКО реальные числа лота (пришли из дайджеста). Ничего не выдумываем.
    def fmt_money(v):
        try:
            v = float(v)
            if v >= 1_000_000:
                return f"{v / 1_000_000:.1f} млн ₽"
            if v > 0:
                return f"{int(v):,} ₽".replace(",", " ")
        except Exception:
            pass
        return None

    price_s = fmt_money(facts.get("price_raw"))
    mkt_s = fmt_money(facts.get("market_raw"))
    disc = facts.get("disc")
    parts = facts.get("parts")

    known = [f"Ссылка на карточку торгов: {url}"]
    known.append(f"Цена лота: {price_s}" if price_s else "Цена лота: НЕТ ДАННЫХ")
    if mkt_s and disc and str(disc) not in ("0", "?", ""):
        known.append(f"Ориентир рыночной цены (из дайджеста): {mkt_s}")
        known.append(f"Дисконт к рынку (из дайджеста): {disc}%")
    else:
        known.append("Рыночная цена и дисконт: НЕТ ДАННЫХ")
    if parts not in (None, "", "None"):
        known.append(f"Количество заявок: {parts}")
    else:
        known.append("Количество заявок: НЕТ ДАННЫХ")
    known_block = "\n".join("- " + k for k in known)

    prompt = f"""Ты помогаешь инвестору проверить лот с банкротных торгов в России.

КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА (нарушать строго запрещено):
- У тебя НЕТ доступа к Росреестру, ФССП, ЕГРН и полной карточке торгов.
- Используй ТОЛЬКО данные из блока «ИЗВЕСТНЫЕ ДАННЫЕ» ниже.
- ЗАПРЕЩЕНО выдумывать конкретные числа: сумму ареста, долги ЖКХ, налоги,
  кадастровую стоимость, число прописанных, количество заявок, рыночную цену —
  если их НЕТ в известных данных. Не придумывай «правдоподобные» числа.
- Если данных для раздела нет — пиши: «НЕТ ДАННЫХ — проверить самостоятельно:
  [где именно проверить]».
- Рыночную цену и дисконт бери ТОЛЬКО из известных данных и НЕ пересчитывай.

ИЗВЕСТНЫЕ ДАННЫЕ:
{known_block}

Сделай ЧЕК-ЛИСТ ПРОВЕРКИ (не отчёт с готовыми фактами) строго по разделам:

✅ ЧТО ТОЧНО ИЗВЕСТНО ИЗ КАРТОЧКИ
- только факты из «ИЗВЕСТНЫЕ ДАННЫЕ», без додумывания.

🔎 ЧТО ОБЯЗАТЕЛЬНО ПРОВЕРИТЬ САМОМУ (И ГДЕ)
- обременения, аресты, залоги — ЕГРН (Росреестр) и банк данных ФССП по адресу/кадастру;
- долги ЖКХ и капремонт — в управляющей компании / по квитанциям;
- прописанные лица — выписка из домовой книги;
- условия торгов, задаток, шаг цены, дедлайн — карточка лота на площадке.
Для каждого пункта укажи, ГДЕ это проверить.

❓ ВОПРОСЫ К ОБЪЕКТУ
- что спросить у организатора торгов и на что обратить внимание.

💡 СТРАТЕГИИ И ВЕРДИКТ (оценочно, требует подтверждения данных)
- 1-2 стратегии и короткий вердикт с пометкой «оценочно».
- не называй конкретных сумм прибыли, если нет цен.

Пиши по-русски, кратко, по пунктам. Без выдуманных чисел."""

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1500,
                    "temperature": 0.3,
                }
            )
            if resp.status_code != 200:
                log.error("deep_analysis: Groq статус %s, тело: %s",
                          resp.status_code, resp.text[:500])
                return (f"\u26a0\ufe0f Анализ временно недоступен (Groq ответил {resp.status_code}).\n"
                        f"Причина записана в логи Railway. Попробуйте позже.")
            data = resp.json()
            choices = data.get("choices")
            if choices:
                return choices[0]["message"]["content"]
            log.error("deep_analysis: в ответе Groq нет choices: %s", str(data)[:500])
    except Exception:
        log.exception("full_analysis failed: ошибка запроса к Groq")
        return "\u26a0\ufe0f Не удалось получить анализ (ошибка запроса). Подробности в логах Railway."
    return "Не удалось получить анализ. Попробуйте позже."

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
    data = q.data

    if data.startswith("deep_"):
        payload = data[5:]
        bits = payload.split("_")
        lot_id = bits[0]
        facts = {}
        if len(bits) >= 5:
            facts = {"price_raw": bits[1], "market_raw": bits[2],
                     "disc": bits[3], "parts": bits[4]}
        try:
            await q.answer("Анализирую...")
            await q.message.reply_text("🔍 Готовлю чек-лист проверки лота, подождите ~30 секунд...")
            analysis = await deep_analysis(lot_id, facts)
            try:
                await q.message.reply_text(analysis, parse_mode="Markdown")
            except Exception:
                log.exception("full_analysis failed: ошибка отправки с Markdown, шлю без форматирования")
                await q.message.reply_text(analysis)
        except Exception:
            log.exception("full_analysis failed")
            await q.message.reply_text("\u26a0\ufe0f Не удалось получить анализ (внутренняя ошибка). Подробности в логах Railway.")
        return

    await q.answer()
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
